# cms/routes_home.py — главная «/» (обзор колледжа) и журнал аудита /audit (владелец — PEOPLE).
#
# Главная: KPI на агрегатах count() (студенты, преподаватели, абитуриенты), регистрации абитуриентов
# за 14 дней (одна выборка select(['createdAt'])), заказы и обращения — те же числа, что в бейджах
# сайдбара, накладки — cms.conflicts.summary() (None → прочерк), сегодняшнее расписание из общего
# снапшота, последние события аудита, быстрые действия. Всё кэшируется на 60 с ('home:*'),
# аудит — 'audit:*' (core.audit() сбрасывает его при каждой записи).
#
# /audit: последние записи cms_audit (по диапазону дат — только поле at, без составных индексов),
# фильтры по действию, объекту, администратору и тексту — в памяти, пагинация, CSV.

from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore
from flask import current_app, render_template, request, session

from . import conflicts
from . import core as _core  # nav_badges не импортируем по имени: core ищет функцию nav_badges в модулях
from .core import (ALMATY, Routes, admin_required, cache, count, csv_response, db, doc_dict, fmt_dt, json_safe, log,
                   nav_url, now_almaty, now_utc, paginate_list, plural, to_almaty, to_datetime, today_almaty, where)
from .schedule_model import (DAY_NAMES_FULL, academic_week_number, call_slots, current_week_type,
                             load_lessons_snapshot, norm_text)

routes = Routes()

DESC = firestore.Query.DESCENDING
HOME_TTL = 60
AUDIT_PAGE_SIZE = 50
AUDIT_SCAN_LIMIT = 3000      # сколько последних записей журнала просматривается фильтрами
AUDIT_TTL = 120
CHART_DAYS = 14

# Подписи действий и объектов журнала (неизвестные показываются как есть).
ACTION_LABELS = {
    'login': 'Вход', 'logout': 'Выход', 'create': 'Создание', 'update': 'Изменение', 'delete': 'Удаление',
    'rename': 'Переименование', 'reorder': 'Порядок', 'export': 'Экспорт', 'reset_password': 'Сброс пароля',
    'disable': 'Блокировка входа', 'enable': 'Разблокировка входа', 'revoke_sessions': 'Завершение сеансов',
    'bulk_delete': 'Массовое удаление', 'bulk_group': 'Смена группы', 'bulk_course': 'Смена курса',
    'status': 'Статус', 'reply': 'Ответ', 'reply_edit': 'Правка ответа', 'close': 'Закрытие',
    'extend': 'Продление', 'add_answer': 'Вариант ответа', 'delete_answer': 'Удаление варианта',
    'delete_registration': 'Удаление регистрации', 'recount': 'Пересчёт', 'remind': 'Напоминание',
    'issue': 'Выдача', 'cancel': 'Отмена', 'award': 'Начисление', 'convert': 'Зачисление',
    'schedule_create': 'Новые занятия', 'schedule_update': 'Правка занятия', 'schedule_delete': 'Удаление занятия',
    'schedule_update_group': 'Правка недели группы', 'schedule_delete_group': 'Удаление расписания группы',
    'schedule_copy': 'Копирование расписания', 'schedule_bulk': 'Массовая правка расписания',
    'cache_invalidate': 'Сброс кэша', 'ack': 'Накладка принята', 'unack': 'Накладка возвращена',
}
ENTITY_LABELS = {
    'session': 'Сеанс', 'user': 'Пользователь', 'teacher': 'Преподаватель', 'group': 'Группа',
    'subject': 'Дисциплина', 'management': 'Администрация', 'news': 'Новость', 'poll': 'Опрос',
    'event': 'Событие', 'club': 'Клуб', 'product': 'Товар', 'feedback': 'Обращение', 'schedule': 'Расписание',
    'cache': 'Кэш', 'applicant': 'Абитуриент', 'applicant_crm': 'CRM абитуриента',
    'applicant_source': 'QR-источник', 'applicant_sources': 'QR-источник', 'applicant_news': 'Новость абитуриентов',
    'coin_campaigns': 'Кампания', 'coin_campaign': 'Кампания', 'coin_products': 'Товар за коины',
    'coin_product': 'Товар за коины', 'coin_orders': 'Заказ', 'coin_order': 'Заказ',
    'coin_transactions': 'Коины', 'coin_transaction': 'Коины', 'admissions': 'Приёмная',
    'conflict': 'Накладка', 'schedule_conflict_acks': 'Накладка',
}
DETAIL_LABELS = {
    'role': 'Роль', 'name': 'Имя', 'email': 'Email', 'title': 'Название', 'count': 'Количество', 'ids': 'Объекты',
    'from': 'Было', 'to': 'Стало', 'status': 'Статус', 'users': 'Пользователей', 'lessons': 'Занятий',
    'changes': 'Изменения', 'rows': 'Строк', 'format': 'Формат', 'value': 'Значение', 'group': 'Группа',
    'subject': 'Дисциплина', 'note': 'Заметка', 'amount': 'Сумма', 'reason': 'Причина', 'login': 'Логин',
    'phone': 'Телефон', 'course': 'Курс', 'failed': 'Ошибки', 'names': 'Названия', 'photo': 'Фото',
}
# Объект журнала → (endpoint, имя параметра) для ссылки «открыть» (удалённые объекты не ссылаются).
ENTITY_LINKS = {
    'user': ('admin_edit_user', 'user_id'), 'teacher': ('teacher_edit', 'uid'),
    'applicant': ('applicant_detail', 'uid'), 'applicant_crm': ('applicant_detail', 'uid'),
    'news': ('news_form', 'news_id'), 'poll': ('poll_form', 'poll_id'), 'event': ('event_form', 'event_id'),
    'club': ('club_form', 'club_id'), 'product': ('product_form', 'product_id'),
    'management': ('administration_edit', 'admin_id'),
}
GROUP_ACTIONS = {'schedule_create', 'schedule_update_group', 'schedule_copy', 'schedule_bulk'}
PERIODS = [('', 'Всё время'), ('today', 'Сегодня'), ('7', '7 дней'), ('30', '30 дней'), ('custom', 'Свои даты')]


# ==================== ГЛАВНАЯ ====================

def _greeting(now):
    h = now.hour
    if 5 <= h < 12:
        return 'Доброе утро'
    if 12 <= h < 18:
        return 'Добрый день'
    if 18 <= h < 23:
        return 'Добрый вечер'
    return 'Доброй ночи'


def _people_counts():
    """Агрегаты count() — параллельно (каждый — отдельный запрос к Firestore)."""
    users = db.collection('users')
    queries = {
        'students': where(users, 'role', '==', 'student'),
        'teachers': where(users, 'role', '==', 'teacher'),
        'applicants': where(users, 'role', '==', 'applicant'),
        'crm_new': where(db.collection('applicant_crm'), 'status', '==', 'new'),
    }
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futures = {k: pool.submit(count, q) for k, q in queries.items()}
        return {k: f.result() for k, f in futures.items()}


def _registrations(days=CHART_DAYS):
    """Регистрации абитуриентов по дням (Алматы) за последние days дней — одна выборка с select()."""
    start_day = today_almaty() - timedelta(days=days - 1)
    since = datetime(start_day.year, start_day.month, start_day.day, tzinfo=ALMATY).astimezone(timezone.utc)
    q = where(where(db.collection('users'), 'role', '==', 'applicant'), 'createdAt', '>=', since)
    per_day = Counter()
    for snap in q.select(['createdAt']).limit(5000).stream():
        dt = to_almaty((snap.to_dict() or {}).get('createdAt'))
        if dt:
            per_day[dt.date()] += 1
    out = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        out.append({'date': d, 'count': per_day.get(d, 0), 'label': d.strftime('%d.%m'),
                    'weekday': ('Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс')[d.weekday()]})
    return out


def _kpi():
    def load():
        data = {'counts': {}, 'chart': []}
        try:
            data['counts'] = _people_counts()
        except Exception:
            log.exception('KPI главной: count() не удался')
        try:
            data['chart'] = _registrations()
        except Exception:
            log.exception('KPI главной: регистрации не загрузились')
            data['chart'] = None
        return data
    return cache.get_or_set('home:kpi', load, ttl=HOME_TTL)


def _attention_counts():
    """Заказы ждут выдачи и обращения без ответа — те же числа, что в бейджах сайдбара."""
    loaded = set(current_app.extensions.get('cms_modules') or [])
    try:
        badges = _core.nav_badges()
    except Exception:
        badges = {}

    def from_badge(endpoint, module, fallback):
        if module in loaded:
            return badges.get(endpoint, 0)
        return cache.get_or_set('home:%s' % endpoint, fallback, ttl=HOME_TTL)

    orders = from_badge('coins_orders', 'cms.routes_coins',
                        lambda: count(where(db.collection('coin_orders'), 'status', '==', 'reserved')))
    feedback = from_badge('feedback_list', 'cms.routes_content',
                          lambda: count(where(db.collection('feedback'), 'status', '==', 'new')))
    return orders, feedback


def _conflicts_summary():
    try:
        s = conflicts.summary()
        return s if isinstance(s, dict) else None
    except Exception:
        log.exception('conflicts.summary() упал')
        return None


def _today_schedule(now):
    """Сегодняшние занятия из снапшота: сколько пар, групп, преподавателей, что идёт сейчас."""
    today = now.date()
    wd = today.weekday()
    week = current_week_type(today)
    info = {'weekday': DAY_NAMES_FULL[wd], 'week': week, 'week_no': academic_week_number(today),
            'lessons': 0, 'groups': 0, 'teachers': 0, 'now': [], 'slot': None, 'next_slot': None,
            'finished': False, 'error': False, 'timeline': []}
    try:
        snap = load_lessons_snapshot()
    except Exception:
        log.exception('Главная: снапшот расписания недоступен')
        info['error'] = True
        return info
    iso = today.isoformat()
    lessons = [l for l in snap.by_day.get(wd, [])
               if (l['kind'] != 'dated' or l['date'] == iso) and l['week'] in ('always', week)]
    minute = now.hour * 60 + now.minute
    info['lessons'] = len(lessons)
    info['groups'] = len({l['groupId'] for l in lessons if l['groupId']})
    info['teachers'] = len({l['teacher_key'] for l in lessons if l['teacher_key']})
    info['now'] = [l for l in lessons if l['start'] is not None and l['end'] is not None
                   and l['start'] <= minute < l['end']]
    slots = [(label, s, e) for label, s, e in call_slots(wd) if s is not None and e is not None]
    for label, s, e in slots:
        if s <= minute < e:
            info['slot'] = {'label': '%02d:%02d–%02d:%02d' % (s // 60, s % 60, e // 60, e % 60)}
            break
    # загрузка по времени начала: сколько занятий в каждой паре сегодня
    by_time = {}
    for l in lessons:
        if l['start'] is None:
            continue
        key = (l['start'], l['end'] if l['end'] is not None else l['start'])
        by_time[key] = by_time.get(key, 0) + 1
    peak = max(by_time.values()) if by_time else 0
    info['timeline'] = [{'label': '%02d:%02d–%02d:%02d' % (s // 60, s % 60, e // 60, e % 60), 'count': n,
                         'pct': round(n * 100.0 / peak, 1) if peak else 0, 'current': s <= minute < e,
                         'past': e <= minute}
                        for (s, e), n in sorted(by_time.items())]
    later = [s for _l, s, _e in slots if s > minute]
    ends = [l['end'] for l in lessons if l['end'] is not None]
    if later and lessons:
        nxt = min(later)
        info['next_slot'] = '%02d:%02d' % (nxt // 60, nxt % 60)
    info['finished'] = bool(lessons) and bool(ends) and minute >= max(ends)
    return info


def _recent_audit(limit=8):
    def load():
        q = db.collection('cms_audit').order_by('at', direction=DESC).limit(limit)
        return [_audit_row(doc_dict(d)) for d in q.stream()]
    try:
        return cache.get_or_set('audit:recent', load, ttl=HOME_TTL)
    except Exception:
        log.exception('Главная: журнал аудита недоступен')
        return []


def _first_name(full):
    """Имя из ФИО «Фамилия Имя Отчество» (для приветствия); одно слово — как есть."""
    parts = str(full or '').split()
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else '')


def _trend(cur, prev):
    if not prev:
        return None
    return round((cur - prev) * 100.0 / prev)


@routes.route('/')
@admin_required
def dashboard():
    """Обзор колледжа: KPI, что требует внимания, сегодня в расписании, регистрации абитуриентов,
    быстрые действия и последние действия админов."""
    now = now_almaty()
    kpi = _kpi()
    counts = kpi.get('counts') or {}
    chart = kpi.get('chart')
    week_now = week_prev = None
    if chart:
        week_now = sum(d['count'] for d in chart[-7:])
        week_prev = sum(d['count'] for d in chart[:-7])
    orders, feedback = _attention_counts()
    summary = _conflicts_summary()
    today = _today_schedule(now)
    attention = []
    if summary and summary.get('critical'):
        attention.append({'icon': 'exclamation-octagon', 'tone': 'danger', 'count': summary['critical'],
                          'label': plural(summary['critical'], 'критичная накладка', 'критичные накладки',
                                          'критичных накладок') + ' в расписании',
                          'href': nav_url('schedule_conflicts')})
    if orders:
        attention.append({'icon': 'box-seam', 'tone': 'warning', 'count': orders,
                          'label': plural(orders, 'заказ ждёт', 'заказа ждут', 'заказов ждут') + ' выдачи',
                          'href': nav_url('coins_orders')})
    if feedback:
        attention.append({'icon': 'chat-dots', 'tone': 'info', 'count': feedback,
                          'label': plural(feedback, 'обращение', 'обращения', 'обращений') + ' без ответа',
                          'href': nav_url('feedback_list')})
    if counts.get('crm_new'):
        n = counts['crm_new']
        attention.append({'icon': 'person-lines-fill', 'tone': 'violet', 'count': n,
                          'label': plural(n, 'новый лид', 'новых лида', 'новых лидов') + ' — никто не связался',
                          'href': nav_url('applicants_leads', status='new')})
    if summary and summary.get('warning'):
        attention.append({'icon': 'exclamation-triangle', 'tone': 'warning', 'count': summary['warning'],
                          'label': plural(summary['warning'], 'предупреждение', 'предупреждения',
                                          'предупреждений') + ' в расписании',
                          'href': nav_url('schedule_conflicts')})
    max_day = max([d['count'] for d in chart] or [0]) if chart else 0
    return render_template(
        'dashboard.html', now=now, greeting=_greeting(now), counts=counts, chart=chart, max_day=max_day,
        week_now=week_now, week_prev=week_prev, applicants_trend=_trend(week_now or 0, week_prev),
        orders=orders, feedback=feedback, summary=summary, today=today, attention=attention,
        recent=_recent_audit(), admin_first=_first_name(session.get('admin_name')))


# ==================== ЖУРНАЛ АУДИТА ====================

def _detail_value(v):
    if isinstance(v, bool):
        return 'да' if v else 'нет'
    if isinstance(v, list):
        if len(v) == 2 and all(not isinstance(x, (list, dict)) for x in v):
            return '%s → %s' % (v[0] if v[0] not in (None, '') else '—', v[1] if v[1] not in (None, '') else '—')
        shown = ', '.join(str(x) for x in v[:8])
        return shown + (' и ещё %d' % (len(v) - 8) if len(v) > 8 else '')
    if isinstance(v, dict):
        return '; '.join('%s: %s' % (DETAIL_LABELS.get(k, k), _detail_value(x)) for k, x in list(v.items())[:8])
    s = '' if v is None else str(v)
    return s if len(s) <= 160 else s[:160] + '…'


def _details_items(details):
    if details in (None, '', {}, []):
        return []
    if not isinstance(details, dict):
        return [('', _detail_value(details))]
    items = []
    for k, v in details.items():
        if k == 'changes' and isinstance(v, dict):
            for ck, cv in v.items():
                items.append((DETAIL_LABELS.get(ck, ck), _detail_value(cv)))
            continue
        items.append((DETAIL_LABELS.get(k, k), _detail_value(v)))
    return items[:12]


def _entity_link(entity, entity_id, action):
    if not entity_id or str(action).startswith(('delete', 'bulk_delete')) or action == 'schedule_delete_group':
        return None
    if entity in ENTITY_LINKS:
        endpoint, param = ENTITY_LINKS[entity]
        url = nav_url(endpoint, **{param: entity_id})
        return None if url == '#' else url
    if entity == 'group':
        return nav_url('admin_users', role='student', group=entity_id)
    if entity == 'schedule':
        if action in GROUP_ACTIONS:
            return nav_url('schedule', group=entity_id)
        if action == 'schedule_update':
            return nav_url('schedule_edit', schedule_id=entity_id)
    return None


def _audit_row(e):
    e = e or {}
    action = str(e.get('action') or '')
    entity = str(e.get('entity') or '')
    entity_id = e.get('entityId')
    at = to_datetime(e.get('at'))
    details = json_safe(e.get('details'))
    items = _details_items(details)
    row = {
        'id': e.get('id'), 'at': at, 'by': e.get('by') or '', 'by_uid': e.get('byUid') or '',
        'action': action, 'action_label': ACTION_LABELS.get(action, action or '—'),
        'entity': entity, 'entity_label': ENTITY_LABELS.get(entity, entity or '—'),
        'entity_id': '' if entity_id is None else str(entity_id), 'details': items, 'raw_details': details,
        'tone': 'danger' if action.startswith(('delete', 'bulk_delete', 'disable', 'reset_password'))
        else ('success' if action in ('create', 'login', 'enable') else 'brand'),
    }
    row['link'] = _entity_link(entity, row['entity_id'], action)
    row['hay'] = norm_text(' '.join([row['by'], action, row['action_label'], entity, row['entity_label'],
                                     row['entity_id']] + ['%s %s' % kv for kv in items]))
    return row


def _period_range(period, date_from, date_to):
    """(начало UTC, конец UTC) по быстрому периоду или датам из формы (по Алматы)."""
    today = today_almaty()
    start = end = None
    if period == 'today':
        start = today
    elif period in ('7', '30'):
        start = today - timedelta(days=int(period) - 1)
    elif period == 'custom' or date_from or date_to:
        try:
            start = datetime.strptime(date_from, '%Y-%m-%d').date() if date_from else None
        except ValueError:
            start = None
        try:
            end = datetime.strptime(date_to, '%Y-%m-%d').date() + timedelta(days=1) if date_to else None
        except ValueError:
            end = None

    def utc(d):
        return datetime(d.year, d.month, d.day, tzinfo=ALMATY).astimezone(timezone.utc) if d else None
    return utc(start), utc(end)


def _audit_entries(start, end):
    key = 'audit:list:%s:%s' % (start.isoformat() if start else '', end.isoformat() if end else '')

    def load():
        q = db.collection('cms_audit')
        if start:
            q = where(q, 'at', '>=', start)
        if end:
            q = where(q, 'at', '<', end)
        q = q.order_by('at', direction=DESC).limit(AUDIT_SCAN_LIMIT)
        return [_audit_row(doc_dict(d)) for d in q.stream()]
    return cache.get_or_set(key, load, ttl=AUDIT_TTL)


def _day_label(dt, today):
    if not dt:
        return 'Без даты'
    d = to_almaty(dt).date()
    if d == today:
        return 'Сегодня'
    if d == today - timedelta(days=1):
        return 'Вчера'
    return '%s, %s' % (fmt_dt(dt, '%d.%m.%Y'), ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')[d.weekday()])


@routes.route('/audit')
@admin_required
def audit_log():
    """Журнал действий админов: фильтры (период, действие, объект, админ, текст), пагинация, CSV."""
    period = request.args.get('period') or ''
    if period not in dict(PERIODS):
        period = ''
    date_from = (request.args.get('from') or '').strip()[:10]
    date_to = (request.args.get('to') or '').strip()[:10]
    action = (request.args.get('action') or '').strip()
    entity = (request.args.get('entity') or '').strip()
    by = (request.args.get('by') or '').strip()
    q = (request.args.get('q') or '').strip()[:100]
    start, end = _period_range(period, date_from, date_to)
    error = False
    try:
        entries = _audit_entries(start, end)
    except Exception:
        log.exception('Журнал аудита не загрузился')
        entries, error = [], True
    words = norm_text(q).split()
    items = [e for e in entries
             if (not action or e['action'] == action) and (not entity or e['entity'] == entity)
             and (not by or e['by'] == by) and all(w in e['hay'] for w in words)]
    if request.args.get('format') == 'csv':
        return csv_response('журнал_аудита.csv', ['Когда', 'Кто', 'Действие', 'Объект', 'id', 'Детали'],
                            [[e['at'], e['by'], e['action_label'], e['entity_label'], e['entity_id'],
                              '; '.join(('%s: %s' % kv) if kv[0] else kv[1] for kv in e['details'])]
                             for e in items])
    page = paginate_list(items, request.args.get('page', 1), AUDIT_PAGE_SIZE)
    today = today_almaty()
    rows, last = [], None
    for e in page.items:
        label = _day_label(e['at'], today)
        if label != last:
            rows.append({'day': label})
            last = label
        rows.append(e)
    facets = {
        'actions': sorted({(e['action'], e['action_label']) for e in entries if e['action']}, key=lambda x: x[1]),
        'entities': sorted({(e['entity'], e['entity_label']) for e in entries if e['entity']}, key=lambda x: x[1]),
        'admins': sorted({e['by'] for e in entries if e['by']}),
    }
    stats = {
        'total': len(items), 'admins': len({e['by'] for e in items if e['by']}),
        'today': sum(1 for e in items if e['at'] and to_almaty(e['at']).date() == today),
        'deletes': sum(1 for e in items if e['action'].startswith(('delete', 'bulk_delete'))),
        'scanned': len(entries), 'limit_hit': len(entries) >= AUDIT_SCAN_LIMIT,
    }
    return render_template('audit.html', page=page, rows=rows, facets=facets, stats=stats, period=period,
                           periods=PERIODS, date_from=date_from, date_to=date_to, action=action, entity=entity,
                           by=by, q=q, error=error, scan_limit=AUDIT_SCAN_LIMIT, now=now_utc())
