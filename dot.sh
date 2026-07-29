#!/usr/bin/env bash
#
# dot.sh — jeden punkt wejścia do stacku: broker MQTT + most meross2mqtt + panel webowy.
#
# Skrypt jest napisany pod bash 3.2 (taki jest w macOS), więc bez tablic asocjacyjnych
# i bez ${var,,}. Ścieżki można podmienić zmiennymi DOT_* — z tego korzystają testy
# w tests/unit/test_dotsh.py, żeby generować config.yml w katalogu tymczasowym.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

ROOT_DIR=${DOT_ROOT:-$SCRIPT_DIR}
ENV_FILE=${DOT_ENV_FILE:-$ROOT_DIR/.env}
ENV_EXAMPLE=${DOT_ENV_EXAMPLE:-$ROOT_DIR/.env.example}
TMPL_FILE=${DOT_TMPL:-$ROOT_DIR/bridge/config/config.yml.tmpl}
CONFIG_FILE=${DOT_CONFIG:-$ROOT_DIR/bridge/config/config.yml}
COMPOSE_FILE=${DOT_COMPOSE:-$ROOT_DIR/docker-compose.yml}
TEST_COMPOSE_FILE=${DOT_TEST_COMPOSE:-$ROOT_DIR/docker-compose.test.yml}
MOSQUITTO_CONF=${DOT_MOSQUITTO_CONF:-$ROOT_DIR/mosquitto/config/mosquitto.conf}
BRIDGE_DIR=${DOT_BRIDGE_DIR:-$ROOT_DIR/meross2mqtt}
BRIDGE_REPO=${DOT_BRIDGE_REPO:-https://github.com/depau/meross2mqtt}

# Nazwa kontenera brokera z docker-compose.yml — potrzebna do odczytu stanu healthchecku.
MOSQUITTO_CONTAINER=gniazdka-mosquitto

if [ -t 1 ]; then
    C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_DIM=$'\033[2m'; C_OFF=$'\033[0m'
else
    C_OK=''; C_WARN=''; C_ERR=''; C_DIM=''; C_OFF=''
fi

info() { printf '%s==>%s %s\n' "$C_OK" "$C_OFF" "$*"; }
warn() { printf '%s[uwaga]%s %s\n' "$C_WARN" "$C_OFF" "$*" >&2; }
err()  { printf '%s[błąd]%s %s\n' "$C_ERR" "$C_OFF" "$*" >&2; }
dim()  { printf '%s%s%s\n' "$C_DIM" "$*" "$C_OFF"; }
die()  { err "$*"; exit 1; }

# ---------------------------------------------------------------------------
# Pomoc
# ---------------------------------------------------------------------------

usage() {
    cat <<'EOF'
dot.sh — sterowanie lokalnym stackiem gniazdek Meross.

Użycie: ./dot.sh <komenda> [argumenty]

Komendy:
  up [usługa...]      Podnosi stack. Po drodze: doklonuje most, jeśli go nie ma,
                      tworzy .env z .env.example, generuje bridge/config/config.yml
                      z szablonu i w razie potrzeby startuje demona Dockera.
  down [argumenty]    Zatrzymuje i usuwa kontenery stacku.
  restart [usługa...] down + up (regeneruje config.yml, więc łapie zmiany w .env).
  logs [usługa]       Podgląd logów na żywo (bez usługi: wszystkie).
  status              Stan kontenerów i healthchecku brokera, obecność config.yml,
                      czy MEROSS_KEY jest ustawiony (bez pokazywania klucza) oraz
                      krótki zrzut tematów homie/# z brokera.
  discover [argumenty] Skan LAN-u i aktualizacja bridge/config/devices.json
                      (tools/discover.py, uruchamiany hostowym python3).
  login [argumenty]   Jednorazowe logowanie do chmury Meross po klucz
                      (tools/meross_login.py, interaktywnie w kontenerze mostu).
  test [unit|int|hw]  Testy w kontenerze (domyślnie unit):
                        unit — bez Dockera i sieci, markery domyślne z pytest.ini
                        int  — podnosi stack testowy i odpala pytest -m integration
                        hw   — pytest -m hardware, dotyka prawdziwych gniazdek
  render-config       Tylko generuje bridge/config/config.yml z szablonu i .env
                      (bez Dockera; przydatne do podglądu i używane przez testy).
  help                Ten tekst.

Przykłady:
  ./dot.sh up                 # cały stack
  ./dot.sh up mosquitto       # tylko broker
  ./dot.sh logs bridge        # logi mostu
  ./dot.sh test unit -k homie # pytest z dodatkowymi argumentami
EOF
}

# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

# Czyta .env linia po linii i eksportuje pary KLUCZ=WARTOŚĆ. Celowo bez `source`:
# .env nie ma być wykonywalnym skryptem, a klucz z chmury bywa dziwny.
load_env() {
    local line key value

    if [ -f "$ENV_FILE" ]; then
        while IFS= read -r line || [ -n "$line" ]; do
            line=${line%$'\r'}
            case $line in
                ''|'#'*) continue ;;
                *=*) ;;
                *) continue ;;
            esac
            key=${line%%=*}
            value=${line#*=}
            key=${key#export }
            key=$(printf '%s' "$key" | tr -d '[:space:]')
            case $key in
                ''|*[!A-Za-z0-9_]*) continue ;;
            esac
            case $value in
                \"*\") value=${value#\"}; value=${value%\"} ;;
                \'*\') value=${value#\'}; value=${value%\'} ;;
            esac
            export "$key=$value"
        done < "$ENV_FILE"
    fi

    # Wartości domyślne zgodne z CONTRACT.md §7 — skrypt ma działać także bez .env.
    export TZ=${TZ:-Europe/Warsaw}
    export MQTT_HOST=${MQTT_HOST:-mosquitto}
    export MQTT_PORT=${MQTT_PORT:-1883}
    export HOMIE_PREFIX=${HOMIE_PREFIX:-homie}
    export WEB_PORT=${WEB_PORT:-8080}
    export MEROSS_KEY=${MEROSS_KEY:-}
    export MEROSS_API_URL=${MEROSS_API_URL:-https://iotx-eu.meross.com}
    # Poniższych dwóch nie ma w .env.example (plik jest zamrożony w fazie 0) —
    # to strojenie mostu z wartościami z planu, nadpisywalne przez środowisko.
    export POLLING_INTERVAL=${POLLING_INTERVAL:-25}
    export BRIDGE_LOG_LEVEL=${BRIDGE_LOG_LEVEL:-INFO}
}

ensure_env_file() {
    if [ -f "$ENV_FILE" ]; then
        return 0
    fi
    [ -f "$ENV_EXAMPLE" ] || die "Nie ma ani $ENV_FILE, ani wzorca $ENV_EXAMPLE."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
    warn "Utworzyłem $ENV_FILE z $ENV_EXAMPLE."
    warn "Uzupełnij MEROSS_KEY (najprościej: ./dot.sh login) — bez klucza gniazdka"
    warn "odpowiadają mostowi błędem podpisu. Sprawdź też LAN_SUBNET i WEB_PORT."
}

# ---------------------------------------------------------------------------
# Generowanie bridge/config/config.yml
# ---------------------------------------------------------------------------

# Podstawianie robimy czystym bashem (${x//wzór/zamiana}), a nie sed-em ani envsubst:
# nie trzeba escape'ować klucza Meross, a na macOS envsubst i tak nie ma.
render_config() {
    [ -f "$TMPL_FILE" ] || die "Brak szablonu $TMPL_FILE."

    case $MQTT_PORT in
        ''|*[!0-9]*) die "MQTT_PORT musi być liczbą, a jest: '$MQTT_PORT'." ;;
    esac
    case $POLLING_INTERVAL in
        ''|*[!0-9]*) die "POLLING_INTERVAL musi być liczbą, a jest: '$POLLING_INTERVAL'." ;;
    esac

    local rendered
    rendered=$(cat "$TMPL_FILE")
    rendered=${rendered//\{\{MQTT_HOST\}\}/$MQTT_HOST}
    rendered=${rendered//\{\{MQTT_PORT\}\}/$MQTT_PORT}
    rendered=${rendered//\{\{HOMIE_PREFIX\}\}/$HOMIE_PREFIX}
    rendered=${rendered//\{\{MEROSS_KEY\}\}/$MEROSS_KEY}
    rendered=${rendered//\{\{POLLING_INTERVAL\}\}/$POLLING_INTERVAL}
    rendered=${rendered//\{\{LOG_LEVEL\}\}/$BRIDGE_LOG_LEVEL}

    # Wzorzec jest wąski (klamry + WIELKIE_LITERY), żeby nie potknąć się o zwykły tekst
    # w komentarzach szablonu.
    case $rendered in
        *'{{'[A-Z]*'}}'*) die "W szablonie został niepodstawiony znacznik — sprawdź $TMPL_FILE." ;;
    esac

    mkdir -p "$(dirname "$CONFIG_FILE")"

    # Nie nadpisujemy bez potrzeby (żeby nie ruszać mtime i nie mylić diffów), ale
    # skoro porównujemy całą treść, każda zmiana w .env albo w szablonie się przebije.
    if [ -f "$CONFIG_FILE" ] && [ "$rendered" = "$(cat "$CONFIG_FILE")" ]; then
        dim "    config.yml bez zmian: $CONFIG_FILE"
        return 0
    fi

    printf '%s\n' "$rendered" > "$CONFIG_FILE"
    info "Zapisałem $CONFIG_FILE (z $TMPL_FILE)."
    if [ -z "$MEROSS_KEY" ]; then
        warn "MEROSS_KEY jest puste — most wstanie, ale gniazdka odrzucą jego zapytania."
    fi
}

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

docker_running() { docker info >/dev/null 2>&1; }

require_docker_cli() {
    command -v docker >/dev/null 2>&1 || die "Nie znalazłem polecenia 'docker'. Zainstaluj Dockera."
}

# Startuje demona, jeśli nie odpowiada. Rozpoznanie systemu po uname.
ensure_docker() {
    require_docker_cli
    if docker_running; then
        return 0
    fi

    local system
    system=$(uname -s)
    case $system in
        Darwin)
            info "Demon Dockera nie odpowiada — uruchamiam Docker Desktop."
            open -a Docker || die "Nie udało się uruchomić Docker Desktop (open -a Docker)."
            ;;
        Linux)
            info "Demon Dockera nie odpowiada — uruchamiam usługę docker."
            if command -v systemctl >/dev/null 2>&1; then
                systemctl start docker 2>/dev/null \
                    || sudo systemctl start docker \
                    || die "Nie udało się wystartować usługi docker."
            else
                die "Brak systemctl — uruchom demona Dockera ręcznie."
            fi
            ;;
        *)
            die "Nieznany system '$system' — uruchom demona Dockera ręcznie."
            ;;
    esac

    local waited=0
    printf '    czekam na demona Dockera'
    while [ "$waited" -lt 180 ]; do
        if docker_running; then
            printf '\n'
            info "Docker gotowy."
            return 0
        fi
        printf '.'
        sleep 2
        waited=$((waited + 2))
    done
    printf '\n'
    die "Demon Dockera nie wstał w ciągu 3 minut."
}

require_running_docker() {
    require_docker_cli
    docker_running || die "Demon Dockera nie działa. Uruchom go albo odpal './dot.sh up'."
}

dc()  { docker compose --project-directory "$ROOT_DIR" -f "$COMPOSE_FILE" "$@"; }
dct() { docker compose --project-directory "$ROOT_DIR" -f "$TEST_COMPOSE_FILE" "$@"; }

# ---------------------------------------------------------------------------
# Most: klon repo
# ---------------------------------------------------------------------------

# meross2mqtt/ jest w .gitignore, więc po świeżym `git clone` (np. na Raspberry Pi)
# katalogu nie będzie, a `docker compose build bridge` padnie. Istniejącego klonu
# NIE ruszamy — żadnego git pull, użytkownik może mieć tam własne zmiany.
ensure_bridge_repo() {
    if [ -d "$BRIDGE_DIR" ]; then
        return 0
    fi
    command -v git >/dev/null 2>&1 || die "Brak katalogu $BRIDGE_DIR i brak gita, żeby go doklonować."
    info "Nie ma katalogu mostu — klonuję $BRIDGE_REPO"
    git clone "$BRIDGE_REPO" "$BRIDGE_DIR" || die "Klonowanie mostu nie udało się."
}

ensure_dirs() {
    mkdir -p "$ROOT_DIR/mosquitto/data" "$ROOT_DIR/mosquitto/log" \
             "$ROOT_DIR/bridge/config" "$ROOT_DIR/data"
}

# ---------------------------------------------------------------------------
# Komendy
# ---------------------------------------------------------------------------

cmd_up() {
    ensure_env_file
    load_env
    ensure_bridge_repo
    ensure_dirs
    [ -f "$MOSQUITTO_CONF" ] || warn "Brak $MOSQUITTO_CONF — broker wstanie na domyślnym configu."
    render_config
    ensure_docker

    info "Podnoszę stack."
    dc up -d --remove-orphans "$@"

    info "Gotowe. Panel: http://localhost:${WEB_PORT}"
    dim "    stan:  ./dot.sh status"
    dim "    logi:  ./dot.sh logs"
}

cmd_down() {
    require_docker_cli
    load_env
    if ! docker_running; then
        info "Demon Dockera nie działa — nie ma czego zatrzymywać."
        return 0
    fi
    info "Zatrzymuję stack."
    dc down "$@"
}

cmd_restart() {
    cmd_down
    cmd_up "$@"
}

cmd_logs() {
    require_running_docker
    load_env
    dc logs -f --tail 200 "$@"
}

cmd_status() {
    load_env

    info "Pliki i konfiguracja"
    if [ -f "$ENV_FILE" ]; then
        dim "    .env: jest ($ENV_FILE)"
    else
        warn "    .env: BRAK — './dot.sh up' utworzy go z .env.example"
    fi
    if [ -f "$CONFIG_FILE" ]; then
        dim "    bridge/config/config.yml: jest"
    else
        warn "    bridge/config/config.yml: BRAK — wygeneruj przez './dot.sh render-config'"
    fi
    if [ -n "$MEROSS_KEY" ]; then
        # Klucza nie pokazujemy, tylko potwierdzamy, że coś tam jest.
        dim "    MEROSS_KEY: ustawiony (${#MEROSS_KEY} znaków)"
    else
        warn "    MEROSS_KEY: PUSTY — './dot.sh login' pobierze klucz z chmury"
    fi
    if [ -d "$BRIDGE_DIR" ]; then
        dim "    klon mostu: jest ($BRIDGE_DIR)"
    else
        warn "    klon mostu: BRAK — './dot.sh up' doklonuje"
    fi

    require_docker_cli
    if ! docker_running; then
        warn "Demon Dockera nie działa — pomijam stan kontenerów i zrzut tematów."
        return 0
    fi

    echo
    info "Kontenery"
    dc ps

    echo
    info "Healthcheck brokera"
    local health
    health=$(docker inspect -f \
        '{{if .State.Health}}{{.State.Health.Status}}{{else}}bez healthchecku{{end}}' \
        "$MOSQUITTO_CONTAINER" 2>/dev/null || true)
    if [ -z "$health" ]; then
        warn "    kontener $MOSQUITTO_CONTAINER nie istnieje"
    else
        dim "    $MOSQUITTO_CONTAINER: $health"
    fi

    echo
    info "Zrzut tematów ${HOMIE_PREFIX}/# (retained, twardy limit czasu)"
    local dump
    # -W 3 to limit samego mosquitto_sub, `timeout 8` to bezpiecznik na wypadek,
    # gdyby klient zawisł na połączeniu; || true, bo brak wiadomości to nie błąd.
    dump=$(dc exec -T mosquitto timeout 8 \
        mosquitto_sub -h 127.0.0.1 -p "$MQTT_PORT" -t "${HOMIE_PREFIX}/#" -v -W 3 \
        2>/dev/null || true)
    if [ -z "$dump" ]; then
        dim "    (nic nie przyszło — most jeszcze nie opublikował stanu albo broker nie działa)"
    else
        printf '%s\n' "$dump" | head -n 25
        local lines
        lines=$(printf '%s\n' "$dump" | wc -l | tr -d ' ')
        if [ "$lines" -gt 25 ]; then
            dim "    ... (razem $lines tematów)"
        fi
    fi
}

cmd_discover() {
    load_env
    local script="$ROOT_DIR/tools/discover.py"
    if [ ! -f "$script" ]; then
        err "Nie ma $script."
        err "Ten skrypt dostarcza osobny strumień pracy — jeśli właśnie klonowałeś repo,"
        err "zaktualizuj je; jeśli pracujesz nad stackiem, poczekaj na tools/discover.py."
        return 1
    fi
    command -v python3 >/dev/null 2>&1 || die "Brak python3 na hoście."
    # Celowo na hoście, nie w kontenerze: skan opiera się na tablicy ARP hosta,
    # której kontener nie widzi, a skrypt nie ma zależności poza biblioteką standardową.
    info "Skanuję LAN (${LAN_SUBNET:-nieustawione}) hostowym python3."
    python3 "$script" "$@"
}

cmd_login() {
    load_env
    local script="$ROOT_DIR/tools/meross_login.py"
    if [ ! -f "$script" ]; then
        err "Nie ma $script."
        err "Ten skrypt dostarcza osobny strumień pracy — jeśli właśnie klonowałeś repo,"
        err "zaktualizuj je; jeśli pracujesz nad stackiem, poczekaj na tools/meross_login.py."
        return 1
    fi
    ensure_bridge_repo
    ensure_docker

    info "Logowanie do chmury Meross w kontenerze mostu (tam jest meross_iot)."
    dim "    hasło nie jest nigdzie zapisywane — skrypt bierze tylko klucz"

    # Kontener mostu chodzi jako root, a skrypt pisze do zamontowanego repozytorium:
    # .env oraz bridge/config/cloud-devices.json. Na Linuksie (i na Raspberry Pi) oba
    # pliki stałyby się własnością roota i późniejsze `./dot.sh up` nie mogłoby już
    # nadpisać .env ani configu. Docker Desktop na macOS sam mapuje właściciela na
    # użytkownika hosta, więc tam --user jest zbędne (i tylko psuje HOME w kontenerze).
    local run_as=""
    if [ "$(uname -s)" = "Linux" ]; then
        run_as="--user $(id -u):$(id -g)"
        dim "    uruchamiam jako $(id -u):$(id -g), żeby .env nie stał się własnością roota"
    fi

    # --no-deps, żeby nie ciągnąć brokera; --entrypoint python3, bo obraz startuje
    # domyślnie samego mostu. Repo montujemy w /project, żeby skrypt widział .env
    # i bridge/config/ tak samo jak na hoście. `docker compose run` jest interaktywne
    # i samo przydziela TTY (odpowiednik `-it` z docker run); -T by to wyłączyło.
    # shellcheck disable=SC2086  # run_as ma się rozdzielić na dwa słowa albo zniknąć
    dc run --rm --no-deps --interactive $run_as \
        --entrypoint python3 \
        -v "$ROOT_DIR:/project" \
        -w /project \
        -e "MEROSS_API_URL=$MEROSS_API_URL" \
        bridge tools/meross_login.py "$@"

    # Kopiuj cloud-devices.json do data/ — webapp (przez /data) odczyta nazwy z chmury
    if [ -f "$ROOT_DIR/bridge/config/cloud-devices.json" ]; then
        cp "$ROOT_DIR/bridge/config/cloud-devices.json" "$ROOT_DIR/data/cloud-devices.json"
        info "Skopiowano cloud-devices.json do data/ dla webappa."
    fi
}

cmd_test() {
    local kind=unit
    if [ $# -gt 0 ]; then
        case $1 in
            unit|int|hw) kind=$1; shift ;;
            -*) ;;
            *) die "Nieznany rodzaj testów: '$1'. Dozwolone: unit, int, hw." ;;
        esac
    fi

    # Testy celują w Pythona 3.12, a hostowy na Macu to 3.9 — dlatego zawsze kontener.
    if [ ! -f "$TEST_COMPOSE_FILE" ]; then
        err "Nie ma $TEST_COMPOSE_FILE, a testy odpalamy w kontenerze (usługa 'tests')."
        err "Ten plik dostarcza osobny strumień pracy — bez niego nie mam gdzie odpalić pytesta."
        return 1
    fi
    load_env
    ensure_docker

    case $kind in
        unit)
            info "Testy jednostkowe (markery domyślne z pytest.ini)."
            dct run --rm tests pytest "$@"
            ;;
        int)
            info "Podnoszę stack testowy (broker, atrapy gniazdek, prawdziwy most)."
            # --build, bo atrapy budują się z tests/fakes i muszą łapać zmiany w kodzie.
            # --wait czeka na healthchecki, żeby pytest nie startował do pustego brokera;
            # usługa `tests` ma replicas: 0, więc `up` jej nie odpala.
            local up_args
            up_args="-d --build"
            if dct up --help 2>/dev/null | grep -q -- '--wait'; then
                up_args="-d --build --wait"
            fi
            # shellcheck disable=SC2086  # up_args ma się rozdzielić na osobne słowa
            dct up $up_args
            info "Testy integracyjne."
            dct run --rm tests pytest -m integration "$@"
            ;;
        hw)
            warn "Testy sprzętowe dotykają prawdziwych gniazdek w LAN-ie."
            if [ -z "$MEROSS_KEY" ]; then
                warn "MEROSS_KEY jest pusty — przejdzie tylko test tożsamości, reszta się pominie."
                warn "Klucz pobierzesz przez './dot.sh login'."
            fi
            if [ "${HW_ALLOW_SWITCHING:-}" = "1" ]; then
                warn "HW_ALLOW_SWITCHING=1 — test przełączający JEST włączony dla"
                warn "HW_TARGET_UUID=${HW_TARGET_UUID:-(nieustawione)} kanał ${HW_TARGET_CHANNEL:-0}."
            else
                dim "    test przełączający pominięty (bez HW_ALLOW_SWITCHING=1 nic nie kliknie)"
            fi
            # -s, bo te testy nie tylko sprawdzają, ale i RAPORTUJĄ (model gniazdka, liczba
            # kanałów, zdolności kontra atrapa, odczyt mocy) — bez tego pytest zjada wydruki
            # przy zielonym przebiegu. -r a pokazuje powody pominięć.
            dct run --rm tests pytest -m hardware -s -r a "$@"
            ;;
    esac
}

cmd_render_config() {
    if [ ! -f "$ENV_FILE" ]; then
        warn "Nie ma $ENV_FILE — generuję na wartościach domyślnych."
    fi
    load_env
    render_config
}

# ---------------------------------------------------------------------------

main() {
    local cmd=help
    if [ $# -gt 0 ]; then
        cmd=$1
        shift
    fi

    case $cmd in
        up)            cmd_up "$@" ;;
        down)          cmd_down "$@" ;;
        restart)       cmd_restart "$@" ;;
        logs)          cmd_logs "$@" ;;
        status)        cmd_status "$@" ;;
        discover)      cmd_discover "$@" ;;
        login)         cmd_login "$@" ;;
        test)          cmd_test "$@" ;;
        render-config) cmd_render_config "$@" ;;
        help|-h|--help) usage ;;
        *)
            err "Nieznana komenda: '$cmd'"
            echo >&2
            usage >&2
            exit 2
            ;;
    esac
}

main "$@"
