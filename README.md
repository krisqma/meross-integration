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

Kolejność ma znaczenie: `login` daje klucz, `discover` daje adresy gniazdek, a dopiero
`up` podnosi stack, który jednego i drugiego potrzebuje.

```bash
git clone <to-repo> tata && cd tata

# Most jest osobnym repozytorium i jest w .gitignore — trzeba go doklonować.
# ./dot.sh robi to automatycznie, gdy katalogu nie ma.
git clone https://github.com/depau/meross2mqtt

./dot.sh login            # 1. jednorazowo: klucz z chmury Meross (zapisuje TYLKO klucz)
./dot.sh discover         # 2. skan LAN-u -> UUID-y i IP do bridge/config/devices.json
./dot.sh up               # 3. generuje config.yml i podnosi cały stack
```

`./dot.sh up` samo tworzy `.env` z `.env.example`, jeśli go nie ma, i za każdym razem
generuje `bridge/config/config.yml` z szablonu `bridge/config/config.yml.tmpl` — tego
wygenerowanego pliku nie edytuj, bo zostanie nadpisany.

Panel: `http://<ip-hosta>:8080`. Logi: `./dot.sh logs [usługa]`, stan: `./dot.sh status`,
zatrzymanie: `./dot.sh down`.

### `login` na Linuksie i na Raspberry Pi

`./dot.sh login` leci w kontenerze mostu (tylko tam jest `meross_iot`) i pisze do
zamontowanego repozytorium: `.env` oraz `bridge/config/cloud-devices.json`. Kontener chodzi
jako root, więc na Linuksie oba pliki stałyby się własnością roota i kolejne `./dot.sh up`
nie mogłoby już nadpisać configu. Dlatego `dot.sh` sam dokłada tam
`--user "$(id -u):$(id -g)"`. Jeśli wołasz Compose'a ręcznie, zrób to samo:

```bash
docker compose run --rm --no-deps --interactive --user "$(id -u):$(id -g)" \
  --entrypoint python3 -v "$PWD:/project" -w /project \
  bridge tools/meross_login.py
```

Na macOS (Docker Desktop) nie jest to potrzebne — właściciel plików jest mapowany na
użytkownika hosta. Gdyby `.env` już należał do roota: `sudo chown "$(id -u):$(id -g)" .env`.

## Testy

```bash
./dot.sh test unit    # 170 testów, bez Dockera i bez sieci, ~2 s
./dot.sh test int     # 15 testów na pełnym stacku z atrapami gniazdek, ~60 s
./dot.sh test hw      # opt-in, dotyka prawdziwych gniazdek (na razie pusty zestaw)
```

Domyślnie `pytest` pomija testy `integration` i `hardware` (patrz `pytest.ini`).
Testy zawsze lecą w kontenerze (`docker-compose.test.yml`, usługa `tests`), bo celują
w Pythona 3.12, a systemowy Python na macOS to 3.9.

### Co dokładnie sprawdzają testy integracyjne

`./dot.sh test int` podnosi osobny projekt Compose `gniazdka-test`: broker, **trzy atrapy
gniazdek** (dwie jednokanałowe `mss310` i jedna 4-kanałowa listwa `mss425e`, z tymi samymi
UUID-ami co sprzęt w domu), **prawdziwy most** i **prawdziwy panel** pod uvicornem. Zero
sprzętu, zero chmury, zero klucza Meross — atrapy weryfikują podpis kluczem
`FAKE_DEVICE_KEY`. Sprawdzane jest m.in.:

- most wystawia wszystkie atrapy jako `$state ready` i buduje węzły `switch`, `switch-1`…
- `GET /api/devices` pokazuje trzy urządzenia z właściwą liczbą kanałów oraz odczytami W/V/A i kWh
- `POST /api/devices/{dev}/{node}/power` naprawdę dochodzi do atrapy jako
  `Appliance.Control.ToggleX` (licznik w `/debug/state` atrapy), a nie tylko odbija stan w panelu
- zmiana zrobiona „na obudowie" atrapy wraca do panelu po cyklu pollingu
- SSE pod prawdziwym uvicornem wysyła ramkę od razu po połączeniu i kolejną po zmianie stanu
- harmonogram jednorazowy realnie odpala i przełącza atrapę

Panel testowy słucha na `127.0.0.1:8081` (produkcja na `8080`), więc można w niego zajrzeć
w przeglądarce po `docker compose -f docker-compose.test.yml up -d --build --wait`. Panel
testowy i produkcyjny **nie mogą chodzić jednocześnie** — używają tego samego
identyfikatora klienta MQTT `gniazdka-webapp`.

## Co działa, a co czeka na sprzęt

Sprawdzone uruchomieniowo na atrapach: broker, most, panel, przełączanie, odczyty energii,
SSE i harmonogramy — cała droga `panel -> MQTT -> most -> gniazdko` i powrót przez polling.

`./dot.sh up` bez `MEROSS_KEY` podniesie stack i **nie** wpadnie w pętlę restartów: broker
jest `healthy`, panel odpowiada na `:8080` z `{"mqtt":true,"devices":0}`, a most startuje,
puka do gniazdek z `devices.json` i dostaje od nich `5001 sign error` (w logach widać wtedy
`KeyError: 'all'` z nieudanego interview). To oczekiwane — bez klucza gniazdka odrzucają
zapytania. Po `./dot.sh login` i `./dot.sh discover` most przepytuje je normalnie.

Nadal niesprawdzone na prawdziwym sprzęcie: klucz z chmury, faktyczne kliknięcie w gniazdku,
przycisk na obudowie, dokładność odczytów mocy i harmonogram na żywo przez dobę.

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
odtworzyć: `meross2mqtt/`, `.env`, `bridge/config/config.yml`, `bridge/config/devices.json`,
`bridge/config/cloud-devices.json` oraz `data/gniazdka.db` (harmonogramy — jeśli chcesz je
zachować, skopiuj ten plik). `config.yml` odtworzy się sam przy `./dot.sh up`.

Pamiętaj o `--user` przy `login` na Pi — `dot.sh` robi to za Ciebie, ale ręczne wywołania
Compose'a już nie (patrz „`login` na Linuksie i na Raspberry Pi" wyżej).

Raspberry Pi jest tu wyraźnie lepsze od Maca: Mac usypia i harmonogramy wtedy nie odpalają.

## Struktura

| Ścieżka | Co to |
|---|---|
| `CONTRACT.md` | zamrożony interfejs: tematy MQTT, REST, schemat bazy, ENV, podział pracy |
| `docker-compose.yml` | produkcja: `mosquitto`, `bridge`, `webapp` |
| `docker-compose.test.yml` | stack testowy: broker, 3 atrapy, most, panel, runner testów |
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
