#!/usr/bin/env bash
# Скриншоты страниц CMS (Playwright) на эмуляторах с сидом.
#
#   tests/run_screens.sh PORT_BASE OUT_DIR /path1 /path2 …
#   tests/run_screens.sh 18000 /tmp/shots/foundation / /schedule/rooms '!/login' '!/join/STAND1'
#
# Путь с префиксом «!» снимается БЕЗ входа (публичные страницы, логин).
# Действия перед снимком (тогда снимается только экран, не вся страница):
#   "/ui-kit|name:kit_confirm|click:form[data-confirm] button"  — шаги click:/type:/key:/wait:/eval:/hover:/files:
# Для каждого пути — два PNG: <slug>_1440.png и <slug>_390.png (полная страница).
# Тёмная тема: SCREENS_THEME=dark. Chromium: CMS_CHROMIUM (иначе из Playwright).
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/_emulators.sh" "${1:?PORT_BASE}"
OUT="${2:?OUT_DIR}"
shift 2
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done

emu_exec "
\"$PY\" -m tests.seed
\"$PY\" -c 'from flask_app import app; app.run(host=\"127.0.0.1\", port=$PORT_FLASK, debug=False, use_reloader=False)' > \"$WORK/flask.log\" 2>&1 &
FLASK_PID=\$!
trap 'kill \$FLASK_PID 2>/dev/null || true' EXIT
for i in \$(seq 1 60); do
  if \"$PY\" -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex((\"127.0.0.1\", $PORT_FLASK))==0 else 1)'; then break; fi
  sleep 0.5
done
\"$PY\" tests/screens.py --base http://127.0.0.1:$PORT_FLASK --out \"$OUT\" $ARGS
echo '--- flask.log (хвост) ---'; tail -n 20 \"$WORK/flask.log\" || true
"
