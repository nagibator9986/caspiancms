# cms/substitution_model.py — модель «Замен» (владелец — SCHEDULE): чистая логика без Flask и Firestore,
# поэтому тестируется без эмуляторов (tests/test_substitution_model.py).
#
# Замена — это ОДНО занятие в ОДИН день: «<дата>, пара <schedules/id>: вместо A проведёт B» (или «пара снята», или
# «замену ещё ищем»). Документ `substitutions/<yyyy-MM-dd>_<id занятия>` — id детерминирован, поэтому одну и ту же
# пару в тот же день нельзя заменить дважды. Само расписание (`schedules`) замена НЕ меняет: оно остаётся недельным,
# замена — исключение на конкретную дату.
#
# Здесь же — «Свободные преподаватели»: кто в слоте [start, end) даты d не ведёт пару (с учётом чётности недели,
# разовых занятий, уже назначенных замен и отсутствующих в этот день), и в каком порядке их предлагать
# (ведёт дисциплину → знает группу → уже в колледже в этот день → пара рядом → меньше замен в месяце).

from collections import Counter, OrderedDict
from datetime import date, datetime, timedelta

from .conflicts import is_one_off
from .schedule_model import (DAY_NAMES_FULL, academic_year_start, current_week_type, fmt_minutes, group_key,
                             intervals_overlap, norm_name, norm_text, time_label)

OPEN, ASSIGNED, CANCELLED = 'open', 'assigned', 'cancelled'
STATUSES = ((OPEN, 'Ищем замену'), (ASSIGNED, 'Заменено'), (CANCELLED, 'Пара снята'))
STATUS_LABELS = dict(STATUSES)
STATUS_TONES = {OPEN: 'warning', ASSIGNED: 'success', CANCELLED: 'muted'}

REASONS = (
    ('sick', 'Болезнь'),
    ('trip', 'Командировка'),
    ('training', 'Курсы, повышение квалификации'),
    ('family', 'Семейные обстоятельства'),
    ('leave', 'Отпуск'),
    ('assignment', 'Поручение администрации'),
    ('other', 'Другое'),
)
REASON_LABELS = dict(REASONS)

MAX_RANGE_DAYS = 14      # сколько дней подряд можно оформить за один раз (болезнь «с понедельника по среду»)
NOTE_MAX = 300           # длина комментария

# Метки кандидата на замену (код → подпись): страница строит из них чипы; 'load' подписывается числом отдельно.
BADGE_LABELS = {'subject': 'ведёт эту дисциплину', 'group': 'знает группу', 'on_site': 'в колледже в этот день',
                'adjacent': 'пара рядом', 'day_off': 'в этот день пар нет'}

# «Пара рядом»: у кандидата в окне ±ADJACENT_MIN минут от заменяемой пары есть своя — он уже в здании.
ADJACENT_MIN = 60


# ==================== ДАТЫ ====================

def parse_iso_date(raw):
    """'2026-11-12' → date | None (только строгий yyyy-MM-dd: значение приходит из <input type=date>)."""
    s = str(raw or '').strip()
    if len(s) != 10:
        return None
    try:
        return datetime.strptime(s, '%Y-%m-%d').date()
    except ValueError:
        return None


def date_range(d_from, d_to):
    """Все даты от d_from до d_to включительно ([] если порядок перепутан или даты нет)."""
    if d_from is None or d_to is None or d_to < d_from:
        return []
    return [d_from + timedelta(days=i) for i in range((d_to - d_from).days + 1)]


def short_name(full):
    """'Ахметова Динара Маратовна' → 'Ахметова Д.М.' (для пушей и тесных ячеек); одно слово — как есть."""
    parts = str(full or '').split()
    if len(parts) < 2:
        return ' '.join(parts)
    return parts[0] + ' ' + ''.join(p[0].upper() + '.' for p in parts[1:3])


def day_label(d, today=None):
    """'Среда, 12.11.2026' (+ ' · сегодня' / ' · завтра')."""
    text = '%s, %s' % (DAY_NAMES_FULL[d.weekday()], d.strftime('%d.%m.%Y'))
    if today is not None:
        if d == today:
            text += ' · сегодня'
        elif d == today + timedelta(days=1):
            text += ' · завтра'
    return text


def period_bounds(preset, today, d_from=None, d_to=None):
    """Период фильтра списка → (from, to) (None — без границы).
    upcoming — с сегодняшнего дня; week — текущая неделя (Пн–Вс); month / prev_month — календарный месяц;
    year — учебный год (с 1 сентября); all — всё; custom — как указано (from/to)."""
    if preset == 'week':
        monday = today - timedelta(days=today.weekday())
        return monday, monday + timedelta(days=6)
    if preset == 'month':
        return _month_bounds(today.year, today.month)
    if preset == 'prev_month':
        first = today.replace(day=1)
        prev = first - timedelta(days=1)
        return _month_bounds(prev.year, prev.month)
    if preset == 'year':
        start = academic_year_start(today)
        return start, date(start.year + 1, 9, 1) - timedelta(days=1)
    if preset == 'all':
        return None, None
    if preset == 'custom':
        return d_from, d_to
    return today, None   # upcoming (по умолчанию)


def _month_bounds(year, month):
    first = date(year, month, 1)
    nxt = date(year + (month == 12), month % 12 + 1, 1)
    return first, nxt - timedelta(days=1)


# ==================== ЗАНЯТИЯ В КОНКРЕТНУЮ ДАТУ ====================

def lesson_occurs_on(lesson, d):
    """Идёт ли нормализованное занятие в дату d: тот же день недели; разовое (one_off) — только в свою дату;
    остальные — каждую неделю или в неделю своей чётности (числитель/знаменатель по дате d)."""
    wd = lesson.get('weekday')
    if wd is None or wd != d.weekday():
        return False
    if is_one_off(lesson):
        return lesson.get('date') == d.isoformat()
    return lesson.get('week') in ('always', current_week_type(d))


def _by_time(lesson):
    return (lesson['start'] if lesson.get('start') is not None else 99999, lesson.get('groupId') or '')


def teacher_lessons_on(snap, teacher_key, d):
    """Занятия преподавателя (teacher_key — 'uid:…' | 'name:…') в дату d, по времени."""
    return sorted((l for l in snap.by_teacher.get(teacher_key, []) if lesson_occurs_on(l, d)), key=_by_time)


def lesson_brief(lesson):
    """Подпись занятия для подсказок: 'ИС 24-1 · Физика · 09:20–10:10'."""
    bits = [lesson.get('groupId'), lesson.get('subject'), lesson.get('time_label') or lesson.get('time')]
    return ' · '.join(str(b) for b in bits if b)


def sub_id(d, lesson_id):
    """id документа замены: пара + дата (одну пару в один день заменить дважды нельзя)."""
    return '%s_%s' % (d.isoformat(), lesson_id)


# ==================== ЗАПИСИ О ЗАМЕНАХ ====================

def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_sub(doc_id, data):
    """Документ substitutions → dict для страниц; битая запись (нет даты) → None.

    Ключи: id, date, date_str, weekday, lesson_id, group, subject, room, week, time, start, end, time_label, minutes,
    from_key, from_uid, from_name, to_uid, to_name, status, status_label, reason, reason_label, note, batch,
    conflict (текст накладки при назначении или ''), created_by, created_at, updated_at, notified."""
    data = data or {}
    d = parse_iso_date(data.get('date'))
    if d is None:
        return None
    status = data.get('status') if data.get('status') in STATUS_LABELS else OPEN
    start, end = _int_or_none(data.get('start')), _int_or_none(data.get('end'))
    reason = data.get('reason') if data.get('reason') in REASON_LABELS else ''
    return {
        'id': doc_id, 'date': d, 'date_str': d.isoformat(), 'weekday': d.weekday(),
        'lesson_id': str(data.get('lessonId') or ''), 'group': str(data.get('groupId') or ''),
        'subject': str(data.get('subject') or ''), 'room': str(data.get('room') or ''),
        'week': str(data.get('week') or 'always'), 'time': str(data.get('time') or ''),
        'start': start, 'end': end, 'time_label': time_label(start, end) or str(data.get('time') or ''),
        'minutes': max(0, end - start) if start is not None and end is not None else 0,
        'from_key': str(data.get('fromKey') or ''), 'from_uid': str(data.get('fromUid') or ''),
        'from_name': str(data.get('fromName') or ''),
        'to_uid': str(data.get('toUid') or '') if status == ASSIGNED else '',
        'to_name': str(data.get('toName') or '') if status == ASSIGNED else '',
        'status': status, 'status_label': STATUS_LABELS[status],
        'reason': reason, 'reason_label': REASON_LABELS.get(reason, ''), 'note': str(data.get('note') or ''),
        'batch': str(data.get('batchId') or ''), 'conflict': str(data.get('conflict') or ''),
        'created_by': str(data.get('createdBy') or ''), 'created_at': data.get('createdAt'),
        'updated_at': data.get('updatedAt'), 'notified': bool(data.get('notified')),
    }


def sub_sort_key(s):
    return (s['date'], s['start'] if s['start'] is not None else 99999, s['group'], s['subject'])


def group_by_date(subs):
    """[(date, [замены по времени])] по возрастанию даты."""
    out = OrderedDict()
    for s in sorted(subs, key=sub_sort_key):
        out.setdefault(s['date'], []).append(s)
    return list(out.items())


# ==================== СВОБОДНЫЕ ПРЕПОДАВАТЕЛИ ====================

def teaching_profile(snap):
    """Что и кому ведёт преподаватель по ВСЕМУ расписанию: {uid: {'subjects': {norm}, 'groups': {group_key}}}.
    Считается один раз на запрос (для баллов «ведёт эту дисциплину / знает группу»)."""
    prof = {}
    for key, lessons in snap.by_teacher.items():
        if not key.startswith('uid:'):
            continue
        p = prof.setdefault(key[4:], {'subjects': set(), 'groups': set()})
        for l in lessons:
            if l.get('subject'):
                p['subjects'].add(norm_text(l['subject']))
            if l.get('group_key'):
                p['groups'].add(l['group_key'])
    return prof


class DayContext:
    """Занятость преподавателей на дату: свои пары (с чётностью), назначенные на эту дату замены и отсутствующие.

    day[teacher_key] — занятия даты со временем; cover[uid] — замены, которые преподаватель УЖЕ ведёт в этот день
    (он занят в эти часы); absent — ключи тех, у кого на эту дату оформлена замена (они не на работе: заменять
    других не могут, даже если в слоте у них нет пар)."""

    def __init__(self, snap, d, subs_on_date=(), also_absent=()):
        self.date = d
        self.day = {}
        for l in snap.by_day.get(d.weekday(), []):
            if l.get('teacher_key') and l.get('time_ok') and lesson_occurs_on(l, d):
                self.day.setdefault(l['teacher_key'], []).append(l)
        for ls in self.day.values():
            ls.sort(key=_by_time)
        self.cover = {}
        self.absent = set(also_absent or ())
        for s in subs_on_date:
            if s['date'] != d:
                continue
            if s['from_key']:
                self.absent.add(s['from_key'])
            if s['status'] == ASSIGNED and s['to_uid'] and s['start'] is not None and s['end'] is not None:
                self.cover.setdefault(s['to_uid'], []).append(s)

    def add_cover(self, uid, sub_like):
        """Учесть замену, назначенную в этом же запросе (две пары в одно время одному человеку не отдать)."""
        self.cover.setdefault(uid, []).append(sub_like)

    def clashes(self, uid, start, end, ignore_sub=None):
        """Чем занят преподаватель uid в [start, end): [('lesson', занятие) | ('cover', замена)]."""
        out = [('lesson', l) for l in self.day.get('uid:' + uid, []) if intervals_overlap(l['start'], l['end'], start, end)]
        for s in self.cover.get(uid, []):
            if s['id'] != ignore_sub and intervals_overlap(s['start'], s['end'], start, end):
                out.append(('cover', s))
        return out

    def window(self, uid, start, end):
        """Окно свободного времени вокруг [start, end): ('с 09:00 до 13:00' | 'весь день' | …) и пара рядом (bool)."""
        busy = [(l['start'], l['end']) for l in self.day.get('uid:' + uid, [])]
        busy += [(s['start'], s['end']) for s in self.cover.get(uid, [])]
        before = [e for _s, e in busy if e <= start]
        after = [s for s, _e in busy if s >= end]
        prev_end, next_start = (max(before) if before else None), (min(after) if after else None)
        adjacent = ((prev_end is not None and start - prev_end <= ADJACENT_MIN) or
                    (next_start is not None and next_start - end <= ADJACENT_MIN))
        if not busy:
            return 'в этот день пар нет', False
        if prev_end is None:
            return 'свободен до %s' % fmt_minutes(next_start), adjacent
        if next_start is None:
            return 'свободен с %s' % fmt_minutes(prev_end), adjacent
        return 'свободен с %s до %s' % (fmt_minutes(prev_end), fmt_minutes(next_start)), adjacent

    def has_day_lessons(self, uid):
        return bool(self.day.get('uid:' + uid) or self.cover.get(uid))


def clash_text(clashes):
    """Что мешает: 'ИС 24-1 · Физика · 09:20–10:10; замена: ПО 24-1 · Базы данных · 09:20–10:10'."""
    parts = []
    for kind, item in clashes:
        if kind == 'lesson':
            parts.append(lesson_brief(item))
        else:
            parts.append('замена: ' + ' · '.join(b for b in (item['group'], item['subject'], item['time_label']) if b))
    return '; '.join(parts)


def suggest_teachers(ctx, directory, *, start, end, subject='', group='', exclude_uids=(), profile=None, load=None,
                     ignore_sub=None):
    """Кандидаты на замену пары [start, end) даты ctx.date.

    directory — {uid: ФИО} (users role=teacher); exclude_uids — кого нельзя (заменяемый); profile — teaching_profile();
    load — {uid: сколько замен уже назначено в этом месяце}. → {'free': [...], 'busy': [...]}:
    свободные — лучшие первыми; занятые — по алфавиту. Строка: uid, name, free, absent, score, badges
    [(код, подпись)], note ('свободен с 09:00 до 13:00' | 'занят: …' | 'отсутствует в этот день'), clash, load."""
    profile = profile or {}
    load = load or {}
    subj, gkey = norm_text(subject), group_key(group)
    known = start is not None and end is not None
    free, busy = [], []
    for uid, name in directory.items():
        if uid in exclude_uids:
            continue
        row = {'uid': uid, 'name': name, 'free': True, 'absent': False, 'score': 0, 'badges': [], 'note': '',
               'clash': '', 'load': load.get(uid, 0)}
        if 'uid:' + uid in ctx.absent:
            row.update(free=False, absent=True, note='отсутствует в этот день (оформлена замена)')
            busy.append(row)
            continue
        clashes = ctx.clashes(uid, start, end, ignore_sub) if known else []
        if clashes:
            row.update(free=False, clash=clash_text(clashes), note='занят: ' + clash_text(clashes))
            busy.append(row)
            continue
        p = profile.get(uid) or {}
        window, adjacent = ctx.window(uid, start, end) if known else ('', False)
        row['note'] = window
        if subj and subj in p.get('subjects', ()):
            row['score'] += 50
            row['badges'].append(('subject', 'ведёт эту дисциплину'))
        if gkey and gkey in p.get('groups', ()):
            row['score'] += 30
            row['badges'].append(('group', 'знает группу'))
        if ctx.has_day_lessons(uid):
            row['score'] += 15
            row['badges'].append(('on_site', 'в колледже в этот день'))
            if adjacent:
                row['score'] += 5
                row['badges'].append(('adjacent', 'пара рядом'))
        else:
            row['badges'].append(('day_off', 'в этот день пар нет'))
        row['score'] -= 3 * row['load']
        if row['load']:
            row['badges'].append(('load', 'замен в месяце: %d' % row['load']))
        free.append(row)
    free.sort(key=lambda r: (-r['score'], norm_name(r['name'])))
    busy.sort(key=lambda r: (r['absent'], norm_name(r['name'])))
    return {'free': free, 'busy': busy}


def month_load(subs, d):
    """{uid: число назначенных замен в календарном месяце даты d} — для «справедливой» очерёдности."""
    out = Counter()
    for s in subs:
        if s['status'] == ASSIGNED and s['to_uid'] and (s['date'].year, s['date'].month) == (d.year, d.month):
            out[s['to_uid']] += 1
    return out


# ==================== СТАТИСТИКА ====================

def duration_text(minutes):
    """135 → '2 ч 15 мин'; 120 → '2 ч'; 45 → '45 мин'; 0 → '—'."""
    minutes = int(minutes or 0)
    if minutes <= 0:
        return '—'
    h, m = divmod(minutes, 60)
    return ' '.join(p for p in ('%d ч' % h if h else '', '%d мин' % m if m else '') if p)


def build_stats(subs):
    """Сводка по списку замен: общие числа, «кто сколько заменил», «кого заменяли», причины.

    replacers — по назначенным заменам (сколько пар и минут провёл вместо других, какие группы);
    absent — по всем (сколько его пар заменили/сняли/ещё ищут замену); reasons — по числу пар."""
    subs = list(subs)
    total = {OPEN: 0, ASSIGNED: 0, CANCELLED: 0}
    replacers, absent, reasons = {}, {}, Counter()
    for s in subs:
        total[s['status']] += 1
        reasons[s['reason'] or 'none'] += 1
        a = absent.setdefault(s['from_key'] or s['from_name'], {
            'key': s['from_key'], 'uid': s['from_uid'], 'name': s['from_name'], 'count': 0, 'assigned': 0, 'open': 0,
            'cancelled': 0, 'minutes': 0, 'days': set()})
        a['count'] += 1
        a[s['status']] += 1
        a['minutes'] += s['minutes']
        a['days'].add(s['date'])
        if s['status'] == ASSIGNED and s['to_uid']:
            r = replacers.setdefault(s['to_uid'], {'uid': s['to_uid'], 'name': s['to_name'], 'count': 0, 'minutes': 0,
                                                    'groups': set(), 'days': set()})
            r['count'] += 1
            r['minutes'] += s['minutes']
            if s['group']:
                r['groups'].add(s['group'])
            r['days'].add(s['date'])
    by_replacer = sorted(replacers.values(), key=lambda r: (-r['count'], -r['minutes'], norm_name(r['name'])))
    by_absent = sorted(absent.values(), key=lambda a: (-a['count'], norm_name(a['name'])))
    for a in by_absent:
        a['days'] = len(a['days'])
    for r in by_replacer:
        r['days'] = len(r['days'])
        r['groups'] = len(r['groups'])
    by_reason = [{'key': k, 'label': REASON_LABELS.get(k, 'Не указана'), 'count': n}
                 for k, n in sorted(reasons.items(), key=lambda kv: (kv[0] == 'none', -kv[1], kv[0]))]   # «Не указана» — в конце
    return {'count': len(subs), 'open': total[OPEN], 'assigned': total[ASSIGNED], 'cancelled': total[CANCELLED],
            'minutes': sum(s['minutes'] for s in subs if s['status'] == ASSIGNED),
            'replacers': by_replacer, 'absent': by_absent, 'reasons': by_reason}
