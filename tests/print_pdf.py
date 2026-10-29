# tests/print_pdf.py — печать страниц CMS в PDF (Chromium, как «Печать → Сохранить как PDF»)
# и растеризация каждой страницы в PNG (вызывается из tests/run_print.sh).
#
#   python tests/print_pdf.py --base http://127.0.0.1:18106 --out OUT '/schedule/print?group=ПО 24-1'
#   python tests/print_pdf.py --seed-demo         # добавить в эмулятор демо-группы для бланков (до запуска Flask)
#
# Для каждого пути: <slug>.pdf (A4, фон печатается, размер листа — из CSS @page) и <slug>_p<N>.png
# (каждая страница, 110 dpi). В выводе — число страниц, размер листа в мм, JS-ошибки и предупреждение,
# если содержимое шире печатной области (то, что принтер обрежет). PNG смотрите глазами (Read).
# Растеризация — pymupdf: ставится только в scratch-venv инструментов (`pip install pymupdf`),
# в requirements.txt его нет.

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

DPI = 110
DENSE_GROUP = 'ПЛОТ 25-1'   # 48 пар (Пн–Сб по 6, часть — числитель/знаменатель): предел «группа на одном листе»
REAL_GROUP = 'ТБ 25-1'      # как на фото бумажного бланка: 21 пара, длинные названия дисциплин и ФИО
# (день 0..5, время, дисциплина, преподаватель (ФИО текстом, вымышленные), аудитория, неделя)
REAL_LESSONS = [
    (0, '10.25-11.15', 'Практика турецкого языка (факультатив)', 'Озтюрк Кемаль', '610', 'always'),
    (0, '11.35-11.55', 'Кураторский час', 'Жумабекова Айгерим Нурлановна', '623', 'always'),
    (0, '12.00-12.50', 'Межкультурная коммуникация и корпоративная культура', 'Тлеубаева Сауле Маратовна', '605', 'always'),
    (0, '12.55-13.45', 'Информационные технологии в гостиничной деятельности', 'Тлеубаева Сауле Маратовна', '605', 'always'),
    (0, '14.00-14.50', 'Основы права', 'Абдрахманов Ерлан Серикович', '605', 'always'),
    (1, '14.05-14.55', 'Основы гостиничного бизнеса', 'Кенжебаев Нурсултан Ерланулы', '008', 'always'),
    (1, '15.00-15.50', 'Культурология', 'Сагинтаева Динара Болатовна', '008', 'always'),
    (1, '15.55-16.45', 'Основы политологии и социологии', 'Байжанов Асет Муратулы', '008', 'always'),
    (2, '13.00-13.50', 'Основы гостиничного бизнеса', 'Кенжебаев Нурсултан Ерланулы', '608', 'numerator'),
    (2, '14.05-14.55', 'Основы гостиничного бизнеса', 'Кенжебаев Нурсултан Ерланулы', '605', 'always'),
    (2, '15.00-15.50', 'Материально-техническая база и оформление гостиничных комплексов', 'Кенжебаев Нурсултан Ерланулы', '608', 'always'),
    (3, '12.05-12.55', 'Организация службы приема и размещения в гостинице', 'Тлеубаева Сауле Маратовна', '613', 'denominator'),
    (3, '13.00-13.50', 'Стандартизация и контроль качества гостиничных услуг', 'Тлеубаева Сауле Маратовна', '602', 'always'),
    (3, '14.05-14.55', 'Организация службы приема и размещения в гостинице', 'Тлеубаева Сауле Маратовна', '613', 'always'),
    (3, '15.00-15.50', 'Практика английского языка (факультатив)', 'Ms. Johnson', '605', 'always'),
    (4, '12.05-12.55', 'Межкультурная коммуникация и корпоративная культура', 'Тлеубаева Сауле Маратовна', '623', 'always'),
    (4, '13.00-13.50', 'Организация службы приема и размещения в гостинице', 'Тлеубаева Сауле Маратовна', '623', 'always'),
    (4, '14.05-14.55', 'Основы экономики', 'Оспанова Гульмира Ертаевна', '623', 'always'),
    (4, '15.00-15.50', 'Основы философии', 'Сагинтаева Динара Болатовна', '609', 'always'),
    (5, '12.05-12.55', 'Физическая культура', 'Ступина Ольга Петровна', 'Спортзал', 'always'),
    (5, '13.00-13.50', 'Физическая культура', 'Ступина Ольга Петровна', 'Спортзал', 'always'),
]

# Ширина листа A4 в CSS-пикселях (96 dpi): 210 мм. Печатная область — лист минус поля @page страницы.
A4_WIDTH_PX = round(210 / 25.4 * 96)
CHROME_DEFAULT_MARGIN = '0.4in'   # поля Chrome «по умолчанию», если страница не задала @page

# Поля @page, которые действуют на странице (именованная страница — по свойству page первого элемента с ним).
_PAGE_MARGINS_JS = """(fallback) => {
  let name = '';
  for (const el of document.querySelectorAll('body *')) {
    const p = getComputedStyle(el).page;
    if (p && p !== 'auto') { name = p; break; }
  }
  let left = null, right = null;
  for (const sh of document.styleSheets) {
    let rules; try { rules = sh.cssRules; } catch (e) { continue; }
    for (const r of rules) {
      if (r.type !== CSSRule.PAGE_RULE) continue;
      const sel = (r.selectorText || '').trim();
      if (sel && sel !== name) continue;
      if (sel === '' && name && left !== null) continue;  // именованная страница важнее общей
      if (r.style.marginLeft) left = r.style.marginLeft;
      if (r.style.marginRight) right = r.style.marginRight;
      if (r.style.margin && !r.style.marginLeft) left = right = r.style.margin.split(' ')[0];
    }
  }
  const px = v => { const d = document.createElement('div'); d.style.width = v; document.body.appendChild(d);
                    const w = d.getBoundingClientRect().width; d.remove(); return w; };
  return {name, left: px(left || fallback), right: px(right || fallback)};
}"""

_OVERFLOW_JS = """() => {
  const w = document.documentElement.clientWidth;
  const out = [];
  for (const el of document.querySelectorAll('body *')) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || cs.position === 'fixed') continue;
    const r = el.getBoundingClientRect();
    if (!r.width || !r.height) continue;
    if (r.right > w + 1 || r.left < -1) {
      out.push((el.className && el.className.baseVal === undefined ? '.' + String(el.className).trim().split(/\\s+/).join('.') : el.tagName.toLowerCase())
               + ' [' + Math.round(r.left) + '..' + Math.round(r.right) + ' из ' + w + ']');
      if (out.length >= 5) break;
    }
  }
  return out;
}"""


# Бланки расписания (.sch-sheet): сколько строк, класс плотности, сколько мм занято из 273 (печатная область листа
# внутри полей) и сработала ли страховка schedule.js (zoom < 1 — сервер недооценил высоту).
_SHEETS_JS = """() => Array.from(document.querySelectorAll('.sch-sheet')).map(s => {
  const page = s.querySelector('.sch-sheet__page'), probe = s.querySelector('.sch-sheet__probe');
  if (!page || !probe) return null;
  const pxmm = probe.getBoundingClientRect().height / 273;
  const tr = Array.from(s.querySelectorAll('tbody tr'));
  const rowsH = tr.reduce((a, r) => a + r.getBoundingClientRect().height, 0);
  return {name: s.getAttribute('aria-label') || '', density: ((s.className.match(/sch-sheet--(\\w+)/) || [])[1] || ''),
          rows: tr.length, used: Math.round(page.getBoundingClientRect().height / pxmm),
          row: tr.length ? Math.round(rowsH / tr.length / pxmm * 10) / 10 : 0, zoom: page.style.zoom || '1'};
}).filter(Boolean)"""


def _need_pymupdf():
    try:
        import pymupdf  # noqa: F401
    except ImportError:
        try:
            import fitz  # noqa: F401  (старое имя модуля pymupdf)
        except ImportError:
            raise SystemExit(
                'Нет модуля pymupdf для растеризации PDF.\n'
                '  Поставьте его в venv инструментов (НЕ в requirements.txt):\n'
                '  "$CMS_PYTHON" -m pip install pymupdf')


def _open_pdf(path):
    try:
        import pymupdf
    except ImportError:
        import fitz as pymupdf
    return pymupdf.open(path)


def rasterize(pdf_path, out_dir, slug):
    """PDF → <slug>_p<N>.png; → [(файл, ширина мм, высота мм)]."""
    doc = _open_pdf(pdf_path)
    pages = []
    try:
        for i, page in enumerate(doc, start=1):
            name = '%s_p%d.png' % (slug, i)
            page.get_pixmap(dpi=DPI).save(os.path.join(out_dir, name))
            r = page.rect
            pages.append((name, round(r.width / 72 * 25.4), round(r.height / 72 * 25.4)))
    finally:
        doc.close()
    return pages


def seed_demo():
    """Демо-группы для бланков: «ПЛОТ 25-1» — 48 строк (Пн–Сб по 6 пар, две пары в слоте — числитель и
    знаменатель), «ТБ 25-1» — как бумажный бланк с фото (21 пара, длинные названия). Только эмулятор."""
    if not os.environ.get('FIRESTORE_EMULATOR_HOST'):
        raise SystemExit('--seed-demo работает только с эмулятором (FIRESTORE_EMULATOR_HOST)')
    from cms import core
    from tests.seed import TEACHERS, _lesson
    teachers = list(TEACHERS)
    monday = ['08.25-09.15', '09.20-10.10', '10.25-11.15', '11.35-12.25', '12.55-13.45', '14.00-14.50']
    other = ['08.00-08.50', '08.55-09.45', '10.00-10.50', '10.55-11.45', '12.05-12.55', '13.00-13.50']
    b = core.db.batch()
    n = 0
    for wd in range(6):
        for si, time in enumerate(monday if wd == 0 else other):
            uid = teachers[(wd * 3 + si) % len(teachers)]
            name, subject, room = TEACHERS[uid]
            if si in (2, 4):  # пара Ч/З в одном слоте
                uid2 = teachers[(wd * 3 + si + 5) % len(teachers)]
                _n2, subject2, room2 = TEACHERS[uid2]
                b.set(core.db.collection('schedules').document('dense_%d_%d_n' % (wd, si)),
                      _lesson(DENSE_GROUP, wd, time, uid, subject, room, 'numerator'))
                b.set(core.db.collection('schedules').document('dense_%d_%d_d' % (wd, si)),
                      _lesson(DENSE_GROUP, wd, time, uid2, subject2, room2, 'denominator'))
                n += 2
            else:
                b.set(core.db.collection('schedules').document('dense_%d_%d' % (wd, si)),
                      _lesson(DENSE_GROUP, wd, time, uid, subject, room))
                n += 1
    for i, (wd, time, subject, teacher, room, week) in enumerate(REAL_LESSONS):
        b.set(core.db.collection('schedules').document('demo_real_%02d' % i),
              _lesson(REAL_GROUP, wd, time, teacher, subject, room, week))
    b.commit()
    print('Демо-группы: «%s» — %d пар, «%s» — %d пар' % (DENSE_GROUP, n, REAL_GROUP, len(REAL_LESSONS)))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base')
    ap.add_argument('--out')
    ap.add_argument('--seed-demo', action='store_true')
    ap.add_argument('paths', nargs='*')
    args = ap.parse_args()
    if args.seed_demo:
        seed_demo()
        return
    if not (args.base and args.out and args.paths):
        ap.error('нужны --base, --out и хотя бы один путь')
    _need_pymupdf()
    from playwright.sync_api import sync_playwright
    from tests.screens import CdnProxy, login, slugify

    os.makedirs(args.out, exist_ok=True)
    cdn = CdnProxy(os.path.join(args.out, '.cdn-cache'))
    launch = {}
    if os.environ.get('CMS_CHROMIUM'):
        launch['executable_path'] = os.environ['CMS_CHROMIUM']
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        ctx = browser.new_context(viewport={'width': 1280, 'height': 900}, device_scale_factor=1,
                                  locale='ru-RU', timezone_id='Asia/Almaty')
        ctx.route('**/*', cdn.handle)
        ctx.add_init_script("try{localStorage.setItem('cc-theme','light')}catch(e){}")
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        page.on('console', lambda m: errors.append('console: ' + m.text) if m.type == 'error' else None)
        login(page, args.base)
        for spec in args.paths:
            anonymous = spec.startswith('!')
            path = spec.lstrip('!')
            if anonymous:
                raise SystemExit('Печать без входа не поддерживается: %s' % spec)
            errors.clear()
            resp = page.goto(args.base + path, wait_until='networkidle')
            page.wait_for_timeout(300)  # шрифты
            slug = slugify(path)
            pdf_path = os.path.join(args.out, slug + '.pdf')
            sheets = page.evaluate(_SHEETS_JS)
            # Проверка ширины: вёрстка печати при ширине печатной области (лист A4 минус поля @page) —
            # ничего не должно вылезать за край (иначе принтер/Chrome обрежет или ужмёт страницу).
            page.emulate_media(media='print')
            page.set_viewport_size({'width': A4_WIDTH_PX, 'height': 1123})
            margins = page.evaluate(_PAGE_MARGINS_JS, CHROME_DEFAULT_MARGIN)
            area = max(200, round(A4_WIDTH_PX - margins['left'] - margins['right']))
            page.set_viewport_size({'width': area, 'height': 1123})
            page.wait_for_timeout(100)
            over = page.evaluate(_OVERFLOW_JS)
            sheets_print = page.evaluate(_SHEETS_JS)  # тот же замер, но в вёрстке печати (шрифт и переносы могут отличаться от экрана)
            # 'null' — снять эмуляцию: с media='screen' page.pdf печатал бы экранные стили
            page.emulate_media(media='null')
            page.set_viewport_size({'width': 1280, 'height': 900})
            page.wait_for_timeout(100)
            # поля по умолчанию — как у Chrome (страница с @page задаёт свои, они важнее)
            page.pdf(path=pdf_path, format='A4', print_background=True, prefer_css_page_size=True,
                     margin={k: CHROME_DEFAULT_MARGIN for k in ('top', 'right', 'bottom', 'left')})
            pages = rasterize(pdf_path, args.out, slug)
            sizes = sorted({'%d×%d мм' % (w, h) for _n, w, h in pages})
            print('%s → %s.pdf (%s): страниц %d, лист %s, печатная область %d мм (поля @page%s)' % (
                path, slug, resp.status if resp else '?', len(pages), ', '.join(sizes), round(area / 96 * 25.4),
                ' ' + margins['name'] if margins['name'] else ''))
            for name, _w, _h in pages:
                print('    %s' % name)
            for sh in sheets:
                print('    бланк «%s»: %d строк, %s, строка %.1f мм, занято %d из 273 мм%s' % (
                    sh['name'], sh['rows'], sh['density'], sh['row'], sh['used'],
                    '' if sh['zoom'] == '1' else ' — ужат schedule.js (zoom %s)' % sh['zoom']))
            for sh in sheets_print:
                if sh['used'] > 274:
                    print('    ! в печати «%s» выше печатной области: %d из 273 мм' % (sh['name'], sh['used']))
            for o in over:
                print('    ! шире печатной области: %s' % o)
            for err in errors:
                print('    JS: %s' % err[:300])
        ctx.close()
        browser.close()


if __name__ == '__main__':
    main()
