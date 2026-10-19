#!/usr/bin/env bash
# Тесты CMS на эмуляторах Firebase (Firestore + Auth + Storage, проект demo-caspian).
#
#   tests/run_tests.sh [PORT_BASE] [модули unittest…]
#   tests/run_tests.sh 18000                          # все тесты (discover)
#   tests/run_tests.sh 18400 tests.test_applicants    # один модуль
#   tests/run_tests.sh 18400 tests.test_smoke.SmokeTest.test_get_routes
#
# Пути к python/firebase — переменные CMS_PYTHON / FIREBASE_BIN (или файл tests/.env.local).
# Прод-ключ НЕ используется: в режиме эмулятора cms.core работает с анонимными кредами.
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/_emulators.sh" "${1:-18000}"
shift || true

if [ "$#" -eq 0 ]; then
  CMD="\"$PY\" -m unittest discover -s tests -t . -v"
else
  CMD="\"$PY\" -m unittest -v"
  for a in "$@"; do CMD="$CMD $(printf '%q' "$a")"; done
fi
echo ">> Эмуляторы на портах $PORT_BASE..$((PORT_BASE + 5)), $("$PY" -c "import sys; print(\"Python\", sys.version.split()[0])"), запуск: $CMD"
emu_exec "$CMD"
