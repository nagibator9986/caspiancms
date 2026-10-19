# cms/routes_coins.py — Caspian Coin: магазин мерча, заказы, кампании с кодами, журнал операций (владелец — COINS).
#
# Схема — как в приложении и Cloud Functions (APP: lib/services/coin_service.dart, functions/index.js; SPEC §2.5–2.7):
#   coin_products/{id}     title ≤120, titleKk, description, descriptionKk, price (int 1..100000), stock (int ≥ 0 |
#                          null — без лимита), active, sort, variants [str ≤ 40], limitPerUser (0 — без лимита),
#                          images [{url, path, w, h}] + imageUrl (обложка), createdAt, updatedAt.
#                          Фото — Storage coin_products/<id>/<stamp>_<i><ext>.
#   coin_orders/{id}       создаёт абитуриент {uid, productId, variant?, createdAt}; статус, снимок товара, списание
#                          и склад — ТОЛЬКО Cloud Function (onCoinOrderCreated). CMS меняет лишь статус
#                          reserved → issued | cancelled (+ issuedAt/issuedBy | cancelledAt/cancelledBy, note,
#                          updatedAt); возврат коинов и склад при отмене делает onCoinOrderUpdated.
#   coin_campaigns/{CODE}  id = код ^[A-Z0-9]{4,16}$; claimsCount меняет только функция (CMS пишет 0 при создании
#                          и никогда не трогает при правке).
#   coin_transactions      журнал; CMS создаёт только ручные операции {uid, amount (int ≠ 0, |amount| ≤ 100000),
#                          type: 'manual', reason, createdBy, createdAt} — кошелёк применяет onCoinTransactionCreated.
# CMS НЕ пишет в coin_wallets и не создаёт purchase/refund-операции.
#
# Производительность: товары, кампании, снимок заказов и каталог абитуриентов (для имён и поиска) — в кэше
# «coins:*» с инвалидацией при записи; счётчики — aggregation count()/sum(); имена — get_all, без N+1.

import re
import secrets
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore
from flask import abort, current_app, flash, jsonify, redirect, render_template, request, url_for

from .applicant_login import canonical_login, display_login, normalize_phone, to_auth_email
from .core import (DELETE_FIELD, IS_EMULATOR, ArrayUnion, SERVER_TIMESTAMP, Routes, UploadError, admin_required,
                   aggregate_sum, audit, back_url, cache, chunked, count, csv_response, current_admin, db,
                   delete_storage_paths, doc_dict, flash_action, flash_error, fmt_dt, get_docs, image_fields,
                   images_from_uploader, images_of, local_input_value, log, next_target, now_utc, paginate,
                   paginate_list, parse_local_datetime, plural, safe_next_url, to_almaty, to_datetime, where)

routes = Routes()

DESC = firestore.Query.DESCENDING
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

MAX_COINS = 100000          # |amount| ручной операции и максимум суммы кампании (SPEC §6.5)
MAX_PRICE = 100000          # цена товара (функция отклоняет заказ с ценой > 100000)
MAX_STOCK = 100000
MAX_LIMIT_PER_USER = 100
MAX_VARIANTS = 30
MAX_CAMPAIGN_CLAIMS = 1000000
MAX_RECIPIENTS = 200        # получателей ручной операции за раз
BULK_LIMIT = 100            # заказов в массовом действии
ORDERS_WINDOW = 1500        # последних заказов в снимке (плюс ВСЕ ждущие выдачи)
CLAIMS_LIMIT = 1000         # вводов кода на странице кампании
PER_PAGE = 30
JOURNAL_PER_PAGE = 30        # UX_SPEC_v2 CRM-8: 30 строк на страницу журнала
EXPORT_LIMIT = 5000

PRODUCTS_TTL = 300
ORDERS_TTL = 60
CAMPAIGNS_TTL = 120
PEOPLE_TTL = 300
STATS_TTL = 300

CODE_RE = re.compile(r'^[A-Z0-9]{4,16}$')
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'   # как CoinService.codeAlphabet: без O/0 и I/1
# Кириллица, похожая на латиницу (код часто набирают в русской раскладке) — как CoinService.normalizeCode.
_LOOKALIKES = {'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C',
               'Т': 'T', 'Х': 'X', 'У': 'Y'}

ORDER_STATUSES = [('reserved', 'Ждут выдачи'), ('issued', 'Выданы'), ('cancelled', 'Отменены'),
                  ('rejected', 'Отклонены')]
ORDER_STATUS_KEYS = [k for k, _ in ORDER_STATUSES]
# Причины отказа onCoinOrderCreated (SPEC §2.6)
REJECT_REASONS = {
    'balance': 'не хватило коинов', 'stock': 'закончился на складе', 'inactive': 'товар скрыт',
    'not_found': 'товар удалён', 'limit': 'превышен лимит на человека', 'variant': 'нет такого варианта',
    'invalid': 'ошибка заказа',
}
# Причины отказа onCoinClaimCreated
CLAIM_REASONS = {
    'invalid': 'ошибка кода', 'not_found': 'кампания удалена', 'inactive': 'кампания выключена',
    'not_started': 'ещё не началась', 'expired': 'срок истёк', 'limit': 'лимит участников исчерпан',
}
TX_TYPES = [('manual', 'Вручную'), ('campaign', 'Кампания'), ('welcome', 'Приветствие'), ('quest', 'Квест'),
            ('referral', 'Реферал'), ('purchase', 'Покупка'), ('refund', 'Возврат')]
TX_TYPE_KEYS = [k for k, _ in TX_TYPES]
TX_STATUSES = [('applied', 'Проведено'), ('pending', 'В обработке'), ('error', 'Ошибка')]
PERIODS = [('7', '7 дней'), ('30', '30 дней'), ('90', '90 дней'), ('365', 'Год')]
PRODUCT_STATES = [('active', 'В продаже'), ('soldout', 'Закончились'), ('hidden', 'Скрытые')]
CAMPAIGN_STATES = {
    'live': ('Идёт', 'success', 'broadcast'), 'scheduled': ('Скоро', 'info', 'clock'),
    'limit': ('Лимит исчерпан', 'warning', 'people-fill'), 'inactive': ('Выключена', 'neutral', 'pause-circle'),
    'expired': ('Завершена', 'neutral', 'calendar-x'),
}
VARIANT_PRESETS = [('Размеры одежды', ['XS', 'S', 'M', 'L', 'XL', 'XXL']), ('Цвета', ['Белый', 'Чёрный', 'Оранжевый'])]

PEOPLE_FIELDS = ['name', 'login', 'email', 'phone', 'role']
ORDER_FIELDS = ['uid', 'productId', 'variant', 'status', 'reason', 'price', 'title', 'titleKk', 'imageUrl',
                'createdAt', 'reservedAt', 'processedAt', 'issuedAt', 'issuedBy', 'cancelledAt', 'cancelledBy',
                'note', 'updatedAt', 'refundedAt']


# ==================== МЕЛКИЕ ПОМОЩНИКИ ====================

def _str(v, maxlen=None):
    s = '' if v is None else str(v).strip()
    return s[:maxlen] if maxlen else s


def _int(v, default=None):
    """'12' / 12 / 12.0 → 12; дробь, мусор, bool → default."""
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else default
    s = _str(v).replace(' ', '').replace(' ', '')
    if re.fullmatch(r'[-+]?\d{1,12}', s):
        return int(s)
    return default


def _norm(s):
    return re.sub(r'\s+', ' ', str(s or '').lower().replace('ё', 'е')).strip()


def _field(name, maxlen=None):
    return _str(request.form.get(name), maxlen)


def _admin_uid():
    return (current_admin() or {}).get('uid')


def _admin_email():
    adm = current_admin() or {}
    return adm.get('email') or adm.get('uid') or 'admin'


def _back(endpoint, **values):
    """Назад на ?next / поле next (только свой сайт), иначе url_for(endpoint)."""
    nxt = safe_next_url(request.form.get('next') or request.args.get('next'))
    return redirect(nxt or url_for(endpoint, **values))


def _ts(v):
    return to_datetime(v) or EPOCH


def _safe_doc_id(value, maxlen=128):
    """Строку можно использовать как id документа: без '/', не '.', '..' и не __x__ (как isSafeDocId в функциях)."""
    s = _str(value)
    return bool(s) and len(s) <= maxlen and '/' not in s and s not in ('.', '..') and not re.match(r'^__.*__$', s)


def _safe(fn, default):
    """Вызвать загрузчик; при ошибке Firestore — лог и default (страница не падает)."""
    try:
        return fn()
    except Exception:
        log.exception('Caspian Coin: %s не загружено', getattr(fn, '__name__', fn))
        return default


def _has_endpoint(name):
    return name in current_app.view_functions


def site_root():
    """Корень сайта для ссылок наружу (QR, registerUrl). В проде — всегда https."""
    root = request.url_root
    if not IS_EMULATOR and root.startswith('http://'):
        host = request.host.split(':')[0]
        if host not in ('localhost', '127.0.0.1'):
            root = 'https://' + root[len('http://'):]
    return root.rstrip('/') + '/'


_CSV_DANGER = ('=', '+', '-', '@', '\t', '\r')


def csv_cell(v):
    """Защита от формул в Excel (CSV injection): опасное начало строки экранируем апострофом; числа — как есть."""
    if v is None:
        return ''
    if isinstance(v, bool):
        return 'да' if v else 'нет'
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, datetime):
        return fmt_dt(v)
    s = str(v)
    return "'" + s if s.startswith(_CSV_DANGER) else s


def normalize_code(raw):
    """Код кампании к виду id документа — порт CoinService.normalizeCode (APP): верхний регистр, без пробелов,
    дефисов, подчёркиваний и точек, кириллица-двойник → латиница. Прочие символы остаются (такой код не пройдёт
    CODE_RE — админ увидит ошибку, а не молча искажённый код)."""
    out = []
    for ch in _str(raw).upper():
        if re.match(r'[\s\-_.]', ch):
            continue
        out.append(_LOOKALIKES.get(ch, ch))
    return ''.join(out)


def is_valid_code(code):
    return bool(CODE_RE.match(code or ''))


def generate_code(length=6, taken=()):
    """Случайный код из алфавита без похожих символов (как CoinService.generateCode)."""
    for _ in range(50):
        code = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length))
        if code not in taken:
            return code
    return ''.join(secrets.choice(CODE_ALPHABET) for _ in range(length + 2))


def _period(days):
    """('7'|'30'|'90'|'365') → aware datetime начала периода, иначе None."""
    n = _int(days) if _str(days) in dict(PERIODS) else None
    return now_utc() - timedelta(days=n) if n else None


def coins_word(n):
    return plural(n, 'коин', 'коина', 'коинов')


def invalidate_coins_cache(*keys):
    """Сбросить кэш раздела: ключи 'coins:<k>' (без аргументов — всё «coins:*») и бейджи сайдбара."""
    if keys:
        for k in keys:
            cache.invalidate('coins:' + k)
    else:
        cache.invalidate_prefix('coins:')
    cache.invalidate('nav:badges')


# ==================== ЛЮДИ (имена, логины, поиск) ====================

def display_login_of(u):
    """Логин для показа — как ApplicantLogin.displayOf (APP): поле login, иначе по email."""
    u = u or {}
    login = _str(u.get('login'))
    if login:
        email = to_auth_email(login)
        return display_login(email) if email else login
    email = _str(u.get('email'))
    return display_login(email) if email else ''


def load_people():
    """Каталог абитуриентов {uid: {name, login, email, phone, role}} для имён и поиска (select, кэш 5 мин)."""
    def load():
        out = {}
        q = where(db.collection('users'), 'role', '==', 'applicant').select(PEOPLE_FIELDS)
        for s in q.stream():
            out[s.id] = s.to_dict() or {}
        return out
    return cache.get_or_set('coins:people', load, PEOPLE_TTL)


def people_for(uids):
    """{uid: данные} для списка uid: из каталога абитуриентов, недостающих (админы, студенты) — get_all."""
    uids = [u for u in dict.fromkeys(uids or []) if _safe_doc_id(u)]
    directory = _safe(load_people, {})
    out = {u: directory[u] for u in uids if u in directory}
    missing = [u for u in uids if u not in out]
    if missing:
        out.update(get_docs('users', missing, PEOPLE_FIELDS))
    return out


def person(uid, people):
    """Человек для таблиц: title (ФИО или логин), login, phone и ссылка на карточку абитуриента."""
    uid = _str(uid)
    u = people.get(uid) or {}
    name = _str(u.get('name'))
    name = name if len(name) >= 2 else ''
    login = display_login_of(u)
    role = _str(u.get('role'))
    link = None
    if uid and role in ('applicant', 'student') and _has_endpoint('applicant_detail'):
        link = url_for('applicant_detail', uid=uid)
    return {'uid': uid, 'name': name, 'login': login, 'title': name or login or uid or '—',
            'phone': _str(u.get('phone')), 'role': role, 'known': uid in people, 'link': link}


_PHONE_CHARS = re.compile(r'[0-9 ()+\-]+')


def _match_login(u, canon, email, phone):
    if canon and _str(u.get('login')).lower() == canon:
        return True
    if email and _str(u.get('email')).lower() == email:
        return True
    return bool(phone and normalize_phone(_str(u.get('phone'))) == phone)


def find_people(q, limit=20):
    """uid'ы по запросу: точный uid, логин, телефон, email или часть ФИО/логина. Абитуриенты — из каталога
    (кэш), остальные — точным совпадением uid/email. → [uid] (не больше limit)."""
    q = _str(q, 120)
    if not q:
        return []
    found = []

    def add(uid):
        if uid and uid not in found:
            found.append(uid)

    directory = _safe(load_people, {})
    if q in directory:
        add(q)
    canon = (canonical_login(q) or '').lower()
    email = (to_auth_email(q) or '').lower()
    phone = normalize_phone(q) if _PHONE_CHARS.fullmatch(q) else None
    for uid, u in directory.items():
        if _match_login(u, canon, email, phone):
            add(uid)
    nq = _norm(q)
    if len(nq) >= 2 and len(found) < limit:
        for uid, u in directory.items():
            hay = _norm(' '.join([_str(u.get('name')), display_login_of(u), _str(u.get('phone')),
                                  _str(u.get('email'))]))
            if nq in hay:
                add(uid)
            if len(found) >= limit:
                break
    if not found:
        try:
            if _safe_doc_id(q) and db.collection('users').document(q).get().exists:
                add(q)
            elif email:
                for s in where(db.collection('users'), 'email', '==', email).limit(5).stream():
                    add(s.id)
        except Exception:
            log.warning('Поиск пользователя «%s» не удался', q, exc_info=True)
    return found[:limit]


# ==================== БЕЙДЖ САЙДБАРА ====================

def nav_badges():
    """Бейдж сайдбара: заказы в статусе reserved (ждут выдачи)."""
    n = count(where(db.collection('coin_orders'), 'status', '==', 'reserved'), default=0)
    return {'coins_orders': n}


# ==================== МАГАЗИН: ТОВАРЫ ====================

def _product_sort_key(p):
    s = _int(p.get('sort'))
    return (s if s is not None else 10 ** 6, _ts(p.get('createdAt')))


def sort_missing(p):
    """У товара нет целого sort. Приложение делает orderBy('sort'): Firestore ИСКЛЮЧАЕТ документы без поля,
    а строка/дробь ломает разбор — такой товар в магазине приложения не виден (UX_SPEC_v2 CRM-1)."""
    v = (p or {}).get('sort')
    return isinstance(v, bool) or not isinstance(v, int)


def next_sort(exclude=()):
    """max(sort) + 1 по СВЕЖИМ данным (не из кэша) — для нового товара и для товара без sort."""
    best = None
    for s in db.collection('coin_products').select(['sort']).stream():
        if s.id in exclude:
            continue
        v = (s.to_dict() or {}).get('sort')
        if isinstance(v, int) and not isinstance(v, bool):
            best = v if best is None else max(best, v)
    return 0 if best is None else best + 1


def sort_fix(d, pid=None):
    """Поля для записи, чтобы у товара d стал целый sort: {} — уже в порядке; '3'/3.0 → 3; нет поля → в конец."""
    if not sort_missing(d):
        return {}
    v = _int((d or {}).get('sort'))
    return {'sort': v if v is not None and v >= 0 else next_sort(exclude=(pid,) if pid else ())}


def load_products():
    """Все товары по порядку sort (кэш 5 мин). Документы без sort — в конце."""
    def load():
        items = []
        for s in db.collection('coin_products').stream():
            d = s.to_dict() or {}
            d['id'] = s.id
            items.append(d)
        items.sort(key=_product_sort_key)
        return items
    return cache.get_or_set('coins:products', load, PRODUCTS_TTL)


def stock_of(p):
    """Остаток: int ≥ 0 или None (без лимита) — как в функции: число → лимит, null/нет поля → без лимита."""
    v = (p or {}).get('stock')
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return max(int(v), 0)
    return None


def limit_of(p):
    """Лимит на человека: нет поля → 1, 0 → без лимита (как orderLimitPerUser)."""
    v = (p or {}).get('limitPerUser')
    if v is None:
        return 1
    n = _int(v)
    return max(n, 0) if n is not None else 1


def variants_of(p):
    raw = (p or {}).get('variants')
    if not isinstance(raw, list):
        return []
    return [_str(v) for v in raw if isinstance(v, str) and _str(v)]


def product_state(p):
    if p.get('active') is not True:
        return 'hidden'
    st = stock_of(p)
    return 'soldout' if st is not None and st <= 0 else 'active'


def product_view(p, stats=None):
    imgs = images_of(p)
    st = stock_of(p)
    stats = stats or {}
    title = _str(p.get('title')) or 'Без названия'
    variants = variants_of(p)
    return {
        'id': p['id'], 'title': title, 'title_kk': _str(p.get('titleKk')),
        'description': _str(p.get('description')), 'price': _int(p.get('price'), 0) or 0,
        'stock': st, 'unlimited': st is None, 'low': st is not None and 0 < st <= 3,
        'active': p.get('active') is True, 'state': product_state(p), 'variants': variants,
        'limit': limit_of(p), 'cover': imgs[0]['url'] if imgs else None, 'photos': len(imgs),
        'reserved': stats.get('reserved', 0), 'issued': stats.get('issued', 0),
        'updated': to_datetime(p.get('updatedAt')),
        'search': _norm(' '.join([title, _str(p.get('titleKk')), _str(p.get('description')), ' '.join(variants)])),
    }


def _product_order_stats(orders):
    """{productId: {'reserved': n, 'issued': n}} по снимку заказов."""
    out = {}
    for o in orders:
        st = o.get('status')
        if st in ('reserved', 'issued'):
            d = out.setdefault(o.get('productId'), {'reserved': 0, 'issued': 0})
            d[st] += 1
    return out


@routes.route('/coins/shop')
@admin_required
def coins_shop():
    products = _safe(load_products, None)
    if products is None:
        flash('Не удалось загрузить товары. Обновите страницу', 'error')
        products = []
    orders = _safe(load_orders, {'orders': []})['orders']
    stats = _product_order_stats(orders)
    views = [product_view(p, stats.get(p['id'])) for p in products]
    state = request.args.get('state', '')
    state = state if state in dict(PRODUCT_STATES) else ''
    counts = {k: sum(1 for v in views if v['state'] == k) for k, _ in PRODUCT_STATES}
    shown = [v for v in views if not state or v['state'] == state]
    totals = {
        'products': len(views), 'active': counts['active'],
        'reserved': sum(v['reserved'] for v in views), 'issued': sum(v['issued'] for v in views),
        'low': sum(1 for v in views if v['low'] and v['active']), 'soldout': counts['soldout'],
    }
    unsorted = [p['id'] for p in products if sort_missing(p)]
    return render_template('coins/shop.html', products=shown, total=len(views), counts=counts, state=state,
                           states=PRODUCT_STATES, totals=totals, can_sort=not state and len(views) > 1,
                           unsorted=unsorted, order_ids=[p['id'] for p in products])


def _parse_variants(raw):
    """Варианты из textarea: по строкам или через запятую, ≤ 40 символов, без повторов. → (list, error)."""
    out, seen = [], set()
    for p in re.split(r'[\n,;]+', raw or ''):
        v = _str(p)
        if not v:
            continue
        if len(v) > 40:
            return out, 'Вариант «%s…» длиннее 40 символов' % v[:20]
        if v.lower() in seen:
            continue
        seen.add(v.lower())
        out.append(v)
    if len(out) > MAX_VARIANTS:
        return out[:MAX_VARIANTS], 'Не больше %d вариантов' % MAX_VARIANTS
    return out, None


def _product_form_values(doc=None):
    if request.method == 'POST':
        return {
            'title': _field('title'), 'titleKk': _field('titleKk'),
            'description': request.form.get('description', ''),
            'descriptionKk': request.form.get('descriptionKk', ''), 'price': _field('price'),
            'stock': _field('stock'), 'stock_orig': _field('stock_orig'),
            'unlimited': bool(request.form.get('unlimited')), 'limitPerUser': _field('limitPerUser'),
            'variants': request.form.get('variants', ''), 'active': bool(request.form.get('active')),
        }
    if doc is None:
        return {'title': '', 'titleKk': '', 'description': '', 'descriptionKk': '', 'price': '', 'stock': '10',
                'stock_orig': '', 'unlimited': False, 'limitPerUser': '1', 'variants': '', 'active': True}
    st = stock_of(doc)
    price = _int(doc.get('price'))
    return {
        'title': _str(doc.get('title')), 'titleKk': _str(doc.get('titleKk')),
        'description': _str(doc.get('description')), 'descriptionKk': _str(doc.get('descriptionKk')),
        'price': '' if price is None else str(price), 'stock': '' if st is None else str(st),
        'stock_orig': '' if st is None else str(st), 'unlimited': st is None,
        'limitPerUser': str(limit_of(doc)), 'variants': '\n'.join(variants_of(doc)),
        'active': doc.get('active') is True,
    }


def _validate_product(v):
    errors = {}
    title = v['title']
    if not title:
        errors['title'] = 'Укажите название'
    elif len(title) > 120:
        errors['title'] = 'Название — не длиннее 120 символов'
    if len(v['titleKk']) > 120:
        errors['titleKk'] = 'Название — не длиннее 120 символов'
    for k in ('description', 'descriptionKk'):
        if len(v[k]) > 2000:
            errors[k] = 'Описание — не длиннее 2000 символов'
    price = _int(v['price'])
    if price is None or not 1 <= price <= MAX_PRICE:
        errors['price'] = 'Цена — целое число от 1 до %d коинов' % MAX_PRICE
    stock = None
    if not v['unlimited']:
        stock = _int(v['stock'])
        if stock is None or not 0 <= stock <= MAX_STOCK:
            errors['stock'] = 'Остаток — целое число от 0 до %d (или «без лимита»)' % MAX_STOCK
    limit = _int(v['limitPerUser'])
    if limit is None or not 0 <= limit <= MAX_LIMIT_PER_USER:
        errors['limitPerUser'] = 'Лимит — от 0 (без лимита) до %d' % MAX_LIMIT_PER_USER
    variants, verr = _parse_variants(v['variants'])
    if verr:
        errors['variants'] = verr
    data = {'title': title, 'titleKk': v['titleKk'] or None, 'description': v['description'].strip() or None,
            'descriptionKk': v['descriptionKk'].strip() or None, 'price': price, 'limitPerUser': limit,
            'variants': variants, 'active': v['active']}
    return errors, data, stock


def apply_stock(current, form_stock, form_orig, unlimited):
    """Итоговый остаток с учётом заказов, оформленных, пока форма была открыта.

    Поле не меняли → оставить текущее значение (функция могла уже списать). Изменили число → применить разницу
    к ТЕКУЩЕМУ остатку (не ниже 0). Переключили «без лимита» ↔ число → записать как есть.
    → (менять_ли, значение)."""
    orig = _int(form_orig) if _str(form_orig) != '' else None
    if unlimited:
        return current is not None, None
    if orig is not None and current is not None:
        if form_stock == orig:
            return False, current
        return True, max(current + (form_stock - orig), 0)
    return True, form_stock


class _Gone(Exception):
    """Документ удалили во время правки."""


def _update_product(ref, data, stock, values):
    """Правка товара в транзакции: склад меняется относительно ТЕКУЩЕГО значения (см. apply_stock)."""
    transaction = db.transaction()

    @firestore.transactional
    def run(t):
        snap = ref.get(transaction=t)
        if not snap.exists:
            raise _Gone()
        current = snap.to_dict() or {}
        change, value = apply_stock(stock_of(current), stock, values['stock_orig'], values['unlimited'])
        upd = dict(data)
        if change:
            upd['stock'] = value
        upd.update(sort_fix(current, ref.id))   # старый товар без sort — сразу в конец витрины приложения
        t.update(ref, upd)

    run(transaction)


@routes.route('/coins/shop/new', methods=['GET', 'POST'])
@routes.route('/coins/shop/<product_id>/edit', methods=['GET', 'POST'])
@admin_required
def coins_product_form(product_id=None):
    doc = None
    if product_id:
        if not _safe_doc_id(product_id):
            abort(404)
        doc = doc_dict(db.collection('coin_products').document(product_id).get())
        if doc is None:
            flash('Товар не найден — возможно, его уже удалили', 'warning')
            return redirect(url_for('coins_shop'))
    values = _product_form_values(doc)
    errors = {}
    if request.method == 'POST':
        errors, data, stock = _validate_product(values)
        if not errors:
            coll = db.collection('coin_products')
            ref = coll.document(product_id) if product_id else coll.document()
            try:
                res = images_from_uploader('images', images_of(doc) if doc else [], 'coin_products/%s' % ref.id)
            except UploadError as e:
                errors['images'] = str(e)
            else:
                data.update(image_fields(res.images))
                data['updatedAt'] = SERVER_TIMESTAMP
                optional = ('titleKk', 'description', 'descriptionKk')
                try:
                    if product_id:
                        # очищенные необязательные поля — удаляем, а не пишем null
                        data.update({k: DELETE_FIELD for k in optional if data.get(k) is None})
                        _update_product(ref, data, stock, values)
                    else:
                        for k in optional:
                            if data.get(k) is None:
                                data.pop(k)
                        data.update({'stock': stock, 'sort': next_sort(), 'createdAt': SERVER_TIMESTAMP})
                        ref.set(data)   # документ — ПОСЛЕ загрузки фото
                except _Gone:
                    delete_storage_paths(res.uploaded_paths)
                    invalidate_coins_cache('products')
                    flash('Товар удалили, пока форма была открыта', 'warning')
                    return redirect(url_for('coins_shop'))
                except Exception as e:
                    delete_storage_paths(res.uploaded_paths)
                    flash_error('Не удалось сохранить товар. Попробуйте ещё раз', e)
                else:
                    delete_storage_paths([p for p in res.removed_paths if p.startswith('coin_products/')])
                    invalidate_coins_cache('products')
                    audit('update' if product_id else 'create', 'coin_product', ref.id,
                          {'title': data['title'], 'price': data['price'], 'stock': stock, 'active': data['active'],
                           'photos': len(res.images)})
                    flash(('Товар «%s» сохранён' if product_id else 'Товар «%s» добавлен в магазин') % data['title'],
                          'success')
                    return redirect((next_target() or url_for('coins_shop')) + '#p-' + ref.id)
        if errors:
            flash('Проверьте поля формы', 'error')
    stats = {}
    if doc:
        stats = _product_order_stats(_safe(load_orders, {'orders': []})['orders']).get(doc['id'], {})
    return render_template('coins/product_form.html', product=doc, values=values, errors=errors,
                           presets=VARIANT_PRESETS, stats=stats, max_price=MAX_PRICE, max_stock=MAX_STOCK,
                           max_limit=MAX_LIMIT_PER_USER)


@routes.route('/coins/shop/<product_id>/toggle', methods=['POST'])
@admin_required
def coins_product_toggle(product_id):
    if not _safe_doc_id(product_id):
        abort(404)
    ref = db.collection('coin_products').document(product_id)
    try:
        snap = ref.get()
        if not snap.exists:
            flash('Товар не найден', 'warning')
            return _back('coins_shop')
        data = snap.to_dict() or {}
        active = data.get('active') is not True
        ref.update(dict({'active': active, 'updatedAt': SERVER_TIMESTAMP}, **sort_fix(data, product_id)))
    except Exception as e:
        flash_error('Не удалось изменить видимость товара', e)
        return _back('coins_shop')
    title = _str(data.get('title')) or product_id
    invalidate_coins_cache('products')
    audit('show' if active else 'hide', 'coin_product', product_id, {'title': title})
    # обратимое действие — без подтверждения, с «Отменить» в тосте (UX_SPEC_v2 §4.11)
    flash_action('«%s» %s' % (title, 'снова в магазине' if active else 'скрыт из магазина — новые заказы невозможны'),
                 'Отменить', post=url_for('coins_product_toggle', product_id=product_id),
                 fields={'next': back_url('coins_shop')})
    return _back('coins_shop')


@routes.route('/coins/shop/<product_id>/stock', methods=['POST'])
@admin_required
def coins_product_stock(product_id):
    """Пополнить/списать склад на delta (в транзакции, не ниже 0) или сделать товар «без лимита»."""
    if not _safe_doc_id(product_id):
        abort(404)
    mode = 'unlimited' if request.form.get('mode') == 'unlimited' else 'delta'
    delta = _int(request.form.get('delta'))
    if mode == 'delta' and (delta is None or delta == 0 or abs(delta) > MAX_STOCK):
        flash('Укажите, на сколько изменить остаток (например, 10 или -2)', 'error')
        return _back('coins_shop')
    ref = db.collection('coin_products').document(product_id)
    transaction = db.transaction()

    @firestore.transactional
    def run(t):
        snap = ref.get(transaction=t)
        if not snap.exists:
            raise _Gone()
        d = snap.to_dict() or {}
        current = stock_of(d)
        fix = sort_fix(d, product_id)
        if mode == 'unlimited':
            t.update(ref, dict({'stock': None, 'updatedAt': SERVER_TIMESTAMP}, **fix))
            return d, current, None
        new = (current if current is not None else 0) + delta
        if new < 0:
            raise ValueError(current or 0)
        if new > MAX_STOCK:
            raise OverflowError()
        t.update(ref, dict({'stock': new, 'updatedAt': SERVER_TIMESTAMP}, **fix))
        return d, current, new

    try:
        d, before, after = run(transaction)
    except _Gone:
        flash('Товар не найден', 'warning')
        return _back('coins_shop')
    except ValueError as e:
        flash('Нельзя списать больше, чем на складе (%s шт.)' % e.args[0], 'error')
        return _back('coins_shop')
    except OverflowError:
        flash('Слишком большой остаток (максимум %d)' % MAX_STOCK, 'error')
        return _back('coins_shop')
    except Exception as e:
        flash_error('Не удалось изменить остаток', e)
        return _back('coins_shop')
    title = _str(d.get('title')) or product_id
    invalidate_coins_cache('products')
    audit('stock', 'coin_product', product_id, {'title': title, 'before': before, 'after': after})
    if after is None:
        flash('«%s»: теперь без лимита' % title, 'success')
    else:
        flash('«%s»: остаток %s → %d шт.' % (title, 'без лимита' if before is None else before, after), 'success')
    return _back('coins_shop')


@routes.route('/coins/shop/<product_id>/delete', methods=['POST'])
@admin_required
def coins_product_delete(product_id):
    """Удалить товар вместе с фото. Товар с заказами не удаляется (история заказов ссылается на него и его
    фото) — его можно скрыть."""
    if not _safe_doc_id(product_id):
        abort(404)
    ref = db.collection('coin_products').document(product_id)
    try:
        snap = ref.get()
        if not snap.exists:
            invalidate_coins_cache('products')
            flash('Товар уже удалён', 'info')
            return _back('coins_shop')
        data = snap.to_dict() or {}
        n = count(where(db.collection('coin_orders'), 'productId', '==', product_id), default=None)
        if n is None:
            flash('Не удалось проверить заказы товара — удаление отменено', 'error')
            return _back('coins_shop')
        if n > 0:
            flash('У товара есть заказы (%d) — удалить его нельзя, чтобы не потерять историю. '
                  'Скройте товар, и его больше не смогут заказать' % n, 'warning')
            return _back('coins_shop')
        ref.delete()
    except Exception as e:
        flash_error('Не удалось удалить товар', e)
        return _back('coins_shop')
    delete_storage_paths([im['path'] for im in images_of(data) if im.get('path', '').startswith('coin_products/')])
    invalidate_coins_cache('products')
    title = _str(data.get('title')) or product_id
    audit('delete', 'coin_product', product_id, {'title': title})
    flash('Товар «%s» удалён вместе с фото' % title, 'success')
    return redirect(url_for('coins_shop'))


@routes.route('/coins/shop/reorder', methods=['POST'])
@admin_required
def coins_shop_reorder():
    """Порядок витрины: JSON {ids: [...]} (drag & drop, ответ JSON) или форма ids=… (кнопка «Упорядочить» —
    redirect с тостом); sort = позиция в списке. Товары, которых нет в списке, но у которых нет целого sort,
    ставятся в конец — после этого у КАЖДОГО товара есть целый sort (приложение делает orderBy('sort'))."""
    payload = request.get_json(silent=True)
    as_json = isinstance(payload, dict)
    ids = payload.get('ids') if as_json else request.form.getlist('ids')

    def fail(msg, code):
        if as_json:
            return jsonify({'ok': False, 'error': msg}), code
        flash(msg, 'error')
        return _back('coins_shop')

    if not isinstance(ids, list) or not ids or len(ids) > 500:
        return fail('Пустой или слишком длинный список', 400)
    ids = [i for i in dict.fromkeys(str(x) for x in ids) if _safe_doc_id(i)]
    try:
        # свежие данные: по кэшу можно «не заметить» товар без sort
        known = {}
        for s in db.collection('coin_products').stream():
            known[s.id] = dict(s.to_dict() or {}, id=s.id)
    except Exception:
        log.exception('Товары не прочитаны')
        return fail('Не удалось прочитать товары. Обновите страницу', 500)
    ids = [i for i in ids if i in known]
    if not ids:
        return fail('Товары не найдены — обновите страницу', 400)
    order = list(ids)
    rest = sorted((p for pid, p in known.items() if pid not in ids), key=_product_sort_key)
    tail = len(order) + max([_int(p.get('sort'), -1) for p in rest if not sort_missing(p)] + [-1]) + 1
    changed = 0
    try:
        batch = db.batch()
        for pos, pid in enumerate(order):
            if sort_missing(known[pid]) or known[pid].get('sort') != pos:
                batch.update(db.collection('coin_products').document(pid), {'sort': pos})
                changed += 1
        for p in rest:   # не попали в список (фильтр, новый товар) и без sort — в конец
            if sort_missing(p):
                batch.update(db.collection('coin_products').document(p['id']), {'sort': tail})
                tail += 1
                changed += 1
        if changed:
            batch.commit()
    except Exception:
        log.exception('Порядок товаров не сохранён')
        invalidate_coins_cache('products')
        return fail('Не удалось сохранить порядок. Обновите страницу', 500)
    invalidate_coins_cache('products')
    if changed:
        audit('reorder', 'coin_products', None, {'ids': ids[:50], 'changed': changed})
    if as_json:
        return jsonify({'ok': True, 'changed': changed, 'message': 'Порядок витрины сохранён'})
    flash('Порядок витрины сохранён — все товары видны в приложении' if changed else 'Порядок уже в порядке',
          'success')
    return _back('coins_shop')


@routes.route('/coins/shop/bulk', methods=['POST'])
@admin_required
def coins_shop_bulk():
    action = request.form.get('action')
    ids = [i for i in dict.fromkeys(request.form.getlist('ids')) if _safe_doc_id(i)][:200]
    if action not in ('show', 'hide') or not ids:
        flash('Отметьте товары и выберите действие', 'warning')
        return _back('coins_shop')
    try:
        current = get_docs('coin_products', ids, ['sort'])   # свежие: нужен ли товару sort
        ids = [i for i in ids if i in current]
        if ids:
            batch = db.batch()
            tail = None
            for pid in ids:
                upd = {'active': action == 'show', 'updatedAt': SERVER_TIMESTAMP}
                if sort_missing(current[pid]):
                    v = _int(current[pid].get('sort'))
                    if v is None or v < 0:
                        tail = next_sort() if tail is None else tail
                        v, tail = tail, tail + 1
                    upd['sort'] = v
                batch.update(db.collection('coin_products').document(pid), upd)
            batch.commit()
    except Exception as e:
        flash_error('Не удалось изменить товары', e)
        return _back('coins_shop')
    invalidate_coins_cache('products')
    audit('bulk_' + action, 'coin_products', None, {'ids': ids})
    flash('%s товаров: %d' % ('Показано' if action == 'show' else 'Скрыто', len(ids)), 'success')
    return _back('coins_shop')


# ==================== ЗАКАЗЫ ====================

def load_orders():
    """Снимок заказов: ВСЕ ждущие выдачи + ORDERS_WINDOW последних, с людьми (кэш 60 с)."""
    def load():
        coll = db.collection('coin_orders')
        by_id = {}
        for s in where(coll, 'status', '==', 'reserved').select(ORDER_FIELDS).stream():
            by_id[s.id] = dict(s.to_dict() or {}, id=s.id)
        recent = list(coll.select(ORDER_FIELDS).order_by('createdAt', direction=DESC).limit(ORDERS_WINDOW).stream())
        for s in recent:
            by_id.setdefault(s.id, dict(s.to_dict() or {}, id=s.id))
        orders = sorted(by_id.values(), key=lambda o: _ts(o.get('createdAt')), reverse=True)
        uids = [o.get('uid') for o in orders]
        uids += [o.get(k) for o in orders for k in ('issuedBy', 'cancelledBy') if o.get(k)]
        return {'orders': orders, 'people': people_for(uids), 'truncated': len(recent) >= ORDERS_WINDOW}
    return cache.get_or_set('coins:orders', load, ORDERS_TTL)


def order_counts():
    """Заказы по статусам (aggregation count(), кэш 60 с)."""
    def load():
        coll = db.collection('coin_orders')
        return {st: count(where(coll, 'status', '==', st), default=None) for st in ORDER_STATUS_KEYS}
    return cache.get_or_set('coins:order_counts', load, ORDERS_TTL)


def order_view(o, people, products):
    p = person(o.get('uid'), people)
    status = o.get('status') if o.get('status') in ORDER_STATUS_KEYS else 'pending'
    product = products.get(o.get('productId')) or {}
    pimgs = images_of(product) if product else []
    created = to_datetime(o.get('createdAt'))
    by_uid = _str(o.get('issuedBy') if status == 'issued' else o.get('cancelledBy'))
    if by_uid and by_uid == p['uid']:
        by = 'абитуриент'
    elif by_uid:
        b = people.get(by_uid) or {}
        by = _str(b.get('name')) or _str(b.get('email')) or 'админ'
    else:
        by = ''
    return {
        'id': o['id'], 'uid': p['uid'], 'person': p, 'status': status,
        'title': _str(o.get('title')) or _str(product.get('title')) or _str(o.get('productId')) or 'Товар',
        'product_id': _str(o.get('productId')), 'product_exists': bool(product),
        'variant': _str(o.get('variant')), 'price': _int(o.get('price'), 0) or 0,
        'image': _str(o.get('imageUrl')) or (pimgs[0]['url'] if pimgs else ''),
        'created': created, 'issued_at': to_datetime(o.get('issuedAt')),
        'cancelled_at': to_datetime(o.get('cancelledAt')), 'refunded': o.get('refundedAt') is not None,
        'by': by, 'note': _str(o.get('note')),
        'reason': REJECT_REASONS.get(_str(o.get('reason')), _str(o.get('reason'))) if status == 'rejected' else '',
        'stuck': status == 'pending' and created is not None and now_utc() - created > timedelta(minutes=10),
        'search': _norm(' '.join([o['id'], _str(o.get('title')), _str(o.get('variant')), p['title'], p['login'],
                                  p['phone'], p['uid'], _str(o.get('note'))])),
    }


def _order_filters(args):
    tab = args.get('status', 'reserved')
    period = _str(args.get('period'))
    return {'status': tab if tab in ORDER_STATUS_KEYS + ['pending', 'all'] else 'reserved',
            'q': _str(args.get('q'), 120), 'product': _str(args.get('product'), 64), 'uid': _str(args.get('uid'), 128),
            'period': period if period in dict(PERIODS) else ''}


def filter_orders(views, f):
    since = _period(f['period'])
    words = _norm(f['q']).split()
    out = []
    for v in views:
        if f['status'] != 'all' and v['status'] != f['status']:
            continue
        if f['product'] and v['product_id'] != f['product']:
            continue
        if f['uid'] and v['uid'] != f['uid']:
            continue
        if since and (v['created'] is None or v['created'] < since):
            continue
        if words and not all(w in v['search'] for w in words):
            continue
        out.append(v)
    if f['status'] == 'reserved':
        out.sort(key=lambda v: v['created'] or EPOCH)   # выдаём по очереди: сначала самые давние
    return out


def _orders_context():
    snap = load_orders()
    products = {p['id']: p for p in _safe(load_products, [])}
    return snap, [order_view(o, snap['people'], products) for o in snap['orders']]


@routes.route('/coins/orders')
@admin_required
def coins_orders():
    f = _order_filters(request.args)
    try:
        snap, views = _orders_context()
    except Exception as e:
        flash_error('Не удалось загрузить заказы. Обновите страницу', e)
        snap, views = {'truncated': False, 'people': {}}, []
    counts = dict(_safe(order_counts, {}))
    local = {k: sum(1 for v in views if v['status'] == k) for k in ORDER_STATUS_KEYS + ['pending']}
    for k in ORDER_STATUS_KEYS:
        if counts.get(k) is None:
            counts[k] = local[k]
    counts['pending'] = local['pending']
    rows = filter_orders(views, f)
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    reserved = [v for v in views if v['status'] == 'reserved']
    week_ago = now_utc() - timedelta(days=7)
    kpi = {
        'reserved': counts.get('reserved') or 0, 'reserved_coins': sum(v['price'] for v in reserved),
        'oldest': min((v['created'] for v in reserved if v['created']), default=None),
        'issued_week': sum(1 for v in views
                           if v['status'] == 'issued' and v['issued_at'] and v['issued_at'] >= week_ago),
        'issued': counts.get('issued') or 0, 'stuck': sum(1 for v in views if v['stuck']),
    }
    who = None
    if f['uid']:
        people = snap.get('people') or {}
        if f['uid'] not in people:
            people = dict(people, **people_for([f['uid']]))
        who = person(f['uid'], people)
    product_options = sorted({(v['product_id'], v['title']) for v in views if v['product_id']}, key=lambda x: x[1])
    return render_template('coins/orders.html', page=page, f=f, counts=counts, kpi=kpi, statuses=ORDER_STATUSES,
                           periods=PERIODS, product_options=product_options, who=who, filtered=len(rows),
                           truncated=snap.get('truncated'), bulk_limit=BULK_LIMIT, now=now_utc())


def set_order_status(order_id, new_status, note=None):
    """reserved → issued | cancelled в транзакции. → ('ok'|'missing'|'state', данные заказа до правки).

    Деньги и склад здесь НЕ трогаем: при отмене возврат и склад делает функция onCoinOrderUpdated."""
    ref = db.collection('coin_orders').document(order_id)
    admin_uid = _admin_uid()
    transaction = db.transaction()

    @firestore.transactional
    def run(t):
        snap = ref.get(transaction=t)
        if not snap.exists:
            return 'missing', None
        d = snap.to_dict() or {}
        if d.get('status') != 'reserved':
            return 'state', d
        upd = {'status': new_status, 'updatedAt': SERVER_TIMESTAMP}
        if new_status == 'issued':
            upd.update({'issuedAt': SERVER_TIMESTAMP, 'issuedBy': admin_uid})
        else:
            upd.update({'cancelledAt': SERVER_TIMESTAMP, 'cancelledBy': admin_uid})
        if note:
            upd['note'] = note
        t.update(ref, upd)
        return 'ok', d

    return run(transaction)


_STATE_TEXT = {'issued': 'уже выдан', 'cancelled': 'уже отменён', 'rejected': 'отклонён системой',
               None: 'ещё обрабатывается'}


def _after_orders_change():
    invalidate_coins_cache('orders', 'order_counts', 'products')
    cache.invalidate('applicants:orders')   # блок «Коины» в аналитике абитуриентов


def _order_action(order_id, new_status):
    if not _safe_doc_id(order_id):
        abort(404)
    note = _field('note', 500)
    try:
        result, d = set_order_status(order_id, new_status, note)
    except Exception as e:
        flash_error('Не удалось %s заказ. Попробуйте ещё раз' % ('выдать' if new_status == 'issued' else 'отменить'), e)
        return _back('coins_orders')
    if result == 'missing':
        flash('Заказ не найден — возможно, абитуриент удалил аккаунт', 'warning')
    elif result == 'state':
        flash('Заказ %s — статус не изменён' % _STATE_TEXT.get(d.get('status'), 'в другом статусе'), 'warning')
    else:
        title = _str(d.get('title')) or 'Заказ'
        _after_orders_change()
        audit('issue' if new_status == 'issued' else 'cancel', 'coin_order', order_id,
              {'uid': d.get('uid'), 'title': title, 'price': d.get('price'), 'note': note or None})
        if new_status == 'issued':
            # выдача — явное действие без модалки; в тосте — ссылка на журнал абитуриента (UX_SPEC_v2 §4.11)
            flash_action('«%s» выдан — абитуриент получит уведомление' % title, 'Журнал',
                         href=url_for('coins_journal', uid=d.get('uid')) if d.get('uid') else url_for('coins_journal'))
        else:
            price = _int(d.get('price'), 0) or 0
            flash('Заказ «%s» отменён — %d %s вернутся на счёт абитуриента через несколько секунд'
                  % (title, price, coins_word(price)), 'success')
    return _back('coins_orders')


@routes.route('/coins/orders/<order_id>/issue', methods=['POST'])
@admin_required
def coins_order_issue(order_id):
    return _order_action(order_id, 'issued')


@routes.route('/coins/orders/<order_id>/cancel', methods=['POST'])
@admin_required
def coins_order_cancel(order_id):
    return _order_action(order_id, 'cancelled')


@routes.route('/coins/orders/<order_id>/note', methods=['POST'])
@admin_required
def coins_order_note(order_id):
    """Заметка к заказу (видна только админам): ≤ 500 символов, пустая — удалить."""
    if not _safe_doc_id(order_id):
        abort(404)
    note = (request.form.get('note') or '').strip()
    if len(note) > 500:
        flash('Заметка — не длиннее 500 символов', 'error')
        return _back('coins_orders')
    ref = db.collection('coin_orders').document(order_id)
    try:
        if not ref.get().exists:
            flash('Заказ не найден', 'warning')
            return _back('coins_orders')
        ref.update({'note': note or DELETE_FIELD, 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось сохранить заметку', e)
        return _back('coins_orders')
    invalidate_coins_cache('orders')
    audit('note', 'coin_order', order_id, {'note': note or None})
    flash('Заметка сохранена' if note else 'Заметка удалена', 'success')
    return _back('coins_orders')


@routes.route('/coins/orders/bulk', methods=['POST'])
@admin_required
def coins_orders_bulk():
    action = request.form.get('action')
    ids = [i for i in dict.fromkeys(request.form.getlist('ids')) if _safe_doc_id(i)]
    if action not in ('issue', 'cancel'):
        flash('Выберите действие', 'error')
        return _back('coins_orders')
    if not ids:
        flash('Отметьте хотя бы один заказ', 'warning')
        return _back('coins_orders')
    if len(ids) > BULK_LIMIT:
        flash('За раз можно обработать не больше %d заказов' % BULK_LIMIT, 'error')
        return _back('coins_orders')
    note = _field('note', 500)
    new_status = 'issued' if action == 'issue' else 'cancelled'
    done, skipped, failed = [], 0, 0
    for oid in ids:
        try:
            result, _d = set_order_status(oid, new_status, note)
        except Exception:
            log.exception('Массовое действие %s: заказ %s', action, oid)
            failed += 1
            continue
        if result == 'ok':
            done.append(oid)
        else:
            skipped += 1
    if done:
        _after_orders_change()
        audit('bulk_' + action, 'coin_orders', None, {'ids': done[:100], 'count': len(done), 'note': note or None})
    msg = '%s: %d' % ('Выдано заказов' if action == 'issue' else 'Отменено заказов (с возвратом коинов)', len(done))
    if skipped:
        msg += '; пропущено %d — они уже не ждут выдачи' % skipped
    if failed:
        msg += '; не удалось обработать %d — повторите' % failed
    flash(msg, 'success' if done and not failed else ('warning' if done or skipped else 'error'))
    return _back('coins_orders')


@routes.route('/coins/orders.csv')
@admin_required
def coins_orders_export():
    f = _order_filters(request.args)
    try:
        _snap, views = _orders_context()
    except Exception as e:
        flash_error('Не удалось выгрузить заказы', e)
        return redirect(url_for('coins_orders'))
    rows = filter_orders(views, f)[:EXPORT_LIMIT]
    labels = dict(ORDER_STATUSES, pending='Обрабатывается')
    header = ['Заказ', 'Дата', 'Статус', 'Товар', 'Вариант', 'Цена (коины)', 'Абитуриент', 'Логин', 'Телефон',
              'Выдан/отменён', 'Кем', 'Причина отказа', 'Заметка', 'UID']
    data = [[csv_cell(x) for x in (
        v['id'], fmt_dt(v['created'], default=''), labels.get(v['status'], v['status']), v['title'], v['variant'],
        v['price'], v['person']['name'], v['person']['login'], v['person']['phone'],
        fmt_dt(v['issued_at'] or v['cancelled_at'], default=''), v['by'], v['reason'], v['note'], v['uid'])]
        for v in rows]
    audit('export', 'coin_orders', None, {'status': f['status'], 'rows': len(data)})
    return csv_response('заказы_%s.csv' % to_almaty(now_utc()).strftime('%Y-%m-%d'), header, data)


# ==================== КАМПАНИИ (КОДЫ) ====================

def load_campaigns():
    """Все кампании (новые сверху, кэш 2 мин). id = код."""
    def load():
        items = []
        for s in db.collection('coin_campaigns').stream():
            d = s.to_dict() or {}
            d['id'] = s.id
            items.append(d)
        items.sort(key=lambda c: _ts(c.get('createdAt')), reverse=True)
        return items
    return cache.get_or_set('coins:campaigns', load, CAMPAIGNS_TTL)


def campaign_state(c, now=None):
    """Состояние для экрана — как CoinService.checkCampaign: inactive, scheduled, expired, limit, live."""
    now = now or now_utc()
    if c.get('active') is not True:
        return 'inactive'
    starts, ends = to_datetime(c.get('startsAt')), to_datetime(c.get('endsAt'))
    if starts and now < starts:
        return 'scheduled'
    if ends and now > ends:
        return 'expired'
    mx, claims = _int(c.get('maxClaims'), 0) or 0, _int(c.get('claimsCount'), 0) or 0
    if mx > 0 and claims >= mx:
        return 'limit'
    return 'live'


def campaign_view(c, now=None):
    now = now or now_utc()
    state = campaign_state(c, now)
    label, tone, icon = CAMPAIGN_STATES[state]
    claims = _int(c.get('claimsCount'), 0) or 0
    mx = _int(c.get('maxClaims'), 0) or 0
    amount = _int(c.get('amount'), 0) or 0
    starts, ends = to_datetime(c.get('startsAt')), to_datetime(c.get('endsAt'))
    days_left = None
    if ends and state == 'live':
        days_left = max((to_almaty(ends).date() - to_almaty(now).date()).days, 0)
    return {
        'code': c['id'], 'title': _str(c.get('title')) or c['id'], 'amount': amount, 'claims': claims, 'max': mx,
        'pct': min(100.0, 100.0 * claims / mx) if mx else None, 'place': _str(c.get('place')),
        'starts': starts, 'ends': ends, 'active': c.get('active') is True, 'state': state, 'state_label': label,
        'state_tone': tone, 'state_icon': icon, 'issued': claims * amount, 'days_left': days_left,
        'created': to_datetime(c.get('createdAt')),
        'search': _norm(' '.join([c['id'], _str(c.get('title')), _str(c.get('place'))])),
    }


def _campaign_stats():
    def load():
        tx = db.collection('coin_transactions')
        return {'coins': aggregate_sum(where(tx, 'type', '==', 'campaign'), 'amount', default=None)}
    return cache.get_or_set('coins:campaign_stats', load, STATS_TTL)


_STATE_ORDER = {'live': 0, 'scheduled': 1, 'limit': 2, 'inactive': 3, 'expired': 4}


@routes.route('/coins/campaigns')
@admin_required
def coins_campaigns():
    items = _safe(load_campaigns, None)
    if items is None:
        flash('Не удалось загрузить кампании. Обновите страницу', 'error')
        items = []
    now = now_utc()
    views = [campaign_view(c, now) for c in items]
    state = request.args.get('state', '')
    state = state if state in CAMPAIGN_STATES else ''
    q = _str(request.args.get('q'), 80)
    words = _norm(q).split()
    rows = [v for v in views if (not state or v['state'] == state) and all(w in v['search'] for w in words)]
    rows.sort(key=lambda v: (_STATE_ORDER[v['state']], -(v['created'] or EPOCH).timestamp()))
    state_counts = {k: sum(1 for v in views if v['state'] == k) for k in CAMPAIGN_STATES}
    stats = _safe(_campaign_stats, {})
    kpi = {'live': state_counts['live'], 'scheduled': state_counts['scheduled'], 'total': len(views),
           'claims': sum(v['claims'] for v in views),
           'coins': stats.get('coins') if stats.get('coins') is not None else sum(v['issued'] for v in views)}
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    return render_template('coins/campaigns.html', page=page, kpi=kpi, state=state, q=q, states=CAMPAIGN_STATES,
                           state_counts=state_counts, filtered=len(rows))


def _campaign_form_values(doc=None):
    if request.method == 'POST':
        return {'code': normalize_code(request.form.get('code')), 'title': _field('title'),
                'amount': _field('amount'), 'startsAt': _field('startsAt'), 'endsAt': _field('endsAt'),
                'maxClaims': _field('maxClaims'), 'place': _field('place'), 'active': bool(request.form.get('active'))}
    if doc:
        amount = _int(doc.get('amount'))
        return {'code': doc['id'], 'title': _str(doc.get('title')), 'amount': '' if amount is None else str(amount),
                'startsAt': local_input_value(doc.get('startsAt')), 'endsAt': local_input_value(doc.get('endsAt')),
                'maxClaims': str(_int(doc.get('maxClaims'), 0) or 0), 'place': _str(doc.get('place')),
                'active': doc.get('active') is True}
    start = to_almaty(now_utc()).replace(minute=0, second=0, microsecond=0)
    return {'code': '', 'title': '', 'amount': '50', 'startsAt': start.strftime('%Y-%m-%dT%H:%M'),
            'endsAt': (start + timedelta(days=7)).strftime('%Y-%m-%dT%H:%M'), 'maxClaims': '0', 'place': '',
            'active': True}


def _validate_campaign(v, creating):
    errors = {}
    if creating and v['code'] and not is_valid_code(v['code']):
        errors['code'] = 'Код — 4–16 латинских букв и цифр без пробелов. Проверьте раскладку'
    if not v['title']:
        errors['title'] = 'Укажите название — его увидит абитуриент'
    elif len(v['title']) > 120:
        errors['title'] = 'Название — не длиннее 120 символов'
    amount = _int(v['amount'])
    if amount is None or not 1 <= amount <= MAX_COINS:
        errors['amount'] = 'Сумма — целое число от 1 до %d' % MAX_COINS
    starts, ends = parse_local_datetime(v['startsAt']), parse_local_datetime(v['endsAt'])
    if not starts:
        errors['startsAt'] = 'Укажите начало'
    if not ends:
        errors['endsAt'] = 'Укажите окончание'
    elif starts and ends <= starts:
        errors['endsAt'] = 'Окончание должно быть позже начала'
    mx = _int(v['maxClaims']) if v['maxClaims'] != '' else 0
    if mx is None or not 0 <= mx <= MAX_CAMPAIGN_CLAIMS:
        errors['maxClaims'] = 'Лимит — от 0 (без лимита) до %d' % MAX_CAMPAIGN_CLAIMS
    if len(v['place']) > 120:
        errors['place'] = 'Место — не длиннее 120 символов'
    data = {'title': v['title'], 'amount': amount, 'startsAt': starts, 'endsAt': ends, 'maxClaims': mx,
            'place': v['place'], 'active': v['active']}
    return errors, data


def _campaign_or_404(code):
    if not is_valid_code(code):
        abort(404)
    doc = doc_dict(db.collection('coin_campaigns').document(code).get())
    if doc is None:
        abort(404)
    return doc


def _invalidate_campaigns():
    invalidate_coins_cache('campaigns', 'campaign_stats')
    cache.invalidate('applicants:campaigns')   # названия кампаний в аналитике абитуриентов


@routes.route('/coins/campaigns/new', methods=['GET', 'POST'])
@routes.route('/coins/campaigns/<code>/edit', methods=['GET', 'POST'])
@admin_required
def coins_campaign_form(code=None):
    from google.api_core import exceptions as gexc
    doc = _campaign_or_404(code) if code else None
    values = _campaign_form_values(doc)
    errors = {}
    taken = {c['id'] for c in _safe(load_campaigns, [])}
    if request.method == 'POST':
        errors, data = _validate_campaign(values, creating=doc is None)
        if not errors:
            try:
                if doc is None:
                    code = values['code'] or generate_code(taken=taken)
                    data.update({'claimsCount': 0, 'createdAt': SERVER_TIMESTAMP, 'createdBy': _admin_uid()})
                    db.collection('coin_campaigns').document(code).create(data)
                else:
                    data.update({'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin_uid()})
                    db.collection('coin_campaigns').document(code).update(data)   # claimsCount не трогаем
            except (gexc.AlreadyExists, gexc.Conflict):
                code = None
                errors['code'] = 'Код %s уже занят — придумайте другой' % values['code']
            except gexc.NotFound:
                _invalidate_campaigns()
                flash('Кампанию удалили, пока форма была открыта', 'warning')
                return redirect(url_for('coins_campaigns'))
            except Exception as e:
                code = doc['id'] if doc else None
                flash_error('Не удалось сохранить кампанию. Попробуйте ещё раз', e)
            else:
                _invalidate_campaigns()
                audit('update' if doc else 'create', 'coin_campaign', code,
                      {'title': data['title'], 'amount': data['amount'], 'maxClaims': data['maxClaims'],
                       'active': data['active']})
                flash('Кампания %s сохранена' % code if doc else
                      'Кампания создана: код %s. Покажите его на экране или раздайте участникам' % code, 'success')
                return redirect(url_for('coins_campaign_detail', code=code))
        if errors:
            flash('Проверьте поля формы', 'error')
    return render_template('coins/campaign_form.html', campaign=doc, values=values, errors=errors,
                           suggestion=generate_code(taken=taken), alphabet=CODE_ALPHABET, max_coins=MAX_COINS)


def load_claims(code, limit=CLAIMS_LIMIT):
    """Вводы кода (coin_claims where code ==), новые сверху. Без order_by — не нужен составной индекс."""
    items = [dict(s.to_dict() or {}, id=s.id) for s in
             where(db.collection('coin_claims'), 'code', '==', code).limit(limit).stream()]
    items.sort(key=lambda c: _ts(c.get('createdAt')), reverse=True)
    return items


def resolve_legacy_claims(claims):
    """Вводы без статуса (старые версии функции или данные до неё): если есть операция claim_<id> — ввод
    засчитан. Один get_all на страницу вместо чтения по одному."""
    pending = [c for c in claims if _str(c.get('status')) not in ('accepted', 'rejected')]
    if not pending:
        return claims
    txs = get_docs('coin_transactions', ['claim_' + c['id'] for c in pending], ['amount'])
    for c in pending:
        tx = txs.get('claim_' + c['id'])
        if tx is not None:
            c['status'] = 'accepted'
            c['amount'] = _int(tx.get('amount'), 0) or 0
    return claims


def claim_view(c, people):
    st = _str(c.get('status'))
    status = st if st in ('accepted', 'rejected') else 'pending'
    return {'id': c['id'], 'person': person(c.get('uid'), people), 'status': status,
            'amount': _int(c.get('amount'), 0) or 0, 'created': to_datetime(c.get('createdAt')),
            'reason': CLAIM_REASONS.get(_str(c.get('reason')), _str(c.get('reason'))) if status == 'rejected' else ''}


@routes.route('/coins/campaigns/<code>')
@admin_required
def coins_campaign_detail(code):
    doc = _campaign_or_404(code)
    view = campaign_view(doc)
    try:
        claims = resolve_legacy_claims(load_claims(code))
    except Exception as e:
        flash_error('Не удалось загрузить вводы кода', e)
        claims = []
    people = people_for([c.get('uid') for c in claims])
    rows = [claim_view(c, people) for c in claims]
    st = request.args.get('st', '')
    st = st if st in ('accepted', 'rejected', 'pending') else ''
    page = paginate_list([r for r in rows if not st or r['status'] == st], request.args.get('page'), 50)
    days = {}
    for r in rows:
        if r['status'] == 'accepted' and r['created']:
            d = to_almaty(r['created']).date()
            days[d] = days.get(d, 0) + 1
    stats = {'accepted': sum(1 for r in rows if r['status'] == 'accepted'),
             'rejected': sum(1 for r in rows if r['status'] == 'rejected'),
             'pending': sum(1 for r in rows if r['status'] == 'pending'),
             'coins': sum(r['amount'] for r in rows if r['status'] == 'accepted'),
             'best_day': max(days.items(), key=lambda kv: kv[1]) if days else None}
    reasons = {}
    for r in rows:
        if r['reason']:
            reasons[r['reason']] = reasons.get(r['reason'], 0) + 1
    return render_template('coins/campaign_detail.html', c=view, page=page, stats=stats, st=st,
                           reasons=sorted(reasons.items(), key=lambda kv: -kv[1]), total=len(rows),
                           truncated=len(claims) >= CLAIMS_LIMIT)


@routes.route('/coins/campaigns/<code>/claims.csv')
@admin_required
def coins_campaign_export(code):
    doc = _campaign_or_404(code)
    try:
        claims = resolve_legacy_claims(load_claims(code, EXPORT_LIMIT))
    except Exception as e:
        flash_error('Не удалось выгрузить вводы кода', e)
        return redirect(url_for('coins_campaign_detail', code=code))
    people = people_for([c.get('uid') for c in claims])
    labels = {'accepted': 'Начислено', 'rejected': 'Отклонено', 'pending': 'Обрабатывается'}
    rows = []
    for c in claims:
        v = claim_view(c, people)
        rows.append([csv_cell(x) for x in (fmt_dt(v['created'], default=''), v['person']['name'], v['person']['login'],
                                           v['person']['phone'], labels[v['status']], v['amount'] or '', v['reason'],
                                           v['person']['uid'])])
    audit('export', 'coin_campaign', code, {'rows': len(rows)})
    return csv_response('код_%s.csv' % doc['id'], ['Дата', 'ФИО', 'Логин', 'Телефон', 'Статус', 'Коины',
                                                   'Причина отказа', 'UID'], rows)


@routes.route('/coins/campaigns/<code>/present')
@admin_required
def coins_campaign_present(code):
    """Режим «показать на экране»: крупный код, сумма, срок и QR на регистрацию (для тех, у кого ещё нет
    аккаунта). ?src=CODE — QR ведёт на /join/<CODE> (регистрация засчитается этому QR-источнику)."""
    view = campaign_view(_campaign_or_404(code))
    sources = []
    try:
        for s in db.collection('applicant_sources').select(['title', 'active']).stream():
            d = s.to_dict() or {}
            if d.get('active') is True:
                sources.append((s.id, _str(d.get('title')) or s.id))
        sources.sort(key=lambda x: x[1])
    except Exception:
        log.warning('QR-источники не прочитаны', exc_info=True)
    src = _str(request.args.get('src'), 24)
    src = src if src in dict(sources) else ''
    join_url = site_root() + 'join'
    if src:
        join_url += '/' + src
    else:
        try:
            snap = db.collection('app_config').document('admissions').get()
            reg = _str((snap.to_dict() or {}).get('registerUrl')) if snap.exists else ''
            if reg.startswith(('http://', 'https://')):
                join_url = reg
        except Exception:
            log.warning('registerUrl не прочитан', exc_info=True)
    return render_template('coins/campaign_present.html', c=view, join_url=join_url, sources=sources, src=src)


@routes.route('/coins/campaigns/<code>/toggle', methods=['POST'])
@admin_required
def coins_campaign_toggle(code):
    doc = _campaign_or_404(code)
    active = doc.get('active') is not True
    try:
        db.collection('coin_campaigns').document(code).update({'active': active, 'updatedAt': SERVER_TIMESTAMP,
                                                               'updatedBy': _admin_uid()})
    except Exception as e:
        flash_error('Не удалось изменить кампанию', e)
        return _back('coins_campaigns')
    _invalidate_campaigns()
    audit('enable' if active else 'disable', 'coin_campaign', code, {'title': doc.get('title')})
    # обратимое — без подтверждения, «Отменить» в тосте (UX_SPEC_v2 §4.11)
    flash_action('Код %s %s' % (code, 'снова принимается' if active else 'выключен — новые вводы будут отклонены'),
                 'Отменить', post=url_for('coins_campaign_toggle', code=code),
                 fields={'next': back_url('coins_campaigns')})
    return _back('coins_campaigns')


@routes.route('/coins/campaigns/<code>/delete', methods=['POST'])
@admin_required
def coins_campaign_delete(code):
    """Удалить можно только кампанию без вводов: иначе пропадёт название в журнале и аналитике."""
    doc = _campaign_or_404(code)
    claims = _int(doc.get('claimsCount'), 0) or 0
    if claims > 0:
        flash('Код %s уже вводили (%d) — удалить нельзя, выключите кампанию' % (code, claims), 'warning')
        return _back('coins_campaigns')
    try:
        db.collection('coin_campaigns').document(code).delete()
    except Exception as e:
        flash_error('Не удалось удалить кампанию', e)
        return _back('coins_campaigns')
    _invalidate_campaigns()
    audit('delete', 'coin_campaign', code, {'title': doc.get('title')})
    flash('Кампания %s удалена' % code, 'success')
    return redirect(url_for('coins_campaigns'))


# ==================== ЖУРНАЛ ====================

def _journal_stats():
    """Сводка журнала (aggregation, кэш 5 мин)."""
    def load():
        tx = db.collection('coin_transactions')
        purchases = aggregate_sum(where(tx, 'type', '==', 'purchase'), 'amount', default=None)
        refunds = aggregate_sum(where(tx, 'type', '==', 'refund'), 'amount', default=None)
        month = now_utc() - timedelta(days=30)
        return {
            'issued': aggregate_sum(where(tx, 'amount', '>', 0), 'amount', default=None),
            'spent': (-(purchases or 0) - (refunds or 0)) if purchases is not None else None,
            'balance': aggregate_sum(db.collection('coin_wallets'), 'balance', default=None),
            'errors': count(where(tx, 'applied', '==', False), default=None),
            'manual_month': count(where(where(tx, 'type', '==', 'manual'), 'createdAt', '>=', month), default=None),
        }
    return cache.get_or_set('coins:journal_stats', load, STATS_TTL)


def _journal_filters(args):
    t, st, period = _str(args.get('type')), _str(args.get('status')), _str(args.get('period'))
    return {'type': t if t in TX_TYPE_KEYS else '', 'status': st if st in dict(TX_STATUSES) else '',
            'q': _str(args.get('q'), 120), 'uid': _str(args.get('uid'), 128),
            'period': period if period in dict(PERIODS) else ''}


def tx_status(t):
    if t.get('applied') is True:
        return 'applied'
    if t.get('applied') is False or t.get('error'):
        return 'error'
    return 'pending'


def _tx_matches(t, f, since):
    if f['type'] and t.get('type') != f['type']:
        return False
    if f['status'] and tx_status(t) != f['status']:
        return False
    return not (since and _ts(t.get('createdAt')) < since)


def _sorted_list(items):
    items.sort(key=lambda t: _ts(t.get('createdAt')), reverse=True)
    return items


def journal_query(f, uids):
    """→ ('page', Query) — курсорная пагинация на сервере, или ('list', [dict]) — выборка, отфильтрованная в памяти
    (поиск по людям и фильтр по статусу: для них нет составных индексов)."""
    tx = db.collection('coin_transactions')
    since = _period(f['period'])
    if uids is not None:
        items = []
        for part in chunked(uids, 10):
            q = where(tx, 'uid', '==', part[0]) if len(part) == 1 else where(tx, 'uid', 'in', part)
            items += [dict(s.to_dict() or {}, id=s.id) for s in q.limit(1000).stream()]
        return 'list', _sorted_list([t for t in items if _tx_matches(t, f, since)])
    if f['status'] == 'error':
        items = [dict(s.to_dict() or {}, id=s.id) for s in where(tx, 'applied', '==', False).limit(1000).stream()]
        return 'list', _sorted_list([t for t in items if _tx_matches(t, f, since)])
    q = tx
    if f['type']:
        q = where(q, 'type', '==', f['type'])
    if since:
        q = where(q, 'createdAt', '>=', since)
    q = q.order_by('createdAt', direction=DESC)
    if f['status']:   # «проведено» / «в обработке» — фильтр в памяти по последним 1000
        items = [dict(s.to_dict() or {}, id=s.id) for s in q.limit(1000).stream()]
        return 'list', [t for t in items if tx_status(t) == f['status']]
    return 'page', q


def tx_view(t, people):
    amount = _int(t.get('amount'), 0) or 0
    created_by = _str(t.get('createdBy'))
    if created_by and created_by != 'system':
        b = people.get(created_by) or {}
        by = _str(b.get('name')) or _str(b.get('email')) or ('абитуриент' if created_by == t.get('uid') else 'админ')
    elif created_by == 'system' or t.get('type') in ('campaign', 'welcome', 'quest', 'referral', 'purchase', 'refund'):
        by = 'система'
    else:
        by = ''
    return {'id': t['id'], 'person': person(t.get('uid'), people), 'type': _str(t.get('type')) or 'manual',
            'amount': amount, 'reason': _str(t.get('reason')), 'status': tx_status(t), 'error': _str(t.get('error')),
            'created': to_datetime(t.get('createdAt')), 'by': by, 'order_id': _str(t.get('orderId')),
            'code': _str(t.get('campaignId') or t.get('code'))}


def _resolve_journal_people(f):
    """uid'ы по фильтрам (uid или поиск) → list | None (без фильтра по людям)."""
    if f['uid']:
        return [f['uid']] if _safe_doc_id(f['uid']) else []
    if f['q']:
        return find_people(f['q'], limit=30)
    return None


@routes.route('/coins/journal')
@admin_required
def coins_journal():
    f = _journal_filters(request.args)
    stats = _safe(_journal_stats, {})
    uids = _resolve_journal_people(f)
    matched, items, error = [], [], False
    try:
        if uids is not None and not uids:
            page = paginate_list([], 1, JOURNAL_PER_PAGE)
        else:
            if uids is not None and len(uids) > 1:
                matched = uids[:12]
            kind, res = journal_query(f, uids)
            if kind == 'page':
                page = paginate(res, JOURNAL_PER_PAGE, request.args.get('cursor'))
                items = page.dicts()
            else:
                page = paginate_list(res, request.args.get('page'), JOURNAL_PER_PAGE)
                items = page.items
    except Exception as e:
        flash_error('Не удалось загрузить журнал. Попробуйте сбросить фильтры', e)
        error = True
        page = paginate_list([], 1, JOURNAL_PER_PAGE)
    ids = [t.get('uid') for t in items] + [t.get('createdBy') for t in items] + matched
    if f['uid']:
        ids.append(f['uid'])
    people = people_for([i for i in ids if i and i != 'system'])
    page.items = [tx_view(t, people) for t in items]
    who, wallet = None, None
    if f['uid'] and _safe_doc_id(f['uid']):
        who = person(f['uid'], people)
        w = get_docs('coin_wallets', [f['uid']]).get(f['uid'])
        if w is not None:
            wallet = {'balance': _int(w.get('balance'), 0) or 0, 'earned': _int(w.get('earnedTotal'), 0) or 0,
                      'spent': _int(w.get('spentTotal'), 0) or 0}
    prefill = _str(request.args.get('to'), 200) or (who['login'] or f['uid'] if who else '')
    return render_template('coins/journal.html', page=page, f=f, stats=stats, types=TX_TYPES, statuses=TX_STATUSES,
                           periods=PERIODS, who=who, wallet=wallet, matches=[person(u, people) for u in matched],
                           error=error, max_coins=MAX_COINS, max_recipients=MAX_RECIPIENTS, prefill=prefill,
                           not_found=uids is not None and not uids and bool(f['q'] or f['uid']))


@routes.route('/coins/journal.csv')
@admin_required
def coins_journal_export():
    f = _journal_filters(request.args)
    try:
        uids = _resolve_journal_people(f)
        if uids is not None and not uids:
            items = []
        else:
            kind, res = journal_query(f, uids)
            items = [dict(s.to_dict() or {}, id=s.id) for s in res.limit(EXPORT_LIMIT).stream()] \
                if kind == 'page' else res[:EXPORT_LIMIT]
    except Exception as e:
        flash_error('Не удалось выгрузить журнал', e)
        return redirect(url_for('coins_journal'))
    people = people_for([t.get('uid') for t in items] + [t.get('createdBy') for t in items
                                                         if t.get('createdBy') != 'system'])
    labels, st_labels = dict(TX_TYPES), dict(TX_STATUSES)
    rows = []
    for t in items:
        v = tx_view(t, people)
        rows.append([csv_cell(x) for x in (fmt_dt(v['created'], default=''), labels.get(v['type'], v['type']),
                                           v['amount'], v['reason'], v['person']['name'], v['person']['login'],
                                           v['person']['phone'], st_labels[v['status']], v['by'], v['person']['uid'],
                                           v['id'])])
    audit('export', 'coin_transactions', None, {'type': f['type'], 'rows': len(rows)})
    return csv_response('журнал_коинов_%s.csv' % to_almaty(now_utc()).strftime('%Y-%m-%d'),
                        ['Дата', 'Тип', 'Коины', 'Причина', 'ФИО', 'Логин', 'Телефон', 'Статус', 'Кто провёл', 'UID',
                         'ID операции'], rows)


def resolve_recipient(token, directory):
    """Строка получателя (uid, логин, телефон, email) → uid или None."""
    token = _str(token, 120)
    if not token:
        return None
    if token in directory:
        return token
    canon = (canonical_login(token) or '').lower()
    email = (to_auth_email(token) or '').lower()
    phone = normalize_phone(token) if _PHONE_CHARS.fullmatch(token) else None
    for uid, u in directory.items():
        if _match_login(u, canon, email, None):
            return uid
    if phone:
        for uid, u in directory.items():
            if normalize_phone(_str(u.get('phone'))) == phone:
                return uid
    try:
        if _safe_doc_id(token) and db.collection('users').document(token).get().exists:
            return token
    except Exception:
        log.warning('Получатель %s не проверен', token, exc_info=True)
    return None


@routes.route('/coins/journal/award', methods=['POST'])
@admin_required
def coins_award():
    """Ручное начисление/списание: по записи coin_transactions на получателя (type 'manual').
    Баланс меняет функция onCoinTransactionCreated; CMS в coin_wallets не пишет."""
    amount = _int(request.form.get('amount'))
    reason = _field('reason', 200)
    tokens = [t for t in dict.fromkeys(_str(x) for x in re.split(r'[\n,;]+', request.form.get('recipients', ''))) if t]
    if amount is not None and request.form.get('direction') == 'minus' and amount > 0:
        amount = -amount
    if amount is None or amount == 0 or abs(amount) > MAX_COINS:
        flash('Укажите количество коинов: целое число от 1 до %d' % MAX_COINS, 'error')
        return _back('coins_journal')
    if not reason:
        flash('Укажите причину — её увидит абитуриент в истории коинов', 'error')
        return _back('coins_journal')
    if not tokens:
        flash('Укажите получателей: логин, телефон или UID — по одному в строке', 'error')
        return _back('coins_journal')
    if len(tokens) > MAX_RECIPIENTS:
        flash('За раз — не больше %d получателей' % MAX_RECIPIENTS, 'error')
        return _back('coins_journal')
    directory = _safe(load_people, {})
    resolved, unknown = [], []
    for t in tokens:
        uid = resolve_recipient(t, directory)
        if uid:
            resolved.append(uid)
        else:
            unknown.append(t)
    if unknown:
        flash('Не найдены: %s. Операция не проведена — проверьте логины' % ', '.join(unknown[:10]), 'error')
        return _back('coins_journal')
    resolved = list(dict.fromkeys(resolved))
    users = get_docs('users', resolved, ['role', 'name'])
    wallets = get_docs('coin_wallets', resolved, ['balance'])
    bad = [u for u in resolved if (users.get(u) or {}).get('role') != 'applicant' and u not in wallets]
    if bad:
        flash('Коины начисляются только абитуриентам: %s' % ', '.join(bad[:10]), 'error')
        return _back('coins_journal')
    if amount < 0:
        short = [u for u in resolved if (_int((wallets.get(u) or {}).get('balance'), 0) or 0) + amount < 0]
        if short:
            flash('Нельзя списать больше, чем на балансе: %s' % ', '.join(
                _str((users.get(u) or {}).get('name')) or u for u in short[:10]), 'error')
            return _back('coins_journal')
    admin_uid = _admin_uid()
    entry = {'at': now_utc(), 'by': _admin_email(), 'action': 'coins', 'details': '%+d: %s' % (amount, reason)}
    tx_ids = []
    try:
        for part in chunked(resolved, 200):   # до 2 записей на человека — лимит батча 500
            batch = db.batch()
            for uid in part:
                ref = db.collection('coin_transactions').document()
                tx_ids.append(ref.id)
                batch.set(ref, {'uid': uid, 'amount': amount, 'type': 'manual', 'reason': reason,
                                'createdBy': admin_uid, 'createdAt': SERVER_TIMESTAMP})
                if (users.get(uid) or {}).get('role') == 'applicant':
                    batch.set(db.collection('applicant_crm').document(uid), {'history': ArrayUnion([entry])},
                              merge=True)
            batch.commit()
    except Exception as e:
        invalidate_coins_cache('journal_stats')
        flash_error('Не удалось провести операцию. Часть начислений могла пройти — проверьте журнал', e)
        return _back('coins_journal')
    invalidate_coins_cache('journal_stats')
    cache.invalidate('applicants:snapshot')
    audit('award' if amount > 0 else 'deduct', 'coin_transactions', tx_ids[0] if len(tx_ids) == 1 else None,
          {'amount': amount, 'reason': reason, 'count': len(resolved), 'uids': resolved[:50]})
    if len(resolved) == 1:
        who = _str((users.get(resolved[0]) or {}).get('name')) or display_login_of(directory.get(resolved[0])) \
            or resolved[0]
    else:
        who = '%d %s' % (len(resolved), plural(len(resolved), 'абитуриенту', 'абитуриентам', 'абитуриентам'))
    flash('%s %d %s: %s. Баланс обновится через несколько секунд' % (
        'Начислено' if amount > 0 else 'Списано', abs(amount), coins_word(abs(amount)), who), 'success')
    return _back('coins_journal')
