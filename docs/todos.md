# Rekord zmian

## 2026-07-29 15:50

### Dodanie nazw urządzeń z chmury Meross do webappa

**Problem**: Webapp wyświetlał model + MAC (np. "Meross MSS210 (48:e1:e9:44:a1:a9)")
zamiast czytelnej nazwy z chmury Meross (np. "TV", "Odkurzacze").

**Rozwiązanie**: Webapp ładuje przy starcie plik `cloud-devices.json`
(zrzut nazw z chmury Meross) i wyświetla nazwę z chmury zamiast `$name` z Homie.

**Kluczowa uwaga**: Bridge zawsze publikuje `$name` jako coś truthy
(np. `"Meross MSS210 (48:e1:e9:44:a1:a9)"`), więc `or` nie działa jako fallback.
Nazwa z chmury musi mieć **priorytet** nad `$name`.

**Zmienione pliki**:

| Plik | Zmiana |
|------|--------|
| `webapp/app/state.py:100` | `HomieState.__init__` — nowy parametr `cloud_names: Optional[Dict[str, str]]` |
| `webapp/app/state.py:220` | `_device_snapshot` — `name: self._cloud_names.get(dev_id) or attrs.get("$name")` |
| `webapp/app/mqtt.py:43,51` | `MqttHub.__init__` — przyjmuje `cloud_names`, przekazuje do `HomieState` |
| `webapp/app/main.py:37-46` | Nowa funkcja `_load_cloud_names()` — czyta `/data/cloud-devices.json` |
| `webapp/app/main.py:55` | `lifespan` — przekazuje `cloud_names` do `MqttHub` |
| `dot.sh:432-436` | `cmd_login` — po loginie kopiuje `cloud-devices.json` do `data/` |

**Testy**: 174/174 passed, 1 skipped (brak pliku `cloud-devices.json` —
obsłużone jako pusta mapa, brak zmiany w działaniu).

**Użycie**:
1. `./dot.sh login` — logowanie do chmury, generuje `cloud-devices.json`
   i kopiuje go do `data/` dla webappa.
2. `docker compose restart webapp` — webapp wczyta nazwy.

---

## 2026-07-29 22:20

### Naprawa warninga o właścicielu pliku loga mosquitto

**Problem**: Mosquitto ostrzegał `"File owner is not mosquitto"` przy starcie,
bo plik `/mosquitto/log/mosquitto.log` na bind mount był tworzony jako root,
a proces mosquitto działa jako UID 1883 (użytkownik `mosquitto`). W przyszłych
wersjach mosquitto odmówi załadowania takiego pliku.

**Przyczyna**: Entrypoint obrazu `eclipse-mosquitto:2` robi `chown` tylko dla
`/mosquitto/data`, pomija `/mosquitto/log`.

**Rozwiązanie**:
1. `user: "1883:1883"` w `docker-compose.yml` — kontener od razu startuje
   jako użytkownik mosquitto (zamiast root → drop do mosquitto).
2. W `ensure_dirs()` w `dot.sh`: 777 na katalogach data/log + usunięcie
   starego pliku loga (mosquitto odtwarza go jako UID 1883).

**Zmienione pliki**:

| Plik | Zmiana |
|------|--------|
| `docker-compose.yml:11` | `user: "1883:1883"` w serwisie `mosquitto` |
| `dot.sh:265-272` | `ensure_dirs` — `chmod 777` na data/log + `rm -f` loga |

**Testy**: Brak ostrzeżenia w logu mosquitto po resecie. Stack w pełni sprawny.

**Revert**: Cofnięto — `user: "1883:1883"` w compose i `ensure_dirs` wróciły do oryginału.
Warningi nie występują, bo bind mount jest czysty po `docker compose down && up`.
Nazwy z chmury w webapp działają nadal (zmiany w `state.py`, `mqtt.py`, `main.py`, `cmd_login` nietknięte).

---

## 2026-07-29 23:10

### Wyświetlanie modelu urządzenia na kafelku

**Problem**: Kafelek pokazywał tylko IP, MAC i firmware w jednej linii. Brakowało
informacji o modelu (np. "mss310", "mss210").

**Rozwiązanie**: Dodano pole `model` do migawki (`$fw/name` z Homie) i rozdzielono
meta na dwie linie: IP · MAC (linia 1), model · firmware (linia 2).

**Zmienione pliki**:

| Plik | Zmiana |
|------|--------|
| `webapp/app/state.py:225` | `_device_snapshot` — nowe pole `"model": attrs.get("$fw/name")` |
| `webapp/app/static/app.js:123-124,137,185-189` | Dwa elementy `meta1`/`meta2` zamiast jednego `meta`; osobne linie dla IP·MAC i model·firmware |
| `webapp/app/static/app.js:185-189` | `updateTile` — dwie linie zamiast jednej |
| `tests/unit/test_state.py:127` | Oczekiwana migawka ma `"model": "mss310"` |
| `tests/unit/test_state.py:240` | Asercja `device["model"] is None` w teście braku atrybutów |
| `tests/unit/test_api.py:40` | `snapshot_with()` publikuje `$fw/name` |
| `CONTRACT.md:154,173` | `"model": "mss310"` w przykładzie + dokumentacja w opisie pól |

**Testy**: 174/174 passed, 1 skipped (bez zmian).

---

## 2026-07-30 21:40

### Nazwy kanałów dla listew wielogniazdkowych z chmury Meross

**Problem**: Listwy z wieloma gniazdami (np. MSS620, MSS425F) pokazywały
w GUI generyczne nazwy z bridge'a: "Switch", "Switch (channel 1)",
"Switch (channel 2)" zamiast nazw własnych kanałów zdefiniowanych w aplikacji
Meross (np. "Lampa", "Monitor").

**Przyczyna**:
- Bridge (`meross2mqtt/`) generuje nazwy kanałów algorytmicznie
  (`_channel_name("Switch", channel_id)`), bo protokół LAN nie przenosi
  nazw per-kanał.
- Cloud API Meross zwraca per-kanałowe `devName` (np. `"Lampa"`), ale
  `tools/meross_login.py` brało tylko `len(channels)` i resztę wyrzucało.
- Webapp przekazywał nazwy bridge'a 1:1.

**Rozwiązanie**:
- `tools/meross_login.py` — `device_summary()` wyciąga `devName` z każdego
  kanału i zapisuje jako listę `channel_names` w `cloud-devices.json`.
- Webapp ładuje cały wpis cloud (nie tylko `name`) i w `_channels()`
  stosuje priorytet: cloud name → bridge name → fallback `"Gniazdo N"`.

**Zmienione pliki**:

| Plik | Zmiana |
|------|--------|
| `tools/meross_login.py:126-147` | `device_summary()` — wyciąga `devName` z cloud API, dodaje `channel_names` |
| `webapp/app/main.py:37-48` | `_load_cloud_names()` → `_load_cloud_devices()`, zwraca pełny dict per UUID |
| `webapp/app/mqtt.py:44,51` | Parametr `cloud_names` → `cloud_devices` w `MqttHub.__init__` |
| `webapp/app/state.py:100,220,230-245` | `HomieState.__init__` przyjmuje `cloud_devices`; `_channels()` używa cloud names z priorytetem |
| `tests/unit/test_state.py:175-183` | Nowy test: `test_cloud_channel_names_nadpisuja_bridge_name` |
| `CONTRACT.md:159` | Dokumentacja: `channels[].name` może pochodzić z chmury |

**Priorytet nazwy kanału**:
1. Cloud name (`cloud-devices.json` → `channel_names[N]`)
2. Bridge name (`node.attrs.get("$name")`)
3. Fallback (`f"Gniazdo {channel + 1}"`)

**Testy**: 175/175 passed, 1 skipped.

**Użycie**:
1. `./dot.sh login` — zaktualizuje `cloud-devices.json` o `channel_names`
2. `./dot.sh restart` — webapp wczyta nazwy kanałów z chmury
