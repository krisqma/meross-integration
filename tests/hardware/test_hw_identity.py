# -*- coding: utf-8 -*-
"""Test sprzętowy 1: wykrywalność i tożsamość trzech gniazdek — BEZ klucza z chmury.

Gniazdko Meross odpowiada na `POST /config` również wtedy, gdy podpis jest zły: zwraca
`5001 sign error`, ale w nagłówku i tak podaje swój UUID. Dlatego ten test celowo pyta
z PUSTYM kluczem — jest sprawdzeniem, że sprzęt jest pod adresem z CONTRACT.md §9 i że
to naprawdę te trzy urządzenia, a nie że mamy klucz.

    ./dot.sh test hw -k tozsamosc

Marker `hardware` jest w pytest.ini domyślnie pomijany.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hw import (  # noqa: E402
    NS_ALL,
    SIGN_ERROR_CODE,
    UNREACHABLE_HINT,
    DeviceSpec,
    Unreachable,
    each_device,
    error_of,
    port_open,
    report,
    rpc,
    uuid_from_header,
)

pytestmark = pytest.mark.hardware


@each_device
def test_gniazdko_odpowiada_i_ma_uuid_z_inwentarza(spec: DeviceSpec) -> None:
    if not port_open(spec.ip):
        pytest.skip(f"{spec.ip}: port 80/tcp zamknięty.\n{UNREACHABLE_HINT}")

    # Pusty klucz jest tu celem, nie zaniedbaniem: chcemy pokazać, że wykrywanie
    # tożsamości nie zależy od MEROSS_KEY.
    try:
        envelope = rpc(spec.ip, NS_ALL, key="")
    except Unreachable as exc:
        pytest.skip(f"{exc}\n{UNREACHABLE_HINT}")

    header = envelope["header"]
    uuid = uuid_from_header(header)
    error = error_of(envelope)

    report(
        f"tożsamość {spec.ip}",
        [
            f"method:      {header.get('method')}",
            f"namespace:   {header.get('namespace')}",
            f"uuid:        {uuid}",
            f"oczekiwany:  {spec.uuid}",
            f"odpowiedź:   {'błąd ' + str(error) if error else 'poprawna koperta (klucz pusty ZADZIAŁAŁ?!)'}",
        ],
    )

    assert uuid is not None, f"{spec.ip}: odpowiedź nie zawiera UUID-a ani w header.uuid, ani w header.from"
    assert uuid == spec.uuid, (
        f"{spec.ip}: pod tym adresem siedzi gniazdko o UUID {uuid}, a inwentarz (CONTRACT.md §9) "
        f"mówi {spec.uuid}. Prawdopodobnie DHCP przestawiło adresy — './dot.sh discover' zaktualizuje "
        f"devices.json, ale inwentarz w CONTRACT.md §9 i w tests/hardware/hw.py trzeba poprawić ręcznie."
    )

    # Pusty klucz MUSI zostać odrzucony — inaczej gniazdko nie ma ustawionego klucza
    # z chmury i cały model bezpieczeństwa lokalnego API jest inny, niż zakłada plan.
    assert error is not None, f"{spec.ip}: gniazdko przyjęło żądanie z pustym kluczem — nie ma klucza z chmury?"
    assert error[0] == SIGN_ERROR_CODE, f"{spec.ip}: spodziewałem się {SIGN_ERROR_CODE} sign error, a jest {error}"
