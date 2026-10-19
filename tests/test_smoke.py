# tests/test_smoke.py — дымовой тест всех страниц (FOUNDATION).
#
# Каждый GET-маршрут без параметров отдаёт 200/302 без исключений (под админом), а анонима
# админские страницы отправляют на /login. Маршруты с параметрами проверяются на примерах
# из сида (PARAM_SAMPLES), если такой endpoint есть.

from tests.base import CmsTestCase

# Публичные (без входа) endpoint'ы
PUBLIC_ENDPOINTS = {'login', 'logout', 'static', 'public_join', 'active_polls', 'poll_vote', 'poll_results'}

# endpoint → kwargs для url_for (значения из tests/seed.py)
PARAM_SAMPLES = {
    'schedule_edit': {'schedule_id': 'base_00_1_0'},
    'schedule_edit_group': {'group_id': 'ПО 24-1'},
    'api_get_group_schedule': {'group_id': 'ПО 24-1'},
    'api_get_teacher_schedule': {'teacher_id': 't_petrov'},
    'api_get_room_schedule': {'room_id': '204'},
    'club_form': {'club_id': 'club_robotics'},
    'club_registrations': {'club_id': 'club_robotics'},
    'event_form': {'event_id': 'ev_openday'},
    'event_registrations': {'event_id': 'ev_openday'},
    'news_form': {'news_id': 'news_open_day'},
    'poll_form': {'poll_id': 'poll_canteen'},
    'poll_results': {'poll_id': 'poll_canteen'},
    'product_form': {'product_id': 'p_badge'},
    'teacher_edit': {'uid': 't_petrov'},
    'administration_edit': {'admin_id': 'mg_0'},
    'applicant_detail': {'uid': 'ap_001'},
    'public_join': {'code': 'STAND1'},
}


class SmokeTest(CmsTestCase):

    def _get_rules(self):
        rules = []
        for rule in self.app.url_map.iter_rules():
            if 'GET' not in rule.methods or rule.endpoint == 'static':
                continue
            rules.append(rule)
        return rules

    def test_get_routes_without_params(self):
        checked = 0
        for rule in self._get_rules():
            if rule.arguments - set(rule.defaults or {}):
                continue
            url = rule.rule
            with self.subTest(url=url, endpoint=rule.endpoint):
                if rule.endpoint == 'logout':
                    continue  # выход разлогинит клиента — проверяется отдельно
                r = self.client.get(url)
                self.assertIn(r.status_code, (200, 302), '%s → %s\n%s' % (url, r.status_code, r.get_data(as_text=True)[:1500]))
                checked += 1
        self.assertGreater(checked, 30)

    def test_get_routes_with_params(self):
        from flask import url_for
        endpoints = {r.endpoint for r in self._get_rules()}
        for endpoint, kwargs in PARAM_SAMPLES.items():
            if endpoint not in endpoints:
                continue
            with self.app.test_request_context():
                url = url_for(endpoint, **kwargs)
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertIn(r.status_code, (200, 302), '%s → %s\n%s' % (url, r.status_code, r.get_data(as_text=True)[:1500]))

    def test_admin_pages_redirect_anonymous_to_login(self):
        anon = self.app.test_client()
        for rule in self._get_rules():
            if rule.arguments or rule.endpoint in PUBLIC_ENDPOINTS:
                continue
            with self.subTest(url=rule.rule):
                r = anon.get(rule.rule)
                if rule.rule.startswith('/api/'):
                    self.assertEqual(r.status_code, 401, rule.rule)
                else:
                    self.assertEqual(r.status_code, 302, rule.rule)
                    self.assertIn('/login', r.headers.get('Location', ''))

    def test_layout_and_sidebar(self):
        html = self.get_ok('/schedule/rooms')
        self.assertIn('cc-sidebar', html)
        self.assertIn('name="csrf-token"', html)
        self.assertIn('bootstrap@5.3.3', html)
        self.assertIn('Аудиторный фонд', html)
        self.assertIn('aria-current="page"', html)
        for label in ('Аналитика', 'Лиды', 'QR-источники', 'Магазин', 'Заказы', 'Накладки', 'Аудит', 'Обратная связь'):
            self.assertIn(label, html)

    def test_sidebar_badges(self):
        # В сиде: 3 заказа reserved и 3 новых обращения
        html = self.get_ok('/audit')
        self.assertRegex(html, r'Заказы</span>\s*<span class="cc-nav-badge[^"]*">3<')
        self.assertRegex(html, r'Обратная связь</span>\s*<span class="cc-nav-badge[^"]*">3<')

    def test_public_join_page(self):
        anon = self.app.test_client()
        for url in ('/join', '/join/STAND1', '/join/STAND1?lang=kk'):
            r = anon.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertNotIn('cc-sidebar', r.get_data(as_text=True))

    def test_404_page(self):
        r = self.client.get('/no-such-page')
        self.assertEqual(r.status_code, 404)
        self.assertIn('Страница не найдена', r.get_data(as_text=True))

    def test_logout(self):
        r = self.logout()
        self.assertRedirectsTo(r, '/login')
        self.assertRedirectsTo(self.client.get('/'), '/login')
