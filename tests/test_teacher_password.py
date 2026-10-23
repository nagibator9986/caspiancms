# tests/test_teacher_password.py — обязательная смена пароля УЧИТЕЛЯ в приложении (пакет «P»).
#
# Контракт с приложением (как у абитуриента, D5): когда админ выдаёт учителю пароль — при создании аккаунта или
# при сбросе на Teacher123 — CMS ставит users/{uid}.mustChangePassword = true (+ passwordResetAt) и завершает
# сеансы (revoke_refresh_tokens); приложение при входе показывает экран «Придумайте новый пароль», а флаг снимает
# сам владелец (правила Firestore разрешают только false).
# Студенты, клубы и админы НЕ меняются: им флаг не ставится.

from unittest import mock

from firebase_admin import auth as fb_auth_module

from tests.base import CmsTestCase


def _sign_in_ok(email, password):
    """True, если Auth-эмулятор пускает с этим паролем."""
    from cms.core import LoginError, sign_in_with_password
    try:
        sign_in_with_password(email, password)
        return True
    except LoginError:
        return False


def _revoke_spy():
    return mock.patch.object(fb_auth_module, 'revoke_refresh_tokens', wraps=fb_auth_module.revoke_refresh_tokens)


class TeacherResetPasswordTest(CmsTestCase):
    """Сброс пароля на Teacher123 из «Пользователей» (и со страницы преподавателя — тот же маршрут)."""

    fresh_seed_each_test = True

    def test_reset_teacher_sets_flag_and_revokes_sessions(self):
        from cms.core import fb_auth
        fb_auth.update_user('t_akhmetova', password='Other999')
        with _revoke_spy() as revoke:
            r = self.post('/admin/users/reset-password/t_akhmetova')
        self.assertEqual(r.status_code, 302)
        revoke.assert_called_once_with('t_akhmetova')
        self.assertTrue(_sign_in_ok('akhmetova@caspian.test', 'Teacher123'))
        self.assertFalse(_sign_in_ok('akhmetova@caspian.test', 'Other999'))
        d = self.doc('users', 't_akhmetova')
        self.assertIs(d.get('mustChangePassword'), True)
        self.assertIsNotNone(d.get('passwordResetAt'))
        self.assertIsNotNone(d.get('updatedAt'))
        self.assertEqual(d['role'], 'teacher')

    def test_reset_teacher_flash_says_app_will_ask_new_password(self):
        r = self.post('/admin/users/reset-password/t_akhmetova')
        self.assertEqual(r.status_code, 302)
        flashes = self.flashes()
        self.assertEqual(flashes[-1][0], 'success')
        text = flashes[-1][1]
        self.assertIn('Teacher123', text)
        self.assertIn('при входе приложение попросит придумать новый пароль', text)

    def test_reset_teacher_audited_without_crm_history(self):
        self.post('/admin/users/reset-password/t_akhmetova')
        entries = [e for e in self.audit_entries('reset_password') if e.get('entityId') == 't_akhmetova']
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['details'].get('role'), 'teacher')
        self.assertIsNone(self.doc('applicant_crm', 't_akhmetova'))   # CRM — только у абитуриентов

    def test_reset_teacher_revoke_failure_does_not_block(self):
        # сбой отзыва сеансов логируется, но пароль и флаг уже должны быть выставлены
        with mock.patch.object(fb_auth_module, 'revoke_refresh_tokens', side_effect=RuntimeError('boom')):
            r = self.post('/admin/users/reset-password/t_akhmetova')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(_sign_in_ok('akhmetova@caspian.test', 'Teacher123'))
        self.assertIs(self.doc('users', 't_akhmetova').get('mustChangePassword'), True)

    def test_reset_student_and_club_do_not_set_flag(self):
        from cms.core import fb_auth
        # в сиде у студентов и клуба нет входа — заводим, как у реальных пользователей
        fb_auth.create_user(uid='st_002', email='student02@caspian.test', password='Other999')
        fb_auth.create_user(uid='club_robotics', email='robotics@caspian.test', password='Other999')
        for uid, email in (('st_002', 'student02@caspian.test'), ('club_robotics', 'robotics@caspian.test')):
            with self.subTest(uid=uid), _revoke_spy() as revoke:
                r = self.post('/admin/users/reset-password/%s' % uid)
                self.assertEqual(r.status_code, 302)
                revoke.assert_not_called()
                self.assertTrue(_sign_in_ok(email, 'Student123'))
                d = self.doc('users', uid)
                self.assertNotIn('mustChangePassword', d)
                self.assertNotIn('passwordResetAt', d)
                self.assertNotIn('приложение попросит', ' '.join(m for _c, m in self.flashes()))

    def test_reset_admin_still_refused(self):
        r = self.post('/admin/users/reset-password/admin_2')
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('mustChangePassword', self.doc('users', 'admin_2'))

    def test_teacher_waits_for_change_badge_in_users_list(self):
        self.post('/admin/users/reset-password/t_akhmetova')
        html = self.get_ok('/admin/users?role=teacher')
        self.assertIn('ждёт смены пароля', html)


class TeacherCreateWithPasswordTest(CmsTestCase):
    """Создание учителя с паролем — тремя путями: «Пользователи → Добавить», «Преподаватели → Добавить»,
    «Преподаватели → Правка» (создание входа / новый пароль). Везде — флаг обязательной смены."""

    fresh_seed_each_test = True

    def test_add_user_teacher_sets_flag(self):
        r = self.post('/admin/users/add', {'name': 'Новый Учитель', 'email': 'nt1@caspian.test',
                                           'password': 'Start123', 'role': 'teacher'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        found = self.docs('users', email='nt1@caspian.test')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['role'], 'teacher')
        self.assertIs(found[0].get('mustChangePassword'), True)
        self.assertIsNotNone(found[0].get('passwordResetAt'))
        self.assertTrue(_sign_in_ok('nt1@caspian.test', 'Start123'))   # вход не блокируется — смена в приложении
        self.assertIn('приложение попросит придумать новый пароль', ' '.join(m for _c, m in self.flashes()))

    def test_add_user_other_roles_do_not_set_flag(self):
        cases = [('student', {'group': 'ПО 24-1', 'course': '2'}), ('club', {}), ('admin', {})]
        for i, (role, extra) in enumerate(cases):
            with self.subTest(role=role):
                email = 'nr%d@caspian.test' % i
                r = self.post('/admin/users/add', dict({'name': 'Новый %s' % role, 'email': email,
                                                        'password': 'Start123', 'role': role}, **extra))
                self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
                found = self.docs('users', email=email)
                self.assertEqual(len(found), 1)
                self.assertNotIn('mustChangePassword', found[0])
                self.assertNotIn('passwordResetAt', found[0])

    def test_teacher_add_with_login_sets_flag(self):
        r = self.post('/teachers/add', {'name': 'Новая Учительница', 'email': 'new.t2@caspian.test',
                                        'password': 'Teach123'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        found = self.docs('users', email='new.t2@caspian.test')
        self.assertEqual(len(found), 1)
        self.assertIs(found[0].get('mustChangePassword'), True)
        self.assertIsNotNone(found[0].get('passwordResetAt'))
        self.assertTrue(_sign_in_ok('new.t2@caspian.test', 'Teach123'))
        self.assertIn('приложение попросит придумать новый пароль', ' '.join(m for _c, m in self.flashes()))

    def test_teacher_add_without_login_has_no_flag(self):
        r = self.post('/teachers/add', {'name': 'Без Входа Совсем'})
        self.assertEqual(r.status_code, 302)
        found = self.docs('users', name='Без Входа Совсем')
        self.assertEqual(len(found), 1)
        self.assertNotIn('mustChangePassword', found[0])

    def test_teacher_edit_create_login_sets_flag(self):
        # у t_petrov в сиде нет входа: создаём его на странице правки (uid сохраняется)
        r = self.post('/teachers/edit/t_petrov', {'name': 'Петров Дмитрий Игоревич', 'email': 'petrov.d@caspian.test',
                                                   'password': 'Petrov123'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        d = self.doc('users', 't_petrov')
        self.assertIs(d.get('mustChangePassword'), True)
        self.assertIsNotNone(d.get('passwordResetAt'))
        self.assertTrue(_sign_in_ok('petrov.d@caspian.test', 'Petrov123'))

    def test_teacher_edit_new_password_sets_flag_and_revokes_sessions(self):
        name = self.doc('users', 't_ivanova')['name']
        with _revoke_spy() as revoke:
            r = self.post('/teachers/edit/t_ivanova', {'name': name, 'email': 'ivanova@caspian.test',
                                                       'password': 'Admin5678'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        revoke.assert_called_once_with('t_ivanova')
        self.assertTrue(_sign_in_ok('ivanova@caspian.test', 'Admin5678'))
        d = self.doc('users', 't_ivanova')
        self.assertIs(d.get('mustChangePassword'), True)
        self.assertIsNotNone(d.get('passwordResetAt'))

    def test_teacher_edit_without_password_does_not_touch_flag(self):
        name = self.doc('users', 't_ivanova')['name']
        with _revoke_spy() as revoke:
            r = self.post('/teachers/edit/t_ivanova', {'name': name + ' Н.', 'email': 'ivanova@caspian.test',
                                                       'phone': '+77015550000'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        revoke.assert_not_called()
        d = self.doc('users', 't_ivanova')
        self.assertNotIn('mustChangePassword', d)
        self.assertEqual(d['name'], name + ' Н.')

    def test_teacher_edit_keeps_flag_already_set_by_cms_when_no_new_password(self):
        from cms.core import db
        db.collection('users').document('t_ivanova').update({'mustChangePassword': True})
        name = self.doc('users', 't_ivanova')['name']
        self.post('/teachers/edit/t_ivanova', {'name': name, 'email': 'ivanova@caspian.test', 'phone': '+77015550001'})
        self.assertIs(self.doc('users', 't_ivanova').get('mustChangePassword'), True)


class TeacherPasswordTextsTest(CmsTestCase):
    """Тексты подтверждения сброса: учителю — «приложение попросит новый пароль», остальным — как раньше."""

    fresh_seed_each_test = True

    def test_teacher_pages_mention_forced_change(self):
        for url in ('/admin/users/edit/t_ivanova', '/teachers/edit/t_ivanova', '/teachers'):
            with self.subTest(url=url):
                html = self.get_ok(url)
                self.assertIn('Teacher123', html)
                self.assertIn('Все сеансы завершатся, при входе приложение попросит придумать новый пароль', html)

    def test_teacher_account_card_shows_password_not_changed_yet(self):
        self.post('/admin/users/reset-password/t_akhmetova')
        html = self.get_ok('/admin/users/edit/t_akhmetova')
        self.assertIn('Пароль выдан', html)
        self.assertIn('преподаватель ещё не сменил его', html)
        self.assertNotIn('абитуриент ещё не сменил', html)

    def test_users_list_teacher_row_mentions_forced_change(self):
        html = self.get_ok('/admin/users?role=teacher')
        self.assertIn('приложение попросит придумать новый пароль', html)

    def test_student_reset_text_unchanged(self):
        from cms.core import fb_auth
        fb_auth.create_user(uid='st_001', email='student01@caspian.test', password='Other999')
        html = self.get_ok('/admin/users/edit/st_001')
        self.assertIn('Student123', html)
        self.assertIn('Сообщите его пользователю лично', html)
        self.assertNotIn('приложение попросит', html)
