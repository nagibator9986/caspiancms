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
