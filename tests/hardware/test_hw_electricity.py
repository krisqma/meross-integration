# -*- coding: utf-8 -*-
"""Test sprzętowy 4: odczyt pomiarów `Appliance.Control.Electricity`.

Test dotyczy tylko urządzeń, które zgłaszają tę zdolność — gniazdka bez pomiaru
(np. mss210) są pomijane, a nie oblewane.

Świadomie NIE sprawdzamy, czy `P = U * I`. Dokumentacja `meross_iot` wprost ostrzega, że
sprzęt raportuje te trzy wielkości niespójnie (osobne, niesynchronizowane pomiary), więc
taka asercja padałaby losowo na poprawnie działającym gniazdku. Sprawdzamy to, co ma sens:
napięcie w zakresie sieciowym (to weryfikuje też skalę jednostek) oraz obecność pól prądu
i mocy. Same wartości są raportowane.

    ./dot.sh test hw -k pomiary
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hw import (  # noqa: E402
    MAINS_VOLTAGE_MAX_V,
    MAINS_VOLTAGE_MIN_V,
    NS_ALL,
    NS_ELECTRICITY,
    DeviceSpec,
    ability_namespaces,
    ask,
    channel_states,
    each_device,
    normalize_electricity,
    report,
    require_key,
)

pytestmark = pytest.mark.hardware


@each_device
def test_pomiary_maja_sieciowe_napiecie_i_pelny_zestaw_pol(spec: DeviceSpec) -> None:
    key = require_key()

    if NS_ELECTRICITY not in ability_namespaces(spec, key):
        pytest.skip(f"{spec.ip}: urządzenie nie zgłasza {NS_ELECTRICITY} — nie ma pomiaru mocy")

    states = channel_states(ask(spec, NS_ALL, key))
    raw = (ask(spec, NS_ELECTRICITY, key, payload={"channel": 0}).get("electricity")) or {}
    reading = normalize_electricity(raw)

    report(
        f"Electricity {spec.ip}",
        [
            f"surowa odpowiedź:  {raw}",
            f"rozpoznana skala:  {reading['scale']}",
            f"napięcie:          {reading['voltage_v']} V",
            f"prąd:              {reading['current_a']} A",
            f"moc:               {reading['power_w']} W",
            f"kanał 0 jest:      {'ON' if states.get(0) else 'OFF'}",
            "uwaga: U, I i P to trzy osobne pomiary sprzętu — iloczyn U*I nie musi się"
            " zgadzać z P i nie jest tu sprawdzany",
        ],
    )

    assert "current" in raw, f"{spec.ip}: brak pola 'current' w odczycie: {raw}"
    assert "power" in raw, f"{spec.ip}: brak pola 'power' w odczycie: {raw}"

    voltage = reading["voltage_v"]
    assert MAINS_VOLTAGE_MIN_V <= voltage <= MAINS_VOLTAGE_MAX_V, (
        f"{spec.ip}: napięcie {voltage} V wypada poza zakresem sieciowym "
        f"({MAINS_VOLTAGE_MIN_V}–{MAINS_VOLTAGE_MAX_V} V). Surowa odpowiedź: {raw}"
    )

    # Prąd i moc czytamy jako liczby (mogą być zerowe — nic nie musi być wpięte),
    # ale nie mogą być tekstem ani niczym, czego most nie przeliczy.
    assert isinstance(reading["current_a"], float), f"{spec.ip}: prąd nie jest liczbą: {raw}"
    assert isinstance(reading["power_w"], float), f"{spec.ip}: moc nie jest liczbą: {raw}"
    assert reading["current_a"] >= 0.0 and reading["power_w"] >= 0.0, f"{spec.ip}: ujemny odczyt: {raw}"
