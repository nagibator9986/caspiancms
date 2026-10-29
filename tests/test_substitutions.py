# tests/test_substitutions.py — «Замены» (SCHEDULE): оформление, подбор свободных преподавателей, правка, список,
# статистика, CSV, листок на день, бейдж. Логика подбора без Firestore — в tests/test_substitution_model.py.

import json
import re
from datetime import timedelta

from cms import core
from cms import routes_substitutions as rs
from cms import substitution_model as sm
from tests import seed as seed_module
from tests.base import CmsTestCase

ABSENT = 't_ivanova'
# (id, группа, день недели 0..6, время, преподаватель, дисциплина, аудитория) — вечерние слоты: базовая сетка сида
# заканчивается к 11:45, поэтому тут все свободны, кроме специально занятого (t_kim).
FIXTURE = {
    'sub_a1': ('ЗАМ 26-1', 3, '16.50-17.40', ABSENT, 'Алгебра', '401'),
    'sub_a2': ('ЗАМ 26-2', 3, '17.55-18.45', ABSENT, 'Геометрия', '402'),
    'sub_a3': ('ЗАМ 26-5', 3, '15.55-16.45', ABSENT, 'Информатика', '403'),
    'sub_busy': ('ЗАМ 26-3', 3, '16.50-17.40', 't_kim', 'Физкультура', 'Спортзал'),
    'sub_alg': ('ЗАМ 26-4', 4, '16.50-17.40', 't_nurlanov', 'Алгебра', '404'),
}


def name_of(uid):
    return seed_module.TEACHERS[uid][0]


def next_thursday():
    d = core.today_almaty() + timedelta(days=1)
    while d.weekday() != 3:
        d += timedelta(days=1)
    return d


def picker_data(html):
    m = re.search(r'<script type="application/json" id="subPickerData">(.*?)</script>', html, re.S)
    assert m, 'нет данных окна выбора преподавателя'
    return json.loads(m.group(1))


class _SubCase(CmsTestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        b = core.db.batch()
        for lid, (group, wd, time, teacher, subject, room) in FIXTURE.items():
            b.set(core.db.collection('schedules').document(lid),
                  seed_module._lesson(group, wd, time, teacher, subject, room, schema='index'))
        b.commit()
        core.cache.invalidate()
        cls.thu = next_thursday()

    def setUp(self):
        super().setUp()
        for doc in core.db.collection('substitutions').stream():
            doc.reference.delete()
        core.cache.invalidate()
        core.sent_pushes.clear()

    def key(self, lid, d=None):
        return '%s|%s' % ((d or self.thu).isoformat(), lid)

    def sub_id(self, lid, d=None):
        return sm.sub_id(d or self.thu, lid)

    def create(self, decisions, reason='sick', note='', notify=False, teacher='uid:' + ABSENT, d=None, d_to='', picks=None):
        """POST шага 2: decisions {id занятия: 'uid' | 'open' | 'cancel'}."""
        d = d or self.thu
        data = {'teacher': teacher, 'from': d.isoformat(), 'to': d_to, 'reason': reason, 'note': note,
                'pick': picks if picks is not None else [self.key(lid, d) for lid in decisions]}
        for lid, v in decisions.items():
            data['to__' + self.key(lid, d)] = v
        if notify:
            data['notify'] = '1'
        return self.post('/substitutions/new', data)

    def make(self, lid, status='assigned', to='t_nurlanov', **kw):
        """Замена одной пары напрямую в базу (для тестов списка/статистики)."""
        group, wd, time, teacher, subject, room = FIXTURE[lid]
        from cms.schedule_model import parse_time_range
        start, end = parse_time_range(time)
        data = {'date': self.thu.isoformat(), 'lessonId': lid, 'groupId': group, 'subject': subject, 'room': room,
                'week': 'always', 'time': time, 'start': start, 'end': end, 'fromKey': 'uid:' + teacher,
                'fromUid': teacher, 'fromName': name_of(teacher), 'status': status, 'reason': 'sick', 'note': '',
                'batchId': 'b1', 'conflict': '', 'createdBy': 'тест'}
        if status == 'assigned':
            data.update(toUid=to, toName=name_of(to))
        data.update(kw)
        core.db.collection('substitutions').document(self.sub_id(lid)).set(data)
        core.cache.invalidate()


class PagesTest(_SubCase):

    def test_list_empty(self):
        html = self.get_ok('/substitutions')
        self.assertIn('Предстоящих замен нет', html)
        self.assertIn('Оформить замену', html)
        self.assertIn('Замен пока нет', self.get_ok('/substitutions?range=all'))

    def test_sidebar_entry(self):
        html = self.get_ok('/substitutions')
        self.assertRegex(html, r'href="/substitutions"[^>]*>[^<]*(<[^>]+>\s*)*<span[^>]*>Замены')

    def test_step1_lists_teachers(self):
        html = self.get_ok('/substitutions/new')
        self.assertIn('Показать пары', html)
        self.assertIn(name_of(ABSENT), html)
        self.assertIn('name="teacher"', html)
        self.assertIn('data-sub-date', html)

    def test_bad_teacher_shows_step1_with_warning(self):
        html = self.get_ok('/substitutions/new?teacher=uid:nobody&from=%s' % self.thu.isoformat())
        self.assertIn('Показать пары', html)
        self.assertIn('Выберите преподавателя из списка', html)

    def test_range_too_long(self):
        r = self.client.get('/substitutions/new?teacher=uid:%s&from=%s&to=%s' % (
            ABSENT, self.thu.isoformat(), (self.thu + timedelta(days=30)).isoformat()))
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Показать пары', html)
        self.assertIn('не больше 14 дней', html)

    def test_teacher_only_link_opens_step2_for_today(self):
        html = self.get_ok('/substitutions/new?teacher=uid:%s' % ABSENT)
        self.assertIn('Замена: ' + name_of(ABSENT), html)

    def test_teacher_schedule_links_to_substitution(self):
        html = self.get_ok('/schedule/teachers?teacher=uid:t_li')
        self.assertRegex(html, r'href="/substitutions/new\?teacher=uid(:|%3A)t_li"')

    def test_requires_login(self):
        self.logout()
        for url in ('/substitutions', '/substitutions/new', '/substitutions/print', '/substitutions/export'):
            self.assertRedirectsTo(self.client.get(url), '/login')

    def test_unknown_substitution_404(self):
        self.get_ok('/substitutions/nope/edit', status=404)
        self.assertEqual(self.post('/substitutions/nope/delete').status_code, 404)


class StepTwoTest(_SubCase):

    def page(self, teacher=ABSENT, **kw):
        return self.get_ok('/substitutions/new?teacher=uid:%s&from=%s' % (teacher, self.thu.isoformat()))

    def test_lessons_and_candidates(self):
        html = self.page()
        for text in ('Алгебра', 'Геометрия', 'Информатика', 'ЗАМ 26-1', '16:50–17:40'):
            self.assertIn(text, html)
        data = picker_data(html)
        c = data['lessons'][self.key('sub_a1')]
        free = [x['u'] for x in c['free']]
        busy = {x['u']: x for x in c['busy']}
        self.assertNotIn(ABSENT, free + list(busy))                        # заменяемого не предлагаем
        self.assertIn('t_kim', busy)                                       # у него в это время ЗАМ 26-3
        self.assertIn('ЗАМ 26-3', busy['t_kim']['n'])
        self.assertEqual(free[0], 't_nurlanov')                            # ведёт «Алгебру» → лучший
        self.assertIn('subject', c['free'][0]['b'])
        self.assertEqual(data['teachers']['t_nurlanov'], name_of('t_nurlanov'))
        self.assertEqual(data['badges']['subject'], 'ведёт эту дисциплину')
        # другой слот: t_kim свободен
        self.assertIn('t_kim', [x['u'] for x in data['lessons'][self.key('sub_a2')]['free']])

    def test_taken_lesson_is_marked(self):
        self.make('sub_a1')
        html = self.page()
        self.assertIn('Уже заменяет', html)
        self.assertRegex(html, r'value="%s" data-sub-pick\s+disabled' % re.escape(self.key('sub_a1')))
        self.assertNotIn(self.key('sub_a1'), picker_data(html)['lessons'])

    def test_absent_that_day_is_unavailable(self):
        self.make('sub_busy', status='open')                               # t_kim отсутствует в этот день
        data = picker_data(self.page())
        c = data['lessons'][self.key('sub_a2')]
        self.assertNotIn('t_kim', [x['u'] for x in c['free']])
        kim = next(x for x in c['busy'] if x['u'] == 't_kim')
        self.assertEqual(kim['a'], 1)

    def test_other_week_lesson_is_not_listed(self):
        other = 'denominator' if sm.current_week_type(self.thu) == 'numerator' else 'numerator'
        core.db.collection('schedules').document('sub_other').set(
            seed_module._lesson('ЗАМ 26-9', 3, '12.05-12.55', ABSENT, 'Химия', '405', other, schema='index'))
        core.cache.invalidate()
        try:
            self.assertNotIn('Химия', self.page())
        finally:
            core.db.collection('schedules').document('sub_other').delete()
            core.cache.invalidate()

    def test_empty_day(self):
        sat_free = self.thu + timedelta(days=3)                            # воскресенье — пар нет
        html = self.get_ok('/substitutions/new?teacher=uid:%s&from=%s' % (ABSENT, sat_free.isoformat()))
        self.assertIn('нет пар', html)


class CreateTest(_SubCase):

    def test_assign_cancel_open(self):
        r = self.create({'sub_a1': 't_nurlanov', 'sub_a2': 'cancel', 'sub_a3': 'open'}, reason='sick', note='Больничный лист',
                        notify=True)
        self.assertRedirectsTo(r, '/substitutions')
        a1, a2, a3 = (self.doc('substitutions', self.sub_id(x)) for x in ('sub_a1', 'sub_a2', 'sub_a3'))
        self.assertEqual((a1['status'], a1['toUid'], a1['toName']), ('assigned', 't_nurlanov', name_of('t_nurlanov')))
        self.assertEqual((a2['status'], a3['status']), ('cancelled', 'open'))
        self.assertNotIn('toUid', a2)
        self.assertEqual((a1['fromUid'], a1['fromName'], a1['reason'], a1['note']), (ABSENT, name_of(ABSENT), 'sick', 'Больничный лист'))
        self.assertEqual((a1['groupId'], a1['subject'], a1['room'], a1['start'], a1['end']), ('ЗАМ 26-1', 'Алгебра', '401', 1010, 1060))
        self.assertEqual(a1['batchId'], a2['batchId'])
        self.assertEqual(a1['conflict'], '')
        self.assertTrue(self.audit_entries('create'))
        texts = [t for c, t in self.flashes() if c == 'success']
        self.assertTrue(any('Замены оформлены' in t and 'заменено 1' in t and 'снято: 1' in t and 'ищем замену: 1' in t for t in texts), texts)
        # у шага «Замены» есть бейдж: ждут замены
        self.assertEqual(rs.nav_badges(), {'substitutions': 1})

    def test_pushes_only_for_decided_lessons(self):
        self.create({'sub_a1': 't_nurlanov', 'sub_a2': 'cancel', 'sub_a3': 'open'}, notify=True)
        topics = {p['topic']: p for p in core.sent_pushes}
        g1 = topics[core.sanitize_topic('schedule_group_ЗАМ 26-1')]
        self.assertEqual(g1['title'], rs.PUSH_TITLE_GROUP)
        self.assertIn('вместо ' + sm.short_name(name_of(ABSENT)), g1['body'])
        self.assertIn('Нурланов', g1['body'])
        self.assertEqual(g1['data'], {'type': 'schedule', 'group': 'ЗАМ 26-1'})
        self.assertIn('пара снята', topics[core.sanitize_topic('schedule_group_ЗАМ 26-2')]['body'])
        t = topics[core.sanitize_topic('schedule_teacher_t_nurlanov')]
        self.assertEqual(t['title'], rs.PUSH_TITLE_TEACHER)
        self.assertIn('ЗАМ 26-1', t['body'])
        self.assertNotIn(core.sanitize_topic('schedule_group_ЗАМ 26-5'), topics)          # «ищем замену» не рассылаем
        self.assertTrue(self.doc('substitutions', self.sub_id('sub_a1'))['notified'])
        self.assertFalse(self.doc('substitutions', self.sub_id('sub_a3'))['notified'])

    def test_no_pushes_without_flag(self):
        self.create({'sub_a1': 't_nurlanov'})
        self.assertEqual(core.sent_pushes, [])
        self.assertFalse(self.doc('substitutions', self.sub_id('sub_a1'))['notified'])

    def test_past_date_is_not_notified(self):
        past = core.today_almaty() - timedelta(days=3)
        while past.weekday() != 3:
            past -= timedelta(days=1)
        self.create({'sub_a1': 't_nurlanov'}, d=past, notify=True)
        self.assertIsNotNone(self.doc('substitutions', sm.sub_id(past, 'sub_a1')))
        self.assertEqual(core.sent_pushes, [])

    def test_duplicate_is_not_created_twice(self):
        self.create({'sub_a1': 't_nurlanov'})
        r = self.create({'sub_a1': 't_smirnova'})
        self.assertRedirectsTo(r, '/substitutions')
        self.assertEqual(self.doc('substitutions', self.sub_id('sub_a1'))['toUid'], 't_nurlanov')      # не перезаписана
        self.assertEqual(len(self.docs('substitutions')), 1)

    def test_busy_teacher_gives_conflict_warning(self):
        self.create({'sub_a1': 't_kim'})
        d = self.doc('substitutions', self.sub_id('sub_a1'))
        self.assertEqual(d['toUid'], 't_kim')
        self.assertIn('ЗАМ 26-3', d['conflict'])
        self.assertTrue(any(c == 'warning' and 'накладкой' in t for c, t in self.flashes()))
        self.assertIn('накладка', self.get_ok('/substitutions?range=all'))

    def test_same_teacher_cannot_take_two_overlapping_lessons(self):
        core.db.collection('schedules').document('sub_a4').set(
            seed_module._lesson('ЗАМ 26-6', 3, '16.50-17.40', ABSENT, 'Статистика', '406', schema='index'))
        core.cache.invalidate()
        try:
            self.create({'sub_a1': 't_nurlanov', 'sub_a4': 't_nurlanov'})
            first, second = (self.doc('substitutions', self.sub_id(x)) for x in ('sub_a1', 'sub_a4'))
            self.assertEqual(first['conflict'], '')
            self.assertIn('замена', second['conflict'])
        finally:
            core.db.collection('schedules').document('sub_a4').delete()
            core.cache.invalidate()

    def test_multi_day_range(self):
        nxt = self.thu + timedelta(days=7)
        data = {'teacher': 'uid:' + ABSENT, 'from': self.thu.isoformat(), 'to': nxt.isoformat(), 'reason': 'trip',
                'pick': [self.key('sub_a1'), self.key('sub_a1', nxt)],
                'to__' + self.key('sub_a1'): 't_nurlanov', 'to__' + self.key('sub_a1', nxt): 't_smirnova'}
        self.assertRedirectsTo(self.post('/substitutions/new', data), '/substitutions')
        self.assertEqual(self.doc('substitutions', self.sub_id('sub_a1'))['toUid'], 't_nurlanov')
        self.assertEqual(self.doc('substitutions', sm.sub_id(nxt, 'sub_a1'))['toUid'], 't_smirnova')

    def test_validation(self):
        r = self.create({}, picks=[])
        self.assertEqual(r.status_code, 200)
        self.assertIn('Отметьте хотя бы одну пару', r.get_data(as_text=True))
        r = self.create({'sub_a1': 'uid_nobody'})
        self.assertIn('Преподавателя нет в справочнике', r.get_data(as_text=True))
        r = self.create({'sub_a1': ABSENT})
        self.assertIn('Нельзя назначить заменяемого', r.get_data(as_text=True))
        r = self.create({'sub_a1': 't_nurlanov'}, reason='bogus')
        self.assertIn('Выберите причину из списка', r.get_data(as_text=True))
        r = self.create({'sub_a1': 't_nurlanov'}, picks=[self.key('sub_a1'), '2020-01-01|ghost'])      # чужие/несуществующие пары
        self.assertEqual(r.status_code, 302)
        self.assertEqual(len(self.docs('substitutions')), 1)
        self.assertEqual(self.docs('substitutions')[0]['lessonId'], 'sub_a1')

    def test_failed_validation_keeps_choices(self):
        r = self.create({'sub_a1': 't_nurlanov', 'sub_a2': 'uid_nobody'}, note='Мой комментарий')
        html = r.get_data(as_text=True)
        self.assertIn('Мой комментарий', html)
        self.assertIn('value="t_nurlanov"', html)
        self.assertEqual(self.docs('substitutions'), [])                   # ничего не записано

    def test_teacher_not_in_schedule_is_rejected(self):
        r = self.create({'sub_a1': 't_nurlanov'}, teacher='uid:ghost')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.docs('substitutions'), [])

    def test_csrf_required(self):
        r = self.client.post('/substitutions/new', data={'teacher': 'uid:' + ABSENT})
        self.assertIn(r.status_code, (400, 403))


class EditDeleteTest(_SubCase):

    def test_edit_page(self):
        self.make('sub_a1')
        html = self.get_ok('/substitutions/%s/edit' % self.sub_id('sub_a1'))
        self.assertIn('Алгебра', html)
        self.assertIn(name_of(ABSENT), html)
        data = picker_data(html)
        c = data['lessons'][self.sub_id('sub_a1')]
        self.assertIn('t_nurlanov', [x['u'] for x in c['free']])           # текущий заменяющий не «занят сам собой»
        self.assertIn('name="to" value="t_nurlanov"', html)

    def test_reassign_to_open_and_cancel(self):
        self.make('sub_a1')
        url = '/substitutions/%s/edit' % self.sub_id('sub_a1')
        self.assertRedirectsTo(self.post(url, {'to': 'open', 'reason': 'leave', 'note': 'передумали'}), '/substitutions')
        d = self.doc('substitutions', self.sub_id('sub_a1'))
        self.assertEqual((d['status'], d['reason'], d['note']), ('open', 'leave', 'передумали'))
        self.assertNotIn('toUid', d)
        self.assertNotIn('toName', d)
        self.assertTrue(self.audit_entries('update'))
        self.post(url, {'to': 'cancel'})
        self.assertEqual(self.doc('substitutions', self.sub_id('sub_a1'))['status'], 'cancelled')

    def test_reassign_notifies_everyone_affected(self):
        self.make('sub_a1')
        self.post('/substitutions/%s/edit' % self.sub_id('sub_a1'), {'to': 't_smirnova', 'notify': '1', 'reason': 'sick'})
        d = self.doc('substitutions', self.sub_id('sub_a1'))
        self.assertEqual((d['status'], d['toUid']), ('assigned', 't_smirnova'))
        titles = {p['topic']: p['title'] for p in core.sent_pushes}
        self.assertEqual(titles[core.sanitize_topic('schedule_teacher_t_smirnova')], rs.PUSH_TITLE_TEACHER)
        self.assertEqual(titles[core.sanitize_topic('schedule_teacher_t_nurlanov')], rs.PUSH_TITLE_REVOKED)
        self.assertEqual(titles[core.sanitize_topic('schedule_group_ЗАМ 26-1')], rs.PUSH_TITLE_GROUP)

    def test_edit_without_change_sends_nothing(self):
        self.make('sub_a1')
        self.post('/substitutions/%s/edit' % self.sub_id('sub_a1'), {'to': 't_nurlanov', 'notify': '1', 'note': 'просто комментарий'})
        self.assertEqual(core.sent_pushes, [])
        self.assertEqual(self.doc('substitutions', self.sub_id('sub_a1'))['note'], 'просто комментарий')

    def test_edit_validation(self):
        self.make('sub_a1')
        url = '/substitutions/%s/edit' % self.sub_id('sub_a1')
        self.assertIn('Нельзя назначить заменяемого', self.post(url, {'to': ABSENT}).get_data(as_text=True))
        self.assertIn('нет в справочнике', self.post(url, {'to': 'nobody'}).get_data(as_text=True))
        self.assertEqual(self.doc('substitutions', self.sub_id('sub_a1'))['toUid'], 't_nurlanov')

    def test_edit_to_busy_teacher_warns(self):
        self.make('sub_a1')
        self.post('/substitutions/%s/edit' % self.sub_id('sub_a1'), {'to': 't_kim'})
        self.assertIn('ЗАМ 26-3', self.doc('substitutions', self.sub_id('sub_a1'))['conflict'])
        self.assertTrue(any(c == 'warning' for c, _t in self.flashes()))

    def test_delete(self):
        self.make('sub_a1')
        r = self.post('/substitutions/%s/delete' % self.sub_id('sub_a1'))
        self.assertRedirectsTo(r, '/substitutions')
        self.assertIsNone(self.doc('substitutions', self.sub_id('sub_a1')))
        self.assertTrue(self.audit_entries('delete'))
        self.assertEqual(rs.nav_badges(), {'substitutions': 0})


class ListStatsTest(_SubCase):

    def setUp(self):
        super().setUp()
        self.make('sub_a1', to='t_nurlanov')
        self.make('sub_a2', status='cancelled')
        self.make('sub_a3', status='open', reason='')
        self.rng = 'range=custom&from=%s&to=%s' % (self.thu.isoformat(), self.thu.isoformat())

    def test_list_groups_by_day(self):
        html = self.get_ok('/substitutions?' + self.rng)
        for text in ('Алгебра', 'Геометрия', 'Информатика', 'Ищем замену', 'Пара снята', name_of('t_nurlanov'),
                     'Болезнь', 'Листок'):
            self.assertIn(text, html)
        self.assertEqual(html.count('class="sub-item sub-item--'), 3)
        self.assertLess(html.index('Информатика'), html.index('Алгебра'))     # по времени: 15:55, 16:50, 17:55
        self.assertLess(html.index('Алгебра'), html.index('Геометрия'))

    def test_filters(self):
        base = '/substitutions?' + self.rng
        self.assertEqual(self.get_ok(base + '&status=open').count('class="sub-item sub-item--'), 1)
        self.assertEqual(self.get_ok(base + '&teacher=uid:t_nurlanov').count('class="sub-item sub-item--'), 1)
        self.assertEqual(self.get_ok(base + '&teacher=uid:' + ABSENT).count('class="sub-item sub-item--'), 3)
        self.assertEqual(self.get_ok(base + '&group=ЗАМ 26-2').count('class="sub-item sub-item--'), 1)
        self.assertEqual(self.get_ok(base + '&reason=sick').count('class="sub-item sub-item--'), 2)
        self.assertIn('Ничего не найдено', self.get_ok(base + '&teacher=uid:t_li'))
        self.assertIn('Оформить замену', self.get_ok(base + '&teacher=uid:' + ABSENT))                  # карточка преподавателя

    def test_period_presets(self):
        self.assertIn('Алгебра', self.get_ok('/substitutions'))                      # предстоящие
        self.assertIn('Алгебра', self.get_ok('/substitutions?range=all'))
        self.assertIn('В этом периоде замен нет', self.get_ok('/substitutions?range=prev_month'))
        self.assertIn('Показать за всё время', self.get_ok('/substitutions?range=prev_month'))

    def test_stats_tab(self):
        html = self.get_ok('/substitutions?tab=stats&range=all')
        self.assertIn('Кто сколько заменил', html)
        self.assertIn('Кого заменяли', html)
        self.assertIn('Причины', html)
        self.assertIn(name_of('t_nurlanov'), html)
        self.assertIn('Болезнь', html)
        self.assertIn('Не указана', html)
        self.assertIn('50 мин', html)                                                # у Нурланова одна пара 16:50–17:40

    def test_stats_empty(self):
        for doc in core.db.collection('substitutions').stream():
            doc.reference.delete()
        self.assertIn('Статистики пока нет', self.get_ok('/substitutions?tab=stats&range=all'))

    def test_export_csv(self):
        r = self.client.get('/substitutions/export?range=all')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.mimetype.startswith('text/csv'))
        text = r.get_data(as_text=True)
        self.assertTrue(text.startswith('﻿'))
        lines = text.strip().split('\r\n')
        self.assertEqual(len(lines), 4)
        self.assertIn('Кто заменяет', lines[0])
        self.assertTrue(any('Алгебра' in l and name_of('t_nurlanov') in l and 'Заменено' in l and 'Болезнь' in l for l in lines[1:]), lines)
        self.assertTrue(self.audit_entries('export'))
        filtered = self.client.get('/substitutions/export?range=all&status=open').get_data(as_text=True).strip().split('\r\n')
        self.assertEqual(len(filtered), 2)

    def test_csv_formula_injection(self):
        self.make('sub_a1', note='=HYPERLINK("http://evil")')
        text = self.client.get('/substitutions/export?range=all').get_data(as_text=True)
        self.assertNotIn(';=HYPERLINK', text)

    def test_print_sheet(self):
        html = self.get_ok('/substitutions/print?date=' + self.thu.isoformat())
        self.assertIn('Замены на ' + self.thu.strftime('%d.%m.%Y'), html)
        self.assertIn('sch-sheet', html)
        self.assertIn('data-no-wrap', html)
        for text in ('Алгебра', 'вместо', 'Пара снята', 'Замена уточняется', name_of('t_nurlanov')):
            self.assertIn(text, html)
        self.assertIn('Нурланов', html)
        self.assertEqual(html.count('<tr>') - 1, 3)                                  # три строки (минус шапка)
        self.assertIn('без назначенного преподавателя', html)                        # предупреждение про «ищем»

    def test_print_empty_and_default_date(self):
        other = (self.thu + timedelta(days=1)).isoformat()
        self.assertIn('замен нет', self.get_ok('/substitutions/print?date=' + other))
        self.assertIn('Замены на ' + self.thu.strftime('%d.%m.%Y'), self.get_ok('/substitutions/print'))   # ближайший день с заменами


class SeedDemoTest(CmsTestCase):
    """Демо-данные сида (скриншоты, ручная проверка): замены читаются и попадают в список и статистику."""

    def test_demo_substitutions(self):
        ids = self.seed['substitutions']
        self.assertEqual(len(ids), len(seed_module.SUBSTITUTION_DEMO))
        html = self.get_ok('/substitutions?range=all')
        self.assertEqual(html.count('class="sub-item sub-item--'), len(ids))
        stats = self.get_ok('/substitutions?range=all&tab=stats')
        self.assertIn(name_of('t_li'), stats)
        self.assertEqual(rs.nav_badges().get('substitutions'), 2)          # две пары ждут замены (с датой от завтра)
