# cms/schedule_model.py — модель расписания, общая для расписания, аудиторного
# фонда и накладок (владелец — FOUNDATION; остальным — только чтение).
#
# Занятие в `schedules` хранит день недели в одной из двух схем (как в приложении):
#   • рекуррентное — `day_index` (0 = Пн … 6 = Вс), без `date`;
#   • с датой      — `date` ('yyyy-MM-dd'), без `day_index` (так писал старый веб).
# Конструктор приложения пишет ОБА поля синхронно; CMS при записи тоже пишет оба
# (day_fields()). Единый резолвер дня — lesson_weekday(): приоритет day_index,
# иначе день недели из date (зеркало ScheduleService.lessonWeekday в APP).
#
# Здесь же: разбор времени 'HH.MM-HH.MM' / 'HH:MM-HH:MM' в минуты, чётность недели
# как isNumeratorWeek в APP (по Алматы), ключи преподавателя/аудитории/группы с
# нормализацией, справочники (группы, преподаватели, дисциплины, аудитории) и
# кэшированный снапшот всех занятий.

import re
import time
from collections import OrderedDict
from datetime import date, datetime, timedelta

from .core import ALMATY, cache, db, fs_opts, log, today_almaty, where

# ==================== СЕТКА ЗВОНКОВ И ДНИ ====================

CALL_SCHEDULE = {
    "monday": [
        "08.00-08.20", "08.25-09.15", "09.20-10.10", "10.25-11.15",
        "11.35-11.55", "11.35-12.25", "12.00-12.50", "12.55-13.45", "14.00-14.50",
        "14.55-15.15", "14.55-15.45", "15.20-16.10", "15.50-16.40", "16.15-17.05", "17.20-18.10"
    ],
    "other": [
        "08.00-08.50", "08.55-09.45", "10.00-10.50", "10.55-11.45", "11.45-12.05",
        "12.05-12.55", "13.00-13.50", "14.05-14.55", "14.30-14.50", "15.00-15.50",
        "15.55-16.45", "16.50-17.40", "17.55-18.45"
    ]
}

DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]
DAY_NAMES_FULL = DAY_NAMES + ["Воскресенье"]
DAY_SHORT = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

# Длительность занятия, если в строке времени нет конца и слота с таким началом нет.
DEFAULT_LESSON_MINUTES = 50
# Учебный день (для «Качества данных»): занятия вне этого окна подозрительны.
SCHOOL_DAY_START = 7 * 60
SCHOOL_DAY_END = 21 * 60

WEEK_TYPES = ('always', 'numerator', 'denominator')
WEEK_LABELS = {'always': 'Каждую неделю', 'numerator': 'Числитель', 'denominator': 'Знаменатель'}
WEEK_SHORT = {'always': 'Всегда', 'numerator': 'Ч', 'denominator': 'З'}


def day_name(weekday, default='—'):
    """0..6 → 'Понедельник'…'Воскресенье'."""
    if isinstance(weekday, int) and 0 <= weekday <= 6:
        return DAY_NAMES_FULL[weekday]
    return default


# ==================== ДЕНЬ НЕДЕЛИ ЗАНЯТИЯ ====================

_DATE_RE = re.compile(r'^\s*(\d{4})-?(\d{2})-?(\d{2})')


def parse_lesson_date(raw):
    """Поле date → date или None. Строка 'yyyy-MM-dd' (как DateTime.tryParse в APP),
    также datetime/date (Timestamp из Firestore)."""
    if raw is None or raw == '':
        return None
    if isinstance(raw, datetime):
        return (raw.astimezone(ALMATY) if raw.tzinfo else raw).date()
    if isinstance(raw, date):
        return raw
    m = _DATE_RE.match(str(raw))
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def _day_index_value(lesson):
    """day_index (или dayIndex) как int 0..6, иначе None. Как в APP: int или строка-число."""
    raw = lesson.get('day_index')
    if raw is None:
        raw = lesson.get('dayIndex')
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        idx = raw
    else:
        try:
            idx = int(str(raw).strip())
        except (TypeError, ValueError):
            return None
    return idx if 0 <= idx <= 6 else None


def lesson_weekday(lesson):
    """День недели занятия: 0 = Пн … 6 = Вс, или None.

    Зеркало ScheduleService.lessonWeekday (APP): приоритет у day_index (0..6), иначе
    день недели из date. ВНИМАНИЕ: APP возвращает ISO 1..7, здесь — 0..6 (как day_index
    и date.weekday() в Python); для ISO — lesson_iso_weekday()."""
    if not lesson:
        return None
    idx = _day_index_value(lesson)
    if idx is not None:
        return idx
    d = parse_lesson_date(lesson.get('date'))
    return d.weekday() if d else None


def lesson_iso_weekday(lesson):
    """То же, что lesson_weekday, но ISO 1 = Пн … 7 = Вс (ровно как в APP)."""
    wd = lesson_weekday(lesson)
    return None if wd is None else wd + 1


def weekday_info(lesson):
    """(weekday 0..6|None, источник 'day_index'|'date'|None, расхождение day_index и date: bool)."""
    idx = _day_index_value(lesson)
    d = parse_lesson_date(lesson.get('date'))
    if idx is not None:
        return idx, 'day_index', bool(d is not None and d.weekday() != idx)
    if d is not None:
        return d.weekday(), 'date', False
    return None, None, False


def next_weekday_date(weekday, today=None):
    """Ближайшая дата с этим днём недели, начиная с сегодня (по Алматы) — как
    _getNextWeekdayDate в конструкторе APP."""
    today = today or today_almaty()
    ahead = int(weekday) - today.weekday()
    if ahead < 0:
        ahead += 7
    return today + timedelta(days=ahead)


def day_fields(weekday, today=None):
    """Поля дня для ЗАПИСИ занятия: {'day_index': 0..6, 'date': 'yyyy-MM-dd'} — оба
    синхронно, как пишет конструктор приложения."""
    wd = int(weekday)
    if not 0 <= wd <= 6:
        raise ValueError('weekday должен быть 0..6')
    return {'day_index': wd, 'date': next_weekday_date(wd, today).strftime('%Y-%m-%d')}


# ==================== ВРЕМЯ ЗАНЯТИЯ ====================

_HM_RE = re.compile(r'(\d{1,2})[:.](\d{2})')


def _hm_to_minutes(match):
    h, m = int(match.group(1)), int(match.group(2))
    if h > 23 or m > 59:
        return None
    return h * 60 + m


def parse_time_range(raw):
    """'08.30-09.50' / '8:30 – 9:50' / '08:30' → (start, end) в минутах от полуночи.

    Как LessonTime.parse (APP): первое совпадение ЧЧ[.:]ММ — начало, второе — конец;
    часы > 23 или минуты > 59 → None для этой части. Нет совпадений → (None, None)."""
    matches = list(_HM_RE.finditer(str(raw or '')))
    if not matches:
        return None, None
    start = _hm_to_minutes(matches[0])
    end = _hm_to_minutes(matches[1]) if len(matches) > 1 else None
    return start, end


def fmt_minutes(minutes, default='--:--'):
    """510 → '08:30'."""
    if minutes is None:
        return default
    return '%02d:%02d' % (int(minutes) // 60, int(minutes) % 60)


def time_label(start, end):
    """(510, 590) → '08:30–09:50'; без конца — '08:30'."""
    if start is None:
        return ''
    return fmt_minutes(start) + ('–' + fmt_minutes(end) if end is not None else '')


def call_slots(weekday):
    """Слоты сетки звонков для дня: [(метка 'HH.MM-HH.MM', start, end)]. Пн — своя сетка."""
    key = 'monday' if weekday == 0 else 'other'
    out = []
    for label in CALL_SCHEDULE[key]:
        s, e = parse_time_range(label)
        out.append((label, s, e))
    return out


def slot_end_for(weekday, start):
    """Конец слота сетки звонков с таким же началом (самый ранний), иначе None."""
    ends = [e for _l, s, e in call_slots(weekday if weekday is not None else 1) if s == start and e is not None]
    return min(ends) if ends else None


def lesson_interval(lesson, weekday=None):
    """(start, end, end_assumed) занятия в минутах. Нет конца → конец слота с тем же
    началом, иначе start + DEFAULT_LESSON_MINUTES (end_assumed=True). Не разобрано → (None, None, False)."""
    if weekday is None:
        weekday = lesson_weekday(lesson)
    start, end = parse_time_range(lesson.get('time'))
    if start is None:
        return None, None, False
    if end is None:
        end = slot_end_for(weekday, start) or start + DEFAULT_LESSON_MINUTES
        return start, end, True
    return start, end, False


def intervals_overlap(a_start, a_end, b_start, b_end):
    """Строгое пересечение интервалов: касание (10:55–11:45 и 11:45–12:05) — НЕ пересечение."""
    if None in (a_start, a_end, b_start, b_end):
        return False
    return a_start < b_end and b_start < a_end


def in_call_grid(lesson, weekday=None):
    """Совпадает ли время занятия со слотом сетки звонков своего дня (для «Качества данных»)."""
    if weekday is None:
        weekday = lesson_weekday(lesson)
    start, end = parse_time_range(lesson.get('time'))
    if start is None or end is None:
        return False
    return any(s == start and e == end for _l, s, e in call_slots(weekday))


# ==================== ЧЁТНОСТЬ НЕДЕЛИ ====================

def _as_date(d):
    if d is None:
        return today_almaty()
    if isinstance(d, datetime):
        return (d.astimezone(ALMATY) if d.tzinfo else d).date()
    return d


def academic_year_start(d=None):
    """1 сентября учебного года, в который входит дата d (по умолчанию — сегодня в Алматы)."""
    d = _as_date(d)
    return date(d.year if d.month >= 9 else d.year - 1, 9, 1)


def first_academic_monday(d=None):
    """Понедельник недели, в которую входит 1 сентября (неделя № 1)."""
    start = academic_year_start(d)
    return start - timedelta(days=start.weekday())


def academic_week_number(d=None):
    """Номер учебной недели (1 — неделя с 1 сентября), как weekNum в APP."""
    d = _as_date(d)
    return (d - first_academic_monday(d)).days // 7 + 1


def is_numerator_week(d=None):
    """Числитель ли неделя даты d — точное зеркало ScheduleService.isNumeratorWeek (APP):
    понедельник недели с 1 сентября = неделя 1; нечётная неделя = числитель.
    Дата по умолчанию — сегодня по Алматы (UTC+5), не по часам сервера."""
    return academic_week_number(d) % 2 != 0


def current_week_type(d=None):
    """'numerator' | 'denominator' для недели даты d."""
    return 'numerator' if is_numerator_week(d) else 'denominator'


def get_week_parity():
    """Старое имя (шаблоны используют is_even_week): True = сейчас ЧИСЛИТЕЛЬ.

    Раньше считалось иначе, чем в приложении (расходилось в годы, где 1 сентября —
    понедельник) и по UTC; теперь = is_numerator_week(сегодня по Алматы)."""
    return is_numerator_week(today_almaty())


_WEEK_ALIASES = {
    'always': 'always', 'всегда': 'always', 'every': 'always', 'both': 'always', 'все': 'always',
    'numerator': 'numerator', 'числитель': 'numerator', 'ч': 'numerator', 'num': 'numerator',
    'denominator': 'denominator', 'знаменатель': 'denominator', 'з': 'denominator', 'den': 'denominator',
}


def norm_week_type(value):
    """week_type → ('always'|'numerator'|'denominator', unknown: bool).

    Как APP (ScheduleService.applyFilter: `(week_type ?? 'always').toString()` сравнивается буквально):
    известны только None и точные 'always' | 'numerator' | 'denominator'. Всё остальное — пустая строка,
    регистр, пробелы, синонимы ('Числитель', 'ч') — unknown=True: приложение прячет такое занятие при
    фильтре недели («Качество данных»). Неделя для CMS — по синониму (подсказка, во что исправить),
    иначе 'always'."""
    if value is None:
        return 'always', False
    if isinstance(value, str) and value in WEEK_TYPES:
        return value, False
    return week_type_suggestion(value) or 'always', True


def week_type_suggestion(value):
    """Канонический ключ для неканонического week_type ('Числитель' → 'numerator', '' → 'always') или None."""
    s = str('' if value is None else value).strip().lower().replace('ё', 'е')
    if not s:
        return 'always'
    return _WEEK_ALIASES.get(s)


def weeks_overlap(a, b):
    """Совместимость недель двух занятий: 'always' с любым; числитель × знаменатель — нет."""
    a = norm_week_type(a)[0] if a not in WEEK_TYPES else a
    b = norm_week_type(b)[0] if b not in WEEK_TYPES else b
    return a == 'always' or b == 'always' or a == b


def week_occurs(week, d=None):
    """Идёт ли занятие с таким типом недели на неделе даты d (по умолчанию — текущей)."""
    w = norm_week_type(week)[0]
    return w == 'always' or w == current_week_type(d)


# ==================== НОРМАЛИЗАЦИЯ КЛЮЧЕЙ ====================

_SPACES_RE = re.compile(r'\s+')
_DASHES = str.maketrans({'‐': '-', '‑': '-', '‒': '-', '–': '-', '—': '-',
                         '―': '-', '−': '-'})
# Латиница, похожая на кириллицу (в нижнем регистре; заглавные приводятся до перевода).
_LATIN_TO_CYR = str.maketrans({'a': 'а', 'b': 'в', 'c': 'с', 'e': 'е', 'h': 'н', 'k': 'к', 'm': 'м',
                               'o': 'о', 'p': 'р', 't': 'т', 'x': 'х', 'y': 'у'})


def norm_text(value):
    """casefold, ё→е, единые тире, схлопнутые пробелы."""
    s = str(value or '').translate(_DASHES).casefold().replace('ё', 'е')
    return _SPACES_RE.sub(' ', s).strip()


def norm_name(value):
    """ФИО для сравнения: norm_text без точек и запятых ('Иванов И.И.' → 'иванов и и')."""
    s = re.sub(r'[.,]', ' ', norm_text(value))
    return _SPACES_RE.sub(' ', s).strip()


VIRTUAL_ROOMS = {'-', '--', '—', '–', 'нет', 'онлайн', 'online', 'дистант', 'дистанционно',
                 'zoom', 'teams', 'meet', 'дот', 'удаленно', 'удалённо'}
_ROOM_PREFIX_RE = re.compile(r'^(кабинет|каб\.?|аудитория|ауд\.?|room|№|n°)\s*', re.IGNORECASE)


def is_virtual_room(raw):
    """Пусто, прочерк, «онлайн», «zoom» и т. п. — не физическая аудитория."""
    s = norm_text(raw)
    return not s or s in VIRTUAL_ROOMS or s.strip('-—– ') == ''


def room_key(raw):
    """Ключ аудитории для сравнения: без префиксов «ауд.»/«каб.»/«№», регистра, пробелов
    и дефисов, латиница-двойник → кириллица ('Ауд. A-205' == 'а205'). None — пусто/виртуальная."""
    if is_virtual_room(raw):
        return None
    s = norm_text(raw)
    prev = None
    while prev != s:  # «ауд. № 205» — несколько префиксов подряд
        prev = s
        s = _ROOM_PREFIX_RE.sub('', s).strip()
    s = s.translate(_LATIN_TO_CYR)
    s = re.sub(r'[\s\-_]+', '', s)
    return s or None


def group_key(raw):
    """Ключ группы для сравнения: регистр, пробелы, тире и латиница-двойник не важны."""
    s = norm_text(raw)
    if not s:
        return None
    s = s.translate(_LATIN_TO_CYR)
    return re.sub(r'[\s_]+', '', s) or None


def looks_like_name(value):
    """ФИО, а не uid (как CollegeScheduleService.looksLikeName в APP: есть пробел)."""
    return ' ' in str(value or '').strip()


# ==================== СПРАВОЧНИКИ (с кэшем) ====================

def group_sort_key(group_name):
    """Ключ сортировки для названий групп"""
    match = re.match(r"([А-ЯЁA-Z]+)\s*(\d+)-(\d+)([пП]?)", group_name or '')
    if match:
        return (match.group(1), int(match.group(2)), int(match.group(3)), 1 if match.group(4) else 0)
    return ("ZZZ", 99, 99, 0)


def room_sort_key(room_name):
    """Натуральная сортировка аудиторий: 101, 102, 201, А-1, Б-2 и т.п."""
    s = str(room_name or '').strip()
    if not s:
        return ('ZZZZ', 99999, '')
    m = re.match(r'^([^\d]*)(\d+)(.*)$', s)
    if m:
        prefix = (m.group(1) or '').upper().strip()
        try:
            number = int(m.group(2))
        except ValueError:
            number = 99999
        suffix = (m.group(3) or '').upper().strip()
        return (prefix, number, suffix)
    return (s.upper(), 99999, '')


def _teacher_directory():
    """{'users': {uid: ФИО} (users role==teacher), 'legacy': {id: ФИО} (старая коллекция teachers)}."""
    def load():
        users, legacy = {}, {}
        for doc in where(db.collection('users'), 'role', '==', 'teacher').select(['name']).stream(**fs_opts(True)):
            name = (doc.to_dict() or {}).get('name')
            if name:
                users[doc.id] = name
        try:
            for doc in db.collection('teachers').select(['name']).stream(**fs_opts(True)):
                name = (doc.to_dict() or {}).get('name')
                if name:
                    legacy[doc.id] = name
        except Exception:
            log.warning('Коллекция teachers недоступна', exc_info=True)
        return {'users': users, 'legacy': legacy}
    return cache.get_or_set('all_teachers:dir', load)


def get_all_teachers():
    """Словарь преподавателей {id: ФИО}, отсортированный по ФИО (кэш 5 мин, ключ 'all_teachers')."""
    def load():
        d = _teacher_directory()
        teachers = dict(d['users'])
        teachers.update(d['legacy'])  # как раньше: старая коллекция teachers дополняет users
        return dict(sorted(teachers.items(), key=lambda x: x[1]))
    return cache.get_or_set('all_teachers', load)


def get_teacher_name(teacher_id):
    """ФИО преподавателя по id (из кэша справочника); '' если не найден. Jinja-глобал."""
    if not teacher_id:
        return ''
    return get_all_teachers().get(teacher_id, '')


def get_all_subjects():
    """Список названий дисциплин (кэш 5 мин, ключ 'all_subjects')."""
    def load():
        subjects = []
        for doc in db.collection('subjects').stream(**fs_opts(True)):
            name = (doc.to_dict() or {}).get('name')
            if name:
                subjects.append(name)
        return sorted(subjects)
    return cache.get_or_set('all_subjects', load)


def get_all_groups():
    """Все группы: groups_list + группы из расписания (снапшот), натуральная сортировка.
    Кэш 5 мин, ключ 'all_groups'."""
    def load():
        groups = set()
        for doc in db.collection('groups_list').select([]).stream(**fs_opts(True)):
            groups.add(doc.id)
        for g in load_lessons_snapshot().by_group:
            if g:
                groups.add(g)
        return sorted(groups, key=group_sort_key)
    return cache.get_or_set('all_groups', load)


def get_all_rooms():
    """Уникальные значения поля room из расписания (как раньше — строки как есть),
    натуральная сортировка. Кэш 5 мин, ключ 'all_rooms'."""
    def load():
        rooms = set()
        for lesson in load_lessons_snapshot().lessons:
            if lesson['room']:
                rooms.add(lesson['room'])
        return sorted(rooms, key=room_sort_key)
    return cache.get_or_set('all_rooms', load)


class TeacherIndex:
    """Справочник для сопоставления поля teacher занятия с преподавателем.

    by_uid — {uid: ФИО} из users (role==teacher); legacy — {id: ФИО} из старой коллекции
    teachers; by_name — {norm_name(ФИО): [uid, …]} (однофамильцы → несколько uid)."""

    def __init__(self, users, legacy=None):
        self.by_uid = dict(users or {})
        self.legacy = dict(legacy or {})
        self.by_name = {}
        for uid, name in self.by_uid.items():
            self.by_name.setdefault(norm_name(name), []).append(uid)

    def name(self, uid):
        return self.by_uid.get(uid) or self.legacy.get(uid) or ''


def build_teacher_index():
    """TeacherIndex по кэшированному справочнику преподавателей."""
    d = _teacher_directory()
    return TeacherIndex(d['users'], d['legacy'])


def resolve_teacher(raw, index=None):
    """Поле teacher занятия → {'key', 'uid', 'name', 'flags'}.

    key: 'uid:<uid>' (найден по uid или однозначно по ФИО) | 'name:<норм. ФИО>' | None (пусто).
    flags (для «Качества данных»): 'no_teacher' — пусто; 'by_name' — записано ФИО вместо uid
    (сопоставлено однозначно); 'ambiguous' — ФИО совпадает с несколькими преподавателями;
    'legacy' — id из старой коллекции teachers (приложение его не покажет); 'orphan' — похоже
    на uid, но в справочнике нет; 'unknown_name' — ФИО, которого нет в справочнике."""
    index = index if index is not None else build_teacher_index()
    s = str(raw or '').strip()
    if not s:
        return {'key': None, 'uid': None, 'name': '', 'flags': ['no_teacher']}
    if s in index.by_uid:
        return {'key': 'uid:' + s, 'uid': s, 'name': index.by_uid[s], 'flags': []}
    norm = norm_name(s)
    if looks_like_name(s):
        uids = index.by_name.get(norm, [])
        if len(uids) == 1:
            return {'key': 'uid:' + uids[0], 'uid': uids[0], 'name': index.by_uid[uids[0]], 'flags': ['by_name']}
        if len(uids) > 1:
            return {'key': 'name:' + norm, 'uid': None, 'name': s, 'flags': ['by_name', 'ambiguous']}
    if s in index.legacy:
        return {'key': 'uid:' + s, 'uid': s, 'name': index.legacy[s], 'flags': ['legacy']}
    if not looks_like_name(s):
        uids = index.by_name.get(norm, [])
        if len(uids) == 1:  # ФИО одним словом («Иванов»), совпало однозначно
            return {'key': 'uid:' + uids[0], 'uid': uids[0], 'name': index.by_uid[uids[0]], 'flags': ['by_name']}
        return {'key': 'uid:' + s, 'uid': s, 'name': s, 'flags': ['orphan']}
    return {'key': 'name:' + norm, 'uid': None, 'name': s, 'flags': ['unknown_name']}


def teacher_key(raw, index=None):
    """Только ключ преподавателя ('uid:…' | 'name:…' | None)."""
    return resolve_teacher(raw, index)['key']


# ==================== СНАПШОТ ЗАНЯТИЙ ====================

SNAPSHOT_KEY = 'schedule:snapshot'
SNAPSHOT_TTL = 120  # с; запись из CMS сбрасывает сразу (invalidate_schedule_caches)


def normalize_lesson(doc_id, data, index=None):
    """Документ schedules → нормализованный dict (для расписания, аудиторий и накладок).

    Ключи: id, raw (исходный документ), groupId, group_key, subject, teacher (как в БД),
    teacher_key/teacher_uid/teacher_name/teacher_flags, room (как в БД), room_key, room_virtual,
    time (как в БД), start/end (мин), end_assumed, time_ok, time_label, weekday (0..6|None),
    day_name, weekday_source ('day_index'|'date'|None), day_mismatch, date ('yyyy-MM-dd'|None),
    day_index (0..6|None), kind ('recurring' — есть day_index, 'dated' — только date,
    'unknown'), week ('always'|'numerator'|'denominator'), week_raw, week_unknown, in_grid."""
    data = data or {}
    wd, source, mismatch = weekday_info(data)
    start, end, end_assumed = lesson_interval(data, wd)
    week, week_unknown = norm_week_type(data.get('week_type'))
    teacher_raw = str(data.get('teacher') or data.get('teacherId') or '').strip()
    t = resolve_teacher(teacher_raw, index)
    room_raw = str(data.get('room') or '').strip()
    group = str(data.get('groupId') or '').strip()
    d = parse_lesson_date(data.get('date'))
    day_idx = _day_index_value(data)
    return {
        'id': doc_id,
        'raw': data,
        'groupId': group,
        'group_key': group_key(group),
        'subject': str(data.get('subject') or '').strip(),
        'teacher': teacher_raw,
        'teacher_key': t['key'],
        'teacher_uid': t['uid'],
        'teacher_name': t['name'],
        'teacher_flags': t['flags'],
        'room': room_raw,
        'room_key': room_key(room_raw),
        'room_virtual': bool(room_raw) and is_virtual_room(room_raw),
        'time': str(data.get('time') or '').strip(),
        'start': start,
        'end': end,
        'end_assumed': end_assumed,
        'time_ok': start is not None and end is not None and end > start,
        'time_label': time_label(start, end),
        'weekday': wd,
        'day_name': day_name(wd),
        'weekday_source': source,
        'day_mismatch': mismatch,
        'date': d.strftime('%Y-%m-%d') if d else None,
        'day_index': day_idx,
        'kind': 'recurring' if day_idx is not None else ('dated' if d else 'unknown'),
        'week': week,
        'week_raw': data.get('week_type'),
        'week_unknown': week_unknown,
        'in_grid': in_call_grid(data, wd) if wd is not None else False,
    }


def lesson_sort_key(lesson):
    """Сортировка нормализованных занятий: день, начало (по минутам, не по строке), группа."""
    wd = lesson.get('weekday')
    start = lesson.get('start')
    return (wd if wd is not None else 9, start if start is not None else 9999,
            group_sort_key(lesson.get('groupId') or ''), lesson.get('subject') or '')


class LessonsSnapshot:
    """Все занятия колледжа, нормализованные, с индексами.

    lessons — список normalize_lesson(); by_id; by_group {groupId: [..]}; by_teacher
    {teacher_key: [..]}; by_room {room_key: [..]}; by_day {0..6|None: [..]};
    teacher_index — TeacherIndex; loaded_at — time.time()."""

    def __init__(self, lessons, teacher_index, loaded_at=None):
        self.lessons = sorted(lessons, key=lesson_sort_key)
        self.teacher_index = teacher_index
        self.loaded_at = loaded_at or time.time()
        self.by_id = {}
        self.by_group = OrderedDict()
        self.by_teacher = {}
        self.by_room = {}
        self.by_day = {}
        for l in self.lessons:
            self.by_id[l['id']] = l
            self.by_group.setdefault(l['groupId'], []).append(l)
            if l['teacher_key']:
                self.by_teacher.setdefault(l['teacher_key'], []).append(l)
            if l['room_key']:
                self.by_room.setdefault(l['room_key'], []).append(l)
            self.by_day.setdefault(l['weekday'], []).append(l)

    def __len__(self):
        return len(self.lessons)

    def __iter__(self):
        return iter(self.lessons)

    def groups(self):
        """Группы из расписания (натуральная сортировка)."""
        return sorted([g for g in self.by_group if g], key=group_sort_key)

    def rooms(self):
        """[(room_key, отображаемое название)] — физические аудитории, натуральная сортировка."""
        names = {}
        for key, lessons in self.by_room.items():
            counts = {}
            for l in lessons:
                counts[l['room']] = counts.get(l['room'], 0) + 1
            names[key] = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]
        return sorted(names.items(), key=lambda kv: room_sort_key(kv[1]))

    def teachers(self):
        """[(teacher_key, ФИО)] по алфавиту."""
        out = {}
        for key, lessons in self.by_teacher.items():
            out[key] = lessons[0]['teacher_name'] or lessons[0]['teacher'] or key
        return sorted(out.items(), key=lambda kv: norm_name(kv[1]))


def _load_snapshot():
    started = time.time()
    index = build_teacher_index()
    lessons = []
    for doc in db.collection('schedules').stream(**fs_opts(True)):
        lessons.append(normalize_lesson(doc.id, doc.to_dict() or {}, index))
    snap = LessonsSnapshot(lessons, index)
    log.info('Снапшот расписания: %d занятий за %.2f с', len(lessons), time.time() - started)
    return snap


def load_lessons_snapshot(force=False):
    """Снапшот всех занятий (кэш SNAPSHOT_TTL с, общий для всех страниц процесса).

    Одно полное чтение schedules вместо скана на каждой странице. После ЛЮБОЙ записи в
    schedules вызывайте invalidate_schedule_caches()."""
    if force:
        cache.invalidate(SNAPSHOT_KEY)
    return cache.get_or_set(SNAPSHOT_KEY, _load_snapshot, ttl=SNAPSHOT_TTL)


def invalidate_schedule_caches():
    """Сбросить снапшот расписания и производные кэши (группы, аудитории, накладки).
    Межпроцессно: другие воркеры увидят сброс в течение секунды."""
    cache.invalidate_prefix('schedule:')
    cache.invalidate('all_groups')
    cache.invalidate('all_rooms')
    cache.invalidate_prefix('conflicts:')
    cache.invalidate('nav:badges')


def invalidate_directory_caches():
    """Сбросить справочники преподавателей/дисциплин/групп (после правок в «Люди»)."""
    cache.invalidate('all_teachers')
    cache.invalidate('all_subjects')
    cache.invalidate('all_groups')
    cache.invalidate_prefix('schedule:')  # снапшот держит teacher_name/teacher_key
    cache.invalidate_prefix('conflicts:')
