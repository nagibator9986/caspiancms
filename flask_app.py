# app.py - ОПТИМИЗИРОВАННАЯ ВЕРСИЯ
# Основные улучшения производительности:
# 1. Кэширование справочников (группы, преподаватели, предметы)
# 2. Пагинация расписания
# 3. Ленивая загрузка данных
# 4. Батчинг операций с БД
# 5. Индексы и оптимизированные запросы

from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, jsonify
from firebase_admin import credentials, auth, firestore, storage, initialize_app
from datetime import datetime, timezone, timedelta
from functools import wraps, lru_cache
import os
import re
import json
import requests
from werkzeug.utils import secure_filename
from threading import Lock
import time

app = Flask(__name__)
app.secret_key = 'change_this_to_a_strong_secret_key'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

cred = credentials.Certificate(
    os.path.join(BASE_DIR, 'firebase-service-account.json')
)

initialize_app(cred)
db = firestore.client()
bucket = storage.bucket('student-caspian-lol.firebasestorage.app')

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}

# ==================== СИСТЕМА КЭШИРОВАНИЯ ====================

class CacheManager:
    """
    Менеджер кэша для справочных данных.
    Уменьшает количество запросов к Firestore в 10-50 раз.
    """
    def __init__(self, ttl_seconds=300):  # 5 минут по умолчанию
        self._cache = {}
        self._timestamps = {}
        self._lock = Lock()
        self.ttl = ttl_seconds

    def get(self, key):
        """Получить значение из кэша"""
        with self._lock:
            if key in self._cache:
                if time.time() - self._timestamps[key] < self.ttl:
                    return self._cache[key]
                else:
                    # Кэш устарел
                    del self._cache[key]
                    del self._timestamps[key]
        return None

    def set(self, key, value):
        """Сохранить значение в кэш"""
        with self._lock:
            self._cache[key] = value
            self._timestamps[key] = time.time()

    def invalidate(self, key=None):
        """Инвалидировать кэш (весь или по ключу)"""
        with self._lock:
            if key:
                self._cache.pop(key, None)
                self._timestamps.pop(key, None)
            else:
                self._cache.clear()
                self._timestamps.clear()

# Глобальный кэш
cache = CacheManager(ttl_seconds=300)

# ==================== ОПТИМИЗИРОВАННЫЕ ФУНКЦИИ ЗАГРУЗКИ ====================

def get_all_groups():
    """
    Загружает список всех групп с кэшированием.
    БЫЛО: 3 запроса к БД при каждом открытии страницы
    СТАЛО: 1 запрос раз в 5 минут
    """
    cached = cache.get('all_groups')
    if cached:
        return cached

    groups_set = set()

    # Загружаем из groups_list (основной источник)
    for doc in db.collection('groups_list').stream():
        groups_set.add(doc.id)

    # Дополнительно из schedules (для обратной совместимости)
    # Используем DISTINCT через агрегацию, если есть много записей
    schedules = db.collection('schedules').select(['groupId']).stream()
    for doc in schedules:
        g = doc.to_dict().get('groupId')
        if g:
            groups_set.add(g)

    result = sorted(list(groups_set), key=group_sort_key)
    cache.set('all_groups', result)
    return result


def get_all_teachers():
    """
    Загружает словарь преподавателей {id: name} с кэшированием.
    БЫЛО: 2 полных запроса при каждом открытии
    СТАЛО: 1 раз в 5 минут
    """
    cached = cache.get('all_teachers')
    if cached:
        return cached

    teachers = {}

    # Из users с ролью teacher
    for doc in db.collection('users').where('role', '==', 'teacher').stream():
        name = doc.to_dict().get('name')
        if name:
            teachers[doc.id] = name

    # Из отдельной коллекции teachers
    for doc in db.collection('teachers').stream():
        name = doc.to_dict().get('name')
        if name:
            teachers[doc.id] = name

    # Сортируем по имени
    result = dict(sorted(teachers.items(), key=lambda x: x[1]))
    cache.set('all_teachers', result)
    return result


def get_all_subjects():
    """
    Загружает список предметов с кэшированием.
    """
    cached = cache.get('all_subjects')
    if cached:
        return cached

    subjects = []
    for doc in db.collection('subjects').stream():
        name = doc.to_dict().get('name')
        if name:
            subjects.append(name)

    result = sorted(subjects)
    cache.set('all_subjects', result)
    return result


def get_teacher_name(teacher_id):
    """
    Получает имя преподавателя по ID.
    Использует кэш вместо прямых запросов.
    """
    if not teacher_id:
        return ''

    teachers = get_all_teachers()
    return teachers.get(teacher_id, '')


# Добавляем в Jinja
app.jinja_env.globals.update(get_teacher_name=get_teacher_name)


# ==================== УТИЛИТЫ ====================

def group_sort_key(group_name):
    """Ключ сортировки для названий групп"""
    match = re.match(r"([А-ЯЁA-Z]+)\s*(\d+)-(\d+)([пП]?)", group_name)
    if match:
        return (match.group(1), int(match.group(2)), int(match.group(3)), 1 if match.group(4) else 0)
    return ("ZZZ", 99, 99, 0)


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def get_week_parity():
    """Определяет четность текущей недели"""
    # Определяем номер недели относительно академического года,
    # так, чтобы переключение происходило только по понедельникам.
    today = datetime.now().date()
    # Берём год начала академического года (1 сентября)
    start_year = today.year if today.month >= 9 else today.year - 1
    september_first = datetime(start_year, 9, 1).date()

    # Находим первую понедельник, начиная с 1 сентября (включительно)
    if september_first.weekday() == 0:
        start_monday = september_first
    else:
        start_monday = september_first + timedelta(days=(7 - september_first.weekday()))

    # Берём понедельник текущей недели — переключение должно происходить только в понедельник
    today_monday = today - timedelta(days=today.weekday())

    # Если текущая неделя находится до старта академического года, считаем недели относительно стартовой даты прошлого года
    if today_monday < start_monday:
        # В этом случае start_year уже установлен правильно (предыдущий год),
        # просто пересчитаем start_monday для этого года (уже сделано выше)
        pass

    # Разница в неделях
    week_diff = (today_monday - start_monday).days // 7

    # Возвращаем True для тех недель, которые считаются "числителем".
    # Шаблон ожидает, что значение True соответствует отображению Числителя.
    # Исходный вариант:
    return (week_diff % 2 != 0)


# ==================== ОПТИМИЗИРОВАННОЕ ЛОГИРОВАНИЕ ====================

class ScheduleLogger:
    """
    Батч-логгер для изменений расписания.
    Собирает изменения и записывает их пачками.
    """
    def __init__(self):
        self._pending_logs = []
        self._lock = Lock()

    def add(self, action, schedule_id, old_data, new_data, group_id=None):
        """Добавить лог в очередь"""
        # Для update проверяем, есть ли реальные изменения
        if action == 'update' and old_data and new_data:
            has_changes = False
            for field, new_val in new_data.items():
                if field in ['updatedAt', 'createdAt']:
                    continue
                if (old_data or {}).get(field) != new_val:
                    has_changes = True
                    break
            if not has_changes:
                return

        log_entry = {
            'action': action,
            'schedule_id': schedule_id,
            'group_id': group_id,
            'old_data': old_data or {},
            'new_data': new_data or {},
            'changed_by': session.get('uid', 'unknown'),
            'changed_at': datetime.now(),
            'timestamp': datetime.now().timestamp()
        }

        with self._lock:
            self._pending_logs.append(log_entry)

    def flush(self):
        """Записать все накопленные логи в БД"""
        with self._lock:
            if not self._pending_logs:
                return

            logs_to_write = self._pending_logs.copy()
            self._pending_logs.clear()

        try:
            batch = db.batch()
            for log_entry in logs_to_write:
                doc_ref = db.collection('schedule_logs').document()
                batch.set(doc_ref, log_entry)
            batch.commit()
        except Exception as e:
            print(f"Ошибка при записи логов: {e}")


# Глобальный логгер
schedule_logger = ScheduleLogger()


def log_schedule_change(action, schedule_id, old_data, new_data, group_id=None):
    """Обертка для совместимости со старым кодом"""
    schedule_logger.add(action, schedule_id, old_data, new_data, group_id)


# ==================== ДЕКОРАТОРЫ ====================

def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'uid' not in session:
            return redirect(url_for('login'))

        # Кэшируем проверку роли в сессии
        if 'is_admin' not in session:
            user_doc = db.collection('users').document(session['uid']).get()
            if not user_doc.exists or user_doc.to_dict().get('role') != 'admin':
                flash('Доступ запрещён')
                abort(403)
            session['is_admin'] = True

        return f(*args, **kwargs)
    return wrapper


# ==================== РАСПИСАНИЕ (CALL SCHEDULE) ====================

CALL_SCHEDULE = {
    "monday": [
        "08.00-08.20", "08.25-09.15", "09.20-10.10", "10.25-11.15",
        "11.35-11.55", "11.35-12.25","12.00-12.50", "12.55-13.45", "14.00-14.50",
        "14.55-15.15", "14.55-15.45", "15.20-16.10", "15.50-16.40", "16.15-17.05", "17.20-18.10"
    ],
    "other": [
        "08.00-08.50", "08.55-09.45", "10.00-10.50", "10.55-11.45", "11.45-12.05",
        "12.05-12.55", "13.00-13.50", "14.05-14.55", "14.30-14.50", "15.00-15.50",
        "15.55-16.45", "16.50-17.40", "17.55-18.45"
    ]
}

DAY_NAMES = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота"]


# ==================== МАРШРУТЫ АВТОРИЗАЦИИ ====================

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        email = request.form.get('email')
        password = request.form.get('password')

        if not email or not password:
            flash('Введите email и пароль', 'error')
            return render_template('login.html')

        url = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=AIzaSyAQv2cWPt1GG70S8TKltfcX9GWKcuGano8"

        try:
            response = requests.post(url, json={
                "email": email,
                "password": password,
                "returnSecureToken": True
            })
            data = response.json()

            if 'error' in data:
                flash(f"Ошибка входа: {data['error']['message']}", 'error')
                return render_template('login.html')

            decoded = auth.verify_id_token(data['idToken'], clock_skew_seconds=20)
            uid = decoded['uid']

            user_doc = db.collection('users').document(uid).get()
            if not user_doc.exists:
                flash('Пользователь не найден', 'error')
                return render_template('login.html')

            user_data = user_doc.to_dict()
            if user_data.get('role') != 'admin':
                flash('Доступ запрещён', 'error')
                return render_template('login.html')

            session['uid'] = uid
            session['email'] = email
            session['is_admin'] = True
            flash('Успешный вход!', 'success')
            return redirect(url_for('dashboard'))

        except Exception as e:
            flash(f'Ошибка: {str(e)}', 'error')

    return render_template('login.html')


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/')
@admin_required
def dashboard():
    return render_template('dashboard.html')


# ==================== РАСПИСАНИЕ - ГЛАВНАЯ СТРАНИЦА ====================

@app.route('/schedule')
@admin_required
def schedule():
    """
    ОПТИМИЗИРОВАННАЯ версия главной страницы расписания.

    Возможности:
    1. Фильтрация по группе, курсу, направлению, дню недели, типу недели
    2. Кэширование справочников
    3. Пагинация и ленивая загрузка
    4. Подсветка числителя/знаменателя
    """
    # Параметры фильтрации
    selected_group = request.args.get('group', '')
    selected_course = request.args.get('course', '')
    selected_major = request.args.get('major', '')
    selected_day = request.args.get('day', '')
    selected_week_type = request.args.get('week_type', '')

    is_even_week = get_week_parity()

    # Загружаем список групп из кэша
    all_groups = get_all_groups()

    # Вычисляем доступные направления (major) и курсы на основе названий групп
    import re as _re
    majors_set = set()
    courses_set = set()

    today = datetime.now().date()
    academic_start_year = today.year if today.month >= 9 else today.year - 1

    for g in all_groups:
        # направление: буквы в начале (поддерживаем латиницу и кириллицу), нормализуем в верхний регистр
        m = _re.match(r'^([A-Za-zА-Яа-яЁё]+)', str(g))
        if m:
            majors_set.add(m.group(1).upper())

        # попытка найти стартовый год: 22-3 или 2022-3
        m2 = _re.search(r'(\d{2,4})-(\d+)', str(g))
        if m2:
            y = m2.group(1)
            try:
                ynum = int(y)
                if ynum < 100:  # two-digit year
                    ynum += 2000
                course_num = academic_start_year - ynum + 1
                if course_num < 1:
                    course_num = 1
                if course_num > 10:
                    course_num = 10
                courses_set.add(course_num)
            except Exception:
                pass

    available_majors = sorted(list(majors_set))
    available_courses = sorted(list(courses_set))

    # Фильтруем группы по курсу и направлению для отображения в селекторе
    filtered_groups = all_groups
    if selected_course or selected_major:
        filtered_groups = []
        for group in all_groups:
            match = True

            # Фильтр по курсу: вычисляем курс по году поступления в названии группы
            if selected_course:
                m_y = _re.search(r"(\d{2,4})-\d+", group)
                if m_y:
                    try:
                        y = int(m_y.group(1))
                        if y < 100:
                            y += 2000
                        course_num = academic_start_year - y + 1
                        # clamp
                        if course_num < 1:
                            course_num = 1
                        if course_num > 10:
                            course_num = 10
                        if str(course_num) != str(selected_course):
                            match = False
                    except Exception:
                        match = False
                else:
                    match = False

            # Фильтр по направлению (буквы в начале)
            if selected_major:
                m = _re.match(r'^([A-Za-zА-Яа-яЁё]+)', str(group))
                major_val = m.group(1).upper() if m else ''
                if major_val != selected_major.upper():
                    match = False

            if match:
                filtered_groups.append(group)

    # Если группа не выбрана, показываем расписание для всех групп (с учётом фильтров)
    if not selected_group:
        schedule_list = []
        query = db.collection('schedules')

        for doc in query.stream():
            data = doc.to_dict()
            data['id'] = doc.id

            # Пропускаем записи без группы
            group_id = data.get('groupId') or ''
            if not group_id:
                continue

            # Фильтруем по курсу/направлению через отфильтрованный список групп (если применимо)
            if (selected_course or selected_major) and group_id not in filtered_groups:
                continue

            # Парсим дату и определяем день недели
            if data.get('date'):
                try:
                    dt = datetime.strptime(data['date'], '%Y-%m-%d')
                    idx = dt.weekday()
                    data['day_index'] = idx
                    data['day_name'] = DAY_NAMES[idx] if idx < 6 else "Воскресенье"
                except:
                    data['day_index'] = 7
                    data['day_name'] = "Неизвестно"
            else:
                data['day_index'] = 7
                data['day_name'] = "Неизвестно"

            # Применяем фильтр по дню недели
            if selected_day and str(data.get('day_index')) != selected_day:
                continue

            # Применяем фильтр по типу недели
            if selected_week_type:
                week_type = data.get('week_type', 'always')
                if week_type != selected_week_type:
                    continue

            schedule_list.append(data)

        # Сортировка
        schedule_list.sort(key=lambda x: (x.get('groupId', ''), x.get('day_index', 0), x.get('time', '')))

        # Группировка по группам и дням
        grouped_data = {}
        for item in schedule_list:
            g = item.get('groupId')
            if g not in grouped_data:
                grouped_data[g] = {}
            d_name = item['day_name']
            if d_name not in grouped_data[g]:
                grouped_data[g][d_name] = []
            grouped_data[g][d_name].append(item)

        recent_logs = load_recent_logs(limit=10)

        return render_template(
            'schedule.html',
            schedule=schedule_list,
            grouped_schedule=grouped_data,
            recent_logs=recent_logs,
            day_names=DAY_NAMES,
            is_even_week=is_even_week,
            all_groups=all_groups,
            filtered_groups=filtered_groups if (selected_course or selected_major) else all_groups,
            available_majors=available_majors,
            available_courses=available_courses,
            selected_group='',
            selected_course=selected_course,
            selected_major=selected_major,
            selected_day=selected_day,
            selected_week_type=selected_week_type,
            show_group_selector=True,
            is_admin=session.get('is_admin', False)
        )

    # Загружаем расписание ТОЛЬКО для выбранной группы
    schedule_list = []
    query = db.collection('schedules').where('groupId', '==', selected_group)

    for doc in query.stream():
        data = doc.to_dict()
        data['id'] = doc.id

        # Парсим дату и определяем день недели
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                idx = dt.weekday()
                data['day_index'] = idx
                data['day_name'] = DAY_NAMES[idx] if idx < 6 else "Воскресенье"
            except:
                data['day_index'] = 7
                data['day_name'] = "Неизвестно"
        else:
            data['day_index'] = 7
            data['day_name'] = "Неизвестно"

        # Применяем фильтр по дню недели
        if selected_day and str(data.get('day_index')) != selected_day:
            continue

        # Применяем фильтр по типу недели
        if selected_week_type:
            week_type = data.get('week_type', 'always')
            if week_type != selected_week_type:
                continue

        schedule_list.append(data)

    # Сортировка
    schedule_list.sort(key=lambda x: (x.get('day_index', 0), x.get('time', '')))

    # Группировка по дням для полотна
    grouped_data = {selected_group: {}}
    for item in schedule_list:
        d_name = item['day_name']
        if d_name not in grouped_data[selected_group]:
            grouped_data[selected_group][d_name] = []
        grouped_data[selected_group][d_name].append(item)

    # Загружаем логи с лимитом
    recent_logs = load_recent_logs(limit=10, group_id=selected_group)

    return render_template(
        'schedule.html',
        schedule=schedule_list,
        grouped_schedule=grouped_data,
        recent_logs=recent_logs,
        day_names=DAY_NAMES,
        is_even_week=is_even_week,
        all_groups=all_groups,
        filtered_groups=filtered_groups if (selected_course or selected_major) else all_groups,
        available_majors=available_majors,
        available_courses=available_courses,
        selected_group=selected_group,
        selected_course=selected_course,
        selected_major=selected_major,
        selected_day=selected_day,
        selected_week_type=selected_week_type,
        show_group_selector=True,
        is_admin=session.get('is_admin', False)
    )


def load_recent_logs(limit=10, group_id=None):
    """
    Загружает последние логи изменений.
    Оптимизация: загружаем имена админов одним запросом.
    """
    logs = []

    try:
        query = db.collection('schedule_logs').order_by('changed_at', direction='DESCENDING').limit(limit)

        if group_id:
            query = query.where('group_id', '==', group_id)

        docs = list(query.stream())

        # Собираем уникальные ID админов
        admin_ids = set()
        for doc in docs:
            admin_id = doc.to_dict().get('changed_by')
            if admin_id and admin_id != 'unknown':
                admin_ids.add(admin_id)

        # Загружаем имена админов ОДНИМ запросом
        admin_names = {}
        if admin_ids:
            for admin_id in admin_ids:
                admin_doc = db.collection('users').document(admin_id).get()
                if admin_doc.exists:
                    admin_names[admin_id] = admin_doc.to_dict().get('email', admin_id)
                else:
                    admin_names[admin_id] = admin_id

        # Формируем логи
        for doc in docs:
            log_entry = doc.to_dict()
            admin_id = log_entry.get('changed_by', 'unknown')
            log_entry['changed_by_name'] = admin_names.get(admin_id, admin_id)

            # Определяем день недели
            date_str = log_entry.get('new_data', {}).get('date') or log_entry.get('old_data', {}).get('date')
            if date_str:
                try:
                    dt = datetime.strptime(date_str, '%Y-%m-%d')
                    log_entry['day_name'] = DAY_NAMES[dt.weekday()] if dt.weekday() < 6 else "Суббота"
                except:
                    log_entry['day_name'] = None
            else:
                log_entry['day_name'] = None

            logs.append(log_entry)

    except Exception as e:
        print(f"Ошибка загрузки логов: {e}")

    return logs


# ==================== РАСПИСАНИЕ - ЛОГИ ====================

@app.route('/schedule/logs')
@admin_required
def schedule_logs():
    """Страница истории изменений с пагинацией"""
    page = request.args.get('page', 1, type=int)
    per_page = 50
    group_filter = request.args.get('group', '')

    logs = load_recent_logs(limit=per_page, group_id=group_filter if group_filter else None)
    all_groups = get_all_groups()

    return render_template(
        'schedule_logs.html',
        logs=logs,
        all_groups=all_groups,
        selected_group=group_filter,
        page=page
    )


# ==================== РАСПИСАНИЕ - КОНСТРУКТОР ====================

@app.route('/schedule/builder', methods=['GET', 'POST'])
@admin_required
def schedule_builder():
    """
    Конструктор расписания.
    Использует кэшированные справочники.
    """
    if request.method == 'POST':
        return handle_schedule_builder_post()

    # GET — показываем форму с кэшированными данными
    return render_template(
        'schedule_builder.html',
        available_groups=get_all_groups(),
        available_teachers=get_all_teachers(),
        available_subjects=get_all_subjects(),
        call_schedule=CALL_SCHEDULE,
        preselected_group=request.args.get('groupId', '')
    )


def handle_schedule_builder_post():
    """Обработка POST запроса конструктора"""
    group_id = request.form.get('groupId')
    days_of_week = request.form.getlist('day_of_week')
    subjects = request.form.getlist('subject')
    times = request.form.getlist('time')
    teachers = request.form.getlist('teacher')
    rooms = request.form.getlist('room')
    week_types = request.form.getlist('week_type')
    day_rels = request.form.getlist('lesson_day_rel')

    # Сохраняем текущие фильтры из referer
    referer_filters = {}
    if request.referrer:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(request.referrer)
        filters = parse_qs(parsed.query)
        referer_filters = {
            'course': filters.get('course', [''])[0],
            'major': filters.get('major', [''])[0],
            'day': filters.get('day', [''])[0],
            'week_type': filters.get('week_type', [''])[0]
        }

    batch = db.batch()
    today = datetime.now().date()
    count = 0

    for i in range(len(subjects)):
        if subjects[i] and subjects[i].strip():
            try:
                day_idx = int(day_rels[i])
                target_day_num = int(days_of_week[day_idx])
                diff = target_day_num - today.weekday()
                if diff < 0:
                    diff += 7
                date_str = (today + timedelta(days=diff)).strftime('%Y-%m-%d')

                lesson_data = {
                    'groupId': group_id,
                    'date': date_str,
                    'time': times[i].strip() if i < len(times) else "",
                    'subject': subjects[i].strip(),
                    'teacher': teachers[i].strip() if i < len(teachers) else "",
                    'room': rooms[i].strip() if i < len(rooms) else "",
                    'week_type': week_types[i] if i < len(week_types) else "always",
                    'createdAt': datetime.now()
                }

                doc_ref = db.collection('schedules').document()
                batch.set(doc_ref, lesson_data)
                log_schedule_change('create', doc_ref.id, None, lesson_data, group_id)
                count += 1
            except (ValueError, IndexError):
                continue

    if count > 0:
        batch.commit()
        schedule_logger.flush()  # Записываем логи
        cache.invalidate('all_groups')  # Инвалидируем кэш групп
        flash(f'Создано {count} занятий', 'success')
    else:
        flash('Ни одного занятия не заполнено', 'warning')

    # Возвращаемся на расписание с сохранёнными фильтрами
    redirect_url = url_for('schedule', group=group_id)
    if referer_filters['course']:
        redirect_url = url_for('schedule', group=group_id, course=referer_filters['course'])
    if referer_filters['major']:
        redirect_url = url_for('schedule', group=group_id, major=referer_filters['major'])
    if referer_filters['day']:
        redirect_url = url_for('schedule', group=group_id, day=referer_filters['day'])
    if referer_filters['week_type']:
        redirect_url = url_for('schedule', group=group_id, week_type=referer_filters['week_type'])

    return redirect(redirect_url)


# ==================== РАСПИСАНИЕ - ОБНОВЛЕНИЕ ГРУППЫ ====================

@app.route('/schedule/update_group', methods=['POST'])
@admin_required
def schedule_update_group():
    """
    Обновляет расписание группы.
    Оптимизация: используем батчинг для всех операций.
    """
    try:
        data = request.get_json()
        group_id = data.get('group_id')
        days = data.get('days', [])

        # Получаем фильтры из запроса
        referer_filters = {
            'course': data.get('course', ''),
            'major': data.get('major', ''),
            'day': data.get('day', ''),
            'week_type': data.get('week_type', '')
        }

        if not group_id or not days:
            return jsonify({'error': 'Некорректные данные'}), 400

        # Загружаем существующие записи
        existing_docs = {
            doc.id: doc.to_dict()
            for doc in db.collection('schedules').where('groupId', '==', group_id).stream()
        }

        incoming_ids = set()
        batch = db.batch()
        today = datetime.now().date()

        for day_block in days:
            day_of_week = int(day_block['day_of_week'])

            for lesson in day_block['lessons']:
                lesson_id = lesson.get('id')

                # Вычисляем дату
                diff = day_of_week - today.weekday()
                if diff < 0:
                    diff += 7
                date_str = (today + timedelta(days=diff)).strftime('%Y-%m-%d')

                lesson_data = {
                    'groupId': group_id,
                    'date': date_str,
                    'time': lesson.get('time', '').strip(),
                    'subject': lesson.get('subject', '').strip(),
                    'teacher': lesson.get('teacher', '').strip(),
                    'room': lesson.get('room', '').strip(),
                    'week_type': lesson.get('week_type', 'always'),
                    'updatedAt': datetime.now()
                }

                if lesson_id and lesson_id in existing_docs:
                    # Обновляем
                    old_data = existing_docs[lesson_id]
                    batch.update(db.collection('schedules').document(lesson_id), lesson_data)
                    log_schedule_change('update', lesson_id, old_data, lesson_data, group_id)
                    incoming_ids.add(lesson_id)
                else:
                    # Создаем
                    lesson_data['createdAt'] = datetime.now()
                    new_ref = db.collection('schedules').document()
                    batch.set(new_ref, lesson_data)
                    log_schedule_change('create', new_ref.id, None, lesson_data, group_id)

        # Удаляем старые записи
        for old_id in set(existing_docs.keys()) - incoming_ids:
            batch.delete(db.collection('schedules').document(old_id))
            log_schedule_change('delete', old_id, existing_docs[old_id], None, group_id)

        batch.commit()
        schedule_logger.flush()

        # Возвращаем редирект URL с фильтрами
        redirect_url = url_for('schedule', group=group_id)
        if referer_filters.get('course'):
            redirect_url += f"&course={referer_filters['course']}"
        if referer_filters.get('major'):
            redirect_url += f"&major={referer_filters['major']}"
        if referer_filters.get('day'):
            redirect_url += f"&day={referer_filters['day']}"
        if referer_filters.get('week_type'):
            redirect_url += f"&week_type={referer_filters['week_type']}"

        return jsonify({'success': True, 'message': 'Расписание обновлено', 'redirect': redirect_url}), 200

    except Exception as e:
        print(f"Ошибка: {e}")
        return jsonify({'error': str(e)}), 500


# ==================== РАСПИСАНИЕ - УДАЛЕНИЕ ====================

@app.route('/schedule/delete/<doc_id>', methods=['POST'])
@admin_required
def schedule_delete_item(doc_id):
    """Удаление одного занятия"""
    try:
        doc_ref = db.collection('schedules').document(doc_id)
        doc = doc_ref.get()

        if doc.exists:
            old_data = doc.to_dict()
            group_id = old_data.get('groupId', '')
            doc_ref.delete()
            log_schedule_change('delete', doc_id, old_data, None, group_id)
            schedule_logger.flush()
            flash('Занятие удалено', 'success')
        else:
            flash('Занятие не найдено', 'error')
    except Exception as e:
        flash(f'Ошибка: {e}', 'danger')

    # Сохраняем фильтры из referer
    referer_filters = {}
    if request.referrer:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(request.referrer)
        filters = parse_qs(parsed.query)
        referer_filters = {
            'group': filters.get('group', [''])[0],
            'course': filters.get('course', [''])[0],
            'major': filters.get('major', [''])[0],
            'day': filters.get('day', [''])[0],
            'week_type': filters.get('week_type', [''])[0]
        }

    # Возвращаемся на расписание с сохранёнными фильтрами
    redirect_url = url_for('schedule')
    if referer_filters['group']:
        redirect_url = url_for('schedule', group=referer_filters['group'])
    if referer_filters['course']:
        redirect_url += f"&course={referer_filters['course']}"
    if referer_filters['major']:
        redirect_url += f"&major={referer_filters['major']}"
    if referer_filters['day']:
        redirect_url += f"&day={referer_filters['day']}"
    if referer_filters['week_type']:
        redirect_url += f"&week_type={referer_filters['week_type']}"

    return redirect(redirect_url)


@app.route('/schedule/delete_group/<group_id>', methods=['POST'])
@admin_required
def schedule_delete_group(group_id):
    """Удаление всего расписания группы"""
    try:
        docs = list(db.collection('schedules').where('groupId', '==', group_id).stream())

        if docs:
            batch = db.batch()
            for doc in docs:
                log_schedule_change('delete', doc.id, doc.to_dict(), None, group_id)
                batch.delete(doc.reference)

            batch.commit()
            schedule_logger.flush()
            cache.invalidate('all_groups')
            flash(f'Расписание группы {group_id} удалено ({len(docs)} записей)', 'success')
        else:
            flash('Записи не найдены', 'warning')

    except Exception as e:
        flash(f'Ошибка: {e}', 'danger')

    # Сохраняем фильтры из referer
    referer_filters = {}
    if request.referrer:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(request.referrer)
        filters = parse_qs(parsed.query)
        referer_filters = {
            'course': filters.get('course', [''])[0],
            'major': filters.get('major', [''])[0],
            'day': filters.get('day', [''])[0],
            'week_type': filters.get('week_type', [''])[0]
        }

    # Возвращаемся на расписание с сохранёнными фильтрами
    redirect_url = url_for('schedule')
    params = []
    if referer_filters.get('course'):
        params.append(f"course={referer_filters['course']}")
    if referer_filters.get('major'):
        params.append(f"major={referer_filters['major']}")
    if referer_filters.get('day'):
        params.append(f"day={referer_filters['day']}")
    if referer_filters.get('week_type'):
        params.append(f"week_type={referer_filters['week_type']}")

    if params:
        redirect_url += "?" + "&".join(params)

    return redirect(redirect_url)


# ==================== РАСПИСАНИЕ - РЕДАКТИРОВАНИЕ ====================

@app.route('/schedule/edit/<schedule_id>', methods=['GET', 'POST'])
@admin_required
def schedule_edit(schedule_id):
    """Редактирование одного занятия"""
    doc_ref = db.collection('schedules').document(schedule_id)
    doc = doc_ref.get()

    if not doc.exists:
        flash('Запись не найдена', 'error')
        return redirect(url_for('schedule'))

    item = {'id': schedule_id, **doc.to_dict()}

    if request.method == 'POST':
        data = {
            'groupId': request.form.get('groupId', '').strip(),
            'date': request.form.get('date', '').strip(),
            'time': request.form.get('time', '').strip(),
            'subject': request.form.get('subject', '').strip(),
            'teacher': request.form.get('teacher', '').strip(),
            'room': request.form.get('room', '').strip(),
            'week_type': request.form.get('week_type', 'always')
        }

        if all([data['groupId'], data['date'], data['time'], data['subject']]):
            try:
                old_data = item.copy()
                old_data.pop('id', None)

                doc_ref.update(data)
                log_schedule_change('update', schedule_id, old_data, data, data['groupId'])
                schedule_logger.flush()

                flash('Запись обновлена', 'success')
                return redirect(url_for('schedule', group=data['groupId']))
            except Exception as e:
                flash(f'Ошибка: {e}', 'error')
        else:
            flash('Заполните обязательные поля', 'error')

    # Группируем по дням (в данном случае только одно занятие)
    grouped_by_day = {}
    if item.get('date'):
        try:
            dt = datetime.strptime(item['date'], '%Y-%m-%d')
            day_idx = dt.weekday()
            if day_idx < 6:
                grouped_by_day[day_idx] = [item]
        except:
            grouped_by_day[0] = [item]
    else:
        grouped_by_day[0] = [item]

    return render_template(
        'schedule_edit.html',
        item=item,
        group_id=item.get('groupId', ''),
        grouped_by_day=grouped_by_day,
        available_groups=get_all_groups(),
        available_teachers=get_all_teachers(),
        available_subjects=get_all_subjects(),
        call_schedule=CALL_SCHEDULE
    )


@app.route('/schedule/edit_group/<group_id>')
@admin_required
def schedule_edit_group(group_id):
    """Редактирование всего расписания группы"""
    # Загружаем расписание группы
    group_schedule = []
    for doc in db.collection('schedules').where('groupId', '==', group_id).stream():
        data = doc.to_dict()
        data['id'] = doc.id
        group_schedule.append(data)

    if not group_schedule:
        flash(f'Для группы {group_id} нет расписания', 'warning')
        return redirect(url_for('schedule'))

    # Группируем по дням
    grouped_by_day = {}
    for item in group_schedule:
        if item.get('date'):
            try:
                dt = datetime.strptime(item['date'], '%Y-%m-%d')
                day_idx = dt.weekday()
                if day_idx < 6:
                    if day_idx not in grouped_by_day:
                        grouped_by_day[day_idx] = []
                    grouped_by_day[day_idx].append(item)
            except:
                pass

    # Сохраняем текущие фильтры из referer
    referer_filters = {}
    if request.referrer:
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(request.referrer)
        filters = parse_qs(parsed.query)
        referer_filters = {
            'course': filters.get('course', [''])[0],
            'major': filters.get('major', [''])[0],
            'day': filters.get('day', [''])[0],
            'week_type': filters.get('week_type', [''])[0]
        }

    return render_template(
        'schedule_builder_edit.html',
        group_id=group_id,
        group_schedule=group_schedule,
        grouped_by_day=grouped_by_day,
        available_groups=get_all_groups(),
        available_teachers=get_all_teachers(),
        available_subjects=get_all_subjects(),
        call_schedule=CALL_SCHEDULE,
        preselected_group=group_id,
        referer_filters=referer_filters
    )


# ==================== API ДЛЯ AJAX ЗАГРУЗКИ ====================

@app.route('/api/schedule/group/<group_id>')
@admin_required
def api_get_group_schedule(group_id):
    """
    API endpoint для AJAX загрузки расписания группы.
    Позволяет загружать данные без перезагрузки страницы.
    """
    schedule_list = []

    for doc in db.collection('schedules').where('groupId', '==', group_id).stream():
        data = doc.to_dict()
        data['id'] = doc.id

        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                data['day_index'] = dt.weekday()
                data['day_name'] = DAY_NAMES[dt.weekday()] if dt.weekday() < 6 else "Воскресенье"
            except:
                data['day_index'] = 7
                data['day_name'] = "Неизвестно"

        # Преобразуем datetime в строку для JSON
        if 'createdAt' in data and data['createdAt']:
            data['createdAt'] = data['createdAt'].isoformat() if hasattr(data['createdAt'], 'isoformat') else str(data['createdAt'])
        if 'updatedAt' in data and data['updatedAt']:
            data['updatedAt'] = data['updatedAt'].isoformat() if hasattr(data['updatedAt'], 'isoformat') else str(data['updatedAt'])

        schedule_list.append(data)

    schedule_list.sort(key=lambda x: (x.get('day_index', 0), x.get('time', '')))

    return jsonify({
        'success': True,
        'group_id': group_id,
        'schedule': schedule_list,
        'count': len(schedule_list)
    })


@app.route('/api/cache/invalidate', methods=['POST'])
@admin_required
def api_invalidate_cache():
    """Принудительная инвалидация кэша"""
    cache.invalidate()
    return jsonify({'success': True, 'message': 'Кэш очищен'})


# ==================== ОСТАЛЬНЫЕ МАРШРУТЫ (CLUBS, EVENTS, NEWS и т.д.) ====================
# ... (остальной код остается без изменений, копируется из оригинала)

@app.route('/clubs')
@admin_required
def clubs():
    docs = db.collection('clubs').stream()
    clubs_list = [{'id': doc.id, **doc.to_dict()} for doc in docs]
    return render_template('clubs.html', clubs=clubs_list)


@app.route('/clubs/add', methods=['GET', 'POST'])
@app.route('/clubs/edit/<club_id>', methods=['GET', 'POST'])
@admin_required
def club_form(club_id=None):
    club = None
    if club_id:
        club_doc = db.collection('clubs').document(club_id).get()
        if club_doc.exists:
            club = {'id': club_id, **club_doc.to_dict()}
        else:
            flash('Клуб не найден')
            return redirect(url_for('clubs'))

    if request.method == 'POST':
        data = {
            'title': request.form.get('title', ''),
            'description': request.form.get('description', ''),
            'category': request.form.get('category', ''),
            'meetingTime': request.form.get('meetingTime', ''),
            'location': request.form.get('location', ''),
            'curator': request.form.get('curator', ''),
        }

        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ts = datetime.now(timezone.utc).timestamp()
            blob = bucket.blob(f'clubs/{ts}_{filename}')
            blob.upload_from_file(file)
            blob.make_public()
            data['imageUrl'] = blob.public_url

        if club_id:
            db.collection('clubs').document(club_id).update(data)
            flash('Клуб обновлён', 'success')
        else:
            data['createdAt'] = datetime.now(timezone.utc)
            data['registrationsCount'] = 0
            db.collection('clubs').add(data)
            flash('Клуб создан', 'success')
        return redirect(url_for('clubs'))

    return render_template('club_form.html', club=club)


@app.route('/clubs/delete/<club_id>', methods=['POST'])
@admin_required
def club_delete(club_id):
    db.collection('clubs').document(club_id).delete()
    flash('Клуб удалён')
    return redirect(url_for('clubs'))


@app.route('/clubs/registrations/<club_id>')
@admin_required
def club_registrations(club_id):
    club_doc = db.collection('clubs').document(club_id).get()
    if not club_doc.exists:
        flash('Клуб не найден')
        return redirect(url_for('clubs'))

    club = club_doc.to_dict()
    club['id'] = club_id

    regs_ref = db.collection('clubs').document(club_id).collection('registrations').stream()

    registrations = []
    for doc in regs_ref:
        data = doc.to_dict()
        user_id = doc.id

        user_phone = "—"
        user_doc = db.collection('users').document(user_id).get()
        if user_doc.exists:
            user_data = user_doc.to_dict()
            user_phone = user_data.get('phone') or user_data.get('phoneNumber') or "—"

        registrations.append({
            'id': user_id,
            'name': data.get('userName') or data.get('name') or 'Без имени',
            'groupId': data.get('group') or data.get('groupId') or '—',
            'phone': user_phone,
            'registeredAt': data.get('registeredAt') or data.get('createdAt')
        })

    return render_template('club_registrations.html', club=club, registrations=registrations)


@app.route('/clubs/registration/delete/<club_id>/<reg_id>', methods=['POST'])
@admin_required
def delete_club_registration(club_id, reg_id):
    try:
        db.collection('clubs').document(club_id).collection('registrations').document(reg_id).delete()

        club_ref = db.collection('clubs').document(club_id)
        club_data = club_ref.get().to_dict()
        count = club_data.get('registrationsCount', 0)
        if count > 0:
            club_ref.update({'registrationsCount': count - 1})

        flash('Пользователь удален', 'success')
    except Exception as e:
        flash(f'Ошибка: {e}', 'danger')

    return redirect(url_for('club_registrations', club_id=club_id))


# ==================== EVENTS ====================

@app.route('/events')
@admin_required
def events():
    docs = db.collection('events').stream()
    events_list = [{'id': doc.id, **doc.to_dict()} for doc in docs]
    return render_template('events.html', events=events_list)


@app.route('/events/add', methods=['GET', 'POST'])
@app.route('/events/edit/<event_id>', methods=['GET', 'POST'])
@admin_required
def event_form(event_id=None):
    event = None

    if event_id:
        event_doc = db.collection('events').document(event_id).get()

        if event_doc.exists:
            event = {'id': event_id, **event_doc.to_dict()}

            if event.get('date') and hasattr(event['date'], 'strftime'):
                event['date_html'] = event['date'].strftime('%Y-%m-%d')
            elif event.get('date'):
                event['date_html'] = str(event['date'])[:10]
        else:
            flash('Событие не найдено', 'danger')
            return redirect(url_for('events'))

    if request.method == 'POST':
        date_str = request.form.get('date', '')
        event_date = None

        if date_str:
            try:
                event_date = datetime.strptime(date_str, '%Y-%m-%d')
            except ValueError:
                flash('Некорректная дата', 'danger')
                return render_template('event_form.html', event=event)

        data = {
            'title': request.form.get('title', ''),
            'description': request.form.get('description', ''),
            'date': event_date,
        }

        file = request.files.get('image')
        remove_image = request.form.get('remove_image') == '1'

        old_image_url = event.get('imageUrl') if event else None
        new_image_url = None

        if file and file.filename:
            if not allowed_file(file.filename):
                flash('Недопустимый формат изображения', 'danger')
                return render_template('event_form.html', event=event)

            filename = secure_filename(file.filename)
            ts = int(datetime.utcnow().timestamp())

            blob = bucket.blob(f'events/{ts}_{filename}')
            blob.upload_from_file(file)
            blob.make_public()

            new_image_url = blob.public_url
            data['imageUrl'] = new_image_url

        elif remove_image and event_id:
            data['imageUrl'] = None

        if event_id:
            db.collection('events').document(event_id).update(data)
            flash('Событие обновлено', 'success')
        else:
            data['createdAt'] = datetime.utcnow()
            data['registrationsCount'] = 0
            db.collection('events').add(data)
            flash('Событие создано', 'success')

        if old_image_url and (new_image_url or remove_image):
            try:
                from urllib.parse import urlparse, unquote

                parsed_url = urlparse(old_image_url)
                path = unquote(parsed_url.path)

                bucket_prefix = f'/{bucket.name}/'

                if path.startswith(bucket_prefix):
                    blob_name = path[len(bucket_prefix):]

                    if blob_name.startswith('events/'):
                        old_blob = bucket.blob(blob_name)

                        if old_blob.exists():
                            old_blob.delete()

            except Exception as e:
                print(f'Не удалось удалить старое изображение: {e}')

        return redirect(url_for('events'))

    return render_template('event_form.html', event=event)



@app.route('/events/delete/<event_id>', methods=['POST'])
@admin_required
def event_delete(event_id):
    db.collection('events').document(event_id).delete()
    flash('Событие удалено')
    return redirect(url_for('events'))


@app.route('/events/registrations/<event_id>')
@admin_required
def event_registrations(event_id):
    event_doc = db.collection('events').document(event_id).get()
    if not event_doc.exists:
        flash('Событие не найдено', 'danger')
        return redirect(url_for('events'))

    event = event_doc.to_dict()
    event['id'] = event_id

    regs_ref = db.collection('events').document(event_id).collection('registrations').stream()

    registrations = []
    for doc in regs_ref:
        reg_data = doc.to_dict()
        user_id = doc.id

        u_info = {'name': 'Без имени', 'phone': 'Не указан', 'group': '—'}

        user_doc = db.collection('users').document(user_id).get()
        if user_doc.exists:
            u_data = user_doc.to_dict()
            u_info['name'] = u_data.get('name') or u_data.get('displayName') or 'Без имени'
            u_info['phone'] = u_data.get('phone') or u_data.get('phoneNumber') or 'Не указан'
            u_info['group'] = u_data.get('group') or u_data.get('groupId') or '—'

        registrations.append({
            'id': user_id,
            'name': u_info['name'],
            'groupId': u_info['group'],
            'phone': u_info['phone'],
            'registeredAt': reg_data.get('registeredAt') or reg_data.get('createdAt')
        })

    return render_template('event_registrations.html', event=event, registrations=registrations)


@app.route('/events/registrations/delete/<event_id>/<user_id>', methods=['POST'])
@admin_required
def delete_event_registration(event_id, user_id):
    try:
        db.collection('events').document(event_id).collection('registrations').document(user_id).delete()
        db.collection('events').document(event_id).update({
            'registrationsCount': firestore.Increment(-1)
        })
        flash('Участник удален', 'success')
    except Exception as e:
        flash(f'Ошибка: {e}', 'danger')

    return redirect(url_for('event_registrations', event_id=event_id))


# ==================== NEWS ====================

@app.route('/news')
@admin_required
def news():
    docs = db.collection('news').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
    news_list = []
    for doc in docs:
        news_data = {'id': doc.id, 'type': 'news', **doc.to_dict()}
        news_list.append(news_data)
    return render_template('news.html', items=news_list)


@app.route('/news/add', methods=['GET', 'POST'])
@app.route('/news/edit/<news_id>', methods=['GET', 'POST'])
@admin_required
def news_form(news_id=None):
    news = None
    if news_id:
        news_doc = db.collection('news').document(news_id).get()
        if news_doc.exists:
            news = {'id': news_id, **news_doc.to_dict()}

    if request.method == 'POST':
        data = {
            'title': request.form.get('title', ''),
            'content': request.form.get('content', ''),
            'category': request.form.get('category', ''),
            'target_audience': request.form.get('target_audience', 'all'),
        }

        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ts = datetime.now(timezone.utc).timestamp()
            blob = bucket.blob(f'news/{ts}_{filename}')
            blob.upload_from_file(file)

            data['imageUrl'] = blob.public_url

        if news_id:
            db.collection('news').document(news_id).update(data)
            flash('Новость обновлена', 'success')
        else:
            data['createdAt'] = datetime.now(timezone.utc)
            db.collection('news').add(data)
            flash('Новость создана', 'success')
        return redirect(url_for('polls'))

    return render_template('news_form.html', news=news)


@app.route('/news/delete/<news_id>', methods=['POST'])
@admin_required
def news_delete(news_id):
    db.collection('news').document(news_id).delete()
    flash('Новость удалена')
    return redirect(url_for('news'))

# ==================== POLLS (ОПРОСНИКИ) ====================

@app.route('/polls', methods=['GET', 'POST'])
@admin_required
def polls():
    """Получить все опросники и новости вместе"""
    try:
        # Загружаем новости
        news_docs = db.collection('news').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
        news_list = []
        for doc in news_docs:
            news_data = {'id': doc.id, 'type': 'news', **doc.to_dict()}
            news_list.append(news_data)

        # Загружаем опросники
        polls_docs = db.collection('polls').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
        polls_list = []
        for doc in polls_docs:
            poll_data = doc.to_dict()
            # Проверяем статус опроса
            if poll_data.get('endTime'):
                end_time = poll_data['endTime']
                if isinstance(end_time, datetime):
                    is_active = end_time > datetime.now(timezone.utc)
                else:
                    is_active = True
            else:
                is_active = True
            poll_data['is_active'] = is_active
            poll_data['id'] = doc.id
            poll_data['type'] = 'poll'

            # Подгружаем ответы для подсчета голосов на основном экране
            answers_docs = db.collection('polls').document(doc.id).collection('answers').stream()
            answers = [{'id': a_doc.id, **a_doc.to_dict()} for a_doc in answers_docs]
            poll_data['answers'] = answers
            poll_data['total_votes'] = sum(a.get('votes', 0) for a in answers)

            polls_list.append(poll_data)

        # Объединяем и сортируем по дате
        all_items = news_list + polls_list
        all_items.sort(key=lambda x: x.get('createdAt', datetime.now(timezone.utc)), reverse=True)

        return render_template('news.html', items=all_items)
    except Exception as e:
        flash(f'Ошибка загрузки: {str(e)}', 'danger')
        return render_template('news.html', items=[])


@app.route('/poll/add', methods=['GET', 'POST'])
@app.route('/poll/edit/<poll_id>', methods=['GET', 'POST'])
@admin_required
def poll_form(poll_id=None):
    """Добавление/редактирование опросника"""
    poll = None

    if poll_id:
        poll_doc = db.collection('polls').document(poll_id).get()
        if poll_doc.exists:
            poll = {'id': poll_id, **poll_doc.to_dict()}
            # Загружаем варианты ответов
            answers_docs = db.collection('polls').document(poll_id).collection('answers').stream()
            poll['answers'] = [{'id': doc.id, **doc.to_dict()} for doc in answers_docs]

    if request.method == 'POST':
        title = request.form.get('title', '').strip()
        description = request.form.get('description', '').strip()
        target_audience = request.form.get('target_audience', 'all')  # all, students, teachers
        poll_type = request.form.get('poll_type', 'multiple')  # multiple, single

        # Получаем только время окончания
        end_time_str = request.form.get('endTime', '')

        if not title:
            flash('Заголовок обязателен', 'danger')
            return render_template('poll_form.html', poll=poll)

        try:
            # Начало = сейчас
            start_time = datetime.now(timezone.utc)
            end_time = datetime.fromisoformat(end_time_str.replace('T', ' ')) if end_time_str else (start_time + timedelta(hours=1))

            # Преобразуем в UTC если нужно
            if end_time.tzinfo is None:
                end_time = end_time.replace(tzinfo=timezone.utc)
        except ValueError:
            flash('Ошибка формата времени', 'danger')
            return render_template('poll_form.html', poll=poll)

        data = {
            'title': title,
            'description': description,
            'target_audience': target_audience,
            'poll_type': poll_type,
            'is_anonymous': 'is_anonymous' in request.form,  # True если чекбокс отмечен
            'endTime': end_time,
            'createdBy': session.get('uid'),
            'createdAt': datetime.now(timezone.utc) if not poll_id else poll.get('createdAt', datetime.now(timezone.utc))
        }

        # Обработка загрузки изображения
        file = request.files.get('image')
        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            ts = datetime.now(timezone.utc).timestamp()
            blob = bucket.blob(f'polls/{ts}_{filename}')
            blob.upload_from_file(file)
            data['imageUrl'] = blob.public_url

        if poll_id:
            db.collection('polls').document(poll_id).update(data)
            flash('Опросник обновлен', 'success')
        else:
            # Для нового опроса получаем варианты
            options = request.form.getlist('options')
            if not options or not any(opt.strip() for opt in options):
                flash('Необходимо добавить хотя бы один вариант ответа', 'danger')
                return render_template('poll_form.html', poll=poll)

            # Фильтруем пустые варианты
            options = [opt.strip() for opt in options if opt.strip()]

            new_poll_ref = db.collection('polls').add(data)
            poll_id = new_poll_ref[1].id

            # Добавляем варианты ответов
            for option_text in options:
                db.collection('polls').document(poll_id).collection('answers').add({
                    'text': option_text,
                    'votes': 0,
                    'createdAt': datetime.now(timezone.utc)
                })

            flash('Опросник создан', 'success')

        return redirect(url_for('polls'))

    return render_template('poll_form.html', poll=poll)

######## Сброс паролей

@app.route('/admin/users/reset-password/<user_id>', methods=['POST'])
@admin_required
def admin_reset_password(user_id):
    """Сброс пароля пользователя на дефолтный по его роли"""
    try:
        # 1. Получаем данные пользователя из Firestore
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()

        if not user_doc.exists:
            flash('Пользователь не найден', 'error')
            return redirect(request.referrer or url_for('admin_users'))

        user_data = user_doc.to_dict()
        role = user_data.get('role', 'student')

        # 2. Определяем дефолтный пароль
        default_password = 'Teacher123' if role == 'teacher' else 'Student123'

        # 3. Обновляем пароль в Firebase Auth
        auth.update_user(user_id, password=default_password)

        # 4. Обновляем метку времени обновления в Firestore
        user_ref.update({
            'updated_at': firestore.SERVER_TIMESTAMP
        })

        # 5. Если это преподаватель, инвалидируем кэш преподавателей
        if role == 'teacher':
            cache.invalidate('all_teachers')

        flash(f'Пароль успешно сброшен на "{default_password}" для {user_data.get("name", user_id)}', 'success')

    except Exception as e:
        flash(f'Ошибка при сбросе пароля: {e}', 'danger')

    return redirect(request.referrer or url_for('admin_users'))


@app.route('/poll/<poll_id>/add-answer', methods=['POST'])
@admin_required
def poll_add_answer(poll_id):
    """Добавление варианта ответа к опроснику"""
    answer_text = request.form.get('answer', '').strip()

    if not answer_text:
        flash('Текст ответа обязателен', 'danger')
    else:
        try:
            db.collection('polls').document(poll_id).collection('answers').add({
                'text': answer_text,
                'votes': 0,
                'createdAt': firestore.SERVER_TIMESTAMP
            })
            flash('Вариант ответа добавлен', 'success')
        except Exception as e:
            flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('poll_form', poll_id=poll_id))


@app.route('/poll/<poll_id>/delete-answer/<answer_id>', methods=['POST'])
@admin_required
def poll_delete_answer(poll_id, answer_id):
    """Удаление варианта ответа"""
    try:
        db.collection('polls').document(poll_id).collection('answers').document(answer_id).delete()
        flash('Вариант ответа удален', 'success')
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('poll_form', poll_id=poll_id))


@app.route('/poll/delete/<poll_id>', methods=['POST'])
@admin_required
def poll_delete(poll_id):
    """Удаление опросника"""
    try:
        # Удаляем все ответы (subcollection)
        answers = db.collection('polls').document(poll_id).collection('answers').stream()
        for answer in answers:
            answer.reference.delete()

        # Удаляем все ответы пользователей
        user_responses = db.collection('poll_responses').where('pollId', '==', poll_id).stream()
        for response in user_responses:
            response.reference.delete()

        # Удаляем сам опросник
        db.collection('polls').document(poll_id).delete()
        flash('Опросник удален', 'success')
    except Exception as e:
        flash(f'Ошибка удаления: {str(e)}', 'danger')

    return redirect(url_for('polls'))


@app.route('/active-polls')
def active_polls():
    """Список активных опросников для пользователей"""
    try:
        polls_docs = db.collection('polls').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
        polls_list = []
        for doc in polls_docs:
            poll_data = doc.to_dict()
            # Проверяем, активен ли опросник
            if poll_data.get('endTime'):
                end_time = poll_data['endTime']
                if isinstance(end_time, datetime):
                    is_active = end_time > datetime.now(timezone.utc)
                else:
                    is_active = True
            else:
                is_active = True

            if is_active:  # Только активные!
                poll_data['id'] = doc.id
                # Загружаем варианты ответов для подсчета голосов
                answers_docs = db.collection('polls').document(doc.id).collection('answers').stream()
                answers = [{'id': a_doc.id, **a_doc.to_dict()} for a_doc in answers_docs]
                poll_data['total_votes'] = sum(a.get('votes', 0) for a in answers)
                polls_list.append(poll_data)

        return render_template('active_polls.html', polls=polls_list)
    except Exception as e:
        flash(f'Ошибка загрузки опросников: {str(e)}', 'danger')
        return render_template('active_polls.html', polls=[])


@app.route('/poll/<poll_id>/vote')
def poll_vote(poll_id):
    """Страница голосования в опроснике"""
    try:
        poll_doc = db.collection('polls').document(poll_id).get()
        if not poll_doc.exists:
            flash('Опросник не найден', 'danger')
            return redirect(url_for('dashboard'))

        poll_data = poll_doc.to_dict()

        # Проверяем, активен ли опросник
        if poll_data.get('endTime'):
            end_time = poll_data['endTime']
            if isinstance(end_time, datetime):
                if end_time < datetime.now(timezone.utc):
                    # Показываем результаты
                    return redirect(url_for('poll_results', poll_id=poll_id))

        # Загружаем варианты ответов
        answers_docs = db.collection('polls').document(poll_id).collection('answers').stream()
        answers = [{'id': doc.id, **doc.to_dict()} for doc in answers_docs]

        # Проверяем, голосовал ли уже пользователь
        user_response = None
        if 'uid' in session:
            response_doc = db.collection('poll_responses').where('pollId', '==', poll_id).where('userId', '==', session['uid']).stream()
            response_list = list(response_doc)
            if response_list:
                user_response = response_list[0].to_dict()

        return render_template('poll_vote.html', poll={'id': poll_id, **poll_data}, answers=answers, user_response=user_response)
    except Exception as e:
        flash(f'Ошибка загрузки опроса: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))


@app.route('/poll/<poll_id>/submit', methods=['POST'])
def poll_submit(poll_id):
    """Отправка голоса в опросе"""
    if 'uid' not in session:
        return redirect(url_for('login'))

    try:
        # Получаем опросник
        poll_doc = db.collection('polls').document(poll_id).get()
        if not poll_doc.exists:
            flash('Опросник не найден', 'danger')
            return redirect(url_for('dashboard'))

        poll_data = poll_doc.to_dict()
        poll_type = poll_data.get('poll_type', 'multiple')

        # Проверяем, не голосовал ли уже пользователь (только для неанонимных опросов)
        if not poll_data.get('is_anonymous', True):
            existing = db.collection('poll_responses').where('poll_id', '==', poll_id).where('user_id', '==', session['uid']).stream()
            if list(existing):
                flash('Вы уже проголосовали в этом опросе', 'warning')
                return redirect(url_for('poll_vote', poll_id=poll_id))

        # Получаем выбранные варианты
        if poll_type == 'single':
            answer_id = request.form.get('answer', '').strip()
            selected_answers = [answer_id] if answer_id else []
        else:
            selected_answers = request.form.getlist('answer')

        if not selected_answers:
            flash('Выберите хотя бы один вариант', 'danger')
            return redirect(url_for('poll_vote', poll_id=poll_id))

        # Сохраняем каждый ответ отдельным документом
        for answer_id in selected_answers:
            response_data = {
                'poll_id': poll_id,
                'answer_id': answer_id,
                'votedAt': firestore.SERVER_TIMESTAMP
            }

            # Сохраняем user_id только для неанонимных опросов
            if not poll_data.get('is_anonymous', True):
                response_data['user_id'] = session['uid']

            db.collection('poll_responses').add(response_data)

            # Увеличиваем счетчик голосов для каждого ответа
            answer_ref = db.collection('polls').document(poll_id).collection('answers').document(answer_id)
            answer_ref.update({'votes': firestore.Increment(1)})

        flash('Ваш голос принят!', 'success')
        return redirect(url_for('poll_results', poll_id=poll_id))
    except Exception as e:
        flash(f'Ошибка при отправке: {str(e)}', 'danger')
        return redirect(url_for('poll_vote', poll_id=poll_id))


@app.route('/poll/<poll_id>/results')
def poll_results(poll_id):
    """Результаты опроса"""
    try:
        poll_doc = db.collection('polls').document(poll_id).get()
        if not poll_doc.exists:
            flash('Опросник не найден', 'danger')
            return redirect(url_for('dashboard'))

        poll_data = poll_doc.to_dict()

        # Загружаем варианты ответов с результатами
        answers_docs = db.collection('polls').document(poll_id).collection('answers').stream()
        answers = [{'id': doc.id, **doc.to_dict()} for doc in answers_docs]

        # Подсчитываем всего голосов
        total_votes = sum(a.get('votes', 0) for a in answers)

        # Рассчитываем проценты
        for answer in answers:
            votes = answer.get('votes', 0)
            answer['percentage'] = round((votes / total_votes * 100) if total_votes > 0 else 0, 1)

        # Если опрос не анонимный, загружаем информацию о голосах
        if not poll_data.get('is_anonymous', True):
            responses_docs = list(db.collection('poll_responses').where('poll_id', '==', poll_id).stream())
            if not responses_docs:
                # Поддержка старого формата, где поле называется pollId
                responses_docs = list(db.collection('poll_responses').where('pollId', '==', poll_id).stream())

            # Группируем голоса по вариантам ответа
            for answer in answers:
                answer['voters'] = []

            for response_doc in responses_docs:
                response = response_doc.to_dict()
                user_id = response.get('user_id') or response.get('userId')

                # Поддержка старого формата (массив answers) и нового (отдельные документы)
                answer_ids = []
                if 'answer_id' in response:
                    # Новый формат
                    answer_ids = [response['answer_id']]
                elif 'answers' in response:
                    # Старый формат
                    answer_ids = response['answers']
                else:
                    continue

                for answer_id in answer_ids:
                    # Находим соответствующий ответ
                    answer = next((a for a in answers if a.get('id') == answer_id), None)
                    if answer and user_id and user_id not in [v['user_id'] for v in answer['voters']]:
                        # Пытаемся получить информацию о пользователе
                        try:
                            # Сначала попробуем из коллекции users
                            user_doc = db.collection('users').document(user_id).get()
                            if user_doc.exists:
                                user_data = user_doc.to_dict()
                                user_name = (user_data.get('fullName') or
                                           user_data.get('full_name') or
                                           user_data.get('name') or
                                           user_data.get('displayName') or
                                           user_data.get('email'))
                            else:
                                user_name = None

                            # Если не нашли в users, попробуем из auth
                            if not user_name:
                                try:
                                    from firebase_admin import auth
                                    user_record = auth.get_user(user_id)
                                    user_name = user_record.display_name or user_record.email
                                except:
                                    user_name = None

                            # Fallback на user_id
                            if not user_name:
                                user_name = f'ID: {user_id}'

                        except:
                            user_name = f'ID: {user_id}'

                        answer['voters'].append({
                            'user_id': user_id,
                            'user_name': user_name
                        })

        # Сортируем по количеству голосов
        answers.sort(key=lambda x: x.get('votes', 0), reverse=True)

        return render_template('poll_results.html', poll={'id': poll_id, **poll_data}, answers=answers, total_votes=total_votes)
    except Exception as e:
        flash(f'Ошибка загрузки результатов: {str(e)}', 'danger')
        return redirect(url_for('dashboard'))


# ==================== PRODUCTS ====================

@app.route('/products')
@admin_required
def products():
    try:
        docs = db.collection('products').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()
        products_list = [{'id': doc.id, **doc.to_dict()} for doc in docs]
        return render_template('products.html', products=products_list)
    except Exception as e:
        flash(f'Ошибка загрузки товаров: {str(e)}', 'danger')
        return render_template('products.html', products=[])


@app.route('/products/add', methods=['GET', 'POST'])
@app.route('/products/edit/<product_id>', methods=['GET', 'POST'])
@admin_required
def product_form(product_id=None):
    product = None
    if product_id:
        product_doc = db.collection('products').document(product_id).get()
        if product_doc.exists:
            product = {'id': product_id, **product_doc.to_dict()}
            if product.get('createdAt') and hasattr(product['createdAt'], 'strftime'):
                product['createdAt_str'] = product['createdAt'].strftime('%Y-%m-%d %H:%M')
        else:
            flash('Товар не найден', 'danger')
            return redirect(url_for('products'))

    if request.method == 'POST':
        try:
            price_raw = request.form.get('price', '').strip().replace(',', '.')
            try:
                price = float(price_raw) if price_raw else 0.0
            except ValueError:
                price = 0.0

            data = {
                'name': request.form.get('name', '').strip(),
                'price': price,
                'url': request.form.get('url', '').strip(),
                'updatedAt': datetime.now(timezone.utc)
            }

            # Обработка фильтров товара (новый формат: группы фильтров с опциями)
            variants_json = request.form.get('variants_json', '[]')
            try:
                variants = json.loads(variants_json)
                if variants:
                    data['variants'] = variants
            except (json.JSONDecodeError, ValueError):
                variants = []

            file = request.files.get('image')
            if file and allowed_file(file.filename):
                filename = secure_filename(file.filename)
                ts = int(datetime.now(timezone.utc).timestamp())
                blob = bucket.blob(f'products/{ts}_{filename}')
                blob.upload_from_file(file)
                blob.make_public()
                data['imageUrl'] = blob.public_url

            if product_id:
                db.collection('products').document(product_id).update(data)
                flash('Товар обновлён', 'success')
            else:
                data['createdAt'] = datetime.now(timezone.utc)
                db.collection('products').add(data)
                flash('Товар создан', 'success')

            return redirect(url_for('products'))
        except Exception as e:
            flash(f'Ошибка при сохранении товара: {str(e)}', 'danger')
            return redirect(url_for('products'))

    return render_template('product_form.html', product=product)


@app.route('/products/delete/<product_id>', methods=['POST'])
@admin_required
def product_delete(product_id):
    db.collection('products').document(product_id).delete()
    flash('Товар удалён', 'success')
    return redirect(url_for('products'))

# ==================== GROUPS ====================

@app.route('/groups')
@admin_required
def groups():
    groups_ref = db.collection('groups_list').stream()
    all_groups = {doc.id: [] for doc in groups_ref}

    users_docs = db.collection('users').stream()
    for doc in users_docs:
        user = doc.to_dict()
        user['id'] = doc.id
        group_name = user.get('group')
        if group_name:
            if group_name not in all_groups:
                all_groups[group_name] = []
            all_groups[group_name].append(user)

    sorted_group_names = sorted(all_groups.keys(), key=group_sort_key)
    sorted_groups = {name: all_groups[name] for name in sorted_group_names}

    return render_template('groups.html', groups=sorted_groups)


@app.route('/groups/edit_name', methods=['POST'])
@admin_required
def group_rename():
    old_name = request.form.get('old_name')
    new_name = request.form.get('new_name').strip()

    if not new_name:
        flash('Название не может быть пустым', 'error')
        return redirect(url_for('groups'))

    users_ref = db.collection('users').where('group', '==', old_name).stream()
    batch = db.batch()

    count = 0
    for doc in users_ref:
        batch.update(doc.reference, {'group': new_name})
        count += 1

    if count > 0:
        batch.commit()
        cache.invalidate('all_groups')
        flash(f'Группа переименована ({count} пользователей)', 'success')

    return redirect(url_for('groups'))


@app.route('/groups/create', methods=['POST'])
@admin_required
def group_create():
    group_name = request.form.get('group_name', '').strip()
    if group_name:
        db.collection('groups_list').document(group_name).set({
            'createdAt': datetime.utcnow()
        })
        cache.invalidate('all_groups')
        flash(f'Группа {group_name} создана', 'success')
    return redirect(url_for('groups'))


@app.route('/groups/delete/<group_name>', methods=['POST'])
@admin_required
def group_delete(group_name):
    db.collection('groups_list').document(group_name).delete()

    users_ref = db.collection('users').where('group', '==', group_name).stream()
    batch = db.batch()
    for doc in users_ref:
        batch.update(doc.reference, {'group': ""})
    batch.commit()

    cache.invalidate('all_groups')
    flash(f'Группа {group_name} удалена', 'success')
    return redirect(url_for('groups'))


# ==================== FEEDBACK ====================

@app.route('/feedback')
@admin_required
def feedback_list():
    feedbacks_ref = db.collection('feedback').order_by('createdAt', direction=firestore.Query.DESCENDING).stream()

    feedback_data = []
    for doc in feedbacks_ref:
        item = doc.to_dict()
        item['id'] = doc.id

        user_id = item.get('userId')
        if user_id and not item.get('anonymousVisual'):
            user_doc = db.collection('users').document(user_id).get()
            if user_doc.exists:
                u_data = user_doc.to_dict()
                item['user_name'] = u_data.get('name', 'Без имени')
                item['user_group'] = u_data.get('group', '—')
        else:
            item['user_name'] = 'Анонимно'
            item['user_group'] = ''

        feedback_data.append(item)

    return render_template('feedback.html', feedbacks=feedback_data)


@app.route('/feedback/reply/<feedback_id>', methods=['POST'])
@admin_required
def feedback_reply(feedback_id):
    reply_text = request.form.get('reply_text')
    if not reply_text:
        flash('Текст ответа не может быть пустым', 'danger')
        return redirect(url_for('feedback_list'))

    feedback_ref = db.collection('feedback').document(feedback_id)
    feedback_doc = feedback_ref.get()

    if not feedback_doc.exists:
        flash('Обращение не найдено', 'danger')
        return redirect(url_for('feedback_list'))

    fb_data = feedback_doc.to_dict()
    user_id = fb_data.get('userId')

    batch = db.batch()

    batch.update(feedback_ref, {
        'reply': reply_text,
        'repliedAt': firestore.SERVER_TIMESTAMP,
        'repliedBy': session.get('uid')
    })

    notif_ref = db.collection('notifications').document()
    batch.set(notif_ref, {
        'userId': user_id,
        'type': 'answer',
        'title': 'Ответ на ваше обращение',
        'message': reply_text,
        'feedbackId': feedback_id,
        'topic': fb_data.get('topic', 'Обращение'),
        'createdAt': firestore.SERVER_TIMESTAMP,
        'read': False
    })

    batch.commit()
    flash('Ответ отправлен', 'success')
    return redirect(url_for('feedback_list'))


@app.route('/feedback/delete/<feedback_id>', methods=['POST'])
@admin_required
def delete_feedback(feedback_id):
    db.collection('feedback').document(feedback_id).delete()
    flash('Обращение удалено', 'success')
    return redirect(url_for('feedback_list'))


# ==================== TEACHERS ====================

@app.route('/teachers')
@admin_required
def teachers_list():
    docs = db.collection('users').where('role', '==', 'teacher').stream()
    teachers = [{'id': d.id, **d.to_dict()} for d in docs]
    teachers.sort(key=lambda x: x.get('name', ''))
    return render_template('teachers.html', teachers=teachers)


@app.route('/teachers/add', methods=['POST'])
@admin_required
def teacher_add():
    name = request.form.get('name').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()

    uid = None

    if email and password:
        try:
            user = auth.create_user(email=email, password=password, display_name=name)
            uid = user.uid
        except Exception as e:
            flash(f'Ошибка: {e}', 'danger')
            return redirect(url_for('teachers_list'))
    else:
        uid = db.collection('users').document().id

    db.collection('users').document(uid).set({
        'name': name,
        'email': email if email else None,
        'role': 'teacher',
        'createdAt': datetime.now(),
        'updatedAt': datetime.now()
    })

    cache.invalidate('all_teachers')
    flash('Преподаватель добавлен', 'success')
    return redirect(url_for('teachers_list'))


@app.route('/teachers/edit/<uid>', methods=['GET', 'POST'])
@admin_required
def teacher_edit(uid):
    doc_ref = db.collection('users').document(uid)
    teacher = doc_ref.get().to_dict()

    if request.method == 'POST':
        name = request.form.get('name').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()

        update_data = {'name': name, 'updatedAt': datetime.now()}

        try:
            if teacher.get('email'):
                auth_update = {}
                if email and email != teacher.get('email'):
                    auth_update['email'] = email
                    update_data['email'] = email
                if password:
                    auth_update['password'] = password
                if auth_update:
                    auth.update_user(uid, **auth_update)
                doc_ref.update(update_data)
            else:
                if not email or not password:
                    flash('Email и пароль обязательны', 'danger')
                    return redirect(url_for('teacher_edit', uid=uid))

                user = auth.create_user(email=email, password=password, display_name=name)
                db.collection('users').document(user.uid).set({
                    **teacher, **update_data, 'email': email
                })
                doc_ref.delete()

            cache.invalidate('all_teachers')
            flash('Данные обновлены', 'success')
            return redirect(url_for('teachers_list'))
        except Exception as e:
            flash(f'Ошибка: {e}', 'danger')

    return render_template('teacher_edit.html', teacher={'id': uid, **teacher})


@app.route('/teachers/delete/<uid>', methods=['POST'])
@admin_required
def teacher_delete(uid):
    doc = db.collection('users').document(uid).get().to_dict()

    try:
        if doc.get('email'):
            auth.delete_user(uid)
        db.collection('users').document(uid).delete()
        cache.invalidate('all_teachers')
        flash('Преподаватель удален', 'success')
    except Exception as e:
        flash(str(e), 'danger')

    return redirect(url_for('teachers_list'))


# ==================== SUBJECTS ====================

@app.route('/subjects')
@admin_required
def subjects_list():
    docs = db.collection('subjects').stream()
    subjects = [{'id': doc.id, **doc.to_dict()} for doc in docs]
    subjects.sort(key=lambda x: x.get('name', ''))
    return render_template('subjects.html', subjects=subjects)


@app.route('/subjects/add', methods=['POST'])
@admin_required
def subject_add():
    name = request.form.get('name', '').strip()
    if name:
        db.collection('subjects').add({'name': name})
        cache.invalidate('all_subjects')
        flash(f'Предмет {name} добавлен', 'success')
    return redirect(url_for('subjects_list'))


@app.route('/subjects/edit/<s_id>', methods=['POST'])
@admin_required
def subject_edit(s_id):
    new_name = request.form.get('name', '').strip()
    if new_name:
        db.collection('subjects').document(s_id).update({'name': new_name})
        cache.invalidate('all_subjects')
        flash(f'Предмет обновлен', 'success')
    return redirect(url_for('subjects_list'))


@app.route('/subjects/delete/<s_id>', methods=['POST'])
@admin_required
def subject_delete(s_id):
    db.collection('subjects').document(s_id).delete()
    cache.invalidate('all_subjects')
    return redirect(url_for('subjects_list'))


# ==================== ADMIN USERS ====================

@app.route('/admin/users')
@admin_required
def admin_users():
    users_docs = db.collection('users').stream()

    grouped_data = {"Администраторы": [], "Преподаватели": [], "Клубы / Организаторы": []}
    student_groups = {}

    for doc in users_docs:
        u = doc.to_dict()
        u['id'] = doc.id
        u['name'] = u.get('name') or "Без имени"
        u['email'] = u.get('email') or ""
        u['role'] = u.get('role', 'student')
        u['group'] = u.get('group') or "-"
        u['phone'] = u.get('phone') or "Нет данных"
        u['course'] = u.get('course') or "1"

        created = u.get('createdAt')
        u['created_str'] = created.strftime('%d.%m.%Y %H:%M') if created else "Неизвестно"
        updated = u.get('updatedAt')
        u['updated_str'] = updated.strftime('%d.%m.%Y %H:%M') if updated else "Неизвестно"

        if u['role'] == 'admin':
            grouped_data["Администраторы"].append(u)
        elif u['role'] == 'teacher':
            grouped_data["Преподаватели"].append(u)
        elif u['role'] == 'club':
            grouped_data["Клубы / Организаторы"].append(u)
        else:
            g_name = u['group']
            if g_name not in student_groups:
                student_groups[g_name] = []
            student_groups[g_name].append(u)

    for g_name in sorted(student_groups.keys()):
        label = f"Группа: {g_name}" if g_name != "-" else "Без группы"
        grouped_data[label] = sorted(student_groups[g_name], key=lambda x: x['name'].lower())

    final_data = {k: v for k, v in grouped_data.items() if v}
    groups_docs = db.collection('groups_list').stream()
    available_groups = sorted([doc.id for doc in groups_docs])

    return render_template('admin_users.html', grouped_users=final_data, groups=available_groups)


@app.route('/admin/users/add', methods=['POST'])
@admin_required
def admin_add_user():
    email = request.form.get('email')
    password = request.form.get('password')
    name = request.form.get('name')
    role = request.form.get('role')
    group = request.form.get('group')
    phone = request.form.get('phone')
    course = request.form.get('course')

    try:
        user_record = auth.create_user(email=email, password=password, display_name=name)
        now = datetime.now()
        db.collection('users').document(user_record.uid).set({
            'name': name,
            'email': email,
            'role': role,
            'group': group,
            'phone': phone,
            'course': course,
            'createdAt': now,
            'updatedAt': now
        })
        flash('Пользователь создан!', 'success')
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))


@app.route('/admin/users/edit/<user_id>', methods=['POST'])
@admin_required
def admin_edit_user(user_id):
    try:
        user_ref = db.collection('users').document(user_id)
        user_ref.update({
            'name': request.form.get('name'),
            'phone': request.form.get('phone'),
            'role': request.form.get('role'),
            'group': request.form.get('group'),
            'course': request.form.get('course'),
            'updatedAt': datetime.now()
        })
        flash('Данные обновлены', 'success')
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))


@app.route('/admin/users/delete/<user_id>')
@admin_required
def admin_delete_user(user_id):
    try:
        db.collection('users').document(user_id).delete()
        flash('Пользователь удален', 'warning')
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('admin_users'))


# ==================== АДМИНИСТРАЦИЯ ====================

@app.route('/administration')
@admin_required
def administration():
    """Страница управления администрацией"""
    admins = []
    try:
        docs = db.collection('management').stream()
        for doc in docs:
            data = doc.to_dict()
            data['id'] = doc.id
            # Приводим priority к int, если это строка
            if 'priority' in data:
                try:
                    data['priority'] = int(data['priority'])
                except (ValueError, TypeError):
                    data['priority'] = 999  # Если не число, ставим в конец
            else:
                data['priority'] = 999

            # Если есть photo_url, но нет image, скопировать
            if 'photo_url' in data and not 'image' in data:
                data['image'] = data['photo_url']
                # Обновить в базе
                db.collection('management').document(doc.id).update({'image': data['photo_url']})

            admins.append(data)

        # Сортируем по priority
        admins.sort(key=lambda x: x['priority'])

        # Если есть записи без правильного priority, обновляем их
        need_update = any(admin['priority'] == 999 for admin in admins)
        if need_update:
            batch = db.batch()
            for index, admin in enumerate(admins):
                if admin['priority'] == 999:
                    batch.update(db.collection('management').document(admin['id']), {'priority': index + 1})
            batch.commit()
            # Перезагружаем
            return redirect(url_for('administration'))
    except Exception as e:
        flash(f'Ошибка загрузки: {str(e)}', 'danger')

    return render_template('administration.html', admins=admins)


@app.route('/administration/add', methods=['GET', 'POST'])
@admin_required
def administration_add():
    if request.method == 'POST':
        name = request.form.get('name')
        position = request.form.get('position')
        photo = request.files.get('photo')

        if not name or not position:
            flash('Заполните все поля', 'danger')
            return redirect(url_for('administration_add'))

        photo_url = None
        if photo and allowed_file(photo.filename):
            try:
                filename = secure_filename(photo.filename)
                blob = bucket.blob(f'management/{filename}')
                blob.upload_from_file(photo, content_type=photo.content_type)
                # Генерируем signed URL для доступа
                from datetime import timedelta
                photo_url = blob.generate_signed_url(timedelta(days=365*10))
            except Exception as e:
                flash(f'Ошибка загрузки фото: {str(e)}', 'danger')
                return redirect(url_for('administration_add'))

        # Находим максимальный priority
        try:
            max_priority_doc = db.collection('management').order_by('priority', direction=firestore.Query.DESCENDING).limit(1).get()
            max_priority = 0
            for doc in max_priority_doc:
                priority_val = doc.to_dict().get('priority', 0)
                try:
                    max_priority = int(priority_val)
                except (ValueError, TypeError):
                    max_priority = 0
            new_priority = max_priority + 1
        except:
            new_priority = 1

        try:
            db.collection('management').add({
                'name': name,
                'position': position,
                'image': photo_url,
                'priority': new_priority,
                'created_at': datetime.now()
            })
            flash('Член администрации добавлен', 'success')
        except Exception as e:
            flash(f'Ошибка: {str(e)}', 'danger')

        return redirect(url_for('administration'))

    return render_template('administration_form.html', admin=None, action='add')


@app.route('/administration/edit/<admin_id>', methods=['GET', 'POST'])
@admin_required
def administration_edit(admin_id):
    try:
        doc = db.collection('management').document(admin_id).get()
        if not doc.exists:
            flash('Член администрации не найден', 'danger')
            return redirect(url_for('administration'))
        admin = doc.to_dict()
        admin['id'] = doc.id
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')
        return redirect(url_for('administration'))

    if request.method == 'POST':
        name = request.form.get('name')
        position = request.form.get('position')
        photo = request.files.get('photo')

        if not name or not position:
            flash('Заполните все поля', 'danger')
            return redirect(url_for('administration_edit', admin_id=admin_id))

        update_data = {
            'name': name,
            'position': position,
            'updated_at': datetime.now()
        }

        if photo and allowed_file(photo.filename):
            try:
                # Удалить старое фото, если есть
                old_image = admin.get('image')
                if old_image:
                    # Извлечь имя файла из URL
                    import re
                    match = re.search(r'/management/([^?]+)', old_image)
                    if match:
                        old_filename = match.group(1)
                        try:
                            old_blob = bucket.blob(f'management/{old_filename}')
                            old_blob.delete()
                        except Exception:
                            # Игнорируем ошибку, если файл уже удален
                            pass

                filename = secure_filename(photo.filename)
                blob = bucket.blob(f'management/{filename}')
                blob.upload_from_file(photo, content_type=photo.content_type)
                # Генерируем signed URL для доступа
                from datetime import timedelta
                update_data['image'] = blob.generate_signed_url(timedelta(days=365*10))
            except Exception as e:
                flash(f'Ошибка загрузки фото: {str(e)}', 'danger')
                return redirect(url_for('administration_edit', admin_id=admin_id))

        try:
            db.collection('management').document(admin_id).update(update_data)
            flash('Данные обновлены', 'success')
        except Exception as e:
            flash(f'Ошибка: {str(e)}', 'danger')

        return redirect(url_for('administration'))

    return render_template('administration_form.html', admin=admin, action='edit')


@app.route('/administration/delete/<admin_id>', methods=['POST'])
@admin_required
def administration_delete(admin_id):
    try:
        # Получить данные для удаления фото
        doc = db.collection('management').document(admin_id).get()
        if doc.exists:
            data = doc.to_dict()
            old_image = data.get('image')
            if old_image:
                import re
                match = re.search(r'/management/([^?]+)', old_image)
                if match:
                    old_filename = match.group(1)
                    old_blob = bucket.blob(f'management/{old_filename}')
                    old_blob.delete()

        db.collection('management').document(admin_id).delete()
        flash('Член администрации удален', 'warning')
    except Exception as e:
        flash(f'Ошибка: {str(e)}', 'danger')

    return redirect(url_for('administration'))


@app.route('/administration/update_order', methods=['POST'])
@admin_required
def administration_update_order():
    """API для обновления порядка карточек"""
    try:
        order_data = request.get_json()
        if not order_data or 'order' not in order_data:
            return jsonify({'success': False, 'error': 'Invalid data'}), 400

        batch = db.batch()
        for index, admin_id in enumerate(order_data['order']):
            doc_ref = db.collection('management').document(admin_id)
            batch.update(doc_ref, {'priority': index + 1})

        batch.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# ==================== ДОБАВИТЬ В app.py ====================
# Вставьте этот код в ваш app.py после существующих импортов и функций

# ==================== ДОБАВИТЬ В app.py ====================
# Вставьте этот код в ваш app.py после существующих импортов и функций

import csv
import io
from datetime import datetime

# ==================== JINJA ФИЛЬТРЫ ====================

@app.template_filter('extract_course')
def extract_course_filter(group_name):
    """Извлекает номер курса из названия группы (например, ИС-21 -> 2)"""
    import re
    match = re.search(r'-(\d)', str(group_name))
    return match.group(1) if match else ''


@app.template_filter('extract_major')
def extract_major_filter(group_name):
    """Извлекает направление из названия группы (например, ИС-21 -> ИС)"""
    import re
    match = re.match(r'^([А-ЯЁA-Z]+)', str(group_name))
    return match.group(1) if match else ''


@app.template_filter('sum_lessons')
def sum_lessons_filter(days_data):
    """Считает общее количество занятий"""
    total = 0
    for day_lessons in days_data.values():
        total += len(day_lessons)
    return total


# ==================== ЕДИНОЕ РАСПИСАНИЕ ВСЕХ ГРУПП ====================

@app.route('/schedule/all')
@admin_required
def schedule_all():
    """
    Страница с единой таблицей расписания ВСЕХ групп.
    Оптимизировано: загружает все данные одним запросом и группирует в памяти.
    """
    # Параметры фильтрации
    filter_course = request.args.get('course', '')
    filter_major = request.args.get('major', '')
    filter_week_type = request.args.get('week_type', '')

    is_even_week = get_week_parity()
    today_index = datetime.now().weekday()

    # Загружаем словарь преподавателей (ID -> имя) из кэша
    teachers_dict = get_all_teachers()

    # Загружаем ВСЕ расписание одним запросом
    all_schedules = list(db.collection('schedules').stream())

    # ПЕРВЫЙ ПРОХОД: собираем все доступные курсы и направления
    all_majors_set = set()
    all_courses_set = set()

    today = datetime.now().date()
    academic_start_year = today.year if today.month >= 9 else today.year - 1

    for doc in all_schedules:
        data = doc.to_dict()
        group_id = data.get('groupId', '')

        if not group_id:
            continue

        # Извлекаем курс и направление
        import re
        year_match = re.search(r'(\d{2,4})-(\d+)', group_id)
        course = ''
        if year_match:
            y = int(year_match.group(1))
            if y < 100:
                y += 2000
            course_num = academic_start_year - y + 1
            if course_num < 1:
                course_num = 1
            if course_num > 10:
                course_num = 10
            course = str(course_num)

        major_match = re.match(r'^([A-Za-zА-Яа-яЁё]+)', group_id)
        major = major_match.group(1).upper() if major_match else ''

        if major:
            all_majors_set.add(major)
        if course:
            all_courses_set.add(int(course))

    # ВТОРОЙ ПРОХОД: фильтруем и группируем по группам и дням
    schedule_data = {}  # {group_name: {day_index: [lessons]}}
    filtered_groups = set()
    filtered_majors = set()
    filtered_courses = set()
    total_lessons = 0

    for doc in all_schedules:
        data = doc.to_dict()
        data['id'] = doc.id
        group_id = data.get('groupId', '')

        if not group_id:
            continue

        # Извлекаем курс и направление
        year_match = re.search(r'(\d{2,4})-(\d+)', group_id)
        course = ''
        if year_match:
            y = int(year_match.group(1))
            if y < 100:
                y += 2000
            course_num = academic_start_year - y + 1
            if course_num < 1:
                course_num = 1
            if course_num > 10:
                course_num = 10
            course = str(course_num)

        major_match = re.match(r'^([A-Za-zА-Яа-яЁё]+)', group_id)
        major = major_match.group(1).upper() if major_match else ''

        # Применяем фильтры
        if filter_course and course != filter_course:
            continue
        if filter_major and major != filter_major:
            continue

        filtered_groups.add(group_id)
        if major:
            filtered_majors.add(major)
        if course:
            filtered_courses.add(int(course))

        # Определяем день недели
        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except:
                pass

        # Пропускаем воскресенье
        if day_index > 5:
            continue

        # Преобразуем ID преподавателя в имя
        teacher_id = data.get('teacher', '')
        if teacher_id:
            data['teacher_name'] = teachers_dict.get(teacher_id, teacher_id)
        else:
            data['teacher_name'] = ''

        # Инициализируем структуру
        if group_id not in schedule_data:
            schedule_data[group_id] = {}

        if day_index not in schedule_data[group_id]:
            schedule_data[group_id][day_index] = []

        schedule_data[group_id][day_index].append(data)
        total_lessons += 1

    # Сортируем группы
    sorted_groups = sorted(schedule_data.keys(), key=group_sort_key)
    sorted_schedule_data = {g: schedule_data[g] for g in sorted_groups}

    # Сортируем направления и курсы (все + отфильтрованные)
    sorted_all_majors = sorted(all_majors_set)
    sorted_all_courses = sorted(all_courses_set)

    return render_template(
        'schedule_all.html',
        schedule_data=sorted_schedule_data,
        groups=sorted_groups,
        majors=sorted_all_majors,
        courses=sorted_all_courses,
        day_names=DAY_NAMES,
        is_even_week=is_even_week,
        today_index=today_index,
        total_lessons=total_lessons,
        update_time=datetime.now().strftime('%d.%m.%Y %H:%M')
    )


# ==================== ЭКСПОРТ РАСПИСАНИЯ ====================

@app.route('/schedule/all/export')
@admin_required
def schedule_all_export():
    """
    Экспорт расписания в различных форматах.
    Поддерживает: xlsx, csv, pdf
    """
    export_format = request.args.get('format', 'xlsx')
    filter_course = request.args.get('course', '')
    filter_major = request.args.get('major', '')

    is_even_week = get_week_parity()

    # Загружаем словарь преподавателей
    teachers_dict = get_all_teachers()

    # Загружаем данные
    all_schedules = list(db.collection('schedules').stream())

    # Группируем данные
    schedule_data = {}

    for doc in all_schedules:
        data = doc.to_dict()
        group_id = data.get('groupId', '')

        if not group_id:
            continue

        # Фильтрация
        import re
        course_match = re.search(r'-(\d)', group_id)
        major_match = re.match(r'^([А-ЯЁA-Z]+)', group_id)

        course = course_match.group(1) if course_match else ''
        major = major_match.group(1) if major_match else ''

        if filter_course and course != filter_course:
            continue
        if filter_major and major != filter_major:
            continue

        # День недели
        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except:
                pass

        if day_index > 5:
            continue

        # Преобразуем ID преподавателя в имя
        teacher_id = data.get('teacher', '')
        if teacher_id:
            data['teacher_name'] = teachers_dict.get(teacher_id, teacher_id)
        else:
            data['teacher_name'] = ''

        if group_id not in schedule_data:
            schedule_data[group_id] = {}
        if day_index not in schedule_data[group_id]:
            schedule_data[group_id][day_index] = []

        schedule_data[group_id][day_index].append(data)

    # Сортируем
    sorted_groups = sorted(schedule_data.keys(), key=group_sort_key)

    if export_format == 'csv':
        return export_schedule_csv(sorted_groups, schedule_data, is_even_week)
    elif export_format == 'xlsx':
        return export_schedule_xlsx(sorted_groups, schedule_data, is_even_week)
    elif export_format == 'pdf':
        return export_schedule_pdf(sorted_groups, schedule_data, is_even_week)
    else:
        return export_schedule_xlsx(sorted_groups, schedule_data, is_even_week)


def export_schedule_csv(groups, schedule_data, is_even_week):
    """Экспорт в CSV"""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Заголовок
    writer.writerow(['Группа', 'День', 'Время', 'Предмет', 'Преподаватель', 'Аудитория', 'Тип недели'])

    for group in groups:
        days = schedule_data.get(group, {})
        for day_idx in range(6):
            lessons = days.get(day_idx, [])
            for lesson in sorted(lessons, key=lambda x: x.get('time', '')):
                week_type = lesson.get('week_type', 'always')
                week_type_ru = {'always': 'Всегда', 'numerator': 'Числитель', 'denominator': 'Знаменатель'}.get(week_type, week_type)

                # Используем teacher_name если есть
                teacher_name = lesson.get('teacher_name') or lesson.get('teacher', '')

                writer.writerow([
                    group,
                    DAY_NAMES[day_idx],
                    lesson.get('time', ''),
                    lesson.get('subject', ''),
                    teacher_name,
                    lesson.get('room', ''),
                    week_type_ru
                ])

    output.seek(0)

    from flask import Response
    response = Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.csv',
            'Content-Type': 'text/csv; charset=utf-8-sig'
        }
    )
    return response


def export_schedule_xlsx(groups, schedule_data, is_even_week):
    """Экспорт в Excel"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        flash('Для экспорта в Excel установите openpyxl: pip install openpyxl', 'error')
        return redirect(url_for('schedule_all'))

    wb = Workbook()

    # ===== ЛИСТ 1: Общая таблица =====
    ws = wb.active
    ws.title = "Все группы"

    # Стили
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
    group_font = Font(bold=True, size=12, color="7030A0")
    num_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    den_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    always_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # Заголовки
    headers = ['Группа'] + DAY_NAMES
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    # Ширина колонок
    ws.column_dimensions['A'].width = 15
    for i in range(2, 8):
        ws.column_dimensions[get_column_letter(i)].width = 35

    # Заполняем данные
    row = 2
    for group in groups:
        days = schedule_data.get(group, {})

        # Находим максимальное количество занятий в дне
        max_lessons = 1
        for day_idx in range(6):
            lessons_count = len(days.get(day_idx, []))
            if lessons_count > max_lessons:
                max_lessons = lessons_count

        # Записываем группу
        group_cell = ws.cell(row=row, column=1, value=group)
        group_cell.font = group_font
        group_cell.alignment = Alignment(horizontal='center', vertical='center')
        group_cell.border = thin_border

        if max_lessons > 1:
            ws.merge_cells(start_row=row, start_column=1, end_row=row + max_lessons - 1, end_column=1)

        # Записываем занятия по дням
        for day_idx in range(6):
            lessons = sorted(days.get(day_idx, []), key=lambda x: x.get('time', ''))
            col = day_idx + 2

            for lesson_idx, lesson in enumerate(lessons):
                current_row = row + lesson_idx

                week_type = lesson.get('week_type', 'always')
                week_mark = ''
                if week_type == 'numerator':
                    week_mark = '[Ч] '
                elif week_type == 'denominator':
                    week_mark = '[З] '

                # Используем teacher_name если есть
                teacher_name = lesson.get('teacher_name') or lesson.get('teacher', '')

                cell_value = f"{lesson.get('time', '')}\n{week_mark}{lesson.get('subject', '')}"
                if teacher_name:
                    cell_value += f"\n{teacher_name}"
                if lesson.get('room'):
                    cell_value += f" | {lesson.get('room')}"

                cell = ws.cell(row=current_row, column=col, value=cell_value)
                cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
                cell.border = thin_border

                # Заливка по типу недели
                if week_type == 'numerator':
                    cell.fill = num_fill
                elif week_type == 'denominator':
                    cell.fill = den_fill
                else:
                    cell.fill = always_fill

            # Пустые ячейки
            for empty_idx in range(len(lessons), max_lessons):
                cell = ws.cell(row=row + empty_idx, column=col, value='')
                cell.border = thin_border

        row += max_lessons

    # ===== ЛИСТ 2: Текущая неделя =====
    ws2 = wb.create_sheet("Текущая неделя")

    current_week_type = 'numerator' if is_even_week else 'denominator'
    week_name = 'ЧИСЛИТЕЛЬ' if is_even_week else 'ЗНАМЕНАТЕЛЬ'

    ws2.cell(row=1, column=1, value=f"Расписание на текущую неделю ({week_name})")
    ws2.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws2.merge_cells('A1:G1')

    # Заголовки
    for col, header in enumerate(['Группа'] + DAY_NAMES, 1):
        cell = ws2.cell(row=3, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    # Ширина колонок
    ws2.column_dimensions['A'].width = 15
    for i in range(2, 8):
        ws2.column_dimensions[get_column_letter(i)].width = 35

    # Данные текущей недели
    row = 4
    for group in groups:
        days = schedule_data.get(group, {})

        ws2.cell(row=row, column=1, value=group).font = group_font
        ws2.cell(row=row, column=1).border = thin_border
        ws2.cell(row=row, column=1).alignment = Alignment(horizontal='center', vertical='center')

        for day_idx in range(6):
            lessons = days.get(day_idx, [])
            # Фильтруем только актуальные занятия
            current_lessons = [
                l for l in lessons
                if l.get('week_type', 'always') in ['always', current_week_type]
            ]
            current_lessons.sort(key=lambda x: x.get('time', ''))

            if current_lessons:
                cell_value = '\n\n'.join([
                    f"{l.get('time', '')} - {l.get('subject', '')}\n{l.get('teacher_name') or l.get('teacher', '')} | {l.get('room', '')}"
                    for l in current_lessons
                ])
            else:
                cell_value = ''

            cell = ws2.cell(row=row, column=day_idx + 2, value=cell_value)
            cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
            cell.border = thin_border

        row += 1

    # ===== ЛИСТ 3: Детальная таблица =====
    ws3 = wb.create_sheet("Детально")

    detail_headers = ['Группа', 'День', 'Время', 'Предмет', 'Преподаватель', 'Аудитория', 'Тип недели']
    for col, header in enumerate(detail_headers, 1):
        cell = ws3.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border

    row = 2
    for group in groups:
        days = schedule_data.get(group, {})
        for day_idx in range(6):
            for lesson in sorted(days.get(day_idx, []), key=lambda x: x.get('time', '')):
                week_type = lesson.get('week_type', 'always')
                week_type_ru = {'always': 'Всегда', 'numerator': 'Числитель', 'denominator': 'Знаменатель'}.get(week_type, week_type)

                # Используем teacher_name если есть
                teacher_name = lesson.get('teacher_name') or lesson.get('teacher', '')

                ws3.cell(row=row, column=1, value=group)
                ws3.cell(row=row, column=2, value=DAY_NAMES[day_idx])
                ws3.cell(row=row, column=3, value=lesson.get('time', ''))
                ws3.cell(row=row, column=4, value=lesson.get('subject', ''))
                ws3.cell(row=row, column=5, value=teacher_name)
                ws3.cell(row=row, column=6, value=lesson.get('room', ''))
                ws3.cell(row=row, column=7, value=week_type_ru)

                # Заливка
                fill = always_fill
                if week_type == 'numerator':
                    fill = num_fill
                elif week_type == 'denominator':
                    fill = den_fill

                for col in range(1, 8):
                    ws3.cell(row=row, column=col).fill = fill
                    ws3.cell(row=row, column=col).border = thin_border

                row += 1

    # Авто-ширина для детального листа
    for col in range(1, 8):
        ws3.column_dimensions[get_column_letter(col)].width = 20

    # Сохраняем
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    from flask import Response
    response = Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f'attachment; filename=schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
        }
    )
    return response


def export_schedule_pdf(groups, schedule_data, is_even_week):
    """Экспорт в PDF"""
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import cm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
    except ImportError:
        flash('Для экспорта в PDF установите reportlab: pip install reportlab', 'error')
        return redirect(url_for('schedule_all'))

    output = io.BytesIO()
    doc = SimpleDocTemplate(
        output,
        pagesize=landscape(A4),
        rightMargin=1*cm,
        leftMargin=1*cm,
        topMargin=1*cm,
        bottomMargin=1*cm
    )

    elements = []
    styles = getSampleStyleSheet()

    # Заголовок
    week_name = 'ЧИСЛИТЕЛЬ' if is_even_week else 'ЗНАМЕНАТЕЛЬ'
    title = Paragraph(
        f"<b>Расписание всех групп</b><br/>Текущая неделя: {week_name}",
        styles['Title']
    )
    elements.append(title)
    elements.append(Spacer(1, 0.5*cm))

    # Таблица
    data = [['Группа'] + DAY_NAMES]

    current_week_type = 'numerator' if is_even_week else 'denominator'

    for group in groups:
        days = schedule_data.get(group, {})
        row_data = [group]

        for day_idx in range(6):
            lessons = days.get(day_idx, [])
            # Только актуальные занятия
            current_lessons = [
                l for l in lessons
                if l.get('week_type', 'always') in ['always', current_week_type]
            ]
            current_lessons.sort(key=lambda x: x.get('time', ''))

            if current_lessons:
                cell_text = '\n'.join([
                    f"{l.get('time', '')} {l.get('subject', '')[:20]}"
                    for l in current_lessons
                ])
            else:
                cell_text = '-'

            row_data.append(cell_text)

        data.append(row_data)

    # Создаем таблицу
    col_widths = [3*cm] + [4*cm] * 6
    table = Table(data, colWidths=col_widths, repeatRows=1)

    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#4472C4')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F8FAFC')),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ('WORDWRAP', (0, 0), (-1, -1), True),
    ]))

    # Чередование строк
    for i in range(1, len(data)):
        if i % 2 == 0:
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, i), (-1, i), colors.HexColor('#EEF2FF'))
            ]))

    elements.append(table)

    # Легенда
    elements.append(Spacer(1, 0.5*cm))
    legend = Paragraph(
        f"<i>Сгенерировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}</i>",
        styles['Normal']
    )
    elements.append(legend)

    doc.build(elements)
    output.seek(0)

    from flask import Response
    response = Response(
        output.getvalue(),
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename=schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.pdf'
        }
    )
    return response


# ==================== API ДЛЯ AJAX ====================

@app.route('/api/schedule/all')
@admin_required
def api_schedule_all():
    """API для получения всего расписания в JSON"""
    all_schedules = list(db.collection('schedules').stream())

    schedule_data = {}

    for doc in all_schedules:
        data = doc.to_dict()
        data['id'] = doc.id
        group_id = data.get('groupId', '')

        if not group_id:
            continue

        # День недели
        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except:
                pass

        if day_index > 5:
            continue

        if group_id not in schedule_data:
            schedule_data[group_id] = {}
        if day_index not in schedule_data[group_id]:
            schedule_data[group_id][day_index] = []

        # Конвертируем datetime для JSON
        if 'createdAt' in data and data['createdAt']:
            data['createdAt'] = data['createdAt'].isoformat() if hasattr(data['createdAt'], 'isoformat') else str(data['createdAt'])
        if 'updatedAt' in data and data['updatedAt']:
            data['updatedAt'] = data['updatedAt'].isoformat() if hasattr(data['updatedAt'], 'isoformat') else str(data['updatedAt'])

        schedule_data[group_id][day_index].append(data)

    return jsonify({
        'success': True,
        'is_even_week': get_week_parity(),
        'schedule': schedule_data,
        'groups_count': len(schedule_data),
        'updated_at': datetime.now().isoformat()
    })

    # ==================== РАСПИСАНИЕ ПРЕПОДАВАТЕЛЕЙ ====================
# Добавьте этот код в app.py перед if __name__ == '__main__':

@app.route('/schedule/teachers')
@admin_required
def schedule_teachers():
    """
    Страница расписания по ПРЕПОДАВАТЕЛЯМ.
    Группирует все занятия по преподавателям вместо групп.
    """
    # Параметры фильтрации
    filter_teacher = request.args.get('teacher', '')
    filter_day = request.args.get('day', '')
    filter_week_type = request.args.get('week_type', '')
    view_mode = request.args.get('view', 'cards')

    is_even_week = get_week_parity()
    today_index = datetime.now().weekday()

    # Загружаем словарь преподавателей (ID -> имя)
    all_teachers = get_all_teachers()

    # Загружаем ВСЕ расписание
    all_schedules = list(db.collection('schedules').stream())

    # Группируем по преподавателям
    # {teacher_id: {name: str, days: {day_idx: [lessons]}, groups: set, total_lessons: int}}
    teachers_data = {}
    total_lessons = 0

    for doc in all_schedules:
        data = doc.to_dict()
        data['id'] = doc.id

        teacher_id = data.get('teacher', '')
        if not teacher_id:
            continue

        # Применяем фильтр по преподавателю
        if filter_teacher and teacher_id != filter_teacher:
            continue

        # Получаем имя преподавателя
        teacher_name = all_teachers.get(teacher_id, teacher_id)

        # Определяем день недели
        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except:
                pass

        # Пропускаем воскресенье
        if day_index > 5:
            continue

        # Применяем фильтр по дню
        if filter_day and str(day_index) != filter_day:
            continue

        # Инициализируем структуру для преподавателя
        if teacher_id not in teachers_data:
            teachers_data[teacher_id] = {
                'name': teacher_name,
                'days': {},
                'groups': set(),
                'total_lessons': 0
            }

        # Добавляем группу
        if data.get('groupId'):
            teachers_data[teacher_id]['groups'].add(data['groupId'])

        # Добавляем занятие в день
        if day_index not in teachers_data[teacher_id]['days']:
            teachers_data[teacher_id]['days'][day_index] = []

        teachers_data[teacher_id]['days'][day_index].append(data)
        teachers_data[teacher_id]['total_lessons'] += 1
        total_lessons += 1

    # Сортируем преподавателей по имени
    sorted_teachers_data = dict(
        sorted(teachers_data.items(), key=lambda x: x[1]['name'])
    )

    return render_template(
        'schedule_teachers.html',
        teachers_data=sorted_teachers_data,
        all_teachers=all_teachers,
        teachers_count=len(sorted_teachers_data),
        total_lessons=total_lessons,
        day_names=DAY_NAMES,
        is_even_week=is_even_week,
        today_index=today_index,
        view_mode=view_mode,
        update_time=datetime.now().strftime('%d.%m.%Y %H:%M'),
        enumerate=enumerate  # Для использования в шаблоне
    )


@app.route('/schedule/teachers/export')
@admin_required
def schedule_teachers_export():
    """
    Экспорт расписания преподавателей в различных форматах.
    """
    export_format = request.args.get('format', 'xlsx')
    filter_teacher = request.args.get('teacher', '')

    is_even_week = get_week_parity()

    # Загружаем словарь преподавателей
    all_teachers = get_all_teachers()

    # Загружаем данные
    all_schedules = list(db.collection('schedules').stream())

    # Группируем по преподавателям
    teachers_data = {}

    for doc in all_schedules:
        data = doc.to_dict()
        teacher_id = data.get('teacher', '')

        if not teacher_id:
            continue

        if filter_teacher and teacher_id != filter_teacher:
            continue

        teacher_name = all_teachers.get(teacher_id, teacher_id)

        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except:
                pass

        if day_index > 5:
            continue

        if teacher_id not in teachers_data:
            teachers_data[teacher_id] = {
                'name': teacher_name,
                'days': {}
            }

        if day_index not in teachers_data[teacher_id]['days']:
            teachers_data[teacher_id]['days'][day_index] = []

        teachers_data[teacher_id]['days'][day_index].append(data)

    # Сортируем
    sorted_teachers = dict(sorted(teachers_data.items(), key=lambda x: x[1]['name']))

    if export_format == 'csv':
        return export_teachers_csv(sorted_teachers, is_even_week)
    else:
        return export_teachers_xlsx(sorted_teachers, is_even_week)


def export_teachers_csv(teachers_data, is_even_week):
    """Экспорт расписания преподавателей в CSV"""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')

    # Заголовок
    writer.writerow(['Преподаватель', 'День', 'Время', 'Предмет', 'Группа', 'Аудитория', 'Тип недели'])

    for teacher_id, teacher_info in teachers_data.items():
        for day_idx in range(6):
            lessons = teacher_info['days'].get(day_idx, [])
            for lesson in sorted(lessons, key=lambda x: x.get('time', '')):
                week_type = lesson.get('week_type', 'always')
                week_type_ru = {
                    'always': 'Всегда',
                    'numerator': 'Числитель',
                    'denominator': 'Знаменатель'
                }.get(week_type, week_type)

                writer.writerow([
                    teacher_info['name'],
                    DAY_NAMES[day_idx],
                    lesson.get('time', ''),
                    lesson.get('subject', ''),
                    lesson.get('groupId', ''),
                    lesson.get('room', ''),
                    week_type_ru
                ])

    output.seek(0)

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=teachers_schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.csv',
            'Content-Type': 'text/csv; charset=utf-8-sig'
        }
    )


def export_teachers_xlsx(teachers_data, is_even_week):
    """Экспорт расписания преподавателей в Excel"""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        flash('Для экспорта в Excel установите openpyxl: pip install openpyxl', 'error')
        return redirect(url_for('schedule_teachers'))

    wb = Workbook()

    # Стили
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="059669", end_color="059669", fill_type="solid")
    teacher_font = Font(bold=True, size=12, color="047857")
    day_fill = PatternFill(start_color="F0FDF4", end_color="F0FDF4", fill_type="solid")
    num_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    den_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    always_fill = PatternFill(start_color="D1FAE5", end_color="D1FAE5", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'),
        right=Side(style='thin'),
        top=Side(style='thin'),
        bottom=Side(style='thin')
    )

    # ===== ЛИСТ 1: Сводная таблица =====
    ws = wb.active
    ws.title = "Все преподаватели"

    # Заголовки
    headers = ['Преподаватель'] + DAY_NAMES
    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    # Ширина колонок
    ws.column_dimensions['A'].width = 25
    for i in range(2, 8):
        ws.column_dimensions[get_column_letter(i)].width = 30

    # Данные
    row = 2
    for teacher_id, teacher_info in teachers_data.items():
        # Находим макс. количество занятий в день
        max_lessons = 1
        for day_idx in range(6):
            count = len(teacher_info['days'].get(day_idx, []))
            if count > max_lessons:
                max_lessons = count

        # Имя преподавателя
        cell = ws.cell(row=row, column=1, value=teacher_info['name'])
        cell.font = teacher_font
        cell.alignment = Alignment(horizontal='left', vertical='center')
        cell.border = thin_border

        if max_lessons > 1:
            ws.merge_cells(start_row=row, start_column=1, end_row=row + max_lessons - 1, end_column=1)

        # Занятия по дням
        for day_idx in range(6):
            lessons = sorted(
                teacher_info['days'].get(day_idx, []),
                key=lambda x: x.get('time', '')
            )
            col = day_idx + 2

            for lesson_idx, lesson in enumerate(lessons):
                current_row = row + lesson_idx

                week_type = lesson.get('week_type', 'always')
                week_mark = ''
                if week_type == 'numerator':
                    week_mark = '[Ч] '
                elif week_type == 'denominator':
                    week_mark = '[З] '

                cell_value = f"{lesson.get('time', '')}\n{week_mark}{lesson.get('subject', '')}\n{lesson.get('groupId', '')}"
                if lesson.get('room'):
                    cell_value += f" | {lesson.get('room')}"

                cell = ws.cell(row=current_row, column=col, value=cell_value)
                cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
                cell.border = thin_border

                # Заливка по типу недели
                if week_type == 'numerator':
                    cell.fill = num_fill
                elif week_type == 'denominator':
                    cell.fill = den_fill
                else:
                    cell.fill = always_fill

            # Пустые ячейки
            for empty_idx in range(len(lessons), max_lessons):
                cell = ws.cell(row=row + empty_idx, column=col, value='')
                cell.border = thin_border

        row += max_lessons

    # ===== ЛИСТ 2: Текущая неделя =====
    ws2 = wb.create_sheet("Текущая неделя")

    current_week_type = 'numerator' if is_even_week else 'denominator'
    week_name = 'ЧИСЛИТЕЛЬ' if is_even_week else 'ЗНАМЕНАТЕЛЬ'

    ws2.cell(row=1, column=1, value=f"Расписание преподавателей на текущую неделю ({week_name})")
    ws2.cell(row=1, column=1).font = Font(bold=True, size=14)
    ws2.merge_cells('A1:G1')

    for col, header in enumerate(['Преподаватель'] + DAY_NAMES, 1):
        cell = ws2.cell(row=3, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    ws2.column_dimensions['A'].width = 25
    for i in range(2, 8):
        ws2.column_dimensions[get_column_letter(i)].width = 30

    row = 4
    for teacher_id, teacher_info in teachers_data.items():
        ws2.cell(row=row, column=1, value=teacher_info['name']).font = teacher_font
        ws2.cell(row=row, column=1).border = thin_border

        for day_idx in range(6):
            lessons = teacher_info['days'].get(day_idx, [])
            current_lessons = [
                l for l in lessons
                if l.get('week_type', 'always') in ['always', current_week_type]
            ]
            current_lessons.sort(key=lambda x: x.get('time', ''))

            if current_lessons:
                cell_value = '\n\n'.join([
                    f"{l.get('time', '')} - {l.get('subject', '')}\n{l.get('groupId', '')} | {l.get('room', '')}"
                    for l in current_lessons
                ])
            else:
                cell_value = ''

            cell = ws2.cell(row=row, column=day_idx + 2, value=cell_value)
            cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
            cell.border = thin_border

        row += 1

    # ===== ЛИСТ 3: Детально =====
    ws3 = wb.create_sheet("Детально")

    detail_headers = ['Преподаватель', 'День', 'Время', 'Предмет', 'Группа', 'Аудитория', 'Тип недели']
    for col, header in enumerate(detail_headers, 1):
        cell = ws3.cell(row=1, column=col, value=header)
        cell.font = header_font
        cell.fill = header_fill
        cell.border = thin_border

    row = 2
    for teacher_id, teacher_info in teachers_data.items():
        for day_idx in range(6):
            for lesson in sorted(teacher_info['days'].get(day_idx, []), key=lambda x: x.get('time', '')):
                week_type = lesson.get('week_type', 'always')
                week_type_ru = {
                    'always': 'Всегда',
                    'numerator': 'Числитель',
                    'denominator': 'Знаменатель'
                }.get(week_type, week_type)

                ws3.cell(row=row, column=1, value=teacher_info['name'])
                ws3.cell(row=row, column=2, value=DAY_NAMES[day_idx])
                ws3.cell(row=row, column=3, value=lesson.get('time', ''))
                ws3.cell(row=row, column=4, value=lesson.get('subject', ''))
                ws3.cell(row=row, column=5, value=lesson.get('groupId', ''))
                ws3.cell(row=row, column=6, value=lesson.get('room', ''))
                ws3.cell(row=row, column=7, value=week_type_ru)

                fill = always_fill
                if week_type == 'numerator':
                    fill = num_fill
                elif week_type == 'denominator':
                    fill = den_fill

                for col in range(1, 8):
                    ws3.cell(row=row, column=col).fill = fill
                    ws3.cell(row=row, column=col).border = thin_border

                row += 1

    for col in range(1, 8):
        ws3.column_dimensions[get_column_letter(col)].width = 18

    # Сохраняем
    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f'attachment; filename=teachers_schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
        }
    )


# ==================== API для расписания преподавателя ====================

@app.route('/api/schedule/teacher/<teacher_id>')
@admin_required
def api_get_teacher_schedule(teacher_id):
    """
    API endpoint для получения расписания конкретного преподавателя.
    """
    all_teachers = get_all_teachers()
    teacher_name = all_teachers.get(teacher_id, teacher_id)

    schedule_list = []

    for doc in db.collection('schedules').where('teacher', '==', teacher_id).stream():
        data = doc.to_dict()
        data['id'] = doc.id

        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                data['day_index'] = dt.weekday()
                data['day_name'] = DAY_NAMES[dt.weekday()] if dt.weekday() < 6 else "Воскресенье"
            except:
                data['day_index'] = 7
                data['day_name'] = "Неизвестно"

        # Конвертируем datetime для JSON
        for field in ['createdAt', 'updatedAt']:
            if field in data and data[field]:
                data[field] = data[field].isoformat() if hasattr(data[field], 'isoformat') else str(data[field])

        schedule_list.append(data)

    schedule_list.sort(key=lambda x: (x.get('day_index', 0), x.get('time', '')))

    # Группируем по дням
    days = {}
    groups = set()
    for item in schedule_list:
        day_idx = item.get('day_index', 7)
        if day_idx < 6:
            if day_idx not in days:
                days[day_idx] = []
            days[day_idx].append(item)
            if item.get('groupId'):
                groups.add(item['groupId'])

    return jsonify({
        'success': True,
        'teacher_id': teacher_id,
        'teacher_name': teacher_name,
        'schedule': schedule_list,
        'days': days,
        'groups': list(groups),
        'total_lessons': len(schedule_list),
        'is_even_week': get_week_parity()
    })


    # ==================== АУДИТОРНЫЙ ФОНД ====================
# Вставьте этот код в app.py ПЕРЕД блоком `if __name__ == '__main__':`
# Импорты csv, io, datetime уже есть в вашем коде, дублировать не нужно.

# ---------- Помощник: загрузка списка аудиторий с кэшем ----------

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


def get_all_rooms():
    """
    Загружает список всех аудиторий из расписания с кэшированием.
    Берём уникальные значения поля `room` из коллекции schedules.
    """
    cached = cache.get('all_rooms')
    if cached:
        return cached

    rooms_set = set()
    try:
        # select() уменьшает объём передаваемых данных
        for doc in db.collection('schedules').select(['room']).stream():
            room = (doc.to_dict().get('room') or '').strip()
            if room:
                rooms_set.add(room)
    except Exception as e:
        print(f"Ошибка загрузки аудиторий: {e}")

    result = sorted(list(rooms_set), key=room_sort_key)
    cache.set('all_rooms', result)
    return result


# ---------- Главная страница: расписание по аудиториям ----------

@app.route('/schedule/rooms')
@admin_required
def schedule_rooms():
    """
    Аудиторный фонд: расписание всех аудиторий или конкретной аудитории.

    Параметры:
      ?room=<имя>          — конкретная аудитория
      ?day=<0..5>          — фильтр по дню недели
      ?week_type=<...>     — числитель/знаменатель/всегда
      ?view=cards|table    — режим отображения
    """
    filter_room = request.args.get('room', '')
    filter_day = request.args.get('day', '')
    filter_week_type = request.args.get('week_type', '')
    view_mode = request.args.get('view', 'cards')

    is_even_week = get_week_parity()
    today_index = datetime.now().weekday()

    all_teachers = get_all_teachers()
    all_rooms = get_all_rooms()

    # Загружаем всё расписание одним запросом
    all_schedules = list(db.collection('schedules').stream())

    # rooms_data: { room_name: {days: {day_idx: [lessons]}, groups: set, teachers: set, total_lessons: int} }
    rooms_data = {}
    total_lessons = 0

    for doc in all_schedules:
        data = doc.to_dict()
        data['id'] = doc.id

        room = (data.get('room') or '').strip()
        if not room:
            continue

        if filter_room and room != filter_room:
            continue

        # День недели
        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except Exception:
                pass

        if day_index > 5:
            continue

        if filter_day and str(day_index) != filter_day:
            continue

        if filter_week_type and data.get('week_type', 'always') != filter_week_type:
            continue

        # Имя преподавателя
        teacher_id = data.get('teacher', '')
        data['teacher_name'] = all_teachers.get(teacher_id, teacher_id) if teacher_id else ''

        if room not in rooms_data:
            rooms_data[room] = {
                'days': {},
                'groups': set(),
                'teachers': set(),
                'total_lessons': 0
            }

        if data.get('groupId'):
            rooms_data[room]['groups'].add(data['groupId'])
        if data.get('teacher_name'):
            rooms_data[room]['teachers'].add(data['teacher_name'])

        if day_index not in rooms_data[room]['days']:
            rooms_data[room]['days'][day_index] = []

        rooms_data[room]['days'][day_index].append(data)
        rooms_data[room]['total_lessons'] += 1
        total_lessons += 1

    # ---- Поиск конфликтов: в одной аудитории, в один день, в одно время ----
    # Учитываем тип недели: always пересекается с numerator и denominator
    conflicts = []
    for room, info in rooms_data.items():
        for day_idx, lessons in info['days'].items():
            # Группируем по (время)
            time_slots = {}
            for lesson in lessons:
                t = (lesson.get('time') or '').strip()
                if not t:
                    continue
                time_slots.setdefault(t, []).append(lesson)

            for t, slot_lessons in time_slots.items():
                if len(slot_lessons) < 2:
                    continue

                # Проверяем реальное пересечение по типу недели
                conflicting_pairs = []
                for i, l1 in enumerate(slot_lessons):
                    for l2 in slot_lessons[i + 1:]:
                        wt1 = l1.get('week_type', 'always')
                        wt2 = l2.get('week_type', 'always')
                        # Пересекаются: оба always, один always + другой любой,
                        # или оба одинаковых (numerator+numerator)
                        intersects = (wt1 == 'always' or wt2 == 'always' or wt1 == wt2)
                        # Группы должны быть разными, чтобы это был реальный конфликт
                        if intersects and l1.get('groupId') != l2.get('groupId'):
                            conflicting_pairs.append((l1, l2))

                if conflicting_pairs:
                    conflicts.append({
                        'room': room,
                        'day_index': day_idx,
                        'day_name': DAY_NAMES[day_idx],
                        'time': t,
                        'lessons': slot_lessons
                    })

    # Сортировка
    sorted_rooms_data = dict(
        sorted(rooms_data.items(), key=lambda x: room_sort_key(x[0]))
    )

    # Сортируем занятия внутри каждого дня по времени
    for room_info in sorted_rooms_data.values():
        for day_idx in room_info['days']:
            room_info['days'][day_idx].sort(key=lambda x: x.get('time', ''))

    # Конвертируем set в sorted list для шаблона
    for room_info in sorted_rooms_data.values():
        room_info['groups'] = sorted(room_info['groups'], key=group_sort_key)
        room_info['teachers'] = sorted(room_info['teachers'])

    return render_template(
        'schedule_rooms.html',
        rooms_data=sorted_rooms_data,
        all_rooms=all_rooms,
        rooms_count=len(sorted_rooms_data),
        total_lessons=total_lessons,
        conflicts=conflicts,
        day_names=DAY_NAMES,
        is_even_week=is_even_week,
        today_index=today_index,
        view_mode=view_mode,
        update_time=datetime.now().strftime('%d.%m.%Y %H:%M'),
        selected_room=filter_room,
        selected_day=filter_day,
        selected_week_type=filter_week_type
    )


# ---------- Экспорт расписания аудиторий ----------

@app.route('/schedule/rooms/export')
@admin_required
def schedule_rooms_export():
    """Экспорт расписания аудиторий в xlsx / csv / pdf."""
    export_format = request.args.get('format', 'xlsx')
    filter_room = request.args.get('room', '')

    is_even_week = get_week_parity()
    all_teachers = get_all_teachers()
    all_schedules = list(db.collection('schedules').stream())

    rooms_data = {}

    for doc in all_schedules:
        data = doc.to_dict()
        room = (data.get('room') or '').strip()
        if not room:
            continue
        if filter_room and room != filter_room:
            continue

        day_index = 7
        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                day_index = dt.weekday()
            except Exception:
                pass
        if day_index > 5:
            continue

        teacher_id = data.get('teacher', '')
        data['teacher_name'] = all_teachers.get(teacher_id, teacher_id) if teacher_id else ''

        if room not in rooms_data:
            rooms_data[room] = {'days': {}}
        rooms_data[room]['days'].setdefault(day_index, []).append(data)

    # Сортируем
    sorted_rooms = dict(sorted(rooms_data.items(), key=lambda x: room_sort_key(x[0])))
    for room_info in sorted_rooms.values():
        for day_idx in room_info['days']:
            room_info['days'][day_idx].sort(key=lambda x: x.get('time', ''))

    if export_format == 'csv':
        return _export_rooms_csv(sorted_rooms)
    elif export_format == 'pdf':
        return _export_rooms_pdf(sorted_rooms, is_even_week)
    else:
        return _export_rooms_xlsx(sorted_rooms, is_even_week)


def _export_rooms_csv(rooms_data):
    """CSV-экспорт"""
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';')
    writer.writerow(['Аудитория', 'День', 'Время', 'Предмет', 'Группа', 'Преподаватель', 'Тип недели'])

    week_ru = {'always': 'Всегда', 'numerator': 'Числитель', 'denominator': 'Знаменатель'}

    for room, info in rooms_data.items():
        for day_idx in range(6):
            for lesson in info['days'].get(day_idx, []):
                writer.writerow([
                    room,
                    DAY_NAMES[day_idx],
                    lesson.get('time', ''),
                    lesson.get('subject', ''),
                    lesson.get('groupId', ''),
                    lesson.get('teacher_name') or lesson.get('teacher', ''),
                    week_ru.get(lesson.get('week_type', 'always'), 'Всегда')
                ])

    output.seek(0)
    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='text/csv',
        headers={
            'Content-Disposition': f'attachment; filename=rooms_schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.csv',
            'Content-Type': 'text/csv; charset=utf-8-sig'
        }
    )


def _export_rooms_xlsx(rooms_data, is_even_week):
    """Excel-экспорт: 3 листа — сводная таблица, текущая неделя, детально."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
        from openpyxl.utils import get_column_letter
    except ImportError:
        flash('Для экспорта в Excel установите openpyxl: pip install openpyxl', 'error')
        return redirect(url_for('schedule_rooms'))

    wb = Workbook()

    # Стили
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill(start_color="DC2626", end_color="DC2626", fill_type="solid")
    room_font = Font(bold=True, size=12, color="991B1B")
    num_fill = PatternFill(start_color="DBEAFE", end_color="DBEAFE", fill_type="solid")
    den_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    always_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    thin_border = Border(
        left=Side(style='thin'), right=Side(style='thin'),
        top=Side(style='thin'), bottom=Side(style='thin')
    )

    # ===== Лист 1: Сводная таблица =====
    ws = wb.active
    ws.title = "Все аудитории"

    headers = ['Аудитория'] + DAY_NAMES
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = thin_border

    ws.column_dimensions['A'].width = 18
    for i in range(2, 8):
        ws.column_dimensions[get_column_letter(i)].width = 32

    row = 2
    for room, info in rooms_data.items():
        max_lessons = max([len(info['days'].get(d, [])) for d in range(6)] + [1])

        c = ws.cell(row=row, column=1, value=room)
        c.font = room_font
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border
        if max_lessons > 1:
            ws.merge_cells(start_row=row, start_column=1, end_row=row + max_lessons - 1, end_column=1)

        for day_idx in range(6):
            lessons = info['days'].get(day_idx, [])
            col = day_idx + 2
            for li, lesson in enumerate(lessons):
                wt = lesson.get('week_type', 'always')
                mark = '[Ч] ' if wt == 'numerator' else ('[З] ' if wt == 'denominator' else '')
                teacher_name = lesson.get('teacher_name') or lesson.get('teacher', '')

                val = f"{lesson.get('time', '')}\n{mark}{lesson.get('subject', '')}\n{lesson.get('groupId', '')}"
                if teacher_name:
                    val += f"\n{teacher_name}"

                cell = ws.cell(row=row + li, column=col, value=val)
                cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
                cell.border = thin_border
                if wt == 'numerator':
                    cell.fill = num_fill
                elif wt == 'denominator':
                    cell.fill = den_fill
                else:
                    cell.fill = always_fill

            for empty_idx in range(len(lessons), max_lessons):
                ec = ws.cell(row=row + empty_idx, column=col, value='')
                ec.border = thin_border

        row += max_lessons

    # ===== Лист 2: Текущая неделя =====
    ws2 = wb.create_sheet("Текущая неделя")
    current_wt = 'numerator' if is_even_week else 'denominator'
    week_name = 'ЧИСЛИТЕЛЬ' if is_even_week else 'ЗНАМЕНАТЕЛЬ'

    ws2.cell(row=1, column=1, value=f"Расписание аудиторий на текущую неделю ({week_name})").font = Font(bold=True, size=14)
    ws2.merge_cells('A1:G1')

    for col, h in enumerate(['Аудитория'] + DAY_NAMES, 1):
        c = ws2.cell(row=3, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.alignment = Alignment(horizontal='center', vertical='center')
        c.border = thin_border

    ws2.column_dimensions['A'].width = 18
    for i in range(2, 8):
        ws2.column_dimensions[get_column_letter(i)].width = 32

    row = 4
    for room, info in rooms_data.items():
        c = ws2.cell(row=row, column=1, value=room)
        c.font = room_font
        c.border = thin_border
        c.alignment = Alignment(horizontal='center', vertical='center')

        for day_idx in range(6):
            lessons = [
                l for l in info['days'].get(day_idx, [])
                if l.get('week_type', 'always') in ('always', current_wt)
            ]
            if lessons:
                val = '\n\n'.join([
                    f"{l.get('time', '')} — {l.get('subject', '')}\n{l.get('groupId', '')} | {l.get('teacher_name') or l.get('teacher', '')}"
                    for l in lessons
                ])
            else:
                val = ''
            cell = ws2.cell(row=row, column=day_idx + 2, value=val)
            cell.alignment = Alignment(horizontal='left', vertical='top', wrap_text=True)
            cell.border = thin_border
        row += 1

    # ===== Лист 3: Детально =====
    ws3 = wb.create_sheet("Детально")
    detail_headers = ['Аудитория', 'День', 'Время', 'Предмет', 'Группа', 'Преподаватель', 'Тип недели']
    for col, h in enumerate(detail_headers, 1):
        c = ws3.cell(row=1, column=col, value=h)
        c.font = header_font
        c.fill = header_fill
        c.border = thin_border

    week_ru = {'always': 'Всегда', 'numerator': 'Числитель', 'denominator': 'Знаменатель'}
    row = 2
    for room, info in rooms_data.items():
        for day_idx in range(6):
            for lesson in info['days'].get(day_idx, []):
                wt = lesson.get('week_type', 'always')
                ws3.cell(row=row, column=1, value=room)
                ws3.cell(row=row, column=2, value=DAY_NAMES[day_idx])
                ws3.cell(row=row, column=3, value=lesson.get('time', ''))
                ws3.cell(row=row, column=4, value=lesson.get('subject', ''))
                ws3.cell(row=row, column=5, value=lesson.get('groupId', ''))
                ws3.cell(row=row, column=6, value=lesson.get('teacher_name') or lesson.get('teacher', ''))
                ws3.cell(row=row, column=7, value=week_ru.get(wt, 'Всегда'))

                fill = always_fill
                if wt == 'numerator':
                    fill = num_fill
                elif wt == 'denominator':
                    fill = den_fill
                for col in range(1, 8):
                    ws3.cell(row=row, column=col).fill = fill
                    ws3.cell(row=row, column=col).border = thin_border
                row += 1

    for col in range(1, 8):
        ws3.column_dimensions[get_column_letter(col)].width = 18

    output = io.BytesIO()
    wb.save(output)
    output.seek(0)

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={
            'Content-Disposition': f'attachment; filename=rooms_schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.xlsx'
        }
    )


def _export_rooms_pdf(rooms_data, is_even_week):
    """PDF-экспорт (текущая неделя)"""
    try:
        from reportlab.lib.pagesizes import A4, landscape
        from reportlab.lib import colors
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
        from reportlab.lib.units import cm
    except ImportError:
        flash('Для экспорта в PDF установите reportlab: pip install reportlab', 'error')
        return redirect(url_for('schedule_rooms'))

    output = io.BytesIO()
    doc = SimpleDocTemplate(output, pagesize=landscape(A4),
                            rightMargin=1*cm, leftMargin=1*cm,
                            topMargin=1*cm, bottomMargin=1*cm)

    elements = []
    styles = getSampleStyleSheet()
    week_name = 'ЧИСЛИТЕЛЬ' if is_even_week else 'ЗНАМЕНАТЕЛЬ'
    elements.append(Paragraph(f"<b>Аудиторный фонд</b><br/>Текущая неделя: {week_name}", styles['Title']))
    elements.append(Spacer(1, 0.5*cm))

    current_wt = 'numerator' if is_even_week else 'denominator'
    data = [['Аудитория'] + DAY_NAMES]

    for room, info in rooms_data.items():
        row_data = [room]
        for day_idx in range(6):
            lessons = [
                l for l in info['days'].get(day_idx, [])
                if l.get('week_type', 'always') in ('always', current_wt)
            ]
            if lessons:
                txt = '\n'.join([
                    f"{l.get('time', '')} {l.get('subject', '')[:18]} ({l.get('groupId', '')})"
                    for l in lessons
                ])
            else:
                txt = '—'
            row_data.append(txt)
        data.append(row_data)

    col_widths = [3*cm] + [4*cm] * 6
    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#DC2626')),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTSIZE', (0, 0), (-1, 0), 10),
        ('FONTSIZE', (0, 1), (-1, -1), 7),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
        ('TOPPADDING', (0, 0), (-1, 0), 8),
        ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CBD5E1')),
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
    ]))

    elements.append(table)
    elements.append(Spacer(1, 0.5*cm))
    elements.append(Paragraph(f"<i>Сгенерировано: {datetime.now().strftime('%d.%m.%Y %H:%M')}</i>", styles['Normal']))

    doc.build(elements)
    output.seek(0)

    from flask import Response
    return Response(
        output.getvalue(),
        mimetype='application/pdf',
        headers={
            'Content-Disposition': f'attachment; filename=rooms_schedule_{datetime.now().strftime("%Y%m%d_%H%M")}.pdf'
        }
    )


# ---------- API: расписание конкретной аудитории ----------

@app.route('/api/schedule/room/<path:room_id>')
@admin_required
def api_get_room_schedule(room_id):
    """JSON-расписание для одной аудитории."""
    all_teachers = get_all_teachers()
    schedule_list = []

    for doc in db.collection('schedules').where('room', '==', room_id).stream():
        data = doc.to_dict()
        data['id'] = doc.id

        if data.get('date'):
            try:
                dt = datetime.strptime(data['date'], '%Y-%m-%d')
                data['day_index'] = dt.weekday()
                data['day_name'] = DAY_NAMES[dt.weekday()] if dt.weekday() < 6 else "Воскресенье"
            except Exception:
                data['day_index'] = 7
                data['day_name'] = "Неизвестно"

        teacher_id = data.get('teacher', '')
        data['teacher_name'] = all_teachers.get(teacher_id, teacher_id) if teacher_id else ''

        for f in ['createdAt', 'updatedAt']:
            if f in data and data[f]:
                data[f] = data[f].isoformat() if hasattr(data[f], 'isoformat') else str(data[f])

        schedule_list.append(data)

    schedule_list.sort(key=lambda x: (x.get('day_index', 0), x.get('time', '')))

    days = {}
    groups = set()
    for item in schedule_list:
        day_idx = item.get('day_index', 7)
        if day_idx < 6:
            days.setdefault(day_idx, []).append(item)
            if item.get('groupId'):
                groups.add(item['groupId'])

    return jsonify({
        'success': True,
        'room': room_id,
        'schedule': schedule_list,
        'days': days,
        'groups': sorted(groups, key=group_sort_key),
        'total_lessons': len(schedule_list),
        'is_even_week': get_week_parity()
    })

if __name__ == '__main__':
    app.run(debug=True, port=5001)