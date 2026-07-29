"""Parser tematów Homie 4.0 na model z CONTRACT.md §3 (`GET /api/devices`).

Klasa `HomieState` jest czysto obliczeniowa: nie zna MQTT, nie ma wejścia/wyjścia i nie
używa czasu. Dostaje strumień par (temat, payload) — dokładnie w tej postaci, w jakiej
przychodzą retainowane wiadomości z brokera — i buduje z nich model urządzeń.

Założenia wynikające z CONTRACT.md §2:

* `<dev>` jest nieprzezroczystym identyfikatorem (nie zakładamy formatu UUID),
* kolejność nadchodzących wiadomości jest dowolna: atrybuty (`$settable`, `$nodes`, ...)
  potrafią przyjść po wartościach, więc parser nie może wymagać żadnej kolejności,
* węzły opcjonalne (`energy*`, `electricity*`, `dnd`) mogą w ogóle nie istnieć,
* nieznany węzeł czy pokaleczony temat nie może wywalić parsera.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

#: Wartość `$settable`, gdy atrybut jeszcze nie przyszedł. Most zawsze tworzy
#: `switch/power` z setterem, więc brak atrybutu traktujemy jako "sterowalne" —
#: inaczej panel byłby martwy do pierwszego pełnego odświeżenia atrybutów.
DEFAULT_SETTABLE = True


def _parse_float(raw: Optional[str]) -> Optional[float]:
    """Zamienia payload na float; `None` dla braku wartości i dla śmieci."""
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _parse_bool(raw: Optional[str]) -> Optional[bool]:
    """`HomieBooleanProperty.serialize` daje dosłownie `true`/`false`."""
    if raw is None:
        return None
    if raw == "true":
        return True
    if raw == "false":
        return False
    return None


def split_channel(node: str) -> Tuple[str, int]:
    """`switch` -> ("switch", 0), `switch-2` -> ("switch", 2).

    Dla nazw bez poprawnego sufiksu numerycznego zwraca całą nazwę i kanał 0,
    żeby nieznany węzeł nie wywracał parsera.
    """
    base, sep, suffix = node.rpartition("-")
    if sep and base and suffix.isdigit():
        return base, int(suffix)
    return node, 0


def channel_node(base: str, channel: int) -> str:
    """Odwrotność `split_channel` — nazwa węzła dla danego kanału."""
    return base if channel == 0 else f"{base}-{channel}"


@dataclass
class Property:
    """Własność Homie: wartość plus jej atrybuty `$...`."""

    value: Optional[str] = None
    attrs: Dict[str, str] = field(default_factory=dict)


@dataclass
class Node:
    """Węzeł Homie: atrybuty `$...` plus własności."""

    attrs: Dict[str, str] = field(default_factory=dict)
    props: Dict[str, Property] = field(default_factory=dict)

    def value(self, prop: str) -> Optional[str]:
        entry = self.props.get(prop)
        return entry.value if entry else None

    def prop_attr(self, prop: str, attr: str) -> Optional[str]:
        entry = self.props.get(prop)
        return entry.attrs.get(attr) if entry else None


@dataclass
class Device:
    """Urządzenie Homie: atrybuty `$...` plus węzły."""

    attrs: Dict[str, str] = field(default_factory=dict)
    nodes: Dict[str, Node] = field(default_factory=dict)


class HomieState:
    """Akumuluje stan z tematów Homie i wystawia go w kształcie z kontraktu."""

    def __init__(self, prefix: str = "homie", cloud_names: Optional[Dict[str, str]] = None) -> None:
        self.prefix = prefix
        self.devices: Dict[str, Device] = {}
        self._cloud_names: Dict[str, str] = cloud_names or {}

    # ------------------------------------------------------------------ wejście

    def ingest(self, topic: str, payload: Optional[str]) -> bool:
        """Wchłania jedną wiadomość. Zwraca `True`, gdy model faktycznie się zmienił.

        Pusty payload na retainowanym temacie to w MQTT usunięcie wartości —
        obsługujemy to jako skasowanie atrybutu/wartości, bez usuwania urządzenia
        (retained `$state` bywa czyszczone przy sprzątaniu brokera).

        Cicho ignorujemy wszystko, czego nie rozumiemy: obcy prefiks, tematy
        wieloznaczników, `/set` (to nasze własne polecenia wracające z subskrypcji)
        i wszelkie zbyt głębokie ścieżki.
        """
        if not isinstance(topic, str) or not topic:
            return False
        parts = topic.split("/")
        if len(parts) < 3 or parts[0] != self.prefix:
            return False
        dev_id = parts[1]
        rest = parts[2:]
        if not dev_id or any(not p for p in rest):
            return False
        if payload is not None and not isinstance(payload, str):
            return False

        # Atrybuty urządzenia: `$state`, ale też dwuczłonowe `$fw/name`.
        if rest[0].startswith("$"):
            if len(rest) > 2:
                return False
            return self._set_attr(self._device(dev_id).attrs, "/".join(rest), payload, dev_id)

        node_name = rest[0]
        sub = rest[1:]

        if len(sub) == 1:
            if sub[0].startswith("$"):
                node = self._node(dev_id, node_name)
                changed = self._set_attr(node.attrs, sub[0], payload, dev_id)
                if sub[0] == "$properties" and payload:
                    for prop_name in _split_list(payload):
                        node.props.setdefault(prop_name, Property())
                return changed
            # Wartość własności: homie/<dev>/<node>/<prop>
            node = self._node(dev_id, node_name)
            prop = node.props.setdefault(sub[0], Property())
            if payload is None or payload == "":
                if prop.value is None:
                    return False
                prop.value = None
                return True
            if prop.value == payload:
                return False
            prop.value = payload
            return True

        if len(sub) == 2 and sub[1].startswith("$"):
            node = self._node(dev_id, node_name)
            prop = node.props.setdefault(sub[0], Property())
            return self._set_attr(prop.attrs, sub[1], payload, dev_id)

        # `<node>/<prop>/set` oraz cokolwiek głębszego — nie nasza sprawa.
        return False

    def ingest_many(self, messages) -> bool:
        """Wchłania iterowalne `(topic, payload)`. `True`, gdy cokolwiek się zmieniło."""
        changed = False
        for topic, payload in messages:
            if self.ingest(topic, payload):
                changed = True
        return changed

    # ------------------------------------------------------------------- wyjście

    def snapshot(self) -> dict:
        """Model w kształcie `GET /api/devices` (CONTRACT.md §3)."""
        return {"devices": [self._device_snapshot(dev_id) for dev_id in sorted(self.devices)]}

    # ----------------------------------------------------------------- wewnętrzne

    def _device(self, dev_id: str) -> Device:
        return self.devices.setdefault(dev_id, Device())

    def _node(self, dev_id: str, node_name: str) -> Node:
        return self._device(dev_id).nodes.setdefault(node_name, Node())

    def _set_attr(
        self, target: Dict[str, str], key: str, payload: Optional[str], dev_id: str
    ) -> bool:
        if payload is None or payload == "":
            return target.pop(key, None) is not None
        if target.get(key) == payload:
            # `$nodes` może dojechać przed pierwszą wartością — placeholdery i tak dosypujemy.
            if key == "$nodes":
                return self._register_nodes(dev_id, payload)
            return False
        target[key] = payload
        if key == "$nodes":
            self._register_nodes(dev_id, payload)
        return True

    def _register_nodes(self, dev_id: str, payload: str) -> bool:
        """Zakłada puste węzły wymienione w `$nodes` (kafelek pojawia się od razu)."""
        device = self._device(dev_id)
        changed = False
        for name in _split_list(payload):
            if name not in device.nodes:
                device.nodes[name] = Node()
                changed = True
        return changed

    def _device_snapshot(self, dev_id: str) -> dict:
        device = self.devices[dev_id]
        attrs = device.attrs
        return {
            "id": dev_id,
            "name": self._cloud_names.get(dev_id) or attrs.get("$name"),
            "state": attrs.get("$state"),
            "mac": attrs.get("$mac"),
            "ip": attrs.get("$localip"),
            "model": attrs.get("$fw/name"),
            "fw": attrs.get("$fw/version"),
            "channels": self._channels(device),
        }

    def _channels(self, device: Device) -> List[dict]:
        channels: List[Tuple[int, dict]] = []
        for node_name, node in device.nodes.items():
            base, channel = split_channel(node_name)
            if base != "switch":
                continue
            energy = device.nodes.get(channel_node("energy", channel))
            electricity = device.nodes.get(channel_node("electricity", channel))
            settable = _parse_bool(node.prop_attr("power", "$settable"))
            channels.append(
                (
                    channel,
                    {
                        "node": node_name,
                        "name": node.attrs.get("$name"),
                        "on": _parse_bool(node.value("power")),
                        "settable": DEFAULT_SETTABLE if settable is None else settable,
                        "power_w": _parse_float(electricity.value("power")) if electricity else None,
                        "voltage_v": _parse_float(electricity.value("voltage")) if electricity else None,
                        "current_a": _parse_float(electricity.value("current")) if electricity else None,
                        "energy_today_kwh": _parse_float(energy.value("daily")) if energy else None,
                    },
                )
            )
        channels.sort(key=lambda item: (item[0], item[1]["node"]))
        return [entry for _, entry in channels]


def _split_list(payload: str) -> List[str]:
    """Rozbija listę Homie po przecinku, odsiewając puste kawałki."""
    return [item.strip() for item in payload.split(",") if item.strip()]
