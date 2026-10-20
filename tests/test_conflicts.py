# tests/test_conflicts.py — движок накладок и страницы «Аудиторный фонд | Накладки | Свободные аудитории»
# (владелец — CONFLICTS).
#
# Движок (cms/conflicts.py) — чистый Python: классы EngineTest… работают без эмулятора
#   python -m unittest tests.test_conflicts
# Страницы, принятие накладок, исправление данных и экспорт — на эмуляторе:
#   tests/run_tests.sh 18600 tests.test_conflicts

import io
import json
import os
import random
import time
import unittest
import zipfile
from datetime import date, datetime, timezone

from cms import conflicts as cf
from cms.schedule_model import CALL_SCHEDULE, TeacherIndex, current_week_type, group_key, normalize_lesson

USERS = {
    't_petrov': 'Петров Дмитрий Игоревич',
    't_smirnova': 'Смирнова Елена Викторовна',
    't_kim': 'Ким Виктор Александрович',
    't_kim2': 'Ким Виктор Александрович',   # полный тёзка
    't_li': 'Ли Сергей Николаевич',
    't_akh': 'Ахметова Динара Маратовна',
    't_nur': 'Нурланов Арман Серикович',
}
LEGACY = {'legacy_t1': 'Старостин Пётр Иванович'}
INDEX = TeacherIndex(USERS, LEGACY)

MON_NUM = '2026-10-12'   # понедельник, 7-я неделя — числитель
MON_DEN = '2026-10-19'   # понедельник, 8-я неделя — знаменатель
WED = '2026-10-21'


def L(doc_id, group='ПО 24-1', day=2, time='13.00-13.50', teacher='t_petrov', subject='Физика', room='204',
      week='always', **extra):
    """Нормализованное занятие (как в снапшоте). day=None — без day_index (тогда задайте date)."""
    data = {'groupId': group, 'time': time, 'teacher': teacher, 'subject': subject, 'room': room, 'week_type': week}
    if day is not None:
        data['day_index'] = day
    data.update(extra)
    return normalize_lesson(doc_id, data, INDEX)


def clusters(lessons, dim, **kw):
    return cf.analyze(lessons, **kw).clusters[dim]


def ids_of(c):
    return sorted(c['ids'])


class PairRulesTest(unittest.TestCase):

    def test_week_compatibility(self):
        a, n, d = L('a'), L('n', week='numerator'), L('d', week='denominator')
        self.assertEqual(cf.pair_weeks(a, L('a2')), 'every')
        self.assertEqual(cf.pair_weeks(a, n), 'numerator')
        self.assertEqual(cf.pair_weeks(d, a), 'denominator')
        self.assertEqual(cf.pair_weeks(n, L('n2', week='numerator')), 'numerator')
        self.assertIsNone(cf.pair_weeks(n, d))
        # русские/сокращённые значения нормализуются моделью
        self.assertEqual(cf.pair_weeks(L('x', week='Числитель'), d), None)

    def test_numerator_vs_denominator_is_not_conflict(self):
        lessons = [L('n', group='ПО 24-1', week='numerator'), L('d', group='ВТ 24-1', week='denominator', room='205')]
        self.assertEqual(clusters(lessons, 'teacher'), [])

    def test_always_vs_numerator_conflicts_only_in_numerator(self):
        lessons = [L('a', group='ПО 24-2', room='102'), L('n', group='ЭК 24-1', room='104', week='numerator')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(c['weeks'], ['numerator'])
        self.assertEqual(c['weeks_label'], 'Только числитель')
        self.assertIn('alternating', c['pairs'][0]['flags'])
        self.assertTrue(cf.occurs_in(c['weeks'], 'numerator'))
        self.assertFalse(cf.occurs_in(c['weeks'], 'denominator'))

    def test_touching_is_not_overlap(self):
        lessons = [L('a', time='10.55-11.45'), L('b', group='ВТ 24-1', time='11.45-12.05', room='205')]
        self.assertEqual(clusters(lessons, 'teacher'), [])

    def test_partial_overlap_and_time_formats(self):
        lessons = [L('a', group='ПО 23-1', time='14.05-14.55', room='301'),
                   L('b', group='ИС 23-1', time='14:30-14:50', room='303')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(c['kind'], 'teacher_double')
        self.assertEqual(c['severity'], 'critical')
        self.assertEqual(c['overlap_min'], 20)
        self.assertIn('partial', c['pairs'][0]['flags'])
        self.assertIn('different_rooms', c['pairs'][0]['flags'])
        self.assertEqual((c['start'], c['end']), (14 * 60 + 5, 14 * 60 + 55))

    def test_different_days_do_not_conflict(self):
        self.assertEqual(clusters([L('a', day=1), L('b', day=2, group='ВТ 24-1')], 'teacher'), [])

    def test_transitive_cluster_union_find(self):
        # A пересекается с B, B с C, A с C — нет: одна карточка из трёх занятий
        lessons = [L('a', group='Г1', time='08.00-09.00', room='1'), L('b', group='Г2', time='08.50-09.40', room='2'),
                   L('c', group='Г3', time='09.30-10.20', room='3')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(ids_of(c), ['a', 'b', 'c'])
        self.assertEqual(len(c['pairs']), 2)


class KindsTest(unittest.TestCase):

    def test_stream_is_info(self):
        lessons = [L('a', group='ВТ 24-1', teacher='t_kim', subject='Физкультура', room='Спортзал', day=0),
                   L('b', group='ВТ 25-1п', teacher='t_kim', subject='Физкультура', room='спортзал', day=0)]
        [t] = clusters(lessons, 'teacher')
        [r] = clusters(lessons, 'room')
        self.assertEqual((t['kind'], t['severity']), ('stream', 'info'))
        self.assertEqual((r['kind'], r['severity']), ('stream', 'info'))
        self.assertEqual(t['hash'], r['hash'])  # один набор занятий — один хэш на всех вкладках
        s = cf.summarize(cf.analyze(lessons))
        self.assertEqual((s['critical'], s['warning'], s['info'], s['teachers']), (0, 0, 1, 0))

    def test_stream_with_different_subject_is_teacher_double(self):
        lessons = [L('a', group='ВТ 24-1', subject='Физика'), L('b', group='ВТ 25-1', subject='Химия')]
        [t] = clusters(lessons, 'teacher')
        self.assertEqual(t['kind'], 'teacher_double')
        self.assertNotIn('different_rooms', t['pairs'][0]['flags'])

    def test_duplicate_counted_once(self):
        lessons = [L('a', group='ЭК 24-1', day=1), L('b', group='ЭК 24-1', day=1)]
        an = cf.analyze(lessons)
        for dim in cf.DIMS:
            [c] = an.clusters[dim]
            self.assertEqual((c['kind'], c['severity']), ('duplicate', 'warning'), dim)
        s = cf.summarize(an)
        self.assertEqual((s['critical'], s['warning']), (0, 1))

    def test_group_overlap_and_subgroups(self):
        sub = [L('a', group='ПО 25-1', day=3, time='15.00-15.50', teacher='t_smirnova', subject='Английский', room='103'),
               L('b', group='ПО 25-1', day=3, time='15.00-15.50', teacher='t_nur', subject='Английский', room='104')]
        [c] = clusters(sub, 'group')
        self.assertEqual((c['kind'], c['severity']), ('subgroups', 'warning'))
        clash = [L('a', group='ИС 23-1', day=4, time='13.00-13.50', teacher='t_li', subject='Сети', room='А-12'),
                 L('b', group='ИС 23-1', day=4, time='13.30-14.20', teacher='t_akh', subject='Физика', room='301')]
        [c] = clusters(clash, 'group')
        self.assertEqual((c['kind'], c['severity']), ('group_overlap', 'critical'))
        self.assertEqual(c['entity'], 'ИС 23-1')
        self.assertEqual(c['overlap_min'], 20)

    def test_room_double_booking_with_normalization(self):
        lessons = [L('a', group='ИС 24-1', teacher='t_nur', room='204'),
                   L('b', group='ПО 25-1', teacher='t_smirnova', room='Каб. 204'),
                   L('c', group='ВТ 24-1', teacher='t_akh', room='ауд. 204', time='13.30-14.20')]
        [c] = clusters(lessons, 'room')
        self.assertEqual(ids_of(c), ['a', 'b', 'c'])
        self.assertEqual((c['kind'], c['severity']), ('room_double', 'critical'))
        self.assertEqual(c['entity'], '204')  # короткое название
        # латиница-двойник: «A-12» (лат.) и «А-12» (кир.) — одна аудитория
        lat = [L('x', group='Г1', teacher='t_li', room='A-12'), L('y', group='Г2', teacher='t_nur', room='А 12')]
        self.assertEqual(len(clusters(lat, 'room')), 1)

    def test_virtual_and_empty_rooms_are_skipped(self):
        lessons = [L('a', group='Г1', teacher='t_li', room='онлайн'), L('b', group='Г2', teacher='t_nur', room='Онлайн'),
                   L('c', group='Г3', teacher='t_akh', room=''), L('d', group='Г4', teacher='t_kim', room='-')]
        self.assertEqual(clusters(lessons, 'room'), [])


class TeacherIdentityTest(unittest.TestCase):

    def test_text_name_matches_uid(self):
        lessons = [L('uid', group='ПО 25-1', teacher='t_smirnova', room='204'),
                   L('fio', group='ЭК 25-1', teacher='Смирнова  Елена Викторовна', room='103')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(c['key'], 'uid:t_smirnova')
        self.assertEqual(c['severity'], 'critical')
        self.assertEqual(c['entity'], 'Смирнова Елена Викторовна')
        q = [x for x in cf.analyze(lessons).quality if x['code'] == 'teacher_by_name']
        self.assertEqual([x['lesson']['id'] for x in q], ['fio'])
        self.assertTrue(q[0]['fixable'])
        self.assertEqual(cf.fix_for('teacher_by_name', lessons[1]), {'teacher': 't_smirnova'})

    def test_namesakes_are_downgraded(self):
        lessons = [L('a', group='МЕН 25-1', teacher='Ким Виктор Александрович', room='201'),
                   L('b', group='ПО 24-1', teacher='ким виктор александрович', room='202')]
        [c] = clusters(lessons, 'teacher')
        self.assertTrue(c['key'].startswith('name:'))
        self.assertEqual(c['severity'], 'warning')   # неясно, один ли это человек
        self.assertTrue(c['uncertain'])
        codes = {x['code'] for x in cf.analyze(lessons).quality}
        self.assertIn('teacher_ambiguous', codes)
        self.assertIsNone(cf.fix_for('teacher_by_name', lessons[0]))

    def test_uid_and_unknown_text_are_different_people(self):
        lessons = [L('a', teacher='t_petrov'), L('b', group='ВТ 24-1', teacher='Петров Д. И.', room='205')]
        self.assertEqual(clusters(lessons, 'teacher'), [])  # ФИО не из справочника — не сопоставляем наугад


class DaysAndDatesTest(unittest.TestCase):

    def test_dated_vs_recurring_same_weekday(self):
        # «только date» приложение показывает каждую неделю — сравниваем по дню недели
        lessons = [L('rec', group='ПО 23-1', day=0, teacher='t_li', room='А-12', subject='Сети'),
                   L('dated', group='ИС 23-1', day=None, date=MON_DEN, teacher='t_li', room='А-12', subject='Сети')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(ids_of(c), ['dated', 'rec'])
        self.assertEqual(c['weekday'], 0)
        # две разные даты-понедельника (созданы в разные недели) — тоже одна и та же пара
        old = [L('d1', group='Г1', day=None, date=MON_NUM, room='1'), L('d2', group='Г2', day=None, date=MON_DEN, room='2')]
        self.assertEqual(len(clusters(old, 'teacher')), 1)

    def test_day_index_has_priority_over_date(self):
        lessons = [L('a', group='Г1', day=1, date=WED, room='1'), L('b', group='Г2', day=1, room='2'),
                   L('c', group='Г3', day=2, room='3')]
        [c] = clusters(lessons, 'teacher')
        self.assertEqual(ids_of(c), ['a', 'b'])
        q = [x for x in cf.analyze(lessons).quality if x['code'] == 'day_mismatch']
        self.assertEqual([x['lesson']['id'] for x in q], ['a'])
        fix = cf.fix_for('day_mismatch', lessons[0])
        self.assertEqual(fix['day_index'], 1)
        self.assertEqual(datetime.strptime(fix['date'], '%Y-%m-%d').weekday(), 1)

    def test_one_off_lesson_uses_its_date_parity(self):
        self.assertEqual(current_week_type(date(2026, 10, 12)), 'numerator')
        once = L('once', group='Г1', day=None, date=MON_NUM, one_off=True, room='1')
        den = L('den', group='Г2', day=0, week='denominator', room='2')
        num = L('num', group='Г3', day=0, week='numerator', room='3')
        self.assertIsNone(cf.pair_weeks(once, den))
        self.assertEqual(cf.pair_weeks(once, num), 'date:' + MON_NUM)
        [c] = clusters([once, den, num], 'teacher')
        self.assertEqual(ids_of(c), ['num', 'once'])
        self.assertTrue(c['weeks_label'].startswith('Разово: 12.10'))
        # разовое × разовое — только в одну дату
        other_day = L('once2', group='Г4', day=None, date=MON_DEN, one_off=True, room='4')
        same_day = L('once3', group='Г5', day=None, date=MON_NUM, oneOff=True, room='5')
        self.assertIsNone(cf.pair_weeks(once, other_day))
        self.assertEqual(cf.pair_weeks(once, same_day), 'date:' + MON_NUM)
        # «идёт на неделе даты»
        self.assertTrue(cf.occurs_in(['date:' + MON_NUM], week_of=date(2026, 10, 14)))
        self.assertFalse(cf.occurs_in(['date:' + MON_NUM], week_of=date(2026, 10, 19)))
        self.assertTrue(cf.occurs_in(['date:' + MON_NUM], 'numerator'))

    def test_uncertain_data_downgrades_critical(self):
        no_end = [L('a', group='Г1', time='08.00', day=4, room='1'), L('b', group='Г2', time='08.00-08.50', day=4, room='2')]
        [c] = clusters(no_end, 'teacher')
        self.assertEqual(c['severity'], 'warning')   # конец угадан по сетке (08.00-08.50)
        self.assertEqual(c['lessons'][0]['end'], 8 * 60 + 50)
        odd = [L('a', group='Г1', week='odd', room='1'), L('b', group='Г2', room='2')]
        [c] = clusters(odd, 'teacher')
        self.assertEqual(c['severity'], 'warning')
        self.assertIn('week_unknown', {x['code'] for x in cf.analyze(odd).quality})


class QualityTest(unittest.TestCase):

    def codes(self, lesson, **kw):
        return cf.quality_issues(lesson, **kw)

    def test_codes(self):
        groups = {group_key('ПО 24-1')}
        subjects = {'физика'}
        cases = {
            'no_day': L('x', day=None),
            'bad_time': L('x', time='после обеда'),
            'reverse_time': L('x', time='10.50-10.00'),
            'sunday': L('x', day=6, time='10.00-10.50'),
            'off_hours': L('x', time='21.30-22.20'),
            'off_grid': L('x', time='09:00-10:20'),
            'end_assumed': L('x', time='08.00'),
            'no_teacher': L('x', teacher=''),
            'teacher_orphan': L('x', teacher='uid_deleted_7'),
            'teacher_legacy': L('x', teacher='legacy_t1'),
            'teacher_unknown': L('x', teacher='Иванов Иван Иванович'),
            'no_room': L('x', room=''),
            'week_unknown': L('x', week='odd'),
            'no_subject': L('x', subject=''),
            'unknown_subject': L('x', subject='Черчение'),
            'unknown_group': L('x', group='XX 99-9'),
            'no_group': L('x', group=''),
        }
        for code, lesson in cases.items():
            with self.subTest(code=code):
                self.assertIn(code, self.codes(lesson, known_groups=groups, known_subjects=subjects))
        ts = normalize_lesson('ts', {'groupId': 'ПО 24-1', 'time': '15.55-16.45', 'teacher': 't_kim', 'room': '1',
                                     'subject': 'Физика', 'date': datetime(2026, 10, 20, tzinfo=timezone.utc)}, INDEX)
        self.assertIn('date_timestamp', self.codes(ts))
        self.assertEqual(cf.fix_for('date_timestamp', ts)['day_index'], 1)
        # чистое занятие из сетки — без замечаний
        self.assertEqual(self.codes(L('ok', time='13.00-13.50', day=2), known_groups=groups, known_subjects=subjects), [])

    def test_bad_lessons_do_not_take_part_in_conflicts(self):
        lessons = [L('a', time='после обеда'), L('b', group='ВТ 24-1', time='10.50-10.00'), L('c', day=None),
                   L('ok1', group='Г1', room='1'), L('ok2', group='Г2', room='2')]
        an = cf.analyze(lessons)
        self.assertEqual(an.lessons_total, 5)
        self.assertEqual(an.lessons_checked, 2)
        [c] = an.clusters['teacher']
        self.assertEqual(ids_of(c), ['ok1', 'ok2'])


class HashAndSummaryTest(unittest.TestCase):

    def test_hash_changes_when_lesson_changes(self):
        base = [L('a', group='Г1', room='1'), L('b', group='Г2', room='2')]
        [c1] = clusters(base, 'teacher')
        [c2] = clusters(list(reversed(base)), 'teacher')
        self.assertEqual(c1['hash'], c2['hash'])            # порядок не важен
        moved = [L('a', group='Г1', room='1', time='13.10-14.00'), L('b', group='Г2', room='2')]
        [c3] = clusters(moved, 'teacher')
        self.assertNotEqual(c1['hash'], c3['hash'])         # время изменилось — накладка «всплывает»
        other_room = [L('a', group='Г1', room='7'), L('b', group='Г2', room='2')]
        self.assertNotEqual(c1['hash'], clusters(other_room, 'teacher')[0]['hash'])
        q1 = cf.quality_hash('no_room', L('a', room=''))
        self.assertNotEqual(q1, cf.quality_hash('no_room', L('a', room='', time='08.00-08.50')))

    def test_summary_excludes_accepted(self):
        lessons = [L('a', group='Г1', room='1'), L('b', group='Г2', room='2'),               # преподаватель
                   L('c', group='Г3', teacher='t_li', room='9', day=3), L('d', group='Г4', teacher='t_nur', room='9', day=3)]
        an = cf.analyze(lessons)
        s = cf.summarize(an)
        self.assertEqual((s['critical'], s['teachers']), (2, 1))
        self.assertEqual(s['by_dim']['teacher']['critical'], 1)
        self.assertEqual(s['by_dim']['room']['critical'], 1)
        h = an.clusters['teacher'][0]['hash']
        s2 = cf.summarize(an, [h])
        self.assertEqual((s2['critical'], s2['teachers'], s2['accepted']), (1, 0, 1))

    def test_summary_contract_keys(self):
        s = cf.summarize(cf.analyze([]))
        for key in ('critical', 'warning', 'teachers'):
            self.assertIsInstance(s[key], int)


class CheckLessonTest(unittest.TestCase):
    """find_lesson_conflicts — основа check_lesson() для формы занятия (SCHEDULE)."""

    def setUp(self):
        self.day = [L('p1', group='ВТ 24-1', room='204', subject='Python'),
                    L('p2', group='МЕН 25-1', room='205', subject='Web', time='13:00-13:50'),
                    L('r1', group='ИС 24-1', teacher='t_nur', room='Каб. 206', subject='БД'),
                    L('n1', group='ЭК 24-1', teacher='t_li', room='301', week='numerator'),
                    L('later', group='ЭК 25-1', room='204', time='14.00-14.50')]

    def test_candidate_conflicts(self):
        cand = L('new', group='ЭК 24-1', room='206', subject='Физика', week='denominator')
        out = cf.find_lesson_conflicts(cand, self.day)
        kinds = {(x['other_id'], x['kind']) for x in out}
        self.assertEqual(kinds, {('p1', 'teacher_double'), ('p2', 'teacher_double'), ('r1', 'room_double')})
        self.assertTrue(all(x['severity'] == 'critical' for x in out))
        msg = {x['other_id']: x['message'] for x in out}
        self.assertIn('Петров Д. И. уже ведёт пару у ВТ 24-1', msg['p1'])
        self.assertIn('Аудитория Каб. 206 занята', msg['r1'])
        self.assertEqual(out[0]['ids'], [out[0]['other_id']])
        self.assertIn('room', next(x for x in out if x['other_id'] == 'r1')['dims'])

    def test_exclude_and_weeks(self):
        cand = L('p1', group='ВТ 24-1', room='204', subject='Python')   # правка самого p1
        out = cf.find_lesson_conflicts(cand, self.day, exclude_ids=['p1'])
        self.assertEqual({x['other_id'] for x in out}, {'p2'})
        # группа ЭК 24-1 по числителю занята (n1): кандидат-знаменатель не мешает, «каждую неделю» — мешает
        g_den = L('g', group='ЭК 24-1', teacher='t_akh', room='777', week='denominator')
        self.assertEqual(cf.find_lesson_conflicts(g_den, self.day), [])
        g_all = L('g', group='ЭК 24-1', teacher='t_akh', room='777')
        [x] = cf.find_lesson_conflicts(g_all, self.day)       # то же время, другой преподаватель — подгруппы?
        self.assertEqual((x['kind'], x['weeks'], x['severity']), ('subgroups', 'numerator', 'warning'))
        self.assertTrue(x['message'].endswith('по числителю'))
        g_part = L('g', group='ЭК 24-1', teacher='t_akh', room='777', time='13.20-14.10')
        [x] = cf.find_lesson_conflicts(g_part, self.day)
        self.assertEqual((x['kind'], x['severity'], x['overlap_min']), ('group_overlap', 'critical', 30))

    def test_unusable_candidate(self):
        self.assertEqual(cf.find_lesson_conflicts(L('x', time='когда-нибудь'), self.day), [])
        self.assertEqual(cf.find_lesson_conflicts(L('x', day=None), self.day), [])


class PerformanceTest(unittest.TestCase):

    def test_5000_lessons_under_a_second(self):
        rnd = random.Random(42)
        teachers = {'t%03d' % i: 'Преподаватель %03d Тестович' % i for i in range(160)}
        index = TeacherIndex(teachers)
        groups = ['ГР %02d-%d' % (20 + i // 10, i % 10) for i in range(120)]
        rooms = [str(100 + i) for i in range(130)] + ['Каб. %d' % (100 + i) for i in range(10)]
        raw = []
        for i in range(5000):
            wd = rnd.randrange(6)
            slot = rnd.choice(CALL_SCHEDULE['monday' if wd == 0 else 'other'])
            raw.append(('l%05d' % i, {
                'groupId': rnd.choice(groups), 'day_index': wd, 'time': slot,
                'teacher': rnd.choice(list(teachers)), 'subject': 'Дисциплина %d' % rnd.randrange(40),
                'room': rnd.choice(rooms), 'week_type': rnd.choice(['always', 'always', 'numerator', 'denominator'])}))
        lessons = [normalize_lesson(i, d, index) for i, d in raw]
        started = time.perf_counter()
        an = cf.analyze(lessons, known_groups=groups, known_subjects=['Дисциплина %d' % i for i in range(40)])
        elapsed = time.perf_counter() - started
        self.assertEqual(an.lessons_total, 5000)
        self.assertGreater(sum(len(v) for v in an.clusters.values()), 0)
        self.assertLess(elapsed, 1.0, 'анализ 5000 занятий занял %.2f с' % elapsed)


class HelpersTest(unittest.TestCase):

    def test_short_name_and_labels(self):
        self.assertEqual(cf.short_name('Петров Дмитрий Игоревич'), 'Петров Д. И.')
        self.assertEqual(cf.short_name('Ли'), 'Ли')
        self.assertEqual(cf.weeks_label(['numerator', 'denominator']), 'Числитель и знаменатель')
        self.assertEqual(cf.weeks_label(['every', 'numerator']), 'Каждую неделю')
        self.assertEqual(cf.weeks_label([]), '—')


# ==================== СТРАНИЦЫ (эмулятор) ====================

if os.environ.get('FIRESTORE_EMULATOR_HOST'):
    from tests.base import CmsTestCase  # noqa: E402

    def _analysis():
        from cms import conflicts
        return conflicts.get_analysis(force=True)

    def _find(analysis, dim, *ids):
        """Кластер измерения dim, содержащий все ids."""
        for c in analysis.clusters[dim]:
            if set(ids) <= set(c['ids']):
                return c
        return None

    class SeedCasesTest(CmsTestCase):
        """Заложенные в сид накладки (tests.seed.CONFLICT_CASES) находятся движком на реальных данных."""

        def test_cases(self):
            an = _analysis()
            c = _find(an, 'teacher', 'cf_petrov_a', 'cf_petrov_b')
            self.assertEqual((c['kind'], c['severity']), ('teacher_double', 'critical'))
            c = _find(an, 'teacher', 'cf_akhm_a', 'cf_akhm_b')
            self.assertIn('partial', c['pairs'][0]['flags'])
            c = _find(an, 'teacher', 'cf_seit_always', 'cf_seit_num')
            self.assertEqual(c['weeks'], ['numerator'])
            self.assertIsNone(_find(an, 'teacher', 'ok_omar_num', 'ok_omar_den'))
            self.assertIsNone(_find(an, 'room', 'ok_omar_num', 'ok_omar_den'))
            self.assertEqual(_find(an, 'teacher', 'cf_stream_a', 'cf_stream_b')['severity'], 'info')
            self.assertEqual(_find(an, 'group', 'cf_dup_a', 'cf_dup_b')['kind'], 'duplicate')
            room = _find(an, 'room', 'cf_room_a', 'cf_room_b')
            self.assertEqual((room['kind'], room['severity']), ('room_double', 'critical'))
            self.assertIn('cf_petrov_a', room['ids'])          # «204» и «Каб. 204» — одна аудитория
            self.assertEqual(_find(an, 'teacher', 'cf_byname', 'cf_room_b')['key'], 'uid:t_smirnova')
            self.assertEqual(_find(an, 'group', 'cf_grp_sub_a', 'cf_grp_sub_b')['kind'], 'subgroups')
            self.assertEqual(_find(an, 'group', 'cf_grp_a', 'cf_grp_b')['severity'], 'critical')
            self.assertIsNotNone(_find(an, 'teacher', 'cf_dated_a', 'cf_dated_b'))
            codes = {}
            for q in an.quality:
                codes.setdefault(q['lesson']['id'], set()).add(q['code'])
            expect = {'q_ambiguous': 'teacher_ambiguous', 'q_orphan': 'teacher_orphan', 'q_legacy': 'teacher_legacy',
                      'q_bad_time': 'bad_time', 'q_reverse': 'reverse_time', 'q_mismatch': 'day_mismatch',
                      'q_sunday': 'sunday', 'q_no_teacher': 'no_teacher', 'q_no_room': 'no_room',
                      'q_bad_week': 'week_unknown', 'q_unknown_group': 'unknown_group', 'q_ts_date': 'date_timestamp',
                      'q_no_day': 'no_day', 'q_late': 'off_hours', 'q_offgrid': 'off_grid', 'q_no_end': 'end_assumed',
                      'cf_byname': 'teacher_by_name'}
            for lesson_id, code in expect.items():
                with self.subTest(lesson=lesson_id):
                    self.assertIn(code, codes.get(lesson_id, set()))
            # базовая сетка (латинский квадрат) без накладок преподавателей
            for c in an.clusters['teacher']:
                self.assertFalse(all(i.startswith('base_') for i in c['ids']), c['ids'])

        def test_public_api_contract(self):
            from cms import conflicts
            s = conflicts.summary()
            self.assertGreaterEqual(s['critical'], 5)
            self.assertGreaterEqual(s['teachers'], 4)
            for key in ('critical', 'warning', 'teachers'):
                self.assertIsInstance(s[key], int)
            out = conflicts.check_lesson({'groupId': 'ЭК 24-1', 'day_index': 2, 'date': WED, 'time': '13.00-13.50',
                                          'teacher': 't_petrov', 'room': '204', 'subject': 'Python',
                                          'week_type': 'always'})
            others = {x['other_id'] for x in out if x['severity'] == 'critical'}
            self.assertTrue({'cf_petrov_a', 'cf_petrov_b'} <= others, out)
            self.assertTrue(all(x['message'] for x in out))
            excl = conflicts.check_lesson(dict(id='cf_petrov_a', groupId='ВТ 24-1', day_index=2, time='13.00-13.50',
                                               teacher='t_petrov', room='204', subject='Программирование на Python'),
                                          exclude_id='cf_petrov_a')
            self.assertNotIn('cf_petrov_a', {x['other_id'] for x in excl})
            self.assertIn('cf_petrov_b', {x['other_id'] for x in excl})
            self.assertEqual(conflicts.check_lesson(None), [])
            self.assertEqual(conflicts.check_lesson({'groupId': 'X', 'time': '?'}), [])

        def test_sidebar_badge_and_dashboard(self):
            html = self.get_ok('/schedule/rooms')
            self.assertIn('cc-nav-badge--danger', html)

    class PagesTest(CmsTestCase):

        def test_rooms_page(self):
            html = self.get_ok('/schedule/rooms')
            self.assertIn('Аудиторный фонд', html)
            self.assertIn('Свободные аудитории', html)   # вкладки
            self.assertIn('rm-matrix', html)              # по умолчанию — шахматка дня (SCH-13)
            self.assertIn('rm-card', self.get_ok('/schedule/rooms?view=cards'))
            self.get_ok('/schedule/rooms?view=table')
            self.get_ok('/schedule/rooms?view=matrix&day=2&week=numerator')
            single = self.get_ok('/schedule/rooms?room=%D0%9A%D0%B0%D0%B1.%20204')   # «Каб. 204» → та же «204»
            self.assertIn('rm-grid--single', single)
            self.assertIn('ВТ 24-1', single)
            self.get_ok('/schedule/rooms?day=2&week=numerator&q=%D0%9F%D0%9E')
            self.get_ok('/schedule/rooms?week_type=denominator')   # старый параметр
            empty = self.get_ok('/schedule/rooms?q=zzzz-nothing')
            self.assertIn('Аудитории не найдены', empty)

        def test_conflicts_page_tabs(self):
            html = self.get_ok('/schedule/conflicts')
            self.assertIn('Петров Дмитрий Игоревич', html)
            self.assertIn('Преподаватель ведёт две пары одновременно', html)
            self.assertIn('cfBulk', html)
            room = self.get_ok('/schedule/conflicts?dim=room')
            self.assertIn('Аудитория занята двумя парами', room)
            group = self.get_ok('/schedule/conflicts?dim=group')
            self.assertIn('Возможно, подгруппы', group)
            quality = self.get_ok('/schedule/conflicts?dim=quality')
            self.assertIn('Время не разобрать', quality)
            self.assertIn('Заменить ФИО на uid', quality)
            self.get_ok('/schedule/conflicts?dim=quality&code=teacher_by_name')
            streams = self.get_ok('/schedule/conflicts?info=1')
            self.assertIn('Поток', streams)
            # фильтры: голый uid, день/час из тепловой карты, неделя, поиск
            only = self.get_ok('/schedule/conflicts?teacher=t_petrov')
            self.assertIn('id="ent-1">Петров Дмитрий Игоревич</h2>', only)
            self.assertNotIn('id="ent-2"', only)
            self.get_ok('/schedule/conflicts?day=2&hour=13&week=numerator&severity=critical&q=%D0%92%D0%A2')
            self.get_ok('/schedule/conflicts?week=current&dim=room&room=204')
            self.get_ok('/schedule/conflicts?dim=nonsense&day=99&hour=x&severity=zzz')
            nothing = self.get_ok('/schedule/conflicts?q=zzzz-nothing')
            self.assertIn('Ничего не найдено', nothing)

        def test_free_rooms(self):
            html = self.get_ok('/schedule/free-rooms?day=2&slot=13.00-13.50')
            self.assertIn('Шахматка', html)
            from cms.routes_rooms import free_rooms, week_context
            from cms.schedule_model import load_lessons_snapshot
            snap = load_lessons_snapshot()
            free, busy = free_rooms(snap, 2, 13 * 60, 13 * 60 + 50, 'both', week_context())
            busy_names = {r['key'] for r in busy}
            self.assertIn('204', busy_names)
            self.assertNotIn('204', {r['key'] for r in free})
            self.assertTrue(free)
            # касание: пара 13.50–14.40 не мешает выбрать 13.00–13.50
            self.get_ok('/schedule/free-rooms?day=4&from=13:55&to=14:40&week=denominator&q=20')
            self.get_ok('/schedule/free-rooms?day=0&week=both')
            r = self.client.get('/api/schedule/free-rooms?day=2&from=13:00&to=13:50&week=current')
            self.assertEqual(r.status_code, 200)
            self.assertIn('204', {b['room'] for b in r.get_json()['busy']})
            self.assertEqual(self.client.get('/api/schedule/free-rooms?day=2').status_code, 400)
            self.assertEqual(self.client.get('/api/schedule/free-rooms').status_code, 200)   # по умолчанию — сейчас
            self.assertRedirectsTo(self.client.get('/schedule/conflicts/export'), '/schedule/conflicts/export?format=xlsx')

        def test_api_and_room_json(self):
            r = self.client.get('/api/schedule/conflicts?dim=teacher')
            data = r.get_json()
            self.assertTrue(data['success'])
            self.assertIn('teacher', data['clusters'])
            self.assertGreaterEqual(data['summary']['critical'], 5)
            r = self.client.get('/api/schedule/room/%D0%9A%D0%B0%D0%B1.%20204')
            self.assertEqual(r.get_json()['room_key'], '204')
            self.assertGreater(r.get_json()['total_lessons'], 5)

        def test_exports(self):
            r = self.client.get('/schedule/conflicts/export?format=csv&dim=teacher')
            self.assertEqual(r.status_code, 200)
            text = r.get_data(as_text=True)
            self.assertTrue(text.startswith('﻿'))
            self.assertIn('Петров Дмитрий Игоревич', text)
            r = self.client.get('/schedule/conflicts/export?format=csv&dim=quality')
            self.assertIn('Время не разобрать', r.get_data(as_text=True))
            r = self.client.get('/schedule/conflicts/export?format=xlsx&teacher=t_petrov')
            self.assertEqual(r.status_code, 200, r.data[:300])
            z = zipfile.ZipFile(io.BytesIO(r.data))
            self.assertIn('xl/workbook.xml', z.namelist())
            self.assertIn('Сводка', z.read('xl/workbook.xml').decode('utf-8'))
            r = self.client.get('/schedule/rooms/export?format=xlsx')
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.data.startswith(b'PK'))
            r = self.client.get('/schedule/rooms/export?format=csv&room=204')
            self.assertIn('Каб. 204', r.get_data(as_text=True))
            r = self.client.get('/schedule/rooms/export?format=pdf')
            self.assertIn(r.status_code, (200, 302))
            self.assertTrue(self.audit_entries('export'))

        def test_anonymous_and_csrf(self):
            anon = self.app.test_client()
            self.assertEqual(anon.get('/schedule/conflicts').status_code, 302)
            self.assertEqual(anon.get('/api/schedule/conflicts').status_code, 401)
            r = self.client.post('/schedule/conflicts/ack', data={'hash': 'x'})
            self.assertEqual(r.status_code, 400)

    class AcksTest(CmsTestCase):

        def test_accept_and_return(self):
            from cms import conflicts
            an = _analysis()
            c = _find(an, 'teacher', 'cf_petrov_a', 'cf_petrov_b')
            before = conflicts.summary()['critical']
            r = self.post('/schedule/conflicts/ack', {'hash': c['hash'], 'note': 'Согласовано с завучем',
                                                       'next': '/schedule/conflicts?dim=teacher'})
            self.assertRedirectsTo(r, '/schedule/conflicts')
            ack = self.doc('schedule_conflict_acks', c['hash'])
            self.assertEqual(ack['note'], 'Согласовано с завучем')
            self.assertEqual(ack['by'], 'admin@caspian.test')
            self.assertEqual(ack['dim'], 'teacher')
            self.assertTrue(self.audit_entries('conflict_ack'))
            self.assertEqual(conflicts.summary()['critical'], before - 1)
            html = self.get_ok('/schedule/conflicts?teacher=t_petrov')
            self.assertNotIn('id="c-%s"' % c['hash'], html)
            html = self.get_ok('/schedule/conflicts?teacher=t_petrov&acked=1')
            self.assertIn('id="c-%s"' % c['hash'], html)
            self.assertIn('Согласовано с завучем', html)
            r = self.post('/schedule/conflicts/unack', {'hash': c['hash']})
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(self.doc('schedule_conflict_acks', c['hash']))
            self.assertEqual(conflicts.summary()['critical'], before)
            # неизвестный хэш (страница устарела) — без записи
            r = self.post('/schedule/conflicts/ack', {'hash': 'deadbeef' * 3})
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(self.doc('schedule_conflict_acks', 'deadbeef' * 3))

        def test_bulk_accept_quality_and_stale_cleanup(self):
            an = _analysis()
            hashes = [q['hash'] for q in an.quality if q['code'] in ('sunday', 'no_room')]
            self.assertGreaterEqual(len(hashes), 2)
            r = self.post('/schedule/conflicts/ack', {'hash': hashes, 'note': 'Так и задумано'})
            self.assertEqual(r.status_code, 302)
            for h in hashes:
                self.assertEqual(self.doc('schedule_conflict_acks', h)['dim'], 'quality')
            self.assertIsNotNone(self.doc('schedule_conflict_acks', 'ack_demo'))    # устаревшая из сида
            html = self.get_ok('/schedule/conflicts')
            self.assertIn('устаревшая отметка', html)
            r = self.post('/schedule/conflicts/acks/cleanup', {})
            self.assertEqual(r.status_code, 302)
            self.assertIsNone(self.doc('schedule_conflict_acks', 'ack_demo'))
            self.assertIsNotNone(self.doc('schedule_conflict_acks', hashes[0]))

        def test_refresh(self):
            r = self.post('/schedule/conflicts/refresh', {'next': '/schedule/conflicts?dim=room'})
            self.assertRedirectsTo(r, '/schedule/conflicts?dim=room')
            r = self.post('/schedule/conflicts/refresh', {'next': 'https://evil.example/'})
            self.assertRedirectsTo(r, '/schedule/conflicts')

    class UxV2Test(CmsTestCase):
        """UX v2: «Принять» без подтверждения с «Отменить» в тосте, период, подписи пар, шахматка."""

        def test_accept_offers_undo_and_undo_returns(self):
            from cms import conflicts
            c = _find(_analysis(), 'teacher', 'cf_petrov_a', 'cf_petrov_b')
            r = self.post('/schedule/conflicts/ack', {'hash': c['hash'], 'next': '/schedule/conflicts?dim=teacher'})
            self.assertRedirectsTo(r, '/schedule/conflicts')
            flashes = self.flashes()
            self.assertIn('Накладка принята', ' '.join(t for cat, t in flashes if cat == 'success'))
            action = json.loads([t for cat, t in flashes if cat == 'cc-action'][0])
            self.assertEqual(action['label'], 'Отменить')
            self.assertEqual(action['post'], '/schedule/conflicts/unack')
            self.assertEqual(action['fields']['hashes'], c['hash'])
            self.assertIsNotNone(self.doc('schedule_conflict_acks', c['hash']))
            r = self.post(action['post'], action['fields'])          # «Отменить» из тоста
            self.assertRedirectsTo(r, '/schedule/conflicts')
            self.assertIsNone(self.doc('schedule_conflict_acks', c['hash']))
            self.assertGreaterEqual(conflicts.summary()['critical'], 5)

        def test_period_filter_and_tabs(self):
            html = self.get_ok('/schedule/conflicts?period=week')
            self.assertIn('cc-tab__count', html)                     # счётчики — во вкладках измерений
            self.assertNotIn('Аудиторный фонд</span>', html)         # у «Накладок» свой пункт — без вкладок фонда
            self.get_ok('/schedule/conflicts?period=today&dim=room')
            only = self.get_ok('/schedule/conflicts?period=numerator&teacher=t_seitkali')
            self.assertIn('id="ent-1">Сейтқали', only)              # накладка только по числителю
            none = self.get_ok('/schedule/conflicts?period=denominator&teacher=t_seitkali')
            self.assertNotIn('id="ent-1"', none)

        def test_free_rooms_slot_labels_and_room_matrix(self):
            html = self.get_ok('/schedule/free-rooms?day=0')
            self.assertIn('1 пара', html)
            self.assertIn('Перемены, кураторский час', html)         # служебные слоты — отдельно
            self.assertIn('fr-slot-select', html)                    # на телефоне — select вместо чипов
            # ссылка из правки занятия: «с — по» совпадает с парой сетки → выбрана эта пара
            sel = self.get_ok('/schedule/free-rooms?day=2&from=13:00&to=13:50')
            self.assertRegex(sel, r'class="cc-chip cc-num active[^"]*"[^>]*><span><b>\d+ пара</b> · 13:00–13:50')
            matrix = self.get_ok('/schedule/rooms?view=matrix&day=2')
            self.assertIn('/schedule/builder?day=2', matrix)         # свободная клетка → «добавить занятие здесь»
            self.assertIn('data-cc-peek', matrix)                    # занятая → быстрый просмотр занятия

    class FixTest(CmsTestCase):

        def test_fix_teacher_by_name_and_day_mismatch(self):
            r = self.post('/schedule/conflicts/fix', {'code': 'teacher_by_name'})
            self.assertEqual(r.status_code, 302)
            self.assertEqual(self.doc('schedules', 'cf_byname')['teacher'], 't_smirnova')
            self.assertEqual(self.doc('schedules', 'q_ambiguous')['teacher'], 'Ким Виктор Александрович')  # однофамильцы — нет
            logs = [d for d in self.docs('schedule_logs') if d.get('schedule_id') == 'cf_byname']
            self.assertTrue(logs)
            self.assertEqual(logs[0]['action'], 'update')
            self.assertTrue(self.audit_entries('schedule_fix'))
            an = _analysis()
            self.assertNotIn('cf_byname', {q['lesson']['id'] for q in an.quality if q['code'] == 'teacher_by_name'})
            # накладка с cf_room_b осталась (это тот же преподаватель)
            self.assertIsNotNone(_find(an, 'teacher', 'cf_byname', 'cf_room_b'))
            mismatch = [q for q in an.quality if q['code'] == 'day_mismatch']
            r = self.post('/schedule/conflicts/fix', {'code': 'day_mismatch', 'hash': [mismatch[0]['hash']]})
            self.assertEqual(r.status_code, 302)
            doc = self.doc('schedules', 'q_mismatch')
            self.assertEqual(doc['day_index'], 1)
            self.assertEqual(datetime.strptime(doc['date'], '%Y-%m-%d').weekday(), 1)
            self.assertNotIn('notify', doc)      # notify:false снят после записи (D15)
            r = self.post('/schedule/conflicts/fix', {'code': 'bad_time'})
            self.assertEqual(r.status_code, 302)
            self.assertIn('нельзя исправить', ' '.join(t for _c, t in self.flashes()))


class WeekUnknownLikeAppTest(unittest.TestCase):
    """DATA-7: неканонический week_type — критичная проблема качества («в приложении не видно»),
    синоним можно исправить массово на канонический ключ."""

    def test_aliases_and_empty_are_critical_and_fixable(self):
        for raw, canon in (('Числитель', 'numerator'), ('з', 'denominator'), ('', 'always'), ('Always', 'always')):
            with self.subTest(raw=raw):
                l = L('x', week=raw)
                q = [x for x in cf.analyze([l]).quality if x['code'] == 'week_unknown']
                self.assertEqual(len(q), 1)
                self.assertEqual(q[0]['severity'], 'critical')
                self.assertTrue(q[0]['fixable'])
                self.assertEqual(cf.fix_for('week_unknown', l), {'week_type': canon})
        odd = L('x', week='odd')
        self.assertIsNone(cf.fix_for('week_unknown', odd))      # во что исправить — непонятно
        self.assertIn('week_unknown', cf.FIXABLE)


class FixTeacherFieldTest(unittest.TestCase):
    """DATA-4: «ФИО → uid» пишет поле teacher (приложение ищет where teacher == uid) и убирает teacherId."""

    def test_teacher_id_only(self):
        from cms.core import DELETE_FIELD
        l = normalize_lesson('x', {'groupId': 'ПО 24-1', 'day_index': 2, 'time': '13.00-13.50', 'subject': 'Физика',
                                   'room': '204', 'teacherId': 'Смирнова Елена Викторовна'}, INDEX)
        self.assertEqual(cf.fix_for('teacher_by_name', l), {'teacher': 't_smirnova', 'teacherId': DELETE_FIELD})

    def test_both_fields(self):
        from cms.core import DELETE_FIELD
        l = normalize_lesson('x', {'groupId': 'ПО 24-1', 'day_index': 2, 'time': '13.00-13.50', 'subject': 'Физика',
                                   'room': '204', 'teacher': 'Смирнова Елена Викторовна', 'teacherId': 'old'}, INDEX)
        self.assertEqual(cf.fix_for('teacher_by_name', l), {'teacher': 't_smirnova', 'teacherId': DELETE_FIELD})


if os.environ.get('FIRESTORE_EMULATOR_HOST'):

    class FixTeacherByNameEmuTest(CmsTestCase):
        fresh_seed_each_test = True

        def test_fix_writes_teacher_field_that_app_queries(self):
            from cms.core import db
            from cms.schedule_model import day_fields, invalidate_schedule_caches
            doc = {'groupId': 'ПО 23-1', 'time': '17.55-18.45', 'subject': 'Математика',
                   'teacherId': 'Иванова Анна Сергеевна', 'room': '101', 'week_type': 'always'}
            doc.update(day_fields(5))
            db.collection('schedules').document('rev_tid').set(doc)
            invalidate_schedule_caches()
            r = self.post('/schedule/conflicts/fix', {'code': 'teacher_by_name'})
            self.assertEqual(r.status_code, 302)
            after = self.doc('schedules', 'rev_tid')
            # приложение: расписание преподавателя = where('teacher', '==', uid); функции берут teacherId раньше teacher
            self.assertIn('rev_tid', [d['id'] for d in self.docs('schedules', teacher='t_ivanova')], after)
            self.assertNotIn('teacherId', after)
