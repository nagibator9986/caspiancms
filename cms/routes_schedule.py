# cms/routes_schedule.py — расписание: по группам, единая таблица, по преподавателям,
# конструктор, правка занятия и группы, копирование и массовые действия, история,
# экспорт (CSV/XLSX), печать, API (владелец — SCHEDULE).
#
# Правила (SPEC §2.9, §6.5, D15) и почему так:
#   • день занятия читается ТОЛЬКО через schedule_model.lesson_weekday (day_index приоритетнее
#     date — как ScheduleService.lessonWeekday в приложении);
#   • любая запись пишет И day_index (0=Пн…6=Вс), И date ('yyyy-MM-dd') синхронно; если день не
#     менялся и date уже согласована — date не трогаем (иначе функция увидит «смену даты»);
#   • редактор группы удаляет ТОЛЬКО явно удалённые занятия (deleted_ids этой группы) — раньше
#     терялись рекуррентные/воскресные занятия, которых не было в форме;
#   • занятия без изменений не переписываются (раньше каждое сохранение переписывало все пары →
#     onScheduleUpdate слал пуш на каждую);
#   • массовые записи идут с notify:false (функции не шлют пуш на каждую пару), после записи флаг
#     снимается, а CMS отправляет ОДИН пуш на группу и на преподавателя; одиночная правка и
#     удаление — пуш шлёт функция, как раньше;
#   • журнал schedule_logs: action add|update|delete (не 'create'), changed_at и timestamp —
#     Timestamp сервера; записи журнала — в тех же батчах, что и данные;
#   • чтение — из кэшированного снапшота (schedule_model.load_lessons_snapshot), а не полным
#     сканом коллекции на каждую страницу; после записи — invalidate_schedule_caches();
#   • «сегодня» и чётность недели — по Алматы (schedule_model), не по часам сервера.

import io
import re
import zipfile
from collections import Counter, OrderedDict, defaultdict
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlparse
from xml.sax.saxutils import escape as _xml_escape

from flask import Response, flash, g, jsonify, redirect, render_template, request, session, url_for

from . import conflicts as conflicts_engine
from . import schedule_model as sm
from .core import (DELETE_FIELD, SERVER_TIMESTAMP, Routes, admin_required, audit, back_url, cache, chunked,
                   csv_response, db, doc_dict, flash_action, fmt_dt, fs_opts, get_docs, humanize_ago, log, next_target,
                   now_almaty, paginate_list, plural, safe_next_url, send_push_many, set_nav_active, topic_variants,
                   url_with, view_param, where)
from .schedule_model import (CALL_SCHEDULE, DAY_NAMES_FULL, DAY_SHORT, WEEK_LABELS, WEEK_TYPES,
                             build_teacher_index, call_slots, current_week_type, day_fields, get_all_groups,
                             get_all_rooms, get_all_subjects, get_all_teachers, get_week_parity, group_sort_key,
                             intervals_overlap, invalidate_schedule_caches, lesson_weekday,
                             load_lessons_snapshot, norm_name, norm_text, normalize_lesson, parse_lesson_date,
                             parse_time_range, resolve_teacher, time_label, weeks_overlap)

routes = Routes()

# ==================== КОНСТАНТЫ ====================

# Фильтр недели (пилюля «Неделя: …»): числитель/знаменатель включают занятия «каждую неделю»
WEEK_FILTERS = [('current', 'Текущая'), ('numerator', 'Числитель'), ('denominator', 'Знаменатель')]
WEEK_OPTIONS = [('always', 'Каждую неделю'), ('numerator', 'Числитель'), ('denominator', 'Знаменатель')]
CUSTOM_TIME = '__custom__'
FIELD_LIMITS = {'groupId': 60, 'time': 40, 'subject': 120, 'teacher': 120, 'room': 40}
LESSON_FIELDS = ('groupId', 'time', 'subject', 'teacher', 'room', 'week_type')
MAX_BATCH_OPS = 450          # лимит Firestore — 500 операций на батч
MAX_EDITOR_LESSONS = 300     # защита от гигантских запросов редактора
CLEAR_NOTIFY_AFTER_WRITE = True  # снимать notify:false после массовой записи (см. ScheduleBatch)
GROUPS_PER_PAGE = 48
TEACHERS_PER_PAGE = 24
TABLE_GROUPS_PER_PAGE = 30
LOGS_PER_PAGE = 30
LOG_FETCH_LIMIT = 500        # функция cleanupScheduleLogs держит журнал коротким — читаем хвост целиком


# ==================== ОБЩИЕ ПОМОЩНИКИ ====================

def _today():
    """Сегодня по Алматы (через schedule_model — тесты подменяют его today_almaty)."""
    return sm.today_almaty()


def _now_minutes():
    n = now_almaty()
    return n.hour * 60 + n.minute


def _clip(value, limit):
    return str(value if value is not None else '').strip()[:limit]


_MAJOR_RE = re.compile(r'^\s*([^\W\d_]+)', re.UNICODE)
_YEAR_RE = re.compile(r'(\d{2,4})\s*-\s*\d+')


def group_major(name):
    """Направление — буквы в начале названия группы: 'ПО 24-1' → 'ПО'."""
    m = _MAJOR_RE.match(str(name or ''))
    return m.group(1).upper() if m else ''


def group_course(name, today=None):
    """Курс по году поступления в названии: 'ПО 24-1' в 2026/27 учебном году → 3."""
    m = _YEAR_RE.search(str(name or ''))
    if not m:
        return None
    year = int(m.group(1))
    if year < 100:
        year += 2000
    today = today or _today()
    start = today.year if today.month >= 9 else today.year - 1
    return min(10, max(1, start - year + 1))


def week_param():
    """Фильтр недели из ?week= (старое имя — ?week_type=): '' | current | numerator | denominator | always."""
    v = (request.args.get('week') or request.args.get('week_type') or '').strip()
    return v if v in ('current', 'numerator', 'denominator', 'always') else ''


def week_matches(week, flt, cur):
    """Как applyFilter в APP: числитель/знаменатель включают «каждую неделю»."""
    if not flt:
        return True
    if flt == 'always':
        return week == 'always'
    target = cur if flt == 'current' else flt
    return week in ('always', target)


def day_param(name='day'):
    v = request.args.get(name, '')
    try:
        d = int(v)
    except (TypeError, ValueError):
        return None
    return d if 0 <= d <= 6 else None


def short_name(full):
    """'Петров Дмитрий Игоревич' → 'Петров Д. И.'; одно слово/uid — как есть."""
    parts = str(full or '').split()
    if len(parts) < 2:
        return str(full or '')
    return parts[0] + ' ' + ' '.join(p[:1] + '.' for p in parts[1:3])


def lesson_time(lesson):
    """Время занятия для показа: нормализованное 'ЧЧ:ММ–ЧЧ:ММ' или как записано."""
    if lesson.get('start') is not None and lesson.get('end') is not None and lesson.get('time_ok', True):
        return lesson.get('time_label') or time_label(lesson.get('start'), lesson.get('end'))
    return lesson.get('time') or '—'


def teacher_label(lesson):
    return lesson.get('teacher_name') or lesson.get('teacher') or ''


def lesson_is_current(lesson, cur):
    return lesson.get('week') in ('always', cur)


def _teacher_hint(lesson):
    """Пометка о качестве ссылки на преподавателя (для показа рядом с ФИО)."""
    flags = lesson.get('teacher_flags') or []
    if 'ambiguous' in flags:
        return 'однофамильцы'
    if 'unknown_name' in flags:
        return 'нет в справочнике'
    if 'orphan' in flags:
        return 'uid не найден'
    if 'legacy' in flags:
        return 'старый справочник'
    if 'by_name' in flags:
        return 'записано ФИО'
    return ''


@routes.template_filter('sch_time')
def _f_time(lesson):
    return lesson_time(lesson)


@routes.template_filter('sch_teacher')
def _f_teacher(lesson):
    return teacher_label(lesson)


@routes.template_filter('sch_short')
def _f_short(name):
    return short_name(name)


@routes.template_filter('sch_teacher_hint')
def _f_teacher_hint(lesson):
    return _teacher_hint(lesson)


# Старые фильтры (могли использоваться в шаблонах других разделов) — сохранены.
@routes.template_filter('extract_course')
def extract_course_filter(group_name):
    """Курс по названию группы (ИС-21 → 2) — старое поведение."""
    match = re.search(r'-(\d)', str(group_name))
    return match.group(1) if match else ''


@routes.template_filter('extract_major')
def extract_major_filter(group_name):
    """Направление из названия группы (ИС-21 → ИС)."""
    return group_major(group_name)


@routes.template_filter('sum_lessons')
def sum_lessons_filter(days_data):
    """Общее количество занятий в {день: [занятия]}."""
    return sum(len(v) for v in (days_data or {}).values())


def week_context():
    """Текущая неделя для шапок страниц."""
    cur = current_week_type()
    today = _today()
    return {'current_week': cur, 'current_week_label': WEEK_LABELS[cur],
            'week_number': sm.academic_week_number(today), 'today_wd': today.weekday(),
            'today_label': '%s, %s' % (DAY_NAMES_FULL[today.weekday()], today.strftime('%d.%m.%Y')),
            'is_even_week': cur == 'numerator'}  # старое имя: True = ЧИСЛИТЕЛЬ


def focus_day(columns, today_wd):
    """День, который переключатель дня на телефоне показывает первым: сегодня, в воскресенье
    (если воскресной колонки нет) — понедельник, иначе первая колонка."""
    cols = [c for c in columns if c is not None]
    if today_wd in cols:
        return today_wd
    if 0 in cols:
        return 0
    return cols[0] if cols else None


_SEV_RANK = {'critical': 0, 'warning': 1}


def conflict_marks(with_status=False):
    """Накладки из движка (cms.conflicts) для подсветки там, где правят расписание.

    → (marks, by_group, by_teacher) (+ engine_ok при with_status=True — движок отработал; иначе страницы
    подсвечивают пересечения запасным _overlap_ids, который про принятые накладки не знает):
      marks      — {id занятия: {'severity': худшая, 'items': ['Преподаватель занят: ВТ 24-1 13:00–13:50', …]}};
      by_group   — {группа: {'critical': n, 'warning': n, 'dims': Counter(dim)}} — уникальные накладки;
      by_teacher — {ключ преподавателя: {'critical': n, 'warning': n}} (вкладка «Преподаватели»).
    Без «инфо» (потоки) и без принятых. Движок упал — пусто (страница работает)."""
    try:
        analysis = conflicts_engine.get_analysis()
    except Exception:
        log.exception('Накладки для подсветки не посчитаны')
        return ({}, {}, {}, False) if with_status else ({}, {}, {})
    try:
        acks = conflicts_engine.load_acks() or {}
    except Exception:
        log.warning('Принятые накладки недоступны', exc_info=True)
        acks = {}
    marks = {}
    groups = defaultdict(dict)
    group_dims = defaultdict(Counter)
    teachers = defaultdict(dict)
    for c in analysis.all_clusters():
        sev = c['severity']
        if sev not in _SEV_RANK or c['hash'] in acks:
            continue
        lessons = {l['id']: l for l in c['lessons']}
        for lid in c['ids']:
            others = ', '.join('%s %s' % (o['groupId'] or '—', o['time_label'] or o['time'])
                               for oid, o in lessons.items() if oid != lid)
            m = marks.setdefault(lid, {'severity': sev, 'items': []})
            if _SEV_RANK[sev] < _SEV_RANK[m['severity']]:
                m['severity'] = sev
            text = c['title'] + (': ' + others if others else '')
            if text not in m['items']:
                m['items'].append(text)
        for gname in c['groups']:
            prev = groups[gname].get(c['hash'])
            if prev is None or _SEV_RANK[sev] < _SEV_RANK[prev]:
                groups[gname][c['hash']] = sev
            group_dims[gname][c['dim']] += 1
        if c['dim'] == 'teacher':
            teachers[c['key']][c['hash']] = sev
    by_group = {g: dict(Counter(v.values()), dims=group_dims[g]) for g, v in groups.items()}
    by_teacher = {k: dict(Counter(v.values())) for k, v in teachers.items()}
    return (marks, by_group, by_teacher, True) if with_status else (marks, by_group, by_teacher)


def group_conflicts_url(gname, info):
    """Ссылка на «Накладки» с фильтром по группе — на вкладку, где их больше всего."""
    dims = (info or {}).get('dims') or Counter()
    dim = dims.most_common(1)[0][0] if dims else 'teacher'
    return _schedule_conflicts_url(dim=dim, group=gname)


def _redirect_next(default):
    """Редирект на ?next= / поле next (только свой сайт), иначе default."""
    nxt = safe_next_url(request.form.get('next') or request.args.get('next'))
    return redirect(nxt or default)


# ==================== СПРАВОЧНИКИ ДЛЯ ФОРМ ====================

def teacher_choices(current=None):
    """[(значение, подпись)] для select преподавателя; текущее значение сохраняется, даже если
    его нет в справочнике (ФИО текстом, удалённый uid) — форма не подменит его первым вариантом."""
    teachers = get_all_teachers()
    index = build_teacher_index()
    out = [('', 'Без преподавателя')]
    for uid, name in teachers.items():
        out.append((uid, '%s (старый справочник)' % name if uid in index.legacy and uid not in index.by_uid else name))
    cur = str(current or '').strip()
    if cur and cur not in teachers:
        res = resolve_teacher(cur, index)
        hint = {'by_name': 'записано ФИО текстом', 'ambiguous': 'однофамильцы — выберите из списка',
                'orphan': 'нет в справочнике', 'unknown_name': 'нет в справочнике'}
        note = next((hint[f] for f in ('ambiguous', 'by_name', 'orphan', 'unknown_name') if f in res['flags']), '')
        out.insert(1, (cur, '%s (%s)' % (cur, note) if note else cur))
    return out


def subject_choices(current=None):
    subjects = list(get_all_subjects())
    out = [(s, s) for s in subjects]
    cur = str(current or '').strip()
    if cur and cur not in subjects:
        out.insert(0, (cur, '%s (нет в справочнике)' % cur))
    return out


def group_choices(current=None):
    groups = list(get_all_groups())
    cur = str(current or '').strip()
    if cur and cur not in groups:
        groups.insert(0, cur)
    return groups


def time_choices(weekday, current=None):
    """[(значение, подпись)] слотов сетки звонков дня + текущее время (если не из сетки) + «Другое…»."""
    slots = call_slots(weekday if weekday is not None else 1)
    cur = str(current or '').strip()
    cur_range = parse_time_range(cur) if cur else (None, None)
    out, matched = [], cur in [label for label, _s, _e in slots]
    for label, start, end in slots:
        value = label
        # '10:25-11:15' (так пишет приложение) = слот '10.25-11.15': оставляем исходную строку,
        # чтобы сохранение без правок не переписало время
        if not matched and cur and (start, end) == cur_range:
            value, matched = cur, True
        out.append((value, label.replace('.', ':').replace('-', '–')))
    if cur and not matched and cur != CUSTOM_TIME:
        out.insert(0, (cur, '%s (не из сетки)' % cur))
    out.append((CUSTOM_TIME, 'Другое время…'))
    return out


def week_choices(current=None):
    out = list(WEEK_OPTIONS)
    cur = str(current or '').strip()
    if cur and cur not in WEEK_TYPES:
        out.append((cur, '%s (неизвестный тип)' % cur))
    return out


def form_context():
    """Справочники для конструктора/редактора (кэшированные) и функции подготовки списков."""
    return {
        'call_schedule': CALL_SCHEDULE,
        'day_names_full': DAY_NAMES_FULL,
        'day_short': DAY_SHORT,
        'week_options': WEEK_OPTIONS,
        'all_rooms': get_all_rooms(),
        'custom_time': CUSTOM_TIME,
        'teacher_choices': teacher_choices,
        'subject_choices': subject_choices,
        'time_choices': time_choices,
        'week_choices': week_choices,
        'group_choices': group_choices,
    }


def _known_group(group):
    """Группа есть в справочнике (кэш) или в groups_list (свежая проверка, если кэш устарел)."""
    if not group:
        return False
    if group in get_all_groups():
        return True
    try:
        return db.collection('groups_list').document(group).get(**fs_opts()).exists
    except Exception:
        log.exception('Проверка группы %s не удалась', group)
        return False


# ==================== ЖУРНАЛ ИЗМЕНЕНИЙ ====================

def _is_sentinel(value):
    return value is SERVER_TIMESTAMP or value is DELETE_FIELD


def _log_payload(data):
    """Снимок занятия для журнала: без служебного notify и без sentinel-значений Firestore."""
    if not data:
        return {}
    return {k: v for k, v in dict(data).items() if k not in ('notify', 'id') and not _is_sentinel(v)}


def _log_has_changes(old, new):
    for field, value in (new or {}).items():
        if field in ('updatedAt', 'createdAt', 'notify'):
            continue
        if (old or {}).get(field) != value:
            return True
    return False


class ScheduleLogger:
    """Журнал изменений расписания (schedule_logs).

    Формат записи (совместим с приложением: оно читает журнал по changed_at и group_id):
      {action: 'add'|'update'|'delete', schedule_id, group_id, old_data, new_data,
       changed_by: uid, changed_by_email, changed_at: Timestamp, timestamp: Timestamp, source: 'cms'}.
    Раньше action был 'create', а timestamp — числом (функция cleanupScheduleLogs сортирует по
    timestamp — смешанные типы ломали порядок).

    Очередь — на запрос (flask.g), а не на процесс: если запись данных упала, её логи не
    запишутся следующим запросом (так было раньше). Новый код кладёт записи журнала прямо в
    батч с данными (entry() + ScheduleBatch); add()/flush() оставлены для совместимости."""

    def entry(self, action, schedule_id, old_data, new_data, group_id=None):
        """Готовый документ журнала или None (update без реальных изменений не пишется)."""
        action = {'create': 'add'}.get(action, action)
        old_p, new_p = _log_payload(old_data), _log_payload(new_data)
        if action == 'update' and old_data and new_data and not _log_has_changes(old_p, new_p):
            return None
        if group_id is None:
            group_id = (new_p or old_p).get('groupId')
        return {
            'action': action,
            'schedule_id': schedule_id,
            'group_id': group_id or None,
            'old_data': old_p,
            'new_data': new_p,
            'changed_by': session.get('uid') or 'unknown',
            'changed_by_email': session.get('email') or None,
            'changed_at': SERVER_TIMESTAMP,
            'timestamp': SERVER_TIMESTAMP,
            'source': 'cms',
        }

    def _queue(self):
        try:
            if not hasattr(g, '_schedule_logs'):
                g._schedule_logs = []
            return g._schedule_logs
        except RuntimeError:  # вне контекста приложения
            if not hasattr(self, '_fallback'):
                self._fallback = []
            return self._fallback

    def add(self, action, schedule_id, old_data, new_data, group_id=None):
        e = self.entry(action, schedule_id, old_data, new_data, group_id)
        if e:
            self._queue().append(e)

    def discard(self):
        del self._queue()[:]

    def flush(self):
        """Записать накопленные записи журнала (ошибка — в лог, не наружу)."""
        queue = self._queue()
        entries = list(queue)
        del queue[:]
        if not entries:
            return
        try:
            for part in chunked(entries, MAX_BATCH_OPS):
                batch = db.batch()
                for e in part:
                    batch.set(db.collection('schedule_logs').document(), e)
                batch.commit()
        except Exception:
            log.exception('Не удалось записать журнал расписания (%d записей)', len(entries))
        cache.invalidate('schedule:logs')


schedule_logger = ScheduleLogger()


def log_schedule_change(action, schedule_id, old_data, new_data, group_id=None):
    """Совместимость со старым кодом: запись попадёт в журнал при schedule_logger.flush()."""
    schedule_logger.add(action, schedule_id, old_data, new_data, group_id)


# ==================== ПАКЕТНАЯ ЗАПИСЬ ====================

def _teacher_uid(raw, index):
    """uid преподавателя из справочника (для пуша в его топик) или None."""
    res = resolve_teacher(raw, index)
    if res['uid'] and not ({'orphan', 'unknown_name'} & set(res['flags'])):
        return res['uid']
    return None


def _summary(counter):
    parts = []
    for key, verb in (('add', 'добавлено'), ('update', 'изменено'), ('delete', 'удалено')):
        n = counter.get(key, 0)
        if n:
            parts.append('%s %d %s' % (verb, n, plural(n, 'занятие', 'занятия', 'занятий')))
    return ', '.join(parts)


STALE_ROW_TEXT = 'Занятие изменено или удалено другим пользователем — обновите страницу'


class PartialCommitError(RuntimeError):
    """Массовая запись прервалась посередине: часть порций уже в Firestore, остальное — нет."""


class ScheduleBatch:
    """Массовая запись в schedules: данные и журнал — в одних батчах (≤ MAX_BATCH_OPS операций).

    quiet=True (массовые операции, D15): все записи с notify:false — функции onSchedule* не шлют
    пуш на каждую пару; удаление в две фазы (пометка notify:false → удаление: функция удаления
    смотрит на документ ДО удаления); после записи флаг снимается (CLEAR_NOTIFY_AFTER_WRITE —
    иначе последующие правки из приложения тоже остались бы без пушей; функция обновления
    пропускает такую правку: предмет/время/кабинет/дата/преподаватель не менялись) и CMS шлёт
    ОДИН пуш на группу и на каждого затронутого преподавателя.
    quiet=False — без notify:false и без своих пушей (их пошлют функции)."""

    def __init__(self, quiet=True):
        self.quiet = quiet
        self._writes = []      # (kind, ref, data) — set / update
        self._deletes = []     # ref
        self._logs = []
        self.ids = {'add': [], 'update': [], 'delete': []}
        self.groups = defaultdict(Counter)
        self.teachers = defaultdict(Counter)
        self.teacher_groups = defaultdict(set)
        self._index = None

    def __len__(self):
        return len(self._writes) + len(self._deletes)

    @property
    def counts(self):
        return {k: len(v) for k, v in self.ids.items()}

    def _track(self, action, old, new):
        if self._index is None:
            self._index = build_teacher_index()
        groups = {d.get('groupId') for d in (old, new) if d and d.get('groupId')}
        for gname in groups:
            self.groups[gname][action] += 1
        uids = set()
        for d in (old, new):
            if d:
                uid = _teacher_uid(d.get('teacher') or d.get('teacherId'), self._index)
                if uid:
                    uids.add(uid)
                    if d.get('groupId'):
                        self.teacher_groups[uid].add(d.get('groupId'))
        for uid in uids:
            self.teachers[uid][action] += 1

    def add(self, data):
        ref = db.collection('schedules').document()
        doc = dict(data)
        doc.setdefault('createdAt', SERVER_TIMESTAMP)
        doc['updatedAt'] = SERVER_TIMESTAMP
        if self.quiet:
            doc['notify'] = False
        self._writes.append(('set', ref, doc))
        e = schedule_logger.entry('add', ref.id, None, doc, doc.get('groupId'))
        if e:
            self._logs.append(e)
        self.ids['add'].append(ref.id)
        self._track('add', None, doc)
        return ref.id

    def update(self, doc_id, old, fields):
        ref = db.collection('schedules').document(doc_id)
        upd = dict(fields)
        upd['updatedAt'] = SERVER_TIMESTAMP
        if self.quiet:
            upd['notify'] = False
        self._writes.append(('update', ref, upd))
        merged = _merge(old, upd)
        e = schedule_logger.entry('update', doc_id, old, merged, merged.get('groupId'))
        if e:
            self._logs.append(e)
        self.ids['update'].append(doc_id)
        self._track('update', old, merged)

    def delete(self, doc_id, old):
        ref = db.collection('schedules').document(doc_id)
        if self.quiet:
            self._writes.append(('mark', ref, {'notify': False}))
        self._deletes.append(ref)
        e = schedule_logger.entry('delete', doc_id, old, None, (old or {}).get('groupId'))
        if e:
            self._logs.append(e)
        self.ids['delete'].append(doc_id)
        self._track('delete', old, None)

    @staticmethod
    def _commit_ops(ops):
        for part in chunked(ops, MAX_BATCH_OPS):
            batch = db.batch()
            for kind, ref, data in part:
                if kind == 'set':
                    batch.set(ref, data)
                elif kind in ('update', 'mark'):
                    batch.update(ref, data)
                else:
                    batch.delete(ref)
            batch.commit()

    def commit(self, notify_users=True):
        """Записать всё. → counts. Сбой до первой записанной порции — исключение Firestore как есть
        (ничего не записано); сбой позже — PartialCommitError (часть уже в базе), при этом notify:false
        снимается с того, что успело записаться, иначе функции молча пропускали бы все будущие правки этих пар."""
        if not len(self):
            return self.counts
        logs_coll = db.collection('schedule_logs')
        ops = list(self._writes) + [('set', logs_coll.document(), e) for e in self._logs]
        phases = [ops] + ([[('delete', ref, None) for ref in self._deletes]] if self._deletes else [])
        committed = 0
        try:
            for phase in phases:
                for part in chunked(phase, MAX_BATCH_OPS):
                    self._commit_ops(part)
                    committed += 1
        except Exception as e:
            if not committed:
                raise
            if self.quiet:
                self._heal_notify(self._notify_refs(deleted_too=True))
            raise PartialCommitError(str(e)) from e
        finally:
            invalidate_schedule_caches()
        if self.quiet and CLEAR_NOTIFY_AFTER_WRITE:
            written = self._notify_refs()
            try:
                self._commit_ops([('update', ref, {'notify': DELETE_FIELD}) for ref in written])
            except Exception:
                log.exception('Не удалось снять notify:false с %d занятий пачкой — снимаем по одному', len(written))
                self._heal_notify(written)
        if self.quiet and notify_users:
            self.send_pushes()
        return self.counts

    def _notify_refs(self, deleted_too=False):
        """Ссылки, на которые писался notify:false: записанные (set/update) и — после сбоя — помеченные
        к удалению (удаление могло не пройти)."""
        kinds = ('set', 'update', 'mark') if deleted_too else ('set', 'update')
        return [ref for kind, ref, _d in self._writes if kind in kinds]

    @staticmethod
    def _heal_notify(refs):
        """Снять notify:false по одному документу (батч упал бы целиком на уже удалённом/не созданном).
        NotFound — документа нет (не создан или удалён) — пропускаем; другая ошибка — база недоступна,
        дальше не пытаемся (лог)."""
        healed = 0
        for ref in refs:
            try:
                ref.update({'notify': DELETE_FIELD})
                healed += 1
            except Exception as e:
                if e.__class__.__name__ == 'NotFound':
                    continue
                log.exception('notify:false не снят с %s (и остальных %d)', ref.id, len(refs) - healed)
                return healed
        return healed

    def send_pushes(self):
        """Один пуш на группу и на преподавателя (D15) — все одной пачкой (send_push_many). Группе с «ъ/ь»
        в названии — в оба топика: функций и приложения (они расходятся, см. core.topic_variants)."""
        messages = []
        for gname, counter in self.groups.items():
            text = _summary(counter)
            if text:
                for topic in topic_variants('schedule_group_%s' % gname):
                    messages.append({'title': 'Изменения в расписании', 'body': 'Группа %s: %s.' % (gname, text),
                                     'topic': topic, 'data': {'type': 'schedule', 'group': gname}})
        for uid, counter in self.teachers.items():
            text = _summary(counter)
            if text:
                groups = ', '.join(sorted(self.teacher_groups.get(uid) or [], key=group_sort_key))
                body = text[:1].upper() + text[1:] + (' (%s)' % groups if groups else '') + '.'
                messages.append({'title': 'Ваше расписание изменилось', 'body': body,
                                 'topic': 'schedule_teacher_%s' % uid, 'data': {'type': 'schedule'}})
        send_push_many(messages)


def _merge(old, upd):
    """Документ после update(): старый + изменения (DELETE_FIELD удаляет поле)."""
    merged = dict(old or {})
    for k, v in upd.items():
        if v is DELETE_FIELD:
            merged.pop(k, None)
        else:
            merged[k] = v
    return merged


# ==================== ВВОД ЗАНЯТИЯ И СРАВНЕНИЕ ====================

def _parse_day(value):
    try:
        d = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return d if 0 <= d <= 6 else None


def _time_error(value):
    start, end = parse_time_range(value)
    if start is None or end is None:
        return 'Время укажите в формате ЧЧ.ММ-ЧЧ.ММ (например, 08.30-09.50)'
    if end <= start:
        return 'Конец занятия должен быть позже начала'
    return None


def _old_value(old, field):
    if field == 'teacher':
        return str((old or {}).get('teacher') or (old or {}).get('teacherId') or '').strip()
    if field == 'week_type':
        return str((old or {}).get('week_type') or 'always').strip()
    return str((old or {}).get(field) or '').strip()


def lesson_from_input(src, old=None, group=None):
    """Ввод формы/JSON → (данные, ошибки). Данные: groupId, day (0..6|None), time, subject,
    teacher, room, week_type. Время проверяется, только если его поменяли (старое «после обеда»
    не мешает сохранить правку кабинета); неизвестный тип недели сохраняется, только если не менялся."""
    src = src or {}
    errors = []
    grp = _clip(group if group is not None else src.get('groupId', src.get('group')), FIELD_LIMITS['groupId'])
    day = _parse_day(src.get('day', src.get('day_of_week', src.get('day_index'))))
    if day is None and src.get('date'):  # старая форма правки присылала дату
        d = parse_lesson_date(src.get('date'))
        day = d.weekday() if d else None
    time = _clip(src.get('time'), FIELD_LIMITS['time'])
    if time == CUSTOM_TIME:
        time = _clip(src.get('time_custom'), FIELD_LIMITS['time'])
    subject = _clip(src.get('subject'), FIELD_LIMITS['subject'])
    teacher = _clip(src.get('teacher'), FIELD_LIMITS['teacher'])
    room = _clip(src.get('room'), FIELD_LIMITS['room'])
    week = _clip(src.get('week_type'), 20) or 'always'
    if week not in WEEK_TYPES and not (old is not None and week == _old_value(old, 'week_type')):
        week = 'always'
    if not grp:
        errors.append('Выберите группу')
    if day is None:
        errors.append('Выберите день недели')
    if not time:
        errors.append('Укажите время')
    elif old is None or time != _old_value(old, 'time'):
        err = _time_error(time)
        if err:
            errors.append(err)
    if not subject:
        errors.append('Укажите дисциплину')
    return {'groupId': grp, 'day': day, 'time': time, 'subject': subject, 'teacher': teacher,
            'room': room, 'week_type': week}, errors


def lesson_doc(data):
    """Новый документ schedules из ввода (без служебных полей): day_index + date синхронно."""
    doc = {f: data[f] for f in LESSON_FIELDS}
    doc.update(day_fields(data['day']))
    return doc


_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def _day_updates(old, weekday):
    """Поля дня при правке: день сменился → day_index + новая date; не сменился → дописать
    недостающее/несогласованное (day_index, date), не трогая уже верную date."""
    if lesson_weekday(old) != weekday:
        return day_fields(weekday)
    out = {}
    di = old.get('day_index')
    if not (isinstance(di, int) and not isinstance(di, bool) and di == weekday):
        out['day_index'] = weekday
    raw = old.get('date')
    d = parse_lesson_date(raw) if isinstance(raw, str) and _ISO_DATE_RE.match(raw.strip()) else None
    if d is None or d.weekday() != weekday:
        out['date'] = day_fields(weekday)['date']
    return out


def _with_day(old, fields):
    """Изменения + согласованные day_index/date («любая запись пишет оба поля»)."""
    wd = lesson_weekday(old)
    out = dict(fields)
    if wd is not None:
        out.update(_day_updates(old, wd))
    return out


def plan_update(old, new, only=None):
    """Поля для update() или {} — если ничего не поменялось.

    only — множество полей, которые менял пользователь ('day' + LESSON_FIELDS); остальные не
    пишем, даже если в базе они уже другие (кто-то поменял их параллельно — не затираем)."""
    def consider(f):
        return only is None or f in only

    fields = {f: new[f] for f in LESSON_FIELDS if consider(f) and _old_value(old, f) != new[f]}
    old_wd = lesson_weekday(old)
    target_wd = new['day'] if (consider('day') or old_wd is None) else old_wd
    if not fields and target_wd == old_wd:
        return {}
    fields.update(_day_updates(old, target_wd))
    if 'teacher' in fields and 'teacherId' in old:
        fields['teacherId'] = DELETE_FIELD  # функции берут teacherId раньше teacher
    return fields


def _orig_fields(orig, new):
    """Какие поля менял пользователь: сравнение с исходными значениями строки редактора."""
    if not isinstance(orig, dict):
        return None
    changed = set()
    for f in LESSON_FIELDS:
        if f == 'groupId':
            continue
        o = orig.get(f)
        o = str(o if o is not None else ('always' if f == 'week_type' else '')).strip()
        if o != new[f]:
            changed.add(f)
    if _parse_day(orig.get('day')) != new['day']:
        changed.add('day')
    return changed


# ==================== ПРОВЕРКА НАКЛАДОК (cms.conflicts) ====================

_CONFLICT_KINDS = {
    'teacher': 'Преподаватель уже занят', 'double_booking': 'Преподаватель уже занят',
    'room': 'Аудитория занята', 'room_double_booking': 'Аудитория занята',
    'group': 'У группы уже есть пара', 'group_overlap': 'У группы уже есть пара',
    'subgroups': 'Возможно, подгруппы', 'subgroups?': 'Возможно, подгруппы',
    'duplicate': 'Дубль занятия', 'stream': 'Поток (объединённая пара)',
}
_SEVERITIES = ('critical', 'warning', 'info')


def _conflict_ids(c):
    ids = []
    for key in ('other_id', 'with_id', 'lesson_id'):
        if c.get(key):
            ids.append(str(c[key]))
    for key in ('other', 'with', 'lesson'):
        v = c.get(key)
        if isinstance(v, dict) and v.get('id'):
            ids.append(str(v['id']))
    for v in c.get('lessons') or []:
        if isinstance(v, dict) and v.get('id'):
            ids.append(str(v['id']))
    for v in c.get('ids') or []:
        ids.append(str(v))
    return ids


def describe_conflict(c):
    """Элемент из conflicts.check_lesson → {'severity', 'text', 'ids'} (формат движка не навязываем:
    берём message/text/title, иначе собираем из kind + «другого» занятия)."""
    if isinstance(c, str):
        return {'severity': 'warning', 'text': c[:300], 'ids': []}
    if not isinstance(c, dict):
        return {'severity': 'warning', 'text': str(c)[:300], 'ids': []}
    sev = str(c.get('severity') or c.get('level') or 'warning')
    text = c.get('message') or c.get('text') or c.get('title') or c.get('label') or c.get('description')
    if not text:
        kind = c.get('kind') or c.get('type') or c.get('dim')
        kinds = c.get('kinds') if isinstance(c.get('kinds'), (list, tuple)) else []
        head = _CONFLICT_KINDS.get(kind) or next((_CONFLICT_KINDS[k] for k in kinds if k in _CONFLICT_KINDS), 'Накладка')
        other = next((c.get(k) for k in ('other', 'with', 'lesson') if isinstance(c.get(k), dict)), None)
        if other is None:
            others = [x for x in (c.get('lessons') or []) if isinstance(x, dict)]
            other = others[-1] if others else {}
        bits = [other.get('groupId'), other.get('subject'), other.get('time_label') or other.get('time'),
                other.get('teacher_name') or other.get('teacher_display'), other.get('room')]
        detail = ' · '.join(str(b) for b in bits if b)
        text = head + (': ' + detail if detail else '')
    return {'severity': sev if sev in _SEVERITIES else 'warning', 'text': str(text)[:300], 'ids': _conflict_ids(c)}


def check_conflicts(doc, exclude_id=None, extra_exclude=()):
    """Накладки занятия через cms.conflicts.check_lesson (движок CONFLICTS). Падение движка не
    ломает сохранение — просто без предупреждений. extra_exclude — id, конфликты только с которыми
    не показываем (занятия этой же группы, которые сейчас редактируются в форме)."""
    try:
        raw = conflicts_engine.check_lesson(doc, exclude_id=exclude_id) or []
    except Exception:
        log.exception('conflicts.check_lesson упал')
        return []
    extra = set(extra_exclude or ())
    out = []
    for c in raw:
        item = describe_conflict(c)
        if item['ids'] and extra and set(item['ids']) <= extra:
            continue
        out.append(item)
    order = {s: i for i, s in enumerate(_SEVERITIES)}
    out.sort(key=lambda x: order.get(x['severity'], 9))
    return out[:10]


def _flash_conflicts(found, prefix='Проверьте накладки'):
    """found: [(подпись занятия, [конфликты])] → одно предупреждение (до 5 строк)."""
    lines = []
    for label, items in found:
        for it in items:
            lines.append(('%s — %s' % (label, it['text'])) if label else it['text'])
    if not lines:
        return
    text = '%s: %s' % (prefix, '; '.join(lines[:5]))
    if len(lines) > 5:
        text += ' (и ещё %d)' % (len(lines) - 5)
    flash(text, 'warning')


def _lesson_label(data):
    day = data.get('day')
    if day is None:
        day = lesson_weekday(data)
    return ' '.join(x for x in (DAY_SHORT[day] if day is not None else '', data.get('time') or '',
                                data.get('subject') or '') if x)


# ==================== СНАПШОТ → ПРЕДСТАВЛЕНИЯ ====================

def _group_stats(lessons, cur):
    days = Counter(l['weekday'] for l in lessons if l['weekday'] is not None)
    teachers = {l['teacher_key'] for l in lessons if l['teacher_key']}
    rooms = {l['room_key'] for l in lessons if l['room_key']}
    per_week = {w: sum(1 for l in lessons if l['week'] in ('always', w)) for w in ('numerator', 'denominator')}
    return {'lessons': len(lessons), 'days': days, 'teachers': len(teachers), 'rooms': len(rooms),
            'num': per_week['numerator'], 'den': per_week['denominator'], 'current': per_week.get(cur, 0),
            'issues': sum(1 for l in lessons if l['weekday'] is None or not l['time_ok'])}


def _filter_groups(groups, q='', course='', major=''):
    qn = norm_text(q)
    out = []
    for gname in groups:
        if qn and qn not in norm_text(gname):
            continue
        if course and str(group_course(gname) or '') != str(course):
            continue
        if major and group_major(gname) != major.upper():
            continue
        out.append(gname)
    return out


def _day_columns(lessons):
    """Колонки дней: Пн–Сб всегда, Вс — если есть занятия, «Без дня» (None) — если есть."""
    cols = list(range(6))
    if any(l['weekday'] == 6 for l in lessons):
        cols.append(6)
    if any(l['weekday'] is None for l in lessons):
        cols.append(None)
    return cols


def _by_day(lessons):
    out = OrderedDict()
    for l in sorted(lessons, key=sm.lesson_sort_key):
        out.setdefault(l['weekday'], []).append(l)
    return out


def _overlap_ids(lessons):
    """id пересекающихся занятий одного человека/группы (без потоков) — для подсветки; полный
    анализ (кластеры, критичность, качество данных) — раздел «Накладки» (cms.conflicts)."""
    ids, pairs = set(), 0
    by_day = defaultdict(list)
    for l in lessons:
        if l['weekday'] is not None and l['time_ok']:
            by_day[l['weekday']].append(l)
    for day_lessons in by_day.values():
        day_lessons.sort(key=lambda x: (x['start'], x['end']))
        for i, a in enumerate(day_lessons):
            for b in day_lessons[i + 1:]:
                if b['start'] >= a['end']:
                    break
                if not intervals_overlap(a['start'], a['end'], b['start'], b['end']):
                    continue
                if not weeks_overlap(a['week'], b['week']):
                    continue
                same_slot = a['start'] == b['start'] and a['end'] == b['end']
                if same_slot and a['room_key'] and a['room_key'] == b['room_key'] and \
                        norm_text(a['subject']) == norm_text(b['subject']) and a['groupId'] != b['groupId']:
                    continue  # поток: один преподаватель, аудитория и дисциплина у нескольких групп
                ids.update((a['id'], b['id']))
                pairs += 1
    return ids, pairs


def _lesson_json(l):
    """Нормализованное занятие → JSON для API и скриптов страниц."""
    return {
        'id': l['id'], 'groupId': l['groupId'], 'subject': l['subject'], 'teacher': l['teacher'],
        'teacher_key': l['teacher_key'], 'teacher_name': teacher_label(l), 'room': l['room'],
        'time': l['time'], 'time_label': lesson_time(l), 'start': l['start'], 'end': l['end'],
        'week_type': l['week'], 'day_index': l['weekday'], 'day_name': l['day_name'], 'date': l['date'],
        'kind': l['kind'],
    }


def _schedule_conflicts_url(**kw):
    """Ссылка на раздел «Накладки» (модуль CONFLICTS), если он подключён."""
    try:
        return url_for('schedule_conflicts', **kw)
    except Exception:
        return None


# ==================== РАСПИСАНИЕ ГРУПП ====================

@routes.route('/schedule')
@admin_required
def schedule():
    """По группам: без ?group — карточки групп с поиском и фильтрами; с ?group — неделя группы
    (сетка или список с массовыми действиями), история, копирование, экспорт, печать."""
    wk = week_context()
    cur = wk['current_week']
    snap = load_lessons_snapshot()
    all_groups = get_all_groups()
    selected_group = (request.args.get('group') or '').strip()
    q = (request.args.get('q') or '').strip()
    course = (request.args.get('course') or '').strip()
    major = (request.args.get('major') or '').strip()
    day = day_param()
    week = week_param()
    majors = sorted({group_major(x) for x in all_groups if group_major(x)})
    courses = sorted({group_course(x) for x in all_groups if group_course(x)})
    common = dict(wk, all_groups=all_groups, majors=majors, courses=courses, q=q, course=course, major=major,
                  day=day, week=week, week_filters=WEEK_FILTERS, day_names=DAY_NAMES_FULL, day_short=DAY_SHORT,
                  day_options=[(d, DAY_NAMES_FULL[d]) for d in range(7)], selected_group=selected_group)

    marks, conf_groups, _conf_teachers, engine_ok = conflict_marks(with_status=True)
    if not selected_group:
        groups = _filter_groups(all_groups, q, course, major)
        cards = []
        totals = Counter()
        for gname in groups:
            lessons = [l for l in snap.by_group.get(gname, []) if week_matches(l['week'], week, cur)
                       and (day is None or l['weekday'] == day)]
            st = _group_stats(lessons, cur)
            totals['lessons'] += st['lessons']
            totals['num'] += st['num']
            totals['den'] += st['den']
            totals['empty'] += 0 if st['lessons'] else 1
            info = conf_groups.get(gname) or {}
            n_conf = info.get('critical', 0) + info.get('warning', 0)
            totals['conflict_groups'] += 1 if info.get('critical') else 0
            cards.append({'name': gname, 'course': group_course(gname), 'major': group_major(gname), 'stats': st,
                          'conflicts': n_conf, 'conflicts_critical': info.get('critical', 0),
                          'today': sum(1 for l in lessons if l['weekday'] == wk['today_wd'] and lesson_is_current(l, cur))})
        page = paginate_list(cards, request.args.get('page', 1), GROUPS_PER_PAGE)
        return render_template('schedule.html', mode='groups', page=page, totals=totals,
                               groups_count=len(groups), course_options=[(c, '%s курс' % c) for c in courses],
                               **common)

    lessons_all = snap.by_group.get(selected_group, [])
    lessons = [l for l in lessons_all if week_matches(l['week'], week, cur) and (day is None or l['weekday'] == day)]
    view = view_param('schedule_group', ('week', 'list'), 'week')
    group_marks = {l['id']: marks[l['id']] for l in lessons_all if l['id'] in marks}
    # подсветка: накладки движка (по всему колледжу, без принятых); пересечения внутри группы — только
    # запасной путь, если движок недоступен (про «принятые» накладки он не знает)
    overlap_ids, overlap_pairs = _overlap_ids(lessons_all) if not engine_ok else (set(), 0)
    for lid in overlap_ids:
        group_marks.setdefault(lid, {'severity': 'critical', 'items': ['Пересекается с другим занятием группы']})
    conf_info = conf_groups.get(selected_group) or {}
    try:
        idx = all_groups.index(selected_group)
        prev_group = all_groups[idx - 1] if idx > 0 else None
        next_group = all_groups[idx + 1] if idx + 1 < len(all_groups) else None
    except ValueError:
        prev_group = next_group = None
    teachers = OrderedDict()
    for l in sorted(lessons_all, key=lambda x: norm_name(teacher_label(x))):
        if l['teacher_key']:
            teachers.setdefault(l['teacher_key'], {'name': teacher_label(l), 'count': 0})
            teachers[l['teacher_key']]['count'] += 1
    columns = _day_columns(lessons)
    by_day = _by_day(lessons)
    return render_template(
        'schedule.html', mode='group', view=view, known_group=selected_group in all_groups, lessons=lessons,
        lessons_total=len(lessons_all), columns=columns, by_day=by_day,
        day_counts={d: len(v) for d, v in by_day.items()},
        focus_day=focus_day(columns, wk['today_wd']),
        stats=_group_stats(lessons_all, cur), overlap_ids=set(group_marks), marks=group_marks,
        overlap_pairs=overlap_pairs, conflicts_critical=conf_info.get('critical', 0),
        conflicts_warning=conf_info.get('warning', 0),
        group_conflicts_url=group_conflicts_url(selected_group, conf_info),
        prev_group=prev_group, next_group=next_group, recent_logs=load_recent_logs(limit=6, group_id=selected_group),
        group_teachers=teachers, now_minutes=_now_minutes(), course_num=group_course(selected_group),
        copy_targets=[x for x in all_groups if x != selected_group], week_options=WEEK_OPTIONS,
        teacher_options=[(uid, name) for uid, name in get_all_teachers().items()],
        all_rooms=get_all_rooms(), conflicts_url=_schedule_conflicts_url(), **common)


# ==================== ЖУРНАЛ: ЧТЕНИЕ И ПРЕДСТАВЛЕНИЕ ====================

ACTION_LABELS = OrderedDict([('add', ('Добавлено', 'success', 'plus-circle')),
                             ('update', ('Изменено', 'warning', 'pencil')),
                             ('delete', ('Удалено', 'danger', 'trash'))])
LOG_FIELDS = (('subject', 'Дисциплина'), ('teacher', 'Преподаватель'), ('time', 'Время'), ('room', 'Аудитория'),
              ('week_type', 'Неделя'), ('day', 'День'), ('groupId', 'Группа'))


def _load_logs():
    """Хвост журнала (кэш 60 с; сбрасывается вместе со снапшотом при любой записи CMS)."""
    def load():
        q = db.collection('schedule_logs').order_by('changed_at', direction='DESCENDING').limit(LOG_FETCH_LIMIT)
        return [doc_dict(s) for s in q.stream(**fs_opts(True))]
    return cache.get_or_set('schedule:logs', load, ttl=60)


def _safe_logs():
    try:
        return _load_logs()
    except Exception:
        log.exception('Не удалось загрузить журнал расписания')
        return []


def _norm_action(action):
    return {'create': 'add'}.get(action, action or 'update')


def _log_value(field, data, teachers):
    if field == 'teacher':
        raw = str(data.get('teacher') or data.get('teacherId') or '').strip()
        return teachers.get(raw) or raw
    if field == 'week_type':
        return WEEK_LABELS.get(str(data.get('week_type') or 'always'), str(data.get('week_type') or ''))
    if field == 'day':
        wd = lesson_weekday(data)
        return DAY_NAMES_FULL[wd] if wd is not None else ''
    return str(data.get(field) or '')


def _log_views(entries):
    """Записи журнала → представление для шаблонов (имена, изменения полей, время по Алматы)."""
    teachers = get_all_teachers()
    uids = {e.get('changed_by') for e in entries if e.get('changed_by') and not e.get('changed_by_email')
            and e.get('changed_by') != 'unknown'}
    users = get_docs('users', uids, ['email', 'name']) if uids else {}
    snap_ids = load_lessons_snapshot().by_id
    out = []
    for e in entries:
        action = _norm_action(e.get('action'))
        old = e.get('old_data') if isinstance(e.get('old_data'), dict) else {}
        new = e.get('new_data') if isinstance(e.get('new_data'), dict) else {}
        data = new or old
        current = snap_ids.get(e.get('schedule_id'))
        if current and not data.get('subject'):  # старые записи журнала хранили только изменённые поля
            data = dict(current['raw'], **data)
        changes = []
        if action == 'update':
            for field, label in LOG_FIELDS:
                if field == 'day' and lesson_weekday(old) is None:
                    continue
                a, b = _log_value(field, old, teachers), _log_value(field, new, teachers)
                if a != b:
                    changes.append({'label': label, 'old': a or '—', 'new': b or '—'})
        uid = e.get('changed_by')
        user = users.get(uid) or {}
        author = e.get('changed_by_email') or user.get('email') or user.get('name') or \
            (uid if uid and uid != 'unknown' else '') or 'Приложение'
        wd = lesson_weekday(data)
        label, tone, icon = ACTION_LABELS.get(action, ('Изменено', 'neutral', 'pencil'))
        view = dict(e)
        view.update({
            'action': action, 'action_raw': e.get('action'), 'action_label': label, 'tone': tone, 'icon': icon,
            'group_id': e.get('group_id') or data.get('groupId') or '',
            'subject': data.get('subject') or '', 'time': data.get('time') or '',
            'teacher_name': _log_value('teacher', data, teachers), 'room': data.get('room') or '',
            'week': str(data.get('week_type') or 'always'),
            'day_name': DAY_NAMES_FULL[wd] if wd is not None else None,
            'changes': changes, 'author': author, 'changed_by_name': author,
            'when': fmt_dt(e.get('changed_at') or e.get('timestamp')),
            'ago': humanize_ago(e.get('changed_at') or e.get('timestamp')),
            'lesson_id': e.get('schedule_id'), 'lesson_exists': e.get('schedule_id') in snap_ids,
            'old_data': old, 'new_data': new,
        })
        view['summary'] = ' · '.join(x for x in (view['day_name'], view['time'], view['subject'], view['teacher_name']) if x)
        out.append(view)
    return out


def load_recent_logs(limit=10, group_id=None):
    """Последние записи журнала (для страниц расписания и дашборда). → [dict]: ключи старого
    формата (action, group_id, changed_by_name, changed_at, day_name, old_data, new_data) и новые
    (action_label, tone, icon, summary, changes, author, when, ago, lesson_id, lesson_exists)."""
    logs = _safe_logs()
    if group_id:
        logs = [e for e in logs if e.get('group_id') == group_id]
    return _log_views(logs[:limit])


@routes.route('/schedule/logs')
@admin_required
def schedule_logs():
    """История изменений: фильтры по группе, действию и тексту, постраничный вывод."""
    group = (request.args.get('group') or '').strip()
    action = (request.args.get('action') or '').strip()
    q_raw = (request.args.get('q') or '').strip()
    q = norm_text(q_raw)
    load_error = False
    try:
        entries = _load_logs()
    except Exception:
        log.exception('Не удалось загрузить журнал расписания')
        entries, load_error = [], True
    log_groups = sorted({e.get('group_id') for e in entries if e.get('group_id')}, key=group_sort_key)
    if group:
        entries = [e for e in entries if e.get('group_id') == group]
    counts = Counter(_norm_action(e.get('action')) for e in entries)
    if action in ACTION_LABELS:
        entries = [e for e in entries if _norm_action(e.get('action')) == action]
    if q:
        def hay(e):
            parts = [e.get('group_id'), e.get('schedule_id'), e.get('changed_by_email')]
            for d in (e.get('old_data'), e.get('new_data')):
                if isinstance(d, dict):
                    parts += [d.get('subject'), d.get('teacher'), d.get('room'), d.get('time')]
            return norm_text(' '.join(str(p) for p in parts if p))
        entries = [e for e in entries if q in hay(e)]
    page = paginate_list(entries, request.args.get('page', 1), LOGS_PER_PAGE)
    page.items = _log_views(page.items)
    return render_template('schedule_logs.html', page=page, logs=page.items, counts=counts, total=len(entries),
                           group=group, action=action, q=q_raw, log_groups=log_groups, load_error=load_error,
                           action_labels=ACTION_LABELS, fetch_limit=LOG_FETCH_LIMIT, **week_context())


# ==================== КОНСТРУКТОР ====================

def _builder_rows_from_form(form):
    """Строки конструктора из формы. Два формата: новый (поле day у каждой строки) и старый
    (day_of_week у блока дня + lesson_day_rel у строки — индекс блока)."""
    subjects = form.getlist('subject')
    n = len(subjects)

    def col(name):
        vals = form.getlist(name)
        return [vals[i] if i < len(vals) else '' for i in range(n)]

    days = form.getlist('day')
    if len(days) != n:
        dows, rels = form.getlist('day_of_week'), form.getlist('lesson_day_rel')
        days = []
        for i in range(n):
            try:
                days.append(dows[int(rels[i])])
            except (IndexError, ValueError):
                days.append('')
    times, customs, teachers, rooms, weeks = col('time'), col('time_custom'), col('teacher'), col('room'), col('week_type')
    return [{'key': 'n%d' % i, 'id': '', 'day': days[i], 'time': times[i], 'time_custom': customs[i],
             'subject': subjects[i], 'teacher': teachers[i], 'room': rooms[i], 'week_type': weeks[i] or 'always'}
            for i in range(n)]


def _row_view(row, errors=None):
    """Строка для макроса редактора (значения строками + ошибки + исходные значения)."""
    time = row.get('time') or ''
    if time == CUSTOM_TIME:
        time = row.get('time_custom') or ''
    return {'key': row.get('key') or row.get('id') or '', 'id': row.get('id') or '', 'day': _parse_day(row.get('day')),
            'time': time, 'subject': row.get('subject') or '', 'teacher': row.get('teacher') or '',
            'room': row.get('room') or '', 'week_type': row.get('week_type') or 'always',
            'errors': errors or [], 'orig': row.get('orig'), 'flags': row.get('flags') or []}


def _render_builder(rows, group, status=200, errors=None):
    snap = load_lessons_snapshot()
    existing = [_lesson_json(l) for l in snap.by_group.get(group, [])] if group else []
    cancel = safe_next_url(request.values.get('next')) or url_for('schedule', group=group or None)
    return render_template('schedule_builder.html', rows=rows, preselected_group=group,
                           available_groups=get_all_groups(), existing_lessons=existing, cancel_url=cancel,
                           form_errors=errors or [], f=form_context(), **week_context()), status


def _prefill_time(raw, day):
    """?time=HH:MM-HH:MM → значение для селекта времени: метка слота сетки звонков с тем же
    интервалом ('08.25-09.15'), иначе нормализованное 'HH.MM-HH.MM' (покажется «не из сетки»)."""
    start, end = parse_time_range(raw)
    if start is None or end is None or end <= start:
        return ''
    for label, s, e in call_slots(day if day is not None else 1):
        if (s, e) == (start, end):
            return label
    return '%02d.%02d-%02d.%02d' % (start // 60, start % 60, end // 60, end % 60)


@routes.route('/schedule/builder', methods=['GET', 'POST'])
@admin_required
def schedule_builder():
    """Конструктор: несколько дней и занятий за раз, живая проверка накладок, существующие пары
    группы рядом. Запись — day_index + date, notify:false, один пуш на группу.

    Предзаполнение первой строки (SCH-10): ?groupId=&day=0..6&time=HH:MM-HH:MM&room= — из «+» в колонке
    дня, «Добавить занятие здесь» (шахматка аудиторий) и главной."""
    if request.method == 'POST':
        return handle_schedule_builder_post()
    group = (request.args.get('groupId') or request.args.get('group') or '').strip()[:FIELD_LIMITS['groupId']]
    day = _parse_day(request.args.get('day'))
    row = {'key': 'n0', 'day': 0 if day is None else day,
           'time': _prefill_time(request.args.get('time'), day),
           'room': _clip(request.args.get('room'), FIELD_LIMITS['room']),
           'teacher': _clip(request.args.get('teacher'), FIELD_LIMITS['teacher'])}
    return _render_builder([_row_view(row)], group)


def handle_schedule_builder_post():
    """Сохранение конструктора (все строки валидны — иначе ничего не пишем и показываем ошибки)."""
    group = _clip(request.form.get('groupId'), FIELD_LIMITS['groupId'])
    rows, valid, form_errors = [], [], []
    for r in _builder_rows_from_form(request.form):
        no_time = not (r['time'] or '').strip() or (r['time'] == CUSTOM_TIME and not (r['time_custom'] or '').strip())
        if not (r['subject'] or '').strip() and no_time:
            continue  # пустая строка
        data, errs = lesson_from_input(r, group=group or '-')
        rows.append(_row_view(r, errs))
        if not errs:
            valid.append(data)
    if not group:
        form_errors.append('Выберите группу')
    elif not _known_group(group):
        form_errors.append('Группа «%s» не найдена. Создайте её в разделе «Люди → Группы».' % group)
    if not rows:
        flash('Ни одного занятия не заполнено', 'warning')
        return _render_builder([_row_view({'key': 'n0', 'day': 0})], group, 400, form_errors)
    if form_errors or len(valid) != len(rows):
        flash('Проверьте отмеченные поля — ничего не сохранено', 'error')
        return _render_builder(rows, group, 400, form_errors)

    batch = ScheduleBatch(quiet=True)
    created = []
    for data in valid:
        data = dict(data, groupId=group)
        created.append((batch.add(lesson_doc(data)), data))
    try:
        batch.commit()
    except PartialCommitError:  # повтор формы добавил бы уже записанные занятия второй раз
        log.exception('Конструктор: запись прервалась посередине')
        flash('Сохранена только часть занятий группы %s — связь с базой прервалась. Проверьте неделю группы '
              'и добавьте недостающие.' % group, 'warning')
        return redirect(url_for('schedule', group=group))
    except Exception:
        log.exception('Конструктор: запись не удалась')
        flash('Не удалось сохранить занятия. Попробуйте ещё раз.', 'error')
        return _render_builder(rows, group, 500)
    audit('schedule_create', 'schedule', group, {'count': len(created)})
    flash('Добавлено %d %s в группу %s' % (len(created), plural(len(created), 'занятие', 'занятия', 'занятий'), group),
          'success')
    found = []
    for doc_id, data in created:
        items = check_conflicts(dict(lesson_doc(data), id=doc_id), exclude_id=doc_id)
        if items:
            found.append((_lesson_label(data), items))
    _flash_conflicts(found, 'Сохранено, но есть накладки')
    return redirect(url_for('schedule', group=group))


# ==================== РЕДАКТОР ГРУППЫ ====================

def _fresh_group_docs(group):
    """Свежие документы группы из Firestore (для редактора и сохранения — без кэша, чтобы не
    затереть правки, сделанные в приложении за последние минуты)."""
    return {s.id: (s.to_dict() or {}) for s in where(db.collection('schedules'), 'groupId', '==', group)
            .stream(**fs_opts(True))}


@routes.route('/schedule/edit_group/<group_id>')
@admin_required
def schedule_edit_group(group_id):
    """Редактор всего расписания группы: ВСЕ занятия, включая рекуррентные (только day_index),
    воскресные и «без дня»; значения не из справочников сохраняются."""
    group_id = (group_id or '').strip()
    try:
        docs = _fresh_group_docs(group_id)
    except Exception:
        log.exception('Редактор группы: чтение не удалось')
        flash('Не удалось загрузить расписание группы. Попробуйте ещё раз.', 'error')
        return redirect(url_for('schedule', group=group_id))
    if not docs and not _known_group(group_id):
        flash('Группа «%s» не найдена' % group_id, 'warning')
        return redirect(url_for('schedule'))
    index = build_teacher_index()
    rows = []
    for doc_id, data in docs.items():
        n = normalize_lesson(doc_id, data, index)
        flags = []
        if n['weekday'] is None:
            flags.append('Нет дня недели — выберите день')
        if n['day_mismatch']:
            flags.append('day_index и date указывают на разные дни — показан день из day_index')
        if n['time'] and not n['time_ok']:
            flags.append('Время не распознано')
        if n['week_unknown']:
            flags.append('Неизвестный тип недели «%s»' % n['week_raw'])
        hint = _teacher_hint(n)
        if hint:
            flags.append('Преподаватель: ' + hint)
        orig = {'day': n['weekday'], 'time': n['time'], 'subject': n['subject'], 'teacher': n['teacher'],
                'room': n['room'], 'week_type': _old_value(data, 'week_type')}
        row = _row_view({'key': doc_id, 'id': doc_id, 'day': n['weekday'], 'time': n['time'], 'subject': n['subject'],
                         'teacher': n['teacher'], 'room': n['room'], 'week_type': orig['week_type'],
                         'orig': orig, 'flags': flags})
        row['sort'] = sm.lesson_sort_key(n)
        rows.append(row)
    rows.sort(key=lambda r: r['sort'])
    return render_template('schedule_builder_edit.html', group_id=group_id, rows=rows, lessons_count=len(rows),
                           return_url=safe_next_url(request.args.get('next')) or url_for('schedule', group=group_id),
                           f=form_context(), **week_context())


def _incoming_lessons(payload):
    """Тело запроса редактора → [dict]: новый формат {lessons:[{…, day}]}, старый — {days:[{day_of_week, lessons}]}."""
    out = []
    if isinstance(payload.get('lessons'), list):
        out.extend(dict(l) for l in payload['lessons'] if isinstance(l, dict))
    for block in payload.get('days') or []:
        if not isinstance(block, dict):
            continue
        for l in block.get('lessons') or []:
            if isinstance(l, dict):
                item = dict(l)
                item.setdefault('day', block.get('day_of_week'))
                out.append(item)
    return out


@routes.route('/schedule/update_group', methods=['POST'])
@admin_required
def schedule_update_group():
    """Сохранение редактора группы (JSON).

    Тело: {group_id, lessons:[{key, id?, day, time, time_custom?, subject, teacher, room, week_type,
    orig?}], deleted_ids:[…], next?} (старый формат days:[{day_of_week, lessons}] тоже принимается).
    Удаляются ТОЛЬКО deleted_ids этой группы; занятия без изменений не пишутся; если есть orig
    (исходные значения строки), пишется только то, что менял пользователь."""
    payload = request.get_json(silent=True) or {}
    group = _clip(payload.get('group_id'), FIELD_LIMITS['groupId'])
    incoming = _incoming_lessons(payload)
    deleted_ids = [str(x) for x in (payload.get('deleted_ids') or []) if x]
    if not group:
        return jsonify({'error': 'Не указана группа'}), 400
    if len(incoming) > MAX_EDITOR_LESSONS:
        return jsonify({'error': 'Слишком много занятий за один раз'}), 400
    try:
        existing = _fresh_group_docs(group)
    except Exception:
        log.exception('update_group: чтение не удалось')
        return jsonify({'error': 'Не удалось прочитать расписание группы. Попробуйте ещё раз.'}), 503

    plans, errors, seen, stale = [], [], set(), []
    for i, l in enumerate(incoming):
        key = str(l.get('key') or l.get('id') or i)
        lesson_id = str(l.get('id') or '')
        if lesson_id and lesson_id in seen:
            continue
        old = existing.get(lesson_id) if lesson_id else None
        if lesson_id and old is None:
            # занятие удалили или перенесли в другую группу, пока была открыта вкладка: создавать его
            # заново (воскрешать удалённое / дублировать перенесённое) нельзя
            stale.append({'key': key, 'messages': [STALE_ROW_TEXT]})
            continue
        data, errs = lesson_from_input(l, old=old, group=group)
        if errs:
            errors.append({'key': key, 'messages': errs})
            continue
        if old is not None:
            seen.add(lesson_id)
            fields = plan_update(old, data, _orig_fields(l.get('orig'), data))
            if fields:
                plans.append(('update', lesson_id, old, fields, data, key))
        else:
            plans.append(('add', None, None, lesson_doc(data), data, key))
    if stale:
        return jsonify({'error': STALE_ROW_TEXT + ' — ничего не сохранено', 'errors': stale + errors}), 409
    if errors:
        return jsonify({'error': 'Проверьте отмеченные занятия — ничего не сохранено', 'errors': errors}), 400

    batch = ScheduleBatch(quiet=True)
    touched = []
    for kind, lesson_id, old, fields, data, key in plans:
        if kind == 'update':
            batch.update(lesson_id, old, fields)
            touched.append((lesson_id, data, key))
        else:
            touched.append((batch.add(fields), data, key))
    for doc_id in dict.fromkeys(deleted_ids):
        if doc_id in existing and doc_id not in seen:
            batch.delete(doc_id, existing[doc_id])
    try:
        counts = batch.commit()
    except PartialCommitError:
        # часть порций уже в базе: «Попробуйте ещё раз» добавил бы новые строки второй раз —
        # перезагружаем редактор со свежими данными и честно говорим, что сохранилось не всё
        log.exception('update_group: запись прервалась посередине')
        message = ('Сохранена только часть изменений группы %s — связь с базой прервалась. Проверьте неделю '
                   'и повторите оставшиеся правки.' % group)
        flash(message, 'warning')
        audit('schedule_update_group', 'schedule', group, {'partial': True})
        nxt = safe_next_url(payload.get('next'))
        return jsonify({'success': False, 'partial': True, 'message': message,
                        'redirect': url_for('schedule_edit_group', group_id=group, next=nxt)}), 200
    except Exception:
        log.exception('update_group: запись не удалась')
        return jsonify({'error': 'Не удалось сохранить расписание. Попробуйте ещё раз.'}), 500

    total = sum(counts.values())
    message = ('Расписание группы %s сохранено: %s' % (group, _summary(counts))) if total \
        else 'Изменений нет — ничего не записано'
    conflicts_out = []
    for doc_id, data, key in touched:
        items = check_conflicts(dict(lesson_doc(data), id=doc_id), exclude_id=doc_id)
        if items:
            conflicts_out.append({'key': key, 'id': doc_id, 'label': _lesson_label(data),
                                  'conflicts': [{'severity': x['severity'], 'text': x['text']} for x in items]})
    if total:
        audit('schedule_update_group', 'schedule', group, counts)
        flash(message, 'success')
        _flash_conflicts([(c['label'], c['conflicts']) for c in conflicts_out], 'Сохранено, но есть накладки')
    redirect_url = safe_next_url(payload.get('next')) or url_for('schedule', group=group)
    return jsonify({'success': True, 'message': message, 'redirect': redirect_url, 'stats': counts,
                    'conflicts': conflicts_out}), 200


# ==================== УДАЛЕНИЕ ====================

@routes.route('/schedule/delete/<doc_id>', methods=['POST'])
@admin_required
def schedule_delete_item(doc_id):
    """Удаление одного занятия (пуш группе отправит функция onScheduleDelete — как раньше)."""
    ref = db.collection('schedules').document(doc_id)
    group = ''
    try:
        snap = ref.get(**fs_opts())
        if not snap.exists:
            flash('Занятие не найдено — возможно, его уже удалили', 'warning')
        else:
            old = snap.to_dict() or {}
            group = old.get('groupId') or ''
            batch = db.batch()
            batch.delete(ref)
            entry = schedule_logger.entry('delete', doc_id, old, None, group)
            if entry:
                batch.set(db.collection('schedule_logs').document(), entry)
            batch.commit()
            invalidate_schedule_caches()
            audit('schedule_delete', 'schedule', doc_id, {'group': group, 'subject': old.get('subject'),
                                                           'time': old.get('time')})
            flash('Занятие «%s» удалено' % (old.get('subject') or doc_id), 'success')
    except Exception:
        log.exception('Удаление занятия %s не удалось', doc_id)
        flash('Не удалось удалить занятие. Попробуйте ещё раз.', 'error')
    return _redirect_next(url_for('schedule', group=group) if group else url_for('schedule'))


@routes.route('/schedule/delete_group/<group_id>', methods=['POST'])
@admin_required
def schedule_delete_group(group_id):
    """Очистить расписание группы (notify:false + один пуш группе и преподавателям)."""
    try:
        docs = _fresh_group_docs(group_id)
        if not docs:
            flash('У группы %s нет занятий' % group_id, 'warning')
            return _redirect_next(url_for('schedule'))
        batch = ScheduleBatch(quiet=True)
        for doc_id, data in docs.items():
            batch.delete(doc_id, data)
        batch.commit()
        audit('schedule_delete_group', 'schedule', group_id, {'count': len(docs)})
        flash('Расписание группы %s очищено (%d %s)' % (group_id, len(docs),
                                                         plural(len(docs), 'занятие', 'занятия', 'занятий')), 'success')
    except Exception:
        log.exception('Очистка расписания группы %s не удалась', group_id)
        flash('Не удалось очистить расписание группы. Попробуйте ещё раз.', 'error')
    return _redirect_next(url_for('schedule', group=group_id))


# ==================== КОПИРОВАНИЕ И МАССОВЫЕ ДЕЙСТВИЯ ====================

def _lesson_signature(data):
    return (lesson_weekday(data), _old_value(data, 'time'), _old_value(data, 'subject'),
            _old_value(data, 'teacher'), _old_value(data, 'room'), _old_value(data, 'week_type'))


@routes.route('/schedule/copy_group', methods=['POST'])
@admin_required
def schedule_copy_group():
    """Скопировать расписание группы в другую: append — добавить (без точных дублей), replace —
    заменить расписание целевой группы. Один пуш целевой группе."""
    source = _clip(request.form.get('source'), FIELD_LIMITS['groupId'])
    target = _clip(request.form.get('target'), FIELD_LIMITS['groupId'])
    mode = 'replace' if request.form.get('mode') == 'replace' else 'append'
    back = url_for('schedule', group=source) if source else url_for('schedule')
    if not source or not target:
        flash('Выберите исходную и целевую группы', 'error')
        return redirect(back)
    if source == target:
        flash('Исходная и целевая группы совпадают', 'error')
        return redirect(back)
    if not _known_group(target):
        flash('Группа «%s» не найдена. Создайте её в разделе «Люди → Группы».' % target, 'error')
        return redirect(back)
    try:
        src_docs = _fresh_group_docs(source)
        dst_docs = _fresh_group_docs(target)
    except Exception:
        log.exception('Копирование: чтение не удалось')
        flash('Не удалось прочитать расписание. Попробуйте ещё раз.', 'error')
        return redirect(back)
    if not src_docs:
        flash('У группы %s нет занятий для копирования' % source, 'warning')
        return redirect(back)
    batch = ScheduleBatch(quiet=True)
    existing_sigs = set()
    if mode == 'replace':
        for doc_id, data in dst_docs.items():
            batch.delete(doc_id, data)
    else:
        existing_sigs = {_lesson_signature(d) for d in dst_docs.values()}
    skipped = 0
    for data in sorted(src_docs.values(), key=lambda d: (9 if lesson_weekday(d) is None else lesson_weekday(d),
                                                          _old_value(d, 'time'))):
        wd = lesson_weekday(data)
        if wd is None or _lesson_signature(data) in existing_sigs:
            skipped += 1
            continue
        doc = {'groupId': target, 'time': _old_value(data, 'time'), 'subject': _old_value(data, 'subject'),
               'teacher': _old_value(data, 'teacher'), 'room': _old_value(data, 'room'),
               'week_type': _old_value(data, 'week_type')}
        doc.update(day_fields(wd))
        batch.add(doc)
    try:
        counts = batch.commit()
    except PartialCommitError:
        log.exception('Копирование расписания %s → %s прервалось посередине', source, target)
        flash('Скопирована только часть расписания %s → %s — связь с базой прервалась. Проверьте неделю группы %s, '
              'прежде чем копировать снова.' % (source, target, target), 'warning')
        return redirect(back)
    except Exception:
        log.exception('Копирование расписания %s → %s не удалось', source, target)
        flash('Не удалось скопировать расписание. Попробуйте ещё раз.', 'error')
        return redirect(back)
    details = dict(counts, source=source, mode=mode, skipped=skipped)
    audit('schedule_copy', 'schedule', target, details)
    msg = 'Расписание %s скопировано в %s: %s' % (source, target, _summary(counts) or 'новых занятий нет')
    if skipped:
        msg += ' (пропущено %d: дубли или занятия без дня)' % skipped
    flash(msg, 'success')
    return redirect(url_for('schedule', group=target))


BULK_ACTIONS = OrderedDict([('week', 'Сменить тип недели'), ('room', 'Сменить аудиторию'),
                            ('teacher', 'Сменить преподавателя'), ('delete', 'Удалить')])


@routes.route('/schedule/bulk', methods=['POST'])
@admin_required
def schedule_bulk():
    """Массовые действия над выбранными занятиями: сменить тип недели, аудиторию, преподавателя, удалить.

    Поля: ids (id занятий), action, next. Ограничения выборки (защита от чужих id):
      group        — только занятия этой группы;
      teacher_from — только занятия этого преподавателя (uid или ФИО — сравнение по ключу справочника).
    action=week: week_type (или короткая форма action='week:numerator'); action=room: room;
    action=teacher: teacher — uid из справочника преподавателей (SCH-11, контракт для PEOPLE)."""
    action = request.form.get('action') or ''
    value = None
    if ':' in action:   # кнопки панели выбора: «week:numerator» — действие и значение одной кнопкой
        action, value = action.split(':', 1)
    ids = [i for i in dict.fromkeys(request.form.getlist('ids')) if i][:MAX_EDITOR_LESSONS]
    group = _clip(request.form.get('group'), FIELD_LIMITS['groupId'])
    teacher_from = _clip(request.form.get('teacher_from'), FIELD_LIMITS['teacher'])
    back = url_for('schedule', group=group, view='list') if group else url_for('schedule')
    if action not in BULK_ACTIONS:
        flash('Неизвестное действие', 'error')
        return _redirect_next(back)
    if not ids:
        flash('Не выбрано ни одного занятия', 'warning')
        return _redirect_next(back)
    index = None
    if action == 'week':
        value = value or request.form.get('week_type')
        if value not in WEEK_TYPES:
            flash('Выберите тип недели', 'error')
            return _redirect_next(back)
    elif action == 'room':
        value = _clip(request.form.get('room'), FIELD_LIMITS['room'])
    elif action == 'teacher':
        value = _clip(request.form.get('teacher'), FIELD_LIMITS['teacher'])
        if value not in get_all_teachers():
            flash('Выберите преподавателя из справочника', 'error')
            return _redirect_next(back)
    docs = get_docs('schedules', ids)
    if group:
        docs = {k: v for k, v in docs.items() if v.get('groupId') == group}
    if teacher_from:
        index = build_teacher_index()
        want = teacher_from if teacher_from.startswith(('uid:', 'name:')) else \
            (resolve_teacher(teacher_from, index)['key'] or teacher_from)
        docs = {k: v for k, v in docs.items()
                if resolve_teacher(v.get('teacher') or v.get('teacherId'), index)['key'] == want}
    batch = ScheduleBatch(quiet=True)
    for doc_id, data in docs.items():
        if action == 'delete':
            batch.delete(doc_id, data)
        elif action == 'week' and _old_value(data, 'week_type') != value:
            batch.update(doc_id, data, _with_day(data, {'week_type': value}))
        elif action == 'room' and _old_value(data, 'room') != value:
            batch.update(doc_id, data, _with_day(data, {'room': value}))
        elif action == 'teacher' and _old_value(data, 'teacher') != value:
            fields = {'teacher': value}
            if 'teacherId' in data:
                fields['teacherId'] = DELETE_FIELD  # функции берут teacherId раньше teacher
            batch.update(doc_id, data, _with_day(data, fields))
    if not len(batch):
        flash('Изменений нет', 'info')
        return _redirect_next(back)
    try:
        counts = batch.commit()
    except Exception:
        log.exception('Массовое действие %s не удалось', action)
        flash('Не удалось выполнить действие. Попробуйте ещё раз.', 'error')
        return _redirect_next(back)
    audit('schedule_bulk', 'schedule', group or None, dict(counts, action=action, value=value))
    if action == 'teacher':
        n = counts.get('update', 0)
        flash('Преподаватель заменён в %d %s' % (n, plural(n, 'занятии', 'занятиях', 'занятиях')), 'success')
    else:
        flash('Готово: %s' % _summary(counts), 'success')
    return _redirect_next(back)


# ==================== ПРАВКА ОДНОГО ЗАНЯТИЯ ====================

def _lesson_history(item_id):
    """Журнал одного занятия (запрос по равенству — индекс не нужен)."""
    try:
        q = where(db.collection('schedule_logs'), 'schedule_id', '==', item_id).limit(30)
        entries = [doc_dict(s) for s in q.stream(**fs_opts(True))]
    except Exception:
        log.exception('История занятия %s не загрузилась', item_id)
        return []
    entries.sort(key=lambda e: fmt_dt(e.get('changed_at') or e.get('timestamp'), '%Y%m%d%H%M%S', ''), reverse=True)
    return _log_views(entries[:10])


def _edit_view(item_id, data, form_values=None, errors=None, status=200, conflicts=None):
    n = normalize_lesson(item_id, data, build_teacher_index())
    values = form_values or {'groupId': n['groupId'], 'day': n['weekday'], 'time': n['time'], 'subject': n['subject'],
                             'teacher': n['teacher'], 'room': n['room'], 'week_type': _old_value(data, 'week_type')}
    if conflicts is None:
        conflicts = check_conflicts(dict(data, id=item_id), exclude_id=item_id) if n['weekday'] is not None else []
    return render_template(
        'schedule_edit.html', item=dict(data, id=item_id), lesson=n, values=values, errors=errors or [],
        history=_lesson_history(item_id), conflicts=conflicts, group_id=n['groupId'],
        return_url=safe_next_url(request.values.get('next')) or url_for('schedule', group=n['groupId']),
        conflicts_url=_schedule_conflicts_url(), f=form_context(), **week_context()), status


@routes.route('/schedule/edit/<schedule_id>', methods=['GET', 'POST'])
@admin_required
def schedule_edit(schedule_id):
    """Правка одного занятия: день недели (пишется day_index + date), время из сетки или своё,
    значения не из справочников сохраняются; предупреждение о накладках не блокирует сохранение.
    Пуш об изменении шлёт функция onScheduleUpdate (одно занятие — один пуш, как раньше)."""
    ref = db.collection('schedules').document(schedule_id)
    try:
        snap = ref.get(**fs_opts())
    except Exception:
        log.exception('Чтение занятия %s не удалось', schedule_id)
        flash('Не удалось загрузить занятие. Попробуйте ещё раз.', 'error')
        return redirect(url_for('schedule'))
    if not snap.exists:
        flash('Занятие не найдено — возможно, его удалили', 'error')
        return redirect(url_for('schedule'))
    old = snap.to_dict() or {}
    if request.method == 'GET':
        return _edit_view(schedule_id, old)

    data, errors = lesson_from_input(request.form, old=old)
    if data['groupId'] and data['groupId'] != _old_value(old, 'groupId') and not _known_group(data['groupId']):
        errors.append('Группа «%s» не найдена' % data['groupId'])
    if errors:
        flash('Проверьте поля формы — изменения не сохранены', 'error')
        return _edit_view(schedule_id, old, form_values=data, errors=errors, status=400, conflicts=[])
    fields = plan_update(old, data)
    group_url = url_for('schedule', group=data['groupId'])
    target = redirect(back_url('schedule', group=data['groupId']))
    if not fields:
        flash('Изменений нет', 'info')
        return target
    fields['updatedAt'] = SERVER_TIMESTAMP
    if 'notify' in old:
        # notify:false «залип» после сбоя массовой записи — снимаем: эту правку функция разошлёт пушем
        fields['notify'] = DELETE_FIELD
    try:
        batch = db.batch()
        batch.update(ref, fields)
        entry = schedule_logger.entry('update', schedule_id, old, _merge(old, fields), data['groupId'])
        if entry:
            batch.set(db.collection('schedule_logs').document(), entry)
        batch.commit()
    except Exception:
        log.exception('Сохранение занятия %s не удалось', schedule_id)
        flash('Не удалось сохранить занятие. Попробуйте ещё раз.', 'error')
        return _edit_view(schedule_id, old, form_values=data, status=500, conflicts=[])
    finally:
        invalidate_schedule_caches()
    audit('schedule_update', 'schedule', schedule_id,
          {'group': data['groupId'], 'fields': sorted(k for k in fields if k != 'updatedAt')})
    # «Открыть группу» — если возвращаемся не на её неделю (например, из накладок или аудиторий)
    nxt = urlparse(next_target() or url_for('schedule', group=data['groupId']))
    if nxt.path == url_for('schedule') and parse_qs(nxt.query).get('group') == [data['groupId']]:
        flash('Занятие сохранено', 'success')
    else:
        flash_action('Занятие сохранено', 'Открыть группу', href=group_url)
    _flash_conflicts([('', check_conflicts(dict(lesson_doc(data), id=schedule_id), exclude_id=schedule_id))],
                     'Сохранено, но есть накладки')
    return target


# ==================== ЕДИНАЯ ТАБЛИЦА ====================

def _filtered_lessons(snap, cur, group=None, course='', major='', q='', week='', day=None):
    """Занятия снапшота по фильтрам страниц и экспорта."""
    qn = norm_text(q)
    out = []
    for l in snap.lessons:
        if not l['groupId']:
            continue
        if group and l['groupId'] != group:
            continue
        if course and str(group_course(l['groupId']) or '') != str(course):
            continue
        if major and group_major(l['groupId']) != major.upper():
            continue
        if not week_matches(l['week'], week, cur):
            continue
        if day is not None and l['weekday'] != day:
            continue
        if qn and qn not in norm_text(' '.join((l['groupId'], l['subject'], teacher_label(l), l['room']))):
            continue
        out.append(l)
    return out


@routes.route('/schedule/all')
@admin_required
def schedule_all():
    """Единая таблица «группа × день» с фильтрами, поиском, постраничным выводом и экспортом."""
    wk = week_context()
    cur = wk['current_week']
    snap = load_lessons_snapshot()
    course = (request.args.get('course') or '').strip()
    major = (request.args.get('major') or '').strip()
    week = week_param()
    day = day_param()
    q = (request.args.get('q') or '').strip()
    lessons = _filtered_lessons(snap, cur, course=course, major=major, q=q, week=week, day=day)
    grid = {}
    for l in lessons:
        grid.setdefault(l['groupId'], {}).setdefault(l['weekday'], []).append(l)
    groups = sorted(grid, key=lambda x: (group_sort_key(x), x))
    per = request.args.get('per')
    per_page = (len(groups) or 1) if per == 'all' else TABLE_GROUPS_PER_PAGE
    page = paginate_list(groups, request.args.get('page', 1), per_page)
    all_groups = get_all_groups()
    hide_empty = request.args.get('hide_empty') == '1'
    if day is not None:
        columns = [day]
    elif hide_empty:
        columns = [d for d in range(7) if any(d in grid[g] for g in page.items)] or _day_columns(lessons)
    else:
        columns = _day_columns(lessons)
    marks, _cg, _ct = conflict_marks()
    day_counts = Counter()
    for g in page.items:
        for wd, ls in grid[g].items():
            day_counts[wd] += len(ls)
    courses = sorted({group_course(x) for x in all_groups if group_course(x)})
    return render_template(
        'schedule_all.html', page=page, grid=grid, columns=columns, focus_day=focus_day(columns, wk['today_wd']),
        day_counts=day_counts, total_lessons=len(lessons), groups_count=len(groups), course=course, major=major,
        week=week, day=day, q=q, per=per, hide_empty=hide_empty, marks=marks,
        majors=sorted({group_major(x) for x in all_groups if group_major(x)}), courses=courses,
        course_options=[(c, '%s курс' % c) for c in courses], week_filters=WEEK_FILTERS,
        day_options=[(d, DAY_NAMES_FULL[d]) for d in range(7)],
        day_names=DAY_NAMES_FULL, day_short=DAY_SHORT, update_time=fmt_dt(now_almaty()), **wk)


# ==================== ЭКСПОРТ: XLSX БЕЗ ЗАВИСИМОСТЕЙ ====================

_XML_BAD = re.compile('[\x00-\x08\x0b\x0c\x0e-\x1f]')


def _xml(value):
    return _xml_escape(_XML_BAD.sub('', str(value)), {'"': '&quot;'})


def _col_letter(n):
    s = ''
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


class XlsxSheet:
    """Лист XLSX: ячейки (значение, стиль), ширины колонок, объединения, закреплённая шапка."""

    def __init__(self, name):
        self.name = re.sub(r'[\[\]:*?/\\]', ' ', name)[:31] or 'Лист'
        self.cells = {}
        self.widths = {}
        self.merges = []
        self.freeze = None

    def cell(self, row, col, value, style=0):
        self.cells[(row, col)] = (value, style)

    def row(self, row, values, style=0, start_col=1):
        for i, v in enumerate(values):
            self.cell(row, start_col + i, v, style)

    def merge(self, r1, c1, r2, c2):
        if (r1, c1) != (r2, c2):
            self.merges.append((r1, c1, r2, c2))

    def xml(self):
        rows = defaultdict(list)
        for (r, c), v in self.cells.items():
            rows[r].append((c, v))
        out = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
               '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
               'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">']
        if self.freeze:
            fr, fc = self.freeze
            attrs = []
            if fc > 1:
                attrs.append('xSplit="%d"' % (fc - 1))
            if fr > 1:
                attrs.append('ySplit="%d"' % (fr - 1))
            pane = 'bottomRight' if fr > 1 and fc > 1 else ('bottomLeft' if fr > 1 else 'topRight')
            out.append('<sheetViews><sheetView workbookViewId="0"><pane %s topLeftCell="%s%d" activePane="%s" '
                       'state="frozen"/></sheetView></sheetViews>' % (' '.join(attrs), _col_letter(fc), fr, pane))
        out.append('<sheetFormatPr defaultRowHeight="15"/>')
        if self.widths:
            out.append('<cols>%s</cols>' % ''.join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (c, c, w)
                                                   for c, w in sorted(self.widths.items())))
        out.append('<sheetData>')
        for r in sorted(rows):
            out.append('<row r="%d">' % r)
            for c, (value, style) in sorted(rows[r]):
                ref = '%s%d' % (_col_letter(c), r)
                s = ' s="%d"' % style if style else ''
                if isinstance(value, bool) or value is None or value == '':
                    out.append('<c r="%s"%s/>' % (ref, s))
                elif isinstance(value, (int, float)):
                    out.append('<c r="%s"%s><v>%s</v></c>' % (ref, s, value))
                else:
                    out.append('<c r="%s"%s t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>' % (ref, s, _xml(value)))
            out.append('</row>')
        out.append('</sheetData>')
        if self.merges:
            out.append('<mergeCells count="%d">%s</mergeCells>' % (
                len(self.merges), ''.join('<mergeCell ref="%s%d:%s%d"/>' % (_col_letter(c1), r1, _col_letter(c2), r2)
                                          for r1, c1, r2, c2 in self.merges)))
        out.append('<pageMargins left="0.5" right="0.5" top="0.6" bottom="0.6" header="0.3" footer="0.3"/>')
        out.append('</worksheet>')
        return ''.join(out)


class XlsxBook:
    """Минимальный XLSX без внешних библиотек (zipfile + SpreadsheetML).

    openpyxl нет в requirements.txt — раньше экспорт без него уходил в «установите openpyxl».
    Стили: 0 обычный, 1 шапка (бренд), 2 перенос строк, 3/4/5 — «каждую неделю»/числитель/
    знаменатель, 6 название группы/преподавателя, 7 заголовок, 8 ячейка с рамкой."""

    S_HEADER, S_WRAP, S_ALWAYS, S_NUM, S_DEN, S_NAME, S_TITLE, S_CELL = range(1, 9)

    STYLES = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="4"><font><sz val="11"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="11"/><color rgb="FFC24B00"/><name val="Calibri"/><family val="2"/></font>'
        '<font><b/><sz val="14"/><name val="Calibri"/><family val="2"/></font></fonts>'
        '<fills count="6"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFC24B00"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFF1F3F5"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFEAF2FF"/><bgColor indexed="64"/></patternFill></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FFFEF3C7"/><bgColor indexed="64"/></patternFill></fill></fills>'
        '<borders count="2"><border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border><left style="thin"><color rgb="FFD1D5DB"/></left><right style="thin"><color rgb="FFD1D5DB"/></right>'
        '<top style="thin"><color rgb="FFD1D5DB"/></top><bottom style="thin"><color rgb="FFD1D5DB"/></bottom>'
        '<diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="9">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" '
        'applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="3" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="4" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="0" fillId="5" borderId="1" xfId="0" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="top" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment horizontal="center" vertical="center" wrapText="1"/></xf>'
        '<xf numFmtId="0" fontId="3" fillId="0" borderId="0" xfId="0" applyFont="1"/>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="center"/></xf>'
        '</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        '</styleSheet>')

    def __init__(self):
        self.sheets = []

    def sheet(self, name):
        sh = XlsxSheet(name)
        names = {s.name for s in self.sheets}
        base, n = sh.name, 2
        while sh.name in names:
            sh.name = '%s %d' % (base[:28], n)
            n += 1
        self.sheets.append(sh)
        return sh

    @staticmethod
    def week_style(week):
        return {'numerator': XlsxBook.S_NUM, 'denominator': XlsxBook.S_DEN}.get(week, XlsxBook.S_ALWAYS)

    def to_bytes(self):
        if not self.sheets:
            self.sheet('Лист1')
        n = len(self.sheets)
        x = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        rel = 'http://schemas.openxmlformats.org/officeDocument/2006/relationships'
        ct = 'application/vnd.openxmlformats-officedocument.spreadsheetml'
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
            z.writestr('[Content_Types].xml', x +
                       '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                       '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                       '<Default Extension="xml" ContentType="application/xml"/>'
                       '<Override PartName="/xl/workbook.xml" ContentType="%s.sheet.main+xml"/>' % ct +
                       ''.join('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="%s.worksheet+xml"/>'
                               % (i + 1, ct) for i in range(n)) +
                       '<Override PartName="/xl/styles.xml" ContentType="%s.styles+xml"/></Types>' % ct)
            z.writestr('_rels/.rels', x +
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                       '<Relationship Id="rId1" Type="%s/officeDocument" Target="xl/workbook.xml"/>'
                       '</Relationships>' % rel)
            z.writestr('xl/workbook.xml', x +
                       '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="%s">'
                       '<sheets>' % rel +
                       ''.join('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (_xml(s.name), i + 1, i + 1)
                               for i, s in enumerate(self.sheets)) +
                       '</sheets></workbook>')
            z.writestr('xl/_rels/workbook.xml.rels', x +
                       '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' +
                       ''.join('<Relationship Id="rId%d" Type="%s/worksheet" Target="worksheets/sheet%d.xml"/>'
                               % (i + 1, rel, i + 1) for i in range(n)) +
                       '<Relationship Id="rId%d" Type="%s/styles" Target="styles.xml"/></Relationships>' % (n + 1, rel))
            z.writestr('xl/styles.xml', self.STYLES)
            for i, s in enumerate(self.sheets):
                z.writestr('xl/worksheets/sheet%d.xml' % (i + 1), s.xml())
        return buf.getvalue()


def xlsx_response(book, filename):
    """XLSX-файл для скачивания (имя в UTF-8)."""
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename) or 'export.xlsx'
    resp = Response(book.to_bytes(), mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    resp.headers['Content-Disposition'] = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, quote(filename))
    return resp


def _stamp():
    return now_almaty().strftime('%Y%m%d_%H%M')


def _lesson_cell(l, show='teacher'):
    """Текст ячейки: время, [Ч]/[З] дисциплина, преподаватель/группа | аудитория."""
    mark = {'numerator': '[Ч] ', 'denominator': '[З] '}.get(l['week'], '')
    who = teacher_label(l) if show == 'teacher' else l['groupId']
    tail = ' | '.join(x for x in (who, l['room']) if x)
    return '%s\n%s%s%s' % (lesson_time(l), mark, l['subject'], ('\n' + tail) if tail else '')


def _grid_sheet(book, title, owners, by_owner, show, cur=None, owner_label='Группа'):
    """Лист «владелец × день» (владелец — группа или преподаватель); cur — только эта неделя."""
    sh = book.sheet(title)
    cols = list(range(6))
    if any(6 in days for days in by_owner.values()):
        cols.append(6)
    sh.row(1, [owner_label] + [DAY_NAMES_FULL[d] for d in cols], XlsxBook.S_HEADER)
    sh.widths[1] = 24
    for i in range(len(cols)):
        sh.widths[i + 2] = 32
    sh.freeze = (2, 2)
    r = 2
    for owner, label in owners:
        days = by_owner.get(owner, {})
        if cur:
            sh.cell(r, 1, label, XlsxBook.S_NAME)
            for i, d in enumerate(cols):
                ls = sorted((l for l in days.get(d, []) if l['week'] in ('always', cur)), key=sm.lesson_sort_key)
                sh.cell(r, i + 2, '\n\n'.join(_lesson_cell(l, show) for l in ls), XlsxBook.S_WRAP)
            r += 1
            continue
        height = max([len(days.get(d, [])) for d in cols] + [1])
        for k in range(height):
            sh.cell(r + k, 1, label if k == 0 else '', XlsxBook.S_NAME)
        sh.merge(r, 1, r + height - 1, 1)
        for i, d in enumerate(cols):
            ls = sorted(days.get(d, []), key=sm.lesson_sort_key)
            for k in range(height):
                if k < len(ls):
                    sh.cell(r + k, i + 2, _lesson_cell(ls[k], show), XlsxBook.week_style(ls[k]['week']))
                else:
                    sh.cell(r + k, i + 2, '', XlsxBook.S_WRAP)
        r += height
    return sh


DETAIL_HEADER = ['Группа', 'День', 'Время', 'Дисциплина', 'Преподаватель', 'Аудитория', 'Неделя']


def _day_label(l):
    return DAY_NAMES_FULL[l['weekday']] if l['weekday'] is not None else 'не указан'


def _detail_row(l):
    return [l['groupId'], _day_label(l), lesson_time(l), l['subject'], teacher_label(l), l['room'],
            WEEK_LABELS.get(l['week'], l['week'])]


@routes.route('/schedule/all/export')
@admin_required
def schedule_all_export():
    """Экспорт расписания групп с фильтрами страницы (group, course, major, week, day, q):
    format=xlsx (по умолчанию: сетка, текущая неделя, детально) | csv | pdf (→ страница печати:
    «Сохранить как PDF» в браузере — кириллица не теряется)."""
    if not request.args.get('format'):  # старые ссылки без формата → явный xlsx (файл, а не страница)
        return redirect(url_with(format='xlsx'))
    fmt = request.args.get('format')
    group = (request.args.get('group') or '').strip()
    course = (request.args.get('course') or '').strip()
    major = (request.args.get('major') or '').strip()
    week = week_param()
    if fmt == 'pdf':
        return redirect(url_for('schedule_print', group=group or None, course=course or None, major=major or None,
                                week='current' if week == 'current' else None))
    cur = current_week_type()
    lessons = _filtered_lessons(load_lessons_snapshot(), cur, group=group or None, course=course, major=major,
                                q=(request.args.get('q') or '').strip(), week=week, day=day_param())
    lessons.sort(key=lambda l: (group_sort_key(l['groupId']), l['groupId'], 9 if l['weekday'] is None else l['weekday'],
                                9999 if l['start'] is None else l['start'], l['subject']))
    suffix = ('_' + group) if group else ''
    if fmt == 'csv':
        return csv_response('расписание%s_%s.csv' % (suffix, _stamp()), DETAIL_HEADER, [_detail_row(l) for l in lessons])
    by_group = {}
    for l in lessons:
        if l['weekday'] is not None:
            by_group.setdefault(l['groupId'], {}).setdefault(l['weekday'], []).append(l)
    owners = [(x, x) for x in sorted(by_group, key=lambda x: (group_sort_key(x), x))]
    book = XlsxBook()
    _grid_sheet(book, 'Все группы', owners, by_group, 'teacher')
    _grid_sheet(book, 'Текущая неделя (%s)' % WEEK_LABELS[cur].lower(), owners, by_group, 'teacher', cur=cur)
    sh = book.sheet('Детально')
    sh.row(1, DETAIL_HEADER, XlsxBook.S_HEADER)
    sh.freeze = (2, 1)
    for i, w in enumerate([16, 14, 14, 32, 30, 12, 16]):
        sh.widths[i + 1] = w
    for r, l in enumerate(lessons, start=2):
        sh.row(r, _detail_row(l), XlsxBook.week_style(l['week']))
    return xlsx_response(book, 'расписание%s_%s.xlsx' % (suffix, _stamp()))


# ==================== ПО ПРЕПОДАВАТЕЛЯМ ====================

def _teacher_key_param(value):
    """?teacher= : ключ 'uid:…'/'name:…' или старый формат — просто uid."""
    v = (value or '').strip()
    if not v:
        return ''
    return v if v.startswith(('uid:', 'name:')) else 'uid:' + v


def _teacher_buckets(snap, cur, key='', q='', day=None, week=''):
    """{teacher_key: {...}} по фильтрам (ФИО текстом и uid одного человека — одна карточка)."""
    qn = norm_text(q)
    out = {}
    for tkey, lessons in snap.by_teacher.items():
        if key and tkey != key:
            continue
        name = teacher_label(lessons[0]) or tkey
        if qn and qn not in norm_text(name):
            continue
        sel = [l for l in lessons if week_matches(l['week'], week, cur) and (day is None or l['weekday'] == day)]
        if not sel:
            continue
        hints = sorted({_teacher_hint(l) for l in lessons if _teacher_hint(l)})
        out[tkey] = {'key': tkey, 'name': name, 'hints': hints, 'lessons': sorted(sel, key=sm.lesson_sort_key),
                     'all': lessons}
    return out


def _week_minutes(lessons, cur):
    return sum((l['end'] - l['start']) for l in lessons if l['time_ok'] and l['week'] in ('always', cur))


@routes.route('/schedule/teachers')
@admin_required
def schedule_teachers():
    """По преподавателям: карточки или таблица «преподаватель × день», нагрузка, подсветка
    пересечений (подробно — раздел «Накладки»), фильтры, поиск, постраничный вывод, экспорт."""
    wk = week_context()
    cur = wk['current_week']
    snap = load_lessons_snapshot()
    key = _teacher_key_param(request.args.get('teacher'))
    q = (request.args.get('q') or '').strip()
    day = day_param()
    week = week_param()
    # по умолчанию — таблица (SCH-7); выбор запоминается (ui.segmented(remember='schedule_teachers'))
    view = view_param('schedule_teachers', ('table', 'cards'), 'table')
    marks, _cg, conf_teachers, engine_ok = conflict_marks(with_status=True)
    known = get_all_teachers()
    cards = []
    total_overlap = 0
    for tkey, b in _teacher_buckets(snap, cur, key, q, day, week).items():
        # запасная подсветка пересечений — только если движок накладок недоступен (принятые он не учитывает)
        ids, pairs = _overlap_ids(b['all']) if not engine_ok else (set(), 0)
        ids = set(ids) | {l['id'] for l in b['all'] if l['id'] in marks}
        info = conf_teachers.get(tkey) or {}
        conf = info.get('critical', 0) + info.get('warning', 0)
        total_overlap += conf or pairs
        lessons = b['lessons']
        uid = tkey[4:] if tkey.startswith('uid:') else ''
        removed = bool(uid) and uid not in known and 'uid не найден' in b['hints']
        cards.append({
            'key': tkey, 'uid': uid, 'name': 'Удалённый преподаватель' if removed else b['name'], 'removed': removed,
            'raw': b['name'], 'hints': [h for h in b['hints'] if not (removed and h == 'uid не найден')],
            'lessons': lessons, 'by_day': _by_day(lessons),
            'days': Counter(l['weekday'] for l in lessons if l['weekday'] is not None),
            'overlaps': [l for l in lessons if l['id'] in ids],
            'groups': sorted({l['groupId'] for l in lessons if l['groupId']}, key=group_sort_key),
            'num': sum(1 for l in lessons if l['week'] in ('always', 'numerator')),
            'den': sum(1 for l in lessons if l['week'] in ('always', 'denominator')),
            'current': sum(1 for l in lessons if l['week'] in ('always', cur)),
            'hours': round(_week_minutes(lessons, cur) / 60.0, 1), 'overlap_ids': ids, 'overlap_pairs': pairs,
            'conflicts': conf or pairs, 'conflicts_critical': info.get('critical', 0),
            'today': sum(1 for l in lessons if l['weekday'] == wk['today_wd'] and lesson_is_current(l, cur)),
            'ids': [l['id'] for l in b['all']],
        })
    cards.sort(key=lambda c: (c['removed'], norm_name(c['name'])))
    single = cards[0] if key and len(cards) == 1 else None
    per_page = TEACHERS_PER_PAGE if view == 'cards' else TEACHERS_PER_PAGE * 2
    page = paginate_list(cards, request.args.get('page', 1), per_page)
    columns = [day] if day is not None else _day_columns([l for c in cards for l in c['lessons']])
    return render_template(
        'schedule_teachers.html', page=page, teachers_count=len(cards), single=single,
        total_lessons=sum(len(c['lessons']) for c in cards), total_overlap=total_overlap,
        teachers_with_conflicts=sum(1 for c in cards if c['conflicts']),
        no_teacher=sum(1 for l in snap.lessons if not l['teacher_key']), teacher_options=snap.teachers(),
        teacher=key, q=q, day=day, week=week, view=view, columns=columns, marks=marks,
        focus_day=focus_day(columns, wk['today_wd']), teacher_choices=list(known.items()),
        week_filters=WEEK_FILTERS, day_names=DAY_NAMES_FULL, day_short=DAY_SHORT,
        day_options=[(d, DAY_NAMES_FULL[d]) for d in range(7)],
        conflicts_url=_schedule_conflicts_url(), now_minutes=_now_minutes(), update_time=fmt_dt(now_almaty()), **wk)


@routes.route('/schedule/teachers/export')
@admin_required
def schedule_teachers_export():
    """Экспорт по преподавателям (фильтры страницы): xlsx (сводка, текущая неделя, детально,
    нагрузка) или csv."""
    if not request.args.get('format'):  # старые ссылки без формата → явный xlsx
        return redirect(url_with(format='xlsx'))
    fmt = request.args.get('format')
    cur = current_week_type()
    buckets = _teacher_buckets(load_lessons_snapshot(), cur, _teacher_key_param(request.args.get('teacher')),
                               request.args.get('q') or '', day_param(), week_param())
    ordered = sorted(buckets.values(), key=lambda b: norm_name(b['name']))
    header = ['Преподаватель', 'День', 'Время', 'Дисциплина', 'Группа', 'Аудитория', 'Неделя']

    def row(name, l):
        return [name, _day_label(l), lesson_time(l), l['subject'], l['groupId'], l['room'],
                WEEK_LABELS.get(l['week'], l['week'])]

    if fmt == 'csv':
        return csv_response('преподаватели_%s.csv' % _stamp(), header,
                            [row(b['name'], l) for b in ordered for l in b['lessons']])
    book = XlsxBook()
    by_owner = {b['key']: {} for b in ordered}
    for b in ordered:
        for l in b['lessons']:
            if l['weekday'] is not None:
                by_owner[b['key']].setdefault(l['weekday'], []).append(l)
    owners = [(b['key'], b['name']) for b in ordered]
    _grid_sheet(book, 'Все преподаватели', owners, by_owner, 'group', owner_label='Преподаватель')
    _grid_sheet(book, 'Текущая неделя (%s)' % WEEK_LABELS[cur].lower(), owners, by_owner, 'group', cur=cur,
                owner_label='Преподаватель')
    sh = book.sheet('Детально')
    sh.row(1, header, XlsxBook.S_HEADER)
    sh.freeze = (2, 1)
    for i, w in enumerate([30, 14, 14, 32, 14, 12, 16]):
        sh.widths[i + 1] = w
    r = 2
    for b in ordered:
        for l in b['lessons']:
            sh.row(r, row(b['name'], l), XlsxBook.week_style(l['week']))
            r += 1
    load = book.sheet('Нагрузка')
    load.row(1, ['Преподаватель', 'Пар (числитель)', 'Пар (знаменатель)', 'Часов на текущей неделе', 'Групп', 'Группы'],
             XlsxBook.S_HEADER)
    load.freeze = (2, 1)
    for i, w in enumerate([30, 16, 18, 22, 8, 50]):
        load.widths[i + 1] = w
    for r, b in enumerate(ordered, start=2):
        ls = b['lessons']
        groups = sorted({l['groupId'] for l in ls if l['groupId']}, key=group_sort_key)
        load.row(r, [b['name'], sum(1 for l in ls if l['week'] in ('always', 'numerator')),
                     sum(1 for l in ls if l['week'] in ('always', 'denominator')),
                     round(_week_minutes(ls, cur) / 60.0, 1), len(groups), ', '.join(groups)], XlsxBook.S_CELL)
    return xlsx_response(book, 'преподаватели_%s.xlsx' % _stamp())


# ==================== ПЕЧАТЬ ====================
# Бланк — лист A4 (книжная) как старый бумажный: «Расписание занятий», таблица [день вертикально][время]
# [дисциплина / преподаватель][ауд.], ФИО жирным. @page без полей (Chrome не печатает колонтитулы с адресом),
# поля 12 мм — внутри листа: принтер с непечатаемыми полями 4–5 мм ничего не срежет.
# Один владелец (группа, преподаватель) — ВСЕГДА один лист: плотность (шрифт, отступы) и высота строки
# считаются здесь по числу строк; страховка на длинные названия — schedule.js ужимает лист (zoom).

# С этого числа строк таблица растягивается на всю высоту листа, как старый бланк (sch-sheet--fill); при 1–7
# парах строки остаются по высоте класса — одна пара на весь лист выглядела бы пустой рамкой.
PRINT_FILL_ROWS = 8
# Высота под строки пар, мм: 297 − 2×12 (поля) − шапка, заголовок таблицы и легенда (≈ 29 мм) − запас; у бланка
# преподавателя в шапке ещё строка с ФИО.
PRINT_BODY_MM = {'group': 238.0, 'teacher': 232.0}
# (класс плотности, до строк, высота строки по содержимому мм, потолок высоты строки мм, кегль pt) — по мерке
# Chromium (tests/run_print.sh). До dense дисциплина и ФИО — двумя строками, как старый бланк; tight/ultra —
# одной строкой через «—». Берётся первый класс, в который строки влезают и по числу, и по высоте.
PRINT_DENSITY = (
    ('roomy', 12, 14.7, 19.0, 14),
    ('normal', 18, 12.8, 14.0, 13),
    ('compact', 24, 10.3, 11.0, 10.5),
    ('dense', 28, 8.6, 9.0, 10),
    ('tight', 40, 6.0, 7.0, 10.5),
    ('ultra', None, 4.6, 6.0, 9.5),
)


def sheet_layout(by_day, kind='group'):
    """Плотность бланка: {'density', 'row_mm', 'rows', 'fill'}.

    Мало пар — крупный шрифт и строки выше (row_mm — минимум; с PRINT_FILL_ROWS строк таблица растянута на
    весь лист, fill), много — плотнее (шрифт и отступы меньше, с 29 строк дисциплина и преподаватель — в одну
    строку), чтобы владелец ВСЕГДА помещался на один лист. День пишется в своей колонке горизонтально и целиком."""
    body = PRINT_BODY_MM.get(kind, PRINT_BODY_MM['group'])
    rows = sum(len(ls) for ls in by_day.values())
    density, _limit, natural, cap, _pt = PRINT_DENSITY[-1]
    for tier in PRINT_DENSITY:
        if (tier[1] is None or rows <= tier[1]) and rows * tier[2] <= body:
            density, _limit, natural, cap, _pt = tier
            break
    row = min(cap, max(natural, body / max(rows, 1)))
    return {'density': density, 'row_mm': '%.1f' % row, 'rows': rows, 'fill': rows >= PRINT_FILL_ROWS}


def _print_week():
    """?week=current — только текущая неделя (пометки Ч/З тогда не нужны)."""
    return 'current' if (request.args.get('week') or '').strip() == 'current' else ''


def _week_dates(today):
    """'19.10–25.10.2026' — неделя (Пн–Вс) даты today."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    return '%s–%s' % (monday.strftime('%d.%m'), sunday.strftime('%d.%m.%Y'))


def _print_context(week, wk):
    return dict(week=week, day_names=DAY_NAMES_FULL, generated=fmt_dt(now_almaty()),
                week_dates=_week_dates(_today()), **wk)


def _stream_rows(lessons):
    """Бланк преподавателя: одна пара у нескольких групп (поток — тот же день, время, дисциплина,
    аудитория и неделя) — одна строка «ВТ 24-1, ВТ 25-1п»."""
    out, seen = [], {}
    for l in sorted(lessons, key=sm.lesson_sort_key):
        k = (l['weekday'], l['start'], l['end'], lesson_time(l), norm_text(l['subject']),
             l['room_key'] or norm_text(l['room']), l['week'])
        if k in seen:
            row = seen[k]
            if l['groupId'] and l['groupId'] not in row['groups']:
                row['groups'].append(l['groupId'])
            continue
        row = dict(l, groups=[l['groupId']] if l['groupId'] else [])
        seen[k] = row
        out.append(row)
    for row in out:
        row['groups'].sort(key=group_sort_key)
    return out


@routes.route('/schedule/print')
@admin_required
def schedule_print():
    """Бланки расписания групп для печати (A4, одна группа — один лист). Фильтры: group
    (можно несколько), course, major, q, week=current — только текущая неделя. Несуществующая
    группа — 404 с пустым состоянием."""
    wk = week_context()
    cur = wk['current_week']
    snap = load_lessons_snapshot()
    selected = [x.strip() for x in request.args.getlist('group') if x.strip()]
    course = (request.args.get('course') or '').strip()
    major = (request.args.get('major') or '').strip()
    q = (request.args.get('q') or '').strip()
    week = _print_week()
    known = set(get_all_groups()) | set(snap.groups())
    missing = [x for x in selected if x not in known]
    groups = [x for x in selected if x in known] if selected else _filter_groups(snap.groups(), q, course, major)
    sheets = []
    for gname in sorted(groups, key=lambda x: (group_sort_key(x), x)):
        lessons = [l for l in snap.by_group.get(gname, [])
                   if week_matches(l['week'], week, cur) and l['weekday'] is not None]
        if lessons or selected:
            by_day = _by_day(lessons)
            sheets.append({'name': gname, 'by_day': by_day, 'count': len(lessons), 'layout': sheet_layout(by_day)})
    status = 404 if selected and not sheets else 200
    return render_template('schedule_print.html', kind='group', sheets=sheets, course=course, major=major, q=q,
                           selected=selected, missing=missing, **_print_context(week, wk)), status


def _print_teacher_key(raw, snap):
    """?teacher= : uid, ключ 'uid:…'/'name:…' или ФИО → ключ снапшота (как API преподавателя);
    преподаватель из справочника без занятий — его ключ (пустой бланк); неизвестный — ''."""
    key = _teacher_key_param(raw)
    if key in snap.by_teacher:
        return key
    resolved = resolve_teacher(raw, snap.teacher_index)['key'] or ''
    if resolved in snap.by_teacher:
        return resolved
    for k in (key, resolved):
        if k.startswith('uid:') and snap.teacher_index.name(k[4:]):
            return k
    return ''


@routes.route('/schedule/print/teacher')
@admin_required
def schedule_print_teacher():
    """Бланки преподавателей для печати (A4, один преподаватель — один лист). ?teacher= — uid, ключ
    или ФИО (можно несколько); без него — все преподаватели из расписания (поиск ?q= по ФИО).
    week=current — только текущая неделя. ФИО и занятия — как на странице преподавателя (снапшот,
    uid и ФИО текстом — один человек; поток у нескольких групп — одна строка)."""
    set_nav_active('schedule_teachers')
    wk = week_context()
    cur = wk['current_week']
    snap = load_lessons_snapshot()
    week = _print_week()
    q = (request.args.get('q') or '').strip()
    selected = [x.strip() for x in request.args.getlist('teacher') if x.strip()]
    keys, missing = [], []
    for raw in selected:
        key = _print_teacher_key(raw, snap)
        if not key:
            missing.append(raw)
        elif key not in keys:
            keys.append(key)
    if not selected:
        qn = norm_text(q)
        keys = [k for k, name in snap.teachers() if not qn or qn in norm_text(name)]
    known = get_all_teachers()
    sheets = []
    for key in keys:
        all_lessons = snap.by_teacher.get(key, [])
        uid = key[4:] if key.startswith('uid:') else ''
        name = ((teacher_label(all_lessons[0]) if all_lessons else '') or snap.teacher_index.name(uid)
                or key.split(':', 1)[-1])
        if uid and uid not in known and name == uid:
            name = 'Удалённый преподаватель (id %s)' % uid
        lessons = [l for l in all_lessons if week_matches(l['week'], week, cur) and l['weekday'] is not None]
        if not lessons and not selected:
            continue
        by_day = _by_day(_stream_rows(lessons))
        sheets.append({'name': name, 'key': key, 'by_day': by_day, 'count': len(lessons),
                       'layout': sheet_layout(by_day, 'teacher')})
    if not selected:
        sheets.sort(key=lambda s: norm_name(s['name']))
    status = 404 if selected and not sheets else 200
    return render_template('schedule_print.html', kind='teacher', sheets=sheets, q=q, selected=selected,
                           missing=missing, **_print_context(week, wk)), status


# ==================== API ====================

@routes.route('/api/schedule/check', methods=['POST'])
@admin_required
def api_schedule_check():
    """Проверка накладок для занятий из формы (конструктор, редактор группы) через
    cms.conflicts.check_lesson. Тело: {lessons:[{key, id?, groupId, day, time, time_custom?, subject,
    teacher, room, week_type}], exclude_ids:[…]}. Ответ: {results:[{key, conflicts:[{severity, text}]}]}."""
    payload = request.get_json(silent=True) or {}
    lessons = payload.get('lessons') if isinstance(payload.get('lessons'), list) else []
    exclude = {str(x) for x in (payload.get('exclude_ids') or []) if x}
    results = []
    for i, l in enumerate(lessons[:80]):
        if not isinstance(l, dict):
            continue
        key = str(l.get('key') or l.get('id') or i)
        data, _errors = lesson_from_input(l)
        start, end = parse_time_range(data['time'])
        if data['day'] is None or start is None or not data['groupId']:
            results.append({'key': key, 'conflicts': []})
            continue
        doc_id = str(l.get('id') or '') or None
        doc = lesson_doc(data)
        if doc_id:
            doc['id'] = doc_id
        items = check_conflicts(doc, exclude_id=doc_id, extra_exclude=exclude)
        results.append({'key': key, 'conflicts': [{'severity': x['severity'], 'text': x['text']} for x in items]})
    return jsonify({'success': True, 'results': results})


@routes.route('/api/schedule/group/<group_id>')
@admin_required
def api_get_group_schedule(group_id):
    """Расписание группы (JSON) — из снапшота; день — по правилам приложения."""
    items = [_lesson_json(l) for l in load_lessons_snapshot().by_group.get(group_id, [])]
    return jsonify({'success': True, 'group_id': group_id, 'schedule': items, 'count': len(items),
                    'current_week': current_week_type()})


@routes.route('/api/cache/invalidate', methods=['POST'])
@admin_required
def api_invalidate_cache():
    """Принудительный сброс кэша (справочники, снапшоты, бейджи)."""
    cache.invalidate()
    audit('cache_invalidate', 'cache')
    return jsonify({'success': True, 'message': 'Кэш очищен'})


@routes.route('/api/schedule/all')
@admin_required
def api_schedule_all():
    """Всё расписание: {группа: {день 0..6: [занятия]}}."""
    data = OrderedDict()
    for l in load_lessons_snapshot().lessons:
        if l['groupId'] and l['weekday'] is not None:
            data.setdefault(l['groupId'], {}).setdefault(l['weekday'], []).append(_lesson_json(l))
    return jsonify({'success': True, 'is_even_week': get_week_parity(), 'current_week': current_week_type(),
                    'schedule': data, 'groups_count': len(data), 'updated_at': now_almaty().isoformat()})


@routes.route('/api/schedule/teacher/<teacher_id>')
@admin_required
def api_get_teacher_schedule(teacher_id):
    """Расписание преподавателя: занятия, записанные и uid, и ФИО текстом (один ключ)."""
    snap = load_lessons_snapshot()
    key = _teacher_key_param(teacher_id)
    lessons = snap.by_teacher.get(key)
    if lessons is None:
        key = resolve_teacher(teacher_id, snap.teacher_index)['key'] or key
        lessons = snap.by_teacher.get(key, [])
    items = [_lesson_json(l) for l in lessons]
    days = OrderedDict()
    for it in items:
        if it['day_index'] is not None:
            days.setdefault(it['day_index'], []).append(it)
    name = teacher_label(lessons[0]) if lessons else (snap.teacher_index.name(teacher_id) or teacher_id)
    return jsonify({'success': True, 'teacher_id': teacher_id, 'teacher_key': key, 'teacher_name': name,
                    'schedule': items, 'days': days, 'groups': sorted({it['groupId'] for it in items if it['groupId']}),
                    'total_lessons': len(items), 'is_even_week': get_week_parity(),
                    'current_week': current_week_type()})
