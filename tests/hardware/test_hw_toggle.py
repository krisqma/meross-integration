# -*- coding: utf-8 -*-
"""Test sprzętowy 5: pojedynczy cykl włącz/wyłącz z odczytem mocy — TYLKO ZA JAWNĄ ZGODĄ.

To jedyny test w tym katalogu, który cokolwiek zmienia w prawdziwym mieszkaniu. Za gniazdkiem
może być lampa, grzejnik albo sprzęt, którego nie wolno wyłączyć, więc:

* domyślnie się POMIJA — trzeba ustawić `HW_ALLOW_SWITCHING=1`,
* nie zgaduje urządzenia — trzeba wskazać `HW_TARGET_UUID` (UUID albo IP z inwentarza)
  i opcjonalnie `HW_TARGET_CHANNEL` (domyślnie 0),
* stan początkowy jest odczytany PRZED zmianą i przywracany w `finally`, także gdy asercja
  padnie w środku,
* jeden cykl, jedno krótkie oczekiwanie — żadnych pętli przełączania.

Uruchomienie:

    HW_ALLOW_SWITCHING=1 HW_TARGET_UUID=2103163085044890845648e1e962e3a6 HW_TARGET_CHANNEL=0 \\
        ./dot.sh test hw -k przelaczanie

Przywracanie stanu i odczyty w `finally` idą przez surowy `rpc()`, a nie przez `ask()`:
`ask()` woła `pytest.skip`/`pytest.fail`, a wyjątek rzucony w `finally` przysłoniłby
prawdziwą przyczynę porażki.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hw import (  # noqa: E402
    NS_ALL,
    NS_ELECTRICITY,
    NS_TOGGLEX,
    UNREACHABLE_HINT,
    DeviceSpec,
    Unreachable,
    ability_namespaces,
    channel_states,
    error_of,
    normalize_electricity,
    report,
    require_key,
    require_switching_consent,
    rpc,
)

pytestmark = pytest.mark.hardware

# Czas na kliknięcie przekaźnika i aktualizację stanu w gniazdku. Krótko i tylko raz.
SETTLE_S = float(os.environ.get("HW_SETTLE_S", "2"))


def _read_channel(spec: DeviceSpec, key: str, channel: int) -> bool:
    envelope = rpc(spec.ip, NS_ALL, key)
    error = error_of(envelope)
    if error is not None:
        raise RuntimeError(f"{spec.ip}: {NS_ALL} zwróciło błąd {error}")
    states = channel_states(envelope.get("payload") or {})
    if channel not in states:
        raise RuntimeError(f"{spec.ip}: gniazdko nie ma kanału {channel} (są: {sorted(states)})")
    return states[channel]


def _set_channel(spec: DeviceSpec, key: str, channel: int, state: bool) -> None:
    envelope = rpc(
        spec.ip,
        NS_TOGGLEX,
        key,
        method="SET",
        payload={"togglex": {"channel": channel, "onoff": int(state)}},
    )
    error = error_of(envelope)
    if error is not None:
        raise RuntimeError(f"{spec.ip}: SET {NS_TOGGLEX} kanał {channel} -> {int(state)} zwróciło błąd {error}")


def _read_power_w(spec: DeviceSpec, key: str, channel: int) -> float | None:
    envelope = rpc(spec.ip, NS_ELECTRICITY, key, payload={"channel": channel})
    if error_of(envelope) is not None:
        return None
    raw = (envelope.get("payload") or {}).get("electricity") or {}
    return normalize_electricity(raw)["power_w"]


def test_przelaczanie_jednego_kanalu_z_przywroceniem_stanu() -> None:
    key = require_key()
    spec, channel = require_switching_consent()

    try:
        measures_power = NS_ELECTRICITY in ability_namespaces(spec, key)
        initial = _read_channel(spec, key, channel)
    except Unreachable as exc:
        pytest.skip(f"{exc}\n{UNREACHABLE_HINT}")
    except RuntimeError as exc:
        pytest.fail(str(exc))

    target = not initial
    power_before = _read_power_w(spec, key, channel) if measures_power else None
    observed = None
    power_after = None
    restored: bool | None = None
    restore_error: BaseException | None = None

    try:
        _set_channel(spec, key, channel, target)
        time.sleep(SETTLE_S)
        observed = _read_channel(spec, key, channel)
        if measures_power:
            power_after = _read_power_w(spec, key, channel)
        assert observed is target, (
            f"{spec.ip}: po SET ToggleX kanał {channel} miał być "
            f"{'ON' if target else 'OFF'}, a {NS_ALL} pokazuje {'ON' if observed else 'OFF'}"
        )
    finally:
        # Przywrócenie stanu jest ważniejsze niż wynik testu — dlatego tutaj i dlatego
        # z własnym łapaniem wyjątków (żeby nie przysłonić przyczyny porażki wyżej).
        try:
            _set_channel(spec, key, channel, initial)
            time.sleep(SETTLE_S)
            restored = _read_channel(spec, key, channel)
        except (Unreachable, RuntimeError, OSError) as exc:
            restore_error = exc

        lines = [
            f"urządzenie:        {spec.uuid} ({spec.ip}), kanał {channel}",
            f"stan początkowy:   {'ON' if initial else 'OFF'}",
            f"przełączono na:    {'ON' if target else 'OFF'}",
            f"odczyt po zmianie: {'ON' if observed else 'OFF'}" if observed is not None else "odczyt po zmianie: (brak)",
            f"moc przed:         {power_before} W" if measures_power else "moc: urządzenie nie mierzy",
            f"moc po zmianie:    {power_after} W" if measures_power else "",
            f"stan przywrócony:  {'ON' if restored else 'OFF'}" if restored is not None else "stan przywrócony: NIE UDAŁO SIĘ",
        ]
        report("przełączanie", [line for line in lines if line])
        if restore_error is not None or (restored is not None and restored is not initial):
            print(
                f"!!! UWAGA: nie udało się przywrócić kanału {channel} gniazdka {spec.ip} do stanu "
                f"{'ON' if initial else 'OFF'} ({restore_error}). Sprawdź gniazdko ręcznie albo z panelu.",
                file=sys.stderr,
                flush=True,
            )

    assert restore_error is None, f"Przywracanie stanu nie udało się: {restore_error}"
    assert restored is initial, (
        f"{spec.ip}: kanał {channel} został w stanie {'ON' if restored else 'OFF'}, "
        f"a miał wrócić do {'ON' if initial else 'OFF'}"
    )
