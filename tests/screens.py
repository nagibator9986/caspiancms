# tests/screens.py — Playwright-скриншоты страниц CMS (вызывается из tests/run_screens.sh).
#
# Внешние ресурсы (Bootstrap, иконки, шрифты с CDN) загружаются через Python requests
# с проверкой TLS (системный CA/прокси окружения) и кэшируются в OUT/.cdn-cache —
# так скриншоты совпадают с тем, что увидит браузер админа.

import argparse
import hashlib
import json
import os
import re
import sys
from urllib.parse import urlparse

import requests
from playwright.sync_api import sync_playwright

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.seed import ADMIN  # noqa: E402

WIDTHS = [(1440, 900), (390, 844)]


def split_steps(spec):
    """'/ui-kit|name:kit_dark|click:#btn|wait:300' → ('/ui-kit', 'kit_dark', [('click', '#btn'), ('wait', '300')])."""
    parts = spec.split('|')
    path, name, steps = parts[0], None, []
    for p in parts[1:]:
        kind, _, arg = p.partition(':')
        if kind == 'name':
            name = arg
        else:
            steps.append((kind, arg))
    return path, name, steps


def run_steps(page, steps):
    """Действия перед снимком: click:<css>, type:<css>=<текст>, key:<клавиши>, wait:<мс>, eval:<js>,
    hover:<css>, files:<css>=<путь>,<путь>."""
    for kind, arg in steps:
        if kind == 'click':
            page.click(arg)
        elif kind == 'hover':
            page.hover(arg)
        elif kind == 'type':
            sel, _, text = arg.partition('=')
            page.fill(sel, text)
        elif kind == 'key':
            page.keyboard.press(arg)
        elif kind == 'wait':
            page.wait_for_timeout(int(arg or 300))
        elif kind == 'eval':
            page.evaluate(arg)
        elif kind == 'files':  # files:<css input[type=file]>=/abs/a.png,/abs/b.png
            sel, _, paths = arg.partition('=')
            page.set_input_files(sel, [p for p in paths.split(',') if p])
        else:
            raise SystemExit('Неизвестный шаг: %s' % kind)
        page.wait_for_timeout(250)


def slugify(path):
    s = path.lstrip('!').strip('/') or 'home'
    s = re.sub(r'[^0-9A-Za-zА-Яа-яЁё_-]+', '_', s).strip('_')
    return (('public_' if path.startswith('!') else '') + s)[:80] or 'page'


class CdnProxy:
    """Отдаёт внешние запросы браузера через requests (TLS проверяется) с дисковым кэшем."""

    def __init__(self, cache_dir):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.http = requests.Session()

    def handle(self, route):
        url = route.request.url
        host = urlparse(url).hostname or ''
        if host in ('127.0.0.1', 'localhost') or url.startswith('data:'):
            return route.continue_()
        key = hashlib.sha1(url.encode()).hexdigest()
        body_path = os.path.join(self.cache_dir, key)
        meta_path = body_path + '.json'
        try:
            if os.path.exists(body_path) and os.path.exists(meta_path):
                with open(meta_path) as fh:
                    meta = json.load(fh)
                with open(body_path, 'rb') as fh:
                    body = fh.read()
            else:
                headers = {'User-Agent': route.request.headers.get('user-agent', 'Mozilla/5.0')}
                r = self.http.get(url, headers=headers, timeout=30)
                body = r.content
                meta = {'status': r.status_code, 'headers': {
                    k: v for k, v in r.headers.items()
                    if k.lower() in ('content-type', 'access-control-allow-origin', 'cache-control')}}
                if r.status_code == 200:
                    with open(body_path, 'wb') as fh:
                        fh.write(body)
                    with open(meta_path, 'w') as fh:
                        json.dump(meta, fh)
            headers = dict(meta['headers'])
            headers.setdefault('access-control-allow-origin', '*')
            return route.fulfill(status=meta['status'], headers=headers, body=body)
        except Exception as e:  # нет сети — страница снимется без внешнего ресурса
            print('  ! внешний ресурс недоступен: %s (%s)' % (url, e))
            return route.abort()


def login(page, base):
    page.goto(base + '/login', wait_until='domcontentloaded')
    page.fill('#login-email', ADMIN['email'])
    page.fill('#login-password', ADMIN['password'])
    with page.expect_navigation():
        page.click('button[type=submit]')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--base', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('paths', nargs='+')
    args = ap.parse_args()
    theme = os.environ.get('SCREENS_THEME', 'light')
    cdn = CdnProxy(os.path.join(args.out, '.cdn-cache'))
    launch = {}
    if os.environ.get('CMS_CHROMIUM'):
        launch['executable_path'] = os.environ['CMS_CHROMIUM']
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        for width, height in WIDTHS:
            contexts = {}
            for anonymous in (False, True):
                ctx = browser.new_context(viewport={'width': width, 'height': height}, device_scale_factor=1,
                                          locale='ru-RU', timezone_id='Asia/Almaty')
                ctx.route('**/*', cdn.handle)
                ctx.add_init_script("try{localStorage.setItem('cc-theme', %s)}catch(e){}" % json.dumps(theme))
                page = ctx.new_page()
                page._cc_errors = []
                page.on('pageerror', lambda e, pg=page: pg._cc_errors.append(str(e)))
                page.on('console', lambda m, pg=page: pg._cc_errors.append('console: ' + m.text) if m.type == 'error' else None)
                if not anonymous:
                    login(page, args.base)
                contexts[anonymous] = (ctx, page)
            for spec in args.paths:
                path, custom_name, steps = split_steps(spec)
                anonymous = path.startswith('!')
                ctx, page = contexts[anonymous]
                url = args.base + path.lstrip('!')
                page._cc_errors.clear()
                resp = page.goto(url, wait_until='networkidle')
                page.wait_for_timeout(350)  # шрифты/анимации тостов
                run_steps(page, steps)
                name = '%s_%d.png' % (custom_name or slugify(path), width)
                page.screenshot(path=os.path.join(args.out, name), full_page=not steps)
                print('%s %s → %s (%s)' % (width, path, name, resp.status if resp else '?'))
                for err in page._cc_errors:
                    print('    JS: %s' % err[:300])
            for ctx, _page in contexts.values():
                ctx.close()
        browser.close()


if __name__ == '__main__':
    main()
