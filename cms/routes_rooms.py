# cms/routes_rooms.py — аудиторный фонд, накладки, свободные аудитории (владелец — CONFLICTS).
#
# Три вкладки одной страницы (общий партиал templates/rooms/_tabs.html):
#   /schedule/rooms       — «Аудиторный фонд»: аудитории, их неделя, загрузка, что идёт сейчас;
#   /schedule/conflicts   — «Накладки»: Преподаватели (по умолчанию) | Аудитории | Группы | Качество данных;
#   /schedule/free-rooms  — «Свободные аудитории»: день + пара (или своё время) + неделя → свободные.
# Данные — общий снапшот расписания (schedule_model.load_lessons_snapshot, кэш) и движок
# cms/conflicts.py (анализ кэшируется вместе со снапшотом). Полных сканов schedules на запрос нет.
# Мутации (принять/вернуть накладку, массовое исправление данных, пересчёт) — POST + CSRF + аудит.

import io
import os
import re
from collections import OrderedDict
from urllib.parse import quote

from flask import Response, flash, jsonify, redirect, render_template, request, url_for

from . import conflicts
from .conflicts import DIM_LABELS, DIMS, FIXABLE, KINDS, QUALITY, SEV_LABELS, SEVERITIES, occurs_in, week_bounds
from .core import (Routes, admin_required, audit, csv_response, current_admin, flash_action, flash_error, fmt_dt, log,
                   now_almaty, paginate_list, plural, safe_next_url, url_with, view_param)
from .schedule_model import (DAY_NAMES_FULL, DAY_SHORT, WEEK_LABELS, academic_week_number, call_slots,
                             current_week_type, fmt_minutes, group_key, group_sort_key, intervals_overlap,
                             invalidate_schedule_caches, load_lessons_snapshot, norm_name, norm_text,
                             parse_lesson_date, parse_time_range, resolve_teacher, room_key, room_sort_key,
                             time_label)

routes = Routes()

WEEK_FILTERS = [('current', 'Текущая'), ('numerator', 'Числитель'), ('denominator', 'Знаменатель')]
# «Период» на странице «Накладки» (вместо отдельных кнопок «Сегодня / Эта неделя»)
PERIODS = OrderedDict([('today', 'Сегодня'), ('week', 'Эта неделя'), ('numerator', 'Числитель'),
                       ('denominator', 'Знаменатель')])
DAY_OPTIONS = [(i, DAY_NAMES_FULL[i]) for i in range(7)]
# Учебный день для расчёта загрузки аудитории: Пн–Сб, 08:00–18:00 (600 минут в день).
LOAD_DAY_START, LOAD_DAY_END, LOAD_DAYS = 8 * 60, 18 * 60, 6
ROOMS_PER_PAGE = {'cards': 12, 'table': 30, 'matrix': 200}
SERVICE_SLOT_MAX = 30   # слот сетки звонков короче 30 мин — служебный (перемена, кураторский час), а не пара
ENTITIES_PER_PAGE = 12
QUALITY_PER_PAGE = 50


# ==================== ОБЩЕЕ ====================

def week_context():
    """Текущая дата/неделя по Алматы для шапок и подсветки «идёт сейчас»."""
    now = now_almaty()
    today = now.date()
    cur = current_week_type(today)
    return {'now': now, 'today': today, 'today_wd': today.weekday(), 'now_minutes': now.hour * 60 + now.minute,
            'current_week': cur, 'current_week_label': WEEK_LABELS[cur],
            'week_number': academic_week_number(today), 'update_time': fmt_dt(now),
            'today_label': '%s, %s' % (DAY_NAMES_FULL[today.weekday()], today.strftime('%d.%m.%Y'))}


def _parse_day(value):
    try:
        d = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return d if 0 <= d <= 6 else None


def _parse_int(value, lo, hi):
    try:
        v = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return v if lo <= v <= hi else None


def _week_param(value):
    return value if value in ('current', 'numerator', 'denominator') else ''


def slot_items(day):
    """Сетка звонков дня для выбора пары: [{label, title, start, end, n, service}].
    n — номер пары по порядку («1 пара · 08:25–09:15»); service — служебный слот (перемена,
    кураторский час) — показывается отдельно и приглушённо."""
    out, n = [], 0
    for label, s, e in call_slots(day):
        service = e - s < SERVICE_SLOT_MAX
        if not service:
            n += 1
        out.append({'label': label, 'title': time_label(s, e), 'start': s, 'end': e,
                    'n': None if service else n, 'service': service})
    return out


def default_day(ctx):
    """День по умолчанию: сегодня, в воскресенье — понедельник."""
    return ctx['today_wd'] if ctx['today_wd'] <= 5 else 0


def lesson_in_week(l, week, ctx):
    """Занятие идёт в неделю фильтра ('' — любая): числитель/знаменатель включают «каждую неделю»
    (как applyFilter в приложении); 'current' — тип текущей недели. Разовое (one_off) — по своей дате."""
    if not week:
        return True
    wt = ctx['current_week'] if week == 'current' else week
    if conflicts.is_one_off(l):
        d = parse_lesson_date(l.get('date'))
        if d is None:
            return False
        if week == 'current':
            monday, sunday = week_bounds(ctx['today'])
            return monday <= d <= sunday
        return current_week_type(d) == wt
    return l['week'] in ('always', wt)


def _tab_counts():
    """Счётчики вкладок: критичные накладки (сводка движка, кэш)."""
    try:
        s = conflicts.summary() or {}
    except Exception:
        log.exception('conflicts.summary() упал')
        s = {}
    return {'conflicts': s.get('critical') or None}


def _safe_analysis():
    try:
        return conflicts.get_analysis()
    except Exception as e:
        flash_error('Не удалось загрузить расписание — показаны неполные данные. Обновите страницу.', e)
        return None


def _safe_acks():
    try:
        return conflicts.load_acks()
    except Exception:
        log.exception('Не удалось загрузить принятые накладки')
        return {}


def teacher_key_param(raw, snap=None):
    """?teacher= → ключ преподавателя: 'uid:…'/'name:…' как есть; голый uid или ФИО — через справочник."""
    raw = (raw or '').strip()
    if not raw:
        return ''
    if raw.startswith('uid:') or raw.startswith('name:'):
        return raw
    if snap is not None and ('uid:' + raw) in snap.by_teacher:
        return 'uid:' + raw
    try:
        index = snap.teacher_index if snap is not None else None
        return resolve_teacher(raw, index)['key'] or ''
    except Exception:
        return 'uid:' + raw


def _lessons_text(lessons, who='group'):
    """Занятия одной ячейки → текст для экспорта (по строке на занятие)."""
    out = []
    for l in lessons:
        mark = {'numerator': '[Ч] ', 'denominator': '[З] '}.get(l['week'], '')
        other = (l.get('teacher_name') or l.get('teacher') or '') if who == 'group' else l.get('groupId')
        out.append('%s %s%s · %s%s' % (l['time_label'] or l['time'], mark, l.get('groupId') or '—',
                                       l.get('subject') or '—', (' · ' + other) if other and who == 'group' else ''))
    return '\n'.join(out)


# ==================== XLSX: openpyxl (лениво) или встроенная запись ====================

class Sheet:
    """Лист экспорта: строки (значения, стиль) + ширины колонок. Стили — смысловые:
    title, header, wrap, critical, warning, info, numerator, denominator, always."""

    def __init__(self, title):
        self.title = re.sub(r'[\[\]:*?/\\]', ' ', title)[:31] or 'Лист'
        self.rows = []
        self.widths = {}
        self.freeze_row = None
        self.filter_row = None

    def add(self, values, style=None):
        self.rows.append((list(values), style))
        return len(self.rows)

    def header(self, values):
        n = self.add(values, 'header')
        self.freeze_row = n + 1
        self.filter_row = n
        return n


_FILLS = {'header': 'C24B00', 'critical': 'FDECEC', 'warning': 'FEF3C7', 'info': 'E3EEFB',
          'numerator': 'EAF2FF', 'denominator': 'FEF3C7', 'always': 'F1F3F5'}


def _xlsx_openpyxl(sheets):
    from openpyxl import Workbook  # ленивый импорт: в requirements его нет
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    thin = Side(style='thin', color='D1D5DB')
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for sh in sheets:
        ws = wb.create_sheet(sh.title)
        width = max([len(v) for v, _s in sh.rows] or [1])
        for r, (values, style) in enumerate(sh.rows, 1):
            for c, v in enumerate(values, 1):
                cell = ws.cell(row=r, column=c, value=v)
                if style == 'title':
                    cell.font = Font(bold=True, size=14)
                    continue
                cell.border = border
                cell.alignment = Alignment(vertical='top', wrap_text=True)
                if style == 'header':
                    cell.font = Font(bold=True, color='FFFFFF')
                    cell.alignment = Alignment(horizontal='center', vertical='center', wrap_text=True)
                if style in _FILLS:
                    cell.fill = PatternFill(start_color=_FILLS[style], end_color=_FILLS[style], fill_type='solid')
        for c, w in sh.widths.items():
            ws.column_dimensions[get_column_letter(c)].width = w
        if sh.freeze_row:
            ws.freeze_panes = 'A%d' % sh.freeze_row
        if sh.filter_row and len(sh.rows) > sh.filter_row:
            ws.auto_filter.ref = 'A%d:%s%d' % (sh.filter_row, get_column_letter(width), len(sh.rows))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_builtin(sheets):
    """Без openpyxl — минимальный XLSX из модуля расписания (zipfile + SpreadsheetML)."""
    from .routes_schedule import XlsxBook
    book = XlsxBook()
    style_map = {'title': XlsxBook.S_TITLE, 'header': XlsxBook.S_HEADER, 'numerator': XlsxBook.S_NUM,
                 'denominator': XlsxBook.S_DEN, 'always': XlsxBook.S_ALWAYS}
    for sh in sheets:
        out = book.sheet(sh.title)
        for r, (values, style) in enumerate(sh.rows, 1):
            out.row(r, values, style_map.get(style, 0 if style is None and r == 1 else XlsxBook.S_WRAP))
        out.widths = dict(sh.widths)
        if sh.freeze_row:
            out.freeze = (sh.freeze_row, 1)
    return book.to_bytes()


def xlsx_response(sheets, filename):
    """Книга XLSX: openpyxl, если установлен (автофильтр, цвета критичности), иначе встроенная запись."""
    try:
        data = _xlsx_openpyxl(sheets)
    except ImportError:
        data = _xlsx_builtin(sheets)
    resp = Response(data, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename) or 'export.xlsx'
    resp.headers['Content-Disposition'] = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, quote(filename))
    return resp


@routes.template_filter('short_name')
def short_name_filter(name):
    """'Петров Дмитрий Игоревич' → 'Петров Д. И.' (плитки занятий в аудиторном фонде)."""
    return conflicts.short_name(name)


# ==================== АУДИТОРНЫЙ ФОНД ====================

def _merge_busy(intervals):
    """Сумма минут объединения интервалов (потоки в одной аудитории не считаются дважды)."""
    total, cur_s, cur_e = 0, None, None
    for s, e in sorted(intervals):
        s, e = max(s, LOAD_DAY_START), min(e, LOAD_DAY_END)
        if e <= s:
            continue
        if cur_e is None or s > cur_e:
            if cur_e is not None:
                total += cur_e - cur_s
            cur_s, cur_e = s, e
        else:
            cur_e = max(cur_e, e)
    if cur_e is not None:
        total += cur_e - cur_s
    return total


def room_load(lessons, week):
    """Загрузка аудитории в неделю типа week: доля занятых минут Пн–Сб 08:00–18:00 (0..100)."""
    by_day = {}
    for l in lessons:
        if l['time_ok'] and l['weekday'] is not None and l['weekday'] < LOAD_DAYS and l['week'] in ('always', week):
            by_day.setdefault(l['weekday'], []).append((l['start'], l['end']))
    busy = sum(_merge_busy(v) for v in by_day.values())
    return round(100.0 * busy / (LOAD_DAYS * (LOAD_DAY_END - LOAD_DAY_START)), 1)


def room_now(lessons, ctx):
    """Что сейчас в аудитории: {'state': 'busy'|'free'|'closed', 'lesson', 'until', 'next', 'ids'}
    (ids — занятия, идущие прямо сейчас: сегодня, в текущую неделю, по времени Алматы)."""
    today = [l for l in lessons if l['weekday'] == ctx['today_wd'] and l['time_ok']
             and lesson_in_week(l, 'current', ctx)]
    now = ctx['now_minutes']
    current = [l for l in today if l['start'] <= now < l['end']]
    if current:
        cur = max(current, key=lambda l: l['end'])
        return {'state': 'busy', 'lesson': cur, 'until': fmt_minutes(cur['end']), 'ids': {l['id'] for l in current}}
    upcoming = sorted([l for l in today if l['start'] > now], key=lambda l: l['start'])
    if upcoming:
        return {'state': 'free', 'next': upcoming[0], 'until': fmt_minutes(upcoming[0]['start']), 'ids': set()}
    return {'state': 'closed', 'ids': set()}


def _room_conflicts(analysis, acks):
    """{room_key: {'count': n, 'ids': {id занятий}}} — накладки аудиторий (без «инфо» и принятых)."""
    out = {}
    if analysis is None:
        return out
    for c in analysis.clusters['room']:
        if c['severity'] == 'info' or c['hash'] in acks:
            continue
        e = out.setdefault(c['key'], {'count': 0, 'ids': set()})
        e['count'] += 1
        e['ids'].update(c['ids'])
    return out


def build_rooms(snap, ctx, analysis=None, acks=None, q='', room='', day=None, week=''):
    """Аудитории снапшота → [{key, name, lessons, by_day, total, num, den, load, groups, teachers,
    now, conflicts, conflict_ids}] с фильтрами (поиск, аудитория, день, неделя)."""
    acks = acks or {}
    rc = _room_conflicts(analysis, acks)
    want_key = room_key(room) if room else None
    qn = norm_text(q)
    words = qn.split()
    rooms = []
    for key, name in snap.rooms():
        if want_key and key != want_key:
            continue
        all_lessons = snap.by_room.get(key, [])
        lessons = [l for l in all_lessons if (day is None or l['weekday'] == day) and lesson_in_week(l, week, ctx)]
        if words:
            name_hit = all(w in norm_text(name) or w in key for w in words)
            if not name_hit:
                lessons = [l for l in lessons if all(w in norm_text(' '.join(
                    (l['groupId'], l['subject'], l['teacher_name'] or l['teacher'], l['room']))) for w in words)]
                if not lessons:
                    continue
        by_day = OrderedDict()
        for l in sorted(lessons, key=lambda x: (x['weekday'] if x['weekday'] is not None else 9,
                                                  x['start'] if x['start'] is not None else 9999)):
            by_day.setdefault(l['weekday'], []).append(l)
        conf = rc.get(key, {'count': 0, 'ids': set()})
        # Колонки карточки: Пн–Сб (+ Вс, только если в эту аудиторию поставлены воскресные пары)
        columns = [day] if day is not None else list(range(6)) + ([6] if 6 in by_day else [])
        rooms.append({
            'key': key, 'name': name, 'lessons': lessons, 'by_day': by_day, 'columns': columns,
            'total': len(all_lessons),
            'num': sum(1 for l in all_lessons if l['week'] in ('always', 'numerator')),
            'den': sum(1 for l in all_lessons if l['week'] in ('always', 'denominator')),
            'load': room_load(all_lessons, ctx['current_week']),
            'groups': sorted({l['groupId'] for l in all_lessons if l['groupId']}, key=group_sort_key),
            'teachers': sorted({l['teacher_name'] or l['teacher'] for l in all_lessons
                                if l['teacher_name'] or l['teacher']}, key=norm_name),
            'now': room_now(all_lessons, ctx), 'conflicts': conf['count'], 'conflict_ids': conf['ids'],
        })
    return rooms


def _day_columns(rooms, day):
    if day is not None:
        return [day]
    cols = list(range(6))
    if any(6 in r['by_day'] for r in rooms):
        cols.append(6)
    return cols


@routes.route('/schedule/rooms')
@admin_required
def schedule_rooms():
    """Аудиторный фонд: аудитории колледжа — шахматка дня (по умолчанию), карточки недели или таблица.

    ?view=matrix|cards|table (запоминается: ui.segmented(remember='schedule_rooms'));
    ?q= — поиск (аудитория, группа, преподаватель, дисциплина); ?room= — одна аудитория (неделя);
    ?day=0..6 (в шахматке по умолчанию — сегодня); ?week=current|numerator|denominator
    (старое ?week_type= тоже понимается); ?page=."""
    ctx = week_context()
    q = (request.args.get('q') or '').strip()[:80]
    room = (request.args.get('room') or '').strip()[:80]
    day = _parse_day(request.args.get('day'))
    week = _week_param(request.args.get('week') or request.args.get('week_type'))
    view = view_param('schedule_rooms', ('matrix', 'cards', 'table'), 'matrix')
    if room:
        view = 'cards'   # одна аудитория — её неделя
    rooms, total_rooms, room_options, no_room, virtual = [], 0, [], 0, 0
    analysis = _safe_analysis()
    acks = _safe_acks()
    snap = None
    try:
        snap = load_lessons_snapshot()
        all_rooms = build_rooms(snap, ctx, analysis, acks)
        total_rooms = len(all_rooms)
        room_options = [(r['name'], r['name']) for r in all_rooms]
        rooms = build_rooms(snap, ctx, analysis, acks, q=q, room=room, day=day if view != 'matrix' else None,
                            week=week) if (q or room or day is not None or week) else all_rooms
        no_room = sum(1 for l in snap.lessons if not l['room'])
        virtual = sum(1 for l in snap.lessons if l['room_virtual'])
    except Exception as e:
        flash_error('Не удалось загрузить расписание. Попробуйте обновить страницу.', e)
        all_rooms = []
    busy_now = sum(1 for r in all_rooms if r['now']['state'] == 'busy')
    avg_load = round(sum(r['load'] for r in all_rooms) / len(all_rooms), 1) if all_rooms else 0
    room_conf = sum(r['conflicts'] for r in all_rooms)
    page = paginate_list(rooms, request.args.get('page'), ROOMS_PER_PAGE[view])
    top_load = sorted(all_rooms, key=lambda r: -r['load'])[:5]
    # шахматка: аудитории × пары выбранного дня (по умолчанию — сегодня, текущая неделя)
    matrix_day = day if day is not None else default_day(ctx)
    matrix_week = week or 'current'
    grid_slots, grid_rows = [], []
    if view == 'matrix' and snap is not None:
        grid_slots, grid_rows = occupancy_grid(snap, matrix_day, matrix_week, ctx, page.items, lessons_only=True)
        conflict_ids = set()
        for r in page.items:
            conflict_ids |= r['conflict_ids']
        by_key = {r['key']: r for r in page.items}
        for row in grid_rows:
            row['room'] = by_key.get(row['key'])
            row['cells'] = [{'lessons': cell, 'conflict': any(l['id'] in conflict_ids for l in cell)} for cell in row['cells']]
    return render_template(
        'schedule_rooms.html', page=page, rooms_count=len(rooms), total_rooms=total_rooms,
        lessons_count=sum(len(r['lessons']) for r in rooms), busy_now=busy_now, avg_load=avg_load,
        room_conflicts=room_conf, no_room=no_room, virtual=virtual, top_load=top_load,
        columns=_day_columns(page.items, day), day_names=DAY_NAMES_FULL, day_short=DAY_SHORT,
        room_options=room_options, day_options=DAY_OPTIONS, week_filters=WEEK_FILTERS,
        q=q, room=room, day=day, week=week, view=view, tab_counts=_tab_counts(),
        matrix_day=matrix_day, matrix_week=matrix_week, grid_slots=grid_slots, grid_rows=grid_rows,
        week_label=_week_label(matrix_week, ctx), single=bool(room) and len(rooms) == 1, **ctx)


def _week_label(week, ctx):
    return {'current': 'текущая неделя (%s)' % ctx['current_week_label'].lower(), 'numerator': 'числитель',
            'denominator': 'знаменатель', 'both': 'обе недели', '': 'все недели'}.get(week, week)


@routes.route('/schedule/rooms/export')
@admin_required
def schedule_rooms_export():
    """Экспорт аудиторного фонда: ?format=xlsx|csv|pdf + те же фильтры, что на странице."""
    if not request.args.get('format'):  # старые ссылки без формата → явный xlsx
        return redirect(url_with(format='xlsx'))
    fmt = request.args.get('format')
    ctx = week_context()
    room = (request.args.get('room') or '').strip()[:80]
    q = (request.args.get('q') or '').strip()[:80]
    day = _parse_day(request.args.get('day'))
    week = _week_param(request.args.get('week') or request.args.get('week_type'))
    try:
        snap = load_lessons_snapshot()
        rooms = build_rooms(snap, ctx, _safe_analysis(), _safe_acks(), q=q, room=room, day=day, week=week)
    except Exception as e:
        flash_error('Не удалось подготовить экспорт. Попробуйте ещё раз.', e)
        return redirect(url_for('schedule_rooms'))
    stamp = ctx['now'].strftime('%Y%m%d_%H%M')
    audit('export', 'schedule_rooms', None, {'format': fmt, 'room': room or None, 'rooms': len(rooms)})
    if fmt == 'csv':
        rows = []
        for r in rooms:
            for l in r['lessons']:
                rows.append([r['name'], l['day_name'], l['time_label'] or l['time'], l['subject'], l['groupId'],
                             l['teacher_name'] or l['teacher'], WEEK_LABELS.get(l['week'], ''),
                             'да' if l['id'] in r['conflict_ids'] else '', l['room'] if l['room'] != r['name'] else ''])
        return csv_response('аудиторный_фонд_%s.csv' % stamp,
                            ['Аудитория', 'День', 'Время', 'Дисциплина', 'Группа', 'Преподаватель', 'Неделя',
                             'Накладка', 'Записано в занятии как'], rows)
    if fmt == 'pdf':
        return _rooms_pdf(rooms, ctx, stamp)
    try:
        return xlsx_response(_rooms_sheets(rooms, ctx), 'аудиторный_фонд_%s.xlsx' % stamp)
    except Exception as e:
        flash_error('Не удалось сформировать Excel — скачайте CSV.', e)
        return redirect(url_for('schedule_rooms'))


def _rooms_sheets(rooms, ctx):
    cols = _day_columns(rooms, None)
    days_header = [DAY_NAMES_FULL[d] for d in cols]
    grid = Sheet('Все аудитории')
    grid.header(['Аудитория'] + days_header)
    cur = Sheet('Текущая неделя')
    cur.add(['Аудитории на текущую неделю (%s, %d-я неделя)' % (ctx['current_week_label'].lower(),
                                                             ctx['week_number'])], 'title')
    cur.header(['Аудитория'] + days_header)
    for r in rooms:
        grid.add([r['name']] + [_lessons_text(r['by_day'].get(d, [])) for d in cols], 'wrap')
        cur.add([r['name']] + [_lessons_text([l for l in r['by_day'].get(d, [])
                                              if l['week'] in ('always', ctx['current_week'])]) for d in cols], 'wrap')
    for sh in (grid, cur):
        sh.widths = {1: 16}
        sh.widths.update({i + 2: 34 for i in range(len(cols))})
    load = Sheet('Загрузка')
    load.header(['Аудитория', 'Пар · числитель', 'Пар · знаменатель', 'Загрузка текущей недели, %', 'Групп',
                 'Преподавателей', 'Накладок'])
    for r in sorted(rooms, key=lambda x: -x['load']):
        load.add([r['name'], r['num'], r['den'], r['load'], len(r['groups']), len(r['teachers']), r['conflicts']],
                 'critical' if r['conflicts'] else None)
    load.widths = {1: 18, 2: 16, 3: 18, 4: 26, 5: 10, 6: 16, 7: 12}
    detail = Sheet('Детально')
    detail.header(['Аудитория', 'День', 'Время', 'Дисциплина', 'Группа', 'Преподаватель', 'Неделя', 'Накладка',
                   'Записано в занятии как'])
    for r in rooms:
        for l in r['lessons']:
            detail.add([r['name'], l['day_name'], l['time_label'] or l['time'], l['subject'], l['groupId'],
                        l['teacher_name'] or l['teacher'], WEEK_LABELS.get(l['week'], ''),
                        'да' if l['id'] in r['conflict_ids'] else '', l['room'] if l['room'] != r['name'] else ''],
                       l['week'])
    detail.widths = {1: 14, 2: 14, 3: 14, 4: 30, 5: 12, 6: 30, 7: 16, 8: 10, 9: 22}
    return [grid, cur, load, detail]


_PDF_FONTS = ('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
              '/usr/share/fonts/dejavu/DejaVuSans.ttf',
              '/Library/Fonts/Arial Unicode.ttf', 'C:/Windows/Fonts/arial.ttf')


def _rooms_pdf(rooms, ctx, stamp):
    """PDF текущей недели (reportlab — лениво; кириллица — шрифт DejaVu/Arial, если есть в системе)."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError:
        flash('PDF недоступен на сервере (нет reportlab) — используйте Excel или «Печать».', 'warning')
        return redirect(url_for('schedule_rooms'))
    font = 'Helvetica'
    for path in _PDF_FONTS:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont('CCSans', path))
                font = 'CCSans'
                break
            except Exception:
                log.warning('Шрифт %s не подключён', path, exc_info=True)
    styles = getSampleStyleSheet()
    for st in styles.byName.values():
        st.fontName = font
    cols = _day_columns(rooms, None)
    data = [['Аудитория'] + [DAY_SHORT[d] for d in cols]]
    for r in rooms:
        row = [r['name']]
        for d in cols:
            ls = [l for l in r['by_day'].get(d, []) if l['week'] in ('always', ctx['current_week'])]
            row.append('\n'.join('%s %s (%s)' % (l['time_label'], (l['subject'] or '')[:18], l['groupId'])
                                 for l in ls) or '—')
        data.append(row)
    out = io.BytesIO()
    doc = SimpleDocTemplate(out, pagesize=landscape(A4), rightMargin=1 * cm, leftMargin=1 * cm,
                            topMargin=1 * cm, bottomMargin=1 * cm)
    table = Table(data, colWidths=[3 * cm] + [(24.7 * cm) / len(cols)] * len(cols), repeatRows=1)
    table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), font),
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#C24B00')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('FONTSIZE', (0, 0), (-1, 0), 10), ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D1D5DB')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))
    doc.build([Paragraph('Аудиторный фонд — %s, %d-я неделя' % (ctx['current_week_label'].lower(),
                                                              ctx['week_number']), styles['Title']),
               Spacer(1, 0.4 * cm), table, Spacer(1, 0.4 * cm),
               Paragraph('Сформировано: %s' % ctx['update_time'], styles['Normal'])])
    resp = Response(out.getvalue(), mimetype='application/pdf')
    resp.headers['Content-Disposition'] = 'attachment; filename=rooms_%s.pdf' % stamp
    return resp


@routes.route('/api/schedule/room/<path:room_id>')
@admin_required
def api_get_room_schedule(room_id):
    """Расписание одной аудитории (JSON) из снапшота; аудитория сопоставляется с нормализацией."""
    snap = load_lessons_snapshot()
    key = room_key(room_id)
    lessons = snap.by_room.get(key, []) if key else []
    items = [conflicts.lesson_view(l) for l in lessons]
    days = OrderedDict()
    for it in items:
        if it['weekday'] is not None:
            days.setdefault(it['weekday'], []).append(it)
    return jsonify({'success': True, 'room': room_id, 'room_key': key, 'schedule': items, 'days': days,
                    'groups': sorted({it['groupId'] for it in items if it['groupId']}, key=group_sort_key),
                    'total_lessons': len(items), 'current_week': current_week_type(),
                    'is_even_week': current_week_type() == 'numerator'})


# ==================== НАКЛАДКИ ====================

def conflict_filters(args=None):
    """Фильтры страницы «Накладки» из query-параметров (общие для страницы, экспорта и API)."""
    a = args if args is not None else request.args
    dim = a.get('dim')
    day = _parse_day(a.get('day'))
    week = _week_param(a.get('week'))
    # «Период» (пилюля фильтров): today — сегодня в текущую неделю, week — эта неделя, numerator/denominator.
    # Старые ссылки с ?week= продолжают работать: период выводится из недели.
    period = a.get('period') if a.get('period') in PERIODS else ''
    if period == 'today':
        week = 'current'
        if day is None:
            day = default_day(week_context())
    elif period == 'week':
        week = 'current'
    elif period in ('numerator', 'denominator'):
        week = period
    elif week:
        period = {'current': 'week'}.get(week, week)
    return {
        'dim': dim if dim in DIM_LABELS else 'teacher',
        'q': (a.get('q') or '').strip()[:80],
        'day': day,
        'period': period,
        'hour': _parse_int(a.get('hour'), 0, 23),
        'week': week,
        'severity': a.get('severity') if a.get('severity') in SEVERITIES else '',
        'teacher': (a.get('teacher') or '').strip()[:200],
        'room': (a.get('room') or '').strip()[:80],
        'group': (a.get('group') or '').strip()[:80],
        'code': a.get('code') if a.get('code') in QUALITY else '',
        'acked': a.get('acked') == '1',
        'info': a.get('info') == '1',
    }


def _cluster_matches(c, f, ctx, tkey, rkey, gkey, words, use_time=True):
    if f['severity'] and c['severity'] != f['severity']:
        return False
    if not f['info'] and not f['severity'] and c['severity'] == 'info':
        return False
    if use_time and f['day'] is not None and c['weekday'] != f['day']:
        return False
    if use_time and f['hour'] is not None and not intervals_overlap(c['start'], c['end'], f['hour'] * 60,
                                                                     f['hour'] * 60 + 60):
        return False
    if f['week']:
        ok = occurs_in(c['weeks'], week_of=ctx['today']) if f['week'] == 'current' else occurs_in(c['weeks'], f['week'])
        if not ok:
            return False
    if tkey and tkey not in c['teacher_keys']:
        return False
    if rkey and rkey not in c['room_keys']:
        return False
    if gkey and gkey not in {group_key(g) for g in c['groups']}:
        return False
    if words and not all(w in c['search'] for w in words):
        return False
    return True


def filter_clusters(analysis, dim, f, ctx, acks, snap=None, use_time=True):
    """[(кластер, отметка|None)] измерения dim по фильтрам. Принятые скрыты (кроме ?acked=1),
    «инфо» (потоки) скрыты (кроме ?info=1 или ?severity=info)."""
    tkey = teacher_key_param(f['teacher'], snap)
    rkey = room_key(f['room']) if f['room'] else None
    gkey = group_key(f['group']) if f['group'] else None
    words = norm_text(f['q']).split()
    out = []
    for c in analysis.clusters[dim]:
        ack = acks.get(c['hash'])
        if ack and not f['acked']:
            continue
        if _cluster_matches(c, f, ctx, tkey, rkey, gkey, words, use_time):
            out.append((c, ack))
    return out


def filter_quality(analysis, f, acks, use_code=True):
    words = norm_text(f['q']).split()
    out = []
    for item in analysis.quality:
        ack = acks.get(item['hash'])
        if ack and not f['acked']:
            continue
        if use_code and f['code'] and item['code'] != f['code']:
            continue
        if f['severity'] and item['severity'] != f['severity']:
            continue
        if not f['code'] and not f['severity'] and not f['info'] and item['severity'] == 'info':
            continue
        l = item['lesson']
        if f['day'] is not None and l['weekday'] != f['day']:
            continue
        if words:
            hay = norm_text(' '.join((l['groupId'], l['subject'], l['teacher_name'], l['room'], l['time'],
                                      item['title'], item['detail'])))
            if not all(w in hay for w in words):
                continue
        out.append((item, ack))
    return out


def group_by_entity(rows):
    """[(кластер, отметка)] → карточки сущностей {key, name, clusters, critical, warning, info, accepted, days}."""
    groups = OrderedDict()
    for c, ack in rows:
        g = groups.get(c['key'])
        if g is None:
            g = groups[c['key']] = {'key': c['key'], 'name': c['entity'], 'clusters': [], 'critical': 0,
                                    'warning': 0, 'info': 0, 'accepted': 0, 'days': set()}
        g['clusters'].append({'c': c, 'ack': ack})
        g['days'].add(c['weekday'])
        if ack:
            g['accepted'] += 1
        else:
            g[c['severity']] += 1
    out = list(groups.values())
    for g in out:
        g['clusters'].sort(key=lambda x: (x['c']['weekday'], x['c']['start']))
        g['days'] = sorted(g['days'])
    out.sort(key=lambda g: (-g['critical'], -g['warning'], -g['info'], norm_name(g['name'])))
    return out


def heatmap(rows):
    """Тепловая карта день × час: число накладок, начинающихся в этот час (без принятых)."""
    cells = {}
    hours = set(range(8, 18))
    days = set(range(6))
    for c, ack in rows:
        if ack:
            continue
        h = c['start'] // 60
        hours.add(h)
        days.add(c['weekday'])
        k = (c['weekday'], h)
        cells[k] = cells.get(k, 0) + 1
    hours = list(range(min(hours), max(hours) + 1))
    peak = max(cells.values() or [0])
    return {'days': sorted(days), 'hours': hours, 'cells': cells, 'peak': peak, 'total': sum(cells.values())}


def _teacher_options(analysis, acks):
    seen = OrderedDict()
    for c in analysis.clusters['teacher']:
        if c['hash'] in acks or c['severity'] == 'info':
            continue
        e = seen.setdefault(c['key'], [c['entity'], 0])
        e[1] += 1
    return sorted(((k, '%s (%d)' % (conflicts.short_name(v[0]), v[1])) for k, v in seen.items()),
                  key=lambda kv: norm_name(kv[1]))


def _conflicts_context():
    """(анализ | None, отметки, снапшот | None) — страница не падает, если Firestore недоступен."""
    analysis = _safe_analysis()
    acks = _safe_acks()
    try:
        snap = load_lessons_snapshot()
    except Exception:
        snap = None
    return analysis, acks, snap


@routes.route('/schedule/conflicts')
@admin_required
def schedule_conflicts():
    """Накладки расписания. ?dim=teacher|room|group|quality, фильтры: q, day, hour, week
    (current|numerator|denominator), severity, teacher (ключ, uid или ФИО), room, group, code
    (для качества данных), acked=1 (показать принятые), info=1 (показать потоки), page."""
    ctx = week_context()
    f = conflict_filters()
    analysis, acks, snap = _conflicts_context()
    if f['teacher'] and snap is not None:  # голый uid/ФИО → ключ (чтобы фильтр показал выбранного)
        f['teacher'] = teacher_key_param(f['teacher'], snap)
    summary = conflicts.summarize(analysis, acks.keys()) if analysis else None
    tabs_counts = {}
    entities, page, heat, quality_rows, quality_chips = [], paginate_list([], 1), None, [], []
    teacher_options, stale = [], []
    if analysis is not None:
        for d in DIMS:
            # счётчики вкладок — с теми же фильтрами, что сохраняют ссылки вкладок (без часа и критичности)
            tabs_counts[d] = len(filter_clusters(analysis, d, dict(f, hour=None, severity='', acked=False, info=False),
                                                 ctx, acks, snap))
        tabs_counts['quality'] = len(filter_quality(analysis, dict(f, code='', severity='', acked=False, info=False),
                                                    acks))
        teacher_options = _teacher_options(analysis, acks)
        stale = conflicts.stale_acks(analysis, acks)
        if f['dim'] == 'quality':
            all_rows = filter_quality(analysis, f, acks, use_code=False)
            counts = OrderedDict()
            for item, _ack in all_rows:
                counts[item['code']] = counts.get(item['code'], 0) + 1
            quality_chips = [(code, QUALITY[code][1], QUALITY[code][0], counts[code]) for code in QUALITY if code in counts]
            quality_rows = [(it, ack) for it, ack in all_rows if not f['code'] or it['code'] == f['code']]
            page = paginate_list(quality_rows, request.args.get('page'), QUALITY_PER_PAGE)
        else:
            rows = filter_clusters(analysis, f['dim'], f, ctx, acks, snap)
            heat = heatmap(filter_clusters(analysis, f['dim'], f, ctx, acks, snap, use_time=False))
            entities = group_by_entity(rows)
            page = paginate_list(entities, request.args.get('page'), ENTITIES_PER_PAGE)
            for g in page.items:
                for it in g['clusters']:
                    c = it['c']
                    it['this_week'] = occurs_in(c['weeks'], week_of=ctx['today'])
                    it['today'] = it['this_week'] and c['weekday'] == ctx['today_wd']
    fixable = []
    if f['dim'] == 'quality' and analysis is not None:
        fix_counts = OrderedDict()
        for it, ack in quality_rows:
            if it['fixable'] and not ack:
                fix_counts[it['code']] = fix_counts.get(it['code'], 0) + 1
        fixable = [(code, FIXABLE[code], n) for code, n in fix_counts.items()]
    filtered = any([f['q'], f['day'] is not None, f['hour'] is not None, f['week'], f['severity'], f['teacher'],
                    f['room'], f['group'], f['code']])
    return render_template(
        'rooms/conflicts.html', f=f, summary=summary, analysis=analysis, page=page, entities=page.items,
        heat=heat, quality_chips=quality_chips, tabs_counts=tabs_counts, teacher_options=teacher_options,
        stale=stale, fixable=fixable, filtered=filtered, kinds=KINDS, quality=QUALITY, sev_labels=SEV_LABELS,
        dim_labels=DIM_LABELS, day_options=DAY_OPTIONS, week_filters=WEEK_FILTERS, day_names=DAY_NAMES_FULL,
        day_short=DAY_SHORT, tab_counts=_tab_counts(), periods=list(PERIODS.items()),
        here=request.full_path.rstrip('?'), **ctx)


def _back_to_conflicts():
    return redirect(safe_next_url(request.form.get('next')) or url_for('schedule_conflicts'))


@routes.route('/schedule/conflicts/ack', methods=['POST'])
@admin_required
def schedule_conflicts_ack():
    """Принять (игнорировать) накладки/проблемы данных: hash (одно или несколько полей), note."""
    hashes = [h for h in request.form.getlist('hash') if h][:500]
    note = (request.form.get('note') or '').strip()[:300]
    if not hashes:
        flash('Отметьте накладки, которые нужно принять', 'warning')
        return _back_to_conflicts()
    try:
        n = conflicts.accept(hashes, note, current_admin())
    except Exception as e:
        flash_error('Не удалось сохранить отметку. Попробуйте ещё раз.', e)
        return _back_to_conflicts()
    if n:
        audit('conflict_ack', 'schedule_conflicts', None, {'count': n, 'note': note or None, 'hashes': hashes[:20]})
        # обратимое действие — без подтверждения, с «Отменить» в тосте (UX_SPEC_v2 §4.11)
        msg = ('Накладка принята — скрыта, пока занятия не изменятся' if n == 1 else
               '%s %d %s — скрыты, пока занятия не изменятся' % (plural(n, 'Принята', 'Приняты', 'Принято'), n,
                                                                plural(n, 'накладка', 'накладки', 'накладок')))
        flash_action(msg, 'Отменить', post=url_for('schedule_conflicts_unack'),
                     fields={'hashes': ','.join(hashes), 'next': safe_next_url(request.form.get('next')) or ''})
    else:
        flash('Накладки уже изменились — обновите страницу', 'warning')
    return _back_to_conflicts()


@routes.route('/schedule/conflicts/unack', methods=['POST'])
@admin_required
def schedule_conflicts_unack():
    """Вернуть принятые накладки в список (удалить отметки)."""
    hashes = [h for h in request.form.getlist('hash') if h]
    # «Отменить» в тосте после «Принять» присылает хэши одной строкой через запятую
    hashes += [h.strip() for h in (request.form.get('hashes') or '').split(',') if h.strip()]
    hashes = list(dict.fromkeys(hashes))[:500]
    if not hashes:
        flash('Не выбрано, что вернуть', 'warning')
        return _back_to_conflicts()
    try:
        n = conflicts.unaccept(hashes)
    except Exception as e:
        flash_error('Не удалось снять отметку. Попробуйте ещё раз.', e)
        return _back_to_conflicts()
    audit('conflict_unack', 'schedule_conflicts', None, {'count': n, 'hashes': hashes[:20]})
    flash('Возвращено в список: %d' % n if n != 1 else 'Накладка снова в списке', 'success')
    return _back_to_conflicts()


@routes.route('/schedule/conflicts/acks/cleanup', methods=['POST'])
@admin_required
def schedule_conflicts_acks_cleanup():
    """Удалить устаревшие отметки «принято» (занятия, к которым они относились, изменены)."""
    try:
        stale = conflicts.stale_acks()
        n = conflicts.unaccept([a.get('id') or a.get('hash') for a in stale])
    except Exception as e:
        flash_error('Не удалось очистить устаревшие отметки.', e)
        return _back_to_conflicts()
    audit('conflict_acks_cleanup', 'schedule_conflicts', None, {'count': n})
    flash('Удалено устаревших отметок: %d' % n, 'success')
    return _back_to_conflicts()


@routes.route('/schedule/conflicts/refresh', methods=['POST'])
@admin_required
def schedule_conflicts_refresh():
    """Перечитать расписание из базы (например, после правок в приложении) и пересчитать накладки."""
    invalidate_schedule_caches()
    conflicts.invalidate()
    flash('Расписание перечитано, накладки пересчитаны', 'success')
    return _back_to_conflicts()


@routes.route('/schedule/conflicts/fix', methods=['POST'])
@admin_required
def schedule_conflicts_fix():
    """Массовое исправление данных (вкладка «Качество данных»): code — что исправить
    (teacher_by_name | day_mismatch | date_timestamp); hash — выбранные проблемы (иначе все такие).
    Пишется пачкой с журналом schedule_logs и без пуша на каждую пару (notify:false, D15)."""
    code = request.form.get('code')
    if code not in FIXABLE:
        flash('Это нельзя исправить автоматически', 'error')
        return _back_to_conflicts()
    selected = set(h for h in request.form.getlist('hash') if h)
    try:
        snap = load_lessons_snapshot(force=True)  # свежие данные: правим только то, что есть сейчас
        analysis = conflicts.get_analysis()
    except Exception as e:
        flash_error('Не удалось загрузить расписание. Попробуйте ещё раз.', e)
        return _back_to_conflicts()
    targets = []
    for item in analysis.quality:
        if item['code'] != code or (selected and item['hash'] not in selected):
            continue
        l = snap.by_id.get(item['lesson']['id'])
        fields = conflicts.fix_for(code, l) if l else None
        if fields:
            targets.append((l, fields))
    if not targets:
        flash('Нечего исправлять — данные уже изменились', 'info')
        return _back_to_conflicts()
    try:
        from .routes_schedule import ScheduleBatch
        batch = ScheduleBatch(quiet=True)
        for l, fields in targets[:2000]:
            batch.update(l['id'], dict(l['raw']), fields)
        batch.commit(notify_users=False)
    except Exception as e:
        flash_error('Не удалось сохранить исправления. Часть занятий могла не измениться — обновите страницу.', e)
        return _back_to_conflicts()
    finally:
        invalidate_schedule_caches()
    n = len(targets[:2000])
    audit('schedule_fix', 'schedules', None, {'code': code, 'count': n, 'ids': [l['id'] for l, _f in targets[:20]]})
    flash('Исправлено %d %s: %s' % (n, plural(n, 'занятие', 'занятия', 'занятий'), FIXABLE[code].lower()), 'success')
    return _back_to_conflicts()


# ---------- экспорт накладок ----------

CONFLICT_HEADER = ['Вкладка', 'Критичность', 'Вид накладки', 'Преподаватель / аудитория / группа', 'День',
                   'Время накладки', 'Когда пересекаются', 'Группа', 'Дисциплина', 'Преподаватель', 'Аудитория',
                   'Время занятия', 'Неделя занятия', 'Принято', 'ID занятия']
QUALITY_HEADER = ['Критичность', 'Проблема', 'Подробности', 'Группа', 'День', 'Время', 'Дисциплина',
                  'Преподаватель', 'Аудитория', 'Неделя', 'Принято', 'ID занятия']


def _conflict_rows(dim, rows):
    out = []
    for c, ack in rows:
        kinds = ', '.join(KINDS[k][1] for k in c['kinds'])
        for l in c['lessons']:
            out.append(([DIM_LABELS[dim], SEV_LABELS[c['severity']], kinds, c['entity'], c['day_name'],
                         c['time_label'], c['weeks_label'], l['groupId'], l['subject'], l['teacher_name'], l['room'],
                         l['time_label'] or l['time'], WEEK_LABELS.get(l['week'], l['week']),
                         ('да' + (': ' + ack.get('note') if ack.get('note') else '')) if ack else '', l['id']],
                        c['severity']))
    return out


def _quality_rows(rows):
    out = []
    for item, ack in rows:
        l = item['lesson']
        out.append(([SEV_LABELS[item['severity']], item['title'], item['detail'], l['groupId'], l['day_name'],
                     l['time'], l['subject'], l['teacher_name'], l['room'], WEEK_LABELS.get(l['week'], ''),
                     'да' if ack else '', l['id']], item['severity']))
    return out


@routes.route('/schedule/conflicts/export')
@admin_required
def schedule_conflicts_export():
    """Экспорт накладок с фильтрами страницы: ?format=xlsx (все вкладки + сводка) | csv (текущая вкладка)."""
    if not request.args.get('format'):
        return redirect(url_with(format='xlsx'))
    ctx = week_context()
    f = conflict_filters()
    fmt = request.args.get('format')
    try:
        analysis = conflicts.get_analysis()
        snap = load_lessons_snapshot()
    except Exception as e:
        flash_error('Не удалось подготовить экспорт. Попробуйте ещё раз.', e)
        return redirect(url_for('schedule_conflicts'))
    acks = _safe_acks()
    stamp = ctx['now'].strftime('%Y%m%d_%H%M')
    audit('export', 'schedule_conflicts', None, {'format': fmt, 'dim': f['dim']})
    if fmt == 'csv':
        if f['dim'] == 'quality':
            rows = _quality_rows(filter_quality(analysis, f, acks))
            header = QUALITY_HEADER
        else:
            rows = _conflict_rows(f['dim'], filter_clusters(analysis, f['dim'], f, ctx, acks, snap))
            header = CONFLICT_HEADER
        return csv_response('накладки_%s_%s.csv' % (f['dim'], stamp), header, [r for r, _s in rows])
    summary = conflicts.summarize(analysis, acks.keys())
    head = Sheet('Сводка')
    head.add(['Накладки в расписании'], 'title')
    head.add(['Сформировано %s · %s, %d-я неделя' % (ctx['update_time'], ctx['current_week_label'].lower(),
                                                     ctx['week_number'])])
    head.add([])
    head.header(['Вкладка', 'Критично', 'Внимание', 'Инфо (потоки)'])
    for d in DIMS:
        n = summary['by_dim'][d]
        head.add([DIM_LABELS[d], n['critical'], n['warning'], n['info']])
    head.add([])
    head.add(['Всего уникальных: критичных %d, предупреждений %d; преподавателей с накладками %d; проблем данных %d; '
              'принято %d' % (summary['critical'], summary['warning'], summary['teachers'], summary['quality'],
                              summary['accepted'])])
    head.widths = {1: 26, 2: 12, 3: 12, 4: 16}
    head.freeze_row = None
    head.filter_row = None
    sheets = [head]
    for d in DIMS:
        sh = Sheet(DIM_LABELS[d])
        sh.header(CONFLICT_HEADER)
        for values, sev in _conflict_rows(d, filter_clusters(analysis, d, f, ctx, acks, snap)):
            sh.add(values, sev)
        sh.widths = {1: 14, 2: 12, 3: 30, 4: 28, 5: 13, 6: 13, 7: 18, 8: 12, 9: 26, 10: 28, 11: 12, 12: 13, 13: 15,
                     14: 14, 15: 22}
        sheets.append(sh)
    q = Sheet(DIM_LABELS['quality'])
    q.header(QUALITY_HEADER)
    for values, sev in _quality_rows(filter_quality(analysis, f, acks)):
        q.add(values, sev)
    q.widths = {1: 12, 2: 30, 3: 40, 4: 12, 5: 13, 6: 14, 7: 26, 8: 28, 9: 12, 10: 15, 11: 10, 12: 22}
    sheets.append(q)
    try:
        return xlsx_response(sheets, 'накладки_%s.xlsx' % stamp)
    except Exception as e:
        flash_error('Не удалось сформировать Excel — скачайте CSV.', e)
        return redirect(url_for('schedule_conflicts', **{k: v for k, v in request.args.items() if k != 'format'}))


@routes.route('/api/schedule/conflicts')
@admin_required
def api_schedule_conflicts():
    """Сводка и кластеры накладок (JSON) с фильтрами страницы; ?dim= — одна вкладка, ?limit= (≤500)."""
    ctx = week_context()
    f = conflict_filters()
    try:
        analysis = conflicts.get_analysis()
        snap = load_lessons_snapshot()
    except Exception:
        log.exception('API накладок: расписание недоступно')
        return jsonify({'success': False, 'error': 'Расписание недоступно'}), 503
    acks = _safe_acks()
    limit = _parse_int(request.args.get('limit'), 1, 500) or 200
    dims = [f['dim']] if request.args.get('dim') in DIMS else list(DIMS)
    data = {}
    for d in dims:
        rows = filter_clusters(analysis, d, f, ctx, acks, snap)[:limit]
        data[d] = [dict({k: v for k, v in c.items() if k not in ('search', 'lesson_flags')},
                        accepted=bool(ack), this_week=occurs_in(c['weeks'], week_of=ctx['today'])) for c, ack in rows]
    return jsonify({'success': True, 'summary': conflicts.summarize(analysis, acks.keys()), 'clusters': data,
                    'current_week': ctx['current_week'], 'computed_at': analysis.computed_at})


# ==================== СВОБОДНЫЕ АУДИТОРИИ ====================

def _hm(value):
    """'09:30' / '9.30' → минуты или None."""
    start, _end = parse_time_range(value)
    return start


def _free_week_ok(l, week, ctx):
    """Занятие мешает в неделю фильтра: 'both' — в любую; иначе как lesson_in_week."""
    if week == 'both':
        return True
    return lesson_in_week(l, week, ctx)


def free_rooms(snap, day, start, end, week, ctx, q=''):
    """Свободные и занятые аудитории дня day в интервале [start, end) для недели week
    ('current'|'numerator'|'denominator'|'both' — свободна в обе недели).
    → (free [{key, name, prev_end, next_start, window, day_lessons}], busy [{key, name, lessons}])."""
    words = norm_text(q).split()
    day_lessons = {}
    for l in snap.by_day.get(day, []):
        if l['room_key'] and l['time_ok'] and _free_week_ok(l, week, ctx):
            day_lessons.setdefault(l['room_key'], []).append(l)
    free, busy = [], []
    for key, name in snap.rooms():
        if words and not all(w in norm_text(name) or w in key for w in words):
            continue
        ls = sorted(day_lessons.get(key, []), key=lambda l: l['start'])
        clash = [l for l in ls if intervals_overlap(l['start'], l['end'], start, end)]
        if clash:
            busy.append({'key': key, 'name': name, 'lessons': clash})
            continue
        ends = [l['end'] for l in ls if l['end'] <= start]
        starts = [l['start'] for l in ls if l['start'] >= end]
        prev_end = max(ends) if ends else None
        next_start = min(starts) if starts else None
        if prev_end is None and next_start is None:
            window = 'весь день'
        elif prev_end is None:
            window = 'до %s' % fmt_minutes(next_start)
        elif next_start is None:
            window = 'с %s до конца дня' % fmt_minutes(prev_end)
        else:
            window = 'с %s до %s' % (fmt_minutes(prev_end), fmt_minutes(next_start))
        free.append({'key': key, 'name': name, 'prev_end': prev_end, 'next_start': next_start, 'window': window,
                     'lessons_count': len(ls), 'all_day': prev_end is None and next_start is None})
    free.sort(key=lambda r: room_sort_key(r['name']))
    busy.sort(key=lambda r: room_sort_key(r['name']))
    return free, busy


def occupancy_grid(snap, day, week, ctx, rooms, lessons_only=False):
    """Шахматка дня: аудитории × слоты сетки звонков → занятия в слоте (или пусто).
    lessons_only — без служебных слотов (перемены, кураторский час)."""
    slots = [(it['label'], it['start'], it['end']) for it in slot_items(day) if not (lessons_only and it['service'])]
    rows = []
    for r in rooms:
        cells = []
        ls = [l for l in snap.by_room.get(r['key'], []) if l['weekday'] == day and l['time_ok']
              and _free_week_ok(l, week, ctx)]
        for label, s, e in slots:
            cells.append([l for l in ls if intervals_overlap(l['start'], l['end'], s, e)])
        rows.append({'key': r['key'], 'name': r['name'], 'cells': cells, 'busy': sum(1 for c in cells if c)})
    numbers = {it['label']: it['n'] for it in slot_items(day)}
    return [{'label': label, 'start': s, 'end': e, 'title': time_label(s, e), 'n': numbers.get(label)}
            for label, s, e in slots], rows


@routes.route('/schedule/free-rooms')
@admin_required
def schedule_free_rooms():
    """Свободные аудитории: ?day=0..6 (по умолчанию сегодня), ?slot=<'08.00-08.50'> (пара сетки звонков)
    или своё время ?from=HH:MM&to=HH:MM, ?week=current|numerator|denominator|both, ?q= (аудитория)."""
    ctx = week_context()
    day = _parse_day(request.args.get('day'))
    if day is None:
        day = default_day(ctx)
    slots = call_slots(day)
    week = request.args.get('week') if request.args.get('week') in ('current', 'numerator', 'denominator', 'both') \
        else 'current'
    q = (request.args.get('q') or '').strip()[:60]
    t_from, t_to = _hm(request.args.get('from')), _hm(request.args.get('to'))
    slot = request.args.get('slot') or ''
    custom = t_from is not None and t_to is not None and t_to > t_from
    same = next((l for l, s, e in slots if custom and (s, e) == (t_from, t_to)), None)
    if same:   # «с — по» совпали с парой сетки (ссылка из правки занятия) — выбираем пару
        custom, slot = False, same
    if custom:
        start, end = t_from, t_to
        slot = ''
    else:
        if (t_from is not None or t_to is not None) and not same:
            flash('Время «с — по» указано не полностью — показана пара из сетки', 'warning')
        chosen = next(((l, s, e) for l, s, e in slots if l == slot), None)
        if chosen is None:  # по умолчанию — текущая или ближайшая пара сегодня
            now = ctx['now_minutes'] if day == ctx['today_wd'] else -1
            chosen = next(((l, s, e) for l, s, e in slots if e > now and e - s >= SERVICE_SLOT_MAX), slots[0])
        slot, start, end = chosen
    free, busy, grid_slots, grid_rows, total = [], [], [], [], 0
    try:
        snap = load_lessons_snapshot()
        total = len(snap.rooms())
        free, busy = free_rooms(snap, day, start, end, week, ctx, q)
        rooms = sorted(free + busy, key=lambda r: room_sort_key(r['name']))
        grid_slots, grid_rows = occupancy_grid(snap, day, week, ctx, rooms, lessons_only=True)
    except Exception as e:
        flash_error('Не удалось загрузить расписание. Попробуйте обновить страницу.', e)
    week_label = {'current': 'текущая (%s)' % ctx['current_week_label'].lower(), 'numerator': 'числитель',
                  'denominator': 'знаменатель', 'both': 'обе недели'}[week]
    copy_text = '%s, %s, %s: свободны %s' % (DAY_NAMES_FULL[day], time_label(start, end), week_label,
                                              ', '.join(r['name'] for r in free) or '—')
    return render_template(
        'rooms/free_rooms.html', day=day, slot=slot, slots=slot_items(day),
        start=start, end=end, interval=time_label(start, end), custom=custom, week=week, week_label=week_label,
        q=q, free=free, busy=busy, total=total, grid_slots=grid_slots, grid_rows=grid_rows,
        from_value=fmt_minutes(start) if custom else '', to_value=fmt_minutes(end) if custom else '',
        all_slots={d: [(l, time_label(s, e)) for l, s, e in call_slots(d)] for d in range(7)},
        copy_text=copy_text, day_options=DAY_OPTIONS, day_names=DAY_NAMES_FULL, tab_counts=_tab_counts(),
        is_now=(day == ctx['today_wd'] and start <= ctx['now_minutes'] < end), **ctx)


@routes.route('/api/schedule/free-rooms')
@admin_required
def api_free_rooms():
    """Свободные аудитории (JSON): ?day=0..6&from=HH:MM&to=HH:MM&week=current|numerator|denominator|both.
    Без параметров — сегодня (в воскресенье — понедельник), текущая или ближайшая пара сетки звонков."""
    ctx = week_context()
    has_args = any(request.args.get(k) for k in ('day', 'from', 'to'))
    day = _parse_day(request.args.get('day'))
    start, end = _hm(request.args.get('from')), _hm(request.args.get('to'))
    if not has_args:
        day = ctx['today_wd'] if ctx['today_wd'] <= 5 else 0
        now = ctx['now_minutes'] if day == ctx['today_wd'] else -1
        slots = call_slots(day)
        _label, start, end = next(((l, s, e) for l, s, e in slots if e > now), slots[0])
    elif day is None or start is None or end is None or end <= start:
        return jsonify({'success': False, 'error': 'Укажите day (0..6), from и to (ЧЧ:ММ)'}), 400
    week = request.args.get('week') if request.args.get('week') in ('current', 'numerator', 'denominator', 'both') \
        else 'current'
    snap = load_lessons_snapshot()
    free, busy = free_rooms(snap, day, start, end, week, ctx)
    return jsonify({'success': True, 'day': day, 'from': fmt_minutes(start), 'to': fmt_minutes(end), 'week': week,
                    'free': [{'room': r['name'], 'window': r['window']} for r in free],
                    'busy': [{'room': r['name'], 'lessons': [conflicts.lesson_view(l) for l in r['lessons']]}
                             for r in busy]})


# ==================== БЕЙДЖ САЙДБАРА ====================

def nav_badges():
    """Бейдж «Накладки»: число критичных накладок без принятых (из conflicts.summary(), кэш)."""
    s = conflicts.summary() or {}
    return {'schedule_conflicts': s.get('critical') or 0}

