# tests/test_schedule.py — расписание в CMS (владелец — SCHEDULE).
#
# Первая часть — тесты, воспроизводящие критичные баги старого кода (из map2_cms_schedule):
#   • /schedule/edit_group не показывал рекуррентные (только day_index), воскресные и «без дня»
#     занятия, а /schedule/update_group удалял всё, чего не было в форме → потеря занятий;
#   • веб писал только date (без day_index) → после переноса дня приложение видело старый день;
#   • update_group пересчитывал date и переписывал КАЖДОЕ занятие → onScheduleUpdate слал пуш на
#     каждую пару (пуш-шторм); теперь notify:false + один пуш на группу от CMS;
#   • «сегодня» и чётность недели считались по UTC сервера, а не по Алматы;
#   • журнал писал action 'create' и timestamp числом (APP/функции ждут 'add' и Timestamp);
#   • формы правки подменяли значения, которых нет в справочниках (ФИО, «осиротевший» uid,
#     время вне сетки) первым вариантом списка.
# Вторая часть — новые функции: копирование расписания, массовые действия, проверка накладок,
# экспорт, печать, история с фильтрами.
#
# Запуск: tests/run_tests.sh 18100 tests.test_schedule

import io
import re
import zipfile
from collections import OrderedDict
from datetime import date, datetime
from unittest import mock

from tests.base import CmsTestCase

GROUP = 'ПО 24-1'
GROUP_TOPIC = 'schedule_group_po_24-1'


def _weekday_of(doc):
    """День недели документа по правилам APP: day_index, иначе date."""
    if doc.get('day_index') is not None:
        return int(doc['day_index'])
    raw = doc.get('date')
    if isinstance(raw, str):
        return datetime.strptime(raw[:10], '%Y-%m-%d').weekday()
    if isinstance(raw, datetime):
        return raw.weekday()
    return None


def _sent_pushes():
    from cms import core
    return core.sent_pushes


def _reset_schedule_data():
    """Лёгкий пересев только расписания (schedules, schedule_logs, cms_audit) — без Auth/Storage:
    полный reset_and_seed на каждый тест перегружает эмулятор Storage."""
    from cms import core
    from tests import seed as seed_module
    for coll in ('schedules', 'schedule_logs', 'cms_audit'):
        refs = [d.reference for d in core.db.collection(coll).select([]).stream()]
        for part in core.chunked(refs, 400):
            batch = core.db.batch()
            for ref in part:
                batch.delete(ref)
            batch.commit()
    b = seed_module._Batch()
    seed_module.seed_schedule(b)
    b.commit()
    core.cache.invalidate()


class _ScheduleCase(CmsTestCase):
    """Каждый тест — на свежем расписании: тесты его меняют (остальные данные — сид класса)."""

    def setUp(self):
        _reset_schedule_data()
        super().setUp()
        _sent_pushes().clear()

    def group_docs(self, group=GROUP):
        return {d['id']: d for d in self.docs('schedules', groupId=group)}

    def new_logs(self):
        """Записи журнала, созданные тестом (сидовые — log_00..log_03)."""
        return [d for d in self.docs('schedule_logs') if not d['id'].startswith('log_')]

    def old_page_payload(self, group=GROUP):
        """Тело запроса так, как его собирала СТАРАЯ страница edit_group: только занятия с date."""
        days = {}
        for doc_id, d in self.group_docs(group).items():
            if not isinstance(d.get('date'), str):
                continue
            wd = datetime.strptime(d['date'], '%Y-%m-%d').weekday()
            if wd > 5:
                continue
            days.setdefault(wd, []).append({'id': doc_id, 'time': d.get('time', ''), 'teacher': d.get('teacher', ''),
                                            'subject': d.get('subject', ''), 'room': d.get('room', ''),
                                            'week_type': d.get('week_type', 'always')})
        return {'group_id': group, 'days': [{'day_of_week': str(wd), 'lessons': ls} for wd, ls in sorted(days.items())]}

    def full_payload(self, group=GROUP):
        """Все занятия группы как есть (новая страница): day_of_week — день по правилам APP."""
        days = {}
        for doc_id, d in self.group_docs(group).items():
            wd = _weekday_of(d)
            days.setdefault(wd, []).append({'id': doc_id, 'time': d.get('time', ''), 'teacher': d.get('teacher', ''),
                                            'subject': d.get('subject', ''), 'room': d.get('room', ''),
                                            'week_type': d.get('week_type', 'always')})
        return {'group_id': group, 'days': [{'day_of_week': str(wd), 'lessons': ls} for wd, ls in sorted(days.items())]}


# ==================== БАГИ СТАРОГО КОДА ====================

class LostLessonsBugTest(_ScheduleCase):
    """КРИТИЧНО: редактор группы терял рекуррентные/воскресные/«без дня» занятия."""

    def test_edit_group_shows_recurring_sunday_and_dayless_lessons(self):
        index_only = [i for i, d in self.group_docs().items() if 'date' not in d]
        self.assertTrue(index_only, 'в сиде у группы должны быть занятия только с day_index')
        html = self.get_ok('/schedule/edit_group/' + GROUP)
        for lesson_id in index_only:
            self.assertIn(lesson_id, html)
        self.assertIn('q_sunday', self.get_ok('/schedule/edit_group/ИС 24-1'))      # воскресенье
        self.assertIn('q_no_day', self.get_ok('/schedule/edit_group/ЭК 24-1'))      # нет ни day_index, ни date

    def test_update_group_does_not_delete_lessons_missing_from_form(self):
        before = self.group_docs()
        index_only = [i for i, d in before.items() if 'date' not in d]
        r = self.post_json('/schedule/update_group', self.old_page_payload())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.group_docs()
        for lesson_id in index_only:
            self.assertIn(lesson_id, after, 'занятие %s удалено, хотя его не удаляли' % lesson_id)
        self.assertEqual(set(before), set(after))
        self.assertFalse([l for l in self.new_logs() if l['action'] == 'delete'])

    def test_update_group_deletes_only_explicit_ids_of_this_group(self):
        before = self.group_docs()
        victim = sorted(before)[0]
        payload = self.full_payload()
        for day in payload['days']:
            day['lessons'] = [l for l in day['lessons'] if l['id'] != victim]
        payload['deleted_ids'] = [victim, 'cf_petrov_a']  # чужая группа — игнорировать
        r = self.post_json('/schedule/update_group', payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        after = self.group_docs()
        self.assertEqual(set(before) - {victim}, set(after))
        self.assertIsNotNone(self.doc('schedules', 'cf_petrov_a'))
        deletes = [l for l in self.new_logs() if l['action'] == 'delete']
        self.assertEqual([l['schedule_id'] for l in deletes], [victim])


class DayFieldsBugTest(_ScheduleCase):
    """КРИТИЧНО: веб писал только date — APP (приоритет day_index) видел старый день."""

    def test_builder_writes_day_index_and_date_by_almaty_today(self):
        # «Сегодня» по Алматы — понедельник 2026-03-02; среда должна стать 2026-03-04.
        with mock.patch('cms.schedule_model.today_almaty', return_value=date(2026, 3, 2)):
            r = self.post('/schedule/builder', {
                'groupId': 'МЕН 25-1', 'day_of_week': ['2'], 'lesson_day_rel': ['0'],
                'time': ['17.55-18.45'], 'teacher': ['t_li'], 'subject': ['Компьютерные сети'],
                'room': ['А-12'], 'week_type': ['numerator']})
        self.assertEqual(r.status_code, 302)
        created = [d for d in self.docs('schedules', groupId='МЕН 25-1') if d.get('time') == '17.55-18.45']
        self.assertEqual(len(created), 1)
        doc = created[0]
        self.assertEqual(doc.get('day_index'), 2)
        self.assertEqual(doc.get('date'), '2026-03-04')
        self.assertEqual(doc.get('week_type'), 'numerator')

    def test_edit_moves_day_in_both_fields(self):
        # base_01_1_1: «ПО 24-1», вторник. Переносим на четверг.
        src = self.doc('schedules', 'base_01_1_1')
        self.assertEqual(_weekday_of(src), 1)
        r = self.post('/schedule/edit/base_01_1_1', {
            'groupId': GROUP, 'day': '3', 'date': '2026-10-22', 'time': src['time'], 'subject': src['subject'],
            'teacher': src['teacher'], 'room': src['room'], 'week_type': src.get('week_type', 'always')})
        self.assertEqual(r.status_code, 302)
        doc = self.doc('schedules', 'base_01_1_1')
        self.assertEqual(doc.get('day_index'), 3)
        self.assertEqual(datetime.strptime(doc['date'], '%Y-%m-%d').weekday(), 3)

    def test_update_group_moves_recurring_lesson_in_both_fields(self):
        payload = self.full_payload()
        index_only = sorted(i for i, d in self.group_docs().items() if 'date' not in d)
        moved = index_only[0]
        found = None
        for day in payload['days']:
            for l in list(day['lessons']):
                if l['id'] == moved:
                    day['lessons'].remove(l)
                    found = l
        self.assertIsNotNone(found)
        payload['days'].append({'day_of_week': '5', 'lessons': [found]})
        r = self.post_json('/schedule/update_group', payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        doc = self.doc('schedules', moved)
        self.assertEqual(doc.get('day_index'), 5)
        self.assertEqual(datetime.strptime(doc['date'], '%Y-%m-%d').weekday(), 5)


class PushStormBugTest(_ScheduleCase):
    """update_group переписывал все занятия (и date) → пуш на каждую пару из onScheduleUpdate."""

    def test_unchanged_lessons_are_not_rewritten_and_one_push_per_group(self):
        payload = self.full_payload()
        target = payload['days'][0]['lessons'][0]
        target['room'] = '999'
        r = self.post_json('/schedule/update_group', payload)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        docs = self.group_docs()
        touched = [i for i, d in docs.items() if d.get('updatedAt') is not None]
        self.assertEqual(touched, [target['id']], 'переписаны занятия без изменений')
        self.assertEqual(docs[target['id']]['room'], '999')
        updates = [l for l in self.new_logs() if l['action'] == 'update']
        self.assertEqual(len(updates), 1)
        group_pushes = [p for p in _sent_pushes() if p['topic'] == GROUP_TOPIC]
        self.assertEqual(len(group_pushes), 1, _sent_pushes())
        self.assertEqual(group_pushes[0]['data'].get('type'), 'schedule')

    def test_mass_writes_are_marked_notify_false(self):
        from cms import routes_schedule
        payload = self.full_payload()
        target = payload['days'][0]['lessons'][0]
        target['room'] = '998'
        # Без последующей очистки флага видно, что массовая запись шла с notify:false.
        with mock.patch.object(routes_schedule, 'CLEAR_NOTIFY_AFTER_WRITE', False):
            self.post_json('/schedule/update_group', payload)
        self.assertIs(self.doc('schedules', target['id']).get('notify'), False)

    def test_notify_flag_is_cleared_after_mass_write(self):
        payload = self.full_payload()
        target = payload['days'][0]['lessons'][0]
        target['room'] = '997'
        self.post_json('/schedule/update_group', payload)
        self.assertNotIn('notify', self.doc('schedules', target['id']))

    def test_noop_save_writes_nothing_and_sends_nothing(self):
        r = self.post_json('/schedule/update_group', self.full_payload())
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertFalse([d for d in self.group_docs().values() if d.get('updatedAt') is not None])
        self.assertEqual(_sent_pushes(), [])
        self.assertEqual(self.new_logs(), [])

    def test_delete_group_sends_single_push(self):
        n = len(self.group_docs())
        r = self.post('/schedule/delete_group/' + GROUP)
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.group_docs(), {})
        self.assertEqual(len([p for p in _sent_pushes() if p['topic'] == GROUP_TOPIC]), 1)
        self.assertEqual(len([l for l in self.new_logs() if l['action'] == 'delete']), n)
        self.assertIsNotNone(self.doc('schedules', 'cf_petrov_a'))  # другие группы на месте


class LogFormatBugTest(_ScheduleCase):
    """Журнал: action 'add' (не 'create'), timestamp/changed_at — Timestamp, а не число."""

    def test_builder_log_entry_format(self):
        self.post('/schedule/builder', {
            'groupId': 'МЕН 25-1', 'day_of_week': ['1'], 'lesson_day_rel': ['0'], 'time': ['17.55-18.45'],
            'teacher': ['t_li'], 'subject': ['Компьютерные сети'], 'room': ['А-12'], 'week_type': ['always']})
        logs = self.new_logs()
        self.assertEqual(len(logs), 1)
        entry = logs[0]
        self.assertEqual(entry['action'], 'add')
        self.assertIsInstance(entry['timestamp'], datetime)
        self.assertIsInstance(entry['changed_at'], datetime)
        self.assertEqual(entry['group_id'], 'МЕН 25-1')
        self.assertEqual(entry['changed_by'], 'admin_main')
        self.assertEqual(entry['new_data'].get('day_index'), 1)


class WeekAndTodayBugTest(_ScheduleCase):
    """Чётность недели — как isNumeratorWeek в APP и по Алматы (раньше — UTC и другая формула)."""

    def test_week_label_matches_app_formula(self):
        # 1 сентября 2025 — понедельник: старая веб-формула давала «знаменатель», APP — числитель.
        with mock.patch('cms.schedule_model.today_almaty', return_value=date(2025, 9, 3)):
            html = self.get_ok('/schedule?group=' + GROUP)
        self.assertRegex(html, r'data-current-week="numerator"')
        with mock.patch('cms.schedule_model.today_almaty', return_value=date(2025, 9, 10)):
            html = self.get_ok('/schedule?group=' + GROUP)
        self.assertRegex(html, r'data-current-week="denominator"')


class FormKeepsCurrentValuesBugTest(_ScheduleCase):
    """Формы правки не должны подменять значения, которых нет в справочниках."""

    def assertSelected(self, html, value):
        self.assertRegex(html, r'<option[^>]*value="%s"[^>]*\bselected' % re.escape(value))

    def test_edit_form_keeps_orphan_teacher_and_offgrid_time(self):
        self.assertSelected(self.get_ok('/schedule/edit/q_orphan'), 'uid_deleted_teacher_7')
        self.assertSelected(self.get_ok('/schedule/edit/q_offgrid'), '09:00-10:20')
        self.assertSelected(self.get_ok('/schedule/edit/cf_byname'), 'Смирнова Елена Викторовна')

    def test_edit_group_keeps_values(self):
        html = self.get_ok('/schedule/edit_group/ЭК 24-1')
        self.assertSelected(html, '09:00-10:20')            # q_offgrid
        html = self.get_ok('/schedule/edit_group/ЭК 25-1')
        self.assertSelected(html, 'Смирнова Елена Викторовна')  # cf_byname
        self.assertSelected(html, 'legacy_t1')              # q_legacy

    def test_unchanged_post_keeps_values(self):
        src = self.doc('schedules', 'q_orphan')
        r = self.post('/schedule/edit/q_orphan', {
            'groupId': src['groupId'], 'day': '3', 'time': src['time'], 'subject': src['subject'],
            'teacher': src['teacher'], 'room': src['room'], 'week_type': src['week_type']})
        self.assertEqual(r.status_code, 302)
        doc = self.doc('schedules', 'q_orphan')
        self.assertEqual(doc['teacher'], 'uid_deleted_teacher_7')
        self.assertEqual(doc['time'], '13.00-13.50')
        self.assertIsNone(doc.get('updatedAt'), 'сохранение без изменений не должно писать')


class ReadPathsBugTest(_ScheduleCase):
    """Все страницы/экспорты видят рекуррентные и воскресные занятия; ФИО и uid — один преподаватель."""

    def test_group_page_has_no_unknown_day(self):
        html = self.get_ok('/schedule?group=' + GROUP)
        self.assertNotIn('Неизвестно', html)
        self.assertIn('Воскресенье', self.get_ok('/schedule?group=ИС 24-1'))

    def test_all_export_csv_contains_every_lesson_with_day(self):
        from cms.schedule_model import load_lessons_snapshot
        snap = load_lessons_snapshot(force=True)
        expected = len([l for l in snap.lessons if l['groupId']])  # и занятия без дня — с пометкой «не указан»
        r = self.client.get('/schedule/all/export?format=csv')
        self.assertEqual(r.status_code, 200)
        text = r.get_data(as_text=True).lstrip('﻿')
        rows = [x for x in text.splitlines() if x.strip()]
        self.assertEqual(len(rows) - 1, expected)
        self.assertIn('Воскресенье', text)
        self.assertIn('не указан', text)

    def test_teacher_api_merges_uid_and_fio(self):
        data = self.client.get('/api/schedule/teacher/t_smirnova').get_json()
        ids = {l['id'] for l in data['schedule']}
        self.assertIn('cf_byname', ids)   # записано ФИО текстом
        self.assertIn('cf_room_b', ids)   # записано uid


# ==================== НОВЫЕ ФУНКЦИИ ====================

class PagesTest(CmsTestCase):
    """Страницы раздела открываются и показывают данные (один сид на класс — только чтение)."""

    def test_pages_render(self):
        for url in ('/schedule', '/schedule?group=' + GROUP, '/schedule?group=' + GROUP + '&view=list',
                    '/schedule?q=ПО&course=3', '/schedule?major=ИС&week=current',
                    '/schedule/all', '/schedule/all?week=numerator&course=2', '/schedule/all?day=6',
                    '/schedule/teachers', '/schedule/teachers?view=table', '/schedule/teachers?teacher=uid:t_petrov',
                    '/schedule/teachers?teacher=t_petrov&day=2', '/schedule/teachers?q=Ким',
                    '/schedule/builder', '/schedule/builder?groupId=' + GROUP,
                    '/schedule/edit/base_00_1_0', '/schedule/edit/q_ts_date', '/schedule/edit/q_no_day',
                    '/schedule/edit_group/' + GROUP, '/schedule/logs', '/schedule/logs?group=ПО 23-1',
                    '/schedule/logs?action=add', '/schedule/print?group=' + GROUP, '/schedule/print?course=3'):
            with self.subTest(url=url):
                self.get_ok(url)

    def test_group_page_content(self):
        html = self.get_ok('/schedule?group=' + GROUP)
        self.assertIn('Физика', html)
        self.assertIn('Ахметова Динара Маратовна', html)  # имя из справочника, не uid
        self.assertNotIn('onsubmit="return confirm', html)  # подтверждения — через data-confirm

    def test_group_list_search(self):
        html = self.get_ok('/schedule?q=ЭК')
        self.assertIn('ЭК 24-1', html)
        self.assertNotIn('data-group-card="ПО 24-1"', html)

    def test_teachers_page_single_card_for_fio_and_uid(self):
        html = self.get_ok('/schedule/teachers?q=Смирнова')
        self.assertEqual(len(re.findall(r'data-teacher-card="uid:t_smirnova"', html)), 1)

    def test_teachers_page_shows_possible_overlaps(self):
        html = self.get_ok('/schedule/teachers?teacher=uid:t_petrov')
        self.assertIn('data-overlap', html)

    def test_missing_lesson_redirects(self):
        r = self.client.get('/schedule/edit/nope')
        self.assertRedirectsTo(r, '/schedule')

    def test_logs_filters(self):
        html = self.get_ok('/schedule/logs?group=ПО 23-1')
        self.assertIn('ПО 23-1', html)
        self.assertNotIn('Черчение', html)  # лог группы ВТ 24-1
        html = self.get_ok('/schedule/logs?action=delete')
        self.assertIn('Черчение', html)
        html = self.get_ok('/schedule/logs?action=add')
        self.assertIn('Физика', html)  # старая запись action='create' считается добавлением

    def test_xlsx_exports_are_valid(self):
        for url in ('/schedule/all/export?format=xlsx', '/schedule/all/export?format=xlsx&group=' + GROUP,
                    '/schedule/teachers/export?format=xlsx', '/schedule/teachers/export?format=xlsx&teacher=uid:t_li'):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 200)
                self.assertIn('spreadsheetml', r.headers['Content-Type'])
                with zipfile.ZipFile(io.BytesIO(r.data)) as z:
                    names = z.namelist()
                    self.assertIn('xl/workbook.xml', names)
                    self.assertIn('xl/worksheets/sheet1.xml', names)
                    sheet = z.read('xl/worksheets/sheet1.xml').decode('utf-8')
                    self.assertIn('<sheetData>', sheet)
                    z.testzip()

    def test_csv_exports_respect_filters(self):
        r = self.client.get('/schedule/all/export?format=csv&group=' + GROUP)
        text = r.get_data(as_text=True)
        self.assertIn(GROUP, text)
        self.assertNotIn('ЭК 24-1', text)
        r = self.client.get('/schedule/teachers/export?format=csv&teacher=uid:t_li')
        text = r.get_data(as_text=True)
        self.assertIn('Ли Сергей Николаевич', text)
        self.assertNotIn('Петров Дмитрий Игоревич', text)

    def test_export_without_format_redirects_to_xlsx(self):
        for url in ('/schedule/all/export', '/schedule/teachers/export?teacher=uid:t_li'):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn('format=xlsx', r.headers['Location'])

    def test_pdf_export_goes_to_print_page(self):
        r = self.client.get('/schedule/all/export?format=pdf&group=' + GROUP)
        self.assertRedirectsTo(r, '/schedule/print')

    def test_api_endpoints(self):
        data = self.client.get('/api/schedule/group/' + GROUP).get_json()
        self.assertTrue(data['success'])
        self.assertGreater(data['count'], 10)
        self.assertTrue(all(l.get('day_index') is not None for l in data['schedule']))
        data = self.client.get('/api/schedule/all').get_json()
        self.assertIn(GROUP, data['schedule'])
        self.assertIn('current_week', data)

    def test_get_delete_is_not_allowed(self):
        self.assertEqual(self.client.get('/schedule/delete/base_00_1_0').status_code, 405)
        self.assertEqual(self.client.get('/schedule/delete_group/' + GROUP).status_code, 405)

    def test_post_without_csrf_is_rejected(self):
        r = self.client.post('/schedule/delete/base_00_1_0')
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(self.doc('schedules', 'base_00_1_0'))


class ConflictWarningTest(_ScheduleCase):
    """Предупреждение о накладке при сохранении — через cms.conflicts.check_lesson (не блокирует)."""

    FAKE = [{'severity': 'critical', 'message': 'Петров Д. И. уже ведёт пару у ВТ 24-1 в это время'}]

    def test_edit_flashes_conflict_warning(self):
        src = self.doc('schedules', 'base_01_1_1')
        with mock.patch('cms.conflicts.check_lesson', return_value=self.FAKE) as m:
            r = self.post('/schedule/edit/base_01_1_1', {
                'groupId': GROUP, 'day': '1', 'time': src['time'], 'subject': src['subject'],
                'teacher': 't_petrov', 'room': src['room'], 'week_type': 'always'})
        self.assertEqual(r.status_code, 302)
        self.assertTrue(m.called)
        self.assertEqual(self.doc('schedules', 'base_01_1_1')['teacher'], 't_petrov')  # не блокирует
        messages = ' '.join(t for _c, t in self.flashes())
        self.assertIn('уже ведёт пару', messages)

    def test_edit_page_shows_current_conflicts(self):
        with mock.patch('cms.conflicts.check_lesson', return_value=self.FAKE):
            html = self.get_ok('/schedule/edit/base_01_1_1')
        self.assertIn('уже ведёт пару', html)

    def test_check_api(self):
        with mock.patch('cms.conflicts.check_lesson', return_value=self.FAKE):
            r = self.post_json('/api/schedule/check', {'lessons': [
                {'key': 'row1', 'groupId': GROUP, 'day': 2, 'time': '13.00-13.50', 'teacher': 't_petrov',
                 'subject': 'Физика', 'room': '204', 'week_type': 'always'}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertEqual(data['results'][0]['key'], 'row1')
        self.assertIn('уже ведёт пару', data['results'][0]['conflicts'][0]['text'])

    def test_broken_engine_does_not_break_save(self):
        src = self.doc('schedules', 'base_01_1_1')
        with mock.patch('cms.conflicts.check_lesson', side_effect=RuntimeError('boom')):
            r = self.post('/schedule/edit/base_01_1_1', {
                'groupId': GROUP, 'day': '1', 'time': src['time'], 'subject': src['subject'],
                'teacher': src['teacher'], 'room': '555', 'week_type': 'always'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('schedules', 'base_01_1_1')['room'], '555')


class MassActionsTest(_ScheduleCase):
    """Копирование расписания группы и массовые действия над выбранными занятиями."""

    def test_copy_group_append_and_replace(self):
        src = self.group_docs(GROUP)
        target = 'ПО 25-1'
        before = self.group_docs(target)
        r = self.post('/schedule/copy_group', {'source': GROUP, 'target': target, 'mode': 'append'})
        self.assertRedirectsTo(r, '/schedule')
        after = self.group_docs(target)
        self.assertEqual(len(after), len(before) + len(src))
        for d in after.values():
            if d['id'] not in before:
                self.assertIsNotNone(d.get('day_index'))
                self.assertEqual(datetime.strptime(d['date'], '%Y-%m-%d').weekday(), d['day_index'])
        self.assertEqual(len([p for p in _sent_pushes() if p['topic'] == 'schedule_group_po_25-1']), 1)

        _sent_pushes().clear()
        r = self.post('/schedule/copy_group', {'source': GROUP, 'target': target, 'mode': 'replace'})
        self.assertRedirectsTo(r, '/schedule')
        self.assertEqual(len(self.group_docs(target)), len(src))
        self.assertTrue(self.audit_entries('schedule_copy'))

    def test_copy_group_validation(self):
        r = self.post('/schedule/copy_group', {'source': GROUP, 'target': GROUP, 'mode': 'append'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.new_logs(), [])

    def test_bulk_delete_and_week(self):
        ids = sorted(self.group_docs())[:3]
        r = self.post('/schedule/bulk', {'action': 'week', 'week_type': 'denominator', 'ids': ids, 'group': GROUP})
        self.assertEqual(r.status_code, 302)
        for i in ids:
            self.assertEqual(self.doc('schedules', i)['week_type'], 'denominator')
        r = self.post('/schedule/bulk', {'action': 'delete', 'ids': ids + ['cf_petrov_a'], 'group': GROUP})
        self.assertEqual(r.status_code, 302)
        for i in ids:
            self.assertIsNone(self.doc('schedules', i))
        self.assertIsNotNone(self.doc('schedules', 'cf_petrov_a'))  # чужая группа не тронута
        self.assertEqual(len([p for p in _sent_pushes() if p['topic'] == GROUP_TOPIC]), 2)

    def test_single_delete(self):
        r = self.post('/schedule/delete/base_01_0_0')
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('schedules', 'base_01_0_0'))
        logs = self.new_logs()
        self.assertEqual([l['action'] for l in logs], ['delete'])
        self.assertTrue(self.audit_entries('schedule_delete'))

    def test_builder_validation_and_audit(self):
        r = self.post('/schedule/builder', {
            'groupId': 'МЕН 25-1', 'day_of_week': ['9'], 'lesson_day_rel': ['0'], 'time': ['17.55-18.45'],
            'teacher': ['t_li'], 'subject': ['Компьютерные сети'], 'room': [''], 'week_type': ['always']})
        self.assertEqual(r.status_code, 400)  # форма показана снова, ошибка — у строки
        self.assertIn('Выберите день недели', r.get_data(as_text=True))
        self.assertEqual(self.new_logs(), [])  # день 9 — неверный, ничего не создано
        r = self.post('/schedule/builder', {
            'groupId': 'МЕН 25-1', 'day_of_week': ['0', '4'], 'lesson_day_rel': ['0', '1', '1'],
            'time': ['17.20-18.10', '17.55-18.45', '16.50-17.40'], 'teacher': ['t_li', 't_li', 't_kim'],
            'subject': ['Компьютерные сети', 'Компьютерные сети', 'Физическая культура'],
            'room': ['А-12', 'А-12', 'Спортзал'], 'week_type': ['always', 'numerator', 'always']})
        self.assertEqual(r.status_code, 302)
        created = [l for l in self.new_logs() if l['action'] == 'add']
        self.assertEqual(len(created), 3)
        self.assertEqual(sorted(l['new_data']['day_index'] for l in created), [0, 4, 4])
        self.assertTrue(self.audit_entries('schedule_create'))
        self.assertEqual(len([p for p in _sent_pushes() if p['topic'] == 'schedule_group_men_25-1']), 1)


# ==================== UX v2: ПРЕДЗАПОЛНЕНИЕ, ЗАМЕНА ПРЕПОДАВАТЕЛЯ, НАКЛАДКИ НА МЕСТЕ ПРАВКИ ====================

class BuilderPrefillTest(CmsTestCase):
    """SCH-10: «+» в колонке дня, «Добавить занятие здесь» (шахматка) и главная открывают конструктор
    с заполненной первой строкой: ?groupId=&day=&time=HH:MM-HH:MM&room=."""

    def assertSelected(self, html, value):
        self.assertRegex(html, r'<option[^>]*value="%s"[^>]*\bselected' % re.escape(value))

    def test_prefill_first_row(self):
        html = self.get_ok('/schedule/builder?groupId=%s&day=2&time=13:00-13:50&room=204' % GROUP)
        self.assertSelected(html, GROUP)                      # группа
        self.assertSelected(html, '13.00-13.50')              # время → метка слота сетки звонков
        self.assertIn('value="204"', html)                    # аудитория
        wed = html[html.index('data-day-section="2"'):html.index('data-day-section="3"')]
        self.assertIn('data-key="n0"', wed)                   # строка — в секции среды

    def test_prefill_offgrid_time_and_garbage(self):
        html = self.get_ok('/schedule/builder?day=1&time=09:05-09:55')
        self.assertSelected(html, '09.05-09.55')              # не из сетки — показано как есть
        self.get_ok('/schedule/builder?day=99&time=zzz&room=' + 'x' * 200)   # мусор не ломает страницу


class BulkTeacherTest(_ScheduleCase):
    """SCH-11 (контракт для PEOPLE): POST /schedule/bulk action=teacher — замена преподавателя у выбранных занятий."""

    def test_replace_teacher_in_two_lessons(self):
        ids = sorted(i for i, d in self.group_docs().items() if d.get('teacher') != 't_li')[:2]
        r = self.post('/schedule/bulk', {'action': 'teacher', 'teacher': 't_li', 'ids': ids,
                                         'next': '/schedule?group=' + GROUP})
        self.assertRedirectsTo(r, '/schedule')
        for i in ids:
            doc = self.doc('schedules', i)
            self.assertEqual(doc['teacher'], 't_li')
            self.assertNotIn('teacherId', doc)
            self.assertNotIn('notify', doc)                    # notify:false снят после записи (D15)
            self.assertEqual(_weekday_of(doc), doc['day_index'])
        self.assertIn('Преподаватель заменён в 2 занятиях', ' '.join(t for _c, t in self.flashes()))
        audit = [a for a in self.audit_entries('schedule_bulk') if (a.get('details') or {}).get('action') == 'teacher']
        self.assertTrue(audit)
        self.assertEqual(audit[-1]['details']['value'], 't_li')
        self.assertEqual(len([p for p in _sent_pushes() if p['topic'] == GROUP_TOPIC]), 1)
        self.assertEqual(len([l for l in self.new_logs() if l['action'] == 'update']), 2)

    def test_unknown_teacher_is_rejected(self):
        ids = sorted(self.group_docs())[:1]
        before = self.doc('schedules', ids[0])['teacher']
        r = self.post('/schedule/bulk', {'action': 'teacher', 'teacher': 'uid_nobody', 'ids': ids})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('schedules', ids[0])['teacher'], before)
        self.assertIn('из справочника', ' '.join(t for _c, t in self.flashes()))
        self.assertEqual(self.new_logs(), [])

    def test_teacher_from_limits_to_his_lessons(self):
        mine = sorted(i for i, d in self.group_docs().items() if d.get('teacher') == 't_akhmetova')
        other = sorted(i for i, d in self.group_docs().items() if d.get('teacher') not in ('t_akhmetova', 't_li'))[:1]
        self.assertTrue(mine and other)
        r = self.post('/schedule/bulk', {'action': 'teacher', 'teacher': 't_li', 'teacher_from': 't_akhmetova',
                                         'ids': mine + other + ['cf_petrov_a']})
        self.assertEqual(r.status_code, 302)
        for i in mine:
            self.assertEqual(self.doc('schedules', i)['teacher'], 't_li')
        self.assertNotEqual(self.doc('schedules', other[0])['teacher'], 't_li')   # чужое занятие не тронуто
        self.assertEqual(self.doc('schedules', 'cf_petrov_a')['teacher'], 't_petrov')

    def test_week_short_form_from_bulkbar(self):
        ids = sorted(self.group_docs())[:2]
        r = self.post('/schedule/bulk', {'action': 'week:numerator', 'ids': ids, 'group': GROUP})
        self.assertEqual(r.status_code, 302)
        for i in ids:
            self.assertEqual(self.doc('schedules', i)['week_type'], 'numerator')


class EditUxTest(_ScheduleCase):
    """SCH-4: возврат в список с фильтрами, тост «Открыть группу», быстрый просмотр формы."""

    def test_save_returns_to_next_and_offers_group(self):
        src = self.doc('schedules', 'base_01_1_1')
        nxt = '/schedule/conflicts?dim=teacher'
        r = self.post('/schedule/edit/base_01_1_1', {
            'groupId': GROUP, 'day': '1', 'time': src['time'], 'subject': src['subject'],
            'teacher': src['teacher'], 'room': '777', 'week_type': 'always', 'next': nxt})
        self.assertRedirectsTo(r, nxt)
        flashes = self.flashes()
        self.assertIn(('success', 'Занятие сохранено'), flashes)
        actions = [t for c, t in flashes if c == 'cc-action']
        self.assertTrue(actions and 'Открыть группу' in actions[0])

    def test_save_from_group_week_without_extra_action(self):
        src = self.doc('schedules', 'base_01_1_1')
        r = self.post('/schedule/edit/base_01_1_1', {
            'groupId': GROUP, 'day': '1', 'time': src['time'], 'subject': src['subject'],
            'teacher': src['teacher'], 'room': '778', 'week_type': 'always', 'next': '/schedule?group=' + GROUP})
        self.assertRedirectsTo(r, '/schedule')
        self.assertFalse([t for c, t in self.flashes() if c == 'cc-action'])

    def test_partial_render_for_quick_view(self):
        r = self.client.get('/schedule/edit/base_01_1_1?next=/schedule', headers={'X-CC-Partial': '1'})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn('cc-sidebar', html)
        self.assertIn('data-lesson-form', html)
        self.assertIn('data-live-conflicts', html)
        self.assertNotIn('История занятия', html)            # в панели — без истории

    def test_edit_page_layout(self):
        html = self.get_ok('/schedule/edit/cf_petrov_a')
        self.assertIn('data-free-rooms', html)               # подсказка свободных аудиторий
        self.assertIn('data-cc-savebar', html)               # панель сохранения + Ctrl+S
        self.assertRegex(html, r'data-critical="[1-9]')       # критичная накладка → подтверждение при сохранении
        self.assertIn('/schedule/teachers?teacher=uid', html)  # переход к неделе преподавателя


class ConflictsInPlaceTest(CmsTestCase):
    """Накладки видны там, где правят расписание: неделя группы, карточки групп, преподаватели."""

    def test_group_week_marks_engine_conflicts(self):
        html = self.get_ok('/schedule?group=ВТ 24-1')
        self.assertIn('data-overlap', html)                  # cf_petrov_a: Петров занят у МЕН 25-1
        self.assertIn('накладк', html)
        self.assertIn('/schedule/conflicts?', html)

    def test_group_cards_show_conflict_badge_and_instant_search(self):
        html = self.get_ok('/schedule')
        self.assertRegex(html, r'data-group-card="ВТ 24-1"[\s\S]{0,600}sch-group-card__conf')
        self.assertIn('data-cc-filter="#schGroups"', html)

    def test_teachers_table_is_default_and_week_view(self):
        html = self.get_ok('/schedule/teachers')
        self.assertIn('id="schTeachers"', html)
        self.assertIn('<table', html)
        self.assertIn('title="Числитель:', html)              # одно число, Ч/З — в подсказке
        week = self.get_ok('/schedule/teachers?teacher=uid:t_petrov')
        self.assertIn('id="schWeek"', week)
        self.assertIn('schHandover', week)                    # «Передать занятия другому…» (SCH-11)
        cards = self.get_ok('/schedule/teachers?view=cards')
        self.assertIn('sch-teacher-card', cards)

    def test_day_switch_marks_days_with_conflicts(self):
        # на телефоне видна одна колонка — день с накладкой отмечен точкой в переключателе
        html = self.get_ok('/schedule?group=ВТ 24-1')
        self.assertRegex(html, r'class="[^"]*has-alert" data-day="2"')        # ср, 13:00 — Петров
        self.assertNotRegex(html, r'class="[^"]*has-alert" data-day="0"')     # в пн накладок нет
        self.assertIn('есть накладки', html)
        teacher = self.get_ok('/schedule/teachers?teacher=uid:t_petrov')
        self.assertRegex(teacher, r'class="[^"]*has-alert" data-day="2"')

    def test_mobile_day_switch_and_hotkeys_markup(self):
        html = self.get_ok('/schedule?group=' + GROUP)
        self.assertIn('data-sch-dayswitch', html)
        self.assertIn('is-active-day', html)
        self.assertIn('data-sch-next', html)


def _orig(l, day):
    return {'day': day, 'time': l['time'], 'subject': l['subject'], 'teacher': l['teacher'],
            'room': l['room'], 'week_type': l.get('week_type') or 'always'}


def _row(lesson_id, l, day, room):
    return {'key': lesson_id, 'id': lesson_id, 'day': day, 'time': l['time'], 'subject': l['subject'],
            'teacher': l['teacher'], 'room': room, 'week_type': l.get('week_type') or 'always', 'orig': _orig(l, day)}


class EditorConcurrencyTest(_ScheduleCase):
    """DATA-1: строка занятия, которое другой админ удалил или перенёс в другую группу, не создаётся заново."""

    def test_row_of_concurrently_deleted_lesson_is_not_recreated(self):
        from cms.core import db
        l = self.doc('schedules', 'base_00_1_0')
        db.collection('schedules').document('base_00_1_0').delete()      # другой админ удалил занятие
        r = self.post_json('/schedule/update_group', {'group_id': 'ПО 23-1', 'deleted_ids': [],
                                                      'lessons': [_row('base_00_1_0', l, 1, '999')]})
        self.assertEqual(r.status_code, 409, r.get_json())
        self.assertEqual([e['key'] for e in r.get_json()['errors']], ['base_00_1_0'])
        self.assertIn('обновите страницу', r.get_json()['error'])
        self.assertEqual([d for d in self.docs('schedules', groupId='ПО 23-1') if d.get('room') == '999'], [])
        self.assertEqual(_sent_pushes(), [])

    def test_row_of_lesson_moved_to_other_group_is_not_duplicated(self):
        from cms.core import db
        l = self.doc('schedules', 'base_00_1_1')
        db.collection('schedules').document('base_00_1_1').update({'groupId': 'ПО 24-1'})
        new = {'key': 'new1', 'day': 4, 'time': '17.55-18.45', 'subject': 'Физика', 'teacher': 't_li',
               'room': '777', 'week_type': 'always'}
        r = self.post_json('/schedule/update_group', {'group_id': 'ПО 23-1', 'deleted_ids': [],
                                                      'lessons': [_row('base_00_1_1', l, 1, '998'), new]})
        self.assertEqual(r.status_code, 409)
        rooms = {d.get('room') for d in self.docs('schedules', groupId='ПО 23-1')}
        self.assertNotIn('998', rooms)
        self.assertNotIn('777', rooms)      # ничего не записано — ни одной строки
        self.assertEqual(self.doc('schedules', 'base_00_1_1')['groupId'], 'ПО 24-1')


class PartialCommitTest(_ScheduleCase):
    """DATA-2: сбой посередине массовой записи — notify:false снимается, ответ честный (часть сохранена)."""

    def _flaky(self, fail_when):
        from cms import routes_schedule as rs
        orig = rs.ScheduleBatch._commit_ops

        def flaky(ops):
            ops = list(ops)
            if fail_when(ops):
                raise RuntimeError('Firestore: deadline exceeded')
            return orig(ops)
        return mock.patch.object(rs.ScheduleBatch, '_commit_ops', staticmethod(flaky))

    def _payload(self):
        upd = self.doc('schedules', 'base_00_1_2')
        return {'group_id': 'ПО 23-1', 'lessons': [_row('base_00_1_2', upd, 1, '777')],
                'deleted_ids': ['base_00_1_3']}

    def test_failed_delete_phase_heals_notify_and_reports_partial(self):
        with self._flaky(lambda ops: ops and all(kind == 'delete' for kind, _r, _d in ops)):
            r = self.post_json('/schedule/update_group', self._payload())
        data = r.get_json()
        self.assertTrue(data.get('partial'), data)
        self.assertIn('/schedule/edit_group/', data['redirect'])     # редактор перезагрузится со свежими данными
        self.assertNotIn('Попробуйте ещё раз', data.get('message', '') + data.get('error', ''))
        after_upd = self.doc('schedules', 'base_00_1_2')
        self.assertEqual(after_upd['room'], '777')
        self.assertNotIn('notify', after_upd, 'правка записана, но notify:false остался навсегда')
        after_del = self.doc('schedules', 'base_00_1_3')
        self.assertTrue(after_del is None or 'notify' not in after_del, 'неудалённое занятие осталось с notify:false')
        self.assertIn('warning', [c for c, _t in self.flashes()])

    def test_failed_first_write_is_plain_error(self):
        with self._flaky(lambda ops: True):
            r = self.post_json('/schedule/update_group', self._payload())
        self.assertEqual(r.status_code, 500)
        self.assertNotEqual(self.doc('schedules', 'base_00_1_2')['room'], '777')
        self.assertIsNotNone(self.doc('schedules', 'base_00_1_3'))

    def test_single_edit_clears_stale_notify_false(self):
        from cms.core import db
        db.collection('schedules').document('base_01_1_1').update({'notify': False})   # «залип» после сбоя
        src = self.doc('schedules', 'base_01_1_1')
        r = self.post('/schedule/edit/base_01_1_1', {
            'groupId': GROUP, 'day': '1', 'time': src['time'], 'subject': src['subject'],
            'teacher': src['teacher'], 'room': '909', 'week_type': src.get('week_type', 'always')})
        self.assertEqual(r.status_code, 302)
        doc = self.doc('schedules', 'base_01_1_1')
        self.assertEqual(doc['room'], '909')
        self.assertNotIn('notify', doc)


class AcceptedConflictsHighlightTest(_ScheduleCase):
    """DATA-6: принятая накладка не подсвечивается снова на неделе группы и у преподавателя."""

    def _accept(self, lesson_id):
        from cms import conflicts
        a = conflicts.get_analysis()
        hashes = [c['hash'] for c in a.all_clusters() if lesson_id in c['ids']]
        self.assertTrue(hashes)
        r = self.post('/schedule/conflicts/ack', {'hash': hashes, 'note': 'так задумано'})
        self.assertIn(r.status_code, (302, 303))

    def test_accepted_group_conflict_not_highlighted(self):
        self._accept('cf_grp_sub_a')
        html = self.get_ok('/schedule?group=%D0%9F%D0%9E+25-1&view=list')
        row = re.search(r'<tr class="([^"]*)" data-href="[^"]*/schedule/edit/cf_grp_sub_a', html)
        self.assertIsNotNone(row)
        self.assertNotIn('is-conflict', row.group(1), 'принятая накладка снова подсвечена как пересечение')

    def test_accepted_teacher_conflict_not_counted(self):
        before = self.get_ok('/schedule/teachers?teacher=uid:t_petrov')
        self.assertRegex(before, r'class="[^"]*has-alert" data-day="2"')
        self._accept('cf_petrov_a')
        after = self.get_ok('/schedule/teachers?teacher=uid:t_petrov')
        self.assertNotRegex(after, r'class="[^"]*has-alert" data-day="2"')

    def test_fallback_highlight_when_engine_fails(self):
        with mock.patch('cms.conflicts.get_analysis', side_effect=RuntimeError('движок упал')):
            html = self.get_ok('/schedule?group=%D0%9F%D0%9E+25-1&view=list')
        row = re.search(r'<tr class="([^"]*)" data-href="[^"]*/schedule/edit/cf_grp_sub_a', html)
        self.assertIn('is-conflict', row.group(1))


# ==================== БЛАНКИ ДЛЯ ПЕЧАТИ ====================

_MARK_RE = re.compile(r'class="sch-sheet__mark sch-sheet__mark--(numerator|denominator)" title=')
_SHEET_RE = re.compile(r'<section class="sch-sheet sch-sheet--(\w+)(?: sch-sheet--fill)?" style="--sch-row: ([\d.]+)mm" data-rows="(\d+)"')


def _sheets(html):
    """[(класс плотности, высота строки мм, строк)] бланков страницы."""
    return [(d, float(r), int(n)) for d, r, n in _SHEET_RE.findall(html)]


def _week_of(doc):
    w = doc.get('week_type') or 'always'
    return w if w in ('always', 'numerator', 'denominator') else 'always'


class SheetLayoutTest(CmsTestCase):
    """Плотность бланка по числу строк (считает сервер): группа всегда на одном листе A4."""
    login_as_admin = False

    @staticmethod
    def _by_day(counts):
        return OrderedDict((d, [{}] * n) for d, n in enumerate(counts) if n)

    def test_density_grows_with_rows(self):
        from cms.routes_schedule import sheet_layout
        cases = [((2, 1, 1, 1), 'roomy'), ((3, 3, 3, 3, 3), 'normal'), ((4, 4, 4, 4, 5), 'compact'),
                 ((5, 5, 5, 5, 6), 'dense'), ((6, 6, 6, 6, 6, 4), 'tight'), ((8, 8, 8, 8, 8, 8), 'ultra')]
        rows = []
        for counts, density in cases:
            with self.subTest(rows=sum(counts)):
                lay = sheet_layout(self._by_day(counts))
                self.assertEqual(lay['density'], density)
                self.assertEqual(lay['rows'], sum(counts))
                rows.append(float(lay['row_mm']))
        self.assertEqual(rows, sorted(rows, reverse=True), 'чем больше пар, тем ниже строка')
        self.assertLessEqual(rows[0], 19.0, 'мало пар — строки выше, но без растягивания на весь лист')

    def test_table_fills_sheet_from_eight_rows(self):
        """Как старый бланк: таблица на всю высоту листа; одна-две пары — не растягиваем в пустую рамку."""
        from cms.routes_schedule import sheet_layout
        self.assertFalse(sheet_layout(self._by_day((2, 1, 1, 1)))['fill'])
        self.assertTrue(sheet_layout(self._by_day((2, 2, 2, 2)))['fill'])
        self.assertTrue(sheet_layout(self._by_day((4, 4, 4, 4, 5)))['fill'])

    def test_rows_fit_one_sheet(self):
        from cms.routes_schedule import PRINT_BODY_MM, PRINT_DENSITY, sheet_layout
        natural = {name: nat for name, _lim, nat, _cap, _pt in PRINT_DENSITY}
        for n in range(1, 61):
            for kind in ('group', 'teacher'):
                lay = sheet_layout(self._by_day([n]), kind)
                if lay['density'] != 'ultra':
                    self.assertLessEqual(n * natural[lay['density']], PRINT_BODY_MM[kind], (n, kind, lay))

    def test_teacher_sheet_is_denser_than_group(self):
        """У бланка преподавателя в шапке ещё ФИО — тот же объём уходит в более плотный класс раньше."""
        from cms.routes_schedule import sheet_layout
        by_day = self._by_day((5, 5, 5, 4, 4))
        self.assertEqual(sheet_layout(by_day)['density'], 'compact')
        self.assertEqual(sheet_layout(by_day, 'teacher')['density'], 'dense')

    def test_short_day_name_only_when_needed(self):
        from cms.routes_schedule import sheet_layout
        lay = sheet_layout(self._by_day((6, 6, 6, 6, 6, 1)))   # суббота — одна пара в плотном бланке
        self.assertEqual(lay['day_labels'][5], 'Сб')
        self.assertEqual(lay['day_labels'][0], 'Понедельник')
        lay = sheet_layout(self._by_day((2, 2)))
        self.assertEqual(lay['day_labels'], {0: 'Понедельник', 1: 'Вторник'})


class PrintSheetsTest(CmsTestCase):
    """/schedule/print — как старый бумажный бланк; /schedule/print/teacher — бланк преподавателя."""

    def test_group_sheet_like_old_blank(self):
        html = self.get_ok('/schedule/print?group=' + GROUP)
        self.assertIn('Расписание занятий', html)
        self.assertIn('Группа: ' + GROUP, html)
        self.assertIn('<th scope="col" colspan="2">Время</th>', html)
        self.assertIn('<th scope="col">Ауд.</th>', html)
        # день — один вертикальный столбец на все пары дня
        days = re.findall(r'<th scope="rowgroup" rowspan="(\d+)" class="sch-sheet__day"[^>]*><span>([^<]+)</span>', html)
        self.assertEqual([d for _n, d in days], ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница'])
        # ФИО преподавателя — жирным, под дисциплиной
        self.assertRegex(html, r'<span class="sch-sheet__subject">Физика[^<]*(<span[^>]*>[ЧЗ]</span>)?</span>'
                               r'<span class="sch-sheet__sep"> — </span><b class="sch-sheet__who">Ахметова Динара Маратовна</b>')
        self.assertEqual(len(_sheets(html)), 1)
        docs = [d for d in self.docs('schedules', groupId=GROUP) if _weekday_of(d) is not None]
        self.assertEqual(_sheets(html)[0][2], len(docs))
        self.assertEqual(sum(int(n) for n, _d in days), len(docs))

    def test_marks_only_on_weekly_pairs_and_true_legend(self):
        from cms import schedule_model as sm
        html = self.get_ok('/schedule/print?group=' + GROUP)
        docs = self.docs('schedules', groupId=GROUP)
        expected = sorted(_week_of(d) for d in docs if _week_of(d) != 'always')
        self.assertTrue(expected, 'в сиде у группы есть пары числителя и знаменателя')
        self.assertEqual(sorted(_MARK_RE.findall(html)), expected)
        # легенда — правда по schedule_model: числитель = нечётная учебная неделя
        self.assertIn('— числитель (нечётная неделя)', html)
        self.assertIn('— знаменатель (чётная неделя)', html)
        for d in (date(2026, 9, 1), date(2026, 9, 8), date(2026, 10, 20), date(2027, 2, 2)):
            with self.subTest(d=d):
                odd = sm.academic_week_number(d) % 2 == 1
                self.assertEqual(sm.current_week_type(d), 'numerator' if odd else 'denominator')
        cur = sm.current_week_type()
        self.assertIn('Текущая неделя: <b>%s</b> · %d-я учебная неделя' % (sm.WEEK_LABELS[cur].upper(),
                                                                         sm.academic_week_number(sm.today_almaty())), html)

    def test_current_week_filter_has_no_marks(self):
        from cms import schedule_model as sm
        cur = sm.current_week_type()
        html = self.get_ok('/schedule/print?week=current&group=' + GROUP)
        self.assertFalse(_MARK_RE.findall(html))
        self.assertIn('Только пары этой недели: %s' % sm.WEEK_LABELS[cur].lower(), html)
        docs = self.docs('schedules', groupId=GROUP)
        self.assertEqual(_sheets(html)[0][2], sum(1 for d in docs if _week_of(d) in ('always', cur)))

    def test_all_groups_one_sheet_each(self):
        html = self.get_ok('/schedule/print')
        sheets = _sheets(html)
        self.assertGreaterEqual(len(sheets), 11)
        self.assertEqual(len(re.findall(r'aria-label="Расписание группы ', html)), len(sheets))

    def test_missing_group_is_404(self):
        r = self.client.get('/schedule/print?group=НЕТ 99-9')
        self.assertEqual(r.status_code, 404)
        html = r.get_data(as_text=True)
        self.assertIn('Группа не найдена', html)
        self.assertFalse(_sheets(html))
        html = self.get_ok('/schedule/print?group=НЕТ 99-9&group=' + GROUP)   # одна из двух есть — бланк + подсказка
        self.assertEqual(len(_sheets(html)), 1)
        self.assertIn('Нет таких групп: НЕТ 99-9', html)

    def test_orphan_uid_not_printed(self):
        html = self.get_ok('/schedule/print?group=ВТ 25-1п')
        sheet = html[html.index('data-sch-sheets'):html.index('</main>')]
        self.assertIn('Веб-разработка', sheet)
        self.assertNotIn('uid_deleted_teacher_7', sheet)

    def test_teacher_sheet(self):
        html = self.get_ok('/schedule/print/teacher?teacher=t_li')
        self.assertIn('Расписание преподавателя', html)
        self.assertIn('<p class="sch-sheet__name">Ли Сергей Николаевич</p>', html)
        self.assertIn('Дисциплина и группа', html)
        self.assertIn('sch-sheet__day', html)
        # поток (тот же день, время, дисциплина и аудитория у двух групп) — одна строка
        self.assertIn('<b class="sch-sheet__who">ИС 23-1, ПО 23-1</b>', html)
        self.assertEqual(len(_sheets(html)), 1)

    def test_teacher_sheet_by_uid_key_or_fio_is_same(self):
        rows = []
        for t in ('t_smirnova', 'uid:t_smirnova', 'Смирнова Елена Викторовна'):
            html = self.get_ok('/schedule/print/teacher?teacher=' + t)
            self.assertIn('Смирнова Елена Викторовна', html)
            rows.append(_sheets(html)[0][2])
        self.assertEqual(len(set(rows)), 1, rows)
        self.assertGreater(rows[0], 10)

    def test_several_teachers_and_missing(self):
        html = self.get_ok('/schedule/print/teacher?teacher=t_li&teacher=t_kim')
        self.assertEqual(len(_sheets(html)), 2)
        html = self.get_ok('/schedule/print/teacher?teacher=t_li&teacher=nobody_here')
        self.assertEqual(len(_sheets(html)), 1)
        self.assertIn('Не найдены в расписании: nobody_here', html)
        r = self.client.get('/schedule/print/teacher?teacher=nobody_here')
        self.assertEqual(r.status_code, 404)
        self.assertIn('Преподаватель не найден', r.get_data(as_text=True))

    def test_all_teachers_without_params(self):
        html = self.get_ok('/schedule/print/teacher')
        self.assertGreaterEqual(len(_sheets(html)), 11)
        html = self.get_ok('/schedule/print/teacher?q=Иванова')
        self.assertEqual(len(_sheets(html)), 1)

    def test_teacher_page_links_to_sheet(self):
        html = self.get_ok('/schedule/teachers?teacher=uid:t_li')
        self.assertRegex(html, r'href="/schedule/print/teacher\?teacher=uid(:|%3A)t_li"')
        self.assertIn('Бланк для печати', html)
        self.assertIn('/schedule/print/teacher', self.get_ok('/schedule/teachers'))

    def test_print_css_contract(self):
        """Печать: лист без полей принтера (поля внутри листа), каждая группа с новой страницы, день
        вертикально, на бланке только чёрный и тёмно-серый (не светлее #333) — без бледного серого."""
        import os
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'css', 'schedule.css'),
                  encoding='utf-8') as fh:
            css = fh.read()
        self.assertRegex(css, r'@page sch-blank \{ size: A4 portrait; margin: 0; \}')
        self.assertIn('page: sch-blank', css)
        self.assertRegex(css, r'\.sch-sheet \{[^}]*break-after: page')
        self.assertIn('writing-mode: vertical-rl', css)
        section = css[css.index('Бланки для печати'):css.index('Мобильные доводки')]
        colors = set(re.findall(r'(?<![-\w])color:\s*([^;}]+)', section))
        self.assertTrue(colors)
        self.assertLessEqual(colors, {'#000', '#333', 'inherit'}, colors)


class PrintDensityTest(_ScheduleCase):
    """Много пар — плотный бланк на одном листе; мало — крупный, строки выше."""

    def _add(self, group, n):
        from cms import core
        from tests.seed import _lesson
        slots = ['08.00-08.50', '08.55-09.45', '10.00-10.50', '10.55-11.45', '12.05-12.55', '13.00-13.50']
        b = core.db.batch()
        for i in range(n):
            b.set(core.db.collection('schedules').document('print_%s_%02d' % (group[:2], i)),
                  _lesson(group, (i // 6) % 6, slots[i % 6], 't_ivanova', 'Математика', '101',
                          ('always', 'numerator', 'denominator')[i % 3] if i >= 36 else 'always'))
        b.commit()
        core.cache.invalidate()

    def test_many_pairs_dense_single_sheet(self):
        self._add('ПЛОТ 26-1', 34)
        html = self.get_ok('/schedule/print?group=ПЛОТ 26-1')
        self.assertEqual(_sheets(html), [('tight', 7.0, 34)])
        # Пн–Сб по 6 пар: дни не рвутся (свой tbody), названия полностью
        self.assertEqual(html.count('<tbody class="sch-sheet__dayrows">'), 6)
        self.assertIn('<span>Суббота</span>', html)
        self._add('ПЛОТ 26-1', 48)
        density, row, n = _sheets(self.get_ok('/schedule/print?group=ПЛОТ 26-1'))[0]
        self.assertEqual((density, n), ('ultra', 48))
        self.assertLess(row, 7.0)

    def test_few_pairs_roomy(self):
        self._add('МАЛО 26-1', 3)
        density, row, n = _sheets(self.get_ok('/schedule/print?group=МАЛО 26-1'))[0]
        self.assertEqual((density, n), ('roomy', 3))
        self.assertEqual(row, 19.0)
        self.assertNotIn('sch-sheet--fill', self.get_ok('/schedule/print?group=МАЛО 26-1'))
        self._add('МАЛО 26-1', 12)
        self.assertIn('class="sch-sheet sch-sheet--roomy sch-sheet--fill"', self.get_ok('/schedule/print?group=МАЛО 26-1'))
