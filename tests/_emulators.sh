#!/usr/bin/env bash
# Общая часть run_tests.sh / run_screens.sh: конфиг эмуляторов Firebase на портах от PORT_BASE.
#   source tests/_emulators.sh PORT_BASE
# После source доступны: ROOT, PY, FB, WORK, PROJECT, PORT_* и функция emu_exec "<команда>".
# Порты: hub=B, logging=B+1, firestore=B+2 (websocket B+3), auth=B+4, storage=B+5, flask=B+6.
# Проект эмуляторов — demo-caspian-<B>, временные файлы CLI — в своём каталоге: параллельные прогоны
# на разных PORT_BASE не пересекаются.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Локальные пути к python/firebase (не в git): tests/.env.local
if [ -f "$ROOT/tests/.env.local" ]; then . "$ROOT/tests/.env.local"; fi

PORT_BASE="${1:-18000}"
case "$PORT_BASE" in ''|*[!0-9]*) echo "PORT_BASE должен быть числом: $PORT_BASE" >&2; exit 2 ;; esac
PY="${CMS_PYTHON:-python3}"
FB="${FIREBASE_BIN:-firebase}"
# Свой demo-проект на каждый PORT_BASE: CLI ведёт реестр хаба по id проекта (os.tmpdir()/hub-<проект>.json),
# и параллельные прогоны с общим id делили один файл реестра («multiple instances…»; первый вышедший его удалял).
# Префикс demo- обязателен: эмулятор не ходит в настоящий Firebase.
PROJECT="demo-caspian-$PORT_BASE"

PORT_HUB=$PORT_BASE
PORT_LOGGING=$((PORT_BASE + 1))
PORT_FIRESTORE=$((PORT_BASE + 2))
PORT_FIRESTORE_WS=$((PORT_BASE + 3))
PORT_AUTH=$((PORT_BASE + 4))
PORT_STORAGE=$((PORT_BASE + 5))
PORT_FLASK=$((PORT_BASE + 6))

# Осиротевший эмулятор Firestore с прошлого аварийного запуска держит наш порт — гасим
# (только процессы на портах ЭТОГО PORT_BASE, чужие диапазоны не трогаем).
kill_stale() {
  pkill -f "cloud-firestore-emulator.*--port $PORT_FIRESTORE( |$)" 2>/dev/null || true
}
kill_stale

WORK="$(mktemp -d "${TMPDIR:-/tmp}/cms-emu-${PORT_BASE}-XXXXXX")"
cleanup_work() { kill_stale; rm -rf "$WORK"; }
trap cleanup_work EXIT

cat > "$WORK/firebase.json" <<JSON
{
  "firestore": { "rules": "firestore.rules" },
  "storage": { "rules": "storage.rules" },
  "emulators": {
    "hub": { "host": "127.0.0.1", "port": $PORT_HUB },
    "logging": { "host": "127.0.0.1", "port": $PORT_LOGGING },
    "firestore": { "host": "127.0.0.1", "port": $PORT_FIRESTORE, "websocketPort": $PORT_FIRESTORE_WS },
    "auth": { "host": "127.0.0.1", "port": $PORT_AUTH },
    "storage": { "host": "127.0.0.1", "port": $PORT_STORAGE },
    "ui": { "enabled": false },
    "singleProjectMode": true
  }
}
JSON
# Правила эмулятора разрешают всё: CMS ходит через Admin SDK, тестируем код, а не правила.
cat > "$WORK/firestore.rules" <<'RULES'
rules_version = '2';
service cloud.firestore { match /databases/{database}/documents { match /{document=**} { allow read, write: if true; } } }
RULES
cat > "$WORK/storage.rules" <<'RULES'
rules_version = '2';
service firebase.storage { match /b/{bucket}/o { match /{allPaths=**} { allow read, write: if true; } } }
RULES

# Окружение для процессов внутри emulators:exec (дублируем то, что ставит CLI, — с явными портами).
emu_env() {
  cat <<ENV
export FIRESTORE_EMULATOR_HOST=127.0.0.1:$PORT_FIRESTORE
export FIREBASE_AUTH_EMULATOR_HOST=127.0.0.1:$PORT_AUTH
export FIREBASE_STORAGE_EMULATOR_HOST=127.0.0.1:$PORT_STORAGE
export STORAGE_EMULATOR_HOST=http://127.0.0.1:$PORT_STORAGE
export GCLOUD_PROJECT=$PROJECT
export CMS_PROJECT_ID=$PROJECT
export CMS_STORAGE_BUCKET=$PROJECT.appspot.com
export FIREBASE_WEB_API_KEY=fake-api-key
export CMS_SECRET_KEY=cms-test-secret-key-0123456789abcdef0123456789abcdef
export CMS_INSTANCE_DIR=$WORK/instance
export CMS_STRICT=1
export CMS_PORT_FLASK=$PORT_FLASK
export PYTHONDONTWRITEBYTECODE=1
export NO_PROXY="127.0.0.1,localhost,\${NO_PROXY:-}"
export no_proxy="127.0.0.1,localhost,\${no_proxy:-}"
cd "$ROOT"
ENV
}

# emu_exec "<bash-скрипт>" — поднимает эмуляторы, выполняет скрипт, гасит эмуляторы.
emu_exec() {
  { echo 'set -euo pipefail'; emu_env; echo "$1"; } > "$WORK/inner.sh"
  # JAVA_TOOL_OPTIONS (прокси/truststore песочницы) эмуляторам не нужен, а его «Picked up …»
  # в stderr рантайм правил Storage принимает за ошибку — убираем для процесса CLI.
  # TMPDIR — свой на прогон: эмулятор Storage держит файлы в os.tmpdir()/firebase/storage/blobs и при
  # остановке чистит каталог, а соседний прогон падал на ENOENT («An unexpected error has occurred»).
  mkdir -p "$WORK/tmp"
  (cd "$WORK" && env -u JAVA_TOOL_OPTIONS TMPDIR="$WORK/tmp" "$FB" emulators:exec --only firestore,auth,storage --project "$PROJECT" \
      --config "$WORK/firebase.json" "bash $WORK/inner.sh")
}
