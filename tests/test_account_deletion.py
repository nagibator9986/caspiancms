# tests/test_account_deletion.py — публичная страница «Удаление аккаунта и данных» (ссылка для Google Play).

from cms import routes_public
from tests.base import CmsTestCase


class AccountDeletionPageTest(CmsTestCase):
    login_as_admin = False                       # страница открыта без входа

    def test_public_ru(self):
        html = self.get_ok('/account-deletion')
        self.assertIn('Удаление аккаунта и данных', html)
        self.assertIn('Профиль → «Удалить профиль»', html)
        self.assertIn('Профиль → «Удалить аккаунт»', html)
        self.assertIn('в течение %d дней' % routes_public.DELETION_DAYS, html)
        self.assertNotIn('{days}', html)

    def test_kazakh(self):
        html = self.get_ok('/account-deletion?lang=kk')
        self.assertIn('Аккаунт пен деректерді жою', html)
        self.assertIn('Профильді жою', html)
        self.assertNotIn('{days}', html)
        self.assertIn('қосымшаға кіру (аккаунт)', html)

    def test_contacts_come_from_admissions_settings(self):
        from cms import core
        core.db.collection('app_config').document('admissions').set(
            {'phone': '+7 701 000 11 22', 'email': 'priem@caspian.test', 'address': 'г. Алматы, ул. Тестовая, 1'}, merge=True)
        core.cache.invalidate()
        html = self.get_ok('/account-deletion')
        self.assertIn('priem@caspian.test', html)
        self.assertIn('mailto:priem@caspian.test', html)
        self.assertIn('Тестовая, 1', html)
        self.assertIn('tel:', html)

    def test_no_session_needed_and_no_admin_chrome(self):
        html = self.get_ok('/account-deletion')
        self.assertNotIn('cc-sidebar', html)
