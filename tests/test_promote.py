# tests/test_promote.py — «Перевод на следующий курс» (cms/routes_promote.py, PEOPLE).
#
# Чистый план (promotion_plan) проверяется без эмулятора; страница, применение и откат — на эмуляторе
# Firestore. «Нынешний» набор первого курса фиксируем патчем start_year_now() = 2026 (группа «ПО 26-1» — 1 курс).

import unittest
from unittest import mock

from cms.routes_promote import group_year, promotion_plan, start_year_now
from tests.base import CmsTestCase

START = 2026


def _s(uid, group, course, promoted=None, name=None):
    return {'id': uid, 'name': name or ('Студент ' + uid), 'group': group, 'course': course, 'promoted': promoted}


def _plan(*students):
    return promotion_plan(list(students), START)


class PromotionPlanTest(unittest.TestCase):
    def test_group_year(self):
        self.assertEqual(group_year('ПО 25-1'), 2025)
        self.assertEqual(group_year('ИТ-221'), None)          # без «-цифра» после года → не распознано
        self.assertEqual(group_year('ПО 2025-1'), 2025)
        self.assertEqual(group_year('Объединение-1'), None)
        self.assertEqual(group_year(''), None)
        self.assertEqual(group_year(None), None)

    def test_first_year_group_is_untouched(self):
        plan = _plan(_s('a', 'ПО 26-1', '1'), _s('b', 'ЭК 26-2', '1'), _s('c', 'ПО 27-1', '1'))
        self.assertEqual(plan['moves'], [])
        self.assertEqual(plan['counts']['first_year'], 3)
        self.assertEqual(plan['review'], [])

    def test_plus_one_for_older_groups(self):
        plan = _plan(_s('a', 'ПО 25-1', '1'), _s('b', 'ПО 24-1', '2'), _s('c', 'ПО 23-1', '3'))
        self.assertEqual([(m['id'], m['old'], m['new']) for m in plan['moves']], [('a', 1, 2), ('b', 2, 3), ('c', 3, 4)])
        self.assertEqual(plan['counts']['move'], 3)

    def test_already_promoted_is_skipped(self):
        # курс уже равен курсу группы — повторный запуск ничего не меняет
        plan = _plan(_s('a', 'ПО 25-1', '2'), _s('b', 'ПО 24-1', '3'), _s('c', 'ПО 23-1', '4'))
        self.assertEqual(plan['moves'], [])
        self.assertEqual(plan['counts']['up_to_date'], 3)

    def test_promoted_marker_wins(self):
        plan = _plan(_s('a', 'ПО 25-1', '1', promoted='2026'))       # метка этого года — не переводим повторно
        self.assertEqual(plan['moves'], [])
        self.assertEqual(plan['counts']['up_to_date'], 1)
        plan = _plan(_s('a', 'ПО 25-1', '1', promoted='2025'))       # метка прошлого года — не мешает
        self.assertEqual(len(plan['moves']), 1)

    def test_graduates_are_not_promoted(self):
        plan = _plan(_s('a', 'ПО 22-1', '4'), _s('b', 'ПО 21-1', '4'))
        self.assertEqual(plan['moves'], [])
        self.assertEqual(plan['counts']['graduated'], 2)

    def test_mismatches_go_to_review_and_are_not_changed(self):
        plan = _plan(_s('behind', 'ПО 24-1', '1'),     # отстаёт на 2: +1 дал бы 2, а группа на 3-м
                     _s('ahead', 'ПО 25-1', '3'),      # опережает группу
                     _s('nocourse', 'ПО 25-1', ''),
                     _s('badcourse', 'ПО 25-1', 'abc'),
                     _s('nogroup', '', '2'),
                     _s('weirdgroup', 'Особая', '2'))
        self.assertEqual(plan['moves'], [])
        reasons = {r['id']: r['reason'] for r in plan['review']}
        self.assertEqual(reasons, {'behind': 'behind', 'ahead': 'ahead', 'nocourse': 'no_course',
                                   'badcourse': 'no_course', 'nogroup': 'no_group', 'weirdgroup': 'no_group'})
        self.assertEqual({r['id']: r['expected'] for r in plan['review'] if r['expected']},
                         {'behind': 3, 'ahead': 2, 'nocourse': 2, 'badcourse': 2})

    def test_counts_add_up(self):
        plan = _plan(_s('1', 'ПО 26-1', '1'), _s('2', 'ПО 25-1', '1'), _s('3', 'ПО 25-1', '2'), _s('4', 'ПО 22-1', '4'),
                     _s('5', 'ПО 24-1', '1'), _s('6', '', '1'))
        c = plan['counts']
        self.assertEqual(c['total'], 6)
        self.assertEqual(c['move'] + c['up_to_date'] + c['first_year'] + c['graduated'] + c['review'], 6)

    def test_start_year_switches_in_september(self):
        from datetime import date
        with mock.patch('cms.routes_promote.today_almaty', return_value=date(2026, 8, 31)):
            self.assertEqual(start_year_now(), 2025)
        with mock.patch('cms.routes_promote.today_almaty', return_value=date(2026, 9, 1)):
            self.assertEqual(start_year_now(), 2026)
        with mock.patch('cms.routes_promote.today_almaty', return_value=date(2027, 1, 15)):
            self.assertEqual(start_year_now(), 2026)


class PromotePagesTest(CmsTestCase):
    fresh_seed_each_test = True

    def setUp(self):
        super().setUp()
        patcher = mock.patch('cms.routes_promote.start_year_now', return_value=START)
        patcher.start()
        self.addCleanup(patcher.stop)
        from cms.core import db
        users = db.collection('users')
        batch = db.batch()
        for d in users.where('role', '==', 'student').stream():      # убираем сидовых студентов — считаем только своих
            batch.delete(d.reference)
        batch.commit()
        rows = {
            'pc_new': ('Новичок Первый', 'ПО 26-1', '1'),          # первый курс — не трогаем
            'pc_a': ('Айгуль Вторая', 'ПО 25-1', '1'),            # +1
            'pc_b': ('Бекзат Третий', 'ПО 24-1', '2'),            # +1
            'pc_c': ('Данияр Четвёртый', 'ПО 23-1', '3'),         # +1
            'pc_done': ('Уже Переведён', 'ПО 25-2', '2'),         # уже на своём курсе
            'pc_grad': ('Выпускник Бывший', 'ПО 22-1', '4'),      # выпуск
            'pc_odd': ('Особый Случай', 'ПО 24-2', '1'),          # отстаёт на 2 → вручную
        }
        batch = db.batch()
        for uid, (name, group, course) in rows.items():
            batch.set(users.document(uid), {'role': 'student', 'name': name, 'group': group, 'course': course,
                                            'email': uid + '@caspian.test'})
        batch.commit()

    def _courses(self):
        return {uid: self.doc('users', uid)['course'] for uid in
                ('pc_new', 'pc_a', 'pc_b', 'pc_c', 'pc_done', 'pc_grad', 'pc_odd')}

    def test_preview_shows_plan_and_changes_nothing(self):
        html = self.get_ok('/promote-courses')
        self.assertIn('Перевод на следующий курс', html)
        self.assertIn('ПО 25-1', html)
        self.assertIn('Перевести 3 студента', html)
        self.assertIn('Особый Случай', html)                     # в «проверить вручную»
        self.assertNotIn('Новичок Первый', html)
        self.assertEqual(self._courses(), {'pc_new': '1', 'pc_a': '1', 'pc_b': '2', 'pc_c': '3', 'pc_done': '2',
                                           'pc_grad': '4', 'pc_odd': '1'})

    def test_csv_backup_lists_only_moves(self):
        r = self.client.get('/promote-courses?format=csv')
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True)
        self.assertIn('Айгуль Вторая', text)
        self.assertIn('pc_c', text)
        self.assertNotIn('Новичок Первый', text)
        self.assertNotIn('Особый Случай', text)

    def test_apply_moves_plus_one_and_marks(self):
        r = self.post('/promote-courses/apply', {'expected': '3', 'year': str(START)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._courses(), {'pc_new': '1', 'pc_a': '2', 'pc_b': '3', 'pc_c': '4', 'pc_done': '2',
                                           'pc_grad': '4', 'pc_odd': '1'})
        self.assertEqual(self.doc('users', 'pc_a')['coursePromoted'], str(START))
        self.assertNotIn('coursePromoted', self.doc('users', 'pc_new'))
        self.assertNotIn('coursePromoted', self.doc('users', 'pc_odd'))
        entries = self.audit_entries('promote')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['details']['moved'], 3)

    def test_apply_is_idempotent(self):
        self.post('/promote-courses/apply', {'expected': '3', 'year': str(START)})
        before = self._courses()
        r = self.post('/promote-courses/apply', {'expected': '0', 'year': str(START)})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._courses(), before)               # второй запуск ничего не двигает
        self.assertEqual(len(self.audit_entries('promote')), 1)

    def test_apply_refuses_when_preview_is_stale(self):
        r = self.post('/promote-courses/apply', {'expected': '2', 'year': str(START)})     # админ видел 2, сейчас 3
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._courses()['pc_a'], '1')
        self.assertTrue(any('изменились' in m for _c, m in self.flashes()))
        r = self.post('/promote-courses/apply', {'expected': '3', 'year': str(START - 1)})  # другой учебный год
        self.assertEqual(self._courses()['pc_a'], '1')
        r = self.post('/promote-courses/apply', {'expected': 'x', 'year': 'y'})
        self.assertEqual(self._courses()['pc_a'], '1')
        self.assertEqual(self.audit_entries('promote'), [])

    def test_rollback_restores_only_marked(self):
        self.post('/promote-courses/apply', {'expected': '3', 'year': str(START)})
        r = self.post('/promote-courses/rollback')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self._courses(), {'pc_new': '1', 'pc_a': '1', 'pc_b': '2', 'pc_c': '3', 'pc_done': '2',
                                           'pc_grad': '4', 'pc_odd': '1'})
        self.assertNotIn('coursePromoted', self.doc('users', 'pc_a'))
        self.assertEqual(self.audit_entries('promote_rollback')[0]['details']['restored'], 3)
        # после отката план снова предлагает тех же трёх
        self.assertIn('Перевести 3 студента', self.get_ok('/promote-courses'))

    def test_rollback_with_nothing_marked(self):
        r = self.post('/promote-courses/rollback')
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.audit_entries('promote_rollback'), [])

    def test_needs_csrf_and_admin(self):
        r = self.client.post('/promote-courses/apply', data={'expected': '3', 'year': str(START)})
        self.assertIn(r.status_code, (302, 400, 403))
        self.assertEqual(self._courses()['pc_a'], '1')
        self.logout()
        r = self.client.get('/promote-courses')
        self.assertIn(r.status_code, (302, 401, 403))
        r = self.client.post('/promote-courses/apply', data={'expected': '3', 'year': str(START)})
        self.assertIn(r.status_code, (302, 400, 401, 403))
        self.assertEqual(self._courses()['pc_a'], '1')

    def test_empty_state(self):
        self.post('/promote-courses/apply', {'expected': '3', 'year': str(START)})
        html = self.get_ok('/promote-courses')
        self.assertIn('Переводить некого', html)
        self.assertIn('Отменить перевод', html)                  # откат доступен после перевода


if __name__ == '__main__':
    unittest.main()
