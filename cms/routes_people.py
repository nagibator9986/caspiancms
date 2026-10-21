# cms/routes_people.py — раздел «Люди»: пользователи, преподаватели, группы, дисциплины,
# администрация (владелец — PEOPLE).
#
# Схема users/{uid} — как в приложении:
#   студент       name, email, role='student', group, course ('1'..'4'), phone?, attendance, grades,
#                 createdAt, updatedAt (как AuthService.createStudentByAdmin);
#   преподаватель name, email?, role='teacher', phone?, subject? — uid служит ссылкой из
#                 schedules.teacher, поэтому НИКОГДА не меняется: вход создаётся с тем же uid;
#   admin / club  name, email, role, phone?;
#   абитуриент    role='applicant', login, email (синтетический), профиль частями (SPEC §2.1).
#                 Роль applicant назначает только регистрация: в формах её нельзя выбрать или снять,
#                 заглушки group/course абитуриенту не пишутся. Сброс пароля → abiturient123 +
#                 mustChangePassword + revoke_refresh_tokens (D5).
# Удаление пользователя — только POST: сначала аккаунт Firebase Auth, потом users/{uid}
# (данные абитуриента подчищает функция onApplicantDeleted).
#
# Производительность: список пользователей — один снапшот users с select() и кэшем 5 мин
# ('people:users'), фильтры, поиск и пагинация — в памяти; любая запись сбрасывает кэш.
# Нагрузка преподавателей и занятия групп берутся из общего снапшота расписания (schedule_model).

import re
from collections import Counter, OrderedDict

from flask import flash, jsonify, redirect, render_template, request, session, url_for

from . import conflicts
from .applicant_login import RESET_PASSWORD, display_login, normalize_phone
from .core import (DELETE_FIELD, SERVER_TIMESTAMP, ArrayUnion, Routes, UploadError, admin_required, audit, cache,
                   chunked, csv_response, db, delete_storage_paths, fb_auth, flash_action, flash_error, fs_opts,
                   get_docs, log, nav_url, next_target, now_utc, paginate_list, redirect_back, safe_next_url,
                   storage_path_from_url, to_datetime, today_almaty, upload_image, url_with, where)
from .schedule_model import (DAY_NAMES_FULL, get_all_groups, get_all_teachers, group_key, group_sort_key,
                             intervals_overlap, invalidate_directory_caches, invalidate_schedule_caches,
                             load_lessons_snapshot, norm_text)

routes = Routes()

PER_PAGE = 30
USERS_KEY = 'people:users'
ACCOUNTS_KEY = 'people:accounts'
MANAGEMENT_KEY = 'people:management'
SUBJECTS_KEY = 'all_subjects:docs'      # пространство all_subjects сбрасывает invalidate_directory_caches()
GROUPS_DIR_KEY = 'all_groups:docs'      # пространство all_groups сбрасывают и расписание, и справочники
USERS_TTL = 300
MAX_BULK = 500

ROLE_LABELS = OrderedDict([
    ('student', 'Студент'), ('teacher', 'Преподаватель'), ('applicant', 'Абитуриент'),
    ('admin', 'Администратор'), ('club', 'Клуб / организатор'),
])
# Порядок вкладок = приоритет работы: абитуриенты — последними (раздел второстепенный, UX_SPEC_v2 PPL-1).
ROLE_TABS = [('student', 'Студенты', 'backpack'), ('teacher', 'Преподаватели', 'person-badge'),
             ('admin', 'Администраторы', 'shield-lock'), ('club', 'Клубы', 'stars'),
             ('applicant', 'Абитуриенты', 'mortarboard')]
EDITABLE_ROLES = ('student', 'teacher', 'admin', 'club')   # applicant — только саморегистрация
DEFAULT_PASSWORDS = {'teacher': 'Teacher123', 'student': 'Student123', 'club': 'Student123',
                     'applicant': RESET_PASSWORD}
COURSES = ('1', '2', '3', '4')
COURSE_OPTIONS = [(c, '%s курс' % c) for c in COURSES]
USER_SORTS = OrderedDict([('name', 'По имени'), ('new', 'Сначала новые'), ('group', 'По группе')])
USER_FIELDS = ['name', 'email', 'role', 'group', 'course', 'phone', 'login', 'source', 'createdAt',
               'mustChangePassword', 'subject']

EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
GROUP_NAME_MAX = 40
SUBJECT_NAME_MAX = 120
MANAGEMENT_FOLDER = 'management'


# ==================== ОБЩИЕ ПОМОЩНИКИ ====================

def _clean(value, limit):
    """Строка из формы: схлопнутые пробелы, обрезка по длине."""
    return ' '.join(str(value or '').split())[:limit]


def _phone_digits(value):
    digits = re.sub(r'\D', '', str(value or ''))
    if len(digits) == 11 and digits[0] == '8':
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    return digits


def _matches(q, hay):
    """Все слова запроса есть в строке поиска; запрос-телефон сравнивается по цифрам."""
    q = (q or '').strip()
    if not q:
        return True
    if re.fullmatch(r'[\d\s()+\-]{5,}', q):
        return _phone_digits(q) in hay or re.sub(r'\D', '', q) in hay
    return all(w in hay for w in norm_text(q).split())


def _password_problem(password):
    """Пароль сотрудника/студента: 6–128 символов, без пробелов по краям (не обрезаем — отклоняем)."""
    if not password:
        return 'Введите пароль'
    if password != password.strip():
        return 'Пароль не должен начинаться или заканчиваться пробелом'
    if len(password) < 6:
        return 'Пароль — не короче 6 символов'
    if len(password) > 128:
        return 'Пароль слишком длинный'
    return None


def _next_url(default_endpoint, **values):
    return safe_next_url(request.values.get('next')) or url_for(default_endpoint, **values)


def _back(default_endpoint, **values):
    """Назад после действия: безопасный next (форма/query) → Referer этого сайта → url_for(default)."""
    target = next_target()
    return redirect(target) if target else redirect_back(default_endpoint, **values)


def _invalidate_people(roles=()):
    """Сбросить кэши раздела после записи в users (и справочник преподавателей, если он задет)."""
    cache.invalidate_prefix('people:')
    cache.invalidate_prefix('home:')
    cache.invalidate_prefix('search:')   # Ctrl+K
    roles = set(r for r in roles if r)
    if 'teacher' in roles:
        invalidate_directory_caches()
    if 'applicant' in roles:
        cache.invalidate_prefix('applicants:')


def _snapshot_safe():
    """Снапшот расписания или None (раздел «Люди» не должен падать из-за расписания)."""
    try:
        return load_lessons_snapshot()
    except Exception:
        log.exception('Снапшот расписания недоступен')
        return None


def _week_weight(lesson):
    """Вес занятия в недельной нагрузке: каждую неделю — 1, числитель/знаменатель — 0.5."""
    return 1.0 if lesson.get('week') == 'always' else 0.5


def _fmt_load(value):
    """12.0 → '12', 12.5 → '12,5'."""
    if not value:
        return '0'
    return str(int(value)) if float(value).is_integer() else ('%.1f' % value).replace('.', ',')


def _auth_meta_dt(ms):
    try:
        return to_datetime(int(ms) / 1000.0) if ms else None
    except (TypeError, ValueError):
        return None


def _auth_info(uid):
    """Аккаунт Firebase Auth: dict; None — аккаунта нет; {'error': True} — Auth недоступен."""
    try:
        u = fb_auth.get_user(uid)
    except fb_auth.UserNotFoundError:
        return None
    except Exception:
        log.exception('Не удалось прочитать аккаунт %s', uid)
        return {'error': True}
    meta = u.user_metadata
    return {
        'email': u.email or '', 'disabled': bool(u.disabled),
        'created': _auth_meta_dt(getattr(meta, 'creation_timestamp', None)),
        'last_sign_in': _auth_meta_dt(getattr(meta, 'last_sign_in_timestamp', None)),
        'last_refresh': _auth_meta_dt(getattr(meta, 'last_refresh_timestamp', None)),
        'tokens_valid_after': _auth_meta_dt(getattr(u, 'tokens_valid_after_timestamp', None)),
    }


def _crm_history(uid, action, details):
    """Запись в историю CRM абитуриента (applicant_crm/{uid}.history, SPEC §2.2)."""
    try:
        db.collection('applicant_crm').document(uid).set({
            'history': ArrayUnion([{'at': now_utc(), 'by': session.get('email') or 'cms', 'action': action,
                                    'details': details}]),
            'updatedAt': SERVER_TIMESTAMP, 'updatedBy': session.get('uid'),
        }, merge=True)
    except Exception:
        log.exception('Не удалось записать историю CRM %s', uid)


# ==================== СНАПШОТ ПОЛЬЗОВАТЕЛЕЙ ====================

def _user_row(uid, d):
    role = str(d.get('role') or '').strip()
    email = str(d.get('email') or '').strip()
    name = str(d.get('name') or '').strip()
    group = str(d.get('group') or '').strip()
    if group in ('-', '—'):
        group = ''
    phone = str(d.get('phone') or '').strip()
    login = ''
    if role == 'applicant':
        login = display_login(email) if email else str(d.get('login') or '')
    row = {
        'id': uid, 'name': name, 'email': email, 'role': role,
        'role_key': role if role in ROLE_LABELS else 'none',
        'group': group, 'course': str(d.get('course') or '').strip(), 'phone': phone, 'login': login,
        'source': str(d.get('source') or ''), 'created': to_datetime(d.get('createdAt')),
        'must_change': bool(d.get('mustChangePassword')), 'subject': str(d.get('subject') or ''),
    }
    row['title'] = name or login or email or uid
    row['hay'] = ' '.join([norm_text(' '.join([name, email, login, group, uid, str(d.get('login') or ''), phone])),
                           _phone_digits(phone)])
    return row


def _users_snapshot():
    """Все пользователи (select нужных полей), кэш 5 мин. Строки общие для всех запросов:
    не менять на месте — копировать (dict(row))."""
    def load():
        return [_user_row(snap.id, snap.to_dict() or {})
                for snap in db.collection('users').select(USER_FIELDS).stream(**fs_opts(True))]
    return cache.get_or_set(USERS_KEY, load, ttl=USERS_TTL)


def _filter_users(rows, role='', q='', group='', course=''):
    out = []
    for r in rows:
        if role and r['role_key'] != role:
            continue
        if group == '__none__':
            if r['role'] != 'student' or r['group']:
                continue
        elif group and r['group'] != group:
            continue
        if course and r['course'] != course:
            continue
        if not _matches(q, r['hay']):
            continue
        out.append(r)
    return out


def _sort_users(rows, sort):
    if sort == 'new':
        return sorted(rows, key=lambda r: r['created'].timestamp() if r['created'] else 0, reverse=True)
    if sort == 'group':
        return sorted(rows, key=lambda r: (r['group'] == '', group_sort_key(r['group']), r['group'],
                                           norm_text(r['title'])))
    return sorted(rows, key=lambda r: (not r['name'], norm_text(r['title'])))  # без имени (логины) — в конце


def _known_groups():
    try:
        return list(get_all_groups())
    except Exception:
        log.exception('Справочник групп недоступен')
        return []


def _users_csv(rows, filename='пользователи.csv'):
    header = ['Имя', 'Email', 'Логин', 'Роль', 'Группа', 'Курс', 'Телефон', 'Источник', 'Создан', 'uid']
    data = [[r['name'], r['email'], r['login'], ROLE_LABELS.get(r['role'], r['role'] or '—'), r['group'],
             r['course'], r['phone'], r['source'], r['created'], r['id']] for r in rows]
    return csv_response(filename, header, data)


# ==================== ПОЛЬЗОВАТЕЛИ ====================

@routes.route('/admin/users')
@admin_required
def admin_users():
    """Пользователи: вкладки по ролям, поиск (имя, email, логин, телефон), группа/курс, сортировка,
    пагинация, массовые действия и CSV-экспорт отфильтрованного списка."""
    if request.args.get('refresh'):   # «Обновить» — перечитать базу и вернуться на тот же список без ?refresh
        cache.invalidate(USERS_KEY)
        cache.invalidate(ACCOUNTS_KEY)
        flash('Список перечитан из базы', 'info')
        return redirect(url_with(refresh=None))
    try:
        rows = _users_snapshot()
    except Exception as e:
        flash_error('Не удалось загрузить пользователей. Попробуйте обновить страницу', e)
        rows = []
    counts = Counter(r['role_key'] for r in rows)
    role = request.args.get('role') or ''
    if role not in ROLE_LABELS and role != 'none':
        role = ''
    q = (request.args.get('q') or '').strip()[:100]
    group = (request.args.get('group') or '').strip()
    course = (request.args.get('course') or '').strip()
    sort = request.args.get('sort')
    if sort not in USER_SORTS:
        sort = 'new' if role == 'applicant' else 'name'
    # «Все» — сотрудники и студенты: абитуриенты (их сотни) — во вкладке «Абитуриенты» и в CRM (PPL-1).
    staff = [r for r in rows if r['role_key'] != 'applicant']
    items = _sort_users(_filter_users(rows if role else staff, role, q, group, course), sort)
    if request.args.get('format') == 'csv':
        audit('export', 'user', None, {'format': 'csv', 'rows': len(items), 'role': role or 'all'})
        return _users_csv(items)
    # поиск во «Всех» подсказывает, что нашлись и абитуриенты (ссылка вместо смешанного списка)
    applicant_hits = len(_filter_users(rows, 'applicant', q)) if q and not role else 0
    page = paginate_list(items, request.args.get('page', 1), PER_PAGE)
    accounts = _account_states([u['id'] for u in page.items])
    no_group = sum(1 for r in rows if r['role'] == 'student' and not r['group'])
    return render_template(
        'admin_users.html', page=page, users=page.items, total=len(staff), counts=counts, role=role, q=q,
        group=group, course=course, sort=sort, sorts=USER_SORTS, role_tabs=ROLE_TABS, role_labels=ROLE_LABELS,
        groups=_known_groups(), courses=COURSES, course_options=COURSE_OPTIONS, no_group=no_group,
        me=session.get('uid'), filtered=len(items), default_passwords=DEFAULT_PASSWORDS, accounts=accounts,
        applicant_hits=applicant_hits)


def _user_form_values(src, data=None):
    data = data or {}
    group = str(src.get('group', data.get('group')) or '').strip()
    return {
        'name': src.get('name', data.get('name')) or '',
        'email': src.get('email', data.get('email')) or '',
        'phone': src.get('phone', data.get('phone')) or '',
        'role': src.get('role', data.get('role')) or 'student',
        'group': '' if group in ('-', '—') else group,
        'course': str(src.get('course', data.get('course')) or ''),
    }


ROLE_OPTIONS = [(r, ROLE_LABELS[r]) for r in EDITABLE_ROLES]


def _render_add_form(values, errors, groups):
    return render_template('admin_users_form.html', values=values, errors=errors, groups=groups, courses=COURSES,
                           role_options=ROLE_OPTIONS)


@routes.route('/admin/users/add', methods=['GET', 'POST'])
@admin_required
def admin_add_user():
    """Новый пользователь (студент, преподаватель, админ, клуб): аккаунт Auth + users/{uid}.
    Абитуриенты регистрируются сами (QR / приложение) — здесь их не создают."""
    groups = _known_groups()
    if request.method == 'GET':
        role = request.args.get('role') if request.args.get('role') in EDITABLE_ROLES else 'student'
        values = {'name': '', 'email': '', 'phone': '', 'role': role, 'group': request.args.get('group') or '',
                  'course': ''}
        return _render_add_form(values, {}, groups)
    f = request.form
    name = _clean(f.get('name'), 120)
    email = (f.get('email') or '').strip().lower()
    password = f.get('password') or ''
    role = f.get('role') or 'student'
    group = _clean(f.get('group'), GROUP_NAME_MAX)
    if group in ('-', '—'):
        group = ''
    course = (f.get('course') or '').strip()
    phone = _clean(f.get('phone'), 20)
    values = {'name': name, 'email': email, 'phone': phone, 'role': role, 'group': group, 'course': course}
    errors = {}
    if len(name) < 2:
        errors['name'] = 'Укажите ФИО (не короче 2 символов)'
    if not EMAIL_RE.match(email) or len(email) > 120:
        errors['email'] = 'Некорректный email'
    problem = _password_problem(password)
    if problem:
        errors['password'] = problem
    if role not in EDITABLE_ROLES:
        errors['role'] = ('Абитуриенты регистрируются сами — по QR-коду или в приложении'
                          if role == 'applicant' else 'Выберите роль')
    if role == 'student':
        if group and group not in groups:
            errors['group'] = 'Такой группы нет — сначала создайте её в разделе «Группы»'
        if course and course not in COURSES:
            errors['course'] = 'Курс — от 1 до 4'
    if errors:
        return _render_add_form(values, errors, groups)
    try:
        user = fb_auth.create_user(email=email, password=password, display_name=name)
    except fb_auth.EmailAlreadyExistsError:
        errors['email'] = 'Пользователь с таким email уже есть'
        return _render_add_form(values, errors, groups)
    except ValueError:
        errors['email'] = 'Firebase не принял email или пароль — проверьте их'
        return _render_add_form(values, errors, groups)
    except Exception as e:
        flash_error('Не удалось создать аккаунт. Попробуйте ещё раз', e)
        return _render_add_form(values, errors, groups)
    doc = {'name': name, 'email': email, 'role': role, 'createdAt': SERVER_TIMESTAMP, 'updatedAt': SERVER_TIMESTAMP}
    if phone:
        doc['phone'] = phone
    if role == 'student':
        doc.update({'attendance': '0%', 'grades': 'N/A'})  # как createStudentByAdmin в приложении
        if group:
            doc['group'] = group
        if course:
            doc['course'] = course
    try:
        db.collection('users').document(user.uid).set(doc)
    except Exception as e:
        try:  # без профиля аккаунт бесполезен — откатываем
            fb_auth.delete_user(user.uid)
        except Exception:
            log.exception('Откат аккаунта %s не удался', user.uid)
        flash_error('Не удалось сохранить профиль — пользователь не создан', e)
        return _render_add_form(values, errors, groups)
    _invalidate_people([role])
    audit('create', 'user', user.uid, {'role': role, 'email': email, 'name': name})
    flash_action('Пользователь «%s» создан. Пароль передайте лично.' % name, 'Открыть',
                 href=url_for('admin_edit_user', user_id=user.uid))
    return redirect(url_for('admin_users', role=role))


def _parse_user_edit(form, data, uid, groups):
    """Разбор формы правки → (values, errors, updates, changes, new_role, email_change).

    Меняются только поля, которые пришли в форме (отсутствующее поле — «не трогать»),
    поэтому старые/урезанные формы не стирают телефон, email или группу."""
    errors, updates, changes = {}, {'updatedAt': SERVER_TIMESTAMP}, {}
    old_role = data.get('role') or ''
    values = _user_form_values(form, data)
    name = _clean(form.get('name'), 120) if 'name' in form else _clean(data.get('name'), 120)
    phone_raw = (form.get('phone') or '').strip() if 'phone' in form else None
    values['name'] = name
    if phone_raw is not None:
        values['phone'] = phone_raw
    email_change = None
    new_vals = {}

    if old_role == 'applicant':
        # Абитуриент: только имя и телефон по правилам профиля (SPEC §2.1). Роль, группа, курс не пишутся.
        new_role = 'applicant'
        values['role'] = 'applicant'
        if 'name' in form:
            if name and len(name) < 2:
                errors['name'] = 'Имя — не короче 2 символов (или оставьте пустым)'
            updates['name'] = name or DELETE_FIELD
            new_vals['name'] = name
        if phone_raw is not None:
            phone = ''
            if phone_raw:
                phone = normalize_phone(phone_raw) or ''
                if not phone:
                    errors['phone'] = 'Телефон в формате +7 7XX XXX XX XX'
            updates['phone'] = phone or DELETE_FIELD
            new_vals['phone'] = phone
    else:
        new_role = form.get('role') or old_role
        if len(name) < 2:
            errors['name'] = 'Укажите ФИО (не короче 2 символов)'
        if new_role not in EDITABLE_ROLES:
            errors['role'] = ('Роль «Абитуриент» назначает только регистрация'
                              if new_role == 'applicant' else 'Выберите роль')
            new_role = old_role if old_role in EDITABLE_ROLES else 'student'
        if uid == session.get('uid') and new_role != 'admin':
            errors['role'] = 'Нельзя снять роль администратора с самого себя'
        updates.update({'name': name, 'role': new_role})
        new_vals.update(name=name, role=new_role)
        if phone_raw is not None:
            if len(phone_raw) > 20:
                errors['phone'] = 'Телефон — не длиннее 20 символов'
            phone = _clean(phone_raw, 20)
            updates['phone'] = phone or DELETE_FIELD
            new_vals['phone'] = phone
        if new_role == 'student':
            if 'group' in form:
                group = _clean(form.get('group'), GROUP_NAME_MAX)
                if group in ('-', '—'):
                    group = ''
                if group and group not in groups and group != (data.get('group') or ''):
                    errors['group'] = 'Такой группы нет — сначала создайте её в разделе «Группы»'
                updates['group'] = group or DELETE_FIELD
                new_vals['group'] = group
                values['group'] = group
            if 'course' in form:
                course = (form.get('course') or '').strip()
                if course and course not in COURSES and course != str(data.get('course') or ''):
                    errors['course'] = 'Курс — от 1 до 4'
                updates['course'] = course or DELETE_FIELD
                new_vals['course'] = course
                values['course'] = course
        else:
            # не студент — группа и курс не нужны (иначе приложение подпишет его на топик группы)
            updates['group'] = DELETE_FIELD
            updates['course'] = DELETE_FIELD
            new_vals.update(group='', course='')
        if 'email' in form:
            email = (form.get('email') or '').strip().lower()
            values['email'] = email
            if email != (data.get('email') or '').strip().lower():
                if email and (not EMAIL_RE.match(email) or len(email) > 120):
                    errors['email'] = 'Некорректный email'
                elif not email:
                    errors['email'] = 'Email нельзя удалить — по нему пользователь входит'
                else:
                    email_change = email
                    updates['email'] = email
                    new_vals['email'] = email
    for k, v in new_vals.items():
        old = data.get(k)
        old = '' if old is None else str(old)
        if k == 'group' and old in ('-', '—'):
            old = ''
        if str(v or '') != old:
            changes[k] = [old, v or '']
    return values, errors, updates, changes, new_role, email_change


@routes.route('/admin/users/edit/<user_id>', methods=['GET', 'POST'])
@admin_required
def admin_edit_user(user_id):
    """Карточка и правка пользователя. GET — форма + аккаунт входа; POST — сохранение по роли."""
    ref = db.collection('users').document(user_id)
    try:
        snap = ref.get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить пользователя', e)
        return redirect(url_for('admin_users'))
    if not snap.exists:
        flash('Пользователь не найден — возможно, его уже удалили', 'error')
        return redirect(url_for('admin_users'))
    data = snap.to_dict() or {}
    groups = _known_groups()
    errors, values = {}, _user_form_values({}, data)
    if request.method == 'POST':
        values, errors, updates, changes, new_role, email_change = _parse_user_edit(request.form, data, user_id,
                                                                                    groups)
        if not errors:
            try:
                if email_change:
                    try:
                        fb_auth.update_user(user_id, email=email_change)
                    except fb_auth.UserNotFoundError:
                        pass  # аккаунта входа нет — меняем только профиль
                ref.update(updates)
            except fb_auth.EmailAlreadyExistsError:
                errors['email'] = 'Этот email уже занят другим аккаунтом'
            except Exception as e:
                flash_error('Не удалось сохранить изменения', e)
                errors['_form'] = 'Изменения не сохранены'
            else:
                _invalidate_people([data.get('role'), new_role])
                if 'name' in changes and new_role == 'teacher':
                    invalidate_directory_caches()
                details = {'role': new_role}
                if changes:
                    details['changes'] = changes
                audit('update', 'user', user_id, details)
                if changes:
                    flash_action('Изменения сохранены: %s' % (values.get('name') or user_id), 'Открыть',
                                 href=url_for('admin_edit_user', user_id=user_id))
                else:
                    flash('Изменений нет', 'info')
                return redirect(_next_url('admin_users', role=new_role if new_role in ROLE_LABELS else None))
    snapshot = _snapshot_safe()
    lessons = []
    if snapshot is not None and data.get('role') == 'teacher':
        lessons = snapshot.by_teacher.get('uid:' + user_id, [])
    return render_template(
        'admin_users_edit.html', user=_user_row(user_id, data), data=data, values=values, errors=errors,
        groups=groups, courses=COURSES, role_labels=ROLE_LABELS, role_options=ROLE_OPTIONS,
        account=_auth_info(user_id), is_self=user_id == session.get('uid'), lessons_count=len(lessons),
        default_password=DEFAULT_PASSWORDS.get(data.get('role') or ''),
        next_url=safe_next_url(request.values.get('next')))


@routes.route('/admin/users/reset-password/<user_id>', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    """Сброс пароля на стандартный по роли: абитуриент → abiturient123 + обязательная смена при
    входе + выход на всех устройствах (D5); студент/клуб → Student123; преподаватель → Teacher123.
    Пароль администратора так не сбрасывается."""
    ref = db.collection('users').document(user_id)
    try:
        snap = ref.get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить пользователя', e)
        return _back('admin_users')
    if not snap.exists:
        flash('Пользователь не найден', 'error')
        return _back('admin_users')
    data = snap.to_dict() or {}
    role = data.get('role') or ''
    title = data.get('name') or (display_login(data.get('email')) if data.get('email') else user_id)
    if role == 'admin' or user_id == session.get('uid'):
        flash('Пароль администратора стандартным не сбрасывается: пусть он сменит его сам '
              '(«Забыли пароль?» на странице входа) или в консоли Firebase', 'error')
        return _back('admin_users')
    password = DEFAULT_PASSWORDS.get(role)
    if not password:
        flash('У пользователя неизвестная роль — назначьте роль, затем сбросьте пароль', 'error')
        return _back('admin_users')
    try:
        fb_auth.update_user(user_id, password=password)
    except fb_auth.UserNotFoundError:
        flash('У «%s» нет аккаунта для входа — сбрасывать нечего' % title, 'error')
        return _back('admin_users')
    except Exception as e:
        flash_error('Не удалось сбросить пароль. Попробуйте ещё раз', e)
        return _back('admin_users')
    updates = {'updatedAt': SERVER_TIMESTAMP}
    if role == 'applicant':
        updates.update({'mustChangePassword': True, 'passwordResetAt': SERVER_TIMESTAMP})
        try:
            fb_auth.revoke_refresh_tokens(user_id)
        except Exception:
            log.exception('revoke_refresh_tokens(%s) не удался', user_id)
    try:
        ref.update(updates)
    except Exception:
        log.exception('Не удалось отметить сброс пароля в профиле %s', user_id)
        flash('Пароль сброшен, но профиль не обновился — при входе смена пароля может не потребоваться', 'warning')
    if role == 'applicant':
        _crm_history(user_id, 'password_reset', 'Пароль сброшен на %s' % RESET_PASSWORD)
    _invalidate_people([role])
    audit('reset_password', 'user', user_id, {'role': role, 'name': data.get('name') or ''})
    if role == 'applicant':
        flash('Пароль «%s» сброшен на %s. Сеансы завершены, при входе приложение попросит придумать новый пароль.'
              % (title, RESET_PASSWORD), 'success')
    else:
        flash('Пароль «%s» сброшен на %s' % (title, password), 'success')
    return _back('admin_users')


def _delete_account_and_profile(uid):
    """Удалить аккаунт Auth (если есть), затем users/{uid}. Бросает исключение при сбое."""
    try:
        fb_auth.delete_user(uid)
    except fb_auth.UserNotFoundError:
        pass
    db.collection('users').document(uid).delete()


def _cleanup_applicant_data(docs):
    """После удаления users/{uid} (docs — {uid: профиль до удаления}). Абитуриент: заметки к его заказам
    (coin_order_notes пишет CMS, функция про них не знает; остальное чистит onApplicantDeleted). Зачисленный
    студент (есть CRM-карточка): функция выходит (role ≠ applicant) — его данные абитуриента чистит CMS.
    → (сколько зачисленных найдено, сколько очищено не полностью)."""
    from .routes_applicants import _delete_order_notes, purge_converted_applicant
    for uid, d in docs.items():
        if (d or {}).get('role') == 'applicant':
            _delete_order_notes(uid)
    students = [uid for uid, d in docs.items() if (d or {}).get('role') == 'student']
    converted = list(get_docs('applicant_crm', students, ['status'])) if students else []
    failed = sum(1 for uid in converted if not purge_converted_applicant(uid))
    if converted:
        cache.invalidate_prefix('applicants:')
        cache.invalidate_prefix('coins:')
        cache.invalidate('nav:badges')
    return len(converted), failed


@routes.route('/admin/users/delete/<user_id>', methods=['POST'])
@admin_required
def admin_delete_user(user_id):
    """Удаление пользователя: аккаунт Firebase Auth + users/{uid} (данные абитуриента подчистит
    функция onApplicantDeleted). Себя удалить нельзя."""
    if user_id == session.get('uid'):
        flash('Нельзя удалить самого себя', 'error')
        return _back('admin_users')
    try:
        snap = db.collection('users').document(user_id).get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить пользователя', e)
        return _back('admin_users')
    if not snap.exists:
        flash('Пользователь не найден — возможно, его уже удалили', 'error')
        return redirect(url_for('admin_users'))
    data = snap.to_dict() or {}
    role = data.get('role') or ''
    title = data.get('name') or (display_login(data.get('email')) if data.get('email') else user_id)
    try:
        _delete_account_and_profile(user_id)
    except Exception as e:
        flash_error('Не удалось удалить пользователя. Попробуйте ещё раз', e)
        return _back('admin_users')
    converted, failed = _cleanup_applicant_data({user_id: data})
    _invalidate_people([role])
    audit('delete', 'user', user_id, {'role': role, 'name': data.get('name') or '', 'email': data.get('email') or '',
                                      'applicant_data': bool(converted) or None})
    if failed:
        flash('Пользователь «%s» удалён, но часть его данных абитуриента (коины, заказы, CRM) не очистилась — '
              'сообщите разработчику' % title, 'warning')
    else:
        flash('Пользователь «%s» удалён вместе с аккаунтом входа%s'
              % (title, ' и данными абитуриента' if converted else ''), 'success')
    return redirect(_next_url('admin_users', role=role if role in ROLE_LABELS else None))


@routes.route('/admin/users/<user_id>/disable', methods=['POST'])
@admin_required
def admin_user_set_disabled(user_id):
    """Заблокировать/разблокировать вход (Firebase Auth disabled) без удаления данных."""
    disabled = request.form.get('disabled') == '1'
    if user_id == session.get('uid'):
        flash('Нельзя заблокировать самого себя', 'error')
        return _back('admin_users')
    try:
        fb_auth.update_user(user_id, disabled=disabled)
        if disabled:
            fb_auth.revoke_refresh_tokens(user_id)
    except fb_auth.UserNotFoundError:
        flash('У пользователя нет аккаунта для входа', 'error')
        return _back('admin_users')
    except Exception as e:
        flash_error('Не удалось изменить доступ', e)
        return _back('admin_users')
    cache.invalidate(ACCOUNTS_KEY)
    audit('disable' if disabled else 'enable', 'user', user_id)
    # обратимо: в тосте «Отменить» (обратный POST с тем же next)
    flash_action('Вход заблокирован, все сеансы завершены' if disabled else 'Вход снова разрешён', 'Отменить',
                 post=url_for('admin_user_set_disabled', user_id=user_id),
                 fields={'disabled': '0' if disabled else '1', 'next': next_target() or ''})
    return _back('admin_users')


@routes.route('/admin/users/<user_id>/revoke', methods=['POST'])
@admin_required
def admin_user_revoke(user_id):
    """Завершить все сеансы пользователя (выход на всех устройствах)."""
    try:
        fb_auth.revoke_refresh_tokens(user_id)
    except fb_auth.UserNotFoundError:
        flash('У пользователя нет аккаунта для входа', 'error')
        return _back('admin_users')
    except Exception as e:
        flash_error('Не удалось завершить сеансы', e)
        return _back('admin_users')
    audit('revoke_sessions', 'user', user_id)
    flash('Все сеансы завершены — пользователю нужно войти заново', 'success')
    return _back('admin_users')


@routes.route('/admin/users/bulk', methods=['POST'])
@admin_required
def admin_users_bulk():
    """Массовые действия над выбранными: группа и курс (только студентам), CSV, удаление
    (кроме себя и администраторов)."""
    ids = [i for i in dict.fromkeys(request.form.getlist('ids')) if i][:MAX_BULK]
    action = request.form.get('action') or ''
    if not ids:
        flash('Сначала отметьте пользователей в списке', 'warning')
        return _back('admin_users')
    if action == 'export':
        wanted = set(ids)
        rows = [r for r in _users_snapshot() if r['id'] in wanted]
        audit('export', 'user', None, {'format': 'csv', 'rows': len(rows), 'selected': True})
        return _users_csv(_sort_users(rows, 'name'), 'пользователи_выбранные.csv')
    try:
        docs = get_docs('users', ids, ['role', 'name'])
    except Exception as e:
        flash_error('Не удалось загрузить выбранных пользователей', e)
        return _back('admin_users')
    if action in ('group', 'course'):
        return _bulk_student_field(action, ids, docs)
    if action == 'delete':
        return _bulk_delete(ids, docs)
    flash('Неизвестное действие', 'error')
    return _back('admin_users')


def _bulk_student_field(action, ids, docs):
    students = [uid for uid in ids if (docs.get(uid) or {}).get('role') == 'student']
    if action == 'group':
        value = _clean(request.form.get('group'), GROUP_NAME_MAX)
        if value and value not in _known_groups():
            flash('Такой группы нет', 'error')
            return _back('admin_users')
        fields = {'group': value or DELETE_FIELD}
        what = 'группа «%s»' % value if value else 'группа убрана'
    else:
        value = (request.form.get('course') or '').strip()
        if value not in COURSES:
            flash('Выберите курс от 1 до 4', 'error')
            return _back('admin_users')
        fields = {'course': value}
        what = 'курс %s' % value
    fields['updatedAt'] = SERVER_TIMESTAMP
    try:
        for part in chunked(students, 450):
            batch = db.batch()
            for uid in part:
                batch.update(db.collection('users').document(uid), fields)
            batch.commit()
    except Exception as e:
        flash_error('Не удалось обновить пользователей', e)
        return _back('admin_users')
    _invalidate_people(['student'])
    audit('bulk_' + action, 'user', None, {'ids': students, 'value': value})
    skipped = len(ids) - len(students)
    if len(ids) == 1 and students:   # перевод одного студента из меню строки
        title = (docs.get(students[0]) or {}).get('name') or students[0]
        flash(('«%s»: %s' % (title, what)) if action == 'course' or not value
              else '«%s» переведён(а) в группу «%s»' % (title, value), 'success')
        return _back('admin_users')
    flash('Обновлено студентов: %d (%s)%s' % (len(students), what,
          '; пропущено не-студентов: %d' % skipped if skipped else ''), 'success' if students else 'warning')
    return _back('admin_users')


def _bulk_delete(ids, docs):
    me = session.get('uid')
    targets = [uid for uid in ids if uid in docs and uid != me and docs[uid].get('role') != 'admin']
    skipped = len(ids) - len(targets)
    if not targets:
        flash('Удалять некого: себя и администраторов массово удалить нельзя', 'warning')
        return _back('admin_users')
    failed = set()
    try:
        for part in chunked(targets, 1000):
            res = fb_auth.delete_users(part)
            for err in getattr(res, 'errors', None) or []:
                failed.add(part[err.index])
                log.warning('Аккаунт %s не удалён: %s', part[err.index], err.reason)
    except Exception as e:
        flash_error('Не удалось удалить аккаунты входа', e)
        return _back('admin_users')
    done = [uid for uid in targets if uid not in failed]
    try:
        for part in chunked(done, 450):
            batch = db.batch()
            for uid in part:
                batch.delete(db.collection('users').document(uid))
            batch.commit()
    except Exception as e:
        flash_error('Аккаунты удалены, но профили — не все. Повторите удаление', e)
    _converted, cleanup_failed = _cleanup_applicant_data({uid: docs.get(uid) for uid in done})
    if cleanup_failed:
        flash('Данные абитуриента очистились не у всех зачисленных (%d) — сообщите разработчику' % cleanup_failed,
              'warning')
    _invalidate_people({(docs.get(uid) or {}).get('role') for uid in done})
    audit('bulk_delete', 'user', None, {'ids': done, 'failed': sorted(failed)})
    msg = 'Удалено пользователей: %d' % len(done)
    if skipped:
        msg += '; пропущено (вы сами, администраторы, не найденные): %d' % skipped
    if failed:
        msg += '; не удалось удалить: %d' % len(failed)
    flash(msg, 'success' if not failed else 'warning')
    return _back('admin_users')


# ==================== ПРЕПОДАВАТЕЛИ ====================

def _account_states(uids):
    """Аккаунты Firebase Auth: {uid: {'disabled': bool}} — только у кого вход есть (нет ключа — входа нет).
    None — Auth недоступен. get_users пачками по 100; кэш 5 мин дополняется недостающими uid, поэтому
    список преподавателей и страницы пользователей не вытесняют друг друга."""
    uids = set(u for u in uids if u)
    try:
        cached = cache.get(ACCOUNTS_KEY) or {'uids': frozenset(), 'found': {}}
        missing = sorted(uids - cached['uids'])
        if missing:
            found = dict(cached['found'])
            for part in chunked(missing, 100):
                res = fb_auth.get_users([fb_auth.UidIdentifier(u) for u in part])
                for user in res.users:
                    found[user.uid] = {'disabled': bool(user.disabled)}
            cached = cache.set(ACCOUNTS_KEY, {'uids': cached['uids'] | frozenset(missing), 'found': found},
                               ttl=USERS_TTL)
        return {u: st for u, st in cached['found'].items() if u in uids}
    except Exception:
        log.exception('Не удалось проверить аккаунты входа')
        return None


def _accounts_with_login(uids):
    """Множество uid, у которых есть аккаунт Firebase Auth. None — Auth недоступен."""
    states = _account_states(uids)
    return None if states is None else set(states)


def _week_split(lessons):
    """Пар в неделю-числитель и неделю-знаменатель: (каждую + Ч, каждую + З) — для подсказки к нагрузке."""
    always = sum(1 for l in lessons if l.get('week') == 'always')
    num = sum(1 for l in lessons if l.get('week') == 'numerator')
    den = sum(1 for l in lessons if l.get('week') == 'denominator')
    return always + num, always + den


def _teacher_rows():
    rows = [dict(r) for r in _users_snapshot() if r['role'] == 'teacher']
    accounts = _account_states([r['id'] for r in rows])
    snap = _snapshot_safe()
    for r in rows:
        lessons = snap.by_teacher.get('uid:' + r['id'], []) if snap is not None else []
        r['lessons'] = len(lessons)
        r['load'] = sum(_week_weight(l) for l in lessons)
        r['load_label'] = _fmt_load(r['load'])
        r['week_num'], r['week_den'] = _week_split(lessons)
        r['groups'] = sorted({l['groupId'] for l in lessons if l['groupId']}, key=group_sort_key)
        r['subjects'] = sorted({l['subject'] for l in lessons if l['subject']}, key=norm_text)
        r['has_account'] = None if accounts is None else r['id'] in accounts
        r['disabled'] = bool(accounts and (accounts.get(r['id']) or {}).get('disabled'))
    return rows


def _teacher_namesakes(name, rows=None):
    """Преподаватели с тем же ФИО (без учёта регистра, пробелов и «ё») — защита от дублей в справочнике."""
    key = norm_text(name)
    if not key:
        return []
    rows = rows if rows is not None else [r for r in _users_snapshot() if r['role'] == 'teacher']
    return [r for r in rows if norm_text(r['name']) == key]


def _render_teachers(add_values=None, add_errors=None, namesakes=None):
    """Список преподавателей; add_* — форма добавления, раскрытая после ошибки (без redirect, значения на месте)."""
    try:
        rows = _teacher_rows()
    except Exception as e:
        flash_error('Не удалось загрузить преподавателей', e)
        rows = []
    q = (request.args.get('q') or '').strip()[:100]
    account = request.args.get('account') or ''
    lessons = request.args.get('lessons') or ''
    sort = request.args.get('sort') if request.args.get('sort') in ('name', 'load') else 'name'
    items = [r for r in rows if _matches(q, r['hay'] + ' ' + norm_text(' '.join(r['subjects'])))]
    if account == 'yes':
        items = [r for r in items if r['has_account']]
    elif account == 'no':
        items = [r for r in items if r['has_account'] is False]
    if lessons == 'no':
        items = [r for r in items if not r['lessons']]
    elif lessons == 'yes':
        items = [r for r in items if r['lessons']]
    if sort == 'load':
        items.sort(key=lambda r: (-r['load'], norm_text(r['name'])))
    else:
        items.sort(key=lambda r: norm_text(r['name']))
    if request.method == 'GET' and request.args.get('format') == 'csv':
        audit('export', 'teacher', None, {'format': 'csv', 'rows': len(items)})
        return csv_response('преподаватели.csv',
                            ['ФИО', 'Email', 'Телефон', 'Вход', 'Пар в неделю', 'Занятий', 'Группы', 'Дисциплины',
                             'uid'],
                            [[r['name'], r['email'], r['phone'], {True: 'есть', False: 'нет'}.get(r['has_account'], '?'),
                              r['load_label'], r['lessons'], ', '.join(r['groups']), ', '.join(r['subjects']), r['id']]
                             for r in items])
    page = paginate_list(items, request.args.get('page', 1), PER_PAGE)
    stats = {
        'total': len(rows),
        'with_account': sum(1 for r in rows if r['has_account']),
        'no_account': sum(1 for r in rows if r['has_account'] is False),
        'no_lessons': sum(1 for r in rows if not r['lessons']),
        'avg_load': _fmt_load(round(sum(r['load'] for r in rows) / len(rows), 1)) if rows else '0',
        'accounts_unknown': any(r['has_account'] is None for r in rows),
    }
    dup_names = {k for k, n in Counter(norm_text(r['name']) for r in rows if r['name']).items() if n > 1}
    for r in rows:
        r['dup'] = norm_text(r['name']) in dup_names
    return render_template('teachers.html', page=page, teachers=page.items, q=q, account=account, sort=sort,
                           lessons=lessons, stats=stats, filtered=len(items),
                           open_add=bool(request.args.get('add')) or bool(add_errors) or bool(namesakes),
                           add_values=add_values or {}, add_errors=add_errors or {},
                           namesakes=namesakes or [])


@routes.route('/teachers', methods=['GET', 'POST'])
@admin_required
def teachers_list():
    """Преподаватели: поиск, наличие входа, нагрузка по расписанию (пар в неделю, группы, дисциплины).
    POST — форма «Добавить» из модалки списка: при ошибке страница рендерится по адресу списка, поэтому
    ссылки фильтров, CSV и пагинации остаются рабочими (/teachers/add — прежний адрес, тоже работает)."""
    if request.method == 'POST':
        return teacher_add()
    return _render_teachers()


@routes.route('/teachers/add', methods=['POST'])
@admin_required
def teacher_add():
    """Новый преподаватель. С email и паролем — сразу с входом в приложение; без пароля — только
    карточка (вход можно создать позже на странице правки, uid сохранится).
    Ошибка — список с раскрытой формой и ошибками по полям (значения не теряются); тёзка в справочнике —
    вопрос «всё равно добавить?» (скрытое confirm_duplicate=1). Успех — карточка преподавателя."""
    name = _clean(request.form.get('name'), 120)
    email = (request.form.get('email') or '').strip().lower()
    password = request.form.get('password') or ''
    phone = _clean(request.form.get('phone'), 20)
    values = {'name': name, 'email': email, 'phone': phone}
    errors = {}
    if len(name) < 2:
        errors['name'] = 'Укажите ФИО (не короче 2 символов)'
    if email and (not EMAIL_RE.match(email) or len(email) > 120):
        errors['email'] = 'Некорректный email'
    if password and not email:
        errors['email'] = 'Для входа в приложение нужен email'
    if password and _password_problem(password):
        errors['password'] = _password_problem(password)
    if errors:
        return _render_teachers(values, errors)
    if request.form.get('confirm_duplicate') != '1':
        try:
            same = _teacher_namesakes(name)
        except Exception:
            log.exception('Проверка тёзок не удалась')
            same = []
        if same:
            return _render_teachers(values, {}, namesakes=same)
    created_auth = False
    if email and password:
        try:
            uid = fb_auth.create_user(email=email, password=password, display_name=name).uid
            created_auth = True
        except fb_auth.EmailAlreadyExistsError:
            return _render_teachers(values, {'email': 'Аккаунт с таким email уже есть — укажите другой'})
        except ValueError:
            return _render_teachers(values, {'email': 'Firebase не принял email или пароль — проверьте их'})
        except Exception as e:
            flash_error('Не удалось создать аккаунт входа', e)
            return _render_teachers(values, {'_form': 'Преподаватель не добавлен'})
    else:
        uid = db.collection('users').document().id
    doc = {'name': name, 'role': 'teacher', 'createdAt': SERVER_TIMESTAMP, 'updatedAt': SERVER_TIMESTAMP}
    if email:
        doc['email'] = email
    if phone:
        doc['phone'] = phone
    try:
        db.collection('users').document(uid).set(doc)
    except Exception as e:
        if created_auth:
            try:
                fb_auth.delete_user(uid)
            except Exception:
                log.exception('Откат аккаунта %s не удался', uid)
        flash_error('Не удалось сохранить преподавателя', e)
        return _render_teachers(values, {'_form': 'Преподаватель не добавлен'})
    _invalidate_people(['teacher'])
    audit('create', 'teacher', uid, {'name': name, 'login': created_auth})
    flash_action('Преподаватель «%s» добавлен%s' % (name, ' с входом в приложение' if created_auth else ''),
                 'Добавить занятия', href=nav_url('schedule_builder'))
    return redirect(url_for('teacher_edit', uid=uid))


@routes.route('/teachers/edit/<uid>', methods=['GET', 'POST'])
@admin_required
def teacher_edit(uid):
    """Правка преподавателя. uid не меняется никогда (на него ссылается расписание): если входа
    не было, аккаунт Auth создаётся с ТЕМ ЖЕ uid."""
    ref = db.collection('users').document(uid)
    try:
        snap = ref.get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить преподавателя', e)
        return redirect(url_for('teachers_list'))
    if not snap.exists:
        flash('Преподаватель не найден — возможно, его уже удалили', 'error')
        return redirect(url_for('teachers_list'))
    data = snap.to_dict() or {}
    if data.get('role') != 'teacher':
        return redirect(url_for('admin_edit_user', user_id=uid))
    account = _auth_info(uid)
    has_account = bool(account) and not account.get('error')
    errors = {}
    values = {'name': data.get('name') or '', 'email': (account or {}).get('email') or data.get('email') or '',
              'phone': data.get('phone') or ''}
    if request.method == 'POST':
        name = _clean(request.form.get('name'), 120)
        email = (request.form.get('email') or '').strip().lower()
        password = request.form.get('password') or ''
        phone = _clean(request.form.get('phone'), 20)
        values = {'name': name, 'email': email, 'phone': phone}
        if len(name) < 2:
            errors['name'] = 'Укажите ФИО'
        if email and not EMAIL_RE.match(email):
            errors['email'] = 'Некорректный email'
        if password and _password_problem(password):
            errors['password'] = _password_problem(password)
        if account and account.get('error'):
            errors['_form'] = 'Сервис входа недоступен — попробуйте позже'
        if has_account and not email:
            errors['email'] = 'У преподавателя есть вход — email обязателен'
        if not has_account and password and not email:
            errors['email'] = 'Чтобы создать вход, укажите email'
        if not errors:
            created = False
            try:
                if has_account:
                    upd = {}
                    if email != (account.get('email') or ''):
                        upd['email'] = email
                    if password:
                        upd['password'] = password
                    if name != (data.get('name') or ''):
                        upd['display_name'] = name
                    if upd:
                        fb_auth.update_user(uid, **upd)
                elif email and password:
                    fb_auth.create_user(uid=uid, email=email, password=password, display_name=name)
                    created = True
                ref.update({'name': name, 'email': email or DELETE_FIELD, 'phone': phone or DELETE_FIELD,
                            'updatedAt': SERVER_TIMESTAMP})
            except fb_auth.EmailAlreadyExistsError:
                errors['email'] = 'Этот email уже занят другим аккаунтом'
            except Exception as e:
                flash_error('Не удалось сохранить преподавателя', e)
                errors['_form'] = 'Изменения не сохранены'
            else:
                _invalidate_people(['teacher'])
                audit('update', 'teacher', uid, {'name': name, 'login_created': created,
                                                  'password_changed': bool(password)})
                flash('Создан вход в приложение для «%s»' % name if created else 'Данные преподавателя сохранены',
                      'success')
                return redirect(_next_url('teachers_list'))
    snapshot = _snapshot_safe()
    lessons = snapshot.by_teacher.get('uid:' + uid, []) if snapshot is not None else []
    days = OrderedDict()
    for l in lessons:
        days.setdefault(l['weekday'], []).append(l)
    week_num, week_den = _week_split(lessons)
    # «Передать занятия…» (PPL-6): форма шлёт POST /schedule/bulk (action=teacher, SCH-11) — показываем, только
    # если модуль расписания уже умеет это действие.
    try:
        from .routes_schedule import BULK_ACTIONS
        bulk_teacher_ready = 'teacher' in BULK_ACTIONS
    except Exception:
        bulk_teacher_ready = False
    replacements = []
    if lessons and bulk_teacher_ready:
        try:
            replacements = [(t_id, t_name) for t_id, t_name in get_all_teachers().items() if t_id != uid and t_name]
        except Exception:
            log.exception('Справочник преподавателей недоступен')
    try:
        namesakes = [r for r in _teacher_namesakes(data.get('name')) if r['id'] != uid]
    except Exception:
        namesakes = []
    return render_template('teacher_edit.html', teacher={'id': uid, **data}, values=values, errors=errors,
                           account=account, has_account=has_account, lessons=lessons, days=days,
                           clashes=_teacher_clashes(days), week_num=week_num, week_den=week_den,
                           day_names=DAY_NAMES_FULL, load_label=_fmt_load(sum(_week_weight(l) for l in lessons)),
                           groups=sorted({l['groupId'] for l in lessons if l['groupId']}, key=group_sort_key),
                           bulk_teacher_ready=bulk_teacher_ready, replacements=replacements, namesakes=namesakes,
                           next_url=safe_next_url(request.values.get('next')))


def _teacher_clashes(days):
    """id занятий преподавателя, которые пересекаются по времени с другим его занятием в тот же день
    (с учётом числителя/знаменателя и дат разовых занятий) — подсветка на странице преподавателя.
    Когда пары идут одновременно — как в разделе «Накладки» (conflicts.pair_weeks): занятие только с date
    (старый веб) приложение показывает каждую неделю, разовое — только явный one_off + date."""
    out = set()
    for items in days.values():
        for i, a in enumerate(items):
            for b in items[i + 1:]:
                if conflicts.pair_weeks(a, b) is None:
                    continue
                if (a['room_key'] and a['room_key'] == b['room_key']
                        and norm_text(a['subject']) == norm_text(b['subject']) and a['groupId'] != b['groupId']):
                    continue  # поток: одна аудитория и дисциплина у нескольких групп — не накладка
                if intervals_overlap(a['start'], a['end'], b['start'], b['end']):
                    out.update((a['id'], b['id']))
    return out


@routes.route('/teachers/delete/<uid>', methods=['POST'])
@admin_required
def teacher_delete(uid):
    """Удаление преподавателя (аккаунт входа + карточка). Если у него есть занятия — только с
    подтверждением force=1 (занятия останутся без преподавателя — их видно в «Качестве данных»)."""
    try:
        snap = db.collection('users').document(uid).get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить преподавателя', e)
        return redirect(url_for('teachers_list'))
    if not snap.exists:
        flash('Преподаватель не найден — возможно, его уже удалили', 'error')
        return redirect(url_for('teachers_list'))
    data = snap.to_dict() or {}
    name = data.get('name') or uid
    snapshot = _snapshot_safe()
    lessons = len(snapshot.by_teacher.get('uid:' + uid, [])) if snapshot is not None else 0
    if lessons and request.form.get('force') != '1':
        flash('У «%s» %d занятий в расписании. Переназначьте их или подтвердите удаление ещё раз'
              % (name, lessons), 'error')
        return redirect_back('teachers_list')
    try:
        _delete_account_and_profile(uid)
    except Exception as e:
        flash_error('Не удалось удалить преподавателя', e)
        return redirect_back('teachers_list')
    _invalidate_people(['teacher'])
    audit('delete', 'teacher', uid, {'name': name, 'lessons': lessons})
    if lessons:
        flash('Преподаватель «%s» удалён. %d занятий остались без преподавателя — назначьте замену в расписании'
              % (name, lessons), 'warning')
    else:
        flash('Преподаватель «%s» удалён' % name, 'success')
    target = next_target()   # обратно в отфильтрованный список (но не на карточку удалённого)
    return redirect(target if target and '/teachers/edit/%s' % uid not in target else url_for('teachers_list'))


# ==================== МАССОВОЕ ПЕРЕИМЕНОВАНИЕ В РАСПИСАНИИ ====================

def _rewrite_schedules(field, match, new_value):
    """Заменить значение поля (groupId / subject) во всех подходящих занятиях — тихо (notify:false,
    без пуша на каждую пару, D15) и с журналом schedule_logs. match(value) → bool. → число занятий."""
    snap = load_lessons_snapshot(force=True)
    ids = [l['id'] for l in snap.lessons if match(l['raw'].get(field))]
    if not ids:
        return 0
    fresh = get_docs('schedules', ids)
    targets = [(i, d) for i, d in fresh.items() if match(d.get(field)) and d.get(field) != new_value]
    if not targets:
        return 0
    try:
        from .routes_schedule import ScheduleBatch
    except Exception:  # модуль расписания не загрузился — пишем напрямую, без журнала
        ScheduleBatch = None
        log.warning('ScheduleBatch недоступен — переименование в расписании без журнала', exc_info=True)
    if ScheduleBatch is not None:
        sb = ScheduleBatch(quiet=True)
        for doc_id, old in targets:
            sb.update(doc_id, old, {field: new_value})
        sb.commit(notify_users=False)
        return len(targets)
    refs = [db.collection('schedules').document(doc_id) for doc_id, _old in targets]
    try:
        for part in chunked(refs, 450):
            batch = db.batch()
            for ref in part:
                batch.update(ref, {field: new_value, 'notify': False, 'updatedAt': SERVER_TIMESTAMP})
            batch.commit()
        for part in chunked(refs, 450):
            batch = db.batch()
            for ref in part:
                batch.update(ref, {'notify': DELETE_FIELD})
            batch.commit()
    finally:
        invalidate_schedule_caches()
    return len(targets)


# ==================== ГРУППЫ ====================

_YEAR_RE = re.compile(r'(\d{2,4})\s*-\s*\d')
CALENDAR_FOLDER = 'academic_calendars'   # Storage: academic_calendars/<slug>/<unixMillis>.pdf (routes_calendar)


def _group_course(name):
    """Курс по году поступления: 'ПО 24-1' в 2026/27 учебном году → 3 (None — не распознано)."""
    m = _YEAR_RE.search(str(name or ''))
    if not m:
        return None
    year = int(m.group(1))
    if year < 100:
        year += 2000
    today = today_almaty()
    start = today.year if today.month >= 9 else today.year - 1
    course = start - year + 1
    return course if 1 <= course <= 6 else None


def _group_major(name):
    m = re.match(r'\s*([^\d\s]+)', str(name or ''))
    return m.group(1).upper() if m else ''


def _groups_directory():
    """groups_list: {название: данные} (кэш; сбрасывается вместе со справочником групп)."""
    def load():
        return {d.id: d.to_dict() or {} for d in db.collection('groups_list').stream(**fs_opts(True))}
    return cache.get_or_set(GROUPS_DIR_KEY, load)


def _calendar_paths(data):
    """Путь объекта PDF академического календаря из groups_list/{группа}.academicCalendar → [путь] или [].
    Только свой каталог academic_calendars/: чужие файлы по ошибочному полю не удаляем."""
    cal = (data or {}).get('academicCalendar')
    if not isinstance(cal, dict):
        return []
    path = str(cal.get('path') or '') or storage_path_from_url(cal.get('url')) or ''
    parts = path.split('/')
    return [path] if len(parts) > 1 and parts[0] == CALENDAR_FOLDER and all(parts) and '..' not in parts else []


def _group_name_problem(name):
    if not name:
        return 'Введите название группы'
    if len(name) > GROUP_NAME_MAX:
        return 'Название группы — не длиннее %d символов' % GROUP_NAME_MAX
    if '/' in name or name in ('.', '..') or name.startswith('__'):
        return 'В названии группы нельзя использовать «/»'
    return None


def _group_rows():
    directory = _groups_directory()
    snap = _snapshot_safe()
    students = Counter(r['group'] for r in _users_snapshot() if r['role'] == 'student' and r['group'])
    names = set(directory) | set(students)
    if snap is not None:
        names |= {g for g in snap.by_group if g}
    rows = []
    for name in sorted(names, key=lambda n: (group_sort_key(n), n)):
        lessons = snap.by_group.get(name, []) if snap is not None else []
        rows.append({
            'name': name, 'in_directory': name in directory, 'students': students.get(name, 0),
            'lessons': len(lessons), 'load': _fmt_load(sum(_week_weight(l) for l in lessons)),
            'teachers': len({l['teacher_key'] for l in lessons if l['teacher_key']}),
            'course': _group_course(name), 'major': _group_major(name), 'hay': norm_text(name),
            'calendar': isinstance((directory.get(name) or {}).get('academicCalendar'), dict),
        })
    return rows


@routes.route('/groups')
@admin_required
def groups():
    """Группы: справочник groups_list + группы из расписания и профилей студентов, с числом
    студентов и занятий, фильтрами и действиями (создать, переименовать везде, удалить)."""
    try:
        rows = _group_rows()
        no_group = sum(1 for r in _users_snapshot() if r['role'] == 'student' and not r['group'])
    except Exception as e:
        flash_error('Не удалось загрузить группы', e)
        rows, no_group = [], 0
    q = (request.args.get('q') or '').strip()[:60]
    major = request.args.get('major') or ''
    course = request.args.get('course') or ''
    state = request.args.get('state') or ''
    items = [r for r in rows if _matches(q, r['hay'])
             and (not major or r['major'] == major)
             and (not course or str(r['course'] or '') == course)
             and (not state or (state == 'no_students' and not r['students'])
                  or (state == 'no_schedule' and not r['lessons'])
                  or (state == 'not_in_directory' and not r['in_directory']))]
    stats = {
        'groups': len(rows), 'students': sum(r['students'] for r in rows), 'no_group': no_group,
        'no_schedule': sum(1 for r in rows if not r['lessons']),
        'not_in_directory': sum(1 for r in rows if not r['in_directory']),
    }
    majors = sorted({r['major'] for r in rows if r['major']})
    courses = [(str(c), '%d курс' % c) for c in sorted({r['course'] for r in rows if r['course']})]
    return render_template('groups.html', rows=items, stats=stats, q=q, major=major, course=course, state=state,
                           majors=majors, courses=courses, max_len=GROUP_NAME_MAX)


@routes.route('/groups/create', methods=['POST'])
@admin_required
def group_create():
    name = _clean(request.form.get('group_name'), GROUP_NAME_MAX + 1)
    problem = _group_name_problem(name)
    if not problem:
        directory = _groups_directory()
        same = [g for g in set(directory) | set(_known_groups()) if group_key(g) == group_key(name)]
        if name in directory:
            problem = 'Группа «%s» уже есть' % name
        elif same and name not in same:
            problem = 'Похожая группа уже есть: «%s»' % same[0]
    if problem:
        flash(problem, 'error')
        return _back('groups')
    try:
        db.collection('groups_list').document(name).set({'name': name, 'createdAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось создать группу', e)
        return _back('groups')
    invalidate_directory_caches()
    _invalidate_people()
    audit('create', 'group', name)
    flash('Группа «%s» создана' % name, 'success')
    return _back('groups')


@routes.route('/groups/edit_name', methods=['POST'])
@admin_required
def group_rename():
    """Переименование группы везде: справочник groups_list, профили (users.group) и, по галочке,
    занятия расписания (schedules.groupId — тихо, с журналом, без пуша на каждую пару)."""
    old = (request.form.get('old_name') or '').strip()
    new = _clean(request.form.get('new_name'), GROUP_NAME_MAX + 1)
    # форма страницы шлёт галочку + маркер schedules_field; старые вызовы без поля — переименовываем везде
    with_schedules = request.form.get('schedules') == '1' if request.form.get('schedules_field') else True
    problem = _group_name_problem(new)
    if not old:
        problem = 'Не выбрана группа'
    elif not problem and new == old:
        problem = 'Название не изменилось'
    elif not problem:
        others = (set(_groups_directory()) | set(_known_groups())) - {old}
        clash = [g for g in others if group_key(g) == group_key(new)]
        if new in others or clash:
            problem = 'Группа «%s» уже есть — выберите другое название' % (clash[0] if clash else new)
    if problem:
        flash(problem, 'error')
        return _back('groups')
    moved_users = lessons = 0
    try:
        dir_ref = db.collection('groups_list').document(old)
        dir_snap = dir_ref.get(**fs_opts())
        data = dict(dir_snap.to_dict() or {}) if dir_snap.exists else {'createdAt': SERVER_TIMESTAMP}
        data.update({'name': new, 'updatedAt': SERVER_TIMESTAMP})
        batch = db.batch()
        batch.set(db.collection('groups_list').document(new), data)
        if dir_snap.exists:
            batch.delete(dir_ref)
        batch.commit()
        refs = [d.reference for d in where(db.collection('users'), 'group', '==', old).select([])
                .stream(**fs_opts(True))]
        for part in chunked(refs, 450):
            b = db.batch()
            for ref in part:
                b.update(ref, {'group': new, 'updatedAt': SERVER_TIMESTAMP})
            b.commit()
        moved_users = len(refs)
        if with_schedules:
            lessons = _rewrite_schedules('groupId', lambda v: str(v or '').strip() == old, new)
    except Exception as e:
        flash_error('Переименование прервано — проверьте группу «%s» и повторите' % old, e)
        invalidate_directory_caches()
        _invalidate_people(['student'])
        return _back('groups')
    invalidate_directory_caches()
    _invalidate_people(['student'])
    audit('rename', 'group', new, {'from': old, 'to': new, 'users': moved_users, 'lessons': lessons})
    flash('Группа «%s» переименована в «%s»: студентов — %d, занятий — %d' % (old, new, moved_users, lessons),
          'success')
    return redirect(url_for('groups', q=new))


@routes.route('/groups/delete/<group_name>', methods=['POST'])
@admin_required
def group_delete(group_name):
    """Удалить группу из справочника; у студентов группа очищается, PDF академического календаря удаляется
    из Storage. Занятия расписания остаются (их удаляют в разделе «Расписание», чтобы случайно не стереть сетку)."""
    try:
        group_ref = db.collection('groups_list').document(group_name)
        group_snap = group_ref.get(**fs_opts())
        files = _calendar_paths(group_snap.to_dict() if group_snap.exists else None)
        group_ref.delete()
        delete_storage_paths(files)   # PDF календаря уходит вместе с группой (объекта уже нет — не беда)
        refs = [d.reference for d in where(db.collection('users'), 'group', '==', group_name).select([])
                .stream(**fs_opts(True))]
        for part in chunked(refs, 450):
            batch = db.batch()
            for ref in part:
                batch.update(ref, {'group': DELETE_FIELD, 'updatedAt': SERVER_TIMESTAMP})
            batch.commit()
    except Exception as e:
        flash_error('Не удалось удалить группу', e)
        return redirect_back('groups')
    invalidate_directory_caches()
    _invalidate_people(['student'])
    snap = _snapshot_safe()
    lessons = len(snap.by_group.get(group_name, [])) if snap is not None else 0
    audit('delete', 'group', group_name, {'users': len(refs), 'lessons_left': lessons})
    msg = 'Группа «%s» удалена; студентов без группы: %d' % (group_name, len(refs))
    if lessons:
        msg += '. В расписании осталось %d занятий этой группы — удалите их в разделе «Расписание»' % lessons
    flash(msg, 'warning' if lessons else 'success')
    return _back('groups')


# ==================== ДИСЦИПЛИНЫ ====================

def _subjects():
    def load():
        rows = [{'id': d.id, 'name': str((d.to_dict() or {}).get('name') or '').strip()}
                for d in db.collection('subjects').stream(**fs_opts(True))]
        return sorted(rows, key=lambda s: norm_text(s['name']))
    return cache.get_or_set(SUBJECTS_KEY, load)


@routes.route('/subjects')
@admin_required
def subjects_list():
    """Дисциплины: справочник + сколько занятий, групп и преподавателей в расписании; дубли и
    дисциплины из расписания, которых нет в справочнике."""
    try:
        subjects = [dict(s) for s in _subjects()]
    except Exception as e:
        flash_error('Не удалось загрузить дисциплины', e)
        subjects = []
    snap = _snapshot_safe()
    usage, names_in_schedule = {}, {}
    for l in (snap.lessons if snap is not None else []):
        key = norm_text(l['subject'])
        if not key:
            continue
        u = usage.setdefault(key, {'lessons': 0, 'groups': set(), 'teachers': set()})
        u['lessons'] += 1
        if l['groupId']:
            u['groups'].add(l['groupId'])
        if l['teacher_key']:
            u['teachers'].add(l['teacher_key'])
        names_in_schedule.setdefault(key, l['subject'])
    dup = Counter(norm_text(s['name']) for s in subjects)
    for s in subjects:
        u = usage.get(norm_text(s['name'])) or {}
        s['lessons'] = u.get('lessons', 0)
        s['groups'] = len(u.get('groups', ()))
        s['teachers'] = len(u.get('teachers', ()))
        s['duplicate'] = dup[norm_text(s['name'])] > 1
    missing = sorted([(name, usage[key]['lessons']) for key, name in names_in_schedule.items() if key not in dup],
                     key=lambda x: norm_text(x[0]))
    state = request.args.get('state') or ''
    q = (request.args.get('q') or '').strip()[:100]
    items = [s for s in subjects if _matches(q, norm_text(s['name']))]
    if state == 'unused':
        items = [s for s in items if not s['lessons']]
    elif state == 'duplicates':
        items = [s for s in items if s['duplicate']]
    stats = {'total': len(subjects), 'used': sum(1 for s in subjects if s['lessons']),
             'unused': sum(1 for s in subjects if not s['lessons']),
             'duplicates': sum(1 for s in subjects if s['duplicate']), 'missing': len(missing)}
    return render_template('subjects.html', subjects=items, stats=stats, missing=missing, state=state, q=q,
                           max_len=SUBJECT_NAME_MAX)


@routes.route('/subjects/add', methods=['POST'])
@admin_required
def subject_add():
    """Добавить одну или несколько дисциплин (по одной на строку); дубли пропускаются."""
    raw = request.form.get('names')
    lines = (raw if raw is not None else request.form.get('name') or '').splitlines()
    existing = {norm_text(s['name']) for s in _subjects()}
    added, skipped, too_long = [], 0, 0
    for line in lines[:200]:
        name = _clean(line, SUBJECT_NAME_MAX + 1)
        if not name:
            continue
        if len(name) > SUBJECT_NAME_MAX:
            too_long += 1
            continue
        key = norm_text(name)
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        added.append(name)
    if not added:
        flash('Ничего не добавлено: %s' % ('такие дисциплины уже есть' if skipped else 'введите название'),
              'warning' if skipped else 'error')
        return _back('subjects_list')
    try:
        for part in chunked(added, 450):
            batch = db.batch()
            for name in part:
                batch.set(db.collection('subjects').document(), {'name': name})
            batch.commit()
    except Exception as e:
        flash_error('Не удалось добавить дисциплины', e)
        return _back('subjects_list')
    invalidate_directory_caches()
    audit('create', 'subject', None, {'names': added})
    msg = ('Добавлено: %s' % ', '.join(added)) if len(added) <= 3 else 'Добавлено дисциплин: %d' % len(added)
    if skipped:
        msg += '; уже были: %d' % skipped
    if too_long:
        msg += '; слишком длинных: %d' % too_long
    flash(msg, 'success')
    return _back('subjects_list')


@routes.route('/subjects/edit/<s_id>', methods=['POST'])
@admin_required
def subject_edit(s_id):
    """Переименовать дисциплину; по галочке — и во всех занятиях расписания."""
    new_name = _clean(request.form.get('name'), SUBJECT_NAME_MAX + 1)
    with_schedules = request.form.get('schedules') == '1'
    if not new_name or len(new_name) > SUBJECT_NAME_MAX:
        flash('Название дисциплины — от 1 до %d символов' % SUBJECT_NAME_MAX, 'error')
        return _back('subjects_list')
    ref = db.collection('subjects').document(s_id)
    try:
        snap = ref.get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить дисциплину', e)
        return _back('subjects_list')
    if not snap.exists:
        flash('Дисциплина не найдена', 'error')
        return _back('subjects_list')
    old_name = str((snap.to_dict() or {}).get('name') or '').strip()
    clash = [s for s in _subjects() if s['id'] != s_id and norm_text(s['name']) == norm_text(new_name)]
    if clash:
        flash('Дисциплина «%s» уже есть' % clash[0]['name'], 'error')
        return _back('subjects_list')
    lessons = 0
    try:
        ref.update({'name': new_name})
        if with_schedules and old_name and old_name != new_name:
            old_key = norm_text(old_name)
            lessons = _rewrite_schedules('subject', lambda v: norm_text(v) == old_key, new_name)
    except Exception as e:
        flash_error('Не удалось переименовать дисциплину', e)
        invalidate_directory_caches()
        return _back('subjects_list')
    invalidate_directory_caches()
    audit('rename', 'subject', s_id, {'from': old_name, 'to': new_name, 'lessons': lessons})
    flash('Дисциплина переименована%s' % (' (занятий в расписании: %d)' % lessons if with_schedules else ''),
          'success')
    return _back('subjects_list')


@routes.route('/subjects/delete/<s_id>', methods=['POST'])
@admin_required
def subject_delete(s_id):
    ref = db.collection('subjects').document(s_id)
    try:
        snap = ref.get(**fs_opts())
        name = str((snap.to_dict() or {}).get('name') or '') if snap.exists else ''
        ref.delete()
    except Exception as e:
        flash_error('Не удалось удалить дисциплину', e)
        return _back('subjects_list')
    invalidate_directory_caches()
    audit('delete', 'subject', s_id, {'name': name})
    flash('Дисциплина «%s» удалена из справочника (занятия в расписании не изменились)' % (name or s_id), 'success')
    return _back('subjects_list')


# ==================== АДМИНИСТРАЦИЯ ====================
# management/{id}: name, position, priority (int, 1 — первый), image (download-URL с токеном),
# imagePath (путь в Storage — для удаления), createdAt, updatedAt. Приложение берёт фото как
# photo ?? image, поэтому при смене фото старые поля photo/photo_url удаляются.

def _priority(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _management_photo(data):
    return str(data.get('photo') or data.get('image') or data.get('photo_url') or '').strip()


def _management(force=False):
    def load():
        rows = []
        for d in db.collection('management').stream(**fs_opts(True)):
            data = d.to_dict() or {}
            p = _priority(data.get('priority'))
            rows.append({'id': d.id, 'name': str(data.get('name') or ''), 'position': str(data.get('position') or ''),
                         'image': _management_photo(data), 'priority': p,
                         'sort': p if p is not None else 10 ** 6})
        rows.sort(key=lambda r: (r['sort'], norm_text(r['name'])))
        return rows
    if force:
        cache.invalidate(MANAGEMENT_KEY)
    return cache.get_or_set(MANAGEMENT_KEY, load)


def _own_photo_path(data):
    """Путь фото в Storage, если оно лежит в папке management/ (чужие файлы не трогаем)."""
    path = str(data.get('imagePath') or '') or storage_path_from_url(_management_photo(data)) or ''
    return path if path.startswith(MANAGEMENT_FOLDER + '/') else None


@routes.route('/administration')
@admin_required
def administration():
    """Администрация колледжа (экран «Руководство» в приложении): карточки в порядке показа,
    перетаскивание с сохранением порядка. Страница ничего не пишет в базу."""
    try:
        people = _management()
    except Exception as e:
        flash_error('Не удалось загрузить администрацию', e)
        people = []
    return render_template('administration.html', admins=people)


def _management_form(admin=None):
    """Форма добавления/правки члена администрации. → redirect или страница формы."""
    is_new = admin is None
    errors = {}
    values = {'name': (admin or {}).get('name') or '', 'position': (admin or {}).get('position') or ''}
    if request.method == 'POST':
        name = _clean(request.form.get('name'), 121)
        position = _clean(request.form.get('position'), 161)
        values = {'name': name, 'position': position}
        if len(name) < 2 or len(name) > 120:
            errors['name'] = 'ФИО — от 2 до 120 символов'
        if not position or len(position) > 160:
            errors['position'] = 'Должность — от 1 до 160 символов'
        coll = db.collection('management')
        ref = coll.document(admin['id']) if admin else coll.document()
        photo = request.files.get('photo')
        remove = request.form.get('remove_photo') == '1'
        uploaded = None
        if not errors and photo is not None and getattr(photo, 'filename', ''):
            try:
                uploaded = upload_image(photo, '%s/%s' % (MANAGEMENT_FOLDER, ref.id))
            except UploadError as e:
                errors['photo'] = str(e)
        if not errors:
            doc = {'name': name, 'position': position, 'updatedAt': SERVER_TIMESTAMP}
            old_path = _own_photo_path(admin['_raw']) if admin and (uploaded or remove) else None
            if uploaded:
                doc.update({'image': uploaded['url'], 'imagePath': uploaded['path']})
            elif remove:
                doc.update({'image': '', 'imagePath': DELETE_FIELD})
            if admin and (uploaded or remove):
                doc.update({'photo': DELETE_FIELD, 'photo_url': DELETE_FIELD})
            try:
                if is_new:
                    known = [r['priority'] for r in _management(force=True) if r['priority'] is not None]
                    doc.update({'priority': (max(known) + 1) if known else 1, 'createdAt': SERVER_TIMESTAMP})
                    doc.setdefault('image', '')
                    if doc.get('imagePath') is DELETE_FIELD:
                        doc.pop('imagePath')
                    ref.set(doc)
                else:
                    ref.update(doc)
            except Exception as e:
                if uploaded:
                    delete_storage_paths([uploaded['path']])
                flash_error('Не удалось сохранить', e)
                errors['_form'] = 'Изменения не сохранены'
            else:
                if old_path and (not uploaded or old_path != uploaded['path']):
                    delete_storage_paths([old_path])
                cache.invalidate(MANAGEMENT_KEY)
                audit('create' if is_new else 'update', 'management', ref.id,
                      {'name': name, 'photo': 'new' if uploaded else ('removed' if remove else 'kept')})
                flash(('«%s» добавлен(а) в администрацию' % name) if is_new else 'Данные сохранены', 'success')
                return redirect(url_for('administration'))
    return render_template('administration_form.html', admin=admin, values=values, errors=errors,
                           action='add' if is_new else 'edit')


@routes.route('/administration/add', methods=['GET', 'POST'])
@admin_required
def administration_add():
    return _management_form(None)


@routes.route('/administration/edit/<admin_id>', methods=['GET', 'POST'])
@admin_required
def administration_edit(admin_id):
    try:
        snap = db.collection('management').document(admin_id).get(**fs_opts())
    except Exception as e:
        flash_error('Не удалось загрузить карточку', e)
        return redirect(url_for('administration'))
    if not snap.exists:
        flash('Член администрации не найден', 'error')
        return redirect(url_for('administration'))
    data = snap.to_dict() or {}
    admin = {'id': snap.id, 'name': data.get('name') or '', 'position': data.get('position') or '',
             'image': _management_photo(data), '_raw': data}
    return _management_form(admin)


@routes.route('/administration/delete/<admin_id>', methods=['POST'])
@admin_required
def administration_delete(admin_id):
    ref = db.collection('management').document(admin_id)
    try:
        snap = ref.get(**fs_opts())
        data = (snap.to_dict() or {}) if snap.exists else {}
        ref.delete()
    except Exception as e:
        flash_error('Не удалось удалить', e)
        return redirect(url_for('administration'))
    path = _own_photo_path(data)
    if path:
        delete_storage_paths([path])
    cache.invalidate(MANAGEMENT_KEY)
    audit('delete', 'management', admin_id, {'name': data.get('name') or ''})
    flash('«%s» удалён(а) из администрации' % (data.get('name') or admin_id), 'success')
    return redirect(url_for('administration'))


@routes.route('/administration/update_order', methods=['POST'])
@admin_required
def administration_update_order():
    """JSON {order: [id, …]} → priority = позиция (1…n). Ответ {success, ok}."""
    payload = request.get_json(silent=True) or {}
    order = payload.get('order')
    if (not isinstance(order, list) or not order or len(order) > 500
            or not all(isinstance(x, str) and 0 < len(x) <= 200 for x in order)):
        return jsonify({'success': False, 'ok': False, 'error': 'Неверный порядок'}), 400
    order = list(dict.fromkeys(order))
    try:
        existing = {r['id'] for r in _management(force=True)}
        batch = db.batch()
        n = 0
        for index, admin_id in enumerate(order):
            if admin_id in existing:
                batch.update(db.collection('management').document(admin_id), {'priority': index + 1})
                n += 1
        if n:
            batch.commit()
    except Exception:
        log.exception('Не удалось сохранить порядок администрации')
        return jsonify({'success': False, 'ok': False, 'error': 'Не удалось сохранить порядок'}), 500
    cache.invalidate(MANAGEMENT_KEY)
    audit('reorder', 'management', None, {'count': n})
    return jsonify({'success': True, 'ok': True, 'count': n})
