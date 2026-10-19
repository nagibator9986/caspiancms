# tests/test_routes_contract.py — контракт имён endpoint'ов (FOUNDATION).
#
# Шаблоны и сайдбар ссылаются на endpoint'ы через url_for/nav_url: переименование ломает
# ссылки молча (nav_url вернёт '#'). Тест фиксирует имена старых endpoint'ов (кроме тех, что
# модулям разрешено убрать) и правила новых разделов SPEC §6.1. Запускается в tests/run_tests.sh
# (без эмулятора пропускается: сборка приложения вне эмулятора читала бы прод-ключ).

import os
import unittest

# Старые endpoint'ы flask_app.py, на которые ссылаются шаблоны/сайдбар — имена сохраняются.
LEGACY_ENDPOINTS = [
    'admin_add_user', 'admin_delete_user', 'admin_edit_user', 'admin_reset_password', 'admin_users', 'administration',
    'administration_add', 'administration_delete', 'administration_edit', 'administration_update_order', 'club_delete',
    'club_form', 'club_registrations', 'clubs', 'dashboard', 'delete_club_registration', 'delete_event_registration',
    'delete_feedback', 'event_delete', 'event_form', 'event_registrations', 'events', 'feedback_list', 'feedback_reply',
    'group_create', 'group_delete', 'group_rename', 'groups', 'login', 'logout', 'news', 'news_delete', 'news_form',
    'poll_add_answer', 'poll_delete', 'poll_delete_answer', 'poll_form', 'poll_results', 'polls', 'product_delete',
    'product_form', 'products', 'schedule', 'schedule_all', 'schedule_all_export', 'schedule_builder',
    'schedule_delete_group', 'schedule_delete_item', 'schedule_edit', 'schedule_edit_group', 'schedule_logs',
    'schedule_rooms', 'schedule_rooms_export', 'schedule_teachers', 'schedule_teachers_export', 'schedule_update_group',
    'subject_add', 'subject_delete', 'subject_edit', 'subjects_list', 'teacher_add', 'teacher_delete', 'teacher_edit',
    'teachers_list',
]
# Модулям можно убрать/заменить: неиспользуемые /api/* и публичное веб-голосование.
OPTIONAL_LEGACY = ['active_polls', 'poll_vote', 'poll_submit', 'api_get_group_schedule', 'api_get_room_schedule',
                   'api_get_teacher_schedule', 'api_invalidate_cache', 'api_schedule_all']

# Новые разделы (SPEC §6.1/§6.3): endpoint → правило
NEW_ENDPOINTS = {
    'applicants_dashboard': '/applicants',
    'applicants_leads': '/applicants/leads',
    'applicant_detail': '/applicants/<uid>',
    'applicant_sources': '/applicants/sources',
    'applicant_news': '/applicants/news',
    'admissions_settings': '/admissions',
    'public_join': '/join',
    'coins_shop': '/coins/shop',
    'coins_orders': '/coins/orders',
    'coins_campaigns': '/coins/campaigns',
    'coins_journal': '/coins/journal',
    'schedule_conflicts': '/schedule/conflicts',
    'schedule_free_rooms': '/schedule/free-rooms',
    'audit_log': '/audit',
    'ui_kit': '/ui-kit',
}


def _app():
    from flask_app import app
    return app


@unittest.skipUnless(os.environ.get('FIRESTORE_EMULATOR_HOST'), 'только на эмуляторе (tests/run_tests.sh)')
class RoutesContractTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = _app()
        cls.rules = {}
        for r in cls.app.url_map.iter_rules():
            cls.rules.setdefault(r.endpoint, set()).add(r.rule)

    def test_legacy_endpoints_kept(self):
        missing = [e for e in LEGACY_ENDPOINTS if e not in self.rules]
        self.assertEqual(missing, [], 'endpoint\'ы старых страниц переименованы/удалены')

    def test_new_sections(self):
        for endpoint, rule in NEW_ENDPOINTS.items():
            with self.subTest(endpoint=endpoint):
                self.assertIn(endpoint, self.rules)
                self.assertIn(rule, self.rules[endpoint])
        self.assertIn('/join/<code>', self.rules['public_join'])

    def test_sidebar_links_resolve(self):
        from cms.core import NAV_SECTIONS
        for _title, _icon, items in NAV_SECTIONS:
            for endpoint, label, _ic, _kw in items:
                with self.subTest(item=label):
                    self.assertIn(endpoint, self.rules)


if __name__ == '__main__':
    unittest.main()
