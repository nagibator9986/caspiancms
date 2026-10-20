# tests/test_people.py — раздел «Люди» и главная (PEOPLE): пользователи, преподаватели, группы,
# дисциплины, администрация, дашборд «/», журнал аудита /audit.
#
# Сначала — тесты на баги из аудита (они падали на старом коде):
#   * правка абитуриента в форме пользователей молча делала его студентом с group='-';
#   * сброс пароля знал только Teacher123/Student123 (абитуриенту — abiturient123 + mustChangePassword, D5);
#   * удаление пользователя шло через GET и не удаляло аккаунт Firebase Auth;
#   * преподаватель без аккаунта при создании входа получал НОВЫЙ uid (ломались ссылки расписания);
#   * хранимый XSS: ФИО подставлялось в onclick/innerHTML.

import io
from unittest import mock

from firebase_admin import auth as fb_auth_module

from tests.base import CmsTestCase
from tests.seed import _png


def _sign_in_ok(email, password):
    """True, если Auth-эмулятор пускает с этим паролем."""
    from cms.core import LoginError, sign_in_with_password
    try:
        sign_in_with_password(email, password)
        return True
    except LoginError:
        return False


def _auth_user(uid):
    from cms.core import fb_auth
    try:
        return fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        return None


class UsersBugsTest(CmsTestCase):
    """Баги «Пользователей»: роль абитуриента, сброс пароля по роли, удаление с Auth."""

    fresh_seed_each_test = True

    def test_edit_applicant_keeps_role_and_writes_no_stubs(self):
        # Старая модалка присылала role=student, group='-', course='1' для ЛЮБОГО пользователя.
        r = self.post('/admin/users/edit/ap_002', {'name': 'Айгерим Новая', 'phone': '+7 701 555 44 33',
                                                    'role': 'student', 'group': '-', 'course': '1'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        d = self.doc('users', 'ap_002')
        self.assertEqual(d['role'], 'applicant')
        self.assertEqual(d['name'], 'Айгерим Новая')
        self.assertEqual(d['phone'], '+77015554433')
        self.assertNotIn('group', d)
        self.assertNotIn('course', d)

    def test_edit_applicant_rejects_bad_phone(self):
        before = self.doc('users', 'ap_002')
        r = self.post('/admin/users/edit/ap_002', {'name': 'Тест', 'phone': '123'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Телефон', r.get_data(as_text=True))
        self.assertEqual(self.doc('users', 'ap_002').get('phone'), before.get('phone'))

    def test_reset_password_applicant(self):
        uid = 'ap_010'
        email = self.doc('users', uid)['email']
        self.assertTrue(_sign_in_ok(email, 'secret12'))
        with mock.patch.object(fb_auth_module, 'revoke_refresh_tokens',
                               wraps=fb_auth_module.revoke_refresh_tokens) as revoke:
            r = self.post('/admin/users/reset-password/%s' % uid)
        self.assertEqual(r.status_code, 302)
        revoke.assert_called_once_with(uid)
        self.assertTrue(_sign_in_ok(email, 'abiturient123'))
        self.assertFalse(_sign_in_ok(email, 'secret12'))
        d = self.doc('users', uid)
        self.assertIs(d.get('mustChangePassword'), True)
        self.assertIsNotNone(d.get('passwordResetAt'))
        self.assertNotIn('updated_at', d)
        entries = [e for e in self.audit_entries('reset_password') if e.get('entityId') == uid]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['details'].get('role'), 'applicant')
        history = self.doc('applicant_crm', uid).get('history') or []
        self.assertTrue(any(h.get('action') == 'password_reset' for h in history), history)
        self.assertIn('abiturient123', ' '.join(m for _c, m in self.flashes()))

    def test_reset_password_teacher_no_flag(self):
        from cms.core import fb_auth
        fb_auth.update_user('t_akhmetova', password='Other999')
        r = self.post('/admin/users/reset-password/t_akhmetova')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(_sign_in_ok('akhmetova@caspian.test', 'Teacher123'))
        d = self.doc('users', 't_akhmetova')
        self.assertNotIn('mustChangePassword', d)
        self.assertIsNotNone(d.get('updatedAt'))

    def test_reset_password_admin_refused(self):
        r = self.post('/admin/users/reset-password/admin_2')
        self.assertEqual(r.status_code, 302)
        self.assertIn('error', [c for c, _m in self.flashes()])
        self.assertEqual([e for e in self.audit_entries('reset_password') if e.get('entityId') == 'admin_2'], [])

    def test_reset_password_without_account(self):
        r = self.post('/admin/users/reset-password/st_001')   # у студентов сида нет Auth-аккаунта
        self.assertEqual(r.status_code, 302)
        self.assertIn('error', [c for c, _m in self.flashes()])

    def test_delete_user_removes_auth_and_doc(self):
        self.assertIsNotNone(_auth_user('ap_015'))
        r = self.post('/admin/users/delete/ap_015')
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(_auth_user('ap_015'))
        self.assertIsNone(self.doc('users', 'ap_015'))
        self.assertTrue([e for e in self.audit_entries('delete') if e.get('entityId') == 'ap_015'])

    def test_delete_via_get_not_allowed(self):
        r = self.client.get('/admin/users/delete/ap_020')
        self.assertEqual(r.status_code, 405)
        self.assertIsNotNone(self.doc('users', 'ap_020'))
        self.assertIsNotNone(_auth_user('ap_020'))

    def test_cannot_delete_or_demote_self(self):
        r = self.post('/admin/users/delete/admin_main')
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.doc('users', 'admin_main'))
        r = self.post('/admin/users/edit/admin_main', {'name': 'Я', 'role': 'student', 'group': 'ПО 24-1'})
        self.assertEqual(self.doc('users', 'admin_main')['role'], 'admin')

    def test_role_change_student_to_teacher_drops_group(self):
        r = self.post('/admin/users/edit/st_002', {'name': 'Новый Преподаватель', 'role': 'teacher',
                                                    'group': 'ПО 24-1', 'course': '2'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        d = self.doc('users', 'st_002')
        self.assertEqual(d['role'], 'teacher')
        self.assertNotIn('group', d)
        self.assertNotIn('course', d)
        from cms.schedule_model import get_all_teachers
        self.assertIn('st_002', get_all_teachers())   # справочник преподавателей сброшен
        self.assertTrue([e for e in self.audit_entries('update') if e.get('entityId') == 'st_002'])

    def test_edit_student_group_validated(self):
        r = self.post('/admin/users/edit/st_003', {'name': 'Студент', 'role': 'student', 'group': 'НЕТ ТАКОЙ',
                                                    'course': '2'})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(self.doc('users', 'st_003').get('group'), 'НЕТ ТАКОЙ')
        r = self.post('/admin/users/edit/st_003', {'name': 'Студент', 'role': 'student', 'group': 'ИС 24-1',
                                                    'course': '2'})
        self.assertEqual(r.status_code, 302)
        d = self.doc('users', 'st_003')
        self.assertEqual((d['group'], d['course']), ('ИС 24-1', '2'))


class UsersPagesTest(CmsTestCase):

    def test_list_tabs_and_applicant_link(self):
        html = self.get_ok('/admin/users')
        for label in ('Студенты', 'Преподаватели', 'Абитуриенты', 'Администраторы'):
            self.assertIn(label, html)
        self.assertNotIn('onclick=', html)
        html = self.get_ok('/admin/users?role=applicant')
        self.assertIn('/applicants/ap_001', html)
        self.assertIn('+7 701 123 45 67', html)   # логин абитуриента (display_login)
        self.assertNotIn('Без группы', html)

    def test_search_and_group_filter(self):
        html = self.get_ok('/admin/users?q=student01')
        self.assertIn('student01@caspian.test', html)
        self.assertNotIn('student02@caspian.test', html)
        st = self.doc('users', 'st_001')
        html = self.get_ok('/admin/users?role=student&group=%s' % st['group'])
        self.assertIn('student01@caspian.test', html)

    def test_csv_export(self):
        r = self.client.get('/admin/users?role=teacher&format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['Content-Type'])
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith('﻿'))
        self.assertIn('Петров Дмитрий Игоревич', body)
        self.assertNotIn('student01@caspian.test', body)

    def test_edit_pages_render(self):
        for uid in ('st_001', 'ap_001', 't_petrov', 'admin_main', 'club_robotics'):
            with self.subTest(uid=uid):
                html = self.get_ok('/admin/users/edit/%s' % uid)
                self.assertIn('cc-page-header', html)
        html = self.get_ok('/admin/users/edit/ap_001')
        self.assertIn('/applicants/ap_001', html)
        self.assertIn('abiturient123', html)
        r = self.client.get('/admin/users/edit/no_such_user')
        self.assertEqual(r.status_code, 302)


class UsersCreateTest(CmsTestCase):

    fresh_seed_each_test = True

    def test_create_student_and_reset(self):
        r = self.post('/admin/users/add', {'name': 'Новиков Илья', 'email': 'novikov@caspian.test',
                                           'password': 'Start123', 'role': 'student', 'group': 'ПО 24-1',
                                           'course': '2', 'phone': '+77017778899'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:800])
        from cms.core import fb_auth
        user = fb_auth.get_user_by_email('novikov@caspian.test')
        d = self.doc('users', user.uid)
        self.assertEqual(d['role'], 'student')
        self.assertEqual((d['group'], d['course'], d['attendance'], d['grades']), ('ПО 24-1', '2', '0%', 'N/A'))
        self.assertIsNotNone(d.get('createdAt'))
        self.assertTrue(_sign_in_ok('novikov@caspian.test', 'Start123'))
        self.post('/admin/users/reset-password/%s' % user.uid)
        self.assertTrue(_sign_in_ok('novikov@caspian.test', 'Student123'))
        self.assertNotIn('mustChangePassword', self.doc('users', user.uid))

    def test_create_validation(self):
        r = self.post('/admin/users/add', {'name': '', 'email': 'bad', 'password': '123', 'role': 'student'})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('is-invalid', html)
        # роль абитуриента вручную не создаётся
        r = self.post('/admin/users/add', {'name': 'Кто-то', 'email': 'x1@caspian.test', 'password': 'Start123',
                                           'role': 'applicant'})
        self.assertEqual(r.status_code, 200)
        from cms.core import fb_auth
        with self.assertRaises(fb_auth.UserNotFoundError):
            fb_auth.get_user_by_email('x1@caspian.test')

    def test_create_duplicate_email(self):
        r = self.post('/admin/users/add', {'name': 'Дубль', 'email': 'ivanova@caspian.test', 'password': 'Start123',
                                           'role': 'teacher'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('уже', r.get_data(as_text=True))

    def test_xss_in_name_is_escaped(self):
        evil = 'Тест\'"><script>alert(1)</script>'
        r = self.post('/admin/users/add', {'name': evil, 'email': 'evil@caspian.test', 'password': 'Start123',
                                           'role': 'student', 'group': 'ПО 24-1', 'course': '1'})
        self.assertEqual(r.status_code, 302)
        for url in ('/admin/users', '/admin/users?q=evil', '/groups'):
            html = self.get_ok(url)
            self.assertNotIn('<script>alert(1)</script>', html)
            self.assertNotIn('onclick=', html)

    def test_bulk_group_course_and_delete(self):
        r = self.post('/admin/users/bulk', {'action': 'group', 'group': 'ВТ 24-1', 'ids': ['st_001', 'st_002', 't_petrov']})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('users', 'st_001')['group'], 'ВТ 24-1')
        self.assertEqual(self.doc('users', 'st_002')['group'], 'ВТ 24-1')
        self.assertNotIn('group', self.doc('users', 't_petrov'))   # не студент — не трогаем
        r = self.post('/admin/users/bulk', {'action': 'course', 'course': '3', 'ids': ['st_001']})
        self.assertEqual(self.doc('users', 'st_001')['course'], '3')
        r = self.post('/admin/users/bulk', {'action': 'delete', 'ids': ['st_004', 'ap_025', 'admin_main']})
        self.assertIsNone(self.doc('users', 'st_004'))
        self.assertIsNone(self.doc('users', 'ap_025'))
        self.assertIsNone(_auth_user('ap_025'))
        self.assertIsNotNone(self.doc('users', 'admin_main'))   # себя не удаляем

    def test_account_toggle_and_revoke(self):
        r = self.post('/admin/users/ap_001/disable', {'disabled': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(_auth_user('ap_001').disabled)
        self.post('/admin/users/ap_001/disable', {'disabled': '0'})
        self.assertFalse(_auth_user('ap_001').disabled)
        with mock.patch.object(fb_auth_module, 'revoke_refresh_tokens',
                               wraps=fb_auth_module.revoke_refresh_tokens) as revoke:
            self.post('/admin/users/ap_001/revoke')
        revoke.assert_called_once_with('ap_001')


class TeachersTest(CmsTestCase):

    fresh_seed_each_test = True

    def test_list_with_workload(self):
        html = self.get_ok('/teachers')
        self.assertIn('Петров Дмитрий Игоревич', html)
        self.assertIn('Нет входа', html)
        self.assertIn('Есть вход', html)
        r = self.client.get('/teachers?format=csv')
        self.assertIn('text/csv', r.headers['Content-Type'])

    def test_teacher_page_marks_clashes(self):
        from collections import OrderedDict
        from cms.routes_people import _teacher_clashes
        from cms.schedule_model import load_lessons_snapshot
        snap = load_lessons_snapshot(force=True)

        def clashes(uid):
            days = OrderedDict()
            for l in snap.by_teacher.get('uid:' + uid, []):
                days.setdefault(l['weekday'], []).append(l)
            return _teacher_clashes(days)
        self.assertTrue({'cf_petrov_a', 'cf_petrov_b'} <= clashes('t_petrov'))
        self.assertTrue({'cf_akhm_a', 'cf_akhm_b'} <= clashes('t_akhmetova'))
        self.assertNotIn('ok_omar_num', clashes('t_omarova'))      # числитель × знаменатель
        self.assertNotIn('cf_stream_a', clashes('t_kim'))          # поток — не накладка
        html = self.get_ok('/teachers/edit/t_petrov')
        self.assertIn('is-clash', html)
        self.assertIn('/schedule/edit/cf_petrov_a', html)

    def test_create_account_keeps_uid(self):
        self.assertIsNone(_auth_user('t_petrov'))
        r = self.post('/teachers/edit/t_petrov', {'name': 'Петров Дмитрий Игоревич', 'email': 'petrov.d@caspian.test',
                                                   'password': 'Petrov123'})
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:800])
        user = _auth_user('t_petrov')
        self.assertIsNotNone(user)
        self.assertEqual(user.email, 'petrov.d@caspian.test')
        d = self.doc('users', 't_petrov')
        self.assertEqual(d['email'], 'petrov.d@caspian.test')
        self.assertEqual(d['role'], 'teacher')
        self.assertEqual(len(self.docs('users', email='petrov.d@caspian.test')), 1)
        self.assertTrue(_sign_in_ok('petrov.d@caspian.test', 'Petrov123'))

    def test_edit_missing_teacher(self):
        r = self.client.get('/teachers/edit/no_such')
        self.assertEqual(r.status_code, 302)
        r = self.post('/teachers/delete/no_such')
        self.assertEqual(r.status_code, 302)

    def test_add_teacher(self):
        r = self.post('/teachers/add', {'name': 'Новая Учительница', 'email': 'new.t@caspian.test', 'password': 'Teach123'})
        self.assertEqual(r.status_code, 302)
        found = self.docs('users', email='new.t@caspian.test')
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]['role'], 'teacher')
        self.assertIsNotNone(_auth_user(found[0]['id']))
        r = self.post('/teachers/add', {'name': 'Без входа'})
        self.assertEqual(len(self.docs('users', name='Без входа')), 1)
        r = self.post('/teachers/add', {'name': 'Только пароль', 'password': 'Teach123'})
        self.assertEqual(self.docs('users', name='Только пароль'), [])

    def test_delete_with_lessons_needs_force(self):
        from cms.schedule_model import load_lessons_snapshot
        snap = load_lessons_snapshot(force=True)
        uid = next(u for u in ('t_seitkali', 't_petrov', 't_nurlanov') if snap.by_teacher.get('uid:' + u))
        r = self.post('/teachers/delete/%s' % uid)
        self.assertIsNotNone(self.doc('users', uid))
        r = self.post('/teachers/delete/%s' % uid, {'force': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('users', uid))


class GroupsSubjectsTest(CmsTestCase):

    fresh_seed_each_test = True

    def test_groups_page(self):
        html = self.get_ok('/groups')
        self.assertIn('ПО 24-1', html)
        self.assertNotIn('onclick=', html)

    def test_rename_group_everywhere(self):
        from cms.schedule_model import load_lessons_snapshot
        old, new = 'ПО 25-1', 'ПО 25-1а'
        lessons = len(load_lessons_snapshot(force=True).by_group.get(old, []))
        students = [u['id'] for u in self.docs('users', group=old)]
        self.assertTrue(lessons and students)
        r = self.post('/groups/edit_name', {'old_name': old, 'new_name': new, 'schedules': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('groups_list', old))
        self.assertEqual(self.doc('groups_list', new)['name'], new)
        self.assertEqual(sorted(u['id'] for u in self.docs('users', group=new)), sorted(students))
        self.assertEqual(len(self.docs('schedules', groupId=new)), lessons)
        self.assertEqual(self.docs('schedules', groupId=old), [])
        self.assertTrue(all(d.get('notify') is not False for d in self.docs('schedules', groupId=new)))
        self.assertTrue(self.audit_entries('rename'))

    def test_rename_to_existing_refused(self):
        r = self.post('/groups/edit_name', {'old_name': 'ПО 24-1', 'new_name': 'ПО 24-2'})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.doc('groups_list', 'ПО 24-1'))
        self.assertIn('error', [c for c, _m in self.flashes()])

    def test_create_and_delete_group(self):
        r = self.post('/groups/create', {'group_name': '  ПО   26-1 '})
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.doc('groups_list', 'ПО 26-1'))
        r = self.post('/groups/create', {'group_name': 'по 26-1'})
        self.assertIn('error', [c for c, _m in self.flashes()])
        r = self.post('/groups/create', {'group_name': 'А/Б'})
        self.assertEqual(self.docs('groups_list', name='А/Б'), [])
        members = [u['id'] for u in self.docs('users', group='ЭК 25-1')]
        r = self.post('/groups/delete/ЭК 25-1')
        self.assertIsNone(self.doc('groups_list', 'ЭК 25-1'))
        for uid in members:
            self.assertNotIn('group', self.doc('users', uid))

    def test_subjects(self):
        html = self.get_ok('/subjects')
        self.assertIn('Математика', html)
        r = self.post('/subjects/add', {'names': 'Химия\nБиология\nматематика\n\nХимия'})
        self.assertEqual(r.status_code, 302)
        names = sorted(d['name'] for d in self.docs('subjects'))
        self.assertEqual(names.count('Химия'), 1)
        self.assertIn('Биология', names)
        self.assertEqual(sum(1 for n in names if n.lower() == 'математика'), 1)
        sid = self.docs('subjects', name='Физика')[0]['id']
        before = len(self.docs('schedules', subject='Физика'))
        self.assertTrue(before)
        r = self.post('/subjects/edit/%s' % sid, {'name': 'Физика и астрономия', 'schedules': '1'})
        self.assertEqual(self.doc('subjects', sid)['name'], 'Физика и астрономия')
        self.assertEqual(len(self.docs('schedules', subject='Физика и астрономия')), before)
        r = self.post('/subjects/delete/%s' % self.docs('subjects', name='Биология')[0]['id'])
        self.assertEqual(self.docs('subjects', name='Биология'), [])


class AdministrationTest(CmsTestCase):

    fresh_seed_each_test = True

    def test_list_does_not_write(self):
        from cms.core import db
        db.collection('management').document('mg_x').set({'name': 'Без приоритета', 'position': 'Методист'})
        html = self.get_ok('/administration')
        self.assertIn('Без приоритета', html)
        self.assertNotIn('priority', self.doc('management', 'mg_x'))
        self.assertNotIn('onclick=', html)

    def test_add_with_photo_and_order(self):
        data = {'name': 'Новый Зам', 'position': 'Заместитель по IT',
                'photo': (io.BytesIO(_png(120, 160, (255, 98, 0))), 'zam.png')}
        r = self.post('/administration/add', data, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:800])
        found = self.docs('management', name='Новый Зам')
        self.assertEqual(len(found), 1)
        doc = found[0]
        self.assertIn('token=', doc['image'])
        self.assertTrue(doc['imagePath'].startswith('management/%s/' % doc['id']))
        self.assertEqual(doc['priority'], 4)   # max(0..3) + 1
        from cms.core import bucket
        self.assertTrue(bucket.blob(doc['imagePath']).exists())
        order = [doc['id'], 'mg_3', 'mg_2', 'mg_1', 'mg_0']
        r = self.post_json('/administration/update_order', {'order': order})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['success'])
        self.assertEqual(self.doc('management', doc['id'])['priority'], 1)
        self.assertEqual(self.doc('management', 'mg_0')['priority'], 5)
        r = self.post('/administration/delete/%s' % doc['id'])
        self.assertIsNone(self.doc('management', doc['id']))
        self.assertFalse(bucket.blob(doc['imagePath']).exists())

    def test_edit_validation(self):
        r = self.post('/administration/edit/mg_1', {'name': '', 'position': 'X'})
        self.assertEqual(r.status_code, 200)
        self.assertNotEqual(self.doc('management', 'mg_1')['position'], 'X')
        r = self.post('/administration/edit/mg_1', {'name': 'Тажибаев Ерлан', 'position': 'Проректор'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('management', 'mg_1')['position'], 'Проректор')

    def test_bad_order_payload(self):
        r = self.post_json('/administration/update_order', {'order': 'nope'})
        self.assertEqual(r.status_code, 400)


class UsersV2Test(CmsTestCase):
    """UX v2 (PPL-1, PPL-2, PPL-9): «Все» без абитуриентов, порядок вкладок, быстрый просмотр, действия строки."""

    def _tabs(self, html):
        import re
        m = re.search(r'<nav class="cc-tabs"[^>]*aria-label="Роли".*?</nav>', html, re.S)
        self.assertIsNotNone(m, 'нет вкладок ролей')
        return m.group(0)

    def test_all_tab_hides_applicants(self):
        html = self.get_ok('/admin/users')
        self.assertNotIn('ap_00', html)                      # ни строк, ни ссылок на абитуриентов
        self.assertIn('data-cc-hint="users-applicants"', html)
        tabs = self._tabs(html)
        labels = ['Все', 'Студенты', 'Преподаватели', 'Администраторы', 'Клубы', 'Абитуриенты']
        pos = [tabs.index('<span>%s</span>' % l) for l in labels]
        self.assertEqual(pos, sorted(pos), 'порядок вкладок: абитуриенты — последними')
        from cms.routes_people import _users_snapshot
        staff = sum(1 for r in _users_snapshot() if r['role_key'] != 'applicant')
        self.assertRegex(tabs, r'<span>Все</span>\s*<span class="cc-tab__count">%d</span>' % staff)
        html = self.get_ok('/admin/users?role=applicant')
        self.assertIn('ap_001', html)
        body = self.client.get('/admin/users?format=csv').get_data(as_text=True)
        self.assertNotIn('ap_001', body)                     # CSV «Всех» — тоже без абитуриентов
        self.assertIn('student01@caspian.test', body)

    def test_search_hints_applicants(self):
        name = self.doc('users', 'ap_001')['name']
        html = self.get_ok('/admin/users?q=%s' % name.split()[0])
        self.assertIn('pp-also', html)                       # «есть ещё N абитуриентов — Показать»
        self.assertIn('role=applicant', html)
        self.assertNotIn('ap_001', html)

    def test_row_menu_and_bulk_markup(self):
        html = self.get_ok('/admin/users?role=student')
        self.assertIn('data-cc-bulkbar="usersBulk"', html)
        self.assertIn('data-cc-select="usersBulk"', html)
        self.assertIn('cc-table--stack', html)
        self.assertIn('id="usersGroupModal"', html)          # перевод в группу — одного и выбранных
        self.assertIn('data-pp-user="st_001"', html)
        self.assertIn('data-cc-peek', html)                  # строка → быстрый просмотр
        self.assertNotIn('pp-desktop-actions', html)         # на телефоне действия не прячутся
        html = self.get_ok('/admin/users?role=applicant')
        self.assertIn('Сбросить пароль', html)
        self.assertIn('станет abiturient123', html)          # новый пароль — в тексте подтверждения

    def test_peek_partial(self):
        r = self.client.get('/admin/users/edit/st_001?next=/admin/users%3Frole%3Dstudent', headers={'X-CC-Partial': '1'})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn('cc-sidebar', html)
        self.assertIn('pp-peek', html)
        self.assertIn('action="/admin/users/edit/st_001"', html)
        self.assertIn('name="next" value="/admin/users?role=student"', html)   # после сохранения — в список
        html = self.client.get('/admin/users/edit/ap_001', headers={'X-CC-Partial': '1'}).get_data(as_text=True)
        self.assertIn('/applicants/ap_001', html)

    def test_refresh_redirects_without_param(self):
        r = self.client.get('/admin/users?role=student&refresh=1')
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/admin/users?role=student'), r.headers['Location'])


class UsersV2ActionsTest(CmsTestCase):

    fresh_seed_each_test = True

    def test_disable_has_undo_toast(self):
        nxt = '/admin/users?role=applicant'
        r = self.post('/admin/users/ap_001/disable', {'disabled': '1', 'next': nxt})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith(nxt))
        self.assertTrue(_auth_user('ap_001').disabled)
        import json
        cat, raw = self.flashes()[-1]
        self.assertEqual(cat, 'cc-action')
        action = json.loads(raw)
        self.assertEqual(action['label'], 'Отменить')
        self.assertEqual(action['post'], '/admin/users/ap_001/disable')
        self.assertEqual(action['fields'], {'disabled': '0', 'next': nxt})
        r = self.post(action['post'], action['fields'])            # «Отменить» из тоста
        self.assertFalse(_auth_user('ap_001').disabled)

    def test_move_one_student_from_row_menu(self):
        r = self.post('/admin/users/bulk', {'action': 'group', 'group': 'ВТ 24-1', 'ids': ['st_001'],
                                            'next': '/admin/users?role=student'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/admin/users?role=student'))
        self.assertEqual(self.doc('users', 'st_001')['group'], 'ВТ 24-1')
        self.assertIn('переведён', ' '.join(m for _c, m in self.flashes()))

    def test_edit_returns_to_list(self):
        r = self.post('/admin/users/edit/st_003', {'name': 'Студент Новый', 'role': 'student', 'group': 'ИС 24-1',
                                                    'course': '2', 'next': '/admin/users?role=student&page=2'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers['Location'].endswith('/admin/users?role=student&page=2'))
        self.assertEqual(self.flashes()[-1][0], 'cc-action')       # «Изменения сохранены · Открыть»


class TeachersV2Test(CmsTestCase):
    """UX v2 (PPL-3, PPL-4, PPL-6): форма добавления без redirect, тёзки, карточка, «Передать занятия…»."""

    fresh_seed_each_test = True

    def test_add_errors_render_open_form(self):
        html = self.get_ok('/teachers')
        self.assertIn('method="post" action="/teachers"', html)    # модалка шлёт POST на адрес списка
        r = self.post('/teachers', {'name': 'Новый Препод', 'email': 'не-email', 'password': '123'})
        self.assertEqual(r.status_code, 200)                       # не redirect на ?add=1
        html = r.get_data(as_text=True)
        self.assertIn('data-cc-open-on-load', html)
        self.assertIn('value="Новый Препод"', html)                 # значения не потерялись
        self.assertIn('Некорректный email', html)
        self.assertIn('is-invalid', html)
        self.assertEqual(self.docs('users', name='Новый Препод'), [])

    def test_namesake_needs_confirmation(self):
        name = 'Петров Дмитрий Игоревич'
        before = len(self.docs('users', name=name))
        r = self.post('/teachers/add', {'name': '  петров  дмитрий игоревич '})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Всё равно добавить', html)
        self.assertIn('name="confirm_duplicate" value="1"', html)
        self.assertEqual(len(self.docs('users', name=name)), before)
        r = self.post('/teachers/add', {'name': name, 'confirm_duplicate': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIn('/teachers/edit/', r.headers['Location'])
        self.assertEqual(len(self.docs('users', name=name)), before + 1)

    def test_add_redirects_to_card_with_action(self):
        r = self.post('/teachers/add', {'name': 'Совсем Новый Преподаватель'})
        self.assertEqual(r.status_code, 302)
        uid = self.docs('users', name='Совсем Новый Преподаватель')[0]['id']
        self.assertTrue(r.headers['Location'].endswith('/teachers/edit/%s' % uid))
        import json
        cat, raw = self.flashes()[-1]
        self.assertEqual(cat, 'cc-action')
        self.assertEqual(json.loads(raw)['label'], 'Добавить занятия')

    def test_list_filters_and_card(self):
        html = self.get_ok('/teachers?lessons=no')
        self.assertIn('kim.v2@caspian.test', html)
        self.assertNotIn('petrov@caspian.test', html)
        html = self.get_ok('/teachers')
        self.assertIn('cc-summary', html)
        self.assertNotIn('cc-stat__value', html)                   # KPI-плиток больше нет
        self.assertIn('тёзка', html)                                # два «Ким Виктор Александрович»
        html = self.get_ok('/teachers/edit/t_petrov')
        self.assertIn('cc-savebar', html)
        self.assertIn('data-cc-back', html)
        r = self.client.get('/teachers/edit/t_petrov?next=/teachers', headers={'X-CC-Partial': '1'})
        html = r.get_data(as_text=True)                            # быстрый просмотр из списка
        self.assertNotIn('cc-sidebar', html)
        self.assertIn('pp-peek', html)
        self.assertIn('is-clash', html)

    def test_transfer_form_posts_to_schedule_bulk(self):
        import re
        from cms.schedule_model import load_lessons_snapshot
        html = self.get_ok('/teachers/edit/t_petrov')
        self.assertIn('id="teacherTransfer"', html)
        form = re.search(r'<form class="modal-content" method="post" action="([^"]+)"[^>]*>(.*?)</form>',
                         html[html.index('id="teacherTransfer"'):], re.S)
        self.assertEqual(form.group(1), '/schedule/bulk')
        body = form.group(2)
        self.assertIn('name="teacher_from" value="t_petrov"', body)
        ids = re.findall(r'name="ids" value="([^"]+)"', body)
        lessons = load_lessons_snapshot(force=True).by_teacher.get('uid:t_petrov', [])
        self.assertEqual(sorted(ids), sorted(l['id'] for l in lessons))
        r = self.post('/schedule/bulk', {'action': 'teacher', 'teacher': 't_kim', 'teacher_from': 't_petrov',
                                         'ids': ids, 'next': '/teachers/edit/t_petrov'})
        self.assertEqual(r.status_code, 302)
        snap = load_lessons_snapshot(force=True)
        self.assertEqual(snap.by_teacher.get('uid:t_petrov', []), [])


class DirectoriesV2Test(CmsTestCase):
    """UX v2 (PPL-5, PPL-7): группы, дисциплины, администрация, аудит — новые компоненты."""

    def test_groups_and_subjects(self):
        html = self.get_ok('/groups')
        self.assertIn('cc-table--stack', html)
        self.assertIn('id="groupRename"', html)
        self.assertIn('data-pp-rename="ПО 24-1"', html)
        self.assertNotIn('cc-stat__value', html)
        html = self.get_ok('/subjects?q=физ')
        self.assertIn('Физика', html)
        self.assertNotIn('Математика', html)
        self.assertIn('id="subjectRename"', html)

    def test_administration_and_audit(self):
        html = self.get_ok('/administration')
        self.assertIn('pp-mgmt__card', html)
        self.assertIn('data-pp-order-bar', html)
        html = self.get_ok('/audit')
        self.assertIn('cc-table--stack', html)
        self.assertIn('cc-summary', html)
        self.assertNotIn('cc-stat__value', html)


class HomeTest(CmsTestCase):

    def test_dashboard_kpi(self):
        with mock.patch('cms.conflicts.summary', return_value=None):
            from cms.core import cache
            cache.invalidate_prefix('home:')
            html = self.get_ok('/')
        self.assertIn('Студенты', html)
        self.assertIn('Абитуриенты', html)
        self.assertIn('Накладки', html)
        self.assertIn('>24<', html.replace(' ', ''))   # 24 студента в сиде
        self.assertIn('Последние действия', html)

    def test_dashboard_conflicts_from_summary(self):
        from cms.core import cache
        cache.invalidate_prefix('home:')
        with mock.patch('cms.conflicts.summary', return_value={'critical': 7, 'warning': 2, 'teachers': 3}):
            html = self.get_ok('/')
        self.assertIn('/schedule/conflicts', html)
        self.assertRegex(html, r'cc-stat__value">\s*7\s*<')

    def test_audit_filters_and_csv(self):
        html = self.get_ok('/audit?action=reset_password')
        self.assertIn('ap_005', html)
        self.assertNotIn('news_open_day', html)
        html = self.get_ok('/audit?entity=news')
        self.assertIn('news_open_day', html)
        r = self.client.get('/audit?format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_data(as_text=True).startswith('﻿'))
        html = self.get_ok('/audit?q=DOD2026')
        self.assertIn('DOD2026', html)
        self.assertNotIn('ap_005', html)
        from cms.core import today_almaty
        today = today_almaty().isoformat()
        html = self.get_ok('/audit?period=custom&from=%s&to=%s' % (today, today))
        self.assertIn('name="from"', html)
        html = self.get_ok('/audit?period=custom&from=2001-01-01&to=2001-01-02')
        self.assertIn('Ничего не найдено', html)


class DateOnlyLessonsTest(CmsTestCase):
    """C1 / DATA-8: занятие только с date (старый веб) приложение показывает КАЖДУЮ неделю в этот день недели
    (matchesWeekday); разовым считается только явный one_off + date — как во вкладке «Накладки»."""
    login_as_admin = False

    def _lesson(self, doc_id, data):
        from cms.schedule_model import TeacherIndex, normalize_lesson
        return normalize_lesson(doc_id, data, TeacherIndex({'t_li': 'Ли Сергей Николаевич'}))

    def test_teacher_page_clashes_match_engine(self):
        from cms import conflicts
        from cms.routes_people import _teacher_clashes
        a = self._lesson('a', {'groupId': 'ПО 23-1', 'date': '2026-10-19', 'time': '14.00-14.50',
                               'teacher': 't_li', 'subject': 'Сети', 'room': 'А-12'})
        b = self._lesson('b', {'groupId': 'ИС 23-1', 'date': '2026-10-26', 'time': '14.00-14.50',
                               'teacher': 't_li', 'subject': 'Сети', 'room': 'А-14'})
        self.assertTrue(conflicts.analyze([a, b]).clusters['teacher'])
        self.assertEqual(_teacher_clashes({0: [a, b]}), {'a', 'b'})
        # настоящие разовые занятия в разные даты — не пересекаются
        c = self._lesson('c', dict(a['raw'], one_off=True))
        d = self._lesson('d', dict(b['raw'], one_off=True))
        self.assertEqual(_teacher_clashes({0: [c, d]}), set())

    def test_today_counts_date_only_lessons_every_week(self):
        from datetime import date
        from cms.routes_home import _today_lessons
        from cms.schedule_model import current_week_type

        class Snap:
            def __init__(self, lessons):
                self.by_day = {}
                for l in lessons:
                    self.by_day.setdefault(l['weekday'], []).append(l)
        wed = date(2026, 10, 21)
        weekly = self._lesson('w', {'groupId': 'ПО 24-1', 'date': '2025-09-03', 'time': '08.00-08.50',
                                    'room': '101', 'week_type': 'always', 'teacher': 't_li', 'subject': 'Сети'})
        once_other = self._lesson('o', {'groupId': 'ПО 24-1', 'date': '2025-09-03', 'one_off': True,
                                        'time': '09.00-09.50', 'room': '102', 'teacher': 't_li', 'subject': 'Сети'})
        once_today = self._lesson('t', {'groupId': 'ПО 24-1', 'date': wed.isoformat(), 'one_off': True,
                                        'time': '10.00-10.50', 'room': '103', 'teacher': 't_li', 'subject': 'Сети'})
        self.assertIsNotNone(current_week_type(wed))
        ids = [l['id'] for l in _today_lessons(Snap([weekly, once_other, once_today]), wed)]
        self.assertEqual(sorted(ids), ['t', 'w'])


class DeleteApplicantDataTest(CmsTestCase):
    """C4: удаление из «Людей». Абитуриент — заметки к его заказам (coin_order_notes) удаляет CMS (остальное —
    onApplicantDeleted). Зачисленный студент — функция выходит (role ≠ applicant), поэтому всё чистит CMS."""
    fresh_seed_each_test = True

    def _extra_data(self, uid, order_id):
        from cms.core import SERVER_TIMESTAMP, db
        db.collection('coin_order_notes').document(order_id).set({'note': 'Размер M', 'uid': uid,
                                                                 'updatedAt': SERVER_TIMESTAMP})
        db.collection('referral_codes').document('ZZ%s' % uid[-3:]).set({'uid': uid})
        db.collection('notifications').document('n_' + uid).set({'userId': uid, 'title': 'x'})
        db.collection('coin_claims').document('X_' + uid).set({'uid': uid, 'code': 'X'})

    def test_delete_enrolled_student_purges_applicant_data(self):
        uid = 'ap_003'                                     # заказ ord_001 (reserved, prod_hoodie)
        self.post('/applicants/%s/convert' % uid, {'group': 'ПО 23-1', 'course': '1'})
        self.assertEqual(self.doc('users', uid)['role'], 'student')
        self._extra_data(uid, 'ord_001')
        stock = self.doc('coin_products', 'prod_hoodie')['stock']
        self.assertIsNotNone(self.doc('applicant_crm', uid))
        r = self.post('/admin/users/delete/%s' % uid)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('users', uid))
        self.assertIsNone(self.doc('applicant_crm', uid), 'CRM-карточка (ПДн) осталась')
        self.assertIsNone(self.doc('coin_wallets', uid))
        for coll, field in (('coin_transactions', 'uid'), ('coin_orders', 'uid'), ('coin_claims', 'uid'),
                            ('referral_codes', 'uid'), ('notifications', 'userId'), ('coin_order_notes', 'uid')):
            self.assertEqual(self.docs(coll, **{field: uid}), [], coll)
        self.assertEqual(self.doc('coin_products', 'prod_hoodie')['stock'], stock + 1)   # резерв вернулся на склад
        self.assertIs(self.doc('applicant_ledger', uid).get('registrationCounted'), True)

    def test_delete_applicant_removes_order_notes(self):
        uid = 'ap_006'                                     # заказ ord_002
        self._extra_data(uid, 'ord_002')
        r = self.post('/admin/users/delete/%s' % uid)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('users', uid))
        self.assertEqual(self.docs('coin_order_notes', uid=uid), [])

    def test_bulk_delete_enrolled_and_applicant(self):
        self.post('/applicants/ap_003/convert', {'group': 'ПО 23-1', 'course': '1'})
        self._extra_data('ap_003', 'ord_001')
        self._extra_data('ap_006', 'ord_002')
        r = self.post('/admin/users/bulk', {'action': 'delete', 'ids': ['ap_003', 'ap_006']})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('applicant_crm', 'ap_003'))
        self.assertEqual(self.docs('coin_transactions', uid='ap_003'), [])
        self.assertEqual(self.docs('coin_order_notes', uid='ap_003'), [])
        self.assertEqual(self.docs('coin_order_notes', uid='ap_006'), [])

    def test_plain_student_delete_does_not_touch_other_data(self):
        r = self.post('/admin/users/delete/st_002')
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('users', 'st_002'))
        self.assertIsNotNone(self.doc('applicant_crm', 'ap_001'))
