# cms/conflicts.py — движок накладок расписания (владелец — CONFLICTS).
#
# Чистая часть (без Firebase, покрыта tests/test_conflicts.py):
#   analyze(lessons, known_groups, known_subjects) — нормализованные занятия
#       (schedule_model.normalize_lesson) → Analysis: кластеры накладок по преподавателям,
#       аудиториям и группам + «Качество данных»;
#   pair_weeks / classify_pair / quality_issues / find_lesson_conflicts — правила.
# Обёртка с кэшем (снапшот расписания + справочники + принятые накладки):
#   get_analysis(), summary(), check_lesson(), load_acks(), accept()/unaccept().
#
# Алгоритм: занятия → корзины (сущность, день недели) → в каждой корзине sweep-line по
# интервалам (строгое пересечение: касание 10:55–11:45 / 11:45–12:05 — не накладка) с проверкой
# совместимости недель → пары → кластеры (union-find: занятия, пересекающиеся транзитивно) →
# классификация пар и критичность кластера (максимум по парам).
#
# Недели. «Каждую неделю» совместима с любой; числитель × знаменатель — нет. День недели — как в
# приложении (lesson_weekday: day_index приоритетнее date). Занятие только с `date` приложение
# показывает КАЖДУЮ неделю (date — носитель дня недели; так писал старый веб) — здесь так же.
# Настоящее разовое занятие помечается полем one_off: true (или oneOff) + date: тогда оно идёт
# только в эту дату, его неделя — чётность этой даты; разовое × разовое — только при равных датах.
#
# Идентичность: преподаватель — teacher_key (uid; ФИО текстом сопоставляется со справочником),
# аудитория — room_key («ауд.»/«каб.», регистр, пробелы, латиница-двойник; «онлайн»/«-» — не
# аудитория), группа — group_key.

import hashlib
import logging
import time
from collections import Counter, OrderedDict
from datetime import timedelta

from .schedule_model import (DAY_NAMES_FULL, DAY_SHORT, SCHOOL_DAY_END, SCHOOL_DAY_START, current_week_type,
                             group_key, norm_name, norm_text, parse_lesson_date, room_sort_key, time_label)

log = logging.getLogger('cms')

DIMS = ('teacher', 'room', 'group')
DIM_LABELS = OrderedDict([('teacher', 'Преподаватели'), ('room', 'Аудитории'), ('group', 'Группы'),
                          ('quality', 'Качество данных')])
SEVERITIES = ('critical', 'warning', 'info')
SEV_RANK = {'critical': 0, 'warning': 1, 'info': 2}
SEV_LABELS = {'critical': 'Критично', 'warning': 'Внимание', 'info': 'Инфо'}

# Вид накладки → (критичность по умолчанию, заголовок, пояснение)
KINDS = OrderedDict([
    ('teacher_double', ('critical', 'Преподаватель ведёт две пары одновременно',
                        'Один преподаватель в одно время у разных групп (не поток).')),
    ('room_double', ('critical', 'Аудитория занята двумя парами',
                     'В одной аудитории в одно время разные преподаватели.')),
    ('group_overlap', ('critical', 'У группы две пары одновременно',
                       'Группа в одно время на разных занятиях.')),
    ('subgroups', ('warning', 'Возможно, подгруппы',
                   'У группы в одно и то же время пары у разных преподавателей — проверьте деление на подгруппы.')),
    ('duplicate', ('warning', 'Дубль занятия',
                   'Одинаковое занятие записано дважды (группа, преподаватель, дисциплина, время).')),
    ('stream', ('info', 'Поток — объединённая пара',
                'Один преподаватель, одна аудитория и дисциплина у нескольких групп. Обычно это норма.')),
])

# Код «Качества данных» → (критичность, заголовок, подсказка)
QUALITY = OrderedDict([
    ('no_day', ('critical', 'Нет дня недели', 'Нет ни day_index, ни date — занятие не видно в приложении.')),
    ('bad_time', ('critical', 'Время не разобрать', 'Ожидается формат ЧЧ.ММ-ЧЧ.ММ, например 08.30-09.50.')),
    ('reverse_time', ('critical', 'Конец раньше начала', 'Время окончания не позже начала.')),
    ('no_group', ('critical', 'Нет группы', 'Поле groupId пустое — занятие никому не показывается.')),
    ('date_timestamp', ('warning', 'Дата записана как Timestamp',
                        'Приложение ждёт строку yyyy-MM-dd; без day_index день не распознается.')),
    ('day_mismatch', ('warning', 'day_index и date расходятся',
                      'Приложение берёт day_index — веб и приложение могли показывать разные дни.')),
    ('teacher_by_name', ('warning', 'ФИО текстом вместо uid',
                         'Преподаватель найден по ФИО, но в его личном расписании в приложении занятия не видно.')),
    ('teacher_ambiguous', ('warning', 'ФИО у нескольких преподавателей',
                           'Однофамильцы: выберите преподавателя в форме занятия.')),
    ('teacher_orphan', ('warning', 'Преподаватель не найден', 'uid преподавателя нет в справочнике (удалён?).')),
    ('teacher_unknown', ('warning', 'ФИО нет в справочнике',
                         'Преподаватель записан текстом и не найден среди преподавателей.')),
    ('teacher_legacy', ('warning', 'Старый справочник преподавателей',
                        'Преподаватель из коллекции teachers — приложение его не покажет.')),
    ('no_teacher', ('warning', 'Нет преподавателя', 'Поле teacher пустое.')),
    ('no_subject', ('warning', 'Нет дисциплины', 'Поле subject пустое.')),
    ('week_unknown', ('critical', 'Тип недели не по формату приложения',
                      'Приложение понимает только always / numerator / denominator (или пусто) — с таким '
                      'week_type занятие не видно при фильтре недели. В накладках CMS считает его как указано.')),
    ('unknown_group', ('warning', 'Группы нет в справочнике', 'Группы нет в списке групп (groups_list).')),
    ('off_hours', ('warning', 'Вне учебного дня', 'Занятие раньше 07:00 или позже 21:00.')),
    ('no_room', ('info', 'Нет аудитории', 'Аудитория не указана — накладки аудиторий не проверяются.')),
    ('end_assumed', ('info', 'Нет времени окончания', 'Конец пары взят из сетки звонков (или +50 мин).')),
    ('off_grid', ('info', 'Время не по сетке звонков', 'Начало и конец не совпадают со слотом сетки этого дня.')),
    ('sunday', ('info', 'Занятие в воскресенье', 'Проверьте, что день указан верно.')),
    ('unknown_subject', ('info', 'Дисциплины нет в справочнике', 'Названия нет в списке дисциплин.')),
])

# Коды, которые CMS умеет исправить массово (кнопка «Исправить» на вкладке «Качество данных»).
FIXABLE = OrderedDict([
    ('teacher_by_name', 'Заменить ФИО на uid преподавателя'),
    ('day_mismatch', 'Переписать date по day_index (как показывает приложение)'),
    ('date_timestamp', 'Записать день как day_index + строку date'),
    ('week_unknown', 'Записать тип недели ключом приложения (always / numerator / denominator)'),
])


# ==================== ВСПОМОГАТЕЛЬНОЕ ====================

def short_name(name):
    """'Петров Дмитрий Игоревич' → 'Петров Д. И.' (для коротких сообщений)."""
    parts = str(name or '').split()
    if len(parts) < 2:
        return str(name or '').strip()
    return parts[0] + ' ' + ' '.join(p[0].upper() + '.' for p in parts[1:3] if p)


def _truthy(v):
    if isinstance(v, str):
        return v.strip().lower() in ('1', 'true', 'yes', 'да')
    return bool(v)


def is_one_off(lesson):
    """Разовое занятие: явный флаг one_off/oneOff и разобранная дата (см. шапку модуля)."""
    raw = lesson.get('raw') or {}
    return _truthy(raw.get('one_off') or raw.get('oneOff')) and lesson.get('date') is not None


def teacher_display(l):
    return l.get('teacher_name') or l.get('teacher') or ''


def lesson_label(l):
    """Короткая подпись занятия: 'ВТ 24-1 · Физика · 13:00–13:50'."""
    bits = [l.get('groupId'), l.get('subject'), l.get('time_label') or l.get('time')]
    return ' · '.join(str(b) for b in bits if b)


class _Item:
    """Занятие для сравнения: ссылка на нормализованный dict + поля, вычисленные один раз
    (снапшот общий для всех страниц — его dict'ы не меняем)."""

    __slots__ = ('l', 'id', 'wd', 'start', 'end', 'week', 'once', 'once_week', 'subj', 'tkey', 'rkey', 'gkey',
                 'uncertain', 'ambiguous')

    def __init__(self, l):
        self.l = l
        self.id = l['id']
        self.wd = l.get('weekday')
        self.start = l.get('start')
        self.end = l.get('end')
        self.week = l.get('week') or 'always'
        once = is_one_off(l)
        self.once = l.get('date') if once else None
        self.once_week = current_week_type(parse_lesson_date(self.once)) if once else None
        self.subj = norm_text(l.get('subject'))
        self.tkey = l.get('teacher_key')
        self.rkey = l.get('room_key')
        self.gkey = l.get('group_key')
        # «Неточные» данные: критичная накладка с такими занятиями понижается до «Внимание»
        self.uncertain = bool(l.get('end_assumed') or l.get('week_unknown'))
        self.ambiguous = 'ambiguous' in (l.get('teacher_flags') or ())

    @property
    def usable(self):
        """Участвует в поиске накладок: есть день и корректный интервал."""
        return (self.wd is not None and self.start is not None and self.end is not None
                and self.end > self.start)

    def key(self, dim):
        return self.tkey if dim == 'teacher' else (self.rkey if dim == 'room' else self.gkey)


def _item(x):
    return x if isinstance(x, _Item) else _Item(x)


def pair_weeks(a, b):
    """Когда два занятия одного дня идут одновременно: 'every' | 'numerator' | 'denominator' |
    'date:YYYY-MM-DD' (разовое) | None (никогда: числитель × знаменатель, разные даты)."""
    a, b = _item(a), _item(b)
    if a.once and b.once:
        return 'date:' + a.once if a.once == b.once else None
    if a.once or b.once:
        o, w = (a, b) if a.once else (b, a)
        return 'date:' + o.once if w.week in ('always', o.once_week) else None
    if a.week == 'always':
        return 'every' if b.week == 'always' else b.week
    if b.week == 'always' or a.week == b.week:
        return a.week
    return None


def classify_pair(a, b):
    """Вид накладки пары (одинаков для всех вкладок) → (kind, flags).

    Одна группа: тот же преподаватель + дисциплина + время → 'duplicate'; другой преподаватель при
    том же времени → 'subgroups'; иначе 'group_overlap'. Разные группы и тот же преподаватель →
    'stream' (та же аудитория, дисциплина и время) или 'teacher_double'; иначе (общая аудитория,
    разные преподаватели) → 'room_double'. flags: partial (время пересекается частично),
    alternating (разные типы недели), different_rooms."""
    a, b = _item(a), _item(b)
    flags = []
    same_time = a.start == b.start and a.end == b.end
    if not same_time:
        flags.append('partial')
    if a.week != b.week or a.once or b.once:
        flags.append('alternating')
    if a.gkey and a.gkey == b.gkey:
        if a.tkey and a.tkey == b.tkey and a.subj == b.subj and same_time:
            if a.rkey != b.rkey:
                flags.append('different_rooms')
            return 'duplicate', flags
        if same_time and a.tkey != b.tkey:
            return 'subgroups', flags
        return 'group_overlap', flags
    if a.tkey and a.tkey == b.tkey:
        if a.rkey and a.rkey == b.rkey and a.subj == b.subj and same_time:
            return 'stream', flags
        if a.rkey != b.rkey:
            flags.append('different_rooms')
        return 'teacher_double', flags
    return 'room_double', flags


def pair_severity(kind, a, b, dim=None):
    """(критичность, понижена?) пары. Критичная понижается до «Внимание», если данные неточны:
    конец времени угадан, неизвестный тип недели, однофамильцы (на вкладке преподавателей)."""
    a, b = _item(a), _item(b)
    sev = KINDS[kind][0]
    if sev == 'critical':
        if a.uncertain or b.uncertain:
            return 'warning', True
        if dim in (None, 'teacher') and (a.ambiguous or b.ambiguous) and a.tkey == b.tkey:
            return 'warning', True
    return sev, False


def weeks_label(weeks):
    """Множество недель пар кластера → подпись для людей."""
    weeks = set(weeks or ())
    dates = sorted(w[5:] for w in weeks if w.startswith('date:'))
    parts = []
    if 'every' in weeks:
        parts.append('Каждую неделю')
    elif 'numerator' in weeks and 'denominator' in weeks:
        parts.append('Числитель и знаменатель')
    elif 'numerator' in weeks:
        parts.append('Только числитель')
    elif 'denominator' in weeks:
        parts.append('Только знаменатель')
    if dates:
        parts.append('Разово: ' + ', '.join('%s.%s' % (d[8:10], d[5:7]) for d in dates))
    return '; '.join(parts) or '—'


def week_bounds(d):
    """Понедельник и воскресенье недели даты d."""
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def occurs_in(weeks, week_type=None, week_of=None):
    """Идёт ли накладка (недели её пар) в неделю типа week_type ('numerator'|'denominator').
    week_of — дата: тип недели берётся из неё, а разовые пары учитываются, только если их дата
    попадает в эту неделю."""
    monday = sunday = None
    if week_of is not None:
        week_type = current_week_type(week_of)
        monday, sunday = week_bounds(week_of)
    for w in weeks or ():
        if w == 'every' or w == week_type:
            return True
        if w.startswith('date:'):
            d = parse_lesson_date(w[5:])
            if d is None:
                continue
            if monday is not None:
                if monday <= d <= sunday:
                    return True
            elif current_week_type(d) == week_type:
                return True
    return False


def lesson_view(l, flags=()):
    """Нормализованное занятие → компактный dict для шаблонов, JSON и экспорта (без raw)."""
    name = teacher_display(l)
    return {
        'id': l['id'], 'groupId': l.get('groupId') or '', 'subject': l.get('subject') or '',
        'teacher': l.get('teacher') or '', 'teacher_key': l.get('teacher_key'),
        'teacher_name': name, 'teacher_short': short_name(name),
        'teacher_flags': list(l.get('teacher_flags') or ()),
        'room': l.get('room') or '', 'room_key': l.get('room_key'),
        'time': l.get('time') or '', 'time_label': l.get('time_label') or l.get('time') or '',
        'start': l.get('start'), 'end': l.get('end'), 'end_assumed': bool(l.get('end_assumed')),
        'week': l.get('week') or 'always', 'week_raw': None if l.get('week_raw') is None else str(l.get('week_raw')),
        'weekday': l.get('weekday'),
        'day_name': l.get('day_name') or '—', 'kind': l.get('kind'), 'date': l.get('date'),
        'one_off': is_one_off(l), 'flags': list(flags),
    }


def _lesson_sig(it):
    return '|'.join(str(x) for x in (it.id, it.wd, it.start, it.end, it.week, it.once or '', it.tkey or '',
                                       it.rkey or '', it.gkey or '', it.subj))


def cluster_hash(items):
    """Хэш набора занятий для «Принять/игнорировать»: id + день, время, неделя, преподаватель,
    аудитория, группа, дисциплина. Изменилось любое занятие — хэш другой, накладка снова видна.
    Вкладка в хэш не входит: принятый поток принят и на вкладке «Аудитории»."""
    raw = '#'.join(sorted(_lesson_sig(_item(it)) for it in items))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]


def quality_hash(code, l):
    """Хэш проблемы данных: код + id + поля занятия (исправили/изменили — отметка не действует)."""
    raw = '|'.join(str(x) for x in ('q', code, l['id'], l.get('weekday'), l.get('time'), l.get('teacher'),
                                     l.get('room'), l.get('groupId'), l.get('week_raw'), l.get('subject')))
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:24]


# ==================== ПОИСК ПАР И КЛАСТЕРОВ ====================

def _sweep(items):
    """Пары (a, b, weeks) с пересекающимися интервалами и совместимыми неделями. O(n log n + k)."""
    items = sorted(items, key=lambda it: (it.start, it.end, it.id))
    active = []
    for cur in items:
        active = [a for a in active if a.end > cur.start]
        for a in active:
            weeks = pair_weeks(a, cur)
            if weeks:
                yield a, cur, weeks
        active.append(cur)


class _UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        root = self.parent.setdefault(x, x)
        while root != self.parent[root]:
            root = self.parent[root]
        while x != root:  # сжатие пути
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _entity_display(dim, members):
    """Название сущности кластера: самое частое написание, при равенстве — самое короткое
    («204», а не «Каб. 204»)."""
    if dim == 'teacher':
        vals = Counter(teacher_display(it.l) for it in members if teacher_display(it.l))
    else:
        field = 'room' if dim == 'room' else 'groupId'
        vals = Counter(it.l.get(field) for it in members if it.l.get(field))
    return max(vals.items(), key=lambda kv: (kv[1], -len(kv[0]), kv[0]))[0] if vals else '—'


def _build_cluster(dim, key, wd, members, pairs):
    members = sorted(members, key=lambda it: (it.start, it.end, it.l.get('groupId') or '', it.id))
    sev = min((p['severity'] for p in pairs), key=lambda s: SEV_RANK[s])
    kind_order = list(KINDS)
    kinds = sorted({p['kind'] for p in pairs}, key=lambda k: (SEV_RANK[KINDS[k][0]], kind_order.index(k)))
    weeks = sorted({p['weeks'] for p in pairs})
    lesson_flags = {}
    for p in pairs:
        for f in p['flags']:
            lesson_flags.setdefault(p['a'], set()).add(f)
            lesson_flags.setdefault(p['b'], set()).add(f)
    start = min(it.start for it in members)
    end = max(it.end for it in members)
    groups = sorted({it.l.get('groupId') for it in members if it.l.get('groupId')})
    rooms = sorted({it.l.get('room') for it in members if it.l.get('room')}, key=room_sort_key)
    entity = _entity_display(dim, members)
    search = norm_text(' '.join([entity] + groups + rooms + [it.l.get('subject') or '' for it in members]
                                + [teacher_display(it.l) for it in members]))
    return {
        'hash': cluster_hash(members), 'dim': dim, 'key': key, 'entity': entity,
        'weekday': wd, 'day_name': DAY_NAMES_FULL[wd], 'day_short': DAY_SHORT[wd],
        'severity': sev, 'kinds': kinds, 'kind': kinds[0], 'title': KINDS[kinds[0]][1],
        'weeks': weeks, 'weeks_label': weeks_label(weeks),
        'start': start, 'end': end, 'time_label': time_label(start, end),
        'overlap_min': max(p['overlap_min'] for p in pairs),
        'lessons': [lesson_view(it.l) for it in members], 'ids': [it.id for it in members],
        'pairs': pairs, 'uncertain': any(p['uncertain'] for p in pairs),
        'teacher_keys': sorted({it.tkey for it in members if it.tkey}),
        'room_keys': sorted({it.rkey for it in members if it.rkey}),
        'groups': groups, 'rooms': rooms, 'search': search,
        'lesson_flags': {k: sorted(v) for k, v in lesson_flags.items()},
    }


def _pair_side(it):
    """Подпись занятия в паре: группа и время ('ВТ 24-1 13:00–13:50')."""
    return ('%s %s' % (it.l.get('groupId') or '—', it.l.get('time_label') or it.l.get('time') or '')).strip()


def find_clusters(items, dim):
    """Кластеры накладок одного измерения ('teacher'|'room'|'group'). items — занятия
    (нормализованные dict'ы или _Item) с корректными днём и временем."""
    buckets = {}
    for it in items:
        it = _item(it)
        k = it.key(dim)
        if k and it.usable:
            buckets.setdefault((k, it.wd), []).append(it)
    clusters = []
    for (key, wd), bucket in buckets.items():
        if len(bucket) < 2:
            continue
        uf = _UnionFind()
        pairs = []
        by_id = {}
        for a, b, weeks in _sweep(bucket):
            kind, flags = classify_pair(a, b)
            sev, uncertain = pair_severity(kind, a, b, dim)
            uf.union(a.id, b.id)
            by_id[a.id], by_id[b.id] = a, b
            pairs.append({'a': a.id, 'b': b.id, 'kind': kind, 'severity': sev, 'title': KINDS[kind][1],
                          'label': '%s × %s' % (_pair_side(a), _pair_side(b)),
                          'weeks': weeks, 'weeks_label': weeks_label([weeks]), 'flags': flags,
                          'uncertain': uncertain, 'overlap_min': min(a.end, b.end) - max(a.start, b.start)})
        components = OrderedDict()
        for p in pairs:
            components.setdefault(uf.find(p['a']), []).append(p)
        for ps in components.values():
            ids = {p['a'] for p in ps} | {p['b'] for p in ps}
            clusters.append(_build_cluster(dim, key, wd, [by_id[i] for i in ids], ps))
    clusters.sort(key=cluster_sort_key)
    return clusters


def cluster_sort_key(c):
    return (SEV_RANK[c['severity']], norm_name(c['entity']), c['weekday'], c['start'])


# ==================== КАЧЕСТВО ДАННЫХ ====================

def quality_issues(l, known_groups=None, known_subjects=None):
    """Коды проблем одного занятия (см. QUALITY). known_groups — множество group_key справочника
    групп (None — не проверять), known_subjects — множество norm_text дисциплин."""
    codes = []
    raw = l.get('raw') or {}
    if not l.get('groupId'):
        codes.append('no_group')
    elif known_groups and l.get('group_key') not in known_groups:
        codes.append('unknown_group')
    if l.get('weekday') is None:
        codes.append('no_day')
    raw_date = raw.get('date')
    if raw_date is not None and not isinstance(raw_date, str) and hasattr(raw_date, 'year'):
        codes.append('date_timestamp')
    if l.get('day_mismatch'):
        codes.append('day_mismatch')
    if l.get('weekday') == 6:
        codes.append('sunday')
    start, end = l.get('start'), l.get('end')
    if start is None:
        codes.append('bad_time')
    elif end is not None and end <= start:
        codes.append('reverse_time')
    else:
        if l.get('end_assumed'):
            codes.append('end_assumed')
        if start < SCHOOL_DAY_START or (end or start) > SCHOOL_DAY_END:
            codes.append('off_hours')
        elif l.get('weekday') is not None and not l.get('in_grid') and not l.get('end_assumed'):
            codes.append('off_grid')
    flags = set(l.get('teacher_flags') or ())
    for flag, code in (('no_teacher', 'no_teacher'), ('ambiguous', 'teacher_ambiguous'),
                       ('by_name', 'teacher_by_name'), ('orphan', 'teacher_orphan'),
                       ('legacy', 'teacher_legacy'), ('unknown_name', 'teacher_unknown')):
        if flag in flags:
            codes.append(code)
            break
    if not l.get('room'):
        codes.append('no_room')
    if l.get('week_unknown'):
        codes.append('week_unknown')
    subject = l.get('subject') or ''
    if not subject:
        codes.append('no_subject')
    elif known_subjects and norm_text(subject) not in known_subjects:
        codes.append('unknown_subject')
    return codes


def _quality_detail(code, l):
    """Пояснение к проблеме конкретного занятия (значение из БД, что предлагается)."""
    raw = l.get('raw') or {}
    if code == 'teacher_by_name':
        return 'Записано «%s» → %s' % (l.get('teacher'), l.get('teacher_name'))
    if code in ('teacher_orphan', 'teacher_unknown', 'teacher_ambiguous', 'teacher_legacy'):
        return 'Записано: %s' % (l.get('teacher') or '—')
    if code in ('bad_time', 'reverse_time', 'off_grid', 'off_hours', 'end_assumed'):
        return 'Время: %s' % (l.get('time') or '—')
    if code == 'day_mismatch':
        d = parse_lesson_date(raw.get('date'))
        wd = l.get('weekday')
        return 'day_index = %s (%s), date = %s (%s)' % (
            raw.get('day_index', raw.get('dayIndex')), DAY_SHORT[wd] if wd is not None else '?',
            raw.get('date'), DAY_SHORT[d.weekday()] if d else '?')
    if code == 'week_unknown':
        return 'week_type = «%s»' % l.get('week_raw')
    if code == 'unknown_group':
        return 'Группа «%s»' % l.get('groupId')
    if code == 'unknown_subject':
        return 'Дисциплина «%s»' % l.get('subject')
    if code == 'date_timestamp':
        return 'date = %s' % (l.get('date') or '—')
    return ''


def fix_for(code, l):
    """Поля для массового исправления проблемы code у занятия l (нормализованного) или None."""
    raw = l.get('raw') or {}
    if code == 'teacher_by_name':
        flags = l.get('teacher_flags') or ()
        if l.get('teacher_uid') and 'by_name' in flags and 'ambiguous' not in flags:
            # всегда поле teacher: приложение строит расписание преподавателя по where('teacher', '==', uid);
            # старый teacherId убираем — функции onSchedule* берут teacherId раньше teacher (как plan_update)
            from .core import DELETE_FIELD
            fields = {'teacher': l['teacher_uid']}
            if 'teacherId' in raw:
                fields['teacherId'] = DELETE_FIELD
            return fields
        return None
    if code == 'week_unknown':
        from .schedule_model import week_type_suggestion
        canon = week_type_suggestion(raw.get('week_type'))
        return {'week_type': canon} if canon else None
    if code in ('day_mismatch', 'date_timestamp') and l.get('weekday') is not None:
        from .schedule_model import day_fields  # «сегодня» — по Алматы
        return day_fields(l['weekday'])
    return None


# ==================== АНАЛИЗ ====================

class Analysis:
    """Результат analyze(): clusters {dim: [кластер]}, quality [проблема], by_hash, статистика."""

    def __init__(self):
        self.clusters = {d: [] for d in DIMS}
        self.quality = []
        self.by_hash = {}
        self.lessons_total = 0
        self.lessons_checked = 0
        self.computed_at = time.time()
        self.duration = 0.0

    def all_clusters(self):
        for d in DIMS:
            for c in self.clusters[d]:
                yield c

    def quality_counts(self):
        return Counter(it['code'] for it in self.quality)


def analyze(lessons, known_groups=None, known_subjects=None):
    """Нормализованные занятия → Analysis. known_groups — названия групп справочника,
    known_subjects — названия дисциплин (None/пусто — эти проверки пропускаются)."""
    started = time.time()
    res = Analysis()
    gkeys = {group_key(g) for g in (known_groups or ()) if group_key(g)} or None
    subj = {norm_text(s) for s in (known_subjects or ()) if s} or None
    items = []
    for l in lessons:
        res.lessons_total += 1
        it = _Item(l)
        if it.usable:
            items.append(it)
        for code in quality_issues(l, gkeys, subj):
            sev, title, _hint = QUALITY[code]
            q = {'hash': quality_hash(code, l), 'code': code, 'severity': sev, 'title': title,
                 'detail': _quality_detail(code, l), 'lesson': lesson_view(l),
                 'fixable': code in FIXABLE and fix_for(code, l) is not None}
            res.quality.append(q)
            res.by_hash[q['hash']] = q
    res.lessons_checked = len(items)
    for dim in DIMS:
        res.clusters[dim] = find_clusters(items, dim)
        for c in res.clusters[dim]:
            res.by_hash.setdefault(c['hash'], c)
    order = list(QUALITY)
    res.quality.sort(key=lambda q: (SEV_RANK[q['severity']], order.index(q['code']),
                                    q['lesson']['weekday'] if q['lesson']['weekday'] is not None else 9,
                                    q['lesson']['start'] if q['lesson']['start'] is not None else 9999,
                                    q['lesson']['groupId']))
    res.duration = time.time() - started
    return res


def summarize(analysis, acked=()):
    """Сводка без принятых. critical/warning/info — уникальные наборы занятий по всем вкладкам
    (кластер, видный на двух вкладках, считается один раз, по худшей критичности); teachers —
    преподаватели с накладками (без «инфо»); quality — проблемы данных (без «инфо»)."""
    acked = set(acked or ())
    uniq = {}
    teachers = set()
    by_dim = {d: {s: 0 for s in SEVERITIES} for d in DIMS}
    accepted = set()
    for dim in DIMS:
        for c in analysis.clusters[dim]:
            if c['hash'] in acked:
                accepted.add(c['hash'])
                continue
            by_dim[dim][c['severity']] += 1
            prev = uniq.get(c['hash'])
            if prev is None or SEV_RANK[c['severity']] < SEV_RANK[prev]:
                uniq[c['hash']] = c['severity']
            if dim == 'teacher' and c['severity'] != 'info':
                teachers.add(c['key'])
    sev_counts = Counter(uniq.values())
    quality = Counter(q['severity'] for q in analysis.quality if q['hash'] not in acked)
    accepted |= {q['hash'] for q in analysis.quality if q['hash'] in acked}
    return {
        'critical': sev_counts.get('critical', 0), 'warning': sev_counts.get('warning', 0),
        'info': sev_counts.get('info', 0), 'teachers': len(teachers),
        'quality': quality.get('critical', 0) + quality.get('warning', 0),
        'quality_critical': quality.get('critical', 0), 'by_dim': by_dim, 'accepted': len(accepted),
        'lessons': analysis.lessons_total, 'computed_at': analysis.computed_at,
    }


# ==================== ПРОВЕРКА ОДНОГО ЗАНЯТИЯ (форма SCHEDULE) ====================

def _conflict_message(kind, other, weeks):
    o = other.l
    teacher = short_name(teacher_display(o))
    when = o.get('time_label') or o.get('time') or ''
    subject = o.get('subject') or 'без дисциплины'
    group = o.get('groupId') or '—'
    with_teacher = ' (%s)' % teacher if teacher else ''
    if kind == 'teacher_double':
        text = '%s уже ведёт пару у %s в %s: %s%s' % (
            teacher or 'Преподаватель', group, when, subject, ', ауд. ' + o['room'] if o.get('room') else '')
    elif kind == 'room_double':
        text = 'Аудитория %s занята в %s: %s, %s%s' % (o.get('room') or '—', when, group, subject, with_teacher)
    elif kind == 'group_overlap':
        text = 'У группы %s в %s уже есть пара: %s%s' % (group, when, subject, with_teacher)
    elif kind == 'subgroups':
        text = 'Возможно, подгруппы: у %s в это время %s%s' % (group, subject, with_teacher)
    elif kind == 'duplicate':
        text = 'Дубль: такое же занятие уже есть у %s в %s' % (group, when)
    else:
        text = 'Поток: в это время та же пара у %s (ауд. %s)' % (group, o.get('room') or '—')
    if weeks == 'numerator':
        text += ' — по числителю'
    elif weeks == 'denominator':
        text += ' — по знаменателю'
    elif weeks.startswith('date:'):
        text += ' — только %s.%s' % (weeks[13:15], weeks[10:12])
    return text


def find_lesson_conflicts(candidate, others, exclude_ids=()):
    """Накладки одного занятия (нормализованного) с другими (обычно — занятия того же дня):
    [{severity, kind, title, message, dims, weeks, weeks_label, overlap_min, other, other_id, ids}],
    по одной записи на каждое пересекающееся занятие, по критичности."""
    cand = _item(candidate)
    if not cand.usable:
        return []
    exclude = {str(x) for x in (exclude_ids or ()) if x}
    out = []
    for l in others:
        o = _item(l)
        if str(o.id) in exclude or o.id == cand.id or not o.usable or o.wd != cand.wd:
            continue
        if not (o.start < cand.end and cand.start < o.end):
            continue
        dims = [d for d in DIMS if cand.key(d) and cand.key(d) == o.key(d)]
        if not dims:
            continue
        weeks = pair_weeks(cand, o)
        if not weeks:
            continue
        kind, flags = classify_pair(cand, o)
        sev, uncertain = pair_severity(kind, cand, o, 'teacher' if 'teacher' in dims else dims[0])
        out.append({
            'severity': sev, 'kind': kind, 'title': KINDS[kind][1], 'dims': dims, 'weeks': weeks,
            'weeks_label': weeks_label([weeks]), 'flags': flags, 'uncertain': uncertain,
            'overlap_min': min(cand.end, o.end) - max(cand.start, o.start),
            'message': _conflict_message(kind, o, weeks),
            'other': lesson_view(o.l), 'other_id': o.id, 'ids': [o.id],
        })
    out.sort(key=lambda c: (SEV_RANK[c['severity']], c['other']['start'] or 0, c['other']['groupId']))
    return out


# ==================== ОБЁРТКА: КЭШ, СНАПШОТ, ПРИНЯТЫЕ ====================
# Ключи кэша — в пространстве 'conflicts:' (его сбрасывают invalidate_schedule_caches() и
# invalidate_directory_caches()). Анализ привязан к объекту снапшота: новый снапшот → новый анализ.

ANALYSIS_KEY = 'conflicts:analysis'
SUMMARY_KEY = 'conflicts:summary'
ACKS_KEY = 'conflicts:acks'
GROUPS_KEY = 'conflicts:groups_list'
SUMMARY_TTL = 600     # бейдж сайдбара и дашборд: без записей в расписание — не чаще раза в 10 мин
ACKS_COLLECTION = 'schedule_conflict_acks'


def _known_groups():
    from .core import cache, db, fs_opts

    def load():
        return [d.id for d in db.collection('groups_list').select([]).stream(**fs_opts(True))]
    return cache.get_or_set(GROUPS_KEY, load)


def get_analysis(force=False):
    """Analysis для текущего снапшота расписания (пересчёт — только при новом снапшоте)."""
    from .core import cache
    from .schedule_model import get_all_subjects, load_lessons_snapshot
    snap = load_lessons_snapshot(force=force)
    cached = cache.get(ANALYSIS_KEY)
    if cached is not None and cached[0] is snap:
        return cached[1]
    try:
        groups = _known_groups()
    except Exception:
        log.warning('Справочник групп недоступен — проверка «Группы нет в справочнике» пропущена', exc_info=True)
        groups = None
    try:
        subjects = get_all_subjects()
    except Exception:
        log.warning('Справочник дисциплин недоступен', exc_info=True)
        subjects = None
    analysis = analyze(snap.lessons, groups, subjects)
    log.info('Накладки: %d занятий, %d кластеров, %d проблем данных за %.0f мс',
             analysis.lessons_total, sum(len(v) for v in analysis.clusters.values()),
             len(analysis.quality), analysis.duration * 1000)
    cache.set(ANALYSIS_KEY, (snap, analysis), ttl=3600)
    # Сводка для сайдбара/дашборда — сразу по свежему анализу (страница и бейдж не расходятся).
    try:
        cache.set(SUMMARY_KEY, summarize(analysis, load_acks().keys()), ttl=SUMMARY_TTL)
    except Exception:
        log.warning('Сводка накладок не обновлена', exc_info=True)
    return analysis


def load_acks():
    """{hash: отметка} из schedule_conflict_acks (кэш 5 мин; сбрасывается при принятии/возврате)."""
    from .core import cache, db, fs_opts

    def load():
        out = {}
        for snap in db.collection(ACKS_COLLECTION).limit(5000).stream(**fs_opts(True)):
            d = snap.to_dict() or {}
            d['id'] = snap.id
            out[d.get('hash') or snap.id] = d
        return out
    return cache.get_or_set(ACKS_KEY, load)


def invalidate():
    """Сбросить кэш анализа, сводки и принятых (после записи отметок) и бейджи сайдбара."""
    from .core import cache
    cache.invalidate_prefix('conflicts:')
    cache.invalidate('nav:badges')


def summary():
    """Сводка для дашборда и бейджа сайдбара: {'critical', 'warning', 'teachers', 'info', 'quality',
    'by_dim', 'accepted', …} без принятых накладок. Кэш 10 мин (запись расписания из CMS сбрасывает
    сразу). None — если расписание недоступно."""
    from .core import cache
    cached = cache.get(SUMMARY_KEY)
    if cached is not None:
        return cached
    try:
        analysis = get_analysis()
        cached = cache.get(SUMMARY_KEY)
        if cached is not None:
            return cached
        try:
            acked = load_acks().keys()
        except Exception:
            log.warning('Принятые накладки недоступны', exc_info=True)
            acked = ()
        result = summarize(analysis, acked)
    except Exception:
        log.exception('Сводка накладок не посчитана')
        return None
    cache.set(SUMMARY_KEY, result, ttl=SUMMARY_TTL)
    return result


def check_lesson(lesson, exclude_id=None):
    """Накладки, которые даст занятие lesson (dict как документ schedules; может содержать 'id'),
    со всем остальным расписанием, без занятия exclude_id (при правке). Для формы SCHEDULE:
    [{severity, kind, title, message, dims, weeks, overlap_min, other: {...}, other_id, ids}]."""
    if not isinstance(lesson, dict):
        return []
    from .schedule_model import load_lessons_snapshot, normalize_lesson
    snap = load_lessons_snapshot()
    cand_id = str(lesson.get('id') or '') or '__candidate__'
    cand = normalize_lesson(cand_id, lesson, snap.teacher_index)
    if cand.get('weekday') is None:
        return []
    exclude = {x for x in (exclude_id, cand_id) if x}
    return find_lesson_conflicts(cand, snap.by_day.get(cand['weekday'], []), exclude)


def accept(hashes, note='', admin=None):
    """Принять (игнорировать) накладки/проблемы данных по хэшам текущего анализа → число принятых.
    Хэши, которых нет в анализе (страница устарела), пропускаются."""
    from .core import SERVER_TIMESTAMP, chunked, db
    analysis = get_analysis()
    admin = admin or {}
    docs = []
    for h in OrderedDict.fromkeys(str(x) for x in hashes or ()):
        obj = analysis.by_hash.get(h)
        if obj is None:
            continue
        if 'code' in obj:  # проблема данных
            meta = {'dim': 'quality', 'kind': obj['code'], 'ids': [obj['lesson']['id']],
                    'label': '%s: %s' % (obj['title'], lesson_label(obj['lesson']))}
        else:
            meta = {'dim': obj['dim'], 'kind': obj['kind'], 'ids': obj['ids'][:20],
                    'label': '%s: %s, %s %s' % (obj['title'], obj['entity'], obj['day_short'], obj['time_label'])}
        meta['label'] = meta['label'][:200]
        docs.append((h, dict(meta, hash=h, note=str(note or '').strip()[:300], by=admin.get('email') or '',
                             byUid=admin.get('uid') or '', at=SERVER_TIMESTAMP)))
    for part in chunked(docs, 400):
        batch = db.batch()
        for h, d in part:
            batch.set(db.collection(ACKS_COLLECTION).document(h), d)
        batch.commit()
    invalidate()
    return len(docs)


def unaccept(hashes):
    """Вернуть принятые в список (удалить отметки) → число удалённых."""
    from .core import chunked, db
    ids = [h for h in OrderedDict.fromkeys(str(x) for x in hashes or ()) if h and '/' not in h and len(h) <= 128]
    for part in chunked(ids, 400):
        batch = db.batch()
        for h in part:
            batch.delete(db.collection(ACKS_COLLECTION).document(h))
        batch.commit()
    invalidate()
    return len(ids)


def stale_acks(analysis=None, acks=None):
    """Отметки «принято», которым больше не соответствует ни одна накладка (занятия изменены)."""
    analysis = analysis or get_analysis()
    acks = load_acks() if acks is None else acks
    return [a for h, a in acks.items() if h not in analysis.by_hash]
