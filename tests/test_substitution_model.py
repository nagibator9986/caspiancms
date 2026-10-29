# tests/test_substitution_model.py — «Замены»: чистая логика (подбор свободных, чётность недели, статистика).
# Без эмуляторов: снапшот собирается из нормализованных занятий вручную.

import unittest
from datetime import date

from cms import substitution_model as sm
from cms.schedule_model import LessonsSnapshot, TeacherIndex, current_week_type, normalize_lesson

TEACHERS = {'t_a': 'Ахметова Динара Маратовна', 't_b': 'Ким Виктор Александрович', 't_c': 'Омарова Айгуль Кайратовна',
            't_d': 'Нурланов Арман Серикович', 't_e': 'Бекова Сауле Тимуровна'}
THU = date(2026, 11, 12)         # четверг (weekday 3)
THU_WEEK = current_week_type(THU)
OTHER_WEEK = 'denominator' if THU_WEEK == 'numerator' else 'numerator'


def lesson(lid, teacher, day, time, group='ПО 24-1', subject='Физика', week='always', **extra):
    data = {'groupId': group, 'subject': subject, 'teacher': teacher, 'day_index': day, 'time': time, 'room': '301',
            'week_type': week}
    data.update(extra)
    return normalize_lesson(lid, data, TeacherIndex(TEACHERS))


def snapshot(*lessons):
    return LessonsSnapshot(list(lessons), TeacherIndex(TEACHERS))


def sub(sid_date, lid, status=sm.ASSIGNED, frm='t_a', to='t_b', start=555, end=605, group='ПО 24-1', subject='Физика',
        reason='sick', **extra):
    data = {'date': sid_date, 'lessonId': lid, 'groupId': group, 'subject': subject, 'start': start, 'end': end,
            'time': '09.15-10.05', 'fromKey': 'uid:' + frm, 'fromUid': frm, 'fromName': TEACHERS[frm], 'status': status,
            'reason': reason}
    if to:
        data.update(toUid=to, toName=TEACHERS[to])
    data.update(extra)
    return sm.normalize_sub('%s_%s' % (sid_date, lid), data)


class DatesTest(unittest.TestCase):

    def test_parse_and_range(self):
        self.assertEqual(sm.parse_iso_date('2026-11-12'), THU)
        for bad in ('', None, '12.11.2026', '2026-13-01', '2026-11-1', 'вчера'):
            self.assertIsNone(sm.parse_iso_date(bad), bad)
        self.assertEqual(len(sm.date_range(THU, date(2026, 11, 14))), 3)
        self.assertEqual(sm.date_range(date(2026, 11, 14), THU), [])
        self.assertEqual(sm.date_range(None, THU), [])

    def test_day_label(self):
        self.assertEqual(sm.day_label(THU), 'Четверг, 12.11.2026')
        self.assertEqual(sm.day_label(THU, today=THU), 'Четверг, 12.11.2026 · сегодня')
        self.assertTrue(sm.day_label(THU, today=date(2026, 11, 11)).endswith('· завтра'))

    def test_period_bounds(self):
        today = date(2026, 11, 12)
        self.assertEqual(sm.period_bounds('upcoming', today), (today, None))
        self.assertEqual(sm.period_bounds('week', today), (date(2026, 11, 9), date(2026, 11, 15)))
        self.assertEqual(sm.period_bounds('month', today), (date(2026, 11, 1), date(2026, 11, 30)))
        self.assertEqual(sm.period_bounds('prev_month', today), (date(2026, 10, 1), date(2026, 10, 31)))
        self.assertEqual(sm.period_bounds('prev_month', date(2027, 1, 5)), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(sm.period_bounds('month', date(2026, 12, 20)), (date(2026, 12, 1), date(2026, 12, 31)))
        self.assertEqual(sm.period_bounds('year', today), (date(2026, 9, 1), date(2027, 8, 31)))
        self.assertEqual(sm.period_bounds('year', date(2027, 3, 1)), (date(2026, 9, 1), date(2027, 8, 31)))
        self.assertEqual(sm.period_bounds('all', today), (None, None))
        self.assertEqual(sm.period_bounds('custom', today, date(2026, 1, 1), date(2026, 2, 1)),
                         (date(2026, 1, 1), date(2026, 2, 1)))

    def test_duration_text(self):
        self.assertEqual(sm.duration_text(0), '—')
        self.assertEqual(sm.duration_text(45), '45 мин')
        self.assertEqual(sm.duration_text(120), '2 ч')
        self.assertEqual(sm.duration_text(135), '2 ч 15 мин')


class OccursOnTest(unittest.TestCase):

    def test_recurring_by_weekday_and_week(self):
        self.assertTrue(sm.lesson_occurs_on(lesson('a', 't_a', 3, '09.15-10.05'), THU))
        self.assertFalse(sm.lesson_occurs_on(lesson('a', 't_a', 2, '09.15-10.05'), THU))                 # другой день
        self.assertTrue(sm.lesson_occurs_on(lesson('a', 't_a', 3, '09.15-10.05', week=THU_WEEK), THU))
        self.assertFalse(sm.lesson_occurs_on(lesson('a', 't_a', 3, '09.15-10.05', week=OTHER_WEEK), THU))
        # через неделю чётность меняется
        self.assertTrue(sm.lesson_occurs_on(lesson('a', 't_a', 3, '09.15-10.05', week=OTHER_WEEK), date(2026, 11, 19)))

    def test_dated_without_flag_is_weekly_but_one_off_is_once(self):
        weekly = lesson('a', 't_a', None, '09.15-10.05', date='2026-10-29')        # старый веб: дата = якорь дня
        self.assertTrue(sm.lesson_occurs_on(weekly, THU))
        once = lesson('b', 't_a', None, '09.15-10.05', date='2026-11-12', one_off=True)
        self.assertTrue(sm.lesson_occurs_on(once, THU))
        self.assertFalse(sm.lesson_occurs_on(once, date(2026, 11, 19)))

    def test_no_weekday(self):
        self.assertFalse(sm.lesson_occurs_on(lesson('a', 't_a', None, '09.15-10.05'), THU))

    def test_teacher_lessons_sorted(self):
        snap = snapshot(lesson('l2', 't_a', 3, '10.25-11.15'), lesson('l1', 't_a', 3, '08.25-09.15'),
                        lesson('l3', 't_a', 3, '12.00-12.50', week=OTHER_WEEK), lesson('l4', 't_a', 4, '08.25-09.15'))
        self.assertEqual([l['id'] for l in sm.teacher_lessons_on(snap, 'uid:t_a', THU)], ['l1', 'l2'])
        self.assertEqual(sm.teacher_lessons_on(snap, 'uid:t_zzz', THU), [])

    def test_sub_id(self):
        self.assertEqual(sm.sub_id(THU, 'abc'), '2026-11-12_abc')


class SuggestTest(unittest.TestCase):
    """Слот 09:20–10:10 четверга; ПО 24-1, «Физика»; заменяется t_a."""
    START, END = 560, 610

    def setUp(self):
        self.snap = snapshot(
            lesson('a1', 't_a', 3, '09.20-10.10'),                                        # заменяемая пара
            lesson('b1', 't_b', 3, '09.20-10.10', group='ИС 24-1', subject='Математика'),  # занят ровно в слот
            lesson('c1', 't_c', 3, '10.25-11.15', group='ИС 24-1', subject='Физика'),      # свободен, рядом, ведёт физику
            lesson('d1', 't_d', 3, '12.00-12.50', group='ПО 24-1', subject='Базы данных'), # свободен, знает группу
            lesson('d2', 't_d', 2, '09.20-10.10', group='ПО 24-1', subject='Базы данных'),
            lesson('e1', 't_e', 3, '09.20-10.10', group='ЭК 24-1', subject='Учёт', week=OTHER_WEEK),  # не эта неделя
        )
        self.profile = sm.teaching_profile(self.snap)

    def ctx(self, subs=(), also_absent=('uid:t_a',)):
        return sm.DayContext(self.snap, THU, subs, also_absent)

    def suggest(self, ctx=None, **kw):
        return sm.suggest_teachers(ctx or self.ctx(), TEACHERS, start=self.START, end=self.END, subject='Физика',
                                   group='ПО 24-1', exclude_uids={'t_a'}, profile=self.profile, **kw)

    def test_free_and_busy_split(self):
        r = self.suggest()
        self.assertEqual({x['uid'] for x in r['busy']}, {'t_b'})
        self.assertEqual({x['uid'] for x in r['free']}, {'t_c', 't_d', 't_e'})
        busy = r['busy'][0]
        self.assertFalse(busy['free'])
        self.assertIn('ИС 24-1 · Математика', busy['note'])

    def test_other_week_lesson_does_not_block(self):
        free = {x['uid'] for x in self.suggest()['free']}
        self.assertIn('t_e', free)            # его пара — другой чётности
        # а в «его» неделю он бы уже был занят
        other_day = date(2026, 11, 19)
        ctx = sm.DayContext(self.snap, other_day, (), ('uid:t_a',))
        r = sm.suggest_teachers(ctx, TEACHERS, start=self.START, end=self.END, exclude_uids={'t_a'})
        self.assertIn('t_e', {x['uid'] for x in r['busy']})

    def test_ranking_subject_beats_group_beats_idle(self):
        free = self.suggest()['free']
        self.assertEqual([x['uid'] for x in free], ['t_c', 't_d', 't_e'])
        c, d, e = free
        self.assertIn(('subject', 'ведёт эту дисциплину'), c['badges'])
        self.assertIn(('adjacent', 'пара рядом'), c['badges'])
        self.assertIn(('group', 'знает группу'), d['badges'])
        self.assertNotIn(('adjacent', 'пара рядом'), d['badges'])           # 12:00 — далеко
        self.assertIn(('day_off', 'в этот день пар нет'), e['badges'])      # на этой неделе у него пар нет
        self.assertEqual(e['note'], 'в этот день пар нет')
        self.assertGreater(c['score'], d['score'])
        self.assertGreater(d['score'], e['score'])

    def test_windows(self):
        free = {x['uid']: x for x in self.suggest()['free']}
        self.assertEqual(free['t_c']['note'], 'свободен до 10:25')          # его пара 10:25 — после слота
        self.assertEqual(free['t_d']['note'], 'свободен до 12:00')

    def test_absent_are_unavailable_even_without_lessons(self):
        ctx = self.ctx(subs=[sub('2026-11-12', 'x1', frm='t_e', to='t_d', start=700, end=750)])
        r = self.suggest(ctx)
        absent = [x for x in r['busy'] if x['absent']]
        self.assertEqual([x['uid'] for x in absent], ['t_e'])
        self.assertNotIn('t_e', {x['uid'] for x in r['free']})
        self.assertEqual(r['busy'][-1]['uid'], 't_e')                       # отсутствующие — в конце списка занятых

    def test_already_covering_teacher_is_busy(self):
        ctx = self.ctx(subs=[sub('2026-11-12', 'x1', frm='t_e', to='t_c', start=self.START, end=self.END)])
        r = self.suggest(ctx)
        c = next(x for x in r['busy'] if x['uid'] == 't_c')
        self.assertIn('замена:', c['note'])
        self.assertIn('t_c', {x['uid'] for x in r['busy']})

    def test_editing_a_sub_ignores_itself(self):
        s = sub('2026-11-12', 'a1', frm='t_a', to='t_c', start=self.START, end=self.END)
        ctx = self.ctx(subs=[s])
        self.assertIn('t_c', {x['uid'] for x in self.suggest(ctx)['busy']})
        r = self.suggest(ctx, ignore_sub=s['id'])
        self.assertIn('t_c', {x['uid'] for x in r['free']})

    def test_touching_is_not_overlap(self):
        snap = snapshot(lesson('k1', 't_c', 3, '08.25-09.20'))             # кончается ровно, когда слот начинается
        ctx = sm.DayContext(snap, THU, (), ())
        r = sm.suggest_teachers(ctx, TEACHERS, start=self.START, end=self.END)
        self.assertIn('t_c', {x['uid'] for x in r['free']})

    def test_load_lowers_score(self):
        base = {x['uid']: x['score'] for x in self.suggest()['free']}
        loaded = {x['uid']: x for x in self.suggest(load={'t_d': 4})['free']}
        self.assertEqual(loaded['t_d']['score'], base['t_d'] - 12)
        self.assertIn(('load', 'замен в месяце: 4'), loaded['t_d']['badges'])

    def test_unknown_time_blocks_nobody(self):
        r = sm.suggest_teachers(self.ctx(), TEACHERS, start=None, end=None, exclude_uids={'t_a'})
        self.assertEqual({x['uid'] for x in r['busy']}, set())
        self.assertEqual(len(r['free']), 4)

    def test_excluded_never_listed(self):
        r = self.suggest()
        self.assertNotIn('t_a', {x['uid'] for x in r['free'] + r['busy']})

    def test_month_load(self):
        subs = [sub('2026-11-03', 'p', to='t_c'), sub('2026-11-12', 'q', to='t_c'), sub('2026-10-30', 'r', to='t_c'),
                sub('2026-11-04', 's', status=sm.CANCELLED, to=None), sub('2026-11-05', 't', status=sm.OPEN, to=None)]
        self.assertEqual(dict(sm.month_load(subs, THU)), {'t_c': 2})


class SubRecordTest(unittest.TestCase):

    def test_normalize(self):
        s = sub('2026-11-12', 'a1')
        self.assertEqual((s['date'], s['status'], s['to_uid'], s['minutes'], s['time_label']),
                         (THU, 'assigned', 't_b', 50, '09:15–10:05'))
        self.assertEqual(s['reason_label'], 'Болезнь')

    def test_open_and_cancelled_have_no_replacer(self):
        for st in (sm.OPEN, sm.CANCELLED):
            s = sub('2026-11-12', 'a1', status=st, to='t_b')       # даже если в документе остался toUid
            self.assertEqual((s['to_uid'], s['to_name']), ('', ''), st)

    def test_garbage(self):
        self.assertIsNone(sm.normalize_sub('x', {'date': 'нет'}))
        self.assertIsNone(sm.normalize_sub('x', None))
        s = sm.normalize_sub('x', {'date': '2026-11-12', 'status': 'ZZZ', 'reason': 'ZZZ', 'start': 'a'})
        self.assertEqual((s['status'], s['reason'], s['start'], s['minutes']), (sm.OPEN, '', None, 0))

    def test_group_by_date_sorted(self):
        a = sub('2026-11-13', 'a', start=555, end=605)
        b = sub('2026-11-12', 'b', start=700, end=750)
        c = sub('2026-11-12', 'c', start=555, end=605)
        got = sm.group_by_date([a, b, c])
        self.assertEqual([(d.isoformat(), [s['id'] for s in ss]) for d, ss in got],
                         [('2026-11-12', ['2026-11-12_c', '2026-11-12_b']), ('2026-11-13', ['2026-11-13_a'])])


class StatsTest(unittest.TestCase):

    def test_stats(self):
        subs = [
            sub('2026-11-12', 'a', frm='t_a', to='t_b', start=555, end=605, reason='sick'),
            sub('2026-11-12', 'b', frm='t_a', to='t_b', start=620, end=670, reason='sick', group='ИС 24-1'),
            sub('2026-11-13', 'c', frm='t_a', to='t_c', start=555, end=600, reason='sick'),
            sub('2026-11-13', 'd', frm='t_d', to=None, status=sm.CANCELLED, reason='trip'),
            sub('2026-11-14', 'e', frm='t_d', to=None, status=sm.OPEN, reason=''),
        ]
        st = sm.build_stats(subs)
        self.assertEqual((st['count'], st['assigned'], st['open'], st['cancelled']), (5, 3, 1, 1))
        self.assertEqual(st['minutes'], 50 + 50 + 45)
        top = st['replacers'][0]
        self.assertEqual((top['uid'], top['count'], top['minutes'], top['groups'], top['days']), ('t_b', 2, 100, 2, 1))
        self.assertEqual([r['uid'] for r in st['replacers']], ['t_b', 't_c'])
        absent = {a['uid']: a for a in st['absent']}
        self.assertEqual((absent['t_a']['count'], absent['t_a']['assigned'], absent['t_a']['days']), (3, 3, 2))
        self.assertEqual((absent['t_d']['cancelled'], absent['t_d']['open'], absent['t_d']['count']), (1, 1, 2))
        self.assertEqual(st['absent'][0]['uid'], 't_a')
        reasons = {r['key']: r['count'] for r in st['reasons']}
        self.assertEqual(reasons, {'sick': 3, 'trip': 1, 'none': 1})
        self.assertEqual(st['reasons'][0]['label'], 'Болезнь')
        self.assertEqual(st['reasons'][-1]['label'], 'Не указана')

    def test_empty(self):
        st = sm.build_stats([])
        self.assertEqual((st['count'], st['replacers'], st['absent'], st['reasons'], st['minutes']), (0, [], [], [], 0))


if __name__ == '__main__':
    unittest.main()
