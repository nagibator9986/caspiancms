# tests/test_core.py — ядро CMS: вход, CSRF, admin_required, кэш, Storage, аудит, пагинация (FOUNDATION).

import io
import time

from tests.base import CmsTestCase
from tests import seed as S


class AuthTest(CmsTestCase):
    login_as_admin = False

    def test_login_page_renders_without_sidebar(self):
        html = self.get_ok('/login')
        self.assertIn('csrf_token', html)
        self.assertNotIn('cc-sidebar', html)

    def test_login_success_and_next(self):
        self.client.get('/login')
        r = self.client.post('/login', data={'email': S.ADMIN['email'], 'password': S.ADMIN['password'],
                                             'csrf_token': self.csrf(), 'next': '/schedule/rooms'})
        self.assertRedirectsTo(r, '/schedule/rooms')
        self.get_ok('/schedule/rooms')

    def test_open_redirect_blocked(self):
        self.client.get('/login')
        r = self.client.post('/login', data={'email': S.ADMIN['email'], 'password': S.ADMIN['password'],
                                             'csrf_token': self.csrf(), 'next': '//evil.example/x'})
        self.assertRedirectsTo(r, '/')
        self.assertNotIn('evil', r.headers['Location'])

    def test_wrong_password(self):
        r = self.login(password='wrong-password', expect_ok=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('Неверный email или пароль', r.get_data(as_text=True))
        self.assertRedirectsTo(self.client.get('/'), '/login')

    def test_non_admin_cannot_login(self):
        r = self.login(S.TEACHER_ACCOUNT['email'], S.TEACHER_ACCOUNT['password'], expect_ok=False)
        self.assertEqual(r.status_code, 200)
        self.assertIn('только для администраторов', r.get_data(as_text=True))

    def test_login_requires_csrf(self):
        self.client.get('/login')
        r = self.client.post('/login', data={'email': S.ADMIN['email'], 'password': S.ADMIN['password']})
        self.assertEqual(r.status_code, 400)
        self.assertIn('Сессия устарела', r.get_data(as_text=True))

    def test_rate_limit(self):
        for _ in range(10):
            self.login(password='bad-%d' % _, expect_ok=False)
        r = self.login(expect_ok=False)  # даже верный пароль — после 10 ошибок блок
        self.assertEqual(r.status_code, 429)


class SessionSecurityTest(CmsTestCase):

    def test_csrf_required_for_post(self):
        r = self.client.post('/schedule/delete/base_00_1_0')
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(self.doc('schedules', 'base_00_1_0'))

    def test_csrf_header_for_json(self):
        r = self.client.post('/api/cache/invalidate', headers={'X-Requested-With': 'XMLHttpRequest'})
        self.assertEqual(r.status_code, 400)
        r = self.post_json('/api/cache/invalidate')
        self.assertEqual(r.status_code, 200)

    def test_role_rechecked_after_interval(self):
        self.get_ok('/')
        from cms.core import db
        db.collection('users').document(S.ADMIN['uid']).update({'role': 'teacher'})
        try:
            self.get_ok('/')  # проверка ещё свежая — пускаем
            with self.client.session_transaction() as s:
                s['role_checked_at'] = time.time() - 11 * 60
            r = self.client.get('/')
            self.assertEqual(r.status_code, 403)
            self.assertRedirectsTo(self.client.get('/'), '/login')  # сессия очищена
        finally:
            db.collection('users').document(S.ADMIN['uid']).update({'role': 'admin'})

    def test_forged_session_without_secret_rejected(self):
        anon = self.app.test_client()
        anon.set_cookie('cc_session', 'eyJ1aWQiOiJ4IiwiaXNfYWRtaW4iOnRydWV9.forged.sig')
        self.assertRedirectsTo(anon.get('/'), '/login')

    def test_session_cookie_flags(self):
        r = self.client.get('/login')
        cookie = r.headers.get('Set-Cookie', '')
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Lax', cookie)

    def test_security_headers(self):
        r = self.client.get('/')
        self.assertEqual(r.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertEqual(r.headers.get('X-Content-Type-Options'), 'nosniff')

    def test_json_401_for_api(self):
        anon = self.app.test_client()
        r = anon.get('/api/schedule/all')
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()['ok'])


class CoreHelpersTest(CmsTestCase):

    def test_audit_written_on_login(self):
        entries = self.audit_entries('login')
        self.assertTrue(any(e.get('by') == S.ADMIN['email'] for e in entries))

    def test_audit_page(self):
        html = self.get_ok('/audit')
        self.assertIn('reset_password', html)          # запись из сида
        self.assertIn('admin@caspian.test', html)

    def test_count_and_sum(self):
        from cms.core import aggregate_sum, count, db, where
        self.assertEqual(count(where(db.collection('users'), 'role', '==', 'applicant')), 42)
        self.assertEqual(count(where(db.collection('coin_orders'), 'status', '==', 'reserved')), 3)
        total = aggregate_sum(where(db.collection('coin_orders'), 'status', '==', 'reserved'), 'price')
        self.assertEqual(total, 800 + 300 + 50)

    def test_paginate_cursor(self):
        from cms.core import db, paginate, where
        q = where(db.collection('users'), 'role', '==', 'applicant').order_by('createdAt', direction='DESCENDING')
        with self.app.test_request_context('/'):
            p1 = paginate(q, 20)
            self.assertEqual(len(p1), 20)
            self.assertTrue(p1.has_next)
            p2 = paginate(q, 20, p1.next_cursor)
            p3 = paginate(q, 20, p2.next_cursor)
        ids = [d.id for d in p1.items] + [d.id for d in p2.items] + [d.id for d in p3.items]
        self.assertEqual(len(ids), 42)
        self.assertEqual(len(set(ids)), 42)
        self.assertFalse(p3.has_next)

    def test_paginate_list(self):
        from cms.core import paginate_list
        p = paginate_list(list(range(53)), page=3, per_page=25)
        self.assertEqual(p.items, [50, 51, 52])
        self.assertEqual(p.pages, 3)
        self.assertTrue(p.has_prev)
        self.assertFalse(p.has_next)
        self.assertEqual(paginate_list([], 5).page, 1)

    def test_get_docs_batch(self):
        from cms.core import get_docs
        docs = get_docs('users', ['t_petrov', 'ap_001', 'missing'], field_paths=['name', 'role'])
        self.assertEqual(set(docs), {'t_petrov', 'ap_001'})
        self.assertEqual(docs['t_petrov']['role'], 'teacher')

    def test_upload_and_delete_images(self):
        from cms import core
        files = [S._File(S._png(320, 200, (255, 98, 0)), 'фото.png'), S._File(S._png(10, 20, (0, 0, 0)), 'a.png')]
        images = core.upload_images(files, 'test_uploads')
        self.assertEqual(len(images), 2)
        self.assertEqual((images[0]['w'], images[0]['h']), (320, 200))
        self.assertTrue(images[0]['path'].startswith('test_uploads/'))
        self.assertTrue(images[0]['path'].endswith('_0.png'))
        self.assertIn('/v0/b/%s/o/test_uploads%%2F' % core.bucket.name, images[0]['url'])
        self.assertIn('alt=media&token=', images[0]['url'])
        blob = core.bucket.get_blob(images[0]['path'])
        self.assertEqual(blob.content_type, 'image/png')
        self.assertEqual(blob.cache_control, 'public, max-age=604800')
        self.assertIn('firebaseStorageDownloadTokens', blob.metadata or {})
        self.assertEqual(core.storage_path_from_url(images[1]['url']), images[1]['path'])
        self.assertEqual(core.delete_storage_paths([images[0]['path'], images[1]['url'], 'missing/x.png']), 2)
        self.assertIsNone(core.bucket.get_blob(images[0]['path']))

    def test_upload_rejects_non_images(self):
        from cms import core
        with self.assertRaises(core.UploadError):
            core.upload_image(S._File(b'<?php echo 1; ?>', 'evil.png'), 'test_uploads')
        with self.assertRaises(core.UploadError):
            core.upload_images([S._File(S._png(2, 2, (1, 1, 1)), 'x.png')] * 11, 'test_uploads')

    def test_images_from_uploader_order_and_removal(self):
        from cms import core
        from werkzeug.datastructures import FileStorage
        existing = core.images_of(self.doc('news', 'news_open_day'))
        self.assertEqual(len(existing), 3)
        state = ('[{"kind":"new","idx":0},{"kind":"existing","url":"%s"},{"kind":"existing","url":"https://evil/x.png"}]'
                 % existing[2]['url'])
        data = {'images__state': state,
                'images': (io.BytesIO(S._png(64, 32, (10, 20, 30))), 'new.png')}
        with self.app.test_request_context('/', method='POST', data=data, content_type='multipart/form-data'):
            res = core.images_from_uploader('images', existing, 'news')
        self.assertEqual(len(res.images), 2)
        self.assertEqual((res.images[0]['w'], res.images[0]['h']), (64, 32))
        self.assertEqual(res.images[1]['url'], existing[2]['url'])
        self.assertEqual(len(res.removed), 2)
        self.assertEqual(len(res.uploaded_paths), 1)
        core.delete_storage_paths(res.uploaded_paths)
        fields = core.image_fields(res.images)
        self.assertEqual(fields['imageUrl'], res.images[0]['url'])

    def test_images_of_legacy(self):
        from cms.core import images_of
        self.assertEqual(images_of({'imageUrl': 'https://x/y.jpg'}), [{'url': 'https://x/y.jpg', 'path': '', 'w': 0, 'h': 0}])
        self.assertEqual(len(images_of({'images': ['a', {'url': 'a'}, {'url': 'b', 'w': '10'}, '', None]})), 2)
        self.assertEqual(images_of({'images': [{'url': 'b', 'w': '10'}]})[0]['w'], 10)
        self.assertEqual(images_of({'images': [], 'imageUrl': ''}), [])

    def test_cache_cross_process_invalidation(self):
        import tempfile
        from cms.core import CacheManager
        d = tempfile.mkdtemp()
        a, b = CacheManager(60, d), CacheManager(60, d)
        b.set('schedule:snapshot', 1)
        b.set('other', 2)
        a.invalidate_prefix('schedule:')
        time.sleep(1.1)  # b перечитывает штамп не чаще раза в секунду
        self.assertIsNone(b.get('schedule:snapshot'))
        self.assertEqual(b.get('other'), 2)
        b.set('k', [])
        self.assertEqual(b.get('k'), [])  # пустые значения кэшируются
        self.assertEqual(b.get_or_set('none', lambda: None), None)
        calls = []
        b.get_or_set('none2', lambda: calls.append(1))
        b.get_or_set('none2', lambda: calls.append(1))
        self.assertEqual(len(calls), 1)

    def test_time_helpers(self):
        from datetime import datetime, timezone
        from cms.core import fmt_dt, parse_local_datetime, plural, to_almaty
        dt = datetime(2026, 10, 19, 20, 30, tzinfo=timezone.utc)
        self.assertEqual(fmt_dt(dt), '20.10.2026 01:30')
        self.assertEqual(to_almaty('2026-10-19T20:30:00Z').hour, 1)
        self.assertEqual(parse_local_datetime('2026-10-20T01:30'), dt)
        self.assertEqual([plural(n, 'голос', 'голоса', 'голосов') for n in (1, 2, 5, 11, 21, 22, 112)],
                         ['голос', 'голоса', 'голосов', 'голосов', 'голос', 'голоса', 'голосов'])
        self.assertEqual(fmt_dt(None), '—')


class UiKitTest(CmsTestCase):

    def test_ui_kit_renders_all_macros(self):
        html = self.get_ok('/ui-kit')
        for marker in ('cc-stat', 'cc-tabs', 'cc-filters', 'data-table-search', 'data-confirm', 'data-cc-uploader',
                       'images__state', 'cc-empty', 'pagination', 'Ждёт выдачи', 'Знаменатель'):
            self.assertIn(marker, html)

    def test_ui_kit_uploader_dry_run(self):
        data = {'images__state': '[{"kind":"new","idx":0},{"kind":"existing","url":"/static/img/logo.png"}]',
                'images': (io.BytesIO(S._png(8, 8, (1, 2, 3))), 'a.png'), 'title': 'x'}
        r = self.post('/ui-kit', data=data, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/ui-kit')
        self.assertIn(('success', 'Порядок: новое, старое; новых файлов: 1'), self.flashes())


class PushAndCsvTest(CmsTestCase):
    login_as_admin = False

    def test_sanitize_topic_like_functions(self):
        from cms.core import sanitize_topic
        self.assertEqual(sanitize_topic('schedule_group_ПО 24-1'), 'schedule_group_po_24-1')
        self.assertEqual(sanitize_topic('schedule_group_ВТ 25-1п'), 'schedule_group_vt_25-1p')
        self.assertEqual(sanitize_topic('schedule_teacher_AbC123xyz789'), 'schedule_teacher_abc123xyz789')
        self.assertEqual(sanitize_topic('Объём'), 'ob_yom')        # ъ → '_' (как в JS)
        self.assertEqual(sanitize_topic('  '), 'default')
        self.assertEqual(sanitize_topic(None), 'default')
        self.assertEqual(sanitize_topic('__a__'), 'a')

    def test_send_push_recorded_in_emulator(self):
        from cms import core
        core.sent_pushes.clear()
        self.assertTrue(core.send_push('Изменение в расписании', 'ПО 24-1: 3 пары', topic='schedule_group_ПО 24-1',
                                       data={'type': 'schedule', 'n': 3}))
        self.assertEqual(core.sent_pushes[-1]['topic'], 'schedule_group_po_24-1')
        self.assertEqual(core.sent_pushes[-1]['data'], {'type': 'schedule', 'n': '3'})
        self.assertFalse(core.send_push('x', 'y'))

    def test_csv_response(self):
        from cms.core import csv_response
        with self.app.test_request_context('/'):
            r = csv_response('лиды.csv', ['Имя', 'Логин'], [['Айгерим', '+77011234567'], ['Тимур; "К"', None]])
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith('﻿Имя;Логин\r\n'))
        self.assertIn('"Тимур; ""К"""', body)
        self.assertIn("filename*=UTF-8''%D0%BB%D0%B8%D0%B4%D1%8B.csv", r.headers['Content-Disposition'])


class CredentialsGuardTest(CmsTestCase):
    """Отклонённый Google ключ сервис-аккаунта → сразу 503 с понятным текстом,
    а не зависание каждого запроса на 300 с (так было 19.10 на проде)."""

    def setUp(self):
        super().setUp()
        from cms import core
        self.core = core
        core.reset_credentials_check()

    def tearDown(self):
        self.core.reset_credentials_check()
        super().tearDown()

    def test_rejected_key_gives_fast_503_and_is_cached(self):
        from unittest import mock
        from google.auth.exceptions import RefreshError
        err = RefreshError('invalid_grant: Invalid JWT Signature.')
        with mock.patch.object(self.core, '_probe_credentials', side_effect=err) as probe:
            started = time.time()
            r = self.client.get('/')
            self.assertEqual(r.status_code, 503)
            self.assertIn('ключ сервис-аккаунта', r.get_data(as_text=True))
            self.assertLess(time.time() - started, 5)
            # Результат кэшируется: следующий запрос не обращается к Google повторно.
            self.assertEqual(self.client.get('/login').status_code, 503)
            self.assertEqual(probe.call_count, 1)
        # Ключ заменили — после сброса кэша админка снова работает.
        self.core.reset_credentials_check()
        self.get_ok('/')

    def test_missing_key_file_gives_503(self):
        from unittest import mock
        with mock.patch.object(self.core, '_probe_credentials', side_effect=FileNotFoundError('нет файла')):
            self.assertEqual(self.client.get('/').status_code, 503)

    def test_network_error_does_not_block_site(self):
        from unittest import mock
        with mock.patch.object(self.core, '_probe_credentials', side_effect=RuntimeError('timeout')):
            self.get_ok('/')

    def test_static_is_not_blocked(self):
        from unittest import mock
        from google.auth.exceptions import RefreshError
        with mock.patch.object(self.core, '_probe_credentials', side_effect=RefreshError('invalid_grant')):
            self.assertEqual(self.client.get('/static/css/app.css').status_code, 200)


class NavigationV2Test(CmsTestCase):
    """UX_SPEC_v2 §2: порядок сайдбара, свёрнутые второстепенные группы, NAV_PARENT, крошки-ссылки."""

    def test_sidebar_order_secondary_last(self):
        from cms.core import NAV_COLLAPSED, NAV_SECTIONS
        titles = [t for t, _i, _items in NAV_SECTIONS]
        self.assertEqual(titles[:5], ['Главная', 'Расписание', 'Контент', 'Люди', 'Сервис'])
        self.assertEqual(titles[-2:], ['Абитуриенты', 'Caspian Coin'])
        self.assertEqual(NAV_COLLAPSED, frozenset({'Абитуриенты', 'Caspian Coin'}))
        html = self.get_ok('/')
        pos = [html.index('data-nav-group="%s"' % t) for t in titles[1:]]
        self.assertEqual(pos, sorted(pos))
        self.assertIn('cc-nav-sep', html)   # разделитель перед второстепенными группами

    def test_collapsed_group_renders_items_and_badges(self):
        html = self.get_ok('/')
        self.assertRegex(html, r'class="cc-nav-group cc-nav-group--secondary" data-nav-group="Абитуриенты"')
        self.assertRegex(html, r'class="cc-nav-group is-open" data-nav-group="Расписание"')
        self.assertIn('Лиды', html)   # пункты свёрнутой группы в HTML (Ctrl+K, бейджи)
        self.assertRegex(html, r'Заказы</span>\s*<span class="cc-nav-badge cc-nav-badge--muted">3<')
        self.assertRegex(html, r'Обратная связь</span>\s*<span class="cc-nav-badge cc-nav-badge--brand">3<')
        self.assertIn('cc-nav-group__count cc-nav-group__count--muted', html)   # сумма бейджей свёрнутой группы

    def test_child_page_highlights_parent_and_crumbs(self):
        html = self.get_ok('/applicants/ap_001')
        self.assertRegex(html, r'class="cc-nav-group is-open cc-nav-group--secondary" data-nav-group="Абитуриенты" data-nav-active')
        self.assertRegex(html, r'class="cc-nav-link active" href="/applicants/leads" aria-current="page"')
        self.assertNotRegex(html, r'class="cc-nav-link active" href="/applicants"[ >]')
        self.assertIn('<a href="/applicants/leads" data-cc-back>Лиды</a>', html)   # крошка — ссылка на список
        self.assertIn('<span>Абитуриенты</span>', html)
        self.assertIn('cc-topbar__back', html)   # на телефоне — «‹ Лиды»

    def test_sidebar_items_and_extra_search(self):
        html = self.get_ok('/schedule/rooms')
        self.assertIn('>Аудитории</span>', html)
        self.assertNotIn('>Конструктор</span>', html)   # конструктор — действие, а не раздел
        import json
        import re
        data = json.loads(re.search(r'<script type="application/json" id="cc-nav-data">(.*?)</script>', html, re.S).group(1))
        labels = {d['label'] for d in data}
        self.assertIn('Добавить занятие (конструктор)', labels)
        self.assertIn('Журнал коинов', labels)
        self.assertIn('command', {d.get('kind') for d in data})

    def test_set_nav_active_and_parent_rules(self):
        from cms import core
        self.assertEqual(core.NAV_PARENT['coins_journal'], 'coins_shop')
        with self.app.test_request_context('/coins/journal'):
            core.set_nav_active('applicants_leads')
            self.assertEqual(core._active_endpoint(), 'applicants_leads')
        html = self.get_ok('/coins/journal')   # дочерняя страница Coin → «Магазин мерча», группа раскрыта
        self.assertRegex(html, r'class="cc-nav-link active" href="/coins/shop" aria-current="page"')


class UiHelpersTest(CmsTestCase):
    """UX_SPEC_v2 §4.15: back_url, here, flash_action/pop_toasts, view_param, filter_state, time_range, частичный рендер."""

    def test_back_url_only_local(self):
        from cms.core import back_url
        for bad in ('//evil.example/x', 'http://evil.example/', 'https://x', 'javascript:alert(1)', '/\\evil'):
            with self.app.test_request_context('/news/add?next=' + bad):
                self.assertEqual(back_url('news'), '/news', bad)
        with self.app.test_request_context('/news/add?next=/news%3Fcategory%3Devents%26page%3D2'):
            self.assertEqual(back_url('news'), '/news?category=events&page=2')
        with self.app.test_request_context('/x', method='POST', data={'next': '/polls?status=active'}):
            self.assertEqual(back_url('news'), '/polls?status=active')

    def test_here_callable_and_str(self):
        from cms.core import here
        with self.app.test_request_context('/news?category=events'):
            self.assertEqual(here(), '/news?category=events')
            self.assertEqual(str(here), '/news?category=events')
        with self.app.test_request_context('/news'):
            self.assertEqual(here(), '/news')

    def test_flash_action_and_pop_toasts(self):
        r = self.post('/ui-kit', data={'demo': 'toast'})
        self.assertRedirectsTo(r, '/ui-kit')
        fl = self.flashes()
        self.assertIn(('success', 'Готово: статус изменён'), fl)   # обычный flash не ломает старые проверки
        self.assertEqual(fl[-1][0], 'cc-action')
        html = self.get_ok('/ui-kit')
        self.assertIn('data-has-action', html)
        self.assertRegex(html, r'<form method="post" action="/ui-kit" class="cc-toast__form">\s*<input type="hidden" name="csrf_token"')
        self.assertIn('class="cc-toast__action">Отменить</button>', html)
        self.assertNotIn('cc-action', html.split('id="ccToasts"')[1][:2000].replace('cc-toast__action', ''))
        # отмена — POST с CSRF
        r = self.post('/ui-kit', data={'demo': 'undo'})
        self.assertIn(('info', 'Действие отменено'), self.flashes())

    def test_pop_toasts_merges_and_rejects_unsafe(self):
        from flask import flash
        from cms.core import flash_action, pop_toasts
        with self.app.test_request_context('/'):
            flash_action('Новость опубликована', 'Открыть', href='/news/edit/x')
            flash_action('Опасно', 'Открыть', href='https://evil.example/')
            flash('Просто сообщение', 'error')
            toasts = pop_toasts()
        self.assertEqual([t['message'] for t in toasts], ['Новость опубликована', 'Опасно', 'Просто сообщение'])
        self.assertEqual(toasts[0]['action'], {'label': 'Открыть', 'href': '/news/edit/x'})
        self.assertIsNone(toasts[1]['action'])
        self.assertEqual(toasts[2]['category'], 'danger')

    def test_view_param(self):
        from cms.core import view_param
        with self.app.test_request_context('/x?view=cards'):
            self.assertEqual(view_param('t', ('table', 'cards'), 'table'), 'cards')
        with self.app.test_request_context('/x?view=evil', headers={'Cookie': 'cc_view_t=cards'}):
            self.assertEqual(view_param('t', ('table', 'cards'), 'table'), 'cards')
        with self.app.test_request_context('/x', headers={'Cookie': 'cc_view_t=bad'}):
            self.assertEqual(view_param('t', ('table', 'cards'), 'table'), 'table')

    def test_filter_state(self):
        from cms.core import filter_state
        with self.app.test_request_context('/news?q=день&category=events&page=2&status=new&photo='):
            st = filter_state(ignore=('status',), search_name='q')
        self.assertEqual(st['count'], 2)
        self.assertEqual(st['sheet_count'], 1)
        self.assertEqual(st['reset'], '/news?status=new')
        self.assertEqual(st['keep'], [('status', 'new')])

    def test_time_range_filter(self):
        from cms.core import fmt_time_range
        self.assertEqual(fmt_time_range('13.00-13.50'), '13:00–13:50')
        self.assertEqual(fmt_time_range('8.30 – 9.50'), '08:30–09:50')
        self.assertEqual(fmt_time_range('13:00-13:50'), '13:00–13:50')
        self.assertEqual(fmt_time_range('09.20'), '09:20')
        self.assertEqual(fmt_time_range('после обеда'), 'после обеда')
        self.assertEqual(fmt_time_range('25.00-26.00'), '25.00-26.00')
        self.assertEqual(fmt_time_range(None), '')
        with self.app.test_request_context('/'):
            from flask import render_template_string
            self.assertEqual(render_template_string("{{ '14.05-14.55'|time_range }}"), '14:05–14:55')

    def test_partial_render(self):
        html = self.client.get('/ui-kit', headers={'X-CC-Partial': '1'}).get_data(as_text=True)
        self.assertNotIn('cc-sidebar', html)
        self.assertNotIn('<!DOCTYPE', html)
        self.assertIn('cc-peek-fragment', html)
        self.assertIn('peek_status', html)          # {% block peek %} страницы
        full = self.get_ok('/news/edit/news_open_day')
        part = self.client.get('/news/edit/news_open_day', headers={'X-CC-Partial': '1'})
        self.assertEqual(part.status_code, 200)
        self.assertIn('X-CC-Partial', part.headers.get('Vary', ''))
        body = part.get_data(as_text=True)
        self.assertIn('<form', body)
        self.assertNotIn('cc-sidebar', body)
        self.assertIn('cc-sidebar', full)

    def test_partial_does_not_consume_flashes(self):
        self.post('/ui-kit', data={'demo': 'delete'})
        self.client.get('/ui-kit', headers={'X-CC-Partial': '1'})
        self.assertTrue(any(m.startswith('Демо: запись') for _c, m in self.flashes()))

    def test_ui_kit_contract_markers(self):
        html = self.get_ok('/ui-kit')
        for marker in ('cc-summary', 'cc-stat-strip', 'cc-filters--inline', 'cc-fsel', 'offcanvas-md', 'data-cc-search',
                       'data-cc-filter="#kitRows"', 'cc-table--stack', 'data-cc-select-all="kitBulk"', 'data-cc-select="kitBulk"',
                       'data-cc-bulkbar="kitBulk"', 'cc-row-menu', '"strategy":"fixed"', 'cc-segmented', 'data-cc-view="ui_kit"',
                       'cc-chips', 'cc-hint', 'data-cc-hint="kit-hint-demo"', 'cc-legend', 'cc-week-chip', 'cc-badge--role',
                       'cc-details', 'data-cc-collapse-key', 'cc-savebar', 'data-cc-save', 'formaction="/ui-kit?demo=delete"',
                       'cc-form-errors', 'cc-form-layout', 'data-cc-peek', 'cc-modal-form', 'data-cc-hotkey="n"',
                       'cc-kbd', 'cc-skel', 'Ничего не найдено по фильтрам', 'data-cc-back', 'cc-page-header__back',
                       'cc-page-header__meta', 'cc-header-menu', 'ccPeek', 'ccHotkeys', 'ccProgress', 'cc-create-btn'):
            self.assertIn(marker, html)
        self.assertNotIn('<h1><i class="bi', html)   # в h1 нет иконки

    def test_bulk_demo(self):
        r = self.post('/ui-kit', data={'demo': 'bulk', 'action': 'delete', 'ids': ['a', 'b']})
        self.assertRedirectsTo(r, '/ui-kit')
        self.assertIn(('success', 'Демо: действие «delete» для 2 записей'), self.flashes())


class DashboardV2Test(CmsTestCase):
    """UX_SPEC_v2 §3: приоритет расписания, абитуриенты — одной строкой внизу."""

    def test_dashboard_blocks_and_priorities(self):
        from unittest import mock
        from cms.core import cache
        cache.invalidate_prefix('home:')
        with mock.patch('cms.conflicts.summary', return_value={'critical': 8, 'warning': 3, 'teachers': 6}):
            html = self.get_ok('/')
        for text in ('Пары сегодня', 'Сегодня в колледже', 'Требует внимания', 'Ближайшее: опросы и события',
                     'Быстрые действия', 'Последние действия', 'Приём и Caspian Coin'):
            self.assertIn(text, html)
        self.assertNotIn('Регистрации абитуриентов', html)   # график с главной убран
        self.assertNotIn('cc-trend', html)                   # и тренд «−58%»
        self.assertNotIn('pp-attention', html)               # стили дашборда — в home.css
        self.assertIn('css/home.css', html)
        # порядок «Требует внимания»: накладки → обращения → предупреждения → заказы → лиды
        order = [html.index(t) for t in ('Критичные накладки в расписании', 'Обращения без ответа',
                                          'Предупреждения в расписании', 'Заказы мерча ждут выдачи', 'Новые лиды')]
        self.assertEqual(order, sorted(order))
        self.assertLess(html.index('Требует внимания'), html.index('Приём и Caspian Coin'))
        # KPI «Абитуриенты» нет — только приглушённая строка
        self.assertNotIn('cc-stat__label" title="Абитуриенты"', html)
        self.assertIn('Опрос', html)
        self.assertIn('Оцените работу столовой', html)       # активный опрос из сида
        self.assertIn('День открытых дверей', html)          # событие через 6 дней
        self.assertNotIn('Спартакиада колледжа', html)       # прошедшее событие
        self.assertIn('data-cc-hotkey="n"', html)

    def test_recent_feed_hides_logins(self):
        from cms.core import cache
        cache.invalidate_prefix('audit:')
        html = self.get_ok('/')
        feed = html.split('Последние действия', 1)[1]
        self.assertNotIn('>Вход<', feed)


class SearchApiTest(CmsTestCase):
    """UX_SPEC_v2 §2.5: /api/search — группы, преподаватели, аудитории, люди."""

    def _search(self, q):
        r = self.client.get('/api/search', query_string={'q': q})
        self.assertEqual(r.status_code, 200)
        return r.get_json()['results']

    def test_groups_teachers_rooms_people(self):
        res = self._search('ПО 24')
        labels = [r['label'] for r in res if r['type'] == 'group']
        self.assertIn('ПО 24-1', labels)
        self.assertIn('ПО 24-2', labels)
        self.assertTrue(all(r['section'] == 'Группы' for r in res if r['type'] == 'group'))
        self.assertIn('ПО 24-1', [r['label'] for r in self._search('по241') if r['type'] == 'group'])
        t = [r for r in self._search('петров') if r['type'] == 'teacher']
        self.assertEqual(t[0]['label'], 'Петров Дмитрий Игоревич')
        self.assertIn('/schedule/teachers?teacher=t_petrov', t[0]['url'])
        self.assertIn('/teachers/edit/t_petrov', t[0]['actions'][0]['url'])
        rooms = [r for r in self._search('204') if r['type'] == 'room']
        self.assertTrue(rooms and rooms[0]['label'] == '204', rooms)
        people = [r for r in self._search('мухамбетов') if r['type'] == 'user']
        self.assertEqual(people[0]['label'], 'Мухамбетов Ербол Кайратович')
        self.assertIn('/admin/users/edit/admin_2', people[0]['url'])

    def test_short_and_long_queries(self):
        self.assertEqual(self._search('п'), [])
        self.assertEqual(self._search('x' * 61), [])

    def test_anonymous_401(self):
        r = self.app.test_client().get('/api/search?q=ПО')
        self.assertEqual(r.status_code, 401)


class OpenRedirectTest(CmsTestCase):
    """SEC-1: next с управляющими символами/обратным слешем не уводит на чужой сайт и не даёт 500.
    Werkzeug выкидывает таб из Location, поэтому '/\\t/evil.example' превращался в '//evil.example'."""

    def _assert_local(self, r):
        from urllib.parse import urlsplit
        self.assertIn(r.status_code, (301, 302, 303))
        loc = r.headers.get('Location', '')
        self.assertFalse(loc.startswith('//'), loc)
        self.assertEqual(urlsplit(loc).netloc in ('', 'localhost'), True, loc)
        self.assertNotIn('evil', loc)

    def test_safe_next_url_rejects_control_chars_and_backslash(self):
        from cms.core import safe_next_url
        for bad in ('/\t/evil.com', '/\n/x', '/\r/x', '/\x00/x', '/ /evil', '/\\evil', '/a\\b', '//evil',
                    '/\x7f/x', '/\x0b/evil', 'https://evil.com', '\t//evil.com'):
            self.assertIsNone(safe_next_url(bad), repr(bad))
        # обычные пути (в т.ч. с пробелом в названии группы — так их отдаёт here()) — как есть
        for ok in ('/schedule/rooms', '/admin/users?role=student&q=x', '/schedule/edit_group/ПО 23-1'):
            self.assertEqual(safe_next_url(ok), ok)

    def test_logged_in_login_with_tab_next_stays_on_site(self):
        self._assert_local(self.client.get('/login?next=/%09/evil.example'))

    def test_crlf_next_is_not_500(self):
        for q in ('/%0d/x', '/%0a/x'):
            r = self.client.get('/login?next=' + q)
            self.assertNotEqual(r.status_code, 500, q)
            self._assert_local(r)

    def test_form_redirect_after_save_ignores_tab_next(self):
        r = self.post('/admin/users/st_001/revoke?next=/%09/evil.example')
        self._assert_local(r)

    def test_anonymous_login_post_with_tab_next(self):
        self.client = self.app.test_client()   # не вошедший: next из скрытого поля формы входа
        self.client.get('/login')
        r = self.client.post('/login', data={'email': S.ADMIN['email'], 'password': S.ADMIN['password'],
                                             'csrf_token': self.csrf(), 'next': '/\t/evil.example'})
        self._assert_local(r)


class SessionRevocationTest(CmsTestCase):
    """SEC-2: блокировка входа, «Завершить сеансы», выход и абсолютный срок 12 ч завершают сессию CMS."""

    _n = 0

    def _second_admin(self):
        """Второй админ (Auth + users) и его вошедший клиент."""
        from cms.core import db, fb_auth
        type(self)._n += 1
        uid = 'admin_sec_%d_%d' % (int(time.time()), self._n)
        email = '%s@caspian.test' % uid
        fb_auth.create_user(uid=uid, email=email, password='Second12345')
        db.collection('users').document(uid).set({'role': 'admin', 'name': 'Второй Админ', 'email': email})
        main = self.client
        self.client = self.app.test_client()
        try:
            self.login(email, 'Second12345')
            second = self.client
        finally:
            self.client = main
        return uid, second

    @staticmethod
    def _force_recheck(client):
        with client.session_transaction() as s:
            s['role_checked_at'] = time.time() - 11 * 60

    def _assert_locked_out(self, client):
        r = client.get('/admin/users')
        self.assertIn(r.status_code, (302, 401, 403), r.status_code)
        if r.status_code == 302:
            self.assertIn('/login', r.headers['Location'])
        self.assertNotEqual(client.get('/applicants/leads.csv').status_code, 200)
        with client.session_transaction() as s:
            self.assertIsNone(s.get('uid'), 'сессия не очищена')

    def test_disabled_admin_loses_cms_access(self):
        uid, second = self._second_admin()
        self.assertEqual(second.get('/admin/users').status_code, 200)
        r = self.post('/admin/users/%s/disable' % uid, {'disabled': '1'})
        self.assertEqual(r.status_code, 302)
        self._force_recheck(second)
        self._assert_locked_out(second)

    def test_revoked_sessions_end_cms_session(self):
        uid, second = self._second_admin()
        time.sleep(1.1)  # validSince в Auth — с точностью до секунды
        self.assertEqual(self.post('/admin/users/%s/revoke' % uid).status_code, 302)
        self._force_recheck(second)
        self._assert_locked_out(second)

    def test_absolute_session_lifetime(self):
        from cms.core import SESSION_HOURS
        with self.client.session_transaction() as s:
            s['login_at'] = time.time() - SESSION_HOURS * 3600 - 60
            s['role_checked_at'] = time.time()   # роль только что проверена — срок всё равно истёк
        r = self.client.get('/admin/users')
        self.assertRedirectsTo(r, '/login')
        with self.client.session_transaction() as s:
            self.assertIsNone(s.get('uid'))
        self.assertEqual(self.client.get('/api/schedule/all').status_code, 401)

    def test_session_without_login_at_is_expired(self):
        with self.client.session_transaction() as s:
            s.pop('login_at', None)
        self.assertRedirectsTo(self.client.get('/admin/users'), '/login')

    def test_logout_invalidates_copied_cookie(self):
        self.get_ok('/admin/users')
        stolen = self.client.get_cookie('cc_session').value
        self.logout()
        thief = self.app.test_client()
        thief.set_cookie('cc_session', stolen)
        self._force_recheck(thief)
        self._assert_locked_out(thief)

    def test_role_recheck_survives_auth_outage(self):
        from unittest import mock
        from cms import core
        self._force_recheck(self.client)
        with mock.patch.object(core.fb_auth, 'get_user', side_effect=RuntimeError('auth timeout')):
            self.get_ok('/admin/users')   # сбой сети — как сбой Firestore: отсрочка, не выход


class ClientIpTest(CmsTestCase):
    """SEC-3: лимиты считаются по X-Real-IP (его ставит прокси PythonAnywhere), а не по левому X-Forwarded-For."""
    login_as_admin = False

    def test_login_limit_not_bypassed_by_forwarded_for(self):
        self.client.get('/login')
        r = None
        for i in range(11):
            r = self.client.post('/login', data={'email': S.ADMIN['email'], 'password': 'bad-%d' % i,
                                                 'csrf_token': self.csrf()},
                                 headers={'X-Forwarded-For': '10.0.%d.%d' % (i, i), 'X-Real-IP': '203.0.113.7'})
        self.assertEqual(r.status_code, 429)

    def test_client_ip_prefers_real_ip(self):
        from cms.core import client_ip
        with self.app.test_request_context('/', headers={'X-Forwarded-For': '1.1.1.1, 203.0.113.9',
                                                         'X-Real-IP': '203.0.113.9'}):
            self.assertEqual(client_ip(), '203.0.113.9')
        with self.app.test_request_context('/', headers={'X-Forwarded-For': '1.1.1.1'},
                                           environ_base={'REMOTE_ADDR': '198.51.100.4'}):
            self.assertEqual(client_ip(), '198.51.100.4')

    def test_join_limit_not_bypassed_by_forwarded_for(self):
        from unittest import mock
        from cms import routes_public
        from cms.core import RateLimiter
        with mock.patch.object(routes_public, '_attempts', RateLimiter(2, 600)):
            r = None
            for i in range(3):
                self.client.get('/join')
                with self.client.session_transaction() as s:
                    s['join_ts'] = time.time() - 30
                r = self.client.post('/join', data={'login': 'Айгерим', 'password': 'secret12', 'password2': 'secret12',
                                                    'consent': '1', 'csrf_token': self.csrf()},
                                     headers={'X-Forwarded-For': '10.1.%d.1' % i, 'X-Real-IP': '203.0.113.8'})
            self.assertEqual(r.status_code, 429)


class CsvInjectionTest(CmsTestCase):
    """SEC-4: общий csv_response экранирует опасное начало ячейки, как выгрузка лидов."""
    fresh_seed_each_test = True

    def test_users_csv_escapes_formula(self):
        from cms.core import cache, db
        db.collection('users').document('ap_002').update({'name': '=1+2'})
        cache.invalidate_prefix('people:')
        body = self.client.get('/admin/users?role=applicant&format=csv').get_data(as_text=True)
        self.assertIn("'=1+2", body)
        self.assertNotIn(';=1+2', body)
        self.assertFalse(any(line.startswith('=1+2') for line in body.splitlines()))

    def test_feedback_csv_escapes_formula(self):
        from cms.core import cache, db
        db.collection('feedback').document('fb_0').update({'message': '=HYPERLINK("https://evil.example")'})
        cache.invalidate_prefix('content:')
        body = self.client.get('/feedback?status=all&format=csv').get_data(as_text=True)
        self.assertIn('\'=HYPERLINK', body)
        self.assertNotIn(';"=HYPERLINK', body)

    def test_csv_response_helper(self):
        from datetime import datetime, timezone
        from cms.core import csv_cell, csv_response
        with self.app.test_request_context('/'):
            r = csv_response('x.csv', ['a', 'b', 'c', 'd'], [['+7 701', '-1', 5, True],
                                                            ['@SUM(A1)', '\tx', -3, datetime(2026, 1, 1, tzinfo=timezone.utc)],
                                                            [csv_cell('=1'), '', None, 'обычный']])
        lines = r.get_data(as_text=True).lstrip('﻿').splitlines()
        self.assertEqual(lines[1], "'+7 701;'-1;5;True")
        self.assertTrue(lines[2].startswith("'@SUM(A1);'\tx;-3;01.01.2026"), lines[2])
        self.assertEqual(lines[3], "'=1;;;обычный")    # уже экранированное — без второго апострофа


class LogoutCsrfTest(CmsTestCase):
    """SEC-5: выход только POST с CSRF; GET /logout не разлогинивает (logout-CSRF с чужого сайта)."""

    def test_get_logout_does_not_clear_session(self):
        r = self.client.get('/logout', headers={'Referer': 'https://evil.example/'})
        self.assertNotEqual(r.status_code, 500)
        with self.client.session_transaction() as s:
            self.assertEqual(s.get('uid'), S.ADMIN['uid'])
        self.get_ok('/admin/users')

    def test_post_logout_requires_csrf(self):
        r = self.client.post('/logout')
        self.assertEqual(r.status_code, 400)
        self.get_ok('/admin/users')
        r = self.logout()
        self.assertRedirectsTo(r, '/login')
        self.assertRedirectsTo(self.client.get('/admin/users'), '/login')


class CacheHeadersTest(CmsTestCase):
    """SEC-6: страницы админки и CSV с ПДн — Cache-Control: no-store; статика и публичные страницы — без него."""

    def test_admin_pages_and_csv_not_stored(self):
        for url in ('/applicants/leads', '/applicants/leads.csv', '/admin/users'):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertIn('no-store', r.headers.get('Cache-Control', ''), url)

    def test_static_and_anonymous_pages_cacheable(self):
        r = self.client.get('/static/css/app.css')
        self.assertNotIn('no-store', r.headers.get('Cache-Control', '') or '')
        anon = self.app.test_client()
        r = anon.get('/join')
        self.assertNotIn('no-store', r.headers.get('Cache-Control', '') or '')

    def test_hsts_only_with_secure_cookies(self):
        r = self.client.get('/admin/users')
        self.assertIsNone(r.headers.get('Strict-Transport-Security'))   # эмулятор: cookie без Secure
        self.app.config['SESSION_COOKIE_SECURE'] = True
        try:
            r = self.app.test_client().get('/login')
            self.assertIn('max-age=', r.headers.get('Strict-Transport-Security', ''))
        finally:
            self.app.config['SESSION_COOKIE_SECURE'] = False


class CacheRaceTest(CmsTestCase):
    """DATA-3: значение, посчитанное до записи, не кэшируется на весь TTL, если сброс пришёл во время расчёта."""
    login_as_admin = False

    def test_invalidate_during_factory_drops_value(self):
        import tempfile
        from cms.core import CacheManager
        c = CacheManager(shared_dir=tempfile.mkdtemp())

        def load():
            data = 'snapshot-before-write'       # прочитали Firestore
            c.invalidate_prefix('schedule:')     # в это время запись сбросила кэш
            return data
        self.assertEqual(c.get_or_set('schedule:snapshot', load, ttl=120), 'snapshot-before-write')
        self.assertIsNone(c.get('schedule:snapshot'), 'устаревший снапшот закэширован на весь TTL')

    def test_invalidate_by_other_worker_during_factory(self):
        import tempfile
        from cms.core import CacheManager
        d = tempfile.mkdtemp()
        a, b = CacheManager(shared_dir=d), CacheManager(shared_dir=d)

        def slow_load():
            time.sleep(0.05)
            a.invalidate_prefix('schedule:')     # воркер A записал расписание
            time.sleep(0.05)
            return 'stale'
        b.get_or_set('schedule:snapshot', slow_load, ttl=120)
        time.sleep(1.1)
        self.assertIsNone(b.get('schedule:snapshot'))
        b.get_or_set('schedule:snapshot', lambda: 'fresh', ttl=120)
        self.assertEqual(b.get('schedule:snapshot'), 'fresh')


class PreciseInvalidateTest(CmsTestCase):
    """PERF-2: invalidate(key) сбрасывает ключ и key:*, а не всё пространство имён — и в своём процессе,
    и в других воркерах; invalidate_prefix('ns:') по-прежнему сбрасывает всё пространство."""
    login_as_admin = False

    def test_same_process_keeps_siblings(self):
        import tempfile
        from cms.core import CacheManager
        c = CacheManager(60, tempfile.mkdtemp())
        c.set('coins:people', 1)
        c.set('coins:orders', 2)
        c.set('coins:orders:x', 3)
        c.invalidate('coins:orders')
        self.assertEqual(c.get('coins:people'), 1)
        self.assertIsNone(c.get('coins:orders'))
        self.assertIsNone(c.get('coins:orders:x'))

    def test_other_worker_drops_only_invalidated_keys(self):
        import tempfile
        from cms.core import CacheManager
        d = tempfile.mkdtemp()
        a, b = CacheManager(60, d), CacheManager(60, d)
        for k, v in (('coins:people', 1), ('coins:orders', 2), ('coins:orders:x', 3), ('schedule:snapshot', 4),
                     ('schedule:logs', 5), ('all_teachers', 6), ('all_teachers:dir', 7), ('people:users', 8)):
            b.set(k, v)
        a.invalidate('coins:orders')
        a.invalidate_prefix('schedule:')
        a.invalidate('all_teachers')
        time.sleep(1.1)  # b перечитывает штампы не чаще раза в секунду
        self.assertEqual(b.get('coins:people'), 1)
        self.assertEqual(b.get('people:users'), 8)
        for k in ('coins:orders', 'coins:orders:x', 'schedule:snapshot', 'schedule:logs', 'all_teachers',
                  'all_teachers:dir'):
            self.assertIsNone(b.get(k), k)
        a.invalidate()
        b.set('x:y', 1)
        time.sleep(1.1)
        self.assertEqual(b.get('x:y'), 1)        # записано после полного сброса — живо

    def test_issue_order_keeps_unrelated_snapshots(self):
        from cms.core import cache
        self.login()
        self.get_ok('/coins/orders')
        self.get_ok('/applicants/leads')
        self.assertIsNotNone(cache.get('coins:people'))
        self.assertIsNotNone(cache.get('applicants:snapshot'))
        r = self.post('/coins/orders/ord_001/issue', {'note': ''})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(cache.get('coins:orders'))
        self.assertIsNotNone(cache.get('coins:people'), 'выдача заказа сбросила каталог абитуриентов')
        self.assertIsNotNone(cache.get('applicants:snapshot'), 'выдача заказа сбросила снапшот абитуриентов')


class TopicVariantsTest(CmsTestCase):
    """C5: группа с «ъ/ь» — CMS шлёт сводный пуш и в топик функций (ъ/ь → '_'), и в тот, на который
    подписано приложение (NotificationService._sanitize выбрасывает ъ/ь)."""
    login_as_admin = False

    def test_topic_variants(self):
        from cms.core import topic_variants
        self.assertEqual(topic_variants('schedule_group_Связь-24'),
                         ['schedule_group_svyaz_-24', 'schedule_group_svyaz-24'])
        self.assertEqual(topic_variants('schedule_group_ПО 24-1'), ['schedule_group_po_24-1'])

    def test_schedule_batch_pushes_both_topics(self):
        from cms import core
        from cms.routes_schedule import ScheduleBatch
        core.sent_pushes.clear()
        with self.app.test_request_context('/'):
            batch = ScheduleBatch(quiet=True)
            batch.add({'groupId': 'Связь-24', 'day_index': 1, 'date': '2026-10-20', 'time': '08.00-08.50',
                       'subject': 'Физика', 'teacher': 't_li', 'room': '101', 'week_type': 'always'})
            batch.commit()
        topics = [p['topic'] for p in core.sent_pushes]
        self.assertIn('schedule_group_svyaz_-24', topics)
        self.assertIn('schedule_group_svyaz-24', topics)


class FirestoreTimeoutTest(CmsTestCase):
    """PERF-1: вызовы Firestore ограничены по времени (retry + timeout вместо дефолтных 300 с google-api-core),
    а после сбоя связи предохранитель FS_COOLDOWN секунд сразу отдаёт 503 — воркер не висит на каждом запросе."""

    def setUp(self):
        super().setUp()
        from unittest import mock
        from google.api_core import retry as gretry
        from cms import core
        self.core = core
        fast = gretry.Retry(predicate=gretry.if_transient_error, initial=0.1, maximum=0.5, timeout=2)
        self.patches = [mock.patch.object(core, 'FS_RETRY', fast), mock.patch.object(core, 'FS_TIMEOUT', 2),
                        mock.patch.object(core, 'FS_TIMEOUT_LONG', 3)]
        for p in self.patches:
            p.start()
        core.reset_firestore_state()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.core.reset_firestore_state()
        super().tearDown()

    @staticmethod
    def _dead_client():
        """Клиент Firestore на закрытый порт — как недоступная сеть."""
        import os
        from unittest import mock
        from google.auth.credentials import AnonymousCredentials
        from google.cloud import firestore as gfs
        with mock.patch.dict(os.environ, {'FIRESTORE_EMULATOR_HOST': '127.0.0.1:59999'}):
            return gfs.Client(project='demo-dead', credentials=AnonymousCredentials())

    def test_helpers_fail_fast_and_trip_breaker(self):
        from unittest import mock
        dead = self._dead_client()
        started = time.time()
        with mock.patch.object(self.core, 'db', dead):
            self.assertIsNone(self.core.count(dead.collection('users')))
            self.assertEqual(self.core.get_docs('users', ['a', 'b']), {})
        self.assertLess(time.time() - started, 20)
        self.assertFalse(self.core.firestore_available(), 'после сбоя связи предохранитель не сработал')

    def test_open_breaker_gives_fast_503_and_recovers(self):
        from google.api_core import exceptions as gexc
        self.core.note_firestore_error(gexc.ServiceUnavailable('нет связи'))
        started = time.time()
        r = self.client.get('/admin/users')
        self.assertEqual(r.status_code, 503)
        self.assertLess(time.time() - started, 3)
        self.assertEqual(self.client.get('/static/css/app.css').status_code, 200)
        self.core.reset_firestore_state()
        self.get_ok('/admin/users')

    def test_logout_works_during_outage(self):
        from google.api_core import exceptions as gexc
        self.core.note_firestore_error(gexc.DeadlineExceeded('таймаут'))
        r = self.logout()
        self.assertRedirectsTo(r, '/login')
        with self.client.session_transaction() as s:
            self.assertIsNone(s.get('uid'))

    def test_role_recheck_during_outage_uses_grace_quickly(self):
        from unittest import mock
        dead = self._dead_client()
        with self.client.session_transaction() as s:
            s['role_checked_at'] = time.time() - 11 * 60
        started = time.time()
        with mock.patch.object(self.core, 'db', dead):
            r = self.client.get('/ui-kit')
        self.assertEqual(r.status_code, 200)               # отсрочка ROLE_OUTAGE_GRACE_SECONDS
        self.assertLess(time.time() - started, 20)

    def test_firestore_calls_get_retry_and_timeout(self):
        from unittest import mock
        calls = []

        class Agg:
            def get(self, **kw):
                calls.append(kw)
                raise RuntimeError('нет индекса')

        class Q:
            def count(self, alias=None):
                return Agg()

            def sum(self, field, alias=None):
                return Agg()
        self.core.count(Q())
        self.core.aggregate_sum(Q(), 'price')
        self.assertEqual(len(calls), 2)
        for kw in calls:
            self.assertIs(kw.get('retry'), self.core.FS_RETRY)
            self.assertTrue(0 < kw.get('timeout') <= 60)
        self.assertTrue(self.core.firestore_available())   # обычная ошибка (не связь) предохранитель не трогает
        self.assertTrue(mock)


class SearchBeyond5000Test(CmsTestCase):
    """PERF-6: Ctrl+K ищет людей по общему снапшоту «Людей» без лимита: пользователь, чей uid в порядке
    сортировки дальше 5000-го, находится, а у такого преподавателя есть действие «Карточка»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from cms.core import chunked, db
        ids = ['aaa_fill_%04d' % i for i in range(5000)]
        for part in chunked(ids, 450):
            b = db.batch()
            for uid in part:
                b.set(db.collection('users').document(uid), {'role': 'student', 'name': 'Заполнитель', 'group': 'ПО 23-1'})
            b.commit()
        db.collection('users').document('zzz_late_student').set({'role': 'student', 'name': 'Поздняков Тимур',
                                                                  'group': 'ПО 24-1'})
        db.collection('users').document('zzz_late_teacher').set({'role': 'teacher', 'name': 'Яковлев Пётр Ильич'})

    def _search(self, q):
        r = self.client.get('/api/search', query_string={'q': q})
        self.assertEqual(r.status_code, 200)
        return r.get_json()['results']

    def test_late_uid_user_found(self):
        people = [r for r in self._search('Поздняков') if r['type'] == 'user']
        self.assertTrue(people and 'zzz_late_student' in people[0]['url'], people)

    def test_late_uid_teacher_has_card_action(self):
        t = [r for r in self._search('Яковлев') if r['type'] == 'teacher']
        self.assertTrue(t, 'преподаватель не найден')
        self.assertIn('/teachers/edit/zzz_late_teacher', ' '.join(a['url'] for a in t[0]['actions']))
