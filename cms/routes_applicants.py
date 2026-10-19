# cms/routes_applicants.py — раздел «Абитуриенты»: аналитика приёма, лиды (CRM), карточка
# абитуриента, QR-источники (владелец — APPLICANTS).
#
# Схема данных — как в мобильном приложении (lib/services/applicant_service.dart, SPEC §2):
#   users/{uid}               role 'applicant', login, email (синтетический …@abiturient… или реальный),
#                             name, phone, city, school, grade, specialties[], source, consents,
#                             marketingConsent, createdAt, refCode, referredBy, referralsRewarded,
#                             mustChangePassword, passwordResetAt
#   applicant_crm/{uid}       status (new|contacted|applied|enrolled|declined), notes, updatedAt, updatedBy,
#                             createdAt, source, login, history[{at, by, action, details}], lastContactAt,
#                             nextStep (≤ 200), nextStepAt (дата следующего шага, 00:00 Алматы) — пишет только CMS
#   applicant_sources/{CODE}  title, channel, note, welcomeCoins, active, scans, registrations, createdAt…
#   coin_wallets/{uid}        balance, earnedTotal, spentTotal (пишет только Cloud Function)
#   coin_transactions         ручная операция: {uid, amount, type:'manual', reason, createdBy, createdAt}
#   coin_orders               заказы мерча (uid, status, title, price, variant…)
# Перевод в студенты — как AdminApplicantsRepository.convertToStudent: role 'student', group, course,
# attendance '0%', grades 'N/A' + applicant_crm.status = 'enrolled'. Сброс пароля — SPEC D5.
#
# Аналитика и список лидов читают СНАПШОТ (users role==applicant + applicant_crm + coin_wallets
# через select()) с кэшем 5 минут; любая запись из этого модуля сбрасывает кэш «applicants:*».
# Карточка абитуриента читает живые данные (параллельно, без N+1).

import re
import secrets
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore
from flask import abort, flash, jsonify, redirect, render_template, request, url_for
from werkzeug.datastructures import MultiDict
from urllib.parse import parse_qs, urlsplit

from .applicant_login import (RESET_PASSWORD, display_login, is_synthetic_email, normalize_phone,
                              to_auth_email)
from .core import (ALMATY, DELETE_FIELD, ArrayUnion, IS_EMULATOR, SERVER_TIMESTAMP, Routes, admin_required, audit,
                   back_url, cache, chunked, count, csv_response, current_admin, db, fb_auth, flash_action,
                   flash_error, fmt_dt, fmt_phone, get_docs, here, is_partial, log, next_target,
                   now_utc, paginate_list, safe_next_url, to_almaty, to_datetime, today_almaty, view_param,
                   wants_json, where)

routes = Routes()

DESC = firestore.Query.DESCENDING
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

SNAPSHOT_KEY = 'applicants:snapshot'
SNAPSHOT_TTL = 300          # с — снапшот абитуриентов (SPEC §6.5)
DIRECTORY_TTL = 300         # с — QR-источники, кампании
ADMISSIONS_TTL = 120        # с — app_config/admissions (пишет модуль COINS, наш кэш короткий)

# ---------- справочники (значения — как в приложении) ----------

STATUSES = [('new', 'Новый'), ('contacted', 'Связались'), ('applied', 'Подал документы'),
            ('enrolled', 'Поступил'), ('declined', 'Отказ')]
STATUS_KEYS = [k for k, _ in STATUSES]
STATUS_LABELS = dict(STATUSES)

GRADES = [('9', '9 класс'), ('10', '10 класс'), ('11', '11 класс'), ('college', 'Учится в колледже'),
          ('graduate', 'Окончил(а) школу')]
GRADE_LABELS = dict(GRADES)

CHANNELS = [('stand', 'Стенд'), ('school', 'Школа'), ('event', 'Мероприятие'), ('social', 'Соцсети'),
            ('print', 'Печать'), ('partner', 'Партнёр'), ('other', 'Другое')]
CHANNEL_LABELS = dict(CHANNELS)
CHANNEL_ICONS = {'stand': 'shop-window', 'school': 'building', 'event': 'calendar-event', 'social': 'instagram',
                 'print': 'printer', 'partner': 'people', 'other': 'three-dots'}

CONTACT_CHANNELS = [('call', 'Звонок'), ('whatsapp', 'WhatsApp'), ('meeting', 'Встреча'), ('sms', 'SMS'),
                    ('other', 'Другое')]

HISTORY_LABELS = {
    'created': ('Регистрация', 'person-plus'), 'status': ('Статус', 'flag'), 'notes': ('Заметка', 'sticky'),
    'contact': ('Контакт', 'telephone'), 'coins': ('Коины', 'coin'), 'password_reset': ('Сброс пароля', 'key'),
    'converted': ('Перевод в студенты', 'mortarboard'), 'profile': ('Профиль', 'person-gear'),
    'next_step': ('Следующий шаг', 'signpost'),
}
# Вид записи в ленте активности (фильтр-чипы карточки): работа с лидом, коины, заказы, система
HISTORY_KINDS = {'status': 'work', 'notes': 'work', 'contact': 'work', 'next_step': 'work', 'coins': 'coins',
                 'password_reset': 'account', 'converted': 'work', 'profile': 'account', 'created': 'account'}
FEED_KINDS = [('work', 'Работа с лидом'), ('coins', 'Коины'), ('orders', 'Заказы'), ('account', 'Аккаунт')]
TX_LABELS = {'campaign': ('Код кампании', 'ticket-perforated'), 'welcome': ('Приветственные коины', 'gift'),
             'quest': ('Квест', 'trophy'), 'referral': ('Реферал', 'people'), 'manual': ('Коины', 'coin')}
ORDER_LABELS = {'reserved': 'ждёт выдачи', 'issued': 'выдан', 'cancelled': 'отменён', 'rejected': 'отклонён',
                'pending': 'обрабатывается'}
NEXT_STEP_MAX = 200
STEP_FILTERS = [('overdue', 'Просрочен'), ('today', 'На сегодня'), ('planned', 'Запланирован'),
                ('none', 'Не назначен')]
BOARD_LIMIT = 40            # карточек в колонке канбана (остальные — в таблице)
VIEWS = ('table', 'board')

PERIODS = [('today', 'Сегодня'), ('7', '7 дней'), ('30', '30 дней'), ('90', '90 дней')]
SORTS = [('new', 'Сначала новые'), ('old', 'Сначала старые'), ('name', 'По имени'),
         ('profile', 'По заполненности профиля'), ('contact', 'Давно без контакта')]
PER_PAGE_OPTIONS = (25, 50, 100)
DASH_DAYS = (30, 90, 180)

# Поля снапшота (select — только нужное для списков и аналитики).
USER_FIELDS = ['login', 'email', 'name', 'phone', 'city', 'school', 'grade', 'specialties', 'source', 'createdAt',
               'refCode', 'referredBy', 'referralsRewarded', 'marketingConsent', 'mustChangePassword']
CRM_FIELDS = ['status', 'notes', 'updatedAt', 'lastContactAt', 'createdAt', 'source', 'login', 'nextStep',
              'nextStepAt']
WALLET_FIELDS = ['balance', 'earnedTotal', 'spentTotal']

SOURCE_CODE_RE = re.compile(r'^[A-Z0-9]{3,24}$')
RESERVED_SOURCES = {'APP', 'WEB'}          # служебные значения users.source (приложение / сайт без QR)
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
MAX_COINS = 100000
MAX_WELCOME = 1000
BULK_LIMIT = 500

PROFILE_FIELDS = [('name', 'ФИО'), ('phone', 'Телефон'), ('city', 'Город'), ('school', 'Школа'),
                  ('grade', 'Класс'), ('specialties', 'Специальности')]


# ==================== ОБЩИЕ ПОМОЩНИКИ ====================

def _norm(s):
    """Строка для поиска: нижний регистр, ё→е, без лишних пробелов."""
    return ' '.join(str(s or '').lower().replace('ё', 'е').split())


def _str(v, maxlen=None):
    s = str(v).strip() if v is not None else ''
    return s[:maxlen] if maxlen else s


def _int(v, default=0):
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).replace(' ', '').replace(' ', '').strip()))
    except (TypeError, ValueError):
        return default


def _field(name, maxlen=None):
    v = (request.form.get(name) or '').strip()
    return v[:maxlen] if maxlen else v


def _admin():
    return current_admin() or {}


def _history_entry(action, details=''):
    """Запись CRM-истории (SPEC §2.2). Время — обычный datetime: SERVER_TIMESTAMP в массиве нельзя."""
    adm = _admin()
    return {'at': now_utc(), 'by': adm.get('email') or adm.get('uid') or 'admin',
            'action': action, 'details': _str(details, 300)}


def invalidate_applicants_cache():
    """Сбросить снапшот, аналитику и бейдж «новые лиды» (после любой записи)."""
    cache.invalidate_prefix('applicants:')
    cache.invalidate('nav:badges')


def _back(endpoint, **values):
    nxt = safe_next_url(request.form.get('next') or request.args.get('next'))
    return redirect(nxt or url_for(endpoint, **values))


def profile_filled(u):
    """Сколько из 6 полей профиля заполнено и заполнен ли он целиком (SPEC §2.1, как в функциях)."""
    name = _str(u.get('name'))
    specs = u.get('specialties') if isinstance(u.get('specialties'), list) else []
    checks = [len(name) >= 2, bool(_str(u.get('phone'))), bool(_str(u.get('city'))),
              bool(_str(u.get('school'))), bool(_str(u.get('grade'))), len(specs) >= 1]
    n = sum(1 for c in checks if c)
    return n, n == len(checks)


def display_login_of(u):
    """Логин для показа — как ApplicantLogin.displayOf (APP): поле login, иначе по email."""
    login = _str(u.get('login'))
    if login:
        email = to_auth_email(login)
        return display_login(email) if email else login
    email = _str(u.get('email'))
    return display_login(email) if email else ''


def wa_link(phone):
    """Ссылка WhatsApp (https://wa.me/7…) или None."""
    n = normalize_phone(phone) if phone else None
    return 'https://wa.me/' + n[1:] if n else None


def _local_date(dt):
    a = to_almaty(dt)
    return a.date() if a else None


def _pct(part, total):
    return round(100.0 * part / total, 1) if total else 0.0


def next_step_info(crm, today=None):
    """Следующий шаг лида: {text, at (datetime), date (дата Алматы), state: none|planned|today|overdue}."""
    text = _str((crm or {}).get('nextStep'), NEXT_STEP_MAX)
    at = to_datetime((crm or {}).get('nextStepAt'))
    day = _local_date(at) if at else None
    if not text and not day:
        state = 'none'
    else:
        today = today or today_almaty()
        state = 'planned' if day is None or day > today else ('today' if day == today else 'overdue')
    return {'text': text, 'at': at, 'date': day, 'state': state}


@routes.template_global('ap_step_label')
def step_label(state, day, today=None):
    """«сегодня», «завтра», «просрочен на 2 дня», «до 25.10» — подпись даты шага."""
    if not day:
        return ''
    today = today or today_almaty()
    delta = (day - today).days
    if delta == 0:
        return 'сегодня'
    if delta == 1:
        return 'завтра'
    if delta < 0:
        n = -delta
        return 'просрочен на %d %s' % (n, 'день' if n % 10 == 1 and n % 100 != 11 else
                                        ('дня' if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else 'дней'))
    return day.strftime('%d.%m')


# ==================== СПРАВОЧНИКИ (кэш) ====================

def load_sources():
    """QR-источники: {CODE: {id, title, channel, …}} (кэш 5 мин)."""
    def load():
        out = {}
        for s in db.collection('applicant_sources').stream():
            d = s.to_dict() or {}
            d['id'] = s.id
            out[s.id] = d
        return out
    return cache.get_or_set('applicants:sources', load, DIRECTORY_TTL)


def load_campaign_titles():
    """Коды кампаний Caspian Coin → название (старые версии приложения писали код в users.source)."""
    def load():
        return {s.id: _str((s.to_dict() or {}).get('title')) for s in
                db.collection('coin_campaigns').select(['title']).stream()}
    try:
        return cache.get_or_set('applicants:campaigns', load, DIRECTORY_TTL)
    except Exception:
        log.exception('Кампании коинов не прочитаны')
        return {}


def load_admissions():
    """app_config/admissions (контакты, privacyUrl, сторы, каталог). Пустой dict, если документа нет."""
    def load():
        snap = db.collection('app_config').document('admissions').get()
        return (snap.to_dict() or {}) if snap.exists else {}
    try:
        return cache.get_or_set('applicants:admissions', load, ADMISSIONS_TTL)
    except Exception:
        log.exception('app_config/admissions не прочитан')
        return {}


def catalog_titles(adm=None):
    """Названия специальностей из каталога приёмной (и старого списка specialties)."""
    adm = adm if adm is not None else load_admissions()
    titles = []
    for raw in list(adm.get('catalog') or []) + list(adm.get('specialties') or []):
        t = _str(raw.get('title')) if isinstance(raw, dict) else _str(raw)
        if t and t not in titles:
            titles.append(t)
    return titles


def source_info(code, sources=None, campaigns=None):
    """Как показывать users.source: {code, title, kind (qr|app|web|campaign|other|none), icon, channel}."""
    c = _str(code)
    if not c:
        return {'code': '', 'title': 'Не указан', 'kind': 'none', 'icon': 'question-circle', 'channel': ''}
    sources = load_sources() if sources is None else sources
    if c in sources:
        s = sources[c]
        ch = s.get('channel') if s.get('channel') in CHANNEL_LABELS else 'other'
        return {'code': c, 'title': _str(s.get('title')) or c, 'kind': 'qr', 'icon': CHANNEL_ICONS[ch], 'channel': ch}
    if c == 'APP':
        return {'code': c, 'title': 'Приложение', 'kind': 'app', 'icon': 'phone', 'channel': ''}
    if c == 'WEB':
        return {'code': c, 'title': 'Сайт (без QR)', 'kind': 'web', 'icon': 'globe2', 'channel': ''}
    campaigns = load_campaign_titles() if campaigns is None else campaigns
    if c in campaigns:
        t = campaigns[c]
        return {'code': c, 'title': 'Кампания «%s»' % t if t else 'Кампания %s' % c, 'kind': 'campaign',
                'icon': 'ticket-perforated', 'channel': ''}
    return {'code': c, 'title': c, 'kind': 'other', 'icon': 'tag', 'channel': ''}


# ==================== СНАПШОТ АБИТУРИЕНТОВ ====================

def _make_lead(uid, u, crm, wallet, sources, campaigns, converted=False):
    name = _str(u.get('name'))
    login = display_login_of(u) or display_login_of(crm)
    email = _str(u.get('email'))
    raw_specs = u.get('specialties') if isinstance(u.get('specialties'), list) else []
    specs = [_str(s) for s in raw_specs if _str(s)]
    phone = _str(u.get('phone'))
    if not phone and _str(u.get('login')).startswith('+'):
        phone = _str(u.get('login'))
    created = to_datetime(u.get('createdAt')) or to_datetime(crm.get('createdAt'))
    source = _str(u.get('source')) or _str(crm.get('source'))
    src = source_info(source, sources, campaigns)
    status = crm.get('status') if crm.get('status') in STATUS_KEYS else 'new'
    filled, complete = profile_filled(u)
    grade = _str(u.get('grade'))
    city = _str(u.get('city'))
    school = _str(u.get('school'))
    notes = _str(crm.get('notes'))
    step = next_step_info(crm)
    return {
        'uid': uid,
        'name': name if len(name) >= 2 else '',
        'title': name if len(name) >= 2 else (login or uid),
        'login': login,
        'login_raw': _str(u.get('login')),
        'email': email,
        'synthetic': is_synthetic_email(email),
        'phone': phone,
        'phone_fmt': fmt_phone(phone) if phone else '',
        'wa': wa_link(phone),
        'tel': normalize_phone(phone) if phone else None,
        'city': city,
        'school': school,
        'grade': grade,
        'grade_label': GRADE_LABELS.get(grade, grade),
        'specialties': specs,
        'source': source,
        'source_title': src['title'],
        'source_kind': src['kind'],
        'source_icon': src['icon'],
        'created': created,
        'status': status,
        'notes': notes,
        'last_contact': to_datetime(crm.get('lastContactAt')),
        'crm_updated': to_datetime(crm.get('updatedAt')),
        'next_step': step['text'], 'next_at': step['at'], 'next_date': step['date'], 'next_state': step['state'],
        'ref_code': _str(u.get('refCode')),
        'referred_by': _str(u.get('referredBy')),
        'referrals': 0,
        'rewarded': _int(u.get('referralsRewarded')),
        'marketing': u.get('marketingConsent') is True,
        'must_change': u.get('mustChangePassword') is True,
        'balance': _int(wallet.get('balance')),
        'earned': _int(wallet.get('earnedTotal')),
        'spent': _int(wallet.get('spentTotal')),
        'filled': filled,
        'complete': complete,
        'converted': converted,
        'group': _str(u.get('group')) if converted else '',
        'search': _norm(' '.join([name, login, email, phone, city, school, notes, _str(u.get('refCode')), uid])),
        'digits': '|'.join(re.sub(r'\D', '', v) for v in (phone, _str(u.get('login')))),
    }


def _load_snapshot():
    t0 = time.time()
    sources = load_sources()
    campaigns = load_campaign_titles()
    users = {}
    q = where(db.collection('users'), 'role', '==', 'applicant').select(USER_FIELDS)
    for s in q.stream():
        users[s.id] = s.to_dict() or {}
    crm = {s.id: (s.to_dict() or {}) for s in db.collection('applicant_crm').select(CRM_FIELDS).stream()}
    wallets = {s.id: (s.to_dict() or {}) for s in db.collection('coin_wallets').select(WALLET_FIELDS).stream()}
    # Зачисленные: convertToStudent меняет role на 'student', но CRM-карточка остаётся — без них
    # воронка и конверсия по источникам врали бы. Удалённые аккаунты (нет users) пропускаем.
    converted = {}
    extra = [uid for uid in crm if uid not in users]
    if extra:
        for uid, d in get_docs('users', extra, field_paths=USER_FIELDS + ['role', 'group']).items():
            if d.get('role') == 'student':
                converted[uid] = d
    leads = [_make_lead(uid, u, crm.get(uid, {}), wallets.get(uid, {}), sources, campaigns)
             for uid, u in users.items()]
    leads += [_make_lead(uid, u, crm.get(uid, {}), wallets.get(uid, {}), sources, campaigns, converted=True)
              for uid, u in converted.items()]
    leads.sort(key=lambda x: (x['created'] or EPOCH, x['uid']), reverse=True)
    by_uid = {x['uid']: x for x in leads}
    for uid, n in Counter(x['referred_by'] for x in leads if x['referred_by']).items():
        if uid in by_uid:
            by_uid[uid]['referrals'] = n
    log.info('Снапшот абитуриентов: %d (+%d зачислены) за %.2f с', len(users), len(converted), time.time() - t0)
    return {'leads': leads, 'by_uid': by_uid, 'sources': sources, 'campaigns': campaigns, 'loaded_at': now_utc()}


def load_snapshot(force=False):
    """Снапшот всех абитуриентов (кэш 5 мин): {'leads': [...], 'by_uid', 'sources', 'campaigns', 'loaded_at'}.
    Элементы НЕ менять — кэш общий."""
    if force:
        cache.invalidate(SNAPSHOT_KEY)
    return cache.get_or_set(SNAPSHOT_KEY, _load_snapshot, SNAPSHOT_TTL)


# ==================== АНАЛИТИКА ====================

def _trend(cur, prev):
    if not prev:
        return None
    return round(100.0 * (cur - prev) / prev, 1)


def _bars(counter, total=None, limit=None, labels=None, order=None):
    """Counter → [{key, label, count, pct, width}] для горизонтальных полос (ширина — от максимума)."""
    items = list(counter.items())
    if order is not None:
        rank = {k: i for i, k in enumerate(order)}
        items.sort(key=lambda kv: (rank.get(kv[0], len(rank)), -kv[1]))
    else:
        items.sort(key=lambda kv: (-kv[1], str(kv[0])))
    if limit:
        items = items[:limit]
    top = max([c for _, c in items] or [0])
    total = sum(counter.values()) if total is None else total
    return [{'key': k, 'label': (labels or {}).get(k, k), 'count': c, 'pct': _pct(c, total),
             'width': round(100.0 * c / top, 1) if top else 0} for k, c in items]


def compute_analytics(snap, days=30, today=None):
    """Все показатели страницы «Аналитика» из снапшота (чистая функция — тестируется)."""
    leads = snap['leads']
    sources = snap['sources']
    today = today or today_almaty()
    total = len(leads)
    dates = [_local_date(x['created']) for x in leads]

    def between(lo, hi):
        return sum(1 for d in dates if d is not None and lo <= d <= hi)

    week = between(today - timedelta(days=6), today)
    prev_week = between(today - timedelta(days=13), today - timedelta(days=7))
    month = between(today - timedelta(days=29), today)
    prev_month = between(today - timedelta(days=59), today - timedelta(days=30))
    complete = sum(1 for x in leads if x['complete'])
    status_counts = Counter(x['status'] for x in leads)
    enrolled = status_counts.get('enrolled', 0)

    # Регистрации по дням (Алматы) + скользящее среднее за 7 дней
    start = today - timedelta(days=days - 1)
    per_day = Counter(d for d in dates if d is not None and start - timedelta(days=6) <= d <= today)
    daily = []
    for i in range(days):
        d = start + timedelta(days=i)
        window = [per_day.get(d - timedelta(days=k), 0) for k in range(7)]
        daily.append({'date': d.isoformat(), 'label': d.strftime('%d.%m'), 'count': per_day.get(d, 0),
                      'avg': round(sum(window) / 7.0, 2)})
    peak = max(daily, key=lambda r: r['count']) if daily else None

    # Воронка CRM: сколько дошло ХОТЯ БЫ до этапа (статусы взаимоисключающие)
    reach = {
        'total': total,
        'contacted': sum(status_counts.get(s, 0) for s in ('contacted', 'applied', 'enrolled')),
        'applied': sum(status_counts.get(s, 0) for s in ('applied', 'enrolled')),
        'enrolled': enrolled,
    }
    funnel, prev = [], total
    for key, label in [('total', 'Зарегистрировались'), ('contacted', 'Связались'),
                       ('applied', 'Подали документы'), ('enrolled', 'Поступили')]:
        n = reach[key]
        funnel.append({'key': key, 'label': label, 'count': n, 'pct': _pct(n, total),
                       'step': _pct(n, prev) if key != 'total' else None})
        prev = n

    # Источники: всего, за неделю, заполнили профиль, поступили
    by_source = {}
    for x, d in zip(leads, dates):
        row = by_source.setdefault(x['source'], {'code': x['source'], 'title': x['source_title'],
                                                 'kind': x['source_kind'], 'icon': x['source_icon'],
                                                 'count': 0, 'complete': 0, 'enrolled': 0, 'week': 0})
        row['count'] += 1
        row['complete'] += 1 if x['complete'] else 0
        row['enrolled'] += 1 if x['status'] == 'enrolled' else 0
        row['week'] += 1 if d is not None and d >= today - timedelta(days=6) else 0
    source_rows = sorted(by_source.values(), key=lambda r: (-r['count'], r['title']))
    top = max([r['count'] for r in source_rows] or [0])
    for r in source_rows:
        r['pct'] = _pct(r['count'], total)
        r['width'] = round(100.0 * r['count'] / top, 1) if top else 0
        r['conv'] = _pct(r['enrolled'], r['count'])

    # Каналы: QR (по типу канала), приложение, сайт, коды кампаний
    channel_counter = Counter()
    for x in leads:
        if x['source_kind'] == 'qr':
            ch = (sources.get(x['source']) or {}).get('channel')
            channel_counter['qr:' + (ch if ch in CHANNEL_LABELS else 'other')] += 1
        else:
            channel_counter[x['source_kind']] += 1
    channel_labels = {'app': 'Приложение', 'web': 'Сайт без QR', 'campaign': 'Коды кампаний',
                      'other': 'Другое', 'none': 'Не указан'}
    channel_labels.update({'qr:' + k: 'QR · ' + v for k, v in CHANNELS})
    channels = _bars(channel_counter, total, labels=channel_labels)

    # QR: сканы → регистрации
    qr_rows = []
    for code, s in sources.items():
        regs = by_source.get(code, {}).get('count', 0)
        scans = _int(s.get('scans'))
        qr_rows.append({'code': code, 'title': _str(s.get('title')) or code, 'active': s.get('active') is True,
                        'channel': CHANNEL_LABELS.get(s.get('channel'), 'Другое'), 'scans': scans,
                        'registrations': regs, 'enrolled': by_source.get(code, {}).get('enrolled', 0),
                        'conv': _pct(regs, scans) if scans else None})
    qr_rows.sort(key=lambda r: (-r['registrations'], -r['scans'], r['title']))
    best_conv = max([r['conv'] for r in qr_rows if r['conv']] or [0])
    for r in qr_rows:   # полоса конверсии — относительно лучшего источника
        r['width'] = round(100.0 * r['conv'] / best_conv, 1) if r['conv'] and best_conv else 0

    # Специальности (каталог приёмной — даже с нулём), города, классы
    spec_counter = Counter({t: 0 for t in catalog_titles()})
    for x in leads:
        for s in x['specialties']:
            spec_counter[s] += 1
    with_specs = sum(1 for x in leads if x['specialties'])
    city_counter, city_label = Counter(), {}
    for x in leads:
        if x['city']:
            k = _norm(x['city'])
            city_counter[k] += 1
            city_label.setdefault(k, x['city'][:1].upper() + x['city'][1:])
    grade_counter = Counter(x['grade'] if x['grade'] in GRADE_LABELS else '' for x in leads)
    grade_labels = dict(GRADE_LABELS)
    grade_labels[''] = 'Не указан'

    # Заполненность полей профиля
    field_rates = []
    for key, label in PROFILE_FIELDS:
        n = sum(1 for x in leads if x[key])
        field_rates.append({'key': key, 'label': label, 'count': n, 'pct': _pct(n, total)})

    # День недели регистрации (Алматы)
    wd_counter = Counter(d.weekday() for d in dates if d is not None)
    weekdays = _bars(Counter({i: wd_counter.get(i, 0) for i in range(7)}), total,
                     labels=dict(enumerate(['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'])), order=list(range(7)))

    # Топ пригласивших
    by_uid = snap['by_uid']
    referrers = Counter(x['referred_by'] for x in leads if x['referred_by'])
    top_referrers = []
    for uid, n in referrers.most_common(10):
        inviter = by_uid.get(uid)
        top_referrers.append({'uid': uid, 'title': inviter['title'] if inviter else uid,
                              'login': inviter['login'] if inviter else '', 'exists': inviter is not None,
                              'count': n, 'complete': sum(1 for x in leads if x['referred_by'] == uid
                                                          and x['complete']),
                              'rewarded': inviter['rewarded'] if inviter else 0})

    coins = {'earned': sum(x['earned'] for x in leads), 'spent': sum(x['spent'] for x in leads),
             'balance': sum(x['balance'] for x in leads), 'holders': sum(1 for x in leads if x['balance'] > 0)}

    return {
        'total': total, 'today': between(today, today), 'week': week, 'week_trend': _trend(week, prev_week),
        'month': month, 'month_trend': _trend(month, prev_month), 'complete': complete,
        'complete_pct': _pct(complete, total), 'enrolled': enrolled, 'enrolled_pct': _pct(enrolled, total),
        'marketing': sum(1 for x in leads if x['marketing']), 'days': days,
        'in_range': sum(r['count'] for r in daily), 'peak': peak,
        'daily': daily, 'funnel': funnel, 'declined': status_counts.get('declined', 0),
        'status_bars': _bars(Counter({k: status_counts.get(k, 0) for k in STATUS_KEYS}), total,
                             labels=STATUS_LABELS, order=STATUS_KEYS),
        'sources': source_rows, 'channels': channels, 'qr': qr_rows,
        'specialties': _bars(spec_counter, with_specs, limit=12), 'with_specialties': with_specs,
        'cities': _bars(city_counter, total, limit=10, labels=city_label), 'cities_total': len(city_counter),
        'grades': _bars(grade_counter, total, labels=grade_labels, order=[k for k, _ in GRADES] + ['']),
        'fields': field_rates, 'weekdays': weekdays, 'referrers': top_referrers,
        'referred_total': sum(referrers.values()), 'coins': coins, 'recent': leads[:8],
    }


def _order_counts():
    """Заказы мерча по статусам (count(), кэш 5 мин) — для блока «Коины» аналитики."""
    def load():
        return {st: count(where(db.collection('coin_orders'), 'status', '==', st), default=None)
                for st in ('reserved', 'issued')}
    return cache.get_or_set('applicants:orders', load, SNAPSHOT_TTL)


@routes.route('/applicants')
@admin_required
def applicants_dashboard():
    days = _int(request.args.get('days'), 30)
    if days not in DASH_DAYS:
        days = 30
    try:
        snap = load_snapshot()
        stats = cache.get_or_set('applicants:analytics:%d:%s' % (days, today_almaty().isoformat()),
                                 lambda: compute_analytics(snap, days), SNAPSHOT_TTL)
        orders = _order_counts()
    except Exception as e:
        flash_error('Не удалось загрузить данные абитуриентов. Попробуйте обновить страницу', e)
        return render_template('applicants/dashboard.html', stats=None, days=days, day_options=DASH_DAYS,
                               orders={}, loaded_at=None, chart=None)
    chart = {'daily': [{'label': d['label'], 'date': d['date'], 'count': d['count'], 'avg': d['avg']}
                       for d in stats['daily']]}
    return render_template('applicants/dashboard.html', stats=stats, days=days, day_options=DASH_DAYS,
                           orders=orders, loaded_at=snap['loaded_at'], chart=chart)


@routes.route('/applicants/refresh', methods=['POST'])
@admin_required
def applicants_refresh():
    invalidate_applicants_cache()
    flash('Данные абитуриентов обновлены', 'success')
    return _back('applicants_dashboard')


# ==================== ЛИДЫ ====================

def _parse_date(v):
    try:
        return datetime.strptime(str(v or '').strip()[:10], '%Y-%m-%d').date()
    except ValueError:
        return None


def parse_filters(args):
    """Фильтры списка лидов из query-параметров (неизвестные значения отбрасываются)."""
    per = _int(args.get('per'), PER_PAGE_OPTIONS[0])
    grade = args.get('grade')
    return {
        'q': _str(args.get('q'), 100),
        'status': args.get('status') if args.get('status') in STATUS_KEYS else '',
        'source': _str(args.get('source'), 40),
        'specialty': _str(args.get('specialty'), 120),
        'city': _str(args.get('city'), 80),
        'grade': grade if grade in GRADE_LABELS or grade == 'none' else '',
        'period': args.get('period') if args.get('period') in dict(PERIODS) else '',
        'from': _parse_date(args.get('from')),
        'to': _parse_date(args.get('to')),
        'profile': args.get('profile') if args.get('profile') in ('complete', 'incomplete') else '',
        'kind': args.get('kind') if args.get('kind') in ('applicant', 'converted') else '',
        'step': args.get('step') if args.get('step') in dict(STEP_FILTERS) else '',
        'sort': args.get('sort') if args.get('sort') in dict(SORTS) else 'new',
        'per': per if per in PER_PAGE_OPTIONS else PER_PAGE_OPTIONS[0],
    }


# Фильтры, которые прячутся в «Ещё фильтры» (на телефоне — вся шторка)
MORE_FILTER_KEYS = ('specialty', 'city', 'grade', 'from', 'to', 'profile', 'kind')


def _date_range(f, today=None):
    today = today or today_almaty()
    lo, hi = f['from'], f['to']
    if f['period'] and not (lo or hi):
        lo = today if f['period'] == 'today' else today - timedelta(days=int(f['period']) - 1)
        hi = today
    return lo, hi


def filter_leads(leads, f, skip=(), today=None):
    """Отфильтровать снапшот по фильтрам f (кроме ключей из skip) и отсортировать."""
    words = _norm(f['q']).split()
    q_digits = re.sub(r'\D', '', f['q'])
    if len(q_digits) == 11 and q_digits.startswith('8'):
        q_digits = '7' + q_digits[1:]
    by_digits = len(q_digits) >= 3 and re.fullmatch(r'[\d\s()+\-]+', f['q'] or '') is not None
    lo, hi = _date_range(f, today)
    city = _norm(f['city'])
    out = []
    for x in leads:
        if words:
            if by_digits:
                if q_digits not in x['digits']:
                    continue
            elif not all(w in x['search'] for w in words):
                continue
        if f['status'] and 'status' not in skip and x['status'] != f['status']:
            continue
        if f['source'] and x['source'] != f['source']:
            continue
        if f['specialty'] and f['specialty'] not in x['specialties']:
            continue
        if city and _norm(x['city']) != city:
            continue
        if f['grade'] == 'none' and x['grade'] in GRADE_LABELS:
            continue
        if f['grade'] and f['grade'] != 'none' and x['grade'] != f['grade']:
            continue
        if lo or hi:
            d = _local_date(x['created'])
            if d is None or (lo and d < lo) or (hi and d > hi):
                continue
        if (f['profile'] == 'complete' and not x['complete']) or (f['profile'] == 'incomplete' and x['complete']):
            continue
        if (f['kind'] == 'converted' and not x['converted']) or (f['kind'] == 'applicant' and x['converted']):
            continue
        if f.get('step'):
            st = x['next_state']
            if f['step'] == 'planned':
                st = 'planned' if st in ('planned', 'today') else st
            if st != f['step'] or (f['step'] != 'none' and x['status'] in ('enrolled', 'declined')):
                continue
        out.append(x)
    sort = f['sort']
    if sort == 'old':
        out.sort(key=lambda x: (x['created'] or EPOCH, x['uid']))
    elif sort == 'name':
        out.sort(key=lambda x: (_norm(x['title']), x['uid']))
    elif sort == 'profile':
        out.sort(key=lambda x: (-x['filled'], -(x['created'] or EPOCH).timestamp()))
    elif sort == 'contact':
        # дольше всего без контакта (никогда — первыми); поступившие и отказы — в конце
        out.sort(key=lambda x: (x['status'] in ('enrolled', 'declined'), x['last_contact'] or EPOCH,
                                x['created'] or EPOCH))
    return out


def _filter_options(snap):
    leads = snap['leads']
    src = Counter(x['source'] for x in leads)
    titles = {x['source']: x['source_title'] for x in leads}
    for code, s in snap['sources'].items():
        src[code] += 0
        titles[code] = _str(s.get('title')) or code
    source_opts = [(code, '%s (%d)' % (titles.get(code) or 'Не указан', n))
                   for code, n in sorted(src.items(), key=lambda kv: (-kv[1], titles.get(kv[0]) or ''))]
    specs = Counter({t: 0 for t in catalog_titles()})
    for x in leads:
        for s in x['specialties']:
            specs[s] += 1
    spec_opts = [(s, '%s (%d)' % (s, n)) for s, n in sorted(specs.items(), key=lambda kv: (-kv[1], kv[0]))]
    cities, labels = Counter(), {}
    for x in leads:
        if x['city']:
            k = _norm(x['city'])
            cities[k] += 1
            labels.setdefault(k, x['city'])
    city_opts = [(labels[k], '%s (%d)' % (labels[k], n)) for k, n in cities.most_common(60)]
    return {'sources': source_opts, 'specialties': spec_opts, 'cities': city_opts,
            'grades': list(GRADES) + [('none', 'Не указан')]}


def leads_summary(leads, today=None):
    """Сводка над списком: сколько ждут звонка, просроченных шагов, новых за 7 дней."""
    today = today or today_almaty()
    week = today - timedelta(days=6)
    active = [x for x in leads if x['status'] not in ('enrolled', 'declined')]
    return {'total': len(leads), 'new': sum(1 for x in leads if x['status'] == 'new'),
            'overdue': sum(1 for x in active if x['next_state'] == 'overdue'),
            'today': sum(1 for x in active if x['next_state'] == 'today'),
            'week': sum(1 for x in leads if (_local_date(x['created']) or EPOCH.date()) >= week)}


@routes.route('/applicants/leads')
@admin_required
def applicants_leads():
    f = parse_filters(request.args)
    view = view_param('applicants_leads', VIEWS, 'table')
    ctx = dict(f=f, statuses=STATUSES, periods=PERIODS, sorts=SORTS, per_options=PER_PAGE_OPTIONS,
               contact_channels=CONTACT_CHANNELS, step_filters=STEP_FILTERS, view=view,
               more_n=sum(1 for k in MORE_FILTER_KEYS if f[k]), board=None, summary=None)
    try:
        snap = load_snapshot()
    except Exception as e:
        flash_error('Не удалось загрузить список абитуриентов. Попробуйте обновить страницу', e)
        return render_template('applicants/leads.html', page=None, opts=None, status_tabs=[], total=0,
                               loaded_at=None, has_filters=False, **ctx)
    base = filter_leads(snap['leads'], f, skip=('status',))
    status_counts = Counter(x['status'] for x in base)
    items = [x for x in base if not f['status'] or x['status'] == f['status']]
    page = paginate_list(items, request.args.get('page'), f['per'])
    tabs = [{'key': '', 'label': 'Все', 'count': len(base)}]
    tabs += [{'key': k, 'label': lbl, 'count': status_counts.get(k, 0)} for k, lbl in STATUSES]
    has_filters = any(f[k] for k in ('q', 'source', 'specialty', 'city', 'grade', 'period', 'from', 'to',
                                     'profile', 'kind', 'step'))
    if view == 'board':
        # канбан: все статусы колонками (вкладка статуса не применяется), в колонке — первые BOARD_LIMIT
        cols = {k: [] for k in STATUS_KEYS}
        for x in base:
            cols[x['status']].append(x)
        ctx['board'] = [{'key': k, 'label': lbl, 'count': len(cols[k]), 'items': cols[k][:BOARD_LIMIT],
                         'more': max(len(cols[k]) - BOARD_LIMIT, 0)} for k, lbl in STATUSES]
    ctx['summary'] = leads_summary(snap['leads'])
    return render_template('applicants/leads.html', page=page, opts=_filter_options(snap), status_tabs=tabs,
                           total=len(snap['leads']), loaded_at=snap['loaded_at'], has_filters=has_filters, **ctx)


_CSV_DANGER = ('=', '+', '-', '@', '\t', '\r')


def csv_cell(v):
    """Защита от формул в Excel (CSV injection): опасное начало ячейки экранируем апострофом."""
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'да' if v else 'нет'
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    return "'" + s if s.startswith(_CSV_DANGER) else s


LEADS_CSV_HEADER = ['UID', 'Логин', 'ФИО', 'Телефон', 'Email', 'Город', 'Школа', 'Класс', 'Специальности',
                    'Код источника', 'Источник', 'Статус', 'Заметки', 'Профиль заполнен', 'Полей профиля',
                    'Согласие на звонки', 'Коины (баланс)', 'Пригласил (UID)', 'Приглашено друзей',
                    'Студент (группа)', 'Дата регистрации', 'Последний контакт', 'Следующий шаг',
                    'Дата шага']


def _leads_csv(leads, what='filter'):
    rows = []
    for x in leads:
        rows.append([csv_cell(v) for v in [
            x['uid'], x['login'], x['name'], x['phone_fmt'] or x['phone'], '' if x['synthetic'] else x['email'],
            x['city'], x['school'], x['grade_label'], ', '.join(x['specialties']), x['source'],
            x['source_title'], STATUS_LABELS.get(x['status'], x['status']), x['notes'], x['complete'],
            '%d из 6' % x['filled'], x['marketing'], x['balance'], x['referred_by'], x['referrals'],
            x['group'] if x['converted'] else '', fmt_dt(x['created'], default=''),
            fmt_dt(x['last_contact'], default=''), x['next_step'],
            x['next_date'].strftime('%d.%m.%Y') if x['next_date'] else '']])
    audit('export', 'applicants', None, {'count': len(rows), 'what': what})
    stamp = to_almaty(now_utc()).strftime('%Y-%m-%d')
    return csv_response('абитуриенты_%s.csv' % stamp, LEADS_CSV_HEADER, rows)


@routes.route('/applicants/leads.csv')
@admin_required
def applicants_leads_export():
    f = parse_filters(request.args)
    try:
        leads = filter_leads(load_snapshot()['leads'], f)
    except Exception as e:
        flash_error('Не удалось выгрузить абитуриентов', e)
        return redirect(url_for('applicants_leads'))
    return _leads_csv(leads)


@routes.route('/applicants/leads/bulk', methods=['POST'])
@admin_required
def applicants_bulk():
    action = request.form.get('action')
    # чекбоксы панели выбора (ui.select_row) — name="ids"; старые формы — uids
    uids = [u for u in dict.fromkeys(request.form.getlist('uids') + request.form.getlist('ids')) if u]
    if not uids:
        flash('Отметьте хотя бы одного абитуриента', 'warning')
        return _back('applicants_leads')
    if len(uids) > BULK_LIMIT:
        flash('За раз можно обработать не больше %d абитуриентов' % BULK_LIMIT, 'error')
        return _back('applicants_leads')
    try:
        known = load_snapshot(force=True)['by_uid']
    except Exception as e:
        flash_error('Не удалось проверить список абитуриентов', e)
        return _back('applicants_leads')
    uids = [u for u in uids if u in known]
    if not uids:
        flash('Отмеченные абитуриенты не найдены — обновите страницу', 'error')
        return _back('applicants_leads')

    adm = _admin()
    if action == 'export':
        return _leads_csv([known[u] for u in uids], what='selected')
    if action == 'status':
        status = request.form.get('status')
        if status not in STATUS_KEYS:
            flash('Выберите новый статус', 'error')
            return _back('applicants_leads')
        changed = [u for u in uids if known[u]['status'] != status]
        try:
            for part in chunked(changed, 400):
                batch = db.batch()
                for uid in part:
                    old = known[uid]['status']
                    data = {'status': status, 'updatedAt': SERVER_TIMESTAMP, 'updatedBy': adm.get('uid'),
                            'history': ArrayUnion([_history_entry('status', '%s → %s' % (
                                STATUS_LABELS[old], STATUS_LABELS[status]))])}
                    if old == 'new' and not known[uid]['last_contact']:
                        data['lastContactAt'] = SERVER_TIMESTAMP
                    batch.set(db.collection('applicant_crm').document(uid), data, merge=True)
                batch.commit()
        except Exception as e:
            flash_error('Не удалось сменить статус. Часть изменений могла не сохраниться', e)
            invalidate_applicants_cache()
            return _back('applicants_leads')
        audit('bulk_status', 'applicants', None, {'status': status, 'count': len(changed), 'uids': changed[:50]})
        invalidate_applicants_cache()
        flash('Статус «%s» установлен: %d' % (STATUS_LABELS[status], len(changed)), 'success')
        return _back('applicants_leads')

    if action == 'coins':
        amount = _int(request.form.get('amount'), 0)
        reason = _field('reason', 200)
        if amount <= 0 or amount > MAX_COINS:
            flash('Укажите, сколько коинов начислить (от 1 до %d)' % MAX_COINS, 'error')
            return _back('applicants_leads')
        if not reason:
            flash('Укажите причину начисления — её увидит абитуриент', 'error')
            return _back('applicants_leads')
        try:
            for part in chunked(uids, 200):   # 2 записи на человека — лимит батча 500
                batch = db.batch()
                for uid in part:
                    batch.set(db.collection('coin_transactions').document(), {
                        'uid': uid, 'amount': amount, 'type': 'manual', 'reason': reason,
                        'createdBy': adm.get('uid'), 'createdAt': SERVER_TIMESTAMP})
                    batch.set(db.collection('applicant_crm').document(uid), {
                        'history': ArrayUnion([_history_entry('coins', '+%d: %s' % (amount, reason))])}, merge=True)
                batch.commit()
        except Exception as e:
            flash_error('Не удалось начислить коины. Часть начислений могла пройти — проверьте журнал', e)
            invalidate_applicants_cache()
            return _back('applicants_leads')
        audit('bulk_award', 'applicants', None, {'amount': amount, 'reason': reason, 'count': len(uids),
                                                 'uids': uids[:50]})
        invalidate_applicants_cache()
        flash('Начислено по %d коинов: %d абитуриентам. Балансы обновятся через несколько секунд'
              % (amount, len(uids)), 'success')
        return _back('applicants_leads')

    flash('Выберите действие', 'error')
    return _back('applicants_leads')


# ==================== КАРТОЧКА АБИТУРИЕНТА ====================

def _get_user(uid):
    snap = db.collection('users').document(uid).get()
    return (snap.to_dict() or {}) if snap.exists else None


def _require_applicant(uid, allow_converted=False):
    """users/{uid} абитуриента (или зачисленного — если allow_converted) или None + flash."""
    try:
        u = _get_user(uid)
    except Exception as e:
        flash_error('Не удалось прочитать профиль. Попробуйте ещё раз', e)
        return None
    if u is None:
        flash('Абитуриент не найден — возможно, аккаунт удалён', 'error')
        return None
    if u.get('role') == 'applicant' or (allow_converted and u.get('role') == 'student'):
        return u
    flash('Это действие доступно только для абитуриента', 'error')
    return None


def _query_list(query_factory, fallback_factory, limit):
    """Запрос с orderBy (составной индекс есть в firestore.indexes.json); без индекса — без сортировки."""
    try:
        return [dict(s.to_dict() or {}, id=s.id) for s in query_factory().limit(limit).stream()]
    except Exception:
        log.warning('Запрос с сортировкой не выполнен — читаем без неё', exc_info=True)
        items = [dict(s.to_dict() or {}, id=s.id) for s in fallback_factory().limit(limit).stream()]
        items.sort(key=lambda d: to_datetime(d.get('createdAt')) or EPOCH, reverse=True)
        return items


def _auth_info(uid):
    try:
        rec = fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        return {'exists': False}
    except Exception:
        log.warning('Auth-аккаунт %s не прочитан', uid, exc_info=True)
        return {'exists': None}
    meta = rec.user_metadata
    return {'exists': True, 'disabled': bool(rec.disabled), 'email': rec.email or '',
            'last_sign_in': to_datetime(getattr(meta, 'last_sign_in_timestamp', None)),
            'created': to_datetime(getattr(meta, 'creation_timestamp', None))}


def _doc_or(coll, uid, default):
    snap = db.collection(coll).document(uid).get()
    return (snap.to_dict() or {}) if snap.exists else default


def build_feed(history, txs, orders, created=None):
    """Лента активности карточки (новые сверху): CRM-история + автоматические начисления (квесты, коды,
    рефералы, приветственные) + заказы. Ручные операции и покупки не дублируются — они уже есть в истории
    (запись «Коины») и в заказах. → [{at, kind, icon, title, details, by, tone}]"""
    feed = []
    has_created = False
    for h in history or []:
        if not isinstance(h, dict):
            continue
        action = _str(h.get('action'))
        label, icon = HISTORY_LABELS.get(action, (action or 'Событие', 'dot'))
        has_created = has_created or action == 'created'
        by = _str(h.get('by'))
        feed.append({'at': to_datetime(h.get('at')), 'kind': HISTORY_KINDS.get(action, 'work'), 'icon': icon,
                     'title': label, 'details': _str(h.get('details')), 'by': '' if by == 'system' else by,
                     'tone': ''})
    for t in txs or []:
        typ = _str(t.get('type')) or 'manual'
        if typ in ('manual', 'purchase', 'refund'):
            continue
        label, icon = TX_LABELS.get(typ, ('Коины', 'coin'))
        amount = t.get('amount') if isinstance(t.get('amount'), int) and not isinstance(t.get('amount'), bool) else 0
        err = t.get('applied') is False or bool(t.get('error'))
        feed.append({'at': to_datetime(t.get('createdAt')), 'kind': 'coins', 'icon': icon,
                     'title': '%s %+d' % (label, amount), 'details': _str(t.get('reason')) + (
                         ' — ошибка: %s' % _str(t.get('error')) if err else ''),
                     'by': '', 'tone': 'danger' if err else ''})
    for o in orders or []:
        st = _str(o.get('status')) or 'pending'
        price = o.get('price') if isinstance(o.get('price'), int) and not isinstance(o.get('price'), bool) else 0
        title = _str(o.get('title')) or _str(o.get('productId')) or 'Товар'
        variant = _str(o.get('variant'))
        feed.append({'at': to_datetime(o.get('createdAt')), 'kind': 'orders', 'icon': 'bag',
                     'title': 'Заказ: %s%s' % (title, ' (%s)' % variant if variant else ''),
                     'details': '%d %s · %s' % (price, 'коинов', ORDER_LABELS.get(st, st)), 'by': '',
                     'tone': 'warning' if st == 'reserved' else ''})
    if created and not has_created:
        feed.append({'at': created, 'kind': 'account', 'icon': 'person-plus', 'title': 'Регистрация',
                     'details': '', 'by': '', 'tone': ''})
    feed.sort(key=lambda e: e['at'] or EPOCH, reverse=True)
    return feed


def lead_neighbors(uid):
    """«‹ Предыдущий / Следующий ›» по фильтру списка, из которого открыли карточку (?next=/applicants/leads?…).
    → {'prev', 'next', 'pos', 'total', 'list_url'} или None."""
    nxt = next_target()
    if not nxt:
        return None
    parts = urlsplit(nxt)
    if parts.path != url_for('applicants_leads'):
        return None
    args = MultiDict([(k, v) for k, vals in parse_qs(parts.query).items() for v in vals])
    try:
        leads = filter_leads(load_snapshot()['leads'], parse_filters(args))
    except Exception:
        log.warning('Соседи лида %s не посчитаны', uid, exc_info=True)
        return None
    ids = [x['uid'] for x in leads]
    if uid not in ids:
        return None
    i = ids.index(uid)

    def link(j):
        return url_for('applicant_detail', uid=ids[j], next=nxt) if 0 <= j < len(ids) else None

    return {'prev': link(i - 1), 'next': link(i + 1), 'pos': i + 1, 'total': len(ids), 'list_url': nxt}


@routes.route('/applicants/<uid>')
@admin_required
def applicant_detail(uid):
    uid = _str(uid, 128)
    users = db.collection('users')
    tx_coll = db.collection('coin_transactions')
    orders_coll = db.collection('coin_orders')
    invited_fields = ['name', 'login', 'email', 'createdAt', 'role', 'phone', 'city', 'school', 'grade',
                      'specialties']
    tasks = {
        'user': lambda: _get_user(uid),
        'crm': lambda: _doc_or('applicant_crm', uid, None),
        'wallet': lambda: _doc_or('coin_wallets', uid, {}),
        'txs': lambda: _query_list(lambda: where(tx_coll, 'uid', '==', uid).order_by('createdAt', direction=DESC),
                                   lambda: where(tx_coll, 'uid', '==', uid), 100),
        'orders': lambda: _query_list(
            lambda: where(orders_coll, 'uid', '==', uid).order_by('createdAt', direction=DESC),
            lambda: where(orders_coll, 'uid', '==', uid), 50),
        'invited': lambda: [dict(s.to_dict() or {}, id=s.id) for s in
                            where(users, 'referredBy', '==', uid).select(invited_fields).limit(100).stream()],
        'auth': lambda: _auth_info(uid),
    }
    try:
        with ThreadPoolExecutor(max_workers=len(tasks)) as pool:
            futures = {k: pool.submit(fn) for k, fn in tasks.items()}
            data = {k: fut.result() for k, fut in futures.items()}
    except Exception as e:
        flash_error('Не удалось загрузить карточку абитуриента. Попробуйте ещё раз', e)
        return redirect(url_for('applicants_leads'))

    u, crm = data['user'], data['crm']
    if u is None or (u.get('role') != 'applicant' and not (u.get('role') == 'student' and crm is not None)):
        abort(404)
    crm = crm or {}
    converted = u.get('role') == 'student'
    filled, complete = profile_filled(u)
    adm_cfg = load_admissions()
    src = source_info(u.get('source') or crm.get('source'))
    referrer = None
    ref_uid = _str(u.get('referredBy'))
    if ref_uid:
        try:
            r = _get_user(ref_uid)
            if r:
                referrer = {'uid': ref_uid, 'title': _str(r.get('name')) or display_login_of(r) or ref_uid,
                            'login': display_login_of(r)}
        except Exception:
            log.warning('Пригласивший %s не прочитан', ref_uid, exc_info=True)
    invited = []
    for d in data['invited']:
        _, full = profile_filled(d)
        invited.append({'uid': d['id'], 'title': _str(d.get('name')) or display_login_of(d) or d['id'],
                        'login': display_login_of(d), 'created': to_datetime(d.get('createdAt')),
                        'complete': full, 'role': d.get('role')})
    invited.sort(key=lambda i: i['created'] or EPOCH, reverse=True)
    tx_ids = {t['id'] for t in data['txs']}
    feed = build_feed(crm.get('history'), data['txs'], data['orders'],
                      to_datetime(u.get('createdAt')) or to_datetime(crm.get('createdAt')))
    email = _str(u.get('email'))
    phone = _str(u.get('phone')) or (_str(u.get('login')) if _str(u.get('login')).startswith('+') else '')
    specs = [_str(s) for s in u.get('specialties') if _str(s)] if isinstance(u.get('specialties'), list) else []
    spec_options = catalog_titles(adm_cfg)
    spec_options += [s for s in specs if s not in spec_options]
    groups = []
    if not converted:
        try:
            from .schedule_model import get_all_groups
            groups = get_all_groups()
        except Exception:
            log.warning('Список групп не загружен', exc_info=True)
    name = _str(u.get('name'))
    login = display_login_of(u)
    lead = {
        'uid': uid, 'name': name, 'title': name if len(name) >= 2 else (login or uid), 'login': login,
        'email': email, 'synthetic': is_synthetic_email(email), 'phone': phone,
        'phone_fmt': fmt_phone(phone) if phone else '', 'wa': wa_link(phone),
        'tel': normalize_phone(phone) if phone else None,
        'city': _str(u.get('city')), 'school': _str(u.get('school')), 'grade': _str(u.get('grade')),
        'grade_label': GRADE_LABELS.get(_str(u.get('grade')), _str(u.get('grade'))), 'specialties': specs,
        'marketing': u.get('marketingConsent') is True, 'created': to_datetime(u.get('createdAt')),
        'updated': to_datetime(u.get('updatedAt')), 'ref_code': _str(u.get('refCode')),
        'rewarded': _int(u.get('referralsRewarded')), 'must_change': u.get('mustChangePassword') is True,
        'reset_at': to_datetime(u.get('passwordResetAt')), 'changed_at': to_datetime(u.get('passwordChangedAt')),
        'consents': u.get('consents') if isinstance(u.get('consents'), dict) else {},
        'filled': filled, 'complete': complete, 'converted': converted, 'group': _str(u.get('group')),
        'course': _str(u.get('course')), 'status': crm.get('status') if crm.get('status') in STATUS_KEYS else 'new',
        'notes': _str(crm.get('notes')), 'last_contact': to_datetime(crm.get('lastContactAt')),
        'crm_updated': to_datetime(crm.get('updatedAt')), 'step': next_step_info(crm),
    }
    wallet = {'balance': _int(data['wallet'].get('balance')), 'earned': _int(data['wallet'].get('earnedTotal')),
              'spent': _int(data['wallet'].get('spentTotal'))}
    register_url = _str(adm_cfg.get('registerUrl'))
    if not register_url.lower().startswith(('http://', 'https://')):   # контракт приложения: только http(s)
        register_url = join_base_url()
    reserved = sum(1 for o in data['orders'] if o.get('status') == 'reserved')
    # формы карточки возвращают на карточку (с её ?next — ссылка «‹ Лиды» помнит фильтры), в быстром просмотре —
    # на список, из которого его открыли
    form_next = (next_target() or url_for('applicants_leads')) if is_partial() else here()
    return render_template(
        'applicants/detail.html', lead=lead, src=src, referrer=referrer, invited=invited, tx_ids=tx_ids,
        feed=feed, feed_kinds=FEED_KINDS, wallet=wallet, txs=data['txs'], orders=data['orders'], auth=data['auth'],
        statuses=STATUSES, grades=GRADES, spec_options=spec_options, groups=groups,
        contact_channels=CONTACT_CHANNELS, reset_password=RESET_PASSWORD, profile_fields=PROFILE_FIELDS,
        reserved=reserved, nav=None if is_partial() else lead_neighbors(uid), form_next=form_next,
        ref_link=(register_url + ('&' if '?' in register_url else '?') + 'ref=' + lead['ref_code'])
        if lead['ref_code'] else None)


def _crm_ref(uid):
    return db.collection('applicant_crm').document(uid)


@routes.route('/applicants/<uid>/status', methods=['POST'])
@admin_required
def applicant_set_status(uid):
    """Смена статуса лида: из карточки, из ячейки таблицы, из быстрого просмотра и перетаскиванием в канбане
    (fetch с Accept: application/json → JSON). Без подтверждения — тост «Отменить» (UX_SPEC_v2 §4.11)."""
    as_json = wants_json()
    status = request.form.get('status')
    if as_json and status is None:
        status = (request.get_json(silent=True) or {}).get('status')

    def fail(msg, code=400):
        if as_json:
            return jsonify({'ok': False, 'error': msg}), code
        flash(msg, 'error')
        return _back('applicant_detail', uid=uid)

    if status not in STATUS_KEYS:
        return fail('Выберите статус из списка')
    if _require_applicant(uid, allow_converted=True) is None:
        if as_json:
            return jsonify({'ok': False, 'error': 'Абитуриент не найден — обновите страницу'}), 404
        return redirect(url_for('applicants_leads'))
    try:
        snap = _crm_ref(uid).get()
        old = (snap.to_dict() or {}).get('status') if snap.exists else None
        old = old if old in STATUS_KEYS else 'new'
        if old == status:
            if as_json:
                return jsonify({'ok': True, 'changed': False, 'status': status, 'label': STATUS_LABELS[status]})
            flash('Статус уже «%s»' % STATUS_LABELS[status], 'info')
            return _back('applicant_detail', uid=uid)
        data = {'status': status, 'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin().get('uid'),
                'history': ArrayUnion([_history_entry('status', '%s → %s' % (STATUS_LABELS[old],
                                                                             STATUS_LABELS[status]))])}
        if old == 'new' and not (snap.exists and (snap.to_dict() or {}).get('lastContactAt')):
            data['lastContactAt'] = SERVER_TIMESTAMP
        _crm_ref(uid).set(data, merge=True)
    except Exception as e:
        log.warning('Статус %s не сменён', uid, exc_info=True)
        if as_json:
            return jsonify({'ok': False, 'error': 'Не удалось сменить статус. Попробуйте ещё раз'}), 500
        flash_error('Не удалось сменить статус. Попробуйте ещё раз', e)
        return _back('applicant_detail', uid=uid)
    audit('status', 'applicant', uid, {'from': old, 'to': status})
    invalidate_applicants_cache()
    undo = {'post': url_for('applicant_set_status', uid=uid), 'fields': {'status': old}}
    if as_json:
        return jsonify({'ok': True, 'changed': True, 'status': status, 'label': STATUS_LABELS[status],
                        'old': old, 'message': 'Статус: %s' % STATUS_LABELS[status], 'undo': undo})
    target = back_url('applicant_detail', uid=uid)
    flash_action('Статус: %s' % STATUS_LABELS[status], 'Отменить', post=undo['post'],
                 fields=dict(undo['fields'], next=target))
    return redirect(target)


@routes.route('/applicants/<uid>/notes', methods=['POST'])
@admin_required
def applicant_save_notes(uid):
    notes = _field('notes', 2000)
    if _require_applicant(uid, allow_converted=True) is None:
        return redirect(url_for('applicants_leads'))
    try:
        _crm_ref(uid).set({'notes': notes, 'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin().get('uid'),
                           'history': ArrayUnion([_history_entry('notes', 'Заметка изменена' if notes
                                                                 else 'Заметка очищена')])}, merge=True)
    except Exception as e:
        flash_error('Не удалось сохранить заметку', e)
        return _back('applicant_detail', uid=uid)
    audit('notes', 'applicant', uid, {'length': len(notes)})
    invalidate_applicants_cache()
    flash('Заметка сохранена', 'success')
    return _back('applicant_detail', uid=uid)


@routes.route('/applicants/<uid>/next-step', methods=['POST'])
@admin_required
def applicant_next_step(uid):
    """Следующий шаг лида: action=save (текст ≤ 200 и/или дата), done (выполнен — в историю), clear (снять)."""
    action = request.form.get('action') or 'save'
    if action not in ('save', 'done', 'clear'):
        action = 'save'
    if _require_applicant(uid, allow_converted=True) is None:
        return redirect(url_for('applicants_leads'))
    text = _field('next_step', NEXT_STEP_MAX)
    raw = _field('next_step_at')
    day = _parse_date(raw) if raw else None
    if action == 'save':
        if raw and day is None:
            flash('Проверьте дату следующего шага', 'error')
            return _back('applicant_detail', uid=uid)
        if not text and not day:
            flash('Опишите следующий шаг или укажите дату', 'error')
            return _back('applicant_detail', uid=uid)
    try:
        snap = _crm_ref(uid).get()
        old = next_step_info(snap.to_dict() if snap.exists else {})
        if action == 'save':
            at = datetime(day.year, day.month, day.day, tzinfo=ALMATY) if day else None
            details = (text or 'без описания') + (' · до %s' % day.strftime('%d.%m.%Y') if day else '')
            data = {'nextStep': text or DELETE_FIELD, 'nextStepAt': at or DELETE_FIELD,
                    'history': ArrayUnion([_history_entry('next_step', 'Назначен: ' + details)])}
        else:
            if old['state'] == 'none':
                flash('Следующий шаг не назначен', 'info')
                return _back('applicant_detail', uid=uid)
            details = old['text'] or 'шаг на %s' % old['date'].strftime('%d.%m.%Y')
            data = {'nextStep': DELETE_FIELD, 'nextStepAt': DELETE_FIELD,
                    'history': ArrayUnion([_history_entry('next_step', ('Выполнен: ' if action == 'done'
                                                                         else 'Снят: ') + details)])}
            if action == 'done':
                data['lastContactAt'] = SERVER_TIMESTAMP
        data.update({'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin().get('uid')})
        _crm_ref(uid).set(data, merge=True)
    except Exception as e:
        flash_error('Не удалось сохранить следующий шаг', e)
        return _back('applicant_detail', uid=uid)
    audit('next_step', 'applicant', uid, {'action': action, 'text': text or old['text'] or None})
    invalidate_applicants_cache()
    target = back_url('applicant_detail', uid=uid)
    if action == 'save':
        flash('Следующий шаг сохранён', 'success')
    else:
        flash_action('Шаг выполнен' if action == 'done' else 'Шаг снят', 'Вернуть',
                     post=url_for('applicant_next_step', uid=uid),
                     fields={'action': 'save', 'next_step': old['text'],
                             'next_step_at': old['date'].isoformat() if old['date'] else '', 'next': target})
    return redirect(target)


@routes.route('/applicants/<uid>/contact', methods=['POST'])
@admin_required
def applicant_log_contact(uid):
    channel = request.form.get('channel')
    labels = dict(CONTACT_CHANNELS)
    if channel not in labels:
        flash('Выберите способ связи', 'error')
        return _back('applicant_detail', uid=uid)
    comment = _field('comment', 300)
    if _require_applicant(uid, allow_converted=True) is None:
        return redirect(url_for('applicants_leads'))
    try:
        snap = _crm_ref(uid).get()
        old = (snap.to_dict() or {}).get('status') if snap.exists else None
        first = old in (None, '', 'new')
        data = {'lastContactAt': SERVER_TIMESTAMP, 'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin().get('uid')}
        entries = [_history_entry('contact', labels[channel] + (': ' + comment if comment else ''))]
        if first:
            # первый контакт — лид сам переходит в «Связались»
            data['status'] = 'contacted'
            entries.append(_history_entry('status', '%s → %s' % (STATUS_LABELS['new'], STATUS_LABELS['contacted'])))
        data['history'] = ArrayUnion(entries)
        _crm_ref(uid).set(data, merge=True)
    except Exception as e:
        flash_error('Не удалось записать контакт', e)
        return _back('applicant_detail', uid=uid)
    audit('contact', 'applicant', uid, {'channel': channel})
    invalidate_applicants_cache()
    flash('Контакт записан%s' % (' — статус «Связались»' if first else ''), 'success')
    return _back('applicant_detail', uid=uid)


@routes.route('/applicants/<uid>/coins', methods=['POST'])
@admin_required
def applicant_award_coins(uid):
    amount = _int(_field('amount', 12), 0)
    reason = _field('reason', 200)
    if request.form.get('direction') == 'minus' and amount > 0:
        amount = -amount
    if amount == 0 or abs(amount) > MAX_COINS:
        flash('Укажите количество коинов: от 1 до %d' % MAX_COINS, 'error')
        return _back('applicant_detail', uid=uid)
    if not reason:
        flash('Укажите причину — её увидит абитуриент в журнале', 'error')
        return _back('applicant_detail', uid=uid)
    if _require_applicant(uid, allow_converted=True) is None:
        return redirect(url_for('applicants_leads'))
    try:
        if amount < 0:
            balance = _int(_doc_or('coin_wallets', uid, {}).get('balance'))
            if balance + amount < 0:
                flash('Нельзя списать больше, чем на балансе (%d)' % balance, 'error')
                return _back('applicant_detail', uid=uid)
        _, ref = db.collection('coin_transactions').add({
            'uid': uid, 'amount': amount, 'type': 'manual', 'reason': reason,
            'createdBy': _admin().get('uid'), 'createdAt': SERVER_TIMESTAMP})
        _crm_ref(uid).set({'history': ArrayUnion([_history_entry('coins', '%+d: %s' % (amount, reason))])},
                          merge=True)
    except Exception as e:
        flash_error('Не удалось провести операцию с коинами', e)
        return _back('applicant_detail', uid=uid)
    audit('award' if amount > 0 else 'deduct', 'applicant', uid, {'amount': amount, 'reason': reason, 'tx': ref.id})
    invalidate_applicants_cache()
    flash('%s %d коинов. Баланс обновится через несколько секунд'
          % ('Начислено' if amount > 0 else 'Списано', abs(amount)), 'success')
    return _back('applicant_detail', uid=uid)


@routes.route('/applicants/<uid>/reset-password', methods=['POST'])
@admin_required
def applicant_reset_password(uid):
    """SPEC D5: пароль → abiturient123, mustChangePassword = true, отзыв сессий, аудит и CRM-история."""
    u = _require_applicant(uid)
    if u is None:
        return _back('applicant_detail', uid=uid)
    try:
        fb_auth.update_user(uid, password=RESET_PASSWORD)
    except fb_auth.UserNotFoundError:
        flash('У абитуриента нет аккаунта входа — сбрасывать нечего', 'error')
        return _back('applicant_detail', uid=uid)
    except Exception as e:
        flash_error('Не удалось сбросить пароль. Попробуйте ещё раз', e)
        return _back('applicant_detail', uid=uid)
    try:
        db.collection('users').document(uid).update({'mustChangePassword': True, 'passwordResetAt': SERVER_TIMESTAMP,
                                                     'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        # пароль уже сменён — честно говорим, что приложение не попросит придумать новый
        flash_error('Пароль сброшен, но отметка «сменить пароль» не записалась. Повторите сброс', e)
        return _back('applicant_detail', uid=uid)
    try:
        fb_auth.revoke_refresh_tokens(uid)
    except Exception:
        log.warning('Не удалось отозвать сессии %s', uid, exc_info=True)
    try:
        _crm_ref(uid).set({'history': ArrayUnion([_history_entry('password_reset', 'Пароль сброшен до стандартного')]),
                           'updatedAt': SERVER_TIMESTAMP}, merge=True)
    except Exception:
        log.warning('История CRM %s не дополнена', uid, exc_info=True)
    audit('reset_password', 'applicant', uid, {'login': display_login_of(u)})
    invalidate_applicants_cache()
    flash('Пароль сброшен до %s. При входе приложение попросит придумать новый' % RESET_PASSWORD, 'success')
    return _back('applicant_detail', uid=uid)


@routes.route('/applicants/<uid>/convert', methods=['POST'])
@admin_required
def applicant_convert(uid):
    """Зачислить: как AdminApplicantsRepository.convertToStudent (uid и история коинов сохраняются)."""
    group = _field('group', 60)
    course = _field('course', 2)
    if not group:
        flash('Укажите группу студента', 'error')
        return _back('applicant_detail', uid=uid)
    if course not in ('1', '2', '3', '4'):
        flash('Выберите курс от 1 до 4', 'error')
        return _back('applicant_detail', uid=uid)
    if _require_applicant(uid) is None:
        return _back('applicant_detail', uid=uid)
    try:
        batch = db.batch()
        batch.update(db.collection('users').document(uid), {
            'role': 'student', 'group': group, 'course': course, 'attendance': '0%', 'grades': 'N/A',
            'updatedAt': SERVER_TIMESTAMP})
        batch.set(_crm_ref(uid), {
            'status': 'enrolled', 'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin().get('uid'),
            'history': ArrayUnion([_history_entry('converted', 'Группа %s, %s курс' % (group, course))])},
            merge=True)
        batch.commit()
    except Exception as e:
        flash_error('Не удалось перевести в студенты', e)
        return _back('applicant_detail', uid=uid)
    audit('convert_to_student', 'applicant', uid, {'group': group, 'course': course})
    invalidate_applicants_cache()
    flash('Абитуриент зачислен: группа %s, %s курс. Вход — по тому же логину и паролю' % (group, course), 'success')
    return _back('applicant_detail', uid=uid)


def _same(old, new):
    if isinstance(new, bool):
        return (old is True) == new
    if isinstance(new, list):
        return [_str(v) for v in (old or [])] == new if isinstance(old, list) else not new
    return _str(old) == new


@routes.route('/applicants/<uid>/profile', methods=['POST'])
@admin_required
def applicant_save_profile(uid):
    """Анкета со слов абитуриента (например, по телефону). Ограничения — как в firestore.rules."""
    u = _require_applicant(uid)
    if u is None:
        return _back('applicant_detail', uid=uid)
    errors = []
    name = _field('name')
    if len(name) > 120 or len(name) == 1:
        errors.append('ФИО — от 2 до 120 символов')
    phone_raw = _field('phone')
    phone = normalize_phone(phone_raw) or '' if phone_raw else ''
    if phone_raw and not phone:
        errors.append('Телефон — казахстанский номер вида +7 7XX XXX XX XX')
    city, school = _field('city'), _field('school')
    if len(city) > 80:
        errors.append('Город — не длиннее 80 символов')
    if len(school) > 120:
        errors.append('Школа — не длиннее 120 символов')
    grade = request.form.get('grade') or ''
    if grade and grade not in GRADE_LABELS:
        errors.append('Выберите класс из списка')
    specs = []
    for s in request.form.getlist('specialties'):
        s = _str(s, 120)
        if s and s not in specs:
            specs.append(s)
    if len(specs) > 10:
        errors.append('Не больше 10 специальностей')
    if errors:
        for err in errors:
            flash(err, 'error')
        return _back('applicant_detail', uid=uid)
    data = {'name': name, 'phone': phone, 'city': city, 'school': school, 'grade': grade, 'specialties': specs,
            'marketingConsent': request.form.get('marketingConsent') == '1'}
    changed = [k for k, v in data.items() if not _same(u.get(k), v)]
    if not changed:
        flash('Изменений нет', 'info')
        return _back('applicant_detail', uid=uid)
    labels = dict(PROFILE_FIELDS + [('marketingConsent', 'согласие на звонки')])
    try:
        db.collection('users').document(uid).update(dict(data, updatedAt=SERVER_TIMESTAMP))
        _crm_ref(uid).set({'history': ArrayUnion([_history_entry(
            'profile', 'Изменено: ' + ', '.join(labels.get(k, k) for k in changed))])}, merge=True)
    except Exception as e:
        flash_error('Не удалось сохранить профиль', e)
        return _back('applicant_detail', uid=uid)
    audit('update_profile', 'applicant', uid, {'fields': changed})
    invalidate_applicants_cache()
    flash('Профиль сохранён', 'success')
    return _back('applicant_detail', uid=uid)


@routes.route('/applicants/<uid>/delete', methods=['POST'])
@admin_required
def applicant_delete_account(uid):
    """Удалить аккаунт: Auth + users/{uid}. Кошелёк, журнал, заказы, CRM чистит onApplicantDeleted."""
    u = _require_applicant(uid)
    if u is None:
        return _back('applicant_detail', uid=uid)
    if _str(request.form.get('confirm')).lower() != 'удалить':
        flash('Чтобы удалить аккаунт, введите слово «удалить»', 'error')
        return _back('applicant_detail', uid=uid)
    try:
        fb_auth.delete_user(uid)
    except fb_auth.UserNotFoundError:
        pass
    except Exception as e:
        flash_error('Не удалось удалить аккаунт входа. Профиль не тронут', e)
        return _back('applicant_detail', uid=uid)
    try:
        db.collection('users').document(uid).delete()
    except Exception as e:
        flash_error('Аккаунт входа удалён, но профиль не удалился. Повторите удаление', e)
        return _back('applicant_detail', uid=uid)
    audit('delete', 'applicant', uid, {'login': display_login_of(u), 'name': _str(u.get('name'))})
    invalidate_applicants_cache()
    flash('Аккаунт %s удалён. Связанные данные (коины, заказы, CRM) очистятся автоматически'
          % (display_login_of(u) or uid), 'success')
    return redirect(url_for('applicants_leads'))


# ==================== QR-ИСТОЧНИКИ ====================

def join_base_url():
    """Адрес публичной регистрации без кода: <url_root>join. В проде — всегда https."""
    root = request.url_root
    if not IS_EMULATOR and root.startswith('http://'):
        host = request.host.split(':')[0]
        if host not in ('localhost', '127.0.0.1'):
            root = 'https://' + root[len('http://'):]
    return root.rstrip('/') + '/join'


def suggest_code(existing=()):
    """Свободный код источника из 6 символов без похожих букв/цифр."""
    for _ in range(20):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if code not in existing and code not in RESERVED_SOURCES:
            return code
    return 'QR' + str(int(time.time()))[-6:]


def _source_form():
    """Поля формы источника → (data, errors)."""
    errors = {}
    title = _field('title')
    if not title:
        errors['title'] = 'Укажите название — например, «Стенд в ТРЦ»'
    elif len(title) > 120:
        errors['title'] = 'Название — не длиннее 120 символов'
    channel = request.form.get('channel') or 'other'
    if channel not in CHANNEL_LABELS:
        errors['channel'] = 'Выберите канал из списка'
        channel = 'other'
    note = _field('note')
    if len(note) > 500:
        errors['note'] = 'Заметка — не длиннее 500 символов'
    raw = _field('welcomeCoins') or '0'
    welcome = _int(raw, -1) if re.fullmatch(r'\d{1,4}', raw) else -1
    if not 0 <= welcome <= MAX_WELCOME:
        errors['welcomeCoins'] = 'Приветственные коины — целое число от 0 до %d' % MAX_WELCOME
    data = {'title': title[:120], 'channel': channel, 'note': note[:500], 'welcomeCoins': max(0, welcome),
            'active': request.form.get('active') == '1'}
    return data, errors


def source_rows(snap, base_url):
    """Строки таблицы QR-источников со статистикой из снапшота."""
    leads = snap['leads']
    regs = Counter(x['source'] for x in leads)
    enrolled = Counter(x['source'] for x in leads if x['status'] == 'enrolled')
    complete = Counter(x['source'] for x in leads if x['complete'])
    week_ago = today_almaty() - timedelta(days=6)
    week = Counter(x['source'] for x in leads if (_local_date(x['created']) or EPOCH.date()) >= week_ago)
    rows = []
    for code, s in snap['sources'].items():
        scans = _int(s.get('scans'))
        n = regs.get(code, 0)
        ch = s.get('channel') if s.get('channel') in CHANNEL_LABELS else 'other'
        rows.append({'code': code, 'title': _str(s.get('title')) or code, 'channel': ch,
                     'channel_label': CHANNEL_LABELS[ch], 'channel_icon': CHANNEL_ICONS[ch],
                     'note': _str(s.get('note')), 'welcome': _int(s.get('welcomeCoins')),
                     'active': s.get('active') is True, 'scans': scans, 'registrations': n,
                     'counter': _int(s.get('registrations')), 'week': week.get(code, 0),
                     'complete': complete.get(code, 0), 'enrolled': enrolled.get(code, 0),
                     'conv': _pct(n, scans) if scans else None, 'url': base_url + '/' + code,
                     'created': to_datetime(s.get('createdAt')), 'last_scan': to_datetime(s.get('lastScanAt')),
                     'last_reg': to_datetime(s.get('lastRegistrationAt'))})
    rows.sort(key=lambda r: (not r['active'], -r['registrations'], -r['scans'], r['title']))
    return rows


def _sources_page(new_values=None, new_errors=None, status=200):
    """Страница QR-источников; new_values/new_errors — форма «Новый источник» после ошибки (модалка откроется)."""
    base = join_base_url()
    try:
        snap = load_snapshot()
        rows = source_rows(snap, base)
    except Exception as e:
        flash_error('Не удалось загрузить QR-источники', e)
        snap, rows = {'sources': {}}, []
    totals = {'scans': sum(r['scans'] for r in rows), 'registrations': sum(r['registrations'] for r in rows),
              'active': sum(1 for r in rows if r['active']), 'enrolled': sum(r['enrolled'] for r in rows),
              'count': len(rows)}
    totals['conv'] = _pct(totals['registrations'], totals['scans']) if totals['scans'] else None
    q = _str(request.args.get('q'), 80)
    words = _norm(q).split()
    if words:
        rows = [r for r in rows if all(w in _norm(' '.join([r['title'], r['code'], r['note'], r['channel_label']]))
                                       for w in words)]
    values = new_values or {'code': suggest_code(snap['sources']), 'title': '', 'channel': 'stand',
                            'welcomeCoins': 0, 'active': True, 'note': ''}
    return render_template('applicants/sources.html', rows=rows, totals=totals, channels=CHANNELS, q=q,
                           suggested=suggest_code(snap['sources']), join_base=base, max_welcome=MAX_WELCOME,
                           new_values=values, new_errors=new_errors or {}), status


@routes.route('/applicants/sources')
@admin_required
def applicant_sources():
    return _sources_page()


@routes.route('/applicants/sources/new', methods=['POST'])
@admin_required
def applicant_source_create():
    code = _field('code').upper()
    data, errors = _source_form()
    if not SOURCE_CODE_RE.match(code):
        errors['code'] = 'Код — 3–24 латинские заглавные буквы и цифры (например, STAND1)'
    elif code in RESERVED_SOURCES:
        errors['code'] = 'Коды APP и WEB зарезервированы — выберите другой'
    values = dict(data, code=code, title=_field('title'), note=_field('note'), welcomeCoins=_field('welcomeCoins'))
    if errors:
        flash('Проверьте поля формы', 'error')
        return _sources_page(values, errors, 400)
    doc = dict(data, scans=0, registrations=0, createdAt=SERVER_TIMESTAMP, createdBy=_admin().get('email'),
               updatedAt=SERVER_TIMESTAMP)
    try:
        db.collection('applicant_sources').document(code).create(doc)
    except Exception as e:
        if e.__class__.__name__ in ('AlreadyExists', 'Conflict'):
            flash('Проверьте поля формы', 'error')
            return _sources_page(values, {'code': 'Источник с кодом %s уже есть — выберите другой код' % code}, 400)
        flash_error('Не удалось создать источник', e)
        return _sources_page(values, {}, 500)
    audit('create', 'applicant_sources', code, data)
    invalidate_applicants_cache()
    flash_action('Источник %s создан — QR-код готов к печати' % code, 'Плакат',
                 href=url_for('applicant_source_poster', code=code))
    return redirect(url_for('applicant_sources', _anchor='src-' + code))


@routes.route('/applicants/sources/<code>/edit', methods=['GET', 'POST'])
@admin_required
def applicant_source_edit(code):
    code = _str(code, 24).upper()
    ref = db.collection('applicant_sources').document(code)
    try:
        snap = ref.get()
    except Exception as e:
        flash_error('Не удалось прочитать источник', e)
        return redirect(url_for('applicant_sources'))
    if not snap.exists:
        abort(404)
    current = snap.to_dict() or {}
    errors = {}
    form = {'title': current.get('title') or '', 'channel': current.get('channel') or 'other',
            'note': current.get('note') or '', 'welcomeCoins': _int(current.get('welcomeCoins')),
            'active': current.get('active') is True}
    if request.method == 'POST':
        form, errors = _source_form()
        if not errors:
            try:
                ref.update(dict(form, updatedAt=SERVER_TIMESTAMP))
            except Exception as e:
                flash_error('Не удалось сохранить источник', e)
                return redirect(url_for('applicant_source_edit', code=code))
            audit('update', 'applicant_sources', code, form)
            invalidate_applicants_cache()
            flash('Источник %s сохранён' % code, 'success')
            return redirect((next_target() or url_for('applicant_sources')) + '#src-' + code)
    base = join_base_url()
    stats = None
    try:
        stats = next((r for r in source_rows(load_snapshot(), base) if r['code'] == code), None)
    except Exception:
        log.warning('Статистика источника %s не посчитана', code, exc_info=True)
    return render_template('applicants/source_form.html', code=code, form=form, errors=errors,
                           channels=CHANNELS, stats=stats, url=base + '/' + code, max_welcome=MAX_WELCOME,
                           created=to_datetime(current.get('createdAt')),
                           created_by=_str(current.get('createdBy'))), (400 if errors else 200)


@routes.route('/applicants/sources/<code>/toggle', methods=['POST'])
@admin_required
def applicant_source_toggle(code):
    code = _str(code, 24).upper()
    ref = db.collection('applicant_sources').document(code)
    try:
        snap = ref.get()
        if not snap.exists:
            flash('Источник не найден', 'error')
            return redirect(url_for('applicant_sources'))
        active = not ((snap.to_dict() or {}).get('active') is True)
        ref.update({'active': active, 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось переключить источник', e)
        return redirect(url_for('applicant_sources'))
    audit('enable' if active else 'disable', 'applicant_sources', code)
    invalidate_applicants_cache()
    # обратимое — без подтверждения, «Отменить» в тосте (UX_SPEC_v2 §4.11)
    flash_action('Источник %s включён' % code if active else
                 'Источник %s выключен: регистрация по QR работает, но без приветственных коинов' % code,
                 'Отменить', post=url_for('applicant_source_toggle', code=code),
                 fields={'next': back_url('applicant_sources')})
    return _back('applicant_sources')


@routes.route('/applicants/sources/<code>/delete', methods=['POST'])
@admin_required
def applicant_source_delete(code):
    code = _str(code, 24).upper()
    ref = db.collection('applicant_sources').document(code)
    try:
        snap = ref.get()
        if not snap.exists:
            flash('Источник уже удалён', 'info')
            return redirect(url_for('applicant_sources'))
        q = where(where(db.collection('users'), 'role', '==', 'applicant'), 'source', '==', code)
        used = max(_int((snap.to_dict() or {}).get('registrations')), count(q, default=0) or 0)
        if used:
            flash('По этому QR уже зарегистрировались (%d) — удалить нельзя, иначе аналитика потеряет название. '
                  'Выключите источник' % used, 'error')
            return redirect(url_for('applicant_sources', _anchor='src-' + code))
        ref.delete()
    except Exception as e:
        flash_error('Не удалось удалить источник', e)
        return redirect(url_for('applicant_sources'))
    audit('delete', 'applicant_sources', code)
    invalidate_applicants_cache()
    flash('Источник %s удалён' % code, 'success')
    return redirect(url_for('applicant_sources'))


@routes.route('/applicants/sources/<code>/poster')
@admin_required
def applicant_source_poster(code):
    code = _str(code, 24).upper()
    try:
        snap = db.collection('applicant_sources').document(code).get()
    except Exception as e:
        flash_error('Не удалось прочитать источник', e)
        return redirect(url_for('applicant_sources'))
    if not snap.exists:
        abort(404)
    s = snap.to_dict() or {}
    fmt = (request.args.get('format') or 'a4').lower()
    if fmt not in ('a4', 'a5'):
        fmt = 'a4'
    return render_template('applicants/poster.html', code=code, title=_str(s.get('title')) or code,
                           welcome=_int(s.get('welcomeCoins')) if s.get('active') is True else 0,
                           active=s.get('active') is True, fmt=fmt, url=join_base_url() + '/' + code,
                           adm=load_admissions())


# ==================== БЕЙДЖ САЙДБАРА ====================

def nav_badges():
    """Новые (необработанные) лиды — статус CRM 'new'."""
    n = count(where(db.collection('applicant_crm'), 'status', '==', 'new'), default=0)
    return {'applicants_leads': n or 0}
