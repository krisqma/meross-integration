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
