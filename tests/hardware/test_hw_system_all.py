# -*- coding: utf-8 -*-
"""Test sprzętowy 2: `Appliance.System.All` poprawnym kluczem.

To jest pierwsza okazja, żeby dowiedzieć się, CO właściwie wisi w mieszkaniu — model
gniazdek i liczba kanałów były do tej pory nieznane (bez klucza sprzęt odpowiada tylko
`5001 sign error`). Dlatego test nie tylko sprawdza kontrakt odczytu, ale też wypisuje
pełną wizytówkę urządzenia.

Asercje są świadomie ograniczone do rzeczy, które MUSZĄ być prawdą niezależnie od modelu:
obecność `hardware.type`, MAC zgodny z inwentarzem, `firmware.innerIp` równy adresowi,
pod którym pytamy, oraz co najmniej jeden kanał w `digest.togglex`. Konkretnego modelu
i liczby kanałów test NIE zakłada — raportuje je.

    ./dot.sh test hw -k system_all
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hw import (  # noqa: E402
    NS_ALL,
    DeviceSpec,
    ask,
    channel_states,
    each_device,
    report,
    require_key,
)

pytestmark = pytest.mark.hardware


@each_device
def test_system_all_zgadza_sie_z_inwentarzem_i_pokazuje_model(spec: DeviceSpec) -> None:
    key = require_key()

    payload = ask(spec, NS_ALL, key)
    system = (payload.get("all") or {}).get("system") or {}
    hardware = system.get("hardware") or {}
    firmware = system.get("firmware") or {}
    states = channel_states(payload)
    states_text = ", ".join(f"{channel}={'ON' if on else 'OFF'}" for channel, on in sorted(states.items()))

    report(
        f"System.All {spec.ip}",
        [
            f"model (hardware.type): {hardware.get('type')}",
            f"subType / wersja HW:   {hardware.get('subType')} / {hardware.get('version')}",
            f"chipType:              {hardware.get('chipType')}",
            f"firmware:              {firmware.get('version')} (compileTime {firmware.get('compileTime')})",
            f"uuid:                  {hardware.get('uuid')}",
            f"macAddress:            {hardware.get('macAddress')}",
            f"innerIp:               {firmware.get('innerIp')}",
            f"server chmury:         {firmware.get('server')}:{firmware.get('port')}",
            f"online.status:         {(system.get('online') or {}).get('status')}",
            f"LICZBA KANAŁÓW:        {len(states)}",
            f"stan kanałów:          {states_text or '(brak)'}",
        ],
    )

    model = hardware.get("type")
    assert isinstance(model, str) and model.strip(), f"{spec.ip}: brak all.system.hardware.type w odpowiedzi"

    mac = str(hardware.get("macAddress", "")).strip().lower()
    assert mac == spec.mac.lower(), f"{spec.ip}: macAddress to {mac}, a inwentarz mówi {spec.mac}"

    # Most po interview przechodzi właśnie na ten adres (`manager.py`, `rpc_http`), więc
    # rozjazd między innerIp a adresem, pod którym pytamy, zerwałby mu komunikację.
    inner_ip = str(firmware.get("innerIp", "")).strip()
    assert inner_ip == spec.ip, (
        f"{spec.ip}: firmware.innerIp to {inner_ip!r} — gniazdko uważa, że ma inny adres niż ten, "
        f"pod którym odpowiada. Most po interview przechodzi na innerIp i straciłby z nim kontakt."
    )

    assert states, f"{spec.ip}: all.digest.togglex jest puste — most nie zbudowałby ani jednego węzła switch"
    assert 0 in states, f"{spec.ip}: brak kanału 0 w digest.togglex (kanały: {sorted(states)})"
