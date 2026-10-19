# tests/base.py — базовый класс тестов CMS на эмуляторах (FOUNDATION).
#
#   from tests.base import CmsTestCase
#
#   class NewsTest(CmsTestCase):
#       def test_list(self):
#           html = self.get_ok('/news')
#           self.assertIn('День открытых дверей', html)
#
#       def test_delete(self):
#           r = self.post('/news/delete/news_schedule')            # CSRF-токен подставится сам
#           self.assertEqual(r.status_code, 302)
#           self.assertIsNone(self.doc('news', 'news_schedule'))
#
# Каждый класс получает свежие данные (seed) — тесты разных модулей не мешают друг другу.
# Внутри класса данные общие: тест, который их меняет, должен это учитывать
# (или класс ставит fresh_seed_each_test = True — медленнее).

import os
import re
import unittest

if not os.environ.get('FIRESTORE_EMULATOR_HOST'):
    raise unittest.SkipTest('Тесты CMS запускаются только на эмуляторах: tests/run_tests.sh PORT_BASE')

from tests import seed as seed_module  # noqa: E402

_APP = None


def get_app():
    """Flask-приложение (один экземпляр на процесс тестов)."""
    global _APP
    if _APP is None:
        from flask_app import app
        app.config['TESTING'] = True
        _APP = app
    return _APP


class CmsTestCase(unittest.TestCase):
    """Тест с настоящим входом админа через /login, CSRF-хелперами и чистыми данными эмулятора.

    Атрибуты класса:
      seed            — dict из tests.seed.seed_all() (id групп, абитуриентов, товаров…)
      login_as_admin  — входить админом в setUp (False — анонимный клиент)
      fresh_seed_each_test — пересевать данные перед КАЖДЫМ тестом (медленно, по умолчанию False)
    """

    login_as_admin = True
    fresh_seed_each_test = False
    seed = None

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.app = get_app()
        cls.seed = seed_module.reset_and_seed()
        cls._reset_process_state()

    @classmethod
    def _reset_process_state(cls):
        """Сбросить кэш CMS и лимитеры (они живут в процессе между тестами)."""
        from cms import core
        core.cache.invalidate()
        try:
            from cms import routes_auth
            routes_auth._login_limiter._hits.clear()
        except Exception:
            pass

    def setUp(self):
        super().setUp()
        if self.fresh_seed_each_test:
            type(self).seed = seed_module.reset_and_seed()
        self._reset_process_state()
        self.client = self.app.test_client()
        if self.login_as_admin:
            self.login()

    # ---------- вход / CSRF ----------
    def login(self, email=None, password=None, expect_ok=True):
        """Настоящий вход через форму /login (Auth-эмулятор). → ответ POST."""
        email = email or seed_module.ADMIN['email']
        password = password or seed_module.ADMIN['password']
        self.client.get('/login')
        r = self.client.post('/login', data={'email': email, 'password': password, 'csrf_token': self.csrf()})
        if expect_ok:
            self.assertEqual(r.status_code, 302, 'вход админом не удался: %s' % r.get_data(as_text=True)[:500])
        return r

    def logout(self):
        return self.post('/logout')

    def csrf(self):
        """CSRF-токен текущей сессии тест-клиента (создаёт сессию при необходимости)."""
        import secrets
        with self.client.session_transaction() as s:
            tok = s.get('_csrf')
            if not tok:  # после входа сессия новая — кладём токен так же, как csrf_token()
                tok = s['_csrf'] = secrets.token_urlsafe(32)
        return tok

    # ---------- запросы ----------
    def post(self, url, data=None, **kw):
        """POST формы с CSRF-токеном. data — dict (можно с файлами: (BytesIO, 'name.png'))."""
        data = dict(data or {})
        data.setdefault('csrf_token', self.csrf())
        return self.client.post(url, data=data, **kw)

    def post_json(self, url, payload=None, **kw):
        """POST JSON с заголовком X-CSRF-Token (как fetch из app.js)."""
        headers = dict(kw.pop('headers', {}) or {})
        headers['X-CSRF-Token'] = self.csrf()
        return self.client.post(url, json=payload or {}, headers=headers, **kw)

    def get_ok(self, url, status=200):
        """GET и проверка статуса → текст ответа."""
        r = self.client.get(url)
        self.assertEqual(r.status_code, status, '%s → %s: %s' % (url, r.status_code, r.get_data(as_text=True)[:800]))
        return r.get_data(as_text=True)

    def assertRedirectsTo(self, response, path_prefix):
        self.assertIn(response.status_code, (301, 302, 303, 307, 308))
        loc = response.headers.get('Location', '')
        self.assertTrue(re.sub(r'^https?://[^/]+', '', loc).startswith(path_prefix), loc)

    def flashes(self):
        """Flash-сообщения в сессии (до их показа): [(категория, текст)]."""
        with self.client.session_transaction() as s:
            return list(s.get('_flashes', []))

    # ---------- данные ----------
    def doc(self, collection, doc_id):
        """Документ Firestore как dict (без id) или None."""
        from cms.core import db
        snap = db.collection(collection).document(doc_id).get()
        return snap.to_dict() if snap.exists else None

    def docs(self, collection, **where):
        """[{id, ...}] документов коллекции (фильтры field=value, равенство)."""
        from cms.core import db, where as _where
        q = db.collection(collection)
        for k, v in where.items():
            q = _where(q, k, '==', v)
        return [dict(d.to_dict() or {}, id=d.id) for d in q.stream()]

    def audit_entries(self, action=None):
        from cms.core import db
        out = [d.to_dict() for d in db.collection('cms_audit').stream()]
        return [e for e in out if action is None or e.get('action') == action]
