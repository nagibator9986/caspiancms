# cms/routes_home.py — главная «/» (обзор колледжа), поиск Ctrl+K /api/search и журнал аудита /audit
# (маршруты — FOUNDATION, шаблон audit.html — PEOPLE).
#
# Главная (UX_SPEC_v2 §3): приоритет — расписание. KPI: пары сегодня, накладки (cms.conflicts.summary(),
# None → прочерк), обращения без ответа, идущие опросы, студенты (count()). «Сегодня в колледже» и свободные
# аудитории — из общего снапшота расписания; «Требует внимания» — накладки → обращения → предупреждения →
# заказы → лиды; ближайшие опросы и события; последние действия без входов/выходов; приём и Caspian Coin —
# одна приглушённая строка внизу. Всё кэшируется на 60 с ('home:*'), аудит — 'audit:*'.
#
# /api/search: группы, преподаватели, аудитории, люди, новости и опросы — только из кэшей (снапшот расписания,
# индекс пользователей и контента на 5 минут), до 6 результатов на тип и 30 всего.
#
# /audit: последние записи cms_audit (по диапазону дат — только поле at, без составных индексов),
# фильтры по действию, объекту, администратору и тексту — в памяти, пагинация, CSV.

import re
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore
from flask import current_app, jsonify, render_template, request, session

from . import conflicts
from . import core as _core  # nav_badges не импортируем по имени: core ищет функцию nav_badges в модулях
from .core import (ALMATY, Routes, admin_required, cache, count, csv_response, db, doc_dict, fmt_dt, fs_opts, json_safe,
                   log, nav_url, now_almaty, now_utc, paginate_list, plural, to_almaty, to_datetime, today_almaty,
                   where)
from .schedule_model import (DAY_NAMES_FULL, academic_week_number, current_week_type, get_all_groups,
                             get_all_teachers, load_lessons_snapshot, norm_text, room_key)

routes = Routes()

DESC = firestore.Query.DESCENDING
HOME_TTL = 60
AUDIT_PAGE_SIZE = 50
AUDIT_SCAN_LIMIT = 3000      # сколько последних записей журнала просматривается фильтрами
AUDIT_TTL = 120
REG_DAYS = 7

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
    'upload': 'Загрузка', 'replace': 'Замена файла',
    'promote': 'Перевод на следующий курс', 'promote_rollback': 'Отмена перевода на курс',
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
    'conflict': 'Накладка', 'schedule_conflict_acks': 'Накладка', 'academic_calendar': 'Академический календарь',
}
DETAIL_LABELS = {
    'role': 'Роль', 'name': 'Имя', 'email': 'Email', 'title': 'Название', 'count': 'Количество', 'ids': 'Объекты',
    'from': 'Было', 'to': 'Стало', 'status': 'Статус', 'users': 'Пользователей', 'lessons': 'Занятий',
    'changes': 'Изменения', 'rows': 'Строк', 'format': 'Формат', 'value': 'Значение', 'group': 'Группа',
    'subject': 'Дисциплина', 'note': 'Заметка', 'amount': 'Сумма', 'reason': 'Причина', 'login': 'Логин',
    'phone': 'Телефон', 'course': 'Курс', 'failed': 'Ошибки', 'names': 'Названия', 'photo': 'Фото',
    'groups': 'Группы', 'file': 'Файл', 'size': 'Размер, байт', 'notify': 'Уведомление', 'version': 'Версия',
    'moved': 'Переведено', 'restored': 'Возвращено', 'year': 'Год набора',
}
# Объект журнала → (endpoint, имя параметра) для ссылки «открыть» (удалённые объекты не ссылаются).
ENTITY_LINKS = {
    'user': ('admin_edit_user', 'user_id'), 'teacher': ('teacher_edit', 'uid'),
    'applicant': ('applicant_detail', 'uid'), 'applicant_crm': ('applicant_detail', 'uid'),
    'news': ('news_form', 'news_id'), 'poll': ('poll_form', 'poll_id'), 'event': ('event_form', 'event_id'),
    'club': ('club_form', 'club_id'), 'product': ('product_form', 'product_id'),
    'management': ('administration_edit', 'admin_id'), 'academic_calendar': ('academic_calendar', 'q'),
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


def _registrations(days=7):
    """Регистрации абитуриентов по дням (Алматы) за последние days дней — одна выборка с select()."""
    start_day = today_almaty() - timedelta(days=days - 1)
    since = datetime(start_day.year, start_day.month, start_day.day, tzinfo=ALMATY).astimezone(timezone.utc)
    q = where(where(db.collection('users'), 'role', '==', 'applicant'), 'createdAt', '>=', since)
    per_day = Counter()
    for snap in q.select(['createdAt']).limit(5000).stream(**fs_opts(True)):
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
    """Счётчики людей (count()) и регистрации абитуриентов за 7 дней (для строки «Приём»). Кэш 60 с."""
    def load():
        data = {'counts': {}, 'reg7': None}
        try:
            data['counts'] = _people_counts()
        except Exception:
            log.exception('KPI главной: count() не удался')
        try:
            data['reg7'] = sum(d['count'] for d in _registrations(REG_DAYS))
        except Exception:
            log.exception('KPI главной: регистрации не загрузились')
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


def _hm(minutes):
    return '%02d:%02d' % (minutes // 60, minutes % 60)


def _today_lessons(snap, today):
    """Занятия сегодня — как в приложении: занятие только с date (старый веб) идёт КАЖДУЮ неделю в этот
    день (matchesWeekday берёт из date день недели); разовое — только явный one_off + date (conflicts.is_one_off)."""
    wd = today.weekday()
    week = current_week_type(today)
    iso = today.isoformat()
    return [l for l in snap.by_day.get(wd, [])
            if (not conflicts.is_one_off(l) or l['date'] == iso) and l['week'] in ('always', week)]


def _today_schedule(now):
    """Сегодняшние занятия из снапшота: сколько пар, групп, преподавателей, что идёт сейчас,
    номер текущей пары, свободные аудитории в текущую пару."""
    today = now.date()
    wd = today.weekday()
    week = current_week_type(today)
    info = {'weekday': DAY_NAMES_FULL[wd], 'week': week, 'week_no': academic_week_number(today),
            'lessons': 0, 'groups': 0, 'teachers': 0, 'now': [], 'slot': None, 'next_slot': None,
            'finished': False, 'not_started': False, 'error': False, 'timeline': [], 'free_rooms': None,
            'rooms_total': 0}
    try:
        snap = load_lessons_snapshot()
    except Exception:
        log.exception('Главная: снапшот расписания недоступен')
        info['error'] = True
        return info
    lessons = _today_lessons(snap, today)
    minute = now.hour * 60 + now.minute
    info['lessons'] = len(lessons)
    info['groups'] = len({l['groupId'] for l in lessons if l['groupId']})
    info['teachers'] = len({l['teacher_key'] for l in lessons if l['teacher_key']})
    info['now'] = [l for l in lessons if l['start'] is not None and l['end'] is not None
                   and l['start'] <= minute < l['end']]
    # загрузка по времени начала: сколько занятий в каждой паре сегодня
    by_time = {}
    for l in lessons:
        if l['start'] is None:
            continue
        key = (l['start'], l['end'] if l['end'] is not None else l['start'])
        by_time[key] = by_time.get(key, 0) + 1
    peak = max(by_time.values()) if by_time else 0
    info['timeline'] = [{'label': '%s–%s' % (_hm(s), _hm(e)), 'count': n,
                         'pct': round(n * 100.0 / peak, 1) if peak else 0, 'current': s <= minute < e,
                         'past': e <= minute}
                        for (s, e), n in sorted(by_time.items())]
    # «идёт N-я пара» — номер по сегодняшним интервалам занятий (сетка звонков содержит служебные слоты)
    for i, t in enumerate(info['timeline']):
        if t['current']:
            info['slot'] = {'label': t['label'], 'number': i + 1}
            break
    later = sorted({l['start'] for l in lessons if l['start'] is not None and l['start'] > minute})
    ends = [l['end'] for l in lessons if l['end'] is not None]
    if later:
        info['next_slot'] = _hm(later[0])
    starts = [l['start'] for l in lessons if l['start'] is not None]
    info['not_started'] = bool(starts) and minute < min(starts)
    info['finished'] = bool(lessons) and bool(ends) and minute >= max(ends)
    info['free_rooms'], info['rooms_total'] = _free_rooms_now(snap, lessons, minute)
    return info


def _free_rooms_now(snap, lessons, minute):
    """Свободные сейчас аудитории: все физические аудитории снапшота минус занятые в эту минуту.
    → (число | None вне учебного времени, всего аудиторий)."""
    rooms = snap.rooms()
    busy_now = [l for l in lessons if l['start'] is not None and l['end'] is not None and l['start'] <= minute < l['end']]
    if not busy_now:
        return None, len(rooms)
    busy = {l['room_key'] for l in busy_now if l['room_key']}
    return len([k for k, _name in rooms if k not in busy]), len(rooms)


def _event_day(value):
    """Дата события (date по Алматы) из Timestamp или строки 'YYYY-MM-DD' (старые записи)."""
    if value in (None, ''):
        return None
    if isinstance(value, str):
        try:
            return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    dt = to_almaty(value)
    return dt.date() if dt else None


def _content_now():
    """Активные опросы (до 3) и события на 7 дней вперёд (до 4). Кэш 60 с ('home:content').
    События: дата хранится Timestamp'ом (новые) или строкой 'YYYY-MM-DD' (старые) — два запроса
    с одним полем в диапазоне (составной индекс не нужен)."""
    def load():
        now = now_utc()
        data = {'polls': [], 'polls_active': 0, 'polls_next_end': None, 'events': [], 'error': False}
        try:
            for d in db.collection('polls').select(['title', 'endTime', 'createdAt']).order_by(
                    'createdAt', direction=DESC).limit(30).stream(**fs_opts(True)):
                p = d.to_dict() or {}
                end = to_datetime(p.get('endTime'))
                if end is not None and end <= now:
                    continue
                left = None
                if end is not None:
                    days = int((end - now).total_seconds() // 86400)
                    left = ('ещё %d %s' % (days, plural(days, 'день', 'дня', 'дней')) if days >= 1 else 'меньше суток')
                data['polls'].append({'id': d.id, 'title': p.get('title') or 'Опрос без названия', 'end': end, 'left': left})
            data['polls_active'] = len(data['polls'])
            ends = [p['end'] for p in data['polls'] if p['end']]
            data['polls_next_end'] = min(ends) if ends else None
            data['polls'] = data['polls'][:3]
        except Exception:
            log.exception('Главная: опросы не загрузились')
            data['error'] = True
        try:
            today = today_almaty()
            start = datetime(today.year, today.month, today.day, tzinfo=ALMATY)
            end = start + timedelta(days=8)
            evs = db.collection('events')
            found = {}
            queries = [
                where(where(evs, 'date', '>=', start.astimezone(timezone.utc)), 'date', '<', end.astimezone(timezone.utc)),
                where(where(evs, 'date', '>=', today.isoformat()), 'date', '<', (today + timedelta(days=8)).isoformat()),
            ]
            for q in queries:
                for d in q.order_by('date').limit(4).stream(**fs_opts(True)):
                    e = d.to_dict() or {}
                    day = _event_day(e.get('date'))
                    if day is None:
                        continue
                    found[d.id] = {'id': d.id, 'title': e.get('title') or 'Событие', 'day': day,
                                   'time': str(e.get('time') or '')[:20], 'location': str(e.get('location') or '')[:60]}
            data['events'] = sorted(found.values(), key=lambda x: (x['day'], x['time']))[:4]
        except Exception:
            log.exception('Главная: события не загрузились')
            data['error'] = True
        return data
    return cache.get_or_set('home:content', load, ttl=HOME_TTL)


HIDDEN_FEED_ACTIONS = ('login', 'logout')


def _recent_audit(limit=8):
    """Последние действия админов без входов/выходов: читаем 24 записи, берём limit."""
    def load():
        q = db.collection('cms_audit').order_by('at', direction=DESC).limit(24)
        rows = [_audit_row(doc_dict(d)) for d in q.stream(**fs_opts(True))]
        return [r for r in rows if r['action'] not in HIDDEN_FEED_ACTIONS][:limit]
    try:
        return cache.get_or_set('audit:recent', load, ttl=HOME_TTL)
    except Exception:
        log.exception('Главная: журнал аудита недоступен')
        return []


def _first_name(full):
    """Имя из ФИО «Фамилия Имя Отчество» (для приветствия); одно слово — как есть."""
    parts = str(full or '').split()
    return parts[1] if len(parts) >= 2 else (parts[0] if parts else '')


def _day_word(d, today):
    if d == today:
        return 'сегодня'
    if d == today + timedelta(days=1):
        return 'завтра'
    return '%s, %s' % (d.strftime('%d.%m'), ('пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс')[d.weekday()])


@routes.route('/')
@admin_required
def dashboard():
    """Обзор: сегодня в расписании, что требует внимания, ближайшие опросы и события, быстрые действия,
    последние действия админов; приём и Caspian Coin — одной приглушённой строкой внизу (UX_SPEC_v2 §3)."""
    now = now_almaty()
    kpi = _kpi()
    counts = kpi.get('counts') or {}
    orders, feedback = _attention_counts()
    summary = _conflicts_summary()
    today = _today_schedule(now)
    content = _content_now()
    critical = (summary or {}).get('critical') or 0
    warnings = (summary or {}).get('warning') or 0
    # Требует внимания: накладки → обращения → предупреждения → заказы → лиды (приглушённо, последними)
    attention = []
    if critical:
        attention.append({'icon': 'exclamation-octagon', 'tone': 'danger', 'count': critical,
                          'label': 'Критичные накладки в расписании',
                          'href': nav_url('schedule_conflicts', severity='critical')})
    if feedback:
        attention.append({'icon': 'chat-dots', 'tone': 'warning', 'count': feedback,
                          'label': 'Обращения без ответа',
                          'href': nav_url('feedback_list', status='new')})
    if warnings:
        attention.append({'icon': 'exclamation-triangle', 'tone': 'neutral', 'count': warnings,
                          'label': 'Предупреждения в расписании',
                          'href': nav_url('schedule_conflicts', severity='warning')})
    if orders:
        attention.append({'icon': 'box-seam', 'tone': 'neutral', 'count': orders,
                          'label': 'Заказы мерча ждут выдачи',
                          'href': nav_url('coins_orders')})
    if counts.get('crm_new'):
        n = counts['crm_new']
        attention.append({'icon': 'person-lines-fill', 'tone': 'muted', 'count': n,
                          'label': 'Новые лиды — никто не связался',
                          'href': nav_url('applicants_leads', status='new')})
    tday = today_almaty()
    for e in content['events']:
        e['when'] = _day_word(e['day'], tday)
    admissions = {'reg7': kpi.get('reg7'), 'crm_new': counts.get('crm_new') or 0, 'orders': orders or 0}
    admissions['show'] = any(admissions[k] for k in ('reg7', 'crm_new', 'orders'))
    return render_template(
        'dashboard.html', now=now, greeting=_greeting(now), counts=counts, orders=orders, feedback=feedback,
        summary=summary, today=today, attention=attention, content=content, admissions=admissions,
        recent=_recent_audit(), admin_first=_first_name(session.get('admin_name')))


# ==================== ПОИСК Ctrl+K (/api/search, UX_SPEC_v2 §2.5) ====================

SEARCH_PER_TYPE = 6
SEARCH_MAX = 30
SEARCH_TTL = 300
ROLE_LABELS = {'student': 'Студент', 'teacher': 'Преподаватель', 'admin': 'Администратор', 'club': 'Клуб',
               'applicant': 'Абитуриент'}


def _compact(s):
    """Для групп: без пробелов и дефисов ('по241' → 'ПО 24-1')."""
    return re.sub(r'[\s\-–—_.]+', '', norm_text(s))


def _matches(hay, words):
    return all(w in hay for w in words)


def _search_users():
    """Индекс людей для поиска: общий снапшот раздела «Люди» (people:users — все пользователи без лимита,
    кэш 5 мин; сбрасывается записями в users из «Людей» и CRM). Строки: id, name, email, role, group, phone,
    login, hay. Раньше был отдельный скан users с limit(5000) по id — поздние uid в поиск не попадали."""
    from .routes_people import _users_snapshot  # модуль «Люди» владеет снапшотом пользователей
    return _users_snapshot()


def _search_content():
    """Последние новости и опросы для поиска (кэш 5 мин)."""
    def load():
        out = []
        for coll, kind in (('news', 'news'), ('polls', 'poll')):
            try:
                for d in db.collection(coll).select(['title', 'createdAt']).order_by(
                        'createdAt', direction=DESC).limit(200).stream(**fs_opts(True)):
                    x = d.to_dict() or {}
                    title = str(x.get('title') or '').strip()
                    if title:
                        out.append({'id': d.id, 'kind': kind, 'title': title, 'at': to_datetime(x.get('createdAt')),
                                    'hay': norm_text(title)})
            except Exception:
                log.warning('Поиск: коллекция %s недоступна', coll, exc_info=True)
        return out
    return cache.get_or_set('search:content', load, ttl=SEARCH_TTL)


def search_entities(q):
    """Сущности для палитры Ctrl+K: группы, преподаватели, аудитории, люди, новости и опросы."""
    q = (q or '').strip()
    if len(q) < 2 or len(q) > 60:
        return []
    words = norm_text(q).split()
    compact = _compact(q)
    results = []
    try:
        snap = load_lessons_snapshot()
    except Exception:
        log.exception('Поиск: снапшот расписания недоступен')
        snap = None

    # группы
    try:
        groups = get_all_groups()
    except Exception:
        groups = []
    found = []
    for g in groups:
        hay = norm_text(g)
        if _matches(hay, words) or (compact and compact in _compact(g)):
            found.append(g)
    for g in found[:SEARCH_PER_TYPE]:
        n = len(snap.by_group.get(g, [])) if snap else 0
        results.append({'type': 'group', 'section': 'Группы', 'label': g,
                        'sub': '%d %s в расписании' % (n, plural(n, 'занятие', 'занятия', 'занятий')) if n else 'нет занятий',
                        'url': nav_url('schedule', group=g), 'icon': 'collection',
                        'actions': [{'label': 'Студенты', 'url': nav_url('admin_users', role='student', group=g)}]})

    # преподаватели
    try:
        teachers = get_all_teachers()
    except Exception:
        teachers = {}
    teacher_uids = set()
    try:
        teacher_uids = {u['id'] for u in _search_users() if u['role'] == 'teacher'}
    except Exception:
        pass
    found = [(tid, name) for tid, name in teachers.items() if _matches(norm_text(name), words)]
    for tid, name in found[:SEARCH_PER_TYPE]:
        n = len(snap.by_teacher.get('uid:%s' % tid, [])) if snap else 0
        item = {'type': 'teacher', 'section': 'Преподаватели', 'label': name,
                'sub': '%d %s в неделю' % (n, plural(n, 'пара', 'пары', 'пар')) if n else 'нет занятий',
                'url': nav_url('schedule_teachers', teacher=tid), 'icon': 'person-badge', 'actions': []}
        if tid in teacher_uids:
            item['actions'].append({'label': 'Карточка', 'url': nav_url('teacher_edit', uid=tid)})
        results.append(item)

    # аудитории
    if snap:
        rq = room_key(q) or compact
        rooms = [(k, name) for k, name in snap.rooms() if rq and (rq in k or _matches(norm_text(name), words))]
        busy_now = set()
        try:
            now = now_almaty()
            minute = now.hour * 60 + now.minute
            busy_now = {l['room_key'] for l in _today_lessons(snap, now.date())
                        if l['start'] is not None and l['end'] is not None and l['start'] <= minute < l['end']}
        except Exception:
            pass
        for k, name in rooms[:SEARCH_PER_TYPE]:
            n = len(snap.by_room.get(k, []))
            results.append({'type': 'room', 'section': 'Аудитории', 'label': name,
                            'sub': ('сейчас занята' if k in busy_now else 'сейчас свободна') + ' · %d %s в неделю' % (
                                n, plural(n, 'занятие', 'занятия', 'занятий')),
                            'url': nav_url('schedule_rooms', room=name), 'icon': 'door-open'})

    # люди (абитуриенты — последними)
    try:
        users = _search_users()
    except Exception:
        log.exception('Поиск: пользователи недоступны')
        users = []
    people = [u for u in users if u['role'] != 'teacher' and _matches(u['hay'], words)]
    people.sort(key=lambda u: (u['role'] == 'applicant', norm_text(u['name'] or u['email'])))
    for u in people[:SEARCH_PER_TYPE]:
        role = ROLE_LABELS.get(u['role'], u['role'] or 'Без роли')
        sub = ' · '.join([x for x in (role, u['group'] or u['email'] or u['login']) if x])
        url = nav_url('applicant_detail', uid=u['id']) if u['role'] == 'applicant' else nav_url('admin_edit_user', user_id=u['id'])
        results.append({'type': 'user', 'section': 'Люди', 'label': u['name'] or u['email'] or u['login'] or u['id'],
                        'sub': sub, 'url': url, 'icon': 'mortarboard' if u['role'] == 'applicant' else 'person'})

    # новости и опросы
    try:
        content = _search_content()
    except Exception:
        content = []
    for c in [c for c in content if _matches(c['hay'], words)][:SEARCH_PER_TYPE]:
        when = fmt_dt(c['at'], '%d.%m.%Y', '') if c['at'] else ''
        if c['kind'] == 'news':
            results.append({'type': 'news', 'section': 'Новости и опросы', 'label': c['title'],
                            'sub': 'Новость' + (' · ' + when if when else ''), 'url': nav_url('news_form', news_id=c['id']),
                            'icon': 'newspaper'})
        else:
            results.append({'type': 'poll', 'section': 'Новости и опросы', 'label': c['title'],
                            'sub': 'Опрос' + (' · ' + when if when else ''), 'url': nav_url('poll_results', poll_id=c['id']),
                            'icon': 'ui-checks', 'actions': [{'label': 'Редактировать', 'url': nav_url('poll_form', poll_id=c['id'])}]})
    return [r for r in results if r.get('url') and r['url'] != '#'][:SEARCH_MAX]


@routes.route('/api/search')
@admin_required
def api_search():
    """Поиск сущностей для Ctrl+K: {q, results: [{type, section, label, sub, url, icon, actions}]}."""
    q = (request.args.get('q') or '').strip()
    try:
        results = search_entities(q)
    except Exception:
        log.exception('Поиск /api/search упал')
        results = []
    return jsonify({'q': q, 'results': results})


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
        return [_audit_row(doc_dict(d)) for d in q.stream(**fs_opts(True))]
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
