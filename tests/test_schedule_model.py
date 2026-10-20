# tests/test_schedule_model.py — модель расписания (FOUNDATION).
# Чистые функции тестируются без эмулятора; снапшот — только на эмуляторе.

import os
import unittest
from datetime import date, datetime, timedelta, timezone

from cms.schedule_model import (CALL_SCHEDULE, TeacherIndex, academic_week_number, current_week_type, day_fields,
                                group_key, group_sort_key, in_call_grid, intervals_overlap, is_numerator_week,
                                is_virtual_room, lesson_interval, lesson_iso_weekday, lesson_weekday, next_weekday_date,
                                norm_name, norm_week_type, normalize_lesson, parse_time_range, resolve_teacher,
                                room_key, room_sort_key, time_label, weekday_info, week_occurs, weeks_overlap)


def dart_is_numerator_week(d):
    """Буквальный перенос ScheduleService.isNumeratorWeek (APP) для сверки на всех днях."""
    start = date(d.year if d.month >= 9 else d.year - 1, 9, 1)
    first_monday = start - timedelta(days=start.isoweekday() - 1)
    week_num = (d - first_monday).days // 7 + 1
    return week_num % 2 != 0


class WeekdayTest(unittest.TestCase):

    def test_day_index_has_priority(self):
        # 2026-10-21 — среда, но day_index=0 (Пн) побеждает, как в APP
        self.assertEqual(lesson_weekday({'day_index': 0, 'date': '2026-10-21'}), 0)
        self.assertEqual(lesson_iso_weekday({'day_index': 0, 'date': '2026-10-21'}), 1)
        self.assertEqual(weekday_info({'day_index': 0, 'date': '2026-10-21'}), (0, 'day_index', True))

    def test_date_fallback(self):
        self.assertEqual(lesson_weekday({'date': '2026-10-21'}), 2)
        self.assertEqual(lesson_iso_weekday({'date': '2026-10-21'}), 3)
        self.assertEqual(lesson_weekday({'date': '2026-10-25'}), 6)  # воскресенье не теряется
        self.assertEqual(weekday_info({'date': '2026-10-21'}), (2, 'date', False))

    def test_variants_like_dart(self):
        self.assertEqual(lesson_weekday({'dayIndex': 4}), 4)
        self.assertEqual(lesson_weekday({'day_index': '3'}), 3)
        self.assertEqual(lesson_weekday({'day_index': 7, 'date': '2026-10-21'}), 2)    # вне 0..6 → date
        self.assertEqual(lesson_weekday({'day_index': 'abc', 'date': '2026-10-19'}), 0)
        self.assertEqual(lesson_weekday({'day_index': 1.0, 'date': '2026-10-19'}), 0)  # Dart: int.tryParse('1.0') → null
        self.assertIsNone(lesson_weekday({'day_index': True}))
        self.assertIsNone(lesson_weekday({}))
        self.assertIsNone(lesson_weekday({'date': 'вчера'}))
        self.assertIsNone(lesson_weekday(None))
        self.assertEqual(lesson_weekday({'date': datetime(2026, 10, 20, 19, 0, tzinfo=timezone.utc)}), 2)  # Алматы: уже среда

    def test_next_weekday_and_day_fields(self):
        mon = date(2026, 10, 19)
        self.assertEqual(next_weekday_date(0, mon), mon)                      # сегодня включительно
        self.assertEqual(next_weekday_date(2, mon), date(2026, 10, 21))
        self.assertEqual(next_weekday_date(0, date(2026, 10, 20)), date(2026, 10, 26))
        self.assertEqual(day_fields(4, mon), {'day_index': 4, 'date': '2026-10-23'})
        with self.assertRaises(ValueError):
            day_fields(7, mon)


class TimeTest(unittest.TestCase):

    def test_parse(self):
        self.assertEqual(parse_time_range('08.30-09.50'), (510, 590))
        self.assertEqual(parse_time_range('08:30-09:50'), (510, 590))
        self.assertEqual(parse_time_range('8.30 – 9.50'), (510, 590))
        self.assertEqual(parse_time_range('14:00'), (840, None))
        self.assertEqual(parse_time_range('25:00-26:00'), (None, None))
        self.assertEqual(parse_time_range(''), (None, None))
        self.assertEqual(parse_time_range(None), (None, None))
        self.assertEqual(parse_time_range('после обеда'), (None, None))
        self.assertEqual(time_label(510, 590), '08:30–09:50')

    def test_all_call_slots_parse(self):
        for key, slots in CALL_SCHEDULE.items():
            for s in slots:
                start, end = parse_time_range(s)
                self.assertIsNotNone(start, s)
                self.assertGreater(end, start, s)

    def test_interval_end_assumed(self):
        self.assertEqual(lesson_interval({'time': '08.00', 'day_index': 1}), (480, 530, True))   # слот 08.00-08.50
        self.assertEqual(lesson_interval({'time': '08.00', 'day_index': 0}), (480, 500, True))   # Пн: 08.00-08.20
        self.assertEqual(lesson_interval({'time': '07.00', 'day_index': 1}), (420, 470, True))   # +50 мин
        self.assertEqual(lesson_interval({'time': '08.00-08.50'}), (480, 530, False))
        self.assertEqual(lesson_interval({'time': 'x'}), (None, None, False))

    def test_overlap_strict(self):
        self.assertTrue(intervals_overlap(845, 895, 870, 890))      # 14.05-14.55 × 14.30-14.50
        self.assertFalse(intervals_overlap(655, 705, 705, 725))     # касание 10.55-11.45 и 11.45-12.05
        self.assertFalse(intervals_overlap(None, 10, 0, 5))

    def test_in_call_grid(self):
        self.assertTrue(in_call_grid({'time': '08.25-09.15', 'day_index': 0}))
        self.assertFalse(in_call_grid({'time': '08.25-09.15', 'day_index': 1}))  # сетка вторника другая
        self.assertTrue(in_call_grid({'time': '08:00-08:50', 'day_index': 2}))


class ParityTest(unittest.TestCase):

    def test_matches_app_formula_every_day(self):
        d = date(2020, 1, 1)
        while d < date(2033, 1, 1):
            self.assertEqual(is_numerator_week(d), dart_is_numerator_week(d), d)
            d += timedelta(days=1)

    def test_known_values(self):
        # 1 сентября 2025 — понедельник: неделя 1 (числитель) — здесь старый веб ошибался
        self.assertTrue(is_numerator_week(date(2025, 9, 1)))
        self.assertFalse(is_numerator_week(date(2025, 9, 8)))
        # 2026-09-01 — вторник: неделя с пн 31.08 — числитель
        self.assertEqual(academic_week_number(date(2026, 9, 1)), 1)
        self.assertEqual(academic_week_number(date(2026, 10, 19)), 8)
        self.assertEqual(current_week_type(date(2026, 10, 19)), 'denominator')
        # смена только по понедельникам
        self.assertEqual(is_numerator_week(date(2026, 10, 25)), is_numerator_week(date(2026, 10, 19)))
        self.assertNotEqual(is_numerator_week(date(2026, 10, 26)), is_numerator_week(date(2026, 10, 25)))

    def test_datetime_in_almaty(self):
        # 2026-10-25 20:00 UTC = 26.10 01:00 по Алматы (понедельник новой недели)
        utc = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)
        self.assertEqual(is_numerator_week(utc), is_numerator_week(date(2026, 10, 26)))

    def test_week_types(self):
        self.assertEqual(norm_week_type(None), ('always', False))
        # DATA-7: синонимы приложение не понимает (сравнивает буквально) — unknown, неделя — подсказка
        self.assertEqual(norm_week_type('Числитель'), ('numerator', True))
        self.assertEqual(norm_week_type('з'), ('denominator', True))
        self.assertEqual(norm_week_type('odd'), ('always', True))
        self.assertTrue(weeks_overlap('always', 'numerator'))
        self.assertTrue(weeks_overlap('denominator', 'denominator'))
        self.assertFalse(weeks_overlap('numerator', 'denominator'))
        self.assertTrue(weeks_overlap('', 'числитель'))
        d = date(2026, 10, 19)  # знаменатель
        self.assertTrue(week_occurs('always', d))
        self.assertTrue(week_occurs('denominator', d))
        self.assertFalse(week_occurs('numerator', d))


class KeysTest(unittest.TestCase):

    def test_room_key(self):
        self.assertEqual(room_key('204'), room_key('Каб. 204'))
        self.assertEqual(room_key('ауд.204'), '204')
        self.assertEqual(room_key('Аудитория № 204'), '204')
        self.assertEqual(room_key('А-12'), room_key('A 12'))   # латинская A
        self.assertEqual(room_key('Спортзал'), room_key('спортзал '))
        for v in ('', None, '-', '—', 'Онлайн', 'online', 'ZOOM'):
            self.assertIsNone(room_key(v), v)
            self.assertTrue(is_virtual_room(v))
        self.assertFalse(is_virtual_room('101'))

    def test_group_key(self):
        self.assertEqual(group_key('ПО 24-1'), group_key('по24-1'))
        self.assertEqual(group_key('ИС 23–1'), group_key('ИС 23-1'))
        self.assertIsNone(group_key('  '))

    def test_sort_keys(self):
        self.assertEqual(sorted(['ПО 25-1', 'ИС 23-1', 'ПО 23-1'], key=group_sort_key), ['ИС 23-1', 'ПО 23-1', 'ПО 25-1'])
        self.assertEqual(sorted(['А-2', '201', '102', 'А-10'], key=room_sort_key), ['102', '201', 'А-2', 'А-10'])

    def test_norm_name(self):
        self.assertEqual(norm_name('  Иванов  И.И. '), 'иванов и и')
        self.assertEqual(norm_name('Сейтқали Ёрлан'), 'сейтқали ерлан')

    def test_resolve_teacher(self):
        idx = TeacherIndex({'u1': 'Иванова Анна Сергеевна', 'u2': 'Ким Виктор', 'u3': 'Ким Виктор'}, {'old1': 'Старый П.'})
        self.assertEqual(resolve_teacher('u1', idx), {'key': 'uid:u1', 'uid': 'u1', 'name': 'Иванова Анна Сергеевна', 'flags': []})
        r = resolve_teacher('иванова  анна сергеевна', idx)
        self.assertEqual((r['key'], r['flags']), ('uid:u1', ['by_name']))
        r = resolve_teacher('Ким Виктор', idx)
        self.assertEqual((r['key'], r['flags']), ('name:ким виктор', ['by_name', 'ambiguous']))
        self.assertEqual(resolve_teacher('old1', idx)['flags'], ['legacy'])
        self.assertEqual(resolve_teacher('uidXYZ', idx)['flags'], ['orphan'])
        self.assertEqual(resolve_teacher('Новый Препод', idx)['flags'], ['unknown_name'])
        self.assertEqual(resolve_teacher('', idx), {'key': None, 'uid': None, 'name': '', 'flags': ['no_teacher']})

    def test_normalize_lesson(self):
        idx = TeacherIndex({'t1': 'Петров Дмитрий'})
        l = normalize_lesson('x', {'groupId': ' ПО 24-1 ', 'date': '2026-10-21', 'time': '13.00-13.50', 'teacher': 't1',
                                   'room': 'Каб. 204', 'week_type': 'numerator', 'subject': 'Python'}, idx)
        self.assertEqual(l['weekday'], 2)
        self.assertEqual(l['day_name'], 'Среда')
        self.assertEqual(l['kind'], 'dated')
        self.assertEqual((l['start'], l['end'], l['time_ok'], l['end_assumed']), (780, 830, True, False))
        self.assertEqual(l['groupId'], 'ПО 24-1')
        self.assertEqual(l['teacher_key'], 'uid:t1')
        self.assertEqual(l['teacher_name'], 'Петров Дмитрий')
        self.assertEqual(l['room_key'], '204')
        self.assertEqual(l['week'], 'numerator')
        self.assertTrue(l['in_grid'])
        bad = normalize_lesson('y', {'time': '10.50-10.00', 'day_index': 6, 'room': 'онлайн'}, idx)
        self.assertFalse(bad['time_ok'])
        self.assertEqual(bad['day_name'], 'Воскресенье')
        self.assertTrue(bad['room_virtual'])
        self.assertIsNone(bad['room_key'])
        self.assertEqual(bad['kind'], 'recurring')


@unittest.skipUnless(os.environ.get('FIRESTORE_EMULATOR_HOST'), 'снапшот проверяется на эмуляторе')
class SnapshotTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from tests import seed
        cls.seed = seed.reset_and_seed()

    def test_snapshot_loads_all_lessons(self):
        from cms.schedule_model import load_lessons_snapshot
        snap = load_lessons_snapshot(force=True)
        self.assertEqual(len(snap), self.seed['lessons_count'])
        self.assertIn('ПО 24-1', snap.by_group)
        # рекуррентное (только day_index), с датой и «только date» — все с днём недели
        self.assertEqual(snap.by_id['cf_dated_a']['weekday'], 0)
        self.assertEqual(snap.by_id['cf_dated_b']['weekday'], 0)
        self.assertEqual(snap.by_id['cf_dated_b']['kind'], 'dated')
        self.assertTrue(snap.by_id['q_mismatch']['day_mismatch'])
        self.assertEqual(snap.by_id['q_mismatch']['weekday'], 1)
        self.assertIsNone(snap.by_id['q_no_day']['weekday'])
        # ФИО текстом → uid Смирновой; однофамильцы → неоднозначно
        self.assertEqual(snap.by_id['cf_byname']['teacher_key'], 'uid:t_smirnova')
        self.assertIn('ambiguous', snap.by_id['q_ambiguous']['teacher_flags'])
        self.assertIn('legacy', snap.by_id['q_legacy']['teacher_flags'])
        self.assertIn('orphan', snap.by_id['q_orphan']['teacher_flags'])
        # «204» и «Каб. 204» — одна аудитория
        self.assertEqual(snap.by_id['cf_room_a']['room_key'], snap.by_id['cf_room_b']['room_key'])
        self.assertGreaterEqual(len(snap.by_teacher['uid:t_smirnova']), 3)

    def test_snapshot_cached_and_invalidated(self):
        from cms.core import db
        from cms.schedule_model import invalidate_schedule_caches, load_lessons_snapshot
        a = load_lessons_snapshot(force=True)
        self.assertIs(load_lessons_snapshot(), a)
        db.collection('schedules').document('tmp_new').set({'groupId': 'ПО 24-1', 'day_index': 5, 'time': '09.00-09.50'})
        self.assertNotIn('tmp_new', load_lessons_snapshot().by_id)  # кэш
        invalidate_schedule_caches()
        self.assertIn('tmp_new', load_lessons_snapshot().by_id)
        db.collection('schedules').document('tmp_new').delete()
        invalidate_schedule_caches()

    def test_directories(self):
        from cms.schedule_model import get_all_groups, get_all_rooms, get_all_teachers, get_teacher_name, get_week_parity
        groups = get_all_groups()
        self.assertIn('ПО 24-1', groups)
        self.assertIn('XX 99-9', groups)       # группа есть только в расписании
        rooms = get_all_rooms()
        self.assertIn('204', rooms)
        self.assertIn('Каб. 204', rooms)       # строки как есть (как раньше)
        self.assertEqual(get_teacher_name('t_petrov'), 'Петров Дмитрий Игоревич')
        self.assertEqual(get_teacher_name('legacy_t1'), 'Старостин Пётр Иванович')
        self.assertIn('t_kim2', get_all_teachers())
        self.assertIn(get_week_parity(), (True, False))


if __name__ == '__main__':
    unittest.main()


class WeekTypeLikeAppTest(unittest.TestCase):
    """DATA-7: приложение сравнивает week_type буквально (`week_type ?? 'always'`) — всё, кроме None и
    'always'|'numerator'|'denominator', оно прячет при фильтре недели. CMS отмечает такие значения."""

    @staticmethod
    def app_visible(raw, numerator_week):
        t = 'always' if raw is None else str(raw)
        if t == 'always':
            return True
        return t == ('numerator' if numerator_week else 'denominator')

    def test_only_exact_keys_are_known(self):
        for raw in (None, 'always', 'numerator', 'denominator'):
            self.assertFalse(norm_week_type(raw)[1], repr(raw))
        for raw in ('Числитель', 'ч', 'Numerator', '', ' always', 'Always', 'odd', 0):
            week, unknown = norm_week_type(raw)
            visible = self.app_visible(raw, True) or self.app_visible(raw, False)
            self.assertFalse(visible)
            self.assertTrue(unknown, 'week_type=%r: приложение прячет занятие, а CMS считает его «%s»' % (raw, week))
        # синоним подсказывает, во что исправить
        self.assertEqual(norm_week_type('Числитель'), ('numerator', True))
        self.assertEqual(norm_week_type('з'), ('denominator', True))
        self.assertEqual(norm_week_type(''), ('always', True))
        self.assertEqual(norm_week_type('odd'), ('always', True))
