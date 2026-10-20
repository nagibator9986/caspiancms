#!/usr/bin/env bash
# Печать страниц CMS в PDF (A4, как «Печать → Сохранить как PDF» в Chrome) на эмуляторах с сидом
# + PNG каждой страницы — проверить бланки глазами (обрезка, лишние листы, мелкий/бледный текст).
#
#   tests/run_print.sh PORT_BASE OUT_DIR /path1 /path2 …
#   tests/run_print.sh 18100 /tmp/print '/schedule/print?group=ПО 24-1' '/schedule/print/teacher?teacher=t_li'
#   PRINT_DEMO=1 tests/run_print.sh 18100 /tmp/print '/schedule/print?group=ПЛОТ 25-1' '/schedule/print?group=ТБ 25-1'
#       — плюс демо-группы: «ПЛОТ 25-1» (48 пар, предел одного листа) и «ТБ 25-1» (как бумажный бланк: длинные названия)
#
# Вход — админом через форму. Для каждого пути: <slug>.pdf и <slug>_p<N>.png; в выводе — число страниц,
# размер листа и предупреждение, если что-то шире листа A4. Растеризация — pymupdf (только в venv
# инструментов: "$CMS_PYTHON" -m pip install pymupdf). Chromium: CMS_CHROMIUM (иначе из Playwright).
set -euo pipefail
. "$(cd "$(dirname "$0")" && pwd)/_emulators.sh" "${1:?PORT_BASE}"
OUT="${2:?OUT_DIR}"
shift 2
[ "$#" -gt 0 ] || { echo "Нужен хотя бы один путь страницы" >&2; exit 2; }
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
"$PY" -c 'import pymupdf' 2>/dev/null || "$PY" -c 'import fitz' 2>/dev/null || {
  echo "Нет модуля pymupdf для растеризации PDF. Поставьте в venv инструментов (не в requirements.txt):" >&2
  echo "  \"$PY\" -m pip install pymupdf" >&2
  exit 3
}
ARGS=""
for a in "$@"; do ARGS="$ARGS $(printf '%q' "$a")"; done
DEMO=""
if [ "${PRINT_DEMO:-}" = "1" ]; then DEMO="\"$PY\" tests/print_pdf.py --seed-demo"; fi

emu_exec "
\"$PY\" -m tests.seed
$DEMO
\"$PY\" -c 'from flask_app import app; app.run(host=\"127.0.0.1\", port=$PORT_FLASK, debug=False, use_reloader=False)' > \"$WORK/flask.log\" 2>&1 &
FLASK_PID=\$!
trap 'kill \$FLASK_PID 2>/dev/null || true' EXIT
for i in \$(seq 1 60); do
  if \"$PY\" -c 'import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex((\"127.0.0.1\", $PORT_FLASK))==0 else 1)'; then break; fi
  sleep 0.5
done
\"$PY\" tests/print_pdf.py --base http://127.0.0.1:$PORT_FLASK --out \"$OUT\" $ARGS
echo '--- flask.log (хвост, без статики) ---'; grep -v '\"GET /static/' \"$WORK/flask.log\" | tail -n 15 || true
"
