# -*- coding: utf-8 -*-
"""Testy narzędziowe dla tools/meross_login.py (bez sieci / chmury)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))

from meross_login import (  # noqa: E402
    ensure_login_response_compat,
    update_env_value,
)


def test_ensure_login_response_compat_dopelnia_mfa_lock_expire():
    raw = {
        "token": "t",
        "key": "sekretnyklucz",
        "userid": "1",
        "email": "a@b.c",
        "domain": "https://iotx-eu.meross.com",
        "mqttDomain": "mqtt-eu.meross.com",
    }
    out = ensure_login_response_compat(raw)
    assert out["mfaLockExpire"] == 0
    assert out["key"] == "sekretnyklucz"


def test_ensure_login_response_compat_nie_nadpisuje_istniejacego():
    raw = {"token": "t", "key": "k", "mfaLockExpire": 42}
    assert ensure_login_response_compat(raw)["mfaLockExpire"] == 42


def test_ensure_login_response_compat_nie_rusza_innych_odpowiedzi():
    raw = {"list": [{"uuid": "abc"}]}
    assert ensure_login_response_compat(raw) == raw
    assert "mfaLockExpire" not in raw


def test_update_env_value_podmienia_meross_key():
    text = "MQTT_HOST=mosquitto\nMEROSS_KEY=\nTZ=Europe/Warsaw\n"
    out = update_env_value(text, "MEROSS_KEY", "abc123")
    assert "MEROSS_KEY=abc123\n" in out
    assert "MQTT_HOST=mosquitto" in out
    assert "TZ=Europe/Warsaw" in out


@pytest.mark.skipif(
    __import__("importlib").util.find_spec("meross_iot") is None,
    reason="meross_iot tylko w obrazie mostu",
)
def test_patch_meross_login_compat_jest_idempotentny():
    from meross_login import patch_meross_login_compat
    from meross_iot.http_api import MerossHttpClient

    patch_meross_login_compat()
    first = MerossHttpClient._async_authenticated_post
    patch_meross_login_compat()
    assert MerossHttpClient._async_authenticated_post is first
