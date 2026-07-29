#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Jednorazowo pobiera klucz urządzeń z chmury Meross i zapisuje go do `.env`.

Po co to jest:
    Most `meross2mqtt` rozmawia z gniazdkami po LAN-ie (HTTP `POST /config`), ale
    każde żądanie musi być podpisane kluczem, który gniazdka dostały z chmury przy
    parowaniu. Ten skrypt loguje się raz do chmury, wyciąga ten klucz i od tej pory
    chmura nie jest już do niczego potrzebna.

Gdzie się to uruchamia:
    W kontenerze mostu, bo tam jest `meross_iot==0.4.6.2` (host ma Pythona 3.9 bez
    tej biblioteki). Kontener nie widzi jednak ani `tools/`, ani `.env`, więc trzeba
    je domontować — `./dot.sh login` robi to za Ciebie. Ręcznie odpowiednik to:

        docker compose run --rm -T \\
            --entrypoint python3 \\
            -v "$PWD/tools:/tools:ro" \\
            -v "$PWD/.env:/env/.env" \\
            bridge /tools/meross_login.py --env-file /env/.env --output /config/cloud-devices.json

    (`--entrypoint python3` jest konieczne — domyślny ENTRYPOINT obrazu to
    `python3 -m meross2homie`. Na Linuksie i Raspberry Pi dodaj jeszcze
    `--user "$(id -u):$(id -g)"`, inaczej zapisane `.env` i `cloud-devices.json`
    będą własnością roota.)

Co zapisuje:
    * `.env` — wyłącznie linię `MEROSS_KEY=...`; pozostałe linie i komentarze zostają nietknięte,
    * `bridge/config/cloud-devices.json` — mapowanie uuid → nazwa i typ urządzenia
      (przyda się później na ładne nazwy w panelu; plik uruchomieniowy, nie do commitu).

Czego NIE robi:
    Nie zapisuje nigdzie hasła ani tokenu i nie wypisuje ich w logach. Hasło jest
    wczytywane przez `getpass`, nigdy z argumentów wiersza poleceń.

API biblioteki jest sprawdzone w źródłach `meross_iot==0.4.6.2` (nie zgadywane):
`MerossHttpClient.async_from_user_password(api_base_url=, email=, password=, mfa_code=)`,
`client.cloud_credentials.key`, `await client.async_list_devices()` → obiekty z
`uuid`, `dev_name`, `device_type`.

Kody wyjścia: 0 — klucz pobrany i zapisany, 1 — błąd logowania lub zapisu, 2 — zły sposób użycia.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent

# Regiony chmury Meross. Konto działa tylko w swoim regionie — przy pomyłce API
# zwraca przekierowanie (BadDomainException) i podpowiada właściwy adres.
REGIONS = {
    "eu": "https://iotx-eu.meross.com",
    "us": "https://iotx-us.meross.com",
    "ap": "https://iotx-ap.meross.com",
}
DEFAULT_API_BASE_URL = REGIONS["eu"]

ENV_KEY = "MEROSS_KEY"
# Znaki, przy których wartość w `.env` trzeba wziąć w cudzysłowy.
_SAFE_ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9_.:/@+=-]*$")


# --- Plik .env ---------------------------------------------------------------


def update_env_value(text: str, key: str, value: str) -> str:
    """Podmienia (albo dopisuje) `key=value` w treści pliku `.env`.

    Reszta pliku — komentarze, kolejność, puste linie — zostaje bez zmian.
    Zakomentowane `# KEY=` nie jest traktowane jako przypisanie.
    """
    if not _SAFE_ENV_VALUE_RE.match(value):
        rendered = '{0}="{1}"'.format(key, value.replace('"', '\\"'))
    else:
        rendered = "{0}={1}".format(key, value)

    pattern = re.compile(r"^\s*(?:export\s+)?" + re.escape(key) + r"\s*=")
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if pattern.match(line):
            lines[index] = rendered
            break
    else:
        lines.append(rendered)

    out = "\n".join(lines)
    return out if out.endswith("\n") else out + "\n"


def save_key_to_env(env_path: Path, key: str) -> str:
    """Zapisuje klucz w `.env`. Gdy pliku nie ma, tworzy go z `.env.example`.

    Zwraca opis tego, co się stało — do wypisania użytkownikowi.
    """
    if env_path.exists():
        original = env_path.read_text(encoding="utf-8")
        note = "zaktualizowano {0}".format(env_path)
    else:
        example = env_path.parent / ".env.example"
        if example.exists():
            original = example.read_text(encoding="utf-8")
            note = "utworzono {0} na podstawie .env.example".format(env_path)
        else:
            original = ""
            note = "utworzono {0}".format(env_path)

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text(update_env_value(original, ENV_KEY, key), encoding="utf-8")
    return note


# --- cloud-devices.json ------------------------------------------------------


def device_summary(device: Any) -> Dict[str, Any]:
    """Wyciąga z `HttpDeviceInfo` tylko to, co nam potrzebne (i nic wrażliwego)."""
    channels = getattr(device, "channels", None)
    online = getattr(device, "online_status", None)
    entry = {
        "name": getattr(device, "dev_name", None),
        "type": getattr(device, "device_type", None),
        "sub_type": getattr(device, "sub_type", None),
        "fw_version": getattr(device, "fmware_version", None),
        "hw_version": getattr(device, "hdware_version", None),
        "channels": len(channels) if isinstance(channels, list) else None,
        "online": getattr(online, "name", None) if online is not None else None,
    }
    return {name: value for name, value in entry.items() if value is not None}


def build_cloud_devices(devices: Sequence[Any]) -> Dict[str, Any]:
    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "note": "Zrzut z chmury Meross (./dot.sh login). Plik uruchomieniowy — nie commituj go.",
        "devices": {str(getattr(device, "uuid", "")): device_summary(device) for device in devices},
    }


def write_cloud_devices(path: Path, content: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(str(path), "w", encoding="utf-8") as handle:
        json.dump(content, handle, indent=2, ensure_ascii=False, sort_keys=False)
        handle.write("\n")


# --- Ścieżki domyślne (host kontra kontener) ---------------------------------


def default_env_path() -> Path:
    """`.env` w katalogu repozytorium; w kontenerze podaj `--env-file` ręcznie."""
    return REPO_ROOT / ".env"


def default_output_path() -> Path:
    """`bridge/config/cloud-devices.json`, a w kontenerze mostu `/config/cloud-devices.json`."""
    repo_config = REPO_ROOT / "bridge" / "config"
    if repo_config.is_dir():
        return repo_config / "cloud-devices.json"
    container_config = Path("/config")
    if container_config.is_dir():
        return container_config / "cloud-devices.json"
    return repo_config / "cloud-devices.json"


def resolve_api_base_url(raw: Optional[str]) -> str:
    """Przyjmuje skrót regionu (`eu`/`us`/`ap`) albo pełny adres."""
    candidate = raw or os.environ.get("MEROSS_API_URL") or DEFAULT_API_BASE_URL
    key = candidate.strip().lower()
    if key in REGIONS:
        return REGIONS[key]
    if not key.startswith(("http://", "https://")):
        return "https://" + candidate.strip()
    return candidate.strip()


def mask_key(key: str) -> str:
    """Klucz to sekret — pokazujemy tylko tyle, żeby dało się go rozpoznać."""
    if len(key) <= 8:
        return "*" * len(key)
    return "{0}...{1} ({2} znaków)".format(key[:4], key[-2:], len(key))


# --- Logowanie ---------------------------------------------------------------


def region_hint(current: str) -> str:
    others = [url for name, url in sorted(REGIONS.items()) if url != current]
    return "Spróbuj innego regionu: {0} (np. --api-base-url us).".format(", ".join(others))


def ensure_login_response_compat(response_data: Dict[str, Any]) -> Dict[str, Any]:
    """Uzupełnia pola, których `meross_iot==0.4.6.2` wymaga, a API Meross już nie zwraca.

    Od ~2024 odpowiedź logowania często nie ma `mfaLockExpire`. Biblioteka robi wtedy
    `response_data["mfaLockExpire"]` → `KeyError`, mimo że logowanie się udało i klucz
    jest w odpowiedzi. Uzupełniamy brakujące pola przed zbudowaniem `MerossCloudCreds`.
    """
    if not isinstance(response_data, dict):
        return response_data
    # Token+key = sukces logowania; inne endpointy (list devices) nie potrzebują łaty.
    if "token" in response_data and "key" in response_data:
        response_data.setdefault("mfaLockExpire", 0)
        response_data.setdefault("mqttDomain", response_data.get("mqttDomain") or "mqtt-eu.meross.com")
        response_data.setdefault("domain", response_data.get("domain") or "https://iotx-eu.meross.com")
    return response_data


def patch_meross_login_compat() -> None:
    """Owija `_async_authenticated_post`, żeby login nie padał na brakującym mfaLockExpire.

    Idempotentne — kolejne wywołania nie nakładają kolejnych warstw.
    """
    from meross_iot.http_api import MerossHttpClient

    if getattr(MerossHttpClient._async_authenticated_post, "_mfa_lock_patched", False):
        return

    original = MerossHttpClient._async_authenticated_post

    @classmethod
    async def _patched(cls, *args: Any, **kwargs: Any) -> Any:  # type: ignore[no-untyped-def]
        raw = getattr(original, "__func__", original)
        data = await raw(cls, *args, **kwargs)
        return ensure_login_response_compat(data)

    _patched._mfa_lock_patched = True  # type: ignore[attr-defined]
    MerossHttpClient._async_authenticated_post = _patched  # type: ignore[method-assign]


async def fetch_key_and_devices(
    api_base_url: str, email: str, password: str, mfa_code: Optional[str]
) -> Tuple[str, List[Any]]:
    """Loguje się, pobiera klucz i listę urządzeń, po czym unieważnia token.

    Token jest nam potrzebny tylko na te dwa żądania. Meross ogranicza liczbę
    aktywnych tokenów na konto, więc wylogowanie to nie kosmetyka.
    """
    from meross_iot.http_api import MerossHttpClient

    patch_meross_login_compat()

    client = await MerossHttpClient.async_from_user_password(
        api_base_url=api_base_url,
        email=email,
        password=password,
        mfa_code=mfa_code,
    )
    try:
        key = client.cloud_credentials.key
        devices = await client.async_list_devices()
    finally:
        try:
            await client.async_logout()
        except Exception as exc:  # wylogowanie jest miłe, ale nie krytyczne
            print("Uwaga: nie udało się wylogować z chmury ({0}).".format(exc.__class__.__name__), file=sys.stderr)
    return key, list(devices)


# Kody z `meross_iot.model.http.error_codes.ErrorCodes`, które warto wyjaśnić po ludzku.
API_ERROR_MESSAGES = {
    "CODE_MISSING_USER": "Chmura nie przyjęła e-maila (puste albo nieprawidłowe pole).",
    "CODE_MISSING_PASSWORD": "Chmura nie przyjęła hasła (puste albo nieprawidłowe pole).",
    "CODE_DISABLED_OR_DELETED_ACCOUNT": "To konto Meross jest zablokowane albo usunięte.",
    "CODE_INVALID_EMAIL": "To nie jest poprawny adres e-mail.",
    "CODE_WRONG_EMAIL": "Ten e-mail nie jest zarejestrowany w tym regionie chmury.",
    "CODE_TOKEN_INVALID": "Token sesji jest nieprawidłowy — spróbuj jeszcze raz.",
    "CODE_REDIRECT_REGION": "Konto należy do innego regionu chmury Meross.",
}


def explain_failure(exc: BaseException, api_base_url: str) -> str:
    """Zamienia wyjątek biblioteki na zdanie po polsku, bez stacktrace'u."""
    from meross_iot.model.http.exception import (
        AuthenticatedPostException,
        BadDomainException,
        BadLoginException,
        HttpApiError,
        MissingMFA,
        TooManyTokensException,
        WrongMFA,
    )

    if isinstance(exc, MissingMFA):
        return (
            "Konto ma włączone uwierzytelnianie dwuetapowe — potrzebny kod z aplikacji Authenticator.\n"
            "Uruchom ponownie i podaj kod, albo od razu: ./dot.sh login --mfa-code 123456"
        )
    if isinstance(exc, KeyError) and exc.args and exc.args[0] == "mfaLockExpire":
        return (
            "Biblioteka meross_iot padła na brakującym polu mfaLockExpire (znany bug API Meross).\n"
            "Zaktualizuj tools/meross_login.py — powinna być już łata ensure_login_response_compat."
        )
    if isinstance(exc, WrongMFA):
        return "Kod dwuetapowy jest nieprawidłowy albo już wygasł. Weź świeży kod i spróbuj jeszcze raz."
    if isinstance(exc, BadLoginException):
        return "Zły e-mail lub hasło (albo konto nie istnieje w tym regionie). " + region_hint(api_base_url)
    if isinstance(exc, BadDomainException):
        return (
            "To konto należy do innego regionu chmury Meross.\n"
            "Powtórz z: --api-base-url {0}".format(getattr(exc, "api_domain", "https://iotx-us.meross.com"))
        )
    if isinstance(exc, TooManyTokensException):
        return (
            "Chmura Meross odmówiła kolejnego tokenu (za dużo logowań bez wylogowania).\n"
            "Odczekaj kilkanaście minut i spróbuj ponownie."
        )
    if isinstance(exc, HttpApiError):
        code = getattr(exc, "error_code", None)
        name = getattr(code, "name", str(code))
        message = API_ERROR_MESSAGES.get(name)
        if message:
            return "{0}\n{1}".format(message, region_hint(api_base_url))
        return "Chmura Meross odrzuciła żądanie (kod {0}).".format(name)
    if isinstance(exc, AuthenticatedPostException):
        return "Chmura Meross odpowiedziała błędem: {0}".format(exc)
    if isinstance(exc, (OSError, asyncio.TimeoutError)):
        return (
            "Nie udało się połączyć z {0} ({1}).\n"
            "Sprawdź internet w kontenerze i poprawność adresu API.".format(api_base_url, exc.__class__.__name__)
        )
    return "Logowanie nie udało się: {0}: {1}".format(exc.__class__.__name__, exc)


# --- Główny przebieg ---------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meross_login.py",
        description="Pobiera z chmury Meross klucz urządzeń i zapisuje go w .env (jednorazowo).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Hasło podaje się interaktywnie — nigdy w argumentach, żeby nie wylądowało\n"
            "w historii powłoki ani w liście procesów.\n"
        ),
    )
    parser.add_argument("--email", help="e-mail konta Meross (gdy pominiesz, skrypt zapyta)")
    parser.add_argument(
        "--api-base-url",
        help="region chmury: skrót eu/us/ap albo pełny adres (domyślnie {0})".format(DEFAULT_API_BASE_URL),
    )
    parser.add_argument("--mfa-code", help="kod uwierzytelniania dwuetapowego, jeśli konto go wymaga")
    parser.add_argument("--env-file", help="ścieżka pliku .env do aktualizacji (domyślnie {0})".format(default_env_path()))
    parser.add_argument("--output", help="ścieżka cloud-devices.json (domyślnie {0})".format(default_output_path()))
    parser.add_argument("--dry-run", action="store_true", help="zaloguj się i pokaż wynik, ale nie zapisuj plików")
    parser.add_argument("--verbose", action="store_true", help="pokaż surowe logi biblioteki meross_iot (diagnostyka)")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Bez tego komunikaty postępu (stdout) i błędy (stderr) mieszają się kolejnością,
    # gdy wyjście idzie do pliku albo przez `docker compose run -T`.
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass

    if not args.verbose:
        # Biblioteka loguje błędy API sama, po angielsku i z surowym JSON-em.
        # My pokazujemy własny komunikat, więc jej logi tylko przeszkadzają.
        logging.getLogger("meross_iot").setLevel(logging.CRITICAL)
        logging.getLogger("meross_iot.http_api").setLevel(logging.CRITICAL)
    else:
        logging.basicConfig(level=logging.DEBUG)

    try:
        import meross_iot  # noqa: F401
    except ImportError:
        print(
            "Brak biblioteki `meross_iot`. Ten skrypt ma się uruchamiać w kontenerze mostu:\n"
            "  ./dot.sh login\n"
            "albo ręcznie:\n"
            "  docker compose run --rm -T --entrypoint python3 \\\n"
            '      -v "$PWD/tools:/tools:ro" -v "$PWD/.env:/env/.env" \\\n'
            "      bridge /tools/meross_login.py --env-file /env/.env --output /config/cloud-devices.json",
            file=sys.stderr,
        )
        return 2

    api_base_url = resolve_api_base_url(args.api_base_url)
    env_path = Path(args.env_file).expanduser() if args.env_file else default_env_path()
    output_path = Path(args.output).expanduser() if args.output else default_output_path()

    print("Logowanie do chmury Meross ({0}).".format(api_base_url))
    print("Dane logowania służą wyłącznie do pobrania klucza urządzeń — hasło nie jest nigdzie zapisywane.\n")

    email = args.email or input("E-mail konta Meross: ").strip()
    if not email:
        print("Bez e-maila się nie zalogujemy.", file=sys.stderr)
        return 2
    try:
        password = getpass("Hasło (nie będzie widoczne): ")
    except (EOFError, KeyboardInterrupt):
        print("\nPrzerwane.", file=sys.stderr)
        return 2
    if not password:
        print("Puste hasło — przerywam.", file=sys.stderr)
        return 2

    mfa_code = args.mfa_code
    print("\nŁączę się z chmurą...")
    try:
        try:
            key, devices = asyncio.run(fetch_key_and_devices(api_base_url, email, password, mfa_code))
        except BaseException as first_exc:
            # Brak MFA w pierwszym strzale — dopytaj i spróbuj jeszcze raz, bez ponownego
            # wpisywania hasła. (MissingMFA importujemy tu, bo meross_iot jest już dostępne.)
            from meross_iot.model.http.exception import MissingMFA

            if not isinstance(first_exc, MissingMFA) or mfa_code:
                raise
            print("\nKonto wymaga kodu dwuetapowego (MFA).")
            try:
                mfa_code = input("Kod z aplikacji Authenticator: ").strip() or None
            except (EOFError, KeyboardInterrupt):
                print("\nPrzerwane.", file=sys.stderr)
                return 2
            if not mfa_code:
                print(explain_failure(first_exc, api_base_url), file=sys.stderr)
                return 1
            print("Próbuję ponownie z kodem MFA...")
            key, devices = asyncio.run(fetch_key_and_devices(api_base_url, email, password, mfa_code))
    except KeyboardInterrupt:
        print("\nPrzerwane.", file=sys.stderr)
        return 1
    except BaseException as exc:  # komunikat po polsku zamiast stacktrace'u
        if isinstance(exc, SystemExit):
            raise
        print("\n" + explain_failure(exc, api_base_url), file=sys.stderr)
        return 1
    finally:
        del password  # nie trzymamy hasła w pamięci dłużej, niż trzeba

    if not key:
        print("Chmura nie zwróciła klucza urządzeń — bez niego most nie pogada z gniazdkami.", file=sys.stderr)
        return 1

    print("Klucz urządzeń pobrany: {0}".format(mask_key(key)))

    if args.dry_run:
        print("\n--dry-run: nie zapisałem ani .env, ani {0}.".format(output_path))
    else:
        try:
            note = save_key_to_env(env_path, key)
        except OSError as exc:
            print(
                "Nie udało się zapisać klucza do {0}: {1}\n"
                "Jeśli uruchamiasz skrypt w kontenerze, domontuj .env i wskaż go przez --env-file.".format(
                    env_path, exc
                ),
                file=sys.stderr,
            )
            return 1
        print("Klucz zapisany: {0} (linia {1}=...).".format(note, ENV_KEY))

        try:
            write_cloud_devices(output_path, build_cloud_devices(devices))
        except OSError as exc:
            print("Uwaga: nie udało się zapisać {0}: {1}".format(output_path, exc), file=sys.stderr)
        else:
            print("Spis urządzeń z chmury: {0}".format(output_path))

    print("\nUrządzenia widziane przez chmurę ({0}):".format(len(devices)))
    for device in devices:
        info = device_summary(device)
        print(
            "  {0}  {1:<24} {2:<10} {3}".format(
                getattr(device, "uuid", "?"),
                info.get("name", "?"),
                info.get("type", "?"),
                "kanałów: {0}".format(info.get("channels")) if info.get("channels") else "",
            ).rstrip()
        )

    types = sorted({str(device_summary(device).get("type", "?")) for device in devices})
    print("\nTypy urządzeń: {0}".format(", ".join(types) if types else "brak"))
    if args.dry_run:
        print("Uruchom bez --dry-run, żeby zapisać klucz do {0}.".format(env_path))
    else:
        print(
            "Od tej chwili chmura Meross NIE jest do niczego potrzebna: most rozmawia z gniazdkami\n"
            "po LAN-ie (HTTP POST /config), a klucz masz już w .env. Kolejny krok: `./dot.sh discover`,\n"
            "żeby wpisać adresy IP do bridge/config/devices.json, a potem `./dot.sh up`."
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
