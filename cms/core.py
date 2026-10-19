# cms/core.py — ядро веб-админки Caspian College.
#
# Здесь всё общее для модулей: инициализация Firebase (прод/эмулятор), кэш,
# коллектор маршрутов Routes, admin_required, CSRF, загрузка картинок в Storage
# с download-токенами, аудит, агрегаты count()/sum(), пагинация, время Алматы,
# Jinja-фильтры и бейджи сайдбара.
#
# Модули (cms/routes_*.py) импортируют отсюда то, что им нужно, и НЕ меняют этот
# файл (владелец — FOUNDATION). Совместимость: Python 3.10 (прод на PythonAnywhere).

import hmac
import io
import json
import logging
import os
import re
import secrets
import struct
import threading
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from functools import wraps
from urllib.parse import quote, unquote, urlencode, urlparse

import requests
from flask import (abort, current_app, flash, g, jsonify, redirect, render_template,
                   request, session, url_for)
from firebase_admin import auth, credentials, firestore, initialize_app, storage
import firebase_admin

try:  # FieldFilter есть в google-cloud-firestore >= 2.11
    from google.cloud.firestore_v1.base_query import FieldFilter
except Exception:  # pragma: no cover — старые версии библиотеки
    FieldFilter = None

log = logging.getLogger('cms')

# ==================== ПУТИ И ОКРУЖЕНИЕ ====================

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INSTANCE_DIR = os.environ.get('CMS_INSTANCE_DIR') or os.path.join(BASE_DIR, 'instance')

# Эмулятор Firebase включается переменной FIRESTORE_EMULATOR_HOST (её ставит
# `firebase emulators:exec`). В этом режиме ключ сервис-аккаунта не нужен.
IS_EMULATOR = bool(os.environ.get('FIRESTORE_EMULATOR_HOST'))
AUTH_EMULATOR_HOST = os.environ.get('FIREBASE_AUTH_EMULATOR_HOST', '').strip()
STORAGE_EMULATOR_HOST = (os.environ.get('FIREBASE_STORAGE_EMULATOR_HOST')
                         or os.environ.get('STORAGE_EMULATOR_HOST', '')).strip()

DEFAULT_PROJECT_ID = 'student-caspian-lol'
PROJECT_ID = (os.environ.get('CMS_PROJECT_ID') or os.environ.get('GCLOUD_PROJECT')
              or os.environ.get('GOOGLE_CLOUD_PROJECT')
              or ('demo-caspian' if IS_EMULATOR else DEFAULT_PROJECT_ID))
STORAGE_BUCKET = (os.environ.get('CMS_STORAGE_BUCKET')
                  or ('%s.appspot.com' % PROJECT_ID if IS_EMULATOR
                      else 'student-caspian-lol.firebasestorage.app'))

# Публичный Web API key клиента Firebase (не секрет: он же зашит в приложение).
DEFAULT_WEB_API_KEY = 'AIzaSyAQv2cWPt1GG70S8TKltfcX9GWKcuGano8'
WEB_API_KEY = os.environ.get('FIREBASE_WEB_API_KEY') or DEFAULT_WEB_API_KEY

# Строгий режим: ошибка импорта модуля роняет приложение (тесты, локальная отладка).
# В проде (по умолчанию) сломанный модуль логируется, остальные разделы работают.
STRICT_MODULES = os.environ.get('CMS_STRICT') == '1'


class _AnonymousCredential(credentials.Base):
    """Креды для эмулятора: Firestore/Storage-эмуляторы не проверяют подпись."""

    def get_credential(self):
        from google.auth.credentials import AnonymousCredentials
        return AnonymousCredentials()


def _make_credential():
    if IS_EMULATOR:
        return _AnonymousCredential()
    raw = (os.environ.get('FIREBASE_CREDENTIALS')
           or os.environ.get('GOOGLE_APPLICATION_CREDENTIALS') or '').strip()
    if raw.startswith('{'):
        # JSON ключа прямо в переменной окружения
        return credentials.Certificate(json.loads(raw))
    path = raw or os.path.join(BASE_DIR, 'firebase-service-account.json')
    return credentials.Certificate(path)


_firebase_lock = threading.Lock()


def _init_firebase():
    """Инициализировать приложение Firebase один раз на процесс (потокобезопасно)."""
    with _firebase_lock:
        if firebase_admin._apps:
            return firebase_admin.get_app()
        options = {'storageBucket': STORAGE_BUCKET, 'projectId': PROJECT_ID}
        return initialize_app(_make_credential(), options)


class _Lazy:
    """Ленивый прокси: Firebase инициализируется при первом обращении (или в init_app),
    а не при импорте — чистые модули (applicant_login, schedule_model) и их тесты
    импортируются без ключа и без сети."""

    __slots__ = ('_factory', '_target', '_lock')

    def __init__(self, factory):
        object.__setattr__(self, '_factory', factory)
        object.__setattr__(self, '_target', None)
        object.__setattr__(self, '_lock', threading.Lock())

    def _get(self):
        target = object.__getattribute__(self, '_target')
        if target is None:
            with object.__getattribute__(self, '_lock'):
                target = object.__getattribute__(self, '_target')
                if target is None:
                    target = object.__getattribute__(self, '_factory')()
                    object.__setattr__(self, '_target', target)
        return target

    def __getattr__(self, item):
        return getattr(self._get(), item)

    def __repr__(self):
        target = object.__getattribute__(self, '_target')
        return '<lazy %r>' % (target,) if target is not None else '<lazy (не инициализирован)>'


def _make_db():
    _init_firebase()
    return firestore.client()


def _make_bucket():
    _init_firebase()
    return storage.bucket(STORAGE_BUCKET)


db = _Lazy(_make_db)            # google.cloud.firestore.Client
bucket = _Lazy(_make_bucket)    # google.cloud.storage.Bucket
fb_auth = auth  # firebase_admin.auth (create_user, update_user, delete_user, revoke_refresh_tokens …)
SERVER_TIMESTAMP = firestore.SERVER_TIMESTAMP
Increment = firestore.Increment
ArrayUnion = firestore.ArrayUnion
ArrayRemove = firestore.ArrayRemove
DELETE_FIELD = firestore.DELETE_FIELD

# ==================== ВРЕМЯ (АЛМАТЫ, UTC+5) ====================

ALMATY = timezone(timedelta(hours=5), 'Asia/Almaty')


def now_utc():
    return datetime.now(timezone.utc)


def now_almaty():
    """Текущее время в Алматы (aware datetime, UTC+5)."""
    return datetime.now(ALMATY)


def today_almaty():
    """Сегодняшняя дата в Алматы (не по часам сервера — на PythonAnywhere там UTC)."""
    return now_almaty().date()


def to_datetime(value):
    """Timestamp / datetime / date / epoch (с или мс) / ISO-строка → aware datetime (UTC) или None.

    Наивный datetime считается UTC (так его писал старый код на сервере в UTC).
    """
    if value is None or value == '':
        return None
    try:
        if isinstance(value, datetime):
            return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        if isinstance(value, date):
            return datetime(value.year, value.month, value.day, tzinfo=ALMATY)
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            ts = float(value)
            if ts > 1e12:  # миллисекунды
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        if hasattr(value, 'to_datetime'):  # protobuf Timestamp
            return to_datetime(value.to_datetime())
        if isinstance(value, str):
            s = value.strip()
            if not s:
                return None
            if s.endswith('Z'):
                s = s[:-1] + '+00:00'
            try:
                dt = datetime.fromisoformat(s)
            except ValueError:
                return None
            return dt if dt.tzinfo else dt.replace(tzinfo=ALMATY)
    except Exception:
        return None
    return None


def to_almaty(value):
    """Любое значение времени → aware datetime в поясе Алматы (или None)."""
    dt = to_datetime(value)
    return dt.astimezone(ALMATY) if dt else None


def fmt_dt(value, fmt='%d.%m.%Y %H:%M', default='—'):
    """Формат времени для показа (по Алматы). Пустое/непонятное → default."""
    dt = to_almaty(value)
    return dt.strftime(fmt) if dt else default


def parse_local_datetime(value):
    """Поле <input type=datetime-local> ('YYYY-MM-DDTHH:MM') → aware UTC datetime (время ввода = Алматы)."""
    if not value:
        return None
    try:
        dt = datetime.strptime(str(value).strip()[:16], '%Y-%m-%dT%H:%M')
    except ValueError:
        try:
            dt = datetime.strptime(str(value).strip()[:10], '%Y-%m-%d')
        except ValueError:
            return None
    return dt.replace(tzinfo=ALMATY).astimezone(timezone.utc)


def local_input_value(value):
    """Время → значение для <input type=datetime-local> по Алматы ('' если пусто)."""
    dt = to_almaty(value)
    return dt.strftime('%Y-%m-%dT%H:%M') if dt else ''


def humanize_ago(value, now=None):
    """'только что', '5 мин назад', '3 ч назад', 'вчера', '12.03.2026'."""
    dt = to_datetime(value)
    if not dt:
        return '—'
    now = now or now_utc()
    sec = (now - dt).total_seconds()
    if sec < 0:
        return fmt_dt(dt)
    if sec < 60:
        return 'только что'
    if sec < 3600:
        return '%d мин назад' % (sec // 60)
    if sec < 86400:
        return '%d ч назад' % (sec // 3600)
    if sec < 2 * 86400:
        return 'вчера'
    if sec < 7 * 86400:
        return '%d %s назад' % (sec // 86400, plural(int(sec // 86400), 'день', 'дня', 'дней'))
    return fmt_dt(dt, '%d.%m.%Y')


def plural(n, one, few, many):
    """Русское склонение: plural(5, 'голос', 'голоса', 'голосов') → 'голосов'."""
    try:
        n = abs(int(n))
    except (TypeError, ValueError):
        return many
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def fmt_num(value, default='—'):
    """12345 → '12 345' (неразрывный пробел)."""
    if value is None or value == '':
        return default
    try:
        if isinstance(value, float) and not value.is_integer():
            return ('{:,.1f}'.format(value)).replace(',', ' ')
        return ('{:,}'.format(int(value))).replace(',', ' ')
    except (TypeError, ValueError):
        return str(value)


def fmt_pct(part, total, digits=0):
    """Процент part/total строкой ('—' при total=0)."""
    try:
        if not total:
            return '—'
        return ('{:.%df}%%' % digits).format(100.0 * float(part) / float(total))
    except (TypeError, ValueError):
        return '—'


def fmt_phone(value):
    """'+77011234567' / '8 701 …' → '+7 701 123 45 67'; иначе как есть."""
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) != 11 or digits[0] != '7':
        return str(value or '')
    d = digits[1:]
    return '+7 %s %s %s %s' % (d[0:3], d[3:6], d[6:8], d[8:10])


def safe_url(url, default='#'):
    """Только http(s) и относительные ссылки (защита от javascript: в href)."""
    s = str(url or '').strip()
    if not s:
        return default
    if s.startswith('/') and not s.startswith('//'):
        return s
    scheme = urlparse(s).scheme.lower()
    return s if scheme in ('http', 'https', 'mailto', 'tel') else default


def json_safe(value, _depth=0):
    """Значение Firestore → JSON-совместимое (datetime → ISO, ссылки → путь, GeoPoint → dict)."""
    if _depth > 6:
        return None
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return to_datetime(value).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(v, _depth + 1) for v in value]
    if isinstance(value, bytes):
        return '<%d bytes>' % len(value)
    if hasattr(value, 'path') and hasattr(value, 'id'):  # DocumentReference
        return getattr(value, 'path', str(value))
    if hasattr(value, 'latitude') and hasattr(value, 'longitude'):
        return {'lat': value.latitude, 'lng': value.longitude}
    return str(value)


def doc_dict(snapshot):
    """DocumentSnapshot → {'id': …, **data} (None, если документа нет)."""
    if snapshot is None or not getattr(snapshot, 'exists', False):
        return None
    data = snapshot.to_dict() or {}
    data['id'] = snapshot.id
    return data


# ==================== КЭШ ====================

_MISSING = object()


class CacheManager:
    """Потокобезопасный TTL-кэш в памяти процесса с межпроцессной инвалидацией.

    На PythonAnywhere у каждого веб-воркера своя память, поэтому invalidate()
    дополнительно «штампует» файл instance/cache/<namespace>.gen: другие воркеры
    увидят штамп (проверка не чаще раза в секунду) и выбросят свои записи этого
    пространства имён. Пространство имён — часть ключа до ':' ('schedule:snapshot'
    → 'schedule'). Пустые значения ([], {}, 0, None) кэшируются как обычные.
    """

    GEN_CHECK_INTERVAL = 1.0

    def __init__(self, ttl_seconds=300, shared_dir=None):
        self.ttl = ttl_seconds
        self._data = {}  # key -> (value, set_at, expires_at)
        self._lock = threading.Lock()
        self._shared_dir = shared_dir
        self._gen_cache = {}  # namespace -> (checked_at, gen)
        self._shared_ok = shared_dir is not None

    @staticmethod
    def namespace(key):
        return str(key).split(':', 1)[0]

    # --- межпроцессные штампы ---
    def _gen_path(self, ns):
        safe = re.sub(r'[^A-Za-z0-9_.-]', '_', ns) or '_'
        return os.path.join(self._shared_dir, safe + '.gen')

    def _read_gen(self, ns):
        if not self._shared_ok:
            return 0.0
        now = time.time()
        cached = self._gen_cache.get(ns)
        if cached and now - cached[0] < self.GEN_CHECK_INTERVAL:
            return cached[1]
        gen = 0.0
        try:
            with open(self._gen_path(ns), 'r') as fh:
                gen = float(fh.read().strip() or 0)
        except FileNotFoundError:
            gen = 0.0
        except (OSError, ValueError):
            gen = 0.0
        self._gen_cache[ns] = (now, gen)
        return gen

    def _bump(self, ns):
        if not self._shared_ok:
            return
        stamp = time.time()
        try:
            os.makedirs(self._shared_dir, exist_ok=True)
            tmp = self._gen_path(ns) + '.%d.%d.tmp' % (os.getpid(), threading.get_ident())
            with open(tmp, 'w') as fh:
                fh.write(repr(stamp))
            os.replace(tmp, self._gen_path(ns))
            self._gen_cache[ns] = (time.time(), stamp)
        except OSError as e:  # каталог недоступен — работаем без межпроцессной части
            self._shared_ok = False
            log.warning('Кэш: межпроцессная инвалидация отключена (%s)', e)

    # --- API ---
    def get(self, key, default=None):
        """Значение или default (None), если нет/устарело/инвалидировано другим воркером."""
        now = time.time()
        with self._lock:
            item = self._data.get(key)
        if item is None:
            return default
        value, set_at, expires_at = item
        if now >= expires_at or set_at < max(self._read_gen(self.namespace(key)), self._read_gen('_all')):
            with self._lock:
                if self._data.get(key) is item:
                    del self._data[key]
            return default
        return value

    def set(self, key, value, ttl=None):
        now = time.time()
        with self._lock:
            self._data[key] = (value, now, now + (self.ttl if ttl is None else ttl))
        return value

    def get_or_set(self, key, factory, ttl=None):
        """Вернуть закэшированное или посчитать factory() и закэшировать (включая None/пустое)."""
        value = self.get(key, _MISSING)
        if value is not _MISSING:
            return value
        return self.set(key, factory(), ttl)

    def invalidate(self, key=None):
        """Сбросить ключ (и его пространство имён в других воркерах) или весь кэш (key=None)."""
        with self._lock:
            if key is None:
                self._data.clear()
            else:
                self._data.pop(key, None)
                for k in [k for k in self._data if str(k).startswith(str(key) + ':')]:
                    del self._data[k]
        self._bump('_all' if key is None else self.namespace(key))

    def invalidate_prefix(self, prefix):
        """Сбросить все ключи, начинающиеся с prefix ('schedule:' → всё расписание)."""
        with self._lock:
            for k in [k for k in self._data if str(k).startswith(prefix)]:
                del self._data[k]
        self._bump(self.namespace(prefix))


cache = CacheManager(ttl_seconds=300, shared_dir=os.path.join(INSTANCE_DIR, 'cache'))


# ==================== КОЛЛЕКТОР МАРШРУТОВ ====================

class Routes:
    """Отложенная регистрация маршрутов без Blueprint — имена endpoint'ов остаются
    «плоскими» (url_for('news'), а не url_for('content.news')).

        routes = Routes()

        @routes.route('/news')
        @admin_required
        def news(): ...

    cms/__init__.py вызывает routes.register(app) для каждого модуля.
    """

    def __init__(self):
        self._rules = []
        self._filters = []
        self._globals = []

    def route(self, rule, **options):
        def decorator(f):
            opts = dict(options)
            endpoint = opts.pop('endpoint', None) or f.__name__
            self._rules.append((rule, endpoint, f, opts))
            return f
        return decorator

    def get(self, rule, **options):
        return self.route(rule, methods=['GET'], **options)

    def post(self, rule, **options):
        return self.route(rule, methods=['POST'], **options)

    def template_filter(self, name=None):
        def decorator(f):
            self._filters.append((name or f.__name__, f))
            return f
        return decorator

    def template_global(self, name=None):
        def decorator(f):
            self._globals.append((name or f.__name__, f))
            return f
        return decorator

    def register(self, app):
        for rule, endpoint, f, opts in self._rules:
            app.add_url_rule(rule, endpoint=endpoint, view_func=f, **opts)
        for name, f in self._filters:
            app.add_template_filter(f, name)
        for name, f in self._globals:
            app.add_template_global(f, name)

    @property
    def endpoints(self):
        return sorted({e for _, e, _, _ in self._rules})


def csrf_exempt(f):
    """Отключить CSRF-проверку для view (только для вебхуков/API с другой защитой)."""
    f._csrf_exempt = True
    return f


# ==================== АВТОРИЗАЦИЯ АДМИНА ====================

ROLE_RECHECK_SECONDS = 600       # роль перечитывается из users/{uid} раз в 10 минут
ROLE_OUTAGE_GRACE_SECONDS = 3600  # если Firestore недоступен — доверяем проверке не старше часа
SESSION_HOURS = 12


def wants_json():
    """Запрос ждёт JSON (API, fetch с Accept: application/json, XHR)."""
    if request.path.startswith('/api/'):
        return True
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return True
    best = request.accept_mimetypes.best_match(['application/json', 'text/html'])
    return best == 'application/json' and request.accept_mimetypes[best] > request.accept_mimetypes['text/html']


def _login_redirect():
    nxt = None
    if request.method == 'GET':
        nxt = request.full_path.rstrip('?') if request.query_string else request.path
    return redirect(url_for('login', next=nxt) if nxt and nxt != '/' else url_for('login'))


def _check_admin_role(uid):
    """True/False — роль admin; None — Firestore недоступен."""
    try:
        snap = db.collection('users').document(uid).get()
    except Exception:
        log.exception('Не удалось перепроверить роль админа %s', uid)
        return None
    data = snap.to_dict() if snap.exists else None
    if not data or data.get('role') != 'admin':
        return False
    session['admin_name'] = (data.get('name') or '')[:120]
    return True


def admin_required(f):
    """Только для вошедшего админа. Роль перепроверяется раз в ROLE_RECHECK_SECONDS:
    разжалованный админ теряет доступ максимум через 10 минут."""

    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = session.get('uid')
        if not uid:
            if wants_json():
                return jsonify({'ok': False, 'success': False, 'error': 'unauthorized'}), 401
            return _login_redirect()
        checked_at = float(session.get('role_checked_at') or 0)
        age = time.time() - checked_at
        if not session.get('is_admin') or age > ROLE_RECHECK_SECONDS:
            ok = _check_admin_role(uid)
            if ok is None:
                if not (session.get('is_admin') and age < ROLE_OUTAGE_GRACE_SECONDS):
                    abort(503)
            elif not ok:
                session.clear()
                abort(403)
            else:
                session['is_admin'] = True
                session['role_checked_at'] = time.time()
        g.admin = current_admin()
        return f(*args, **kwargs)

    return wrapper


def current_admin():
    """{'uid', 'email', 'name'} текущего админа (или None)."""
    uid = session.get('uid')
    if not uid:
        return None
    return {'uid': uid, 'email': session.get('email') or '', 'name': session.get('admin_name') or ''}


class LoginError(Exception):
    """Понятное пользователю сообщение об ошибке входа (str(e) можно показывать)."""


_http = requests.Session()

_LOGIN_ERRORS = {
    'EMAIL_NOT_FOUND': 'Неверный email или пароль',
    'INVALID_PASSWORD': 'Неверный email или пароль',
    'INVALID_LOGIN_CREDENTIALS': 'Неверный email или пароль',
    'INVALID_EMAIL': 'Некорректный email',
    'USER_DISABLED': 'Аккаунт отключён. Обратитесь к администратору',
    'TOO_MANY_ATTEMPTS_TRY_LATER': 'Слишком много попыток. Попробуйте через несколько минут',
    'MISSING_PASSWORD': 'Введите пароль',
}


def identitytoolkit_url(method):
    """URL REST Identity Toolkit (с учётом эмулятора Auth)."""
    if AUTH_EMULATOR_HOST:
        return 'http://%s/identitytoolkit.googleapis.com/v1/accounts:%s?key=%s' % (
            AUTH_EMULATOR_HOST, method, WEB_API_KEY)
    return 'https://identitytoolkit.googleapis.com/v1/accounts:%s?key=%s' % (method, WEB_API_KEY)


def sign_in_with_password(email, password):
    """Проверка email+пароля через Firebase Auth REST. → decoded id-token (dict с 'uid').

    Бросает LoginError с текстом для пользователя."""
    try:
        resp = _http.post(identitytoolkit_url('signInWithPassword'),
                          json={'email': email, 'password': password, 'returnSecureToken': True},
                          timeout=10)
        data = resp.json()
    except requests.RequestException:
        log.exception('Firebase Auth недоступен')
        raise LoginError('Сервис входа недоступен. Попробуйте позже')
    except ValueError:
        raise LoginError('Сервис входа вернул неожиданный ответ. Попробуйте позже')
    if 'error' in data:
        code = str((data.get('error') or {}).get('message') or '')
        key = code.split(' ')[0].split(':')[0]
        raise LoginError(_LOGIN_ERRORS.get(key, 'Не удалось войти. Проверьте email и пароль'))
    try:
        return auth.verify_id_token(data['idToken'], clock_skew_seconds=20)
    except Exception:
        log.exception('verify_id_token не прошёл')
        raise LoginError('Не удалось подтвердить вход. Попробуйте ещё раз')


class RateLimiter:
    """Простой лимит попыток в памяти процесса: не больше limit событий за window секунд на ключ."""

    def __init__(self, limit, window):
        self.limit = limit
        self.window = window
        self._hits = {}
        self._lock = threading.Lock()

    def _prune(self, key, now):
        hits = [t for t in self._hits.get(key, []) if now - t < self.window]
        if hits:
            self._hits[key] = hits
        else:
            self._hits.pop(key, None)
        return hits

    def blocked(self, key):
        with self._lock:
            return len(self._prune(key, time.time())) >= self.limit

    def hit(self, key):
        """Зафиксировать событие. → True, если лимит ещё не превышен."""
        now = time.time()
        with self._lock:
            hits = self._prune(key, now)
            hits.append(now)
            self._hits[key] = hits
            if len(self._hits) > 10000:  # защита памяти
                for k in list(self._hits)[:5000]:
                    self._hits.pop(k, None)
            return len(hits) <= self.limit

    def reset(self, key):
        with self._lock:
            self._hits.pop(key, None)


def client_ip():
    """IP клиента (PythonAnywhere ставит X-Real-IP / X-Forwarded-For)."""
    fwd = request.headers.get('X-Forwarded-For', '')
    if fwd:
        return fwd.split(',')[0].strip()
    return request.headers.get('X-Real-IP') or request.remote_addr or '?'


def safe_next_url(target, default=None):
    """Только относительный путь этого сайта (защита от open redirect)."""
    s = str(target or '').strip()
    if s.startswith('/') and not s.startswith('//') and not s.startswith('/\\'):
        return s
    return default


def redirect_back(default_endpoint='dashboard', **values):
    """Назад на Referer (если он с этого сайта), иначе url_for(default_endpoint, **values)."""
    ref = request.referrer or ''
    if ref:
        p = urlparse(ref)
        if p.netloc == request.host:
            path = p.path + ('?' + p.query if p.query else '')
            if safe_next_url(path):
                return redirect(path)
    return redirect(url_for(default_endpoint, **values))


# ==================== CSRF ====================

CSRF_FIELD = 'csrf_token'
CSRF_HEADERS = ('X-CSRF-Token', 'X-CSRFToken')


def csrf_token():
    """Токен CSRF текущей сессии (создаётся при первом обращении). В Jinja: {{ csrf_token() }}."""
    tok = session.get('_csrf')
    if not tok:
        tok = secrets.token_urlsafe(32)
        session['_csrf'] = tok
    return tok


def csrf_valid():
    expected = session.get('_csrf')
    sent = request.form.get(CSRF_FIELD) if request.form else None
    if not sent:
        for h in CSRF_HEADERS:
            sent = request.headers.get(h)
            if sent:
                break
    return bool(expected and sent and hmac.compare_digest(str(sent), str(expected)))


def _csrf_protect():
    if request.method in ('GET', 'HEAD', 'OPTIONS', 'TRACE'):
        return None
    if current_app.config.get('CSRF_DISABLED'):
        return None
    view = current_app.view_functions.get(request.endpoint)
    if view is not None and getattr(view, '_csrf_exempt', False):
        return None
    if not csrf_valid():
        log.warning('CSRF: отклонён %s %s', request.method, request.path)
        abort(400, description='csrf')
    return None


# ==================== ЗАГРУЗКА КАРТИНОК В STORAGE ====================

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif', 'webp'}  # для старого allowed_file()
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 10
IMAGE_CACHE_CONTROL = 'public, max-age=604800'
_IMAGE_TYPES = {  # тип → (расширение, content-type)
    'jpeg': ('.jpg', 'image/jpeg'),
    'png': ('.png', 'image/png'),
    'gif': ('.gif', 'image/gif'),
    'webp': ('.webp', 'image/webp'),
}


class UploadError(ValueError):
    """Ошибка загрузки с понятным текстом (str(e) можно показать во flash)."""


def allowed_file(filename):
    """Старый помощник: допустимо ли расширение файла картинки."""
    return '.' in (filename or '') and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def sniff_image_type(data):
    """Тип картинки по сигнатуре: 'jpeg' | 'png' | 'gif' | 'webp' | None."""
    head = data[:16]
    if head.startswith(b'\xff\xd8\xff'):
        return 'jpeg'
    if head.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'png'
    if head[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'
    if head[:4] == b'RIFF' and head[8:12] == b'WEBP':
        return 'webp'
    return None


def image_size(data):
    """(w, h) картинки по заголовку без Pillow (JPEG/PNG/GIF/WEBP); (0, 0), если не удалось."""
    try:
        kind = sniff_image_type(data)
        if kind == 'png' and len(data) >= 24:
            return struct.unpack('>II', data[16:24])
        if kind == 'gif' and len(data) >= 10:
            return struct.unpack('<HH', data[6:10])
        if kind == 'webp' and len(data) >= 30:
            chunk = data[12:16]
            if chunk == b'VP8 ':
                w, h = struct.unpack('<HH', data[26:30])
                return w & 0x3FFF, h & 0x3FFF
            if chunk == b'VP8L':
                b = data[21:25]
                w = 1 + (((b[1] & 0x3F) << 8) | b[0])
                h = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
                return w, h
            if chunk == b'VP8X':
                w = 1 + int.from_bytes(data[24:27], 'little')
                h = 1 + int.from_bytes(data[27:30], 'little')
                return w, h
        if kind == 'jpeg':
            i = 2
            n = len(data)
            while i + 9 < n:
                if data[i] != 0xFF:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    i += 2
                    continue
                seg_len = struct.unpack('>H', data[i + 2:i + 4])[0]
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    h, w = struct.unpack('>HH', data[i + 5:i + 9])
                    return w, h
                i += 2 + seg_len
    except Exception:
        pass
    try:  # Pillow — необязательно (зависимостью не является)
        from PIL import Image  # noqa
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return 0, 0


def download_url(path, token):
    """Download-URL Firebase Storage с токеном (как getDownloadURL в приложении)."""
    if STORAGE_EMULATOR_HOST:
        host = STORAGE_EMULATOR_HOST
        base = host if host.startswith('http') else 'http://' + host
    else:
        base = 'https://firebasestorage.googleapis.com'
    return '%s/v0/b/%s/o/%s?alt=media&token=%s' % (base, bucket.name, quote(path, safe=''), token)


def storage_path_from_url(url):
    """Путь объекта в НАШЕМ бакете по URL (токен-URL, storage.googleapis.com, подписанный) или None."""
    s = str(url or '').strip()
    if not s:
        return None
    p = urlparse(s)
    m = re.match(r'^/v0/b/([^/]+)/o/(.+)$', p.path)
    if m:
        return unquote(m.group(2)) if m.group(1) == bucket.name else None
    if p.netloc in ('storage.googleapis.com', 'storage.cloud.google.com'):
        parts = p.path.lstrip('/').split('/', 1)
        if len(parts) == 2 and parts[0] == bucket.name:
            return unquote(parts[1])
        return None
    m = re.match(r'^([^.]+\.[^.]+.*)\.storage\.googleapis\.com$', p.netloc)
    if m and m.group(1) == bucket.name:
        return unquote(p.path.lstrip('/'))
    return None


def upload_image(file_storage, folder, index=0):
    """Загрузить один файл-картинку (werkzeug FileStorage) в Storage.

    → {'url', 'path', 'w', 'h'}; путь `<folder>/<stamp>_<index><ext>`. Проверяет тип
    по сигнатуре (jpg/png/webp/gif) и размер (≤ 10 МБ). Бросает UploadError.
    """
    name = getattr(file_storage, 'filename', '') or 'файл'
    data = file_storage.read(MAX_IMAGE_BYTES + 1)
    if not data:
        raise UploadError('Файл «%s» пустой' % name)
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError('Файл «%s» больше 10 МБ' % name)
    kind = sniff_image_type(data)
    if kind is None:
        raise UploadError('Файл «%s» — не картинка (нужен JPG, PNG, WEBP или GIF)' % name)
    ext, content_type = _IMAGE_TYPES[kind]
    stamp = int(time.time() * 1000)
    path = '%s/%d_%d%s' % (str(folder).strip('/'), stamp, int(index), ext)
    token = str(uuid.uuid4())
    blob = bucket.blob(path)
    blob.metadata = {'firebaseStorageDownloadTokens': token}
    blob.cache_control = IMAGE_CACHE_CONTROL
    try:
        blob.upload_from_string(data, content_type=content_type)
    except Exception:
        log.exception('Загрузка в Storage не удалась: %s', path)
        raise UploadError('Не удалось загрузить «%s». Попробуйте ещё раз' % name)
    w, h = image_size(data)
    return {'url': download_url(path, token), 'path': path, 'w': int(w or 0), 'h': int(h or 0)}


def upload_images(files, folder, max_count=MAX_IMAGES):
    """Загрузить несколько файлов → [{url, path, w, h}] в том же порядке.

    Пустые поля формы пропускаются. Если файлов больше max_count — UploadError.
    При ошибке посередине уже загруженные файлы удаляются (всё или ничего)."""
    files = [f for f in (files or []) if f is not None and getattr(f, 'filename', '')]
    if len(files) > max_count:
        raise UploadError('Можно загрузить не больше %d фото' % max_count)
    done = []
    try:
        for i, f in enumerate(files):
            done.append(upload_image(f, folder, i))
    except Exception:
        delete_storage_paths([d['path'] for d in done])
        raise
    return done


def delete_storage_paths(paths):
    """Удалить объекты Storage по путям (или URL нашего бакета). Ошибки логируются. → сколько удалено."""
    deleted = 0
    for raw in paths or []:
        path = raw
        if isinstance(raw, dict):
            path = raw.get('path') or storage_path_from_url(raw.get('url'))
        elif isinstance(raw, str) and '://' in raw:
            path = storage_path_from_url(raw)
        if not path:
            continue
        try:
            bucket.blob(path).delete()
            deleted += 1
        except Exception as e:
            if e.__class__.__name__ == 'NotFound':
                continue
            log.warning('Не удалось удалить %s из Storage: %s', path, e)
    return deleted


def _as_int(v):
    if isinstance(v, bool):
        return 0
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(round(v))
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return 0


def images_of(doc, max_count=MAX_IMAGES):
    """Фото документа как NewsMedia.imagesOf (APP): поле images[] (url/path/w/h или строки),
    иначе старое imageUrl. Пустые и повторяющиеся URL отбрасываются, не больше max_count.
    → [{'url', 'path', 'w', 'h'}] (path '' и w/h 0, если неизвестны)."""
    if doc is None:
        return []
    data = doc if isinstance(doc, dict) else (doc.to_dict() or {})
    out, seen = [], set()
    raw = data.get('images')
    if isinstance(raw, list):
        for it in raw:
            if isinstance(it, str):
                url = it.strip()
                item = {'url': url, 'path': '', 'w': 0, 'h': 0}
            elif isinstance(it, dict):
                url = str(it.get('url') or '').strip()
                item = {'url': url, 'path': str(it.get('path') or ''),
                        'w': _as_int(it.get('w')), 'h': _as_int(it.get('h'))}
            else:
                continue
            if url and url not in seen:
                seen.add(url)
                out.append(item)
            if len(out) >= max_count:
                break
    if not out:
        legacy = str(data.get('imageUrl') or '').strip()
        if legacy:
            out.append({'url': legacy, 'path': '', 'w': 0, 'h': 0})
    return out


def image_fields(images):
    """Поля документа для набора фото: {'images': [...], 'imageUrl': обложка или None}."""
    clean = []
    for im in images or []:
        item = {'url': im['url'], 'path': im.get('path') or '',
                'w': _as_int(im.get('w')), 'h': _as_int(im.get('h'))}
        clean.append(item)
    return {'images': clean, 'imageUrl': clean[0]['url'] if clean else None}


class UploaderResult:
    """Итог формы image_uploader: images — итоговый список по порядку; removed_paths —
    файлы старых фото, убранных пользователем (удалить ПОСЛЕ успешной записи документа);
    uploaded_paths — новые файлы (удалить, если запись документа не удалась)."""

    def __init__(self, images, removed, uploaded):
        self.images = images
        self.removed = removed
        self.removed_paths = [r['path'] for r in removed if r.get('path')]
        self.uploaded_paths = [u['path'] for u in uploaded if u.get('path')]

    @property
    def changed(self):
        return bool(self.removed or self.uploaded_paths)


def images_from_uploader(name, existing, folder, max_count=MAX_IMAGES):
    """Разобрать поле макроса ui.image_uploader(name, existing) и загрузить новые файлы.

    Форма присылает файлы в request.files[name] и порядок в request.form[name + '__state']
    (JSON: [{"kind":"existing","url":…}, {"kind":"new","idx":0}, …]). Старые фото
    принимаются только из existing (подделать чужой URL нельзя). Без JS (нет state):
    все старые + новые в конце. → UploaderResult. Бросает UploadError.
    """
    existing = images_of({'images': list(existing or [])})
    by_url = {im['url']: im for im in existing}
    new_files = [f for f in request.files.getlist(name)]
    raw_state = request.form.get(name + '__state')
    plan = []
    if raw_state:
        try:
            state = json.loads(raw_state)
        except ValueError:
            state = None
        if isinstance(state, list):
            for it in state:
                if not isinstance(it, dict):
                    continue
                if it.get('kind') == 'existing' and it.get('url') in by_url:
                    plan.append(('existing', by_url[it['url']]))
                elif it.get('kind') == 'new':
                    idx = _as_int(it.get('idx'))
                    if 0 <= idx < len(new_files) and getattr(new_files[idx], 'filename', ''):
                        plan.append(('new', new_files[idx]))
        else:
            raw_state = None
    if not raw_state:
        plan = [('existing', im) for im in existing]
        plan += [('new', f) for f in new_files if getattr(f, 'filename', '')]
    # дубли старых фото убираем
    seen, dedup = set(), []
    for kind, item in plan:
        key = item['url'] if kind == 'existing' else id(item)
        if key in seen:
            continue
        seen.add(key)
        dedup.append((kind, item))
    if len(dedup) > max_count:
        raise UploadError('Можно прикрепить не больше %d фото' % max_count)
    uploaded, result = [], []
    try:
        for i, (kind, item) in enumerate(dedup):
            if kind == 'existing':
                result.append(item)
            else:
                up = upload_image(item, folder, i)
                uploaded.append(up)
                result.append(up)
    except Exception:
        delete_storage_paths([u['path'] for u in uploaded])
        raise
    keep = {im['url'] for im in result}
    removed = [im for im in existing if im['url'] not in keep]
    return UploaderResult(result, removed, uploaded)


# ==================== АУДИТ, АГРЕГАТЫ, ПАГИНАЦИЯ ====================

def audit(action, entity, entity_id=None, details=None):
    """Запись в журнал cms_audit: {at, by, byUid, action, entity, entityId, details}.

    Никогда не бросает исключений (журнал не должен ломать основное действие)."""
    try:
        doc = {
            'at': SERVER_TIMESTAMP,
            'by': session.get('email') or None,
            'byUid': session.get('uid') or None,
            'action': str(action)[:64],
            'entity': str(entity)[:64],
            'entityId': (str(entity_id)[:200] if entity_id is not None else None),
            'details': _truncate(json_safe(details)) if details is not None else None,
        }
        db.collection('cms_audit').add(doc)
        cache.invalidate_prefix('audit:')
    except Exception:
        log.exception('Не удалось записать аудит %s/%s', entity, action)


def _truncate(value, _depth=0):
    if isinstance(value, str):
        return value if len(value) <= 500 else value[:500] + '…'
    if isinstance(value, list):
        return [_truncate(v, _depth + 1) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _truncate(v, _depth + 1) for k, v in list(value.items())[:50]}
    return value


def where(query, field, op, value):
    """query.where(...) без DeprecationWarning (FieldFilter, если доступен)."""
    if FieldFilter is not None:
        return query.where(filter=FieldFilter(field, op, value))
    return query.where(field, op, value)


def count(query, default=None):
    """Число документов запроса через aggregation count() (1 чтение на 1000 документов).

    При ошибке (нет индекса, сеть) — лог + default."""
    try:
        res = query.count(alias='n').get()
        return int(res[0][0].value)
    except Exception:
        log.exception('count() не удался')
        return default


def aggregate_sum(query, field, default=None):
    """Сумма числового поля через aggregation sum()."""
    try:
        res = query.sum(field, alias='s').get()
        v = res[0][0].value
        return v if v is not None else 0
    except Exception:
        log.exception('sum(%s) не удался', field)
        return default


class Page:
    """Страница курсорной пагинации Firestore.

    items — DocumentSnapshot'ы; next_cursor — id последнего документа (для ?cursor=), если
    есть следующая страница; cursor — текущий курсор; number — номер страницы (для показа)."""

    def __init__(self, items, next_cursor, cursor, page_size, number=1):
        self.items = items
        self.next_cursor = next_cursor
        self.cursor = cursor
        self.page_size = page_size
        self.number = number

    @property
    def has_next(self):
        return bool(self.next_cursor)

    @property
    def has_prev(self):
        return bool(self.cursor)

    def dicts(self):
        return [doc_dict(d) for d in self.items]

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


def paginate(query, page_size=25, cursor=None, collection=None):
    """Курсорная пагинация: query должен иметь order_by. cursor — id документа, после
    которого начинать (из Page.next_cursor). Номер страницы берётся из ?p= (для показа).

        page = paginate(db.collection('news').order_by('createdAt', direction='DESCENDING'),
                        25, request.args.get('cursor'))
    """
    page_size = max(1, min(int(page_size or 25), 500))
    q = query
    if cursor:
        coll = collection
        if coll is None:
            coll = query if hasattr(query, 'document') else getattr(query, '_parent', None)
        try:
            snap = coll.document(str(cursor)).get() if coll is not None else None
            if snap is not None and snap.exists:
                q = q.start_after(snap)
        except Exception:
            log.warning('paginate: курсор %s не применён', cursor)
    docs = list(q.limit(page_size + 1).stream())
    has_next = len(docs) > page_size
    docs = docs[:page_size]
    number = 1
    try:
        number = max(1, int(request.args.get('p', 1))) if cursor else 1
    except (RuntimeError, ValueError, TypeError):
        number = 1
    return Page(docs, docs[-1].id if has_next and docs else None, cursor, page_size, number)


class ListPage:
    """Страница обычного списка (для данных из кэша/снапшота): items, page, pages, total."""

    def __init__(self, items, page, pages, total, per_page):
        self.items = items
        self.page = page
        self.pages = pages
        self.total = total
        self.per_page = per_page

    @property
    def has_prev(self):
        return self.page > 1

    @property
    def has_next(self):
        return self.page < self.pages

    def __iter__(self):
        return iter(self.items)

    def __len__(self):
        return len(self.items)


def paginate_list(items, page=1, per_page=25):
    """Нумерованная пагинация готового списка. page берётся как есть (1..pages)."""
    items = list(items or [])
    per_page = max(1, int(per_page or 25))
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    try:
        page = int(page or 1)
    except (TypeError, ValueError):
        page = 1
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return ListPage(items[start:start + per_page], page, pages, total, per_page)


def chunked(seq, size=450):
    """Порции для batch (лимит Firestore — 500 операций)."""
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def get_docs(collection, ids, field_paths=None):
    """Пакетное чтение документов по id (db.get_all вместо N+1). → {id: dict}."""
    ids = [str(i) for i in dict.fromkeys(ids or []) if i]
    out = {}
    if not ids:
        return out
    coll = db.collection(collection)
    for part in chunked(ids, 300):
        refs = [coll.document(i) for i in part]
        try:
            for snap in db.get_all(refs, field_paths=field_paths):
                if snap.exists:
                    out[snap.id] = snap.to_dict() or {}
        except Exception:
            log.exception('get_all(%s) не удался', collection)
    return out


# ==================== URL-ПОМОЩНИКИ ДЛЯ ШАБЛОНОВ ====================

def url_with(**changes):
    """Текущий URL с изменёнными query-параметрами (None/'' — удалить параметр).

    {{ url_with(page=2) }}, {{ url_with(status='new', cursor=None) }}"""
    args = request.args.to_dict(flat=False)
    for k, v in changes.items():
        if v is None or v == '':
            args.pop(k, None)
        elif isinstance(v, (list, tuple)):
            args[k] = [str(x) for x in v]
        else:
            args[k] = [str(v)]
    qs = urlencode([(k, x) for k, vals in args.items() for x in vals])
    return request.path + ('?' + qs if qs else '')


def nav_url(endpoint, **values):
    """url_for, который не роняет страницу, если endpoint ещё не зарегистрирован ('#')."""
    try:
        return url_for(endpoint, **values)
    except Exception:
        return '#'


def public_lang():
    """Язык публичных страниц: ?lang=ru|kk, иначе cookie cc_lang, иначе 'ru'."""
    lang = (request.args.get('lang') or request.cookies.get('cc_lang') or 'ru').lower()
    return lang if lang in ('ru', 'kk') else 'ru'


# ==================== НАВИГАЦИЯ (САЙДБАР) ====================

# Разделы сайдбара: (заголовок группы, иконка, [(endpoint, подпись, иконка, ключевые слова)]).
# Порядок и состав — по SPEC §6.3. Бейджи: функции nav_badges() модулей → {endpoint: число}.
NAV_SECTIONS = [
    ('Главная', 'house', [
        ('dashboard', 'Обзор', 'speedometer2', 'дашборд главная kpi'),
    ]),
    ('Абитуриенты', 'mortarboard', [
        ('applicants_dashboard', 'Аналитика', 'graph-up-arrow', 'абитуриенты статистика воронка источники'),
        ('applicants_leads', 'Лиды', 'person-lines-fill', 'абитуриенты crm заявки лиды'),
        ('applicant_sources', 'QR-источники', 'qr-code', 'qr стенд источник ссылка'),
        ('applicant_news', 'Новости абитуриентов', 'megaphone', 'новости абитуриент'),
        ('admissions_settings', 'Приёмная', 'building-gear', 'приёмная настройки специальности документы faq дедлайн'),
    ]),
    ('Caspian Coin', 'coin', [
        ('coins_shop', 'Магазин', 'bag', 'магазин мерч товары коины'),
        ('coins_orders', 'Заказы', 'box-seam', 'заказы выдача мерч'),
        ('coins_campaigns', 'Кампании', 'ticket-perforated', 'коды кампании коины'),
        ('coins_journal', 'Журнал', 'journal-text', 'журнал транзакции коины начисление'),
    ]),
    ('Расписание', 'calendar-week', [
        ('schedule', 'По группам', 'collection', 'расписание группы'),
        ('schedule_all', 'Единая таблица', 'table', 'расписание все группы таблица'),
        ('schedule_teachers', 'По преподавателям', 'person-workspace', 'расписание преподаватели'),
        ('schedule_rooms', 'Аудиторный фонд', 'door-open', 'аудитории кабинеты фонд'),
        ('schedule_conflicts', 'Накладки', 'exclamation-diamond', 'накладки конфликты пересечения'),
        ('schedule_free_rooms', 'Свободные аудитории', 'door-closed', 'свободные аудитории кабинеты'),
        ('schedule_builder', 'Конструктор', 'plus-square', 'конструктор добавить занятие'),
        ('schedule_logs', 'История', 'clock-history', 'история изменения логи'),
    ]),
    ('Контент', 'newspaper', [
        ('news', 'Новости', 'newspaper', 'новости'),
        ('polls', 'Опросы', 'ui-checks', 'опросы голосование'),
        ('events', 'События', 'calendar-event', 'события мероприятия'),
        ('clubs', 'Клубы', 'stars', 'клубы кружки'),
        ('products', 'Товары', 'cart', 'товары магазин'),
    ]),
    ('Люди', 'people', [
        ('admin_users', 'Пользователи', 'people', 'пользователи студенты аккаунты'),
        ('teachers_list', 'Преподаватели', 'person-badge', 'преподаватели учителя'),
        ('administration', 'Администрация', 'person-vcard', 'администрация руководство'),
        ('groups', 'Группы', 'collection', 'группы'),
        ('subjects_list', 'Дисциплины', 'book', 'дисциплины предметы'),
    ]),
    ('Сервис', 'gear', [
        ('feedback_list', 'Обратная связь', 'chat-dots', 'обратная связь отзывы линия доверия'),
        ('audit_log', 'Аудит', 'shield-check', 'аудит журнал действий'),
    ]),
]

_nav_modules = []  # модули с функцией nav_badges() (заполняет cms.register_all)
NAV_BADGES_TTL = 60


def nav_badges():
    """{endpoint: число} для бейджей сайдбара. Собирается из nav_badges() модулей,
    кэш 60 с; упавший модуль просто не даёт бейджа."""
    cached = cache.get('nav:badges')
    if cached is not None:
        return cached
    result = {}
    for mod in list(_nav_modules):
        fn = getattr(mod, 'nav_badges', None)
        if not callable(fn):
            continue
        try:
            for k, v in (fn() or {}).items():
                try:
                    n = int(v or 0)
                except (TypeError, ValueError):
                    continue
                if n > 0:
                    result[str(k)] = n
        except Exception:
            log.warning('nav_badges() модуля %s упал', getattr(mod, '__name__', mod), exc_info=True)
    cache.set('nav:badges', result, ttl=NAV_BADGES_TTL)
    return result


def _active_endpoint():
    """Пункт сайдбара для текущей страницы: точный endpoint, иначе самый длинный префикс URL."""
    ep = request.endpoint
    best, best_len = None, -1
    for _title, _icon, items in NAV_SECTIONS:
        for endpoint, _label, _ic, _kw in items:
            if endpoint == ep:
                return endpoint
            url = nav_url(endpoint)
            if url in ('#', '/'):
                continue
            if (request.path == url or request.path.startswith(url.rstrip('/') + '/')) and len(url) > best_len:
                best, best_len = endpoint, len(url)
    return best


def build_nav():
    """Структура сайдбара для base.html: [{title, icon, active, items:[{endpoint,label,icon,url,active,badge}]}]."""
    active = _active_endpoint()
    try:
        badges = nav_badges() if session.get('uid') else {}
    except Exception:
        badges = {}
    sections, crumbs = [], []
    for title, icon, items in NAV_SECTIONS:
        sec_items = []
        for endpoint, label, ic, kw in items:
            is_active = endpoint == active
            sec_items.append({'endpoint': endpoint, 'label': label, 'icon': ic, 'keywords': kw,
                              'url': nav_url(endpoint), 'active': is_active,
                              'badge': badges.get(endpoint)})
            if is_active:
                crumbs = [title, label] if title != 'Главная' else [label]
        sections.append({'title': title, 'icon': icon, 'items': sec_items,
                         'active': any(i['active'] for i in sec_items),
                         'badge': sum(i['badge'] or 0 for i in sec_items)})
    return sections, crumbs


def nav_index():
    """Плоский список разделов для поиска Ctrl+K: [{label, section, url, icon, keywords}]."""
    out = []
    for title, _icon, items in NAV_SECTIONS:
        for endpoint, label, ic, kw in items:
            url = nav_url(endpoint)
            if url != '#':
                out.append({'label': label, 'section': title, 'url': url, 'icon': ic, 'keywords': kw})
    return out


# ==================== ИНИЦИАЛИЗАЦИЯ ПРИЛОЖЕНИЯ ====================

ASSET_VERSION = '2026.10.19'  # ?v= для app.css/app.js (менять при правке статики)


def load_secret_key():
    """CMS_SECRET_KEY из окружения, иначе instance/secret_key (создаётся при первом запуске)."""
    key = os.environ.get('CMS_SECRET_KEY', '').strip()
    if key:
        return key
    path = os.path.join(INSTANCE_DIR, 'secret_key')
    try:
        with open(path, 'r') as fh:
            existing = fh.read().strip()
        if len(existing) >= 32:
            return existing
    except FileNotFoundError:
        pass
    os.makedirs(INSTANCE_DIR, exist_ok=True)
    new_key = secrets.token_hex(32)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as fh:
            fh.write(new_key)
        return new_key
    except FileExistsError:  # другой воркер успел создать — читаем его ключ
        with open(path, 'r') as fh:
            return fh.read().strip()


_static_mtimes = {}


def static_url(filename):
    """url_for('static') с ?v=<mtime файла> — браузер перекачивает файл сразу после деплоя.
    В Jinja: {{ static_url('css/applicants.css') }} (или макросы ui.css()/ui.js())."""
    now = time.time()
    cached = _static_mtimes.get(filename)
    if cached is None or now - cached[0] > 30:
        try:
            mtime = int(os.path.getmtime(os.path.join(BASE_DIR, 'static', filename)))
        except OSError:
            mtime = 0
        cached = (now, mtime)
        _static_mtimes[filename] = cached
    return url_for('static', filename=filename, v='%s.%d' % (ASSET_VERSION, cached[1]))


def init_app(app):
    """Конфиг, CSRF, cookies, Jinja-фильтры/глобалы, context_processor. Вызывает cms.register_all."""
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s: %(message)s')
    _init_firebase()  # auth.* требует инициализированного приложения Firebase
    app.secret_key = load_secret_key()
    insecure = IS_EMULATOR or app.debug or os.environ.get('CMS_INSECURE_COOKIES') == '1'
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE='Lax',
        SESSION_COOKIE_SECURE=not insecure,
        SESSION_COOKIE_NAME='cc_session',
        PERMANENT_SESSION_LIFETIME=timedelta(hours=SESSION_HOURS),
        MAX_CONTENT_LENGTH=int(os.environ.get('CMS_MAX_UPLOAD_MB', '64')) * 1024 * 1024,
        JSON_AS_ASCII=False,
        TEMPLATES_AUTO_RELOAD=app.debug,
    )
    app.json.ensure_ascii = False

    app.before_request(_csrf_protect)

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault('X-Content-Type-Options', 'nosniff')
        resp.headers.setdefault('X-Frame-Options', 'SAMEORIGIN')
        resp.headers.setdefault('Referrer-Policy', 'strict-origin-when-cross-origin')
        lang = request.args.get('lang')
        if lang in ('ru', 'kk') and request.cookies.get('cc_lang') != lang:
            resp.set_cookie('cc_lang', lang, max_age=365 * 86400, samesite='Lax',
                            secure=app.config['SESSION_COOKIE_SECURE'])
        return resp

    filters = {
        'dt': fmt_dt,
        'dateonly': lambda v, default='—': fmt_dt(v, '%d.%m.%Y', default),
        'timeonly': lambda v, default='—': fmt_dt(v, '%H:%M', default),
        'ago': humanize_ago,
        'num': fmt_num,
        'phone': fmt_phone,
        'safe_url': safe_url,
        'plural': lambda n, one, few, many: plural(n, one, few, many),
        'input_dt': local_input_value,
        'images_of': images_of,
    }
    for name, fn in filters.items():
        app.add_template_filter(fn, name)

    app.add_template_global(csrf_token, 'csrf_token')
    app.add_template_global(url_with, 'url_with')
    app.add_template_global(nav_url, 'nav_url')
    app.add_template_global(static_url, 'static_url')
    app.add_template_global(fmt_pct, 'pct')
    app.add_template_global(plural, 'plural')
    app.add_template_global(public_lang, 'public_lang')

    @app.context_processor
    def _layout_context():
        ctx = {'asset_version': ASSET_VERSION, 'is_emulator': IS_EMULATOR}
        try:
            if session.get('uid') and request.endpoint != 'static':
                sections, crumbs = build_nav()
                ctx.update(nav_sections=sections, nav_crumbs=crumbs, nav_search=nav_index(),
                           admin=current_admin())
        except Exception:
            log.warning('Сайдбар не построен', exc_info=True)
        return ctx


def flash_error(message, exc=None):
    """flash('…', 'error') + запись исключения в лог (текст исключения пользователю не показываем)."""
    if exc is not None:
        log.error('%s: %s', message, exc, exc_info=exc)
    flash(message, 'error')


def stub_page(title, subtitle='', icon='tools', features=(), public=False):
    """Страница-заглушка «раздел в разработке» (временная, владельцы модулей заменят)."""
    template = '_stub_public.html' if public else '_stub.html'
    return render_template(template, stub_title=title, stub_subtitle=subtitle, stub_icon=icon,
                           stub_features=list(features))


# ==================== ПУШ-УВЕДОМЛЕНИЯ (FCM) ====================

_TOPIC_TRANSLIT = {
    'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'zh', 'з': 'z', 'и': 'i',
    'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't',
    'у': 'u', 'ф': 'f', 'х': 'h', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sch', 'ъ': '', 'ы': 'y', 'ь': '',
    'э': 'e', 'ю': 'yu', 'я': 'ya', 'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
    'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'Y', 'К': 'K', 'Л': 'L', 'М': 'M', 'Н': 'N', 'О': 'O', 'П': 'P',
    'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U', 'Ф': 'F', 'Х': 'H', 'Ц': 'Ts', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Sch',
    'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya',
}


def sanitize_topic(value):
    """Имя FCM-топика — точный порт sanitizeTopic из functions/index.js (APP).

    'schedule_group_ПО 24-1' → 'schedule_group_po_24-1'. Как в JS, ъ/ь (пустая замена) остаются
    символом и превращаются в '_'."""
    if not value or not isinstance(value, str):
        return 'default'
    out = ''.join(_TOPIC_TRANSLIT.get(ch) or ch for ch in value)
    out = re.sub(r'[^a-zA-Z0-9\-_.~%]', '_', out)
    out = re.sub(r'_+', '_', out)
    out = re.sub(r'^_|_$', '', out).lower()
    return out or 'default'


# В эмуляторе (и при CMS_DISABLE_PUSH=1) пуши не отправляются, а складываются сюда — для тестов.
sent_pushes = []


def send_push(title, body, topic=None, token=None, data=None):
    """Отправить пуш в FCM-топик (sanitize_topic применяется автоматически) или на токен.

    data — плоский dict (значения приводятся к строкам), например {'type': 'schedule'}.
    → True, если отправлено (или записано в sent_pushes в тестах); ошибки логируются, не бросаются."""
    if not topic and not token:
        return False
    payload = {'title': str(title)[:200], 'body': str(body)[:1000],
               'topic': sanitize_topic(topic) if topic else None, 'token': token,
               'data': {str(k): str(v) for k, v in (data or {}).items() if v is not None}}
    if IS_EMULATOR or os.environ.get('CMS_DISABLE_PUSH') == '1':
        sent_pushes.append(payload)
        log.info('Пуш (не отправлен, тестовый режим): %s', payload)
        return True
    try:
        from firebase_admin import messaging
        _init_firebase()
        msg = messaging.Message(
            notification=messaging.Notification(title=payload['title'], body=payload['body']),
            data=payload['data'] or None, topic=payload['topic'], token=payload['token'])
        messaging.send(msg)
        return True
    except Exception:
        log.exception('Не удалось отправить пуш (%s)', payload['topic'] or 'token')
        return False


# ==================== ЭКСПОРТ CSV ====================

def csv_response(filename, header, rows, delimiter=';'):
    """CSV для Excel: UTF-8 с BOM, разделитель ';' (русская локаль Excel), имя файла в UTF-8.

        return csv_response('лиды.csv', ['Имя', 'Логин'], [[l['name'], l['login']] for l in leads])
    """
    import csv
    from flask import Response
    buf = io.StringIO()
    buf.write('﻿')
    w = csv.writer(buf, delimiter=delimiter, lineterminator='\r\n')
    if header:
        w.writerow(header)
    for row in rows:
        w.writerow(['' if v is None else (fmt_dt(v) if isinstance(v, datetime) else v) for v in row])
    ascii_name = re.sub(r'[^A-Za-z0-9._-]+', '_', filename) or 'export.csv'
    resp = Response(buf.getvalue(), mimetype='text/csv; charset=utf-8')
    resp.headers['Content-Disposition'] = "attachment; filename=\"%s\"; filename*=UTF-8''%s" % (
        ascii_name, quote(filename))
    return resp
