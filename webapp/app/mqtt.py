"""Styk z brokerem MQTT — jedyne miejsce w webappie, które wie o aiomqtt.

Publiczny interfejs jest zamrożony w CONTRACT.md §5: `connected`, `publish`,
`publish_set`, `snapshot`, `subscribe_updates`. `start`/`stop` dochodzą na potrzeby
lifespanu FastAPI (kontrakt pokazuje je w §5 jako `MqttHub(...)` tworzony w lifespanie,
ale nie nazywa metod cyklu życia).

Hub nie blokuje startu aplikacji: pętla połączeniowa leci w tle i sama się ponawia,
więc panel wstaje nawet wtedy, gdy broker jeszcze nie odpowiada — tylko z pustą listą
urządzeń i `mqtt: false` w `/api/health`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Callable, List, Optional

import aiomqtt

from .state import HomieState

log = logging.getLogger(__name__)

#: Odstęp między próbami połączenia z brokerem (sekundy).
RECONNECT_DELAY = 3.0


class MqttNotConnected(RuntimeError):
    """Podniesione, gdy ktoś chce publikować, a nie ma połączenia z brokerem."""


class MqttHub:
    """Trzyma połączenie z brokerem, lustro stanu Homie i listę obserwatorów."""

    def __init__(
        self,
        host: str = "mosquitto",
        port: int = 1883,
        prefix: str = "homie",
        client_id: str = "gniazdka-webapp",
        reconnect_delay: float = RECONNECT_DELAY,
    ) -> None:
        self.host = host
        self.port = port
        self.prefix = prefix
        self.client_id = client_id
        self.reconnect_delay = reconnect_delay
        self.connected: bool = False
        self._state = HomieState(prefix=prefix)
        self._client: Optional[aiomqtt.Client] = None
        self._task: Optional[asyncio.Task] = None
        self._stopping = False
        self._listeners: List[Callable[[], None]] = []

    # ------------------------------------------------------------- cykl życia

    async def start(self) -> None:
        """Odpala pętlę połączeniową w tle. Nie czeka na broker."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._run(), name="mqtt-hub")

    async def stop(self) -> None:
        """Zatrzymuje pętlę i czeka na jej zakończenie."""
        self._stopping = True
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        self.connected = False
        self._client = None

    # ------------------------------------------------------------- publikacja

    async def publish(self, topic: str, payload: str) -> None:
        """Publikuje bez retain, QoS 1 (most subskrybuje `/set` z QoS 1)."""
        client = self._client
        if client is None or not self.connected:
            raise MqttNotConnected(f"Brak połączenia z brokerem {self.host}:{self.port}")
        await client.publish(topic, payload, qos=1, retain=False)

    async def publish_set(self, dev: str, node: str, prop: str, value: str) -> None:
        """Publikuje na `homie/<dev>/<node>/<prop>/set` (CONTRACT.md §2.4)."""
        await self.publish(f"{self.prefix}/{dev}/{node}/{prop}/set", value)

    # ------------------------------------------------------------------- stan

    def snapshot(self) -> dict:
        """Aktualny stan w kształcie `GET /api/devices`."""
        return self._state.snapshot()

    def subscribe_updates(self, cb: Callable[[], None]) -> Callable[[], None]:
        """Rejestruje callback wołany po każdej zmianie stanu. Zwraca odrejestrowanie."""
        self._listeners.append(cb)

        def unsubscribe() -> None:
            try:
                self._listeners.remove(cb)
            except ValueError:
                pass

        return unsubscribe

    def ingest(self, topic: str, payload: Optional[str]) -> bool:
        """Wpuszcza wiadomość do modelu i powiadamia obserwatorów, gdy coś się zmieniło."""
        changed = self._state.ingest(topic, payload)
        if changed:
            self._notify()
        return changed

    # ------------------------------------------------------------ wewnętrzne

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # obserwator SSE nie może zabić pętli MQTT
                log.exception("Obserwator stanu podniósł wyjątek")

    async def _run(self) -> None:
        while not self._stopping:
            try:
                async with aiomqtt.Client(
                    hostname=self.host,
                    port=self.port,
                    identifier=self.client_id,
                ) as client:
                    self._client = client
                    self.connected = True
                    log.info("Połączono z brokerem %s:%s", self.host, self.port)
                    await client.subscribe(f"{self.prefix}/#", qos=1)
                    self._notify()  # zmiana `connected` jest widoczna w /api/health
                    async for message in client.messages:
                        self._handle_message(message)
            except asyncio.CancelledError:
                raise
            except aiomqtt.MqttError as exc:
                log.warning("Broker %s:%s niedostępny (%s)", self.host, self.port, exc)
            except Exception:
                log.exception("Nieoczekiwany błąd pętli MQTT")
            finally:
                self._client = None
                if self.connected:
                    self.connected = False
                    self._notify()
            if self._stopping:
                break
            await asyncio.sleep(self.reconnect_delay)

    def _handle_message(self, message) -> None:
        try:
            payload = message.payload
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode("utf-8", errors="replace")
            elif payload is None:
                payload = ""
            self.ingest(str(message.topic), payload)
        except Exception:  # jedna zepsuta wiadomość nie może zerwać połączenia
            log.exception("Nie udało się przetworzyć wiadomości MQTT")
