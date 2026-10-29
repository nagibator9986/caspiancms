# cms/routes_substitutions.py — «Замены» (владелец — SCHEDULE): кто кого заменяет в конкретный день.
#
#   /substitutions                  список замен по дням + вкладка «Статистика» (кто сколько заменил), фильтры, CSV
#   /substitutions/new              оформление: преподаватель → даты → какие пары заменить → кто заменит
#   /substitutions/<id>/edit        правка одной замены (кто заменяет, причина, комментарий)
#   /substitutions/<id>/delete      удалить замену (POST)
#   /substitutions/print            листок замен на день (A4, для доски объявлений)
#   /substitutions/export           CSV для бухгалтерии/отчёта
#
# Данные: коллекция `substitutions`, документ = ОДНА пара в ОДИН день (id = '<yyyy-MM-dd>_<id занятия>', см.
# substitution_model). `schedules` замена не меняет — расписание остаётся недельным, замена — исключение на дату.
# Статус пары: open — замену ищут, assigned — назначен преподаватель, cancelled — пара снята.
# Приложение коллекцию не читает (правила Firestore её закрывают); студентам и преподавателям уходит пуш
# `type: schedule` в топики `schedule_group_<группа>` / `schedule_teacher_<uid>` — как при правке расписания.
#
# Свободные преподаватели считает substitution_model.suggest_teachers: нет пары в этот слот (с учётом чётности
# недели и разовых занятий), не ведёт уже назначенную замену, не отсутствует в этот день; лучшие — первыми
# (ведёт дисциплину → знает группу → уже в колледже → пара рядом → меньше замен в месяце). Занятого назначить
# можно (поток, объединение групп) — сохраняется предупреждение «накладка», как в расписании.

import logging
import uuid
from datetime import timedelta

from flask import abort, flash, redirect, render_template, request, url_for

from . import substitution_model as sm
from .core import (DELETE_FIELD, SERVER_TIMESTAMP, Routes, admin_required, audit, cache, chunked, csv_response,
                   current_admin, db, flash_error, fmt_dt, fs_opts, now_almaty, plural, send_push_many, set_nav_active,
                   today_almaty, topic_variants, where)
from .routes_schedule import sheet_layout
from .schedule_model import DAY_NAMES_FULL, DAY_SHORT, WEEK_LABELS, group_sort_key, load_lessons_snapshot, norm_name

routes = Routes()
log = logging.getLogger('cms')

COLLECTION = 'substitutions'
SUBS_LIMIT = 3000        # документов за один запрос (период ограничен фильтром; «всё время» — потолок)
PUSH_TITLE_GROUP = 'Замена в расписании'
PUSH_TITLE_TEACHER = 'Вам назначена замена'
PUSH_TITLE_REVOKED = 'Замена отменена'

RANGES = (('upcoming', 'Предстоящие'), ('week', 'Эта неделя'), ('month', 'Этот месяц'),
          ('prev_month', 'Прошлый месяц'), ('year', 'Учебный год'), ('all', 'За всё время'),
          ('custom', 'Свой период'))
RANGE_KEYS = {k for k, _l in RANGES}
RANGE_LABELS = dict(RANGES)


# ==================== ДАННЫЕ ====================

def _admin_label():
    a = current_admin() or {}
    return a.get('name') or a.get('email') or ''


def load_subs(d_from=None, d_to=None):
    """Замены периода [d_from, d_to] (None — без границы) → ([normalize_sub], обрезано ли по SUBS_LIMIT).
    Одно поле `date` в условии и сортировке — составной индекс не нужен."""
    q = db.collection(COLLECTION)
    if d_from:
        q = where(q, 'date', '>=', d_from.isoformat())
    if d_to:
        q = where(q, 'date', '<=', d_to.isoformat())
    out = []
    for doc in q.order_by('date').limit(SUBS_LIMIT).stream(**fs_opts(True)):
        s = sm.normalize_sub(doc.id, doc.to_dict())
        if s:
            out.append(s)
    return out, len(out) >= SUBS_LIMIT


def _month_span(d_from, d_to):
    """Календарные месяцы, в которые попадает период (для «замен в месяце» — справедливая очерёдность)."""
    first = d_from.replace(day=1)
    nxt = d_to.replace(day=28) + timedelta(days=4)
    return first, nxt.replace(day=1) - timedelta(days=1)


def _resolve_key(raw, snap):
    """Ключ преподавателя из параметра ('uid:t1' | 't1' | 'name:…') → ключ, у которого есть занятия, или None."""
    s = str(raw or '').strip()
    if not s:
        return None
    for key in (s, 'uid:' + s):
        if key in snap.by_teacher:
            return key
    return None


def _teacher_name(snap, key):
    ls = snap.by_teacher.get(key) or []
    return (ls[0]['teacher_name'] or ls[0]['teacher']) if ls else ''


def _teacher_uid(snap, key):
    ls = snap.by_teacher.get(key) or []
    return (ls[0]['teacher_uid'] or '') if ls else ''


def _fmt(d):
    return d.strftime('%d.%m.%Y')


def _plain(value, limit):
    return ' '.join(str(value or '').replace('\x00', ' ').split())[:limit]


def _bust():
    """После записи: бейдж сайдбара пересчитать сразу."""
    cache.invalidate('nav:badges')
    cache.invalidate_prefix('substitutions:')


# ==================== КАНДИДАТЫ НА ЗАМЕНУ ====================

def _pack(res, label, known):
    """Кандидаты одной пары → компактный dict для JS (имена — в общем словаре teachers)."""
    return {
        'label': label, 'known': known,
        'free': [{'u': r['uid'], 's': r['score'], 'b': [c for c, _t in r['badges'] if c != 'load'], 'l': r['load'],
                  'n': r['note']} for r in res['free']],
        'busy': [{'u': r['uid'], 'n': r['note'], 'a': 1 if r['absent'] else 0} for r in res['busy']],
    }


def _candidates(snap, items, subs, teacher_uid, teacher_key):
    """Кандидаты для каждой пары из items [(key, date, lesson, ignore_sub_id)] → {key: _pack(...)}.
    subs — замены месяца (занятость «ведёт замену» и нагрузка), кроме них ничего не читается."""
    directory = snap.teacher_index.by_uid
    profile = sm.teaching_profile(snap)
    contexts, loads, out = {}, {}, {}
    for key, d, l, ignore in items:
        if d not in contexts:
            contexts[d] = sm.DayContext(snap, d, [s for s in subs if s['date'] == d], also_absent={teacher_key})
            loads[d] = sm.month_load(subs, d)
        known = l['start'] is not None and l['end'] is not None
        res = sm.suggest_teachers(contexts[d], directory, start=l['start'], end=l['end'], subject=l['subject'],
                                  group=l['groupId'], exclude_uids={teacher_uid} if teacher_uid else set(),
                                  profile=profile, load=loads[d], ignore_sub=ignore)
        out[key] = _pack(res, ' · '.join(b for b in (l['groupId'], l['subject'], l['time_label']) if b), known)
    return out


def _check_assignment(ctx, uid, start, end, ignore_sub=None):
    """Занят ли uid в [start, end) → текст накладки ('' если свободен)."""
    if start is None or end is None:
        return ''
    text = sm.clash_text(ctx.clashes(uid, start, end, ignore_sub))
    if 'uid:' + uid in ctx.absent:
        text = ('отсутствует в этот день; ' + text) if text else 'отсутствует в этот день'
    return text


def _decide(raw, directory, absent_uid):
    """Выбор в форме ('' | 'open' | 'cancel' | uid) → (status, uid, ошибка)."""
    raw = str(raw or '').strip()
    if raw in ('', 'open'):
        return sm.OPEN, '', ''
    if raw == 'cancel':
        return sm.CANCELLED, '', ''
    if raw not in directory:
        return None, '', 'Преподавателя нет в справочнике'
    if raw == absent_uid:
        return None, '', 'Нельзя назначить заменяемого преподавателя'
    return sm.ASSIGNED, raw, ''


# ==================== ПУШИ ====================

def _join_lines(lines, limit=3):
    text = '; '.join(lines[:limit])
    return text + (' и ещё %d' % (len(lines) - limit) if len(lines) > limit else '')


def _line(s):
    return '%s (%s) %s «%s»' % (s['date'].strftime('%d.%m'), DAY_SHORT[s['weekday']].lower(), s['time_label'], s['subject'])


def _push_messages(subs, today):
    """Пуши по назначенным и снятым парам с датой ≥ сегодня: по группе (одним сообщением) и по заменяющему."""
    by_group, by_teacher = {}, {}
    for s in sorted(subs, key=sm.sub_sort_key):
        if s['date'] < today or s['status'] == sm.OPEN:
            continue
        who = sm.short_name(s['from_name'])
        if s['status'] == sm.ASSIGNED:
            if s['group']:
                by_group.setdefault(s['group'], []).append('%s: вместо %s — %s' % (_line(s), who, sm.short_name(s['to_name'])))
            room = ', ауд. ' + s['room'] if s['room'] else ''
            by_teacher.setdefault(s['to_uid'], []).append('%s, %s%s — вместо %s' % (_line(s), s['group'], room, who))
        elif s['group']:
            by_group.setdefault(s['group'], []).append('%s: пара снята' % _line(s))
    messages = []
    for group, lines in by_group.items():
        for topic in topic_variants('schedule_group_%s' % group):
            messages.append({'title': PUSH_TITLE_GROUP, 'body': 'Группа %s: %s.' % (group, _join_lines(lines)),
                             'topic': topic, 'data': {'type': 'schedule', 'group': group}})
    for uid, lines in by_teacher.items():
        messages.append({'title': PUSH_TITLE_TEACHER, 'body': _join_lines(lines) + '.',
                         'topic': 'schedule_teacher_%s' % uid, 'data': {'type': 'schedule'}})
    return messages


def _notify(subs, extra=()):
    """Отправить пуши по subs (+ готовые extra) и пометить документы notified. → сколько пушей ушло."""
    messages = _push_messages(subs, today_almaty()) + list(extra)
    if not messages:
        return 0
    sent = send_push_many(messages)
    if sent:
        ids = [s['id'] for s in subs if s['status'] != sm.OPEN and s['date'] >= today_almaty()]
        try:
            for part in chunked(ids, 400):
                batch = db.batch()
                for sid in part:
                    batch.update(db.collection(COLLECTION).document(sid), {'notified': True})
                batch.commit()
        except Exception:
            log.warning('Не удалось пометить замены как отправленные', exc_info=True)
    return sent


# ==================== СПИСОК И СТАТИСТИКА ====================

def _list_filters():
    today = today_almaty()
    tab = 'stats' if request.args.get('tab') == 'stats' else 'list'
    d_from, d_to = sm.parse_iso_date(request.args.get('from')), sm.parse_iso_date(request.args.get('to'))
    preset = request.args.get('range') or ''
    if preset not in RANGE_KEYS:
        preset = 'custom' if (d_from or d_to) else ('month' if tab == 'stats' else 'upcoming')
    bounds = sm.period_bounds(preset, today, d_from, d_to)
    status = request.args.get('status') if request.args.get('status') in sm.STATUS_LABELS else ''
    reason = request.args.get('reason') if request.args.get('reason') in sm.REASON_LABELS else ''
    return {'tab': tab, 'range': preset, 'from': d_from, 'to': d_to, 'bounds': bounds, 'status': status,
            'reason': reason, 'group': _plain(request.args.get('group'), 60),
            'teacher': _plain(request.args.get('teacher'), 80), 'today': today}


def _apply_filters(subs, f):
    out = []
    for s in subs:
        if f['status'] and s['status'] != f['status']:
            continue
        if f['reason'] and s['reason'] != f['reason']:
            continue
        if f['group'] and s['group'] != f['group']:
            continue
        if f['teacher'] and f['teacher'] not in (s['from_key'], 'uid:' + s['to_uid'] if s['to_uid'] else ''):
            continue
        out.append(s)
    return out


def _teacher_options(subs):
    """[(ключ, ФИО)] — кто встречается в заменах периода (заменяемые и заменяющие)."""
    names = {}
    for s in subs:
        if s['from_key']:
            names[s['from_key']] = s['from_name'] or s['from_key']
        if s['to_uid']:
            names['uid:' + s['to_uid']] = s['to_name'] or s['to_uid']
    return sorted(names.items(), key=lambda kv: norm_name(kv[1]))


def _teacher_card(subs, key):
    """Сводка по выбранному преподавателю: сколько пар заменил он и сколько заменили ему."""
    mine_out = [s for s in subs if s['from_key'] == key]
    mine_in = [s for s in subs if s['status'] == sm.ASSIGNED and 'uid:' + s['to_uid'] == key]
    name = (mine_out[0]['from_name'] if mine_out else '') or (mine_in[0]['to_name'] if mine_in else '')
    return {'key': key, 'name': name or key, 'replaced_n': len(mine_in), 'replaced_min': sum(s['minutes'] for s in mine_in),
            'was_replaced_n': len(mine_out), 'was_replaced_min': sum(s['minutes'] for s in mine_out)}


@routes.route('/substitutions')
@admin_required
def substitutions():
    """Замены: ?tab=list|stats, ?range=upcoming|week|month|prev_month|year|all|custom (+from/to), ?teacher=<ключ>,
    ?group=, ?status=open|assigned|cancelled, ?reason=."""
    f = _list_filters()
    d_from, d_to = f['bounds']
    all_subs, truncated, failed = [], False, False
    try:
        all_subs, truncated = load_subs(d_from, d_to)
    except Exception as e:
        failed = True
        flash_error('Не удалось загрузить замены. Попробуйте обновить страницу.', e)
    shown = _apply_filters(all_subs, f)
    stats = sm.build_stats(shown)
    by_status = {k: sum(1 for s in all_subs if s['status'] == k) for k in sm.STATUS_LABELS}
    teacher_card = _teacher_card(shown, f['teacher']) if f['teacher'] else None
    export_args = {k: v for k, v in {
        'range': f['range'], 'from': f['from'].isoformat() if f['from'] else '', 'to': f['to'].isoformat() if f['to'] else '',
        'teacher': f['teacher'], 'group': f['group'], 'status': f['status'], 'reason': f['reason']}.items() if v}
    return render_template(
        'substitutions/list.html', f=f, tab=f['tab'], days=sm.group_by_date(shown), subs=shown, stats=stats,
        total_all=len(all_subs), by_status=by_status, truncated=truncated, failed=failed, ranges=RANGES,
        range_label=RANGE_LABELS[f['range']], teacher_options=_teacher_options(all_subs),
        group_options=sorted({s['group'] for s in all_subs if s['group']}, key=group_sort_key),
        reason_options=sm.REASONS, status_options=sm.STATUSES, teacher_card=teacher_card, duration=sm.duration_text,
        day_label=sm.day_label, export_args=export_args, week_labels=WEEK_LABELS, short=sm.short_name)


@routes.route('/substitutions/export')
@admin_required
def substitutions_export():
    """CSV замен с теми же фильтрами, что и список."""
    f = _list_filters()
    try:
        subs, _truncated = load_subs(*f['bounds'])
    except Exception as e:
        flash_error('Не удалось загрузить замены для выгрузки.', e)
        return redirect(url_for('substitutions'))
    subs = _apply_filters(subs, f)
    rows = []
    for s in sorted(subs, key=sm.sub_sort_key):
        rows.append([s['date'].strftime('%d.%m.%Y'), DAY_NAMES_FULL[s['weekday']], s['time_label'], s['group'], s['subject'],
                     s['room'], s['from_name'], s['to_name'], s['status_label'], s['reason_label'], s['note'],
                     s['minutes'] or '', s['created_by'], s['created_at']])
    audit('export', 'substitutions', None, {'count': len(rows), 'range': f['range']})
    return csv_response('замены_%s.csv' % today_almaty().isoformat(),
                        ['Дата', 'День', 'Время', 'Группа', 'Дисциплина', 'Аудитория', 'Кого заменяют', 'Кто заменяет',
                         'Статус', 'Причина', 'Комментарий', 'Минут', 'Оформил', 'Оформлено'], rows)


# ==================== ОФОРМЛЕНИЕ ====================

def _parse_range(src):
    """(from, to, ошибка) из полей from/to (to пустое — один день)."""
    d_from = sm.parse_iso_date(src.get('from') or src.get('date'))
    d_to = sm.parse_iso_date(src.get('to')) or d_from
    if d_from is None:
        return None, None, 'Укажите дату замены.'
    if d_to < d_from:
        return d_from, d_to, 'Дата «по» раньше даты «с».'
    if (d_to - d_from).days + 1 > sm.MAX_RANGE_DAYS:
        return d_from, d_to, 'За один раз можно оформить не больше %d дней.' % sm.MAX_RANGE_DAYS
    return d_from, d_to, ''


def _absence_days(snap, teacher_key, dates, subs, today):
    """Пары преподавателя по дням периода: [{'date', 'label', 'rows': [{'key', 'lesson', 'taken'}]}]."""
    existing = {s['id']: s for s in subs}
    days = []
    for d in dates:
        rows = [{'key': '%s|%s' % (d.isoformat(), l['id']), 'lesson': l, 'taken': existing.get(sm.sub_id(d, l['id']))}
                for l in sm.teacher_lessons_on(snap, teacher_key, d)]
        days.append({'date': d, 'label': sm.day_label(d, today), 'rows': rows})
    return days


def _teacher_choices(snap):
    """Кого можно «отсутствовать»: все, у кого есть занятия → [{'key', 'name', 'lessons'}] по алфавиту."""
    return [{'key': k, 'name': n, 'lessons': len(snap.by_teacher.get(k, []))} for k, n in snap.teachers()]


def _default_date(today):
    """Ближайший рабочий день: сегодня (в воскресенье — завтра)."""
    return today + timedelta(days=1) if today.weekday() == 6 else today


@routes.route('/substitutions/new', methods=['GET', 'POST'])
@admin_required
def substitution_new():
    """Оформление замен. Шаг 1 — преподаватель и даты (GET ?teacher=&from=&to=), шаг 2 — отметить пары и выбрать,
    кто заменит (то же URL; сохранение — POST)."""
    set_nav_active('substitutions')
    today = today_almaty()
    posting = request.method == 'POST'
    src = request.form if posting else request.args
    try:
        snap = load_lessons_snapshot()
    except Exception as e:
        flash_error('Не удалось загрузить расписание. Попробуйте обновить страницу.', e)
        return redirect(url_for('substitutions'))
    teacher_key = _resolve_key(src.get('teacher'), snap)
    d_from, d_to, range_error = _parse_range(src)
    if teacher_key and not posting and not (src.get('from') or src.get('date')):   # ссылка «Заменить» со страницы преподавателя
        d_from = d_to = _default_date(today)
        range_error = ''
    asked = bool((src.get('teacher') or '').strip() or (src.get('from') or src.get('date') or '').strip())
    error = ''
    if asked:
        if not teacher_key:
            error = 'Выберите преподавателя из списка — у него должны быть занятия в расписании.'
        elif range_error:
            error = range_error
    step2 = bool(teacher_key and d_from and not range_error)
    ctx = {'today': today, 'teachers': _teacher_choices(snap), 'reasons': sm.REASONS, 'error': error,
           'teacher_key': teacher_key or '', 'step2': step2, 'max_days': sm.MAX_RANGE_DAYS, 'badge_labels': sm.BADGE_LABELS,
           'from_value': (d_from or _default_date(today)).isoformat(),
           'to_value': d_to.isoformat() if d_to and d_to != d_from else '', 'week_labels': WEEK_LABELS,
           'quick_dates': {'today': today.isoformat(), 'tomorrow': (today + timedelta(days=1)).isoformat()}}
    if not step2:
        if error and not posting:
            flash(error, 'warning')
        return render_template('substitutions/form.html', **ctx, days=[], errors={}, values={})

    teacher_name, teacher_uid = _teacher_name(snap, teacher_key), _teacher_uid(snap, teacher_key)
    dates = sm.date_range(d_from, d_to)
    try:
        subs, _truncated = load_subs(*_month_span(d_from, d_to))
    except Exception as e:
        flash_error('Не удалось загрузить замены. Попробуйте обновить страницу.', e)
        return redirect(url_for('substitutions'))
    days = _absence_days(snap, teacher_key, dates, subs, today)
    rows = {r['key']: (d['date'], r) for d in days for r in d['rows']}
    free_rows = {k: v for k, v in rows.items() if not v[1]['taken']}

    errors, values = {}, {}
    if posting:
        values = {'picks': set(request.form.getlist('pick')), 'to': {k: request.form.get('to__' + k, '') for k in rows},
                  'reason': request.form.get('reason', ''), 'note': _plain(request.form.get('note'), sm.NOTE_MAX),
                  'notify': request.form.get('notify') == '1'}
        picks = [k for k in values['picks'] if k in free_rows]
        if not picks and any(k in rows for k in values['picks']):    # форму отправили повторно / страница устарела
            flash('Эти пары уже оформлены — ничего нового не добавлено.', 'warning')
            return redirect(url_for('substitutions', **{'range': 'custom', 'from': d_from.isoformat(), 'to': d_to.isoformat()}))
        if not picks:
            errors['pick'] = 'Отметьте хотя бы одну пару, которую нужно заменить.'
        if values['reason'] and values['reason'] not in sm.REASON_LABELS:
            errors['reason'] = 'Выберите причину из списка.'
        directory = snap.teacher_index.by_uid
        decisions = {}
        for k in picks:
            status, uid, err = _decide(values['to'].get(k), directory, teacher_uid)
            if err:
                errors['to__' + k] = err
            decisions[k] = (status, uid)
        if not errors:
            return _save_new(snap, teacher_key, teacher_uid, teacher_name, free_rows, picks, decisions, subs, values,
                             d_from, d_to)
    else:
        values = {'picks': {k for k in free_rows}, 'to': {}, 'reason': '', 'note': '', 'notify': True}

    items = [(k, d, r['lesson'], None) for k, (d, r) in free_rows.items()]
    cands = _candidates(snap, items, subs, teacher_uid, teacher_key)
    names = {u: n for u, n in snap.teacher_index.by_uid.items()}
    return render_template(
        'substitutions/form.html', **ctx, days=days, errors=errors, values=values, teacher_name=teacher_name,
        picker={'teachers': names, 'badges': sm.BADGE_LABELS, 'lessons': cands}, d_from=d_from, d_to=d_to,
        total_lessons=sum(len(d['rows']) for d in days), free_count=len(free_rows), duration=sm.duration_text,
        short=sm.short_name)


def _save_new(snap, teacher_key, teacher_uid, teacher_name, free_rows, picks, decisions, subs, values, d_from, d_to):
    """Создать документы замен для отмеченных пар и отправить пуши."""
    admin = current_admin() or {}
    directory = snap.teacher_index.by_uid
    batch_id = uuid.uuid4().hex[:12]
    contexts = {}
    docs, warnings = [], []
    for k in sorted(picks, key=lambda x: (free_rows[x][0], free_rows[x][1]['lesson']['start'] or 99999, x)):
        d, row = free_rows[k]
        l = row['lesson']
        status, uid = decisions[k]
        data = {'date': d.isoformat(), 'lessonId': l['id'], 'groupId': l['groupId'], 'subject': l['subject'],
                'room': l['room'], 'week': l['week'], 'time': l['time'], 'start': l['start'], 'end': l['end'],
                'fromKey': teacher_key, 'fromUid': teacher_uid, 'fromName': teacher_name, 'status': status,
                'reason': values['reason'], 'note': values['note'], 'batchId': batch_id, 'conflict': '',
                'createdAt': SERVER_TIMESTAMP, 'createdBy': _admin_label(), 'createdByUid': admin.get('uid') or '',
                'notified': False}
        if status == sm.ASSIGNED:
            if d not in contexts:
                contexts[d] = sm.DayContext(snap, d, [s for s in subs if s['date'] == d], also_absent={teacher_key})
            conflict = _check_assignment(contexts[d], uid, l['start'], l['end'])
            data.update(toUid=uid, toName=directory[uid], conflict=conflict)
            if conflict:
                warnings.append('%s %s — %s: %s' % (d.strftime('%d.%m'), l['time_label'], directory[uid], conflict))
            if l['start'] is not None and l['end'] is not None:
                contexts[d].add_cover(uid, {'id': sm.sub_id(d, l['id']), 'start': l['start'], 'end': l['end'],
                                            'group': l['groupId'], 'subject': l['subject'], 'time_label': l['time_label']})
        docs.append((sm.sub_id(d, l['id']), data))
    created = _create_docs(docs)
    skipped = len(docs) - len(created)
    _bust()
    if not created:
        flash('Эти пары уже оформлены — ничего нового не добавлено.', 'warning')
        return redirect(url_for('substitutions', **{'range': 'custom', 'from': d_from.isoformat(), 'to': d_to.isoformat()}))
    counts = {sm.OPEN: 0, sm.ASSIGNED: 0, sm.CANCELLED: 0}
    for _sid, data in created:
        counts[data['status']] += 1
    audit('create', 'substitution', batch_id, {
        'teacher': teacher_name, 'from': d_from.isoformat(), 'to': d_to.isoformat(), 'count': len(created),
        'assigned': counts[sm.ASSIGNED], 'open': counts[sm.OPEN], 'cancelled': counts[sm.CANCELLED],
        'reason': values['reason'], 'ids': [sid for sid, _d in created][:50]})
    parts = ['заменено %d' % counts[sm.ASSIGNED]] if counts[sm.ASSIGNED] else []
    if counts[sm.OPEN]:
        parts.append('ищем замену: %d' % counts[sm.OPEN])
    if counts[sm.CANCELLED]:
        parts.append('снято: %d' % counts[sm.CANCELLED])
    msg = 'Замены оформлены (%s): %s.' % (teacher_name, ', '.join(parts))
    if skipped:
        msg += ' Ещё %d уже были оформлены ранее.' % skipped
    flash(msg, 'success')
    if warnings:
        flash('Назначено с накладкой — проверьте: ' + '; '.join(warnings[:3]) + (' и ещё %d' % (len(warnings) - 3) if len(warnings) > 3 else ''),
              'warning')
    if values['notify']:
        normalized = [sm.normalize_sub(sid, data) for sid, data in created]
        sent = _notify([s for s in normalized if s])
        if sent:
            flash('Уведомления отправлены: %d %s.' % (sent, plural(sent, 'пуш', 'пуша', 'пушей')), 'info')
    return redirect(url_for('substitutions', **{'range': 'custom', 'from': d_from.isoformat(), 'to': d_to.isoformat()}))


def _create_docs(docs):
    """Создать документы (create — не перезаписывает уже оформленные). → [(id, data)] реально созданных.
    Пачкой; если пачка отвергнута (кто-то оформил ту же пару секундой раньше) — по одному, занятые пропускаем."""
    coll = db.collection(COLLECTION)
    created = []
    for part in chunked(docs, 400):
        try:
            batch = db.batch()
            for sid, data in part:
                batch.create(coll.document(sid), data)
            batch.commit()
            created.extend(part)
        except Exception as e:
            if e.__class__.__name__ not in ('AlreadyExists', 'Conflict'):
                raise
            for sid, data in part:
                try:
                    coll.document(sid).create(data)
                    created.append((sid, data))
                except Exception as inner:
                    if inner.__class__.__name__ not in ('AlreadyExists', 'Conflict'):
                        raise
    return created


# ==================== ПРАВКА / УДАЛЕНИЕ ====================

def _get_sub(sid):
    doc = db.collection(COLLECTION).document(sid).get(**fs_opts())
    s = sm.normalize_sub(doc.id, doc.to_dict()) if doc.exists else None
    if s is None:
        abort(404)
    return s


@routes.route('/substitutions/<sid>/edit', methods=['GET', 'POST'])
@admin_required
def substitution_edit(sid):
    """Одна замена: кто заменяет (или «ищем» / «пара снята»), причина, комментарий."""
    set_nav_active('substitutions')
    s = _get_sub(sid)
    snap = load_lessons_snapshot()
    directory = snap.teacher_index.by_uid
    month_subs, _t = load_subs(*_month_span(s['date'], s['date']))
    lesson = {'start': s['start'], 'end': s['end'], 'subject': s['subject'], 'groupId': s['group'],
              'time_label': s['time_label']}
    current = {'to': s['to_uid'] if s['status'] == sm.ASSIGNED else ('cancel' if s['status'] == sm.CANCELLED else 'open'),
               'reason': s['reason'], 'note': s['note'], 'notify': False}
    errors, values = {}, dict(current)
    if request.method == 'POST':
        values = {'to': request.form.get('to', ''), 'reason': request.form.get('reason', ''),
                  'note': _plain(request.form.get('note'), sm.NOTE_MAX), 'notify': request.form.get('notify') == '1'}
        status, uid, err = _decide(values['to'], directory, s['from_uid'])
        if err:
            errors['to'] = err
        if values['reason'] and values['reason'] not in sm.REASON_LABELS:
            errors['reason'] = 'Выберите причину из списка.'
        if not errors:
            return _save_edit(s, snap, month_subs, status, uid, values, directory)
    cands = _candidates(snap, [(s['id'], s['date'], lesson, s['id'])], month_subs, s['from_uid'], s['from_key'])
    return render_template(
        'substitutions/edit.html', s=s, values=values, errors=errors, reasons=sm.REASONS, today=today_almaty(),
        picker={'teachers': dict(directory), 'badges': sm.BADGE_LABELS, 'lessons': cands},
        day_label=sm.day_label, week_labels=WEEK_LABELS, duration=sm.duration_text, short=sm.short_name)


def _save_edit(s, snap, month_subs, status, uid, values, directory):
    update = {'status': status, 'reason': values['reason'], 'note': values['note'], 'conflict': '',
              'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin_label()}
    warning = ''
    if status == sm.ASSIGNED:
        ctx = sm.DayContext(snap, s['date'], [x for x in month_subs if x['date'] == s['date']], also_absent={s['from_key']})
        warning = _check_assignment(ctx, uid, s['start'], s['end'], ignore_sub=s['id'])
        update.update(toUid=uid, toName=directory[uid], conflict=warning)
    else:
        update.update(toUid=DELETE_FIELD, toName=DELETE_FIELD)
    changed_assignment = (status, uid) != (s['status'], s['to_uid'])
    if changed_assignment:
        update['notified'] = False
    db.collection(COLLECTION).document(s['id']).update(update)
    _bust()
    audit('update', 'substitution', s['id'], {
        'status': status, 'to': directory.get(uid, '') if uid else '', 'was': s['status'], 'was_to': s['to_name'],
        'reason': values['reason']})
    flash('Замена сохранена.', 'success')
    if warning:
        flash('Назначено с накладкой: %s.' % warning, 'warning')
    if values['notify'] and changed_assignment:
        new = sm.normalize_sub(s['id'], dict(_raw(s), **{k: v for k, v in update.items() if v is not DELETE_FIELD
                                                         and k not in ('updatedAt',)}))
        extra = []
        if s['status'] == sm.ASSIGNED and s['to_uid'] and s['to_uid'] != uid and s['date'] >= today_almaty():
            extra.append({'title': PUSH_TITLE_REVOKED, 'body': '%s: замена вместо %s отменена.' % (_line(s), sm.short_name(s['from_name'])),
                          'topic': 'schedule_teacher_%s' % s['to_uid'], 'data': {'type': 'schedule'}})
        sent = _notify([new] if new and status != sm.OPEN else [], extra)
        if sent:
            flash('Уведомления отправлены: %d %s.' % (sent, plural(sent, 'пуш', 'пуша', 'пушей')), 'info')
    return redirect(url_for('substitutions', **{'range': 'custom', 'from': s['date_str'], 'to': s['date_str']}))


def _raw(s):
    """Поля документа из normalize_sub обратно в «сырой» вид (для повторной нормализации после правки)."""
    return {'date': s['date_str'], 'lessonId': s['lesson_id'], 'groupId': s['group'], 'subject': s['subject'],
            'room': s['room'], 'week': s['week'], 'time': s['time'], 'start': s['start'], 'end': s['end'],
            'fromKey': s['from_key'], 'fromUid': s['from_uid'], 'fromName': s['from_name'], 'batchId': s['batch']}


@routes.route('/substitutions/<sid>/delete', methods=['POST'])
@admin_required
def substitution_delete(sid):
    s = _get_sub(sid)
    db.collection(COLLECTION).document(sid).delete()
    _bust()
    audit('delete', 'substitution', sid, {'date': s['date_str'], 'group': s['group'], 'subject': s['subject'],
                                          'from': s['from_name'], 'to': s['to_name'], 'status': s['status']})
    flash('Замена удалена: %s, %s, %s.' % (s['date'].strftime('%d.%m'), s['time_label'], s['group']), 'success')
    return redirect(url_for('substitutions', **{'range': 'custom', 'from': s['date_str'], 'to': s['date_str']}))


# ==================== ЛИСТОК ЗАМЕН НА ДЕНЬ ====================

@routes.route('/substitutions/print')
@admin_required
def substitutions_print():
    """Листок замен на день (A4): ?date=yyyy-MM-dd; по умолчанию — ближайший день с заменами (сегодня и позже)."""
    set_nav_active('substitutions')
    today = today_almaty()
    d = sm.parse_iso_date(request.args.get('date'))
    subs = []
    try:
        if d is None:
            upcoming, _t = load_subs(today, today + timedelta(days=30))
            d = min((s['date'] for s in upcoming), default=today)
        subs, _t = load_subs(d, d)
    except Exception as e:
        d = d or today
        flash_error('Не удалось загрузить замены. Попробуйте обновить страницу.', e)
    subs.sort(key=sm.sub_sort_key)
    layout = sheet_layout({0: subs}, 'group')
    return render_template('substitutions/print.html', d=d, subs=subs, layout=layout, day_name=DAY_NAMES_FULL[d.weekday()],
                           generated=_stamp(), short=sm.short_name, counts={k: sum(1 for s in subs if s['status'] == k)
                                                                           for k in sm.STATUS_LABELS},
                           prev_day=(d - timedelta(days=1)).isoformat(), next_day=(d + timedelta(days=1)).isoformat(),
                           today=today)


def _stamp():
    return fmt_dt(now_almaty())


# ==================== БЕЙДЖ САЙДБАРА ====================

def nav_badges():
    """«Замены»: сколько пар с датой сегодня и позже ждут, пока им найдут замену."""
    try:
        subs, _t = load_subs(today_almaty(), None)
    except Exception:
        return {}
    return {'substitutions': sum(1 for s in subs if s['status'] == sm.OPEN)}
