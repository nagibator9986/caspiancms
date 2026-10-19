# cms/routes_auth.py — вход/выход администратора и страницы ошибок (владелец — FOUNDATION).
#
# Вход: REST Identity Toolkit (с учётом эмулятора Auth) → verify_id_token →
# users/{uid}.role == 'admin'. Сессия: подписанная cookie (HttpOnly, SameSite=Lax,
# Secure в проде), 12 часов; роль перепроверяется admin_required раз в 10 минут.

import time

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from .core import (LoginError, RateLimiter, Routes, admin_required, audit, client_ip, db, log, safe_next_url,
                   sign_in_with_password, wants_json)

routes = Routes()

# Не больше 10 неудачных попыток входа с одного IP за 10 минут (в памяти процесса).
_login_limiter = RateLimiter(limit=10, window=600)


def _render_login(email='', next_url=None, status=200):
    return render_template('login.html', email=email, next=next_url or ''), status


@routes.route('/login', methods=['GET', 'POST'])
def login():
    next_url = safe_next_url(request.values.get('next'))
    if request.method == 'GET':
        if session.get('uid') and session.get('is_admin'):
            return redirect(next_url or url_for('dashboard'))
        return _render_login(next_url=next_url)

    email = (request.form.get('email') or '').strip()
    password = request.form.get('password') or ''
    if not email or not password:
        flash('Введите email и пароль', 'error')
        return _render_login(email, next_url)

    ip = client_ip()
    if _login_limiter.blocked(ip):
        flash('Слишком много попыток входа. Подождите 10 минут и попробуйте снова', 'error')
        return _render_login(email, next_url, 429)

    try:
        decoded = sign_in_with_password(email, password)
    except LoginError as e:
        _login_limiter.hit(ip)
        flash(str(e), 'error')
        return _render_login(email, next_url)

    uid = decoded['uid']
    try:
        snap = db.collection('users').document(uid).get()
        data = snap.to_dict() if snap.exists else None
    except Exception:
        log.exception('Не удалось прочитать профиль при входе')
        flash('Сервис временно недоступен. Попробуйте позже', 'error')
        return _render_login(email, next_url)
    if not data or data.get('role') != 'admin':
        _login_limiter.hit(ip)
        flash('Вход только для администраторов', 'error')
        return _render_login(email, next_url)

    # Новая сессия: старые данные (в т.ч. чужой CSRF-токен) не переносим.
    session.clear()
    session.permanent = True
    session['uid'] = uid
    session['email'] = decoded.get('email') or email
    session['admin_name'] = (data.get('name') or '')[:120]
    session['is_admin'] = True
    session['role_checked_at'] = time.time()
    _login_limiter.reset(ip)
    audit('login', 'session', uid)
    flash('Добро пожаловать!', 'success')
    return redirect(next_url or url_for('dashboard'))


@routes.route('/logout', methods=['GET', 'POST'])
def logout():
    # POST (с CSRF) — из меню; GET оставлен для старых ссылок.
    session.clear()
    flash('Вы вышли из админ-панели', 'info')
    return redirect(url_for('login'))


# ==================== СТРАНИЦЫ ОШИБОК ====================

_ERRORS = {
    400: ('Некорректный запрос', 'Проверьте введённые данные и попробуйте ещё раз.', 'exclamation-octagon'),
    403: ('Доступ запрещён', 'У этой учётной записи нет прав администратора или сессия устарела.', 'shield-lock'),
    404: ('Страница не найдена', 'Возможно, ссылка устарела или раздел переехал. Воспользуйтесь меню или поиском (Ctrl+K).', 'signpost-split'),
    405: ('Метод не поддерживается', 'Это действие нельзя выполнить переходом по ссылке.', 'slash-circle'),
    413: ('Файл слишком большой', 'Уменьшите фото (до 10 МБ каждое) и загрузите снова.', 'file-earmark-x'),
    429: ('Слишком много запросов', 'Подождите немного и повторите.', 'hourglass-split'),
    500: ('Что-то пошло не так', 'Мы уже записали ошибку в журнал. Обновите страницу или вернитесь назад.', 'bug'),
    503: ('Сервис временно недоступен', 'Не удалось связаться с базой данных. Попробуйте через минуту.', 'cloud-slash'),
}
_CREDENTIALS_TEXT = ('Нет доступа к базе данных',
                     'Google отклонил ключ сервис-аккаунта Firebase, поэтому админка не может читать '
                     'и сохранять данные. Администратору сайта: создайте новый ключ в Google Cloud и '
                     'пропишите путь к нему в FIREBASE_CREDENTIALS (DEPLOY.md, раздел 3).', 'key')
_CSRF_TEXT = ('Сессия устарела', 'Страница была открыта слишком давно или cookie были очищены. '
              'Обновите страницу и повторите действие.', 'arrow-clockwise')


def _handle_error(e):
    code = getattr(e, 'code', None) or 500
    if code == 400 and getattr(e, 'description', '') == 'csrf':
        title, text, icon = _CSRF_TEXT
    elif code == 503 and getattr(e, 'description', '') == 'credentials':
        title, text, icon = _CREDENTIALS_TEXT
    else:
        title, text, icon = _ERRORS.get(code, _ERRORS[500])
    if code >= 500:
        log.error('Ошибка %s на %s %s', code, request.method, request.path,
                  exc_info=getattr(e, 'original_exception', None) or None)
    if wants_json():
        return jsonify({'ok': False, 'success': False, 'error': title, 'code': code}), code
    admin_layout = bool(session.get('uid')) and code not in (500, 503)
    if code == 404 and not session.get('uid'):
        text = 'Возможно, ссылка устарела или страница переехала.'
    try:
        return render_template('errors/error.html', code=code, title=title, text=text, icon=icon,
                               admin_layout=admin_layout), code
    except Exception:
        log.exception('Не удалось отрисовать страницу ошибки')
        return '%d — %s' % (code, title), code


def setup(app):
    """Регистрирует обработчики ошибок (вызывается из cms.register_all)."""
    for code in (400, 403, 404, 405, 413, 429, 500, 503):
        app.register_error_handler(code, _handle_error)


# ==================== UI KIT (живой справочник компонентов) ====================

@routes.route('/ui-kit', methods=['GET', 'POST'])
@admin_required
def ui_kit():
    """Все макросы _ui.html и классы app.css на одной странице (для разработчиков модулей).
    POST — «сухой» разбор формы ui.image_uploader без загрузки в Storage."""
    from flask import request as rq
    demo_images = [
        {'url': url_for('static', filename='img/logo.png'), 'path': 'demo/logo.png', 'w': 160, 'h': 160},
        {'url': url_for('static', filename='img/apple-touch-icon.png'), 'path': 'demo/icon.png', 'w': 180, 'h': 180},
    ]
    if rq.method == 'POST':
        import json as _json
        state = rq.form.get('images__state') or '[]'
        try:
            parsed = _json.loads(state)
        except ValueError:
            parsed = []
        files = [f.filename for f in rq.files.getlist('images') if f.filename]
        flash('Порядок: %s; новых файлов: %d' % (
            ', '.join('новое' if p.get('kind') == 'new' else 'старое' for p in parsed if isinstance(p, dict)) or '—',
            len(files)), 'success')
        return redirect(url_for('ui_kit'))
    rows = [
        {'name': 'Сапарова Айгерим', 'login': '+7 701 123 45 67', 'status': 'new', 'source': 'STAND1', 'coins': 150},
        {'name': 'Жумабаев Тимур', 'login': 'timur_2008', 'status': 'contacted', 'source': 'INSTA', 'coins': 50},
        {'name': 'Ким Дана', 'login': '+7 705 555 01 02', 'status': 'enrolled', 'source': 'OPENDAY', 'coins': 320},
        {'name': '—', 'login': 'aruzhan.k', 'status': 'declined', 'source': 'APP', 'coins': 0},
    ]
    from .core import paginate_list
    kit_page = paginate_list(list(range(160)), 3, 25)
    return render_template('_ui_kit.html', demo_images=demo_images, rows=rows, kit_page=kit_page)
