# CONTRACT.md — zamrożony interfejs stacku

Ten plik jest **źródłem prawdy** dla wszystkich strumieni pracy (A–E). Powstał w fazie 0, zanim
którykolwiek strumień ruszył, właśnie po to, żeby czterech agentów mogło pisać kod równolegle
i żeby te kawałki po prostu do siebie pasowały w fazie 2.

**Zasada nadrzędna: kontrakt jest niezmienny.** Jeśli w trakcie pracy okaże się, że coś tu jest
błędne albo niewykonalne — nie zmieniaj tego pliku po cichu i nie „napraw” tego u siebie. Zgłoś
rozbieżność w podsumowaniu swojej pracy; scalanie robi faza 2.

---

## 1. Własność plików — tylko właściciel dotyka swoich plików

Strumienie pracują **w tym samym katalogu, bez gałęzi i bez worktree**. Jedyne zabezpieczenie
przed konfliktem to rozłączna własność plików. Nie edytuj, nie formatuj i nie „porządkuj” plików,
których nie masz na swojej liście — nawet jeśli widzisz w nich błąd.

| Właściciel | Pliki |
|---|---|
| **FAZA 0** (gotowe, zamrożone) | `.gitignore`, `CONTRACT.md`, `README.md`, `.env.example`, `docker-compose.yml`, `webapp/Dockerfile`, `webapp/requirements.txt`, `pytest.ini`, `tests/requirements-dev.txt` |
| **STRUMIEŃ A** (infrastruktura) | `mosquitto/config/mosquitto.conf`, `mosquitto/config/acl`, `bridge/config/config.yml.tmpl`, `dot.sh`, `tests/unit/test_dotsh.py` |
| **STRUMIEŃ B** (narzędzia) | `tools/discover.py`, `tools/meross_login.py`, `tests/unit/test_discover.py`, `tests/unit/fixtures/*` |
| **STRUMIEŃ C** (atrapy) | `tests/fakes/*`, `docker-compose.test.yml`, `tests/integration/*` |
| **STRUMIEŃ D** (webapp — całość, core + harmonogramy) | `webapp/app/*`, `webapp/app/static/*`, `tests/unit/test_state.py`, `tests/unit/test_api.py`, `tests/unit/test_scheduler.py` |

Dodatkowo **nikt nie modyfikuje katalogu `meross2mqtt/`** — to osobny klon forka
`depau/meross2mqtt` z własnym `.git`, ma zostać aktualizowalny przez `git pull`. Jest w
`.gitignore` i celowo nie jest submodułem.

Pliki współdzielone z fazy 0 zawierają już wszystkie zależności i całą konfigurację, jakiej
strumienie potrzebują. Jeśli brakuje Ci biblioteki — zgłoś to, nie dopisuj jej sam.

---

## 2. Tematy MQTT (Homie 4.0)

Most `meross2mqtt` publikuje konwencję Homie 4.0 pod prefiksem `homie` (konfigurowalne przez
`homie_prefix`, u nas zostaje `homie`). Poniższe wynika wprost z `meross2homie/homie.py`
i `meross2homie/device.py` w klonie — nie z domysłów.

### 2.1 Identyfikator urządzenia `<dev>`

`<dev>` to **UUID gniazdka** (32 znaki hex, małe litery). Most używa UUID-a, chyba że w
`config.yml` ustawiono dla urządzenia `pretty_topic` — wtedy bierze `pretty_topic`
(`manager.py`, `_process_message`).

> **Wymóg dla strumienia A:** w `bridge/config/config.yml.tmpl` **nie ustawiaj `pretty_topic`**.
> Nazwę czytelną dla człowieka podawaj wyłącznie jako `pretty_name` — ona trafia do `$name`.
> Dzięki temu `<dev>` == UUID i jest stabilne.

> **Wymóg dla strumienia D:** mimo powyższego webapp **nie zakłada formatu `<dev>`**. Lista
> urządzeń powstaje z obserwacji tematów `homie/+/$state`, a lista węzłów z `$nodes`. Traktuj
> `<dev>` jako nieprzezroczysty identyfikator.

### 2.2 Stan (retained)

```
homie/<dev>/<node>/<prop>              # wartość własności
```

### 2.3 Atrybuty (retained)

Urządzenie:

```
homie/<dev>/$homie            # "4.0.0"
homie/<dev>/$name             # nazwa czytelna (pretty_name albo nazwa z chmury Meross)
homie/<dev>/$state            # init | ready | lost | disconnected | sleeping | alert
homie/<dev>/$nodes            # lista węzłów po przecinku, np. "system,switch,energy,electricity"
homie/<dev>/$implementation   # "meross2homie"
homie/<dev>/$extensions
homie/<dev>/$localip          # IP gniazdka w LAN-ie   (rozszerzenie legacy-firmware)
homie/<dev>/$mac              # MAC gniazdka           (rozszerzenie legacy-firmware)
homie/<dev>/$fw/name
homie/<dev>/$fw/version
```

`$state` = `lost` jest ustawiane przez LWT brokera, gdy most padnie
(`Homie.add_device`: `Will(f"{prefix}/{topic}/$state", "lost", True)`).

Węzeł:

```
homie/<dev>/<node>/$name
homie/<dev>/<node>/$type        # switch | stats | settings | system
homie/<dev>/<node>/$properties  # lista własności po przecinku
```

Własność:

```
homie/<dev>/<node>/<prop>/$name
homie/<dev>/<node>/<prop>/$datatype   # boolean | float | string | enum
homie/<dev>/<node>/<prop>/$settable   # "true" | "false"
homie/<dev>/<node>/<prop>/$retained   # "true" | "false"
homie/<dev>/<node>/<prop>/$unit       # tylko gdy własność ma jednostkę
homie/<dev>/<node>/<prop>/$format     # tylko dla enum i JSON
```

### 2.4 Sterowanie

```
publish -> homie/<dev>/<node>/<prop>/set
```

Payload dla `boolean` to dokładnie `true` albo `false` (małymi literami — `HomieBooleanProperty.deserialize`
traktuje **wszystko poza dosłownym `"true"` jako `false`**, więc `True`, `1`, `on` się nie liczą).

Most subskrybuje `/set` wyłącznie dla własności, które mają setter (czyli `$settable true`).

### 2.5 Węzły i własności, jakie tworzy most

`N` to numer kanału. Kanał 0 nie ma sufiksu, kanały 1+ mają `-N` (`_channel_topic()` w `device.py`).
Listwa czterogniazdkowa da więc `switch`, `switch-1`, `switch-2`, `switch-3`.

| Węzeł | `$type` | Własność | `$datatype` | `$unit` | `$settable` | Uwagi |
|---|---|---|---|---|---|---|
| `system` | `system` | `reboot` | `enum` | — | `true` | `$format REQUEST`, nieretainowana; publikacja `REQUEST` restartuje gniazdko |
| `switch` / `switch-N` | `switch` | `power` | `boolean` | — | `true` | jedyny temat do przełączania |
| `energy` / `energy-N` | `stats` | `daily` | `float` | `kWh` | `false` | dzienne zużycie |
| | | `total` | `float` | `kWh` | `false` | suma z całej historii |
| | | `history` | `string` | — | `false` | `$format json`, tablica `{timestamp, total_consumption_kwh}` |
| `electricity` / `electricity-N` | `stats` | `voltage` | `float` | `V` | `false` | |
| | | `current` | `float` | `A` | `false` | |
| | | `power` | `float` | `W` | `false` | moc chwilowa |
| `dnd` | `settings` | `dnd` | `boolean` | — | `true` | tryb „nie przeszkadzać” (dioda) |

**Węzeł `system` istnieje zawsze**, reszta tylko jeśli urządzenie ma daną zdolność
(`ToggleX`, `ConsumptionX`, `Electricity`, `DNDMode`). Webapp musi bez błędu obsłużyć gniazdko,
które ma sam `switch` i nic poza tym.

Uwaga na kolizję nazw: `power` istnieje w dwóch znaczeniach — `switch/power` to **stan włączenia**
(boolean), a `electricity/power` to **moc w watach** (float).

---

## 3. REST API webappa

Baza: `http://<host>:${WEB_PORT}`. Odpowiedzi i błędy w JSON. Brak uwierzytelniania (świadoma
decyzja: panel siedzi w LAN-ie, broker tylko na loopbacku).

### `GET /api/devices`

```json
{
  "devices": [
    {
      "id": "2103163085044890845648e1e962e3a6",
      "name": "Gniazdko salon",
      "state": "ready",
      "mac": "48:e1:e9:62:e3:a6",
      "ip": "192.168.1.122",
      "fw": "6.1.9",
      "channels": [
        {
          "node": "switch",
          "name": "Switch",
          "on": true,
          "settable": true,
          "power_w": 41.2,
          "voltage_v": 233.1,
          "current_a": 0.18,
          "energy_today_kwh": 0.42
        }
      ]
    }
  ]
}
```

- `state` — dosłownie wartość z `$state` (`init`/`ready`/`lost`/`disconnected`/`sleeping`/`alert`).
- `mac`, `ip`, `fw`, `name` — `null`, jeśli most jeszcze ich nie opublikował.
- `channels[]` — po jednym wpisie na węzeł `switch*`; `node` to nazwa węzła Homie
  (`switch`, `switch-1`, …), którą trzeba potem oddać w POST-ach.
- **Pola pomiarowe (`power_w`, `voltage_v`, `current_a`, `energy_today_kwh`) są `null`, gdy
  urządzenie ich nie ma** albo gdy jeszcze nie przyszedł pierwszy odczyt. Frontend musi to
  ogarnąć bez wysypywania się.
- `power_w`/`voltage_v`/`current_a` pochodzą z węzła `electricity[-N]`, a `energy_today_kwh`
  z `energy[-N]/daily` — dla kanału o tym samym numerze co `switch[-N]`.

### `GET /api/stream`

Server-Sent Events. Nazwa zdarzenia: **`state`**. Pole `data` to JSON o **dokładnie tym samym
kształcie co `GET /api/devices`** (czyli obiekt z kluczem `devices`), żeby frontend miał jedną
ścieżkę renderowania.

```
event: state
data: {"devices":[...]}
```

Po nawiązaniu połączenia serwer wysyła pełny stan natychmiast, a potem przy każdej zmianie.

### `POST /api/devices/{dev}/{node}/power`

Body: `{"on": true}` → publikuje `homie/{dev}/{node}/power/set` z `true`/`false`.
Odpowiedź: **`202 Accepted`** (to jest polecenie „wyślij”, nie potwierdzenie, że gniazdko kliknęło;
prawdziwy stan wróci przez SSE po pollingu mostu).

### `POST /api/devices/{dev}/{node}/timer`

Body: `{"minutes": 45, "on": true}` → „ustaw na 45 minut w stan `on`”, czyli akcja natychmiast
plus jednorazowe zadanie odwrotne za `minutes` minut. Odpowiedź: **`201 Created`** z utworzonym
harmonogramem. **Trasę obsługuje moduł harmonogramów** (`scheduler.router`), nie moduł urządzeń.

### Harmonogramy

| Metoda i ścieżka | Body | Odpowiedź |
|---|---|---|
| `GET /api/schedules` | — | `{"schedules": [ <harmonogram>, ... ]}` |
| `POST /api/schedules` | `<harmonogram bez id>` | `201` + utworzony `<harmonogram>` |
| `PATCH /api/schedules/{id}` | `{"enabled": bool}` | `200` + zaktualizowany `<harmonogram>` |
| `DELETE /api/schedules/{id}` | — | `204` |

### `GET /api/health`

```json
{"mqtt": true, "devices": 3, "jobs": 5}
```

`mqtt` = `hub.connected`, `devices` = liczba widzianych urządzeń, `jobs` = `scheduler.job_count()`.

---

## 4. Model harmonogramu i baza SQLite

Baza: `${DB_PATH}` = `/data/gniazdka.db` w kontenerze (host: `./data/gniazdka.db`).
**SQLite jest źródłem prawdy**, APScheduler to tylko silnik czasu — joby są odtwarzane z bazy
przy każdym starcie, żeby przeżywały restart kontenera i podmianę kodu.

```sql
CREATE TABLE IF NOT EXISTS schedules (
    id         INTEGER PRIMARY KEY,
    label      TEXT    NOT NULL,   -- etykieta dla człowieka
    dev        TEXT    NOT NULL,   -- <dev> z tematów Homie
    node       TEXT    NOT NULL,   -- np. "switch" albo "switch-1"
    action     TEXT    NOT NULL,   -- "on" | "off"
    kind       TEXT    NOT NULL,   -- "cron" | "once" | "timer"
    hour       INTEGER,            -- tylko dla kind="cron"
    minute     INTEGER,            -- tylko dla kind="cron"
    days       TEXT,               -- tylko dla kind="cron": dni ISO po przecinku, 1=poniedziałek ... 7=niedziela, np. "1,2,3,4,5"
    run_at     TEXT,               -- tylko dla kind="once"|"timer": ISO8601
    enabled    INTEGER NOT NULL,   -- 0 | 1
    created_at TEXT    NOT NULL    -- ISO8601
);
```

Reprezentacja w API to te same pola plus jedno **wyliczane, niezapisywane**:

- `next_run` — ISO8601 najbliższego uruchomienia albo `null` (dla wyłączonych i przeterminowanych).

`enabled` w JSON-ie jest booleanem (`true`/`false`), w bazie `INTEGER` 0/1.

Czas: wszystko liczone w strefie **`Europe/Warsaw`** (`TZ` z `.env`). To istotne dla crona typu
„codziennie 6:30” w dni zmiany czasu — to realna klasa błędów i ma być pokryta testem.

---

## 5. Styk w Pythonie (krytyczny — dzieli moduły webappa)

Nazwy poniżej są **dosłowne**. To po nich dwie połówki webappa się spotykają.

### `webapp/app/mqtt.py`

```python
class MqttHub:
    connected: bool                       # stan połączenia z brokerem, dla /api/health

    async def publish(self, topic: str, payload: str) -> None: ...
    async def publish_set(self, dev: str, node: str, prop: str, value: str) -> None:
        """Publikuje na homie/<dev>/<node>/<prop>/set."""
    def snapshot(self) -> dict:
        """Aktualny stan w kształcie GET /api/devices, czyli {"devices": [...]}."""
    def subscribe_updates(self, cb) -> ...:
        """Rejestruje callback wołany przy każdej zmianie stanu — źródło dla SSE."""
```

### `webapp/app/scheduler.py`

Moduł wystawia **dokładnie** te cztery nazwy:

```python
router: APIRouter                                  # wszystkie trasy /api/schedules*
                                                   # ORAZ POST /api/devices/{dev}/{node}/timer
async def start_scheduler(hub: "MqttHub") -> None  # wczytuje włączone joby z SQLite i startuje
async def stop_scheduler() -> None                 # czyste zamknięcie
def job_count() -> int                             # dla /api/health
```

### `webapp/app/main.py`

W lifespanie aplikacji:

```python
hub = MqttHub(...)
app.state.hub = hub
app.include_router(scheduler.router)
await start_scheduler(hub)
...
await stop_scheduler()
```

Harmonogramy sięgają po hub **wyłącznie** przez obiekt przekazany do `start_scheduler`
(albo przez `request.app.state.hub` w trasach) — żadnych globali współdzielonych między modułami.

---

## 6. Styk we frontendzie

Bez build-stepu. Czysty JS jako moduły ES, ciemny motyw, **interfejs w całości po polsku**.

`webapp/app/static/index.html` zawiera pusty kontener i ładuje moduł harmonogramów:

```html
<section id="schedules" class="card"></section>
...
<script type="module" src="/static/schedules.js"></script>
```

`schedules.js` montuje się w `#schedules` i nie dotyka reszty DOM-u.

`style.css` **musi** definiować klasy używane przez moduł harmonogramów:

```
.card  .row  .muted  .badge  .btn  .btn-primary  .btn-danger  .input  .select  .form-grid  .switch
```

---

## 7. Zmienne środowiskowe, porty i nazwy usług

Wzorzec w `.env.example`, realny plik `.env` jest w `.gitignore`.

| Zmienna | Wartość domyślna | Do czego |
|---|---|---|
| `TZ` | `Europe/Warsaw` | wszystkie kontenery, kluczowe dla harmonogramów |
| `MQTT_HOST` | `mosquitto` | nazwa usługi w sieci Compose |
| `MQTT_PORT` | `1883` | |
| `HOMIE_PREFIX` | `homie` | musi być identyczny w `config.yml` mostu i w webappie |
| `DB_PATH` | `/data/gniazdka.db` | ścieżka **w kontenerze** |
| `WEB_PORT` | `8080` | port na hoście *i* w kontenerze |
| `MEROSS_KEY` | (puste) | klucz z chmury, wypełnia `./dot.sh login` |
| `MEROSS_API_URL` | `https://iotx-eu.meross.com` | region chmury Meross |
| `LAN_SUBNET` | `192.168.1.0/24` | zakres skanowania w `tools/discover.py` |
| `FAKE_DEVICE_KEY` | `testkey123` | klucz atrap gniazdek w testach |

Nazwy usług w Compose (stałe, używane jako nazwy hostów): **`mosquitto`**, **`bridge`**, **`webapp`**.

Porty:

- `127.0.0.1:1883:1883` — broker **tylko na loopbacku**, celowo. Nie zmieniaj tego na `0.0.0.0`.
- `${WEB_PORT}:8080` — panel na wszystkich interfejsach, ma być dostępny z LAN-u.

Wolumeny:

- `./mosquitto/config:/mosquitto/config:ro`, `./mosquitto/data`, `./mosquitto/log`
- `./bridge/config:/config` — **zapisywalny**, most trzyma tam `devices.json` obok `config.yml`
- `./webapp/app:/app/app` (żywy kod) i `./data:/data`

---

## 8. Wymagania wobec strumienia A (infrastruktura)

1. **`dot.sh` musi doklonować most, gdy go nie ma.** Katalog `meross2mqtt/` jest w `.gitignore`,
   więc na świeżej maszynie (np. na Raspberry Pi po `git clone`) po prostu go nie będzie, a
   `docker compose build bridge` się wtedy wywali. Przed `up`/`build` sprawdź, czy katalog
   istnieje, i jeśli nie — `git clone https://github.com/depau/meross2mqtt`. Istniejącego klonu
   nie ruszaj (żadnego `git pull` bez pytania — użytkownik może mieć tam własne zmiany).
2. **`mosquitto.conf` i `acl` muszą przepuścić healthcheck z `docker-compose.yml`**, czyli
   anonimowe `mosquitto_sub` z `127.0.0.1` na temat `$SYS/broker/uptime` wewnątrz kontenera.
   Jeśli ACL to zablokuje, usługa nigdy nie stanie się `healthy`, a `bridge` i `webapp` mają
   `depends_on: condition: service_healthy` i nie wystartują.
3. Listener `1883` dla mostu i webappa; przygotowany, **zakomentowany** blok listenera `8883`
   z TLS i ACL dla `/appliance/#` (wymaga `per_listener_settings true`) — furtka na późniejsze
   odcięcie chmury.
4. W `config.yml.tmpl`: `homie_prefix: homie`, `enable_http: true`, `mqtt_clean_session: true`,
   `try_reboot_on_timeout: false`, `polling_interval` 20–30 s, `persistence_file` w `/config`,
   `pretty_name` bez `pretty_topic` (patrz §2.1).

## 9. Inwentarz sprzętu (potwierdzony skanem)

Sieć `192.168.1.0/24`, host `192.168.1.114`, brama `192.168.1.1`.

| IP | UUID | MAC |
|---|---|---|
| `192.168.1.122` | `2103163085044890845648e1e962e3a6` | `48:e1:e9:62:e3:a6` |
| `192.168.1.177` | `2101157387887490839348e1e945c257` | `48:e1:e9:45:c2:57` |
| `192.168.1.240` | `2101070928400790839048e1e944b53f` | `48:e1:e9:44:b5:3f` |

Wszystkie trzy odpowiadają na lokalne API HTTP `POST /config` i zwracają `5001 sign error`
przy pustym kluczu, czyli mają ustawiony klucz z chmury. Te same UUID-y (i klucz
`FAKE_DEVICE_KEY`) mają udawać atrapy ze strumienia C, żeby testy integracyjne odpowiadały
temu, co jest w domu.
