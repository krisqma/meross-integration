"""Testy dot.sh — bez Dockera, bez sieci, bez mutowania repo.

Wszystko, co dotyka plików, dzieje się w katalogu tymczasowym: dot.sh respektuje
zmienne DOT_* (DOT_ROOT, DOT_ENV_FILE, DOT_TMPL, DOT_CONFIG, ...), więc generowanie
bridge/config/config.yml da się przetestować bez ruszania prawdziwego configu.

Komendy mutujące stan (up/down/restart/logs) nie są tu odpalane — wymagają demona
Dockera. Testowane są za to wszystkie ścieżki, które kończą się czytelnym komunikatem
o brakującym pliku dostarczanym przez inny strumień pracy.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
DOT_SH = ROOT / "dot.sh"
TEMPLATE = ROOT / "bridge" / "config" / "config.yml.tmpl"

# Własne ustawienie sys.path — w tym projekcie świadomie nie ma tests/conftest.py.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

COMMANDS = [
    "up",
    "down",
    "restart",
    "logs",
    "status",
    "discover",
    "login",
    "test",
    "render-config",
    "help",
]


def run_dot(args, root: Path | None = None, extra_env: dict | None = None):
    """Odpala dot.sh w minimalnym środowisku, żeby wynik nie zależał od powłoki wołającego."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
    }
    if root is not None:
        env["DOT_ROOT"] = str(root)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(DOT_SH), *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def make_project(tmp_path: Path, **env_values) -> Path:
    """Buduje w tmp minimalny szkielet projektu: szablon (prawdziwy) + .env."""
    root = tmp_path / "proj"
    (root / "bridge" / "config").mkdir(parents=True)
    (root / "bridge" / "config" / "config.yml.tmpl").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    values = {
        "TZ": "Europe/Warsaw",
        "MQTT_HOST": "mosquitto",
        "MQTT_PORT": "1883",
        "HOMIE_PREFIX": "homie",
        "MEROSS_KEY": "abc123klucz",
    }
    values.update(env_values)
    lines = ["# testowy .env", ""]
    lines += [f"{key}={value}" for key, value in values.items()]
    (root / ".env").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return root


def render(tmp_path: Path, **env_values) -> tuple[Path, dict]:
    root = make_project(tmp_path, **env_values)
    result = run_dot(["render-config"], root=root)
    assert result.returncode == 0, result.stderr
    config = root / "bridge" / "config" / "config.yml"
    assert config.is_file(), "render-config nie zapisał bridge/config/config.yml"
    return config, yaml.safe_load(config.read_text(encoding="utf-8"))


# --- składnia i interfejs -------------------------------------------------


def test_skladnia_basha_jest_poprawna():
    result = subprocess.run(
        ["bash", "-n", str(DOT_SH)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""


def test_dot_sh_jest_wykonywalny():
    assert os.access(DOT_SH, os.X_OK), "dot.sh musi mieć bit wykonywalności (git go pamięta)"


def test_help_wypisuje_wszystkie_komendy():
    result = run_dot(["help"])
    assert result.returncode == 0, result.stderr
    for command in COMMANDS:
        assert command in result.stdout, f"pomoc nie wspomina komendy '{command}'"


def test_bez_argumentow_pokazuje_pomoc():
    result = run_dot([])
    assert result.returncode == 0, result.stderr
    assert "Użycie: ./dot.sh" in result.stdout


def test_nieznana_komenda_konczy_sie_bledem():
    result = run_dot(["zrob-kawe"])
    assert result.returncode != 0
    assert "Nieznana komenda" in result.stderr
    # Przy błędzie pomoc też ma się pokazać, ale na stderr — nie zaśmiecamy stdout.
    assert "Użycie: ./dot.sh" in result.stderr


# --- generowanie config.yml ----------------------------------------------


def test_render_podstawia_zmienne_z_env(tmp_path):
    _, data = render(
        tmp_path,
        MQTT_HOST="broker-testowy",
        MQTT_PORT="18830",
        HOMIE_PREFIX="domek",
        MEROSS_KEY="sekret-z-chmury",
    )
    assert data["mqtt_host"] == "broker-testowy"
    assert data["mqtt_port"] == 18830
    assert data["homie_prefix"] == "domek"
    assert data["meross_key"] == "sekret-z-chmury"


def test_render_nie_zostawia_znacznikow(tmp_path):
    config, _ = render(tmp_path)
    assert "{{" not in config.read_text(encoding="utf-8")


def test_render_utrzymuje_decyzje_z_planu(tmp_path):
    _, data = render(tmp_path)
    assert data["mqtt_clean_session"] is True
    assert data["enable_http"] is True
    assert data["try_reboot_on_timeout"] is False
    assert 20 <= data["polling_interval"] <= 30
    # Ścieżka relatywna: obraz mostu ma WORKDIR /config, więc to /config/devices.json.
    assert data["persistence_file"] == "devices.json"
    assert data["log_level"] == "INFO"
    # CONTRACT.md §2.1: pretty_topic nigdzie nie może być aktywne, bo <dev> ma zostać UUID-em.
    assert "pretty_topic" not in yaml.safe_dump(data)


def test_render_bez_env_uzywa_domyslnych_z_kontraktu(tmp_path):
    root = tmp_path / "proj"
    (root / "bridge" / "config").mkdir(parents=True)
    (root / "bridge" / "config" / "config.yml.tmpl").write_text(
        TEMPLATE.read_text(encoding="utf-8"), encoding="utf-8"
    )
    result = run_dot(["render-config"], root=root)
    assert result.returncode == 0, result.stderr
    data = yaml.safe_load((root / "bridge" / "config" / "config.yml").read_text(encoding="utf-8"))
    assert data["mqtt_host"] == "mosquitto"
    assert data["mqtt_port"] == 1883
    assert data["homie_prefix"] == "homie"
    assert data["meross_key"] == ""


def test_render_nie_nadpisuje_bez_zmian_ale_lapie_zmiane_env(tmp_path):
    root = make_project(tmp_path, MQTT_HOST="pierwszy")
    config = root / "bridge" / "config" / "config.yml"

    assert run_dot(["render-config"], root=root).returncode == 0
    first_mtime = config.stat().st_mtime_ns

    assert run_dot(["render-config"], root=root).returncode == 0
    assert config.stat().st_mtime_ns == first_mtime, "identyczny config nie powinien być nadpisywany"

    env_file = root / ".env"
    env_file.write_text(
        env_file.read_text(encoding="utf-8").replace("pierwszy", "drugi"), encoding="utf-8"
    )
    assert run_dot(["render-config"], root=root).returncode == 0
    data = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert data["mqtt_host"] == "drugi", "zmiana .env musi się przebić do config.yml"


def test_render_odrzuca_nieliczbowy_port(tmp_path):
    root = make_project(tmp_path, MQTT_PORT="tysiąc")
    result = run_dot(["render-config"], root=root)
    assert result.returncode != 0
    assert "MQTT_PORT" in result.stderr
    assert not (root / "bridge" / "config" / "config.yml").exists()


def test_render_bez_szablonu_konczy_sie_bledem(tmp_path):
    root = tmp_path / "pusty"
    root.mkdir()
    result = run_dot(["render-config"], root=root)
    assert result.returncode != 0
    assert "szablon" in result.stderr.lower()


def test_wartosc_env_w_cudzyslowach_jest_rozbierana(tmp_path):
    root = make_project(tmp_path)
    env_file = root / ".env"
    env_file.write_text('MQTT_HOST="w-cudzyslowach"\nMEROSS_KEY=\n', encoding="utf-8")
    assert run_dot(["render-config"], root=root).returncode == 0
    data = yaml.safe_load(
        (root / "bridge" / "config" / "config.yml").read_text(encoding="utf-8")
    )
    assert data["mqtt_host"] == "w-cudzyslowach"
    assert data["meross_key"] == ""


# --- brakujące pliki innych strumieni ------------------------------------


@pytest.mark.parametrize("command", ["discover", "login"])
def test_brak_skryptu_to_komunikat_a_nie_crash(tmp_path, command):
    root = make_project(tmp_path)
    result = run_dot([command], root=root)
    assert result.returncode != 0
    assert f"tools/{'discover' if command == 'discover' else 'meross_login'}.py" in result.stderr
    # Żadnego wysypu basha ani pythonowego tracebacku.
    assert "Traceback" not in result.stderr
    assert "line " not in result.stderr


def test_brak_docker_compose_test_yml_to_jasny_komunikat(tmp_path):
    root = make_project(tmp_path)
    result = run_dot(["test", "unit"], root=root)
    assert result.returncode != 0
    assert "docker-compose.test.yml" in result.stderr
    assert "tests" in result.stderr


def test_nieznany_rodzaj_testow_konczy_sie_bledem(tmp_path):
    root = make_project(tmp_path)
    result = run_dot(["test", "wszystkie"], root=root)
    assert result.returncode != 0
    assert "unit" in result.stderr
