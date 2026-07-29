# -*- coding: utf-8 -*-
"""Test sprzętowy 3: zdolności prawdziwego gniazdka kontra atrapa — świadoma diagnostyka.

Atrapa ze strumienia C (`tests/fakes/fake_device.py`) udaje siedem przestrzeni nazw i na
niej przeszło całe testowanie stacku. Prawdziwy mss310 zgłasza kilkanaście, a `meross_iot`
dobiera mixiny właśnie po tej liście — np. przy `Appliance.System.Runtime` doczepia
`SystemRuntimeMixin`, który dokłada własne odpytanie sprzętu. Tam, gdzie atrapa milczy,
w produkcji leci ruch, którego żaden test na atrapach nie zobaczył.

Dlatego ten test **nie pada z powodu różnicy** — różnica jest tu oczekiwana i jest
WYNIKIEM testu. Pada tylko wtedy, gdy gniazdko nie odda listy zdolności. Wszystko inne
raportuje: co sprzęt ma, czego atrapa nie udaje, i co atrapa udaje na zapas.

    ./dot.sh test hw -k zdolnosci
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hw import (  # noqa: E402
    NS_ALL,
    DeviceSpec,
    ability_namespaces,
    ask,
    each_device,
    fake_ability_namespaces,
    report,
    require_key,
)

pytestmark = pytest.mark.hardware

# Zdolności, których brak w atrapie pociąga za sobą DODATKOWE zachowanie `meross_iot`
# (mixiny dobierane po liście z Ability). To one najmocniej różnicują most na sprzęcie
# i most na atrapach, więc raport ma je nazywać wprost.
MIXIN_HINTS = {
    "Appliance.System.Runtime": "SystemRuntimeMixin — dokłada odpytanie Appliance.System.Runtime (siła sygnału Wi-Fi)",
    "Appliance.System.DNDMode": "sterowanie diodą (tryb nie przeszkadzać)",
    "Appliance.Control.Electricity": "ElectricityMixin — odczyt napięcia, prądu i mocy",
    "Appliance.Control.ConsumptionX": "ConsumptionXMixin — dzienne zużycie energii",
    "Appliance.Control.ToggleX": "ToggleXMixin — właściwe przełączanie kanałów",
    "Appliance.Control.TimerX": "harmonogramy trzymane w gniazdku (u nas robi je webapp)",
    "Appliance.Control.TriggerX": "reguły trzymane w gniazdku",
    "Appliance.Config.OverTemp": "zabezpieczenie termiczne (jest w mss310)",
}


def _bullets(namespaces: list[str], marker: str, empty: str, hints: bool = True) -> list[str]:
    if not namespaces:
        return [f"  {empty}"]
    lines = []
    for namespace in namespaces:
        hint = MIXIN_HINTS.get(namespace) if hints else None
        lines.append(f"  {marker} {namespace}" + (f"   <- {hint}" if hint else ""))
    return lines


@each_device
def test_zdolnosci_sprzetu_kontra_atrapa(spec: DeviceSpec) -> None:
    key = require_key()

    real = ability_namespaces(spec, key)
    fake = fake_ability_namespaces()

    hardware = (((ask(spec, NS_ALL, key)).get("all") or {}).get("system") or {}).get("hardware") or {}
    missing_in_fake = sorted(real - fake)
    only_in_fake = sorted(fake - real)
    common = sorted(real & fake)

    lines = [
        f"model:                {hardware.get('type')} (uuid {spec.uuid})",
        f"zdolności w sprzęcie: {len(real)}",
        f"zdolności w atrapie:  {len(fake)}",
        f"wspólne ({len(common)}):",
        # Podpowiedzi o mixinach tylko przy różnicach — na liście wspólnej byłyby szumem.
        *_bullets(common, "=", "(brak — atrapa i sprzęt nie mają nic wspólnego?!)", hints=False),
        f"OBECNE W SPRZĘCIE, NIEOBSŁUGIWANE PRZEZ ATRAPĘ ({len(missing_in_fake)}):",
        *_bullets(missing_in_fake, "+", "(brak — atrapa pokrywa cały sprzęt)"),
        f"UDAWANE PRZEZ ATRAPĘ, NIEOBECNE W SPRZĘCIE ({len(only_in_fake)}):",
        *_bullets(only_in_fake, "-", "(brak)"),
    ]
    report(f"Ability {spec.ip}", lines)

    # Jedyna asercja tego testu. Pustą albo błędną odpowiedź wyłapuje już
    # `ability_namespaces`; tu pilnujemy tylko, że mamy z czym porównywać.
    assert real, f"{spec.ip}: gniazdko nie zgłosiło ani jednej zdolności"
    assert fake, "atrapa nie zgłosiła ani jednej zdolności — porównanie nie miałoby sensu"
