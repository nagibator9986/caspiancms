# tests/test_ui_foundation.py — общие компоненты дизайн-системы в браузере (FOUNDATION).
#
# Стенд — фрагмент разметки (как её выдают макросы _ui.html) + настоящие static/css/app.css и static/js/app.js,
# отдаются через page.route с адреса http://cc.test (сеть не нужна, Flask и эмуляторы — тоже):
#
#     python -m unittest tests.test_ui_foundation
#
# Нужны Playwright и Chromium (CMS_CHROMIUM или браузеры Playwright); без них тесты пропускаются.

import os
import unittest
from urllib.parse import urlparse

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # прод (PythonAnywhere) и venv без Playwright
    sync_playwright = None

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN = 'http://cc.test'

STAND = '''<!DOCTYPE html>
<html lang="ru" data-bs-theme="light"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<script>document.documentElement.className += ' cc-js';</script>
<link rel="stylesheet" href="/static/css/app.css">
<style>/* без Bootstrap: border-box (reboot) и отступы/рамка кнопок, как у .btn */
*, ::before, ::after { box-sizing: border-box; } .btn { padding: 4px 10px; border: 1px solid #ccc; }</style>
%(head)s</head>
<body class="cc-app">%(body)s
<script src="/static/js/app.js"></script>%(scripts)s</body></html>'''


class BrowserTestCase(unittest.TestCase):
    """Один Chromium на класс; self.open(body, width=…) — новая вкладка со стендом."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if sync_playwright is None:
            raise unittest.SkipTest('Playwright не установлен')
        cls._pw = sync_playwright().start()
        kw = {'executable_path': os.environ['CMS_CHROMIUM']} if os.environ.get('CMS_CHROMIUM') else {}
        try:
            cls.browser = cls._pw.chromium.launch(**kw)
        except Exception as e:  # нет браузера — пропускаем, а не падаем
            cls._pw.stop()
            raise unittest.SkipTest('Chromium недоступен: %s' % e)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls._pw.stop()
        super().tearDownClass()

    def open(self, body, width=1280, height=800, head='', scripts='', init_script=None, reduced_motion=True):
        ctx = self.browser.new_context(viewport={'width': width, 'height': height}, locale='ru-RU')
        self.addCleanup(ctx.close)
        if init_script:
            ctx.add_init_script(init_script)
        html = STAND % {'head': head, 'body': body, 'scripts': scripts}

        def handle(route):
            url = urlparse(route.request.url)
            if url.hostname != 'cc.test':
                return route.abort()
            if url.path.startswith('/static/'):
                path = os.path.join(ROOT, url.path.lstrip('/'))
                ctype = 'text/css' if path.endswith('.css') else 'application/javascript'
                with open(path, 'rb') as fh:
                    return route.fulfill(status=200, body=fh.read(), headers={'Content-Type': ctype + '; charset=utf-8'})
            if url.path == '/page':
                return route.fulfill(status=200, body=html, headers={'Content-Type': 'text/html; charset=utf-8'})
            return route.fulfill(status=200, body='<!DOCTYPE html><title>ok</title>', headers={'Content-Type': 'text/html'})

        ctx.route('**/*', handle)
        page = ctx.new_page()
        errors = []
        page.on('pageerror', lambda e: errors.append(str(e)))
        self.addCleanup(lambda: self.assertEqual(errors, [], 'JS-ошибки на стенде'))
        if reduced_motion:
            page.emulate_media(reduced_motion='reduce')   # переходы мгновенные — opacity без ожидания
        page.goto(ORIGIN + '/page')
        page.wait_for_load_state('load')
        return page


# ----------------------------------------------------------------------------- панель массовых действий
BULK = '''
<form id="b" method="post" action="/sink"></form>
<table class="cc-table"><tbody>
  <tr><td class="cc-select"><input type="checkbox" class="form-check-input" name="ids" value="1" form="b" data-cc-select="b"></td><td>Абдрахманова Арсен</td></tr>
  <tr><td class="cc-select"><input type="checkbox" class="form-check-input" name="ids" value="2" form="b" data-cc-select="b"></td><td>Ахметов Арсен</td></tr>
</tbody></table>
<div class="cc-bulkbar" data-cc-bulkbar="b" data-noun="пользователь|пользователя|пользователей" role="region" aria-label="Действия с выбранными">
  <span class="cc-bulkbar__count"><b class="cc-num" data-cc-bulk-count>0</b> выбрано</span>
  <div class="cc-bulkbar__actions">
    <button type="button" class="btn btn-sm btn-light"><span>В группу…</span></button>
    <button type="button" class="btn btn-sm btn-light"><span>Курс…</span></button>
    <button type="button" class="btn btn-sm btn-light"><span>CSV</span></button>
    <button type="button" class="btn btn-sm btn-outline-danger"><span>Удалить</span></button>
  </div>
  <button type="button" class="btn btn-ghost btn-sm cc-bulkbar__clear" data-cc-bulk-clear><span>Снять выделение</span></button>
</div>'''

BULK_GEOMETRY = '''() => {
  const bar = document.querySelector('.cc-bulkbar'), r = bar.getBoundingClientRect();
  return {visible: bar.classList.contains('is-visible'), left: r.left, right: r.right, width: r.width,
          tops: [...bar.querySelectorAll('.btn')].map(b => Math.round(b.getBoundingClientRect().top))};
}'''


class BulkbarTest(BrowserTestCase):

    def test_one_row_and_inside_screen(self):
        # fixed + left: 50% сжимает панель до половины экрана — кнопки уходили на 2 строки (≥ 576 и ≥ 992 px)
        for width in (800, 1100, 1440):
            with self.subTest(width=width):
                page = self.open(BULK, width=width)
                page.click('input[value="1"]')
                g = page.evaluate(BULK_GEOMETRY)
                self.assertTrue(g['visible'])
                self.assertEqual(len(set(g['tops'])), 1, 'кнопки панели в одну строку: %s' % g)
                self.assertGreaterEqual(g['left'], 16)
                self.assertLessEqual(g['right'], width - 16)

    def test_narrow_tablet_limited_by_screen(self):
        page = self.open(BULK, width=600)
        page.click('input[value="1"]')
        g = page.evaluate(BULK_GEOMETRY)
        self.assertLessEqual(g['width'], 600 - 32 + 0.5)
        self.assertGreaterEqual(g['left'], 15.5)


# ----------------------------------------------------------------------------- « · » в строках-карточках
STACK = '''
<table class="cc-table cc-table--stack"><tbody>
  <tr id="hidden-first"><td class="cc-cell-primary">Имя</td><td class="cc-cell-meta cc-col-optional">скрыта</td>
      <td class="cc-cell-meta">A</td><td class="cc-cell-meta">B</td></tr>
  <tr id="hidden-between"><td class="cc-cell-primary">Имя</td><td class="cc-cell-meta">A</td>
      <td class="cc-col-optional">+7 701 000 00 00</td><td class="cc-cell-meta">B</td></tr>
  <tr id="empty-class"><td class="cc-cell-primary">Имя</td><td class="cc-cell-meta cc-cell-empty">—</td>
      <td class="cc-cell-meta">A</td><td class="cc-cell-meta cc-cell-empty">—</td><td class="cc-cell-meta">B</td></tr>
  <tr id="empty-blank"><td class="cc-cell-primary">Имя</td><td class="cc-cell-meta">A</td><td class="cc-cell-meta">
      </td><td class="cc-cell-meta"></td><td class="cc-cell-meta">B</td></tr>
  <tr id="block-between"><td class="cc-cell-meta">08:00</td><td class="cc-cell-primary">Предмет</td>
      <td class="cc-cell-meta">A</td><td class="cc-cell-meta">B</td></tr>
</tbody></table>
<style>.cc-table--stack td.cc-cell-meta:first-child { display: block; }</style>'''

STACK_METAS = '''() => Object.fromEntries([...document.querySelectorAll('tbody tr')].map(tr => [tr.id,
  [...tr.querySelectorAll('td.cc-cell-meta')].filter(td => getComputedStyle(td).display !== 'none').map(td => {
    const c = getComputedStyle(td, '::before').content;
    return [td.textContent.trim(), c !== 'none' && c !== 'normal' && c.indexOf('·') !== -1];
  })]))'''


class StackSeparatorTest(BrowserTestCase):

    def test_separator_only_between_visible_meta(self):
        page = self.open(STACK, width=390, height=844)
        metas = page.evaluate(STACK_METAS)
        self.assertEqual(metas['hidden-first'], [['A', False], ['B', True]], 'скрытая первая мета не даёт « · » перед A')
        self.assertEqual(metas['hidden-between'], [['A', False], ['B', True]], 'скрытая ячейка между метами не съедает « · »')
        self.assertEqual(metas['empty-class'], [['A', False], ['B', True]], '.cc-cell-empty в карточке не видна')
        self.assertEqual(metas['empty-blank'], [['A', False], ['B', True]], 'пустая (пробелы) ячейка — без двойного « · »')
        self.assertEqual(metas['block-between'], [['08:00', False], ['A', False], ['B', True]], 'после блочной ячейки — с новой строки без « · »')

    def test_desktop_cells_untouched(self):
        page = self.open(STACK, width=1280)
        shown = page.evaluate('''() => [...document.querySelectorAll('#empty-class td')].map(td => getComputedStyle(td).display)''')
        self.assertNotIn('none', shown, 'на компьютере «—» в пустой ячейке остаётся')


# ----------------------------------------------------------------------------- CC.hotkey
HOTKEYS = '''<script>
  window.__calls = [];
  CC.hotkey('ArrowLeft', function (e) { window.__calls.push('left'); }, 'Предыдущий день', {group: 'Тест', preventDefault: false});
  CC.hotkey('ArrowRight', function (e) { window.__calls.push('right'); }, 'Следующий день', {group: 'Тест'});
  document.addEventListener('keydown', function (e) { window.__prevented = e.defaultPrevented; });
</script>'''


class HotkeyTest(BrowserTestCase):

    def test_prevent_default_option(self):
        page = self.open('<div style="height: 3000px; width: 3000px">Длинная страница</div>', scripts=HOTKEYS)
        page.keyboard.press('ArrowLeft')
        self.assertEqual(page.evaluate('[window.__calls, window.__prevented]'), [['left'], False],
                         'preventDefault: false — стрелка по-прежнему прокручивает страницу')
        page.keyboard.press('ArrowRight')
        self.assertEqual(page.evaluate('[window.__calls, window.__prevented]'), [['left', 'right'], True])

    def test_not_in_inputs(self):
        page = self.open('<input id="q" value="текст">', scripts=HOTKEYS)
        page.focus('#q')
        page.keyboard.press('ArrowLeft')
        page.keyboard.press('ArrowRight')
        self.assertEqual(page.evaluate('[window.__calls, window.__prevented]'), [[], False])


# ----------------------------------------------------------------------------- data-dirty-guard
DIRTY = '''
<iframe name="sink" title="sink" style="display: none"></iframe>
<form id="json" method="post" action="/save" data-dirty-guard><input name="a"><button type="submit">Сохранить</button></form>
<form id="plain" method="post" action="/save" target="sink" data-dirty-guard><input name="a"><button type="submit">Сохранить</button></form>
<form id="prog" method="post" action="/done" data-dirty-guard><input name="a"></form>'''
# модуль вешает свой обработчик ПОСЛЕ app.js (как schedule.js): сохранение через JSON, отправку формы отменяет
DIRTY_JS = '''<script>
  CC.onReady(function () {
    document.getElementById('json').addEventListener('submit', function (e) { e.preventDefault(); window.__saves = (window.__saves || 0) + 1; });
  });
  window.addEventListener('beforeunload', function () { sessionStorage.setItem('dirtyAtUnload', String(document.getElementById('prog')._ccIsDirty)); });
</script>'''


class DirtyGuardTest(BrowserTestCase):

    def dirty(self, page, fid):
        return page.evaluate('id => document.getElementById(id)._ccIsDirty', fid)

    def test_json_save_keeps_dirty_until_success(self):
        page = self.open(DIRTY, scripts=DIRTY_JS)
        page.fill('#json input', 'новое значение')
        self.assertTrue(self.dirty(page, 'json'))
        page.click('#json button')
        self.assertEqual(page.evaluate('window.__saves'), 1)
        self.assertTrue(self.dirty(page, 'json'), 'отправка отменена (JSON) — изменения ещё не сохранены')
        page.evaluate("CC.markClean(document.getElementById('json'))")       # сервер ответил успешно
        self.assertFalse(self.dirty(page, 'json'))
        page.evaluate("CC.markDirty(document.getElementById('json'))")
        self.assertTrue(self.dirty(page, 'json'))

    def test_programmatic_submit_marks_clean(self):
        # модуль сам проверил/подтвердил и отправил form.submit() (событие submit не возникает) — без вопроса при уходе
        page = self.open(DIRTY, scripts=DIRTY_JS)
        page.fill('#prog input', 'x')
        self.assertTrue(self.dirty(page, 'prog'))
        with page.expect_navigation():
            page.evaluate("document.getElementById('prog').submit()")
        self.assertEqual(page.evaluate("sessionStorage.getItem('dirtyAtUnload')"), 'false')

    def test_plain_submit_marks_clean(self):
        page = self.open(DIRTY, scripts=DIRTY_JS)
        page.fill('#plain input', 'x')
        self.assertTrue(self.dirty(page, 'plain'))
        page.click('#plain button')
        self.assertFalse(self.dirty(page, 'plain'))


# ----------------------------------------------------------------------------- пилюли filter_select
FSEL = '''
<form class="cc-filters cc-filters--inline" method="get" action="/list" role="search" data-cc-filters>
  <div class="offcanvas-md offcanvas-bottom cc-filters__sheet" id="flt"><div class="offcanvas-body cc-filters__fields">
    <div class="cc-filters__field cc-fsel">
      <label class="form-label cc-fsel__label" for="flt-group">Группа</label>
      <select class="form-select cc-fsel__select" id="flt-group" name="group">
        <option value="">Все</option><option value="long">Информационные системы и программирование, 3 курс</option>
      </select>
    </div>
  </div></div>
</form>'''
NO_FIELD_SIZING = '''(() => {   // браузер без field-sizing (Firefox, Safari)
  const orig = CSS.supports.bind(CSS);
  CSS.supports = function (a, b) { return String(a).indexOf('field-sizing') !== -1 ? false : orig.apply(null, arguments); };
})();'''
NO_FIELD_SIZING_CSS = '<style>select.cc-fsel__select { field-sizing: fixed !important; }</style>'


class FilterSelectWidthTest(BrowserTestCase):

    def width(self, page):
        return page.evaluate("document.getElementById('flt-group').getBoundingClientRect().width")

    def test_fallback_without_field_sizing(self):
        page = self.open(FSEL, head=NO_FIELD_SIZING_CSS, init_script=NO_FIELD_SIZING)
        self.assertLess(self.width(page), 100, 'пилюля «Группа: Все» — по выбранному значению, а не по самому длинному')
        page.select_option('#flt-group', 'long')
        self.assertGreaterEqual(self.width(page), 150, 'длинное значение — шире (до max-width)')
        page.set_viewport_size({'width': 390, 'height': 844})
        page.wait_for_timeout(50)
        self.assertEqual(page.evaluate("document.getElementById('flt-group').style.width"), '',
                         'в шторке на телефоне поле во всю ширину — инлайн-ширину снимаем')

    def test_native_field_sizing_untouched(self):
        page = self.open(FSEL)
        self.assertEqual(page.evaluate("document.getElementById('flt-group').style.width"), '')


# ----------------------------------------------------------------------------- .cc-row-quick при наведении
QUICK = '''
<article class="cc-card" id="card"><div id="card-head" style="padding: 20px">Карточка со списком</div>
  <table class="cc-table"><tbody>
    <tr id="r1"><td>Строка 1</td><td class="cc-actions"><div class="cc-row-menu"><a class="btn cc-row-quick" id="q1" href="#">✎</a></div></td></tr>
    <tr id="r2"><td>Строка 2</td><td class="cc-actions"><div class="cc-row-menu"><a class="btn cc-row-quick" id="q2" href="#">✎</a></div></td></tr>
  </tbody></table>
</article>
<ul style="margin-top: 20px"><li id="li1" style="padding: 10px">Член администрации <div class="cc-row-menu"><a class="btn cc-row-quick" id="q3" href="#">✎</a></div></li></ul>
<article id="club" style="padding: 10px">Клуб робототехники <div class="cc-row-menu"><a class="btn cc-row-quick" id="q4" href="#">✎</a></div></article>
<div data-cc-row id="row" style="padding: 10px">Лид <div class="cc-row-menu"><a class="btn cc-row-quick" id="q5" href="#">✎</a></div></div>
<p id="away" style="padding: 20px">Пусто</p>'''


class RowQuickTest(BrowserTestCase):

    def opacity(self, page):
        return page.evaluate('''() => Object.fromEntries([...document.querySelectorAll('.cc-row-quick')]
                                    .map(q => [q.id, getComputedStyle(q).opacity]))''')

    def test_hover_any_row_type(self):
        page = self.open(QUICK)
        if not page.evaluate("matchMedia('(hover: hover) and (pointer: fine)').matches"):
            self.skipTest('браузер без hover')
        page.hover('#away')
        self.assertEqual(set(self.opacity(page).values()), {'0'})
        expected = {'#r1': 'q1', '#li1': 'q3', '#club': 'q4', '#row': 'q5'}
        for target, quick in expected.items():
            with self.subTest(target=target):
                page.hover(target)
                op = self.opacity(page)
                self.assertEqual(op[quick], '1', 'наведение на %s показывает свою иконку' % target)
                self.assertEqual([k for k, v in op.items() if v == '1'], [quick], 'остальные строки — без иконки')
        page.hover('#card-head')   # карточка под курсором, строка — нет
        self.assertEqual(set(self.opacity(page).values()), {'0'})


# ----------------------------------------------------------------------------- «‹ Лиды» на телефоне — одна
def back_stand(top_href, top_label, head_href, head_label):
    return '''
<header class="cc-topbar"><div class="cc-topbar__title"><div class="cc-topbar__mobile">
  <a class="cc-topbar__back" href="%s" data-cc-back><i class="bi bi-chevron-left"></i><span>%s</span></a></div></div></header>
<main id="cc-main" class="cc-content"><div class="cc-page-header">
  <a class="cc-page-header__back" href="%s" data-cc-back><i class="bi bi-chevron-left"></i><span>%s</span></a>
  <div class="cc-page-header__row"><div class="cc-page-header__text"><div class="cc-page-header__title"><h1>Айгерим Сапарова</h1></div></div></div>
</div></main>''' % (top_href, top_label, head_href, head_label)


BACK_STATE = '''() => {
  const t = document.querySelector('.cc-topbar__back'), h = document.querySelector('.cc-page-header__back');
  const shown = el => getComputedStyle(el).display !== 'none' && getComputedStyle(el.parentElement).display !== 'none';
  return {top: [t.getAttribute('href'), shown(t)], head: [h.getAttribute('href'), shown(h)]};
}'''


class BackLinkDedupTest(BrowserTestCase):

    def test_same_label_other_list(self):
        # карточка лида, открытая из «Пользователей» (next=…): в топбаре «‹ Лиды» и в шапке «‹ Лиды» — дубль
        page = self.open(back_stand('/applicants/leads', 'Лиды', '/admin/users?role=applicant', 'Лиды'), width=390, height=844)
        self.assertEqual(page.evaluate(BACK_STATE), {'top': ['/admin/users?role=applicant', True],
                                                     'head': ['/admin/users?role=applicant', False]})

    def test_same_list_with_filters(self):
        page = self.open(back_stand('/applicants/leads', 'Лиды', '/applicants/leads?status=new', 'Лиды'), width=390, height=844)
        self.assertEqual(page.evaluate(BACK_STATE), {'top': ['/applicants/leads?status=new', True],
                                                     'head': ['/applicants/leads?status=new', False]})

    def test_other_target_keeps_both(self):
        # «‹ Расписание» (раздел) и «‹ ВТ 24-1» (неделя группы) — разные цели, обе ссылки остаются
        page = self.open(back_stand('/schedule', 'Расписание', '/schedule?group=ВТ 24-1', 'ВТ 24-1'), width=390, height=844)
        state = page.evaluate(BACK_STATE)
        self.assertEqual(state['top'], ['/schedule', True])
        self.assertTrue(state['head'][1])

    def test_desktop_header_back_visible(self):
        page = self.open(back_stand('/applicants/leads', 'Лиды', '/admin/users?role=applicant', 'Лиды'), width=1280)
        self.assertTrue(page.evaluate(BACK_STATE)['head'][1], 'на компьютере ссылка в шапке видна (топбар-ссылки нет)')


if __name__ == '__main__':
    unittest.main()
