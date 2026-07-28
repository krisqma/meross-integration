# Gniazdka — lokalne sterowanie gniazdkami Meross

Kompletny, lokalny stack na Dockerze: broker MQTT, most `meross2mqtt` (rozmawia z gniazdkami
po HTTP w LAN-ie, bez chmury w czasie działania) i własny panel webowy z przełącznikami,
odczytem zużycia energii i harmonogramami.

Chmura Meross jest potrzebna **jeden raz**, żeby pobrać klucz urządzeń. Potem można ją odciąć —
gniazdka nie muszą się łączyć z naszym brokerem, most odpytuje je bezpośrednio po adresie IP.
Aplikacja Meross w telefonie działa dalej.

```
przeglądarka ──REST+SSE──> webapp ──homie/#──> mosquitto <──homie/#── bridge ──HTTP──> gniazdka
                             │
                             └── SQLite (harmonogramy)
```

## Wymagania

- Docker z BuildKitem (Docker Desktop albo Docker Engine ≥ 23) i Compose v2+
- Gniazdka Meross w tej samej sieci co host
- Nic poza tym — Python na hoście nie jest potrzebny, wszystko leci w kontenerach

## Start

```bash
git clone <to-repo> tata && cd tata

# Most jest osobnym repozytorium i jest w .gitignore — trzeba go doklonować.
# ./dot.sh robi to automatycznie, gdy katalogu nie ma.
git clone https://github.com/depau/meross2mqtt

cp .env.example .env      # uzupełnij MEROSS_KEY albo zrób to poniższym `login`

./dot.sh login            # jednorazowo: pobiera klucz z chmury Meross (zapisuje TYLKO klucz)
./dot.sh discover         # skanuje LAN i wpisuje UUID-y oraz IP do bridge/config/devices.json
./dot.sh up               # buduje i podnosi cały stack
```

Panel: `http://<ip-hosta>:8080`. Logi: `./dot.sh logs`, stan: `./dot.sh status`,
zatrzymanie: `./dot.sh down`.

Bez `dot.sh` (strumień A dopiero go dostarcza) wystarczy `docker compose up -d --build`,
ale najpierw musi istnieć `bridge/config/config.yml`.

## Testy

```bash
./dot.sh test unit    # szybkie, bez Dockera i bez sieci
./dot.sh test int     # pełny stack na atrapach gniazdek, bez sprzętu i bez chmury
./dot.sh test hw      # opt-in, dotyka prawdziwych gniazdek
```

Domyślnie `pytest` pomija testy `integration` i `hardware` (patrz `pytest.ini`).
Testy wymagają Pythona 3.12 — systemowy Python na macOS to 3.9, więc użyj venva albo kontenera.

## Przeniesienie na Raspberry Pi

Wszystkie obrazy są multi-arch i całość była budowana na arm64, więc przeprowadzka to
przekopiowanie repozytorium i jeden `up`:

```bash
git clone <to-repo> tata && cd tata
git clone https://github.com/depau/meross2mqtt      # albo po prostu ./dot.sh up
cp .env.example .env                                 # i przepisz MEROSS_KEY ze starej maszyny
./dot.sh up
```

Czego **nie** ma w repozytorium (bo jest w `.gitignore`) i trzeba przenieść ręcznie albo
odtworzyć: `meross2mqtt/`, `.env`, `bridge/config/config.yml`, `bridge/config/devices.json`
oraz `data/gniazdka.db` (harmonogramy — jeśli chcesz je zachować, skopiuj ten plik).

Raspberry Pi jest tu wyraźnie lepsze od Maca: Mac usypia i harmonogramy wtedy nie odpalają.

## Struktura

| Ścieżka | Co to |
|---|---|
| `CONTRACT.md` | zamrożony interfejs: tematy MQTT, REST, schemat bazy, ENV, podział pracy |
| `docker-compose.yml` | trzy usługi: `mosquitto`, `bridge`, `webapp` |
| `meross2mqtt/` | klon forka `depau/meross2mqtt` — **nie modyfikujemy**, ma się dać `git pull` |
| `bridge/config/` | `config.yml` mostu i generowany `devices.json` |
| `mosquitto/config/` | konfiguracja brokera i ACL |
| `webapp/` | FastAPI + APScheduler + statyczny frontend (vanilla JS, bez build-stepu) |
| `tools/` | `discover.py` (skan LAN), `meross_login.py` (klucz z chmury) |
| `tests/` | `unit/`, `integration/`, `fakes/` (atrapy gniazdek) |
| `data/` | SQLite z harmonogramami |

## Bezpieczeństwo

Broker nasłuchuje wyłącznie na `127.0.0.1:1883` i nie ma uwierzytelniania — dostęp mają tylko
kontenery i host. Panel webowy jest wystawiony na LAN **bez logowania**; to świadoma decyzja
na sieć domową. Klucz Meross trzymamy w `.env` i w `bridge/config/config.yml`, oba są
w `.gitignore`. Hasło do konta Meross nie jest nigdzie zapisywane.
