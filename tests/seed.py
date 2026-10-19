# tests/seed.py — реалистичные тестовые данные для эмулятора Firebase (FOUNDATION).
#
#   python -m tests.seed            # очистить эмуляторы и засеять (нужны переменные эмулятора)
#   from tests.seed import reset_and_seed; data = reset_and_seed()
#
# Всё на русском, id детерминированные (см. SEED_IDS / возвращаемый dict), даты — относительно
# «сегодня». В расписании специально заложены накладки и ошибки данных (см. CONFLICT_CASES).
# ТОЛЬКО для эмулятора: без FIRESTORE_EMULATOR_HOST модуль откажется работать.

import io
import os
import random
import struct
import zlib
from datetime import datetime, timedelta, timezone

import requests

if not os.environ.get('FIRESTORE_EMULATOR_HOST'):
    raise RuntimeError('tests.seed работает только с эмулятором (запускайте через tests/run_tests.sh)')

from cms import core  # noqa: E402
from cms.applicant_login import DOMAIN  # noqa: E402
from cms.schedule_model import CALL_SCHEDULE, next_weekday_date  # noqa: E402

UTC = timezone.utc

ADMIN = {'uid': 'admin_main', 'email': 'admin@caspian.test', 'password': 'Admin12345', 'name': 'Сапарова Гульнара Ерлановна'}
TEACHER_ACCOUNT = {'uid': 't_ivanova', 'email': 'ivanova@caspian.test', 'password': 'Teacher123'}
APPLICANT_ACCOUNT = {'uid': 'ap_001', 'email': '77011234567@' + DOMAIN, 'password': 'secret12', 'login': '+77011234567'}

GROUPS = ['ПО 23-1', 'ПО 24-1', 'ПО 24-2', 'ПО 25-1', 'ИС 23-1', 'ИС 24-1', 'ВТ 24-1', 'ВТ 25-1п',
          'ЭК 24-1', 'ЭК 25-1', 'МЕН 25-1']

# uid → (ФИО, основная дисциплина, аудитория)
TEACHERS = {
    't_ivanova': ('Иванова Анна Сергеевна', 'Математика', '101'),
    't_seitkali': ('Сейтқали Ержан Болатович', 'Казахский язык', '102'),
    't_petrov': ('Петров Дмитрий Игоревич', 'Программирование на Python', '204'),
    't_akhmetova': ('Ахметова Динара Маратовна', 'Физика', '301'),
    't_kim': ('Ким Виктор Александрович', 'Физическая культура', 'Спортзал'),
    't_nurlanov': ('Нурланов Арман Серикович', 'Базы данных', '205'),
    't_smirnova': ('Смирнова Елена Викторовна', 'Английский язык', '103'),
    't_zhaksybekov': ('Жаксыбеков Нурлан Ермекович', 'История Казахстана', '104'),
    't_omarova': ('Омарова Айгуль Кайратовна', 'Экономика организации', '302'),
    't_li': ('Ли Сергей Николаевич', 'Компьютерные сети', 'А-12'),
    't_bekova': ('Бекова Сауле Тимуровна', 'Бухгалтерский учёт', '303'),
}
# Однофамилец (полный тёзка) — для проверки «неоднозначного ФИО»; в базовой сетке не ведёт занятий.
NAMESAKE = ('t_kim2', 'Ким Виктор Александрович')
LEGACY_TEACHER = ('legacy_t1', 'Старостин Пётр Иванович')

SUBJECTS = ['Математика', 'Казахский язык', 'Русский язык и литература', 'Английский язык', 'Физика',
            'История Казахстана', 'Информатика', 'Программирование на Python', 'Базы данных',
            'Веб-разработка', 'Компьютерные сети', 'Экономика организации', 'Бухгалтерский учёт',
            'Физическая культура', 'Менеджмент']

SPECIALTIES = [
    {'id': 'po', 'title': 'Программное обеспечение', 'titleKk': 'Бағдарламалық қамтамасыз ету',
     'duration': '3 г. 10 мес.', 'base': '9,11', 'form': 'очная', 'language': 'рус/каз', 'places': 50, 'price': '650 000 ₸/год'},
    {'id': 'is', 'title': 'Информационные системы', 'titleKk': 'Ақпараттық жүйелер',
     'duration': '2 г. 10 мес.', 'base': '11', 'form': 'очная', 'language': 'рус', 'places': 25, 'price': '600 000 ₸/год'},
    {'id': 'vt', 'title': 'Вычислительная техника', 'titleKk': 'Есептеу техникасы',
     'duration': '3 г. 10 мес.', 'base': '9', 'form': 'очная', 'language': 'рус/каз', 'places': 25, 'price': '620 000 ₸/год'},
    {'id': 'ek', 'title': 'Учёт и аудит', 'titleKk': 'Есеп және аудит',
     'duration': '2 г. 10 мес.', 'base': '9,11', 'form': 'очная', 'language': 'рус', 'places': 30, 'price': '550 000 ₸/год'},
    {'id': 'men', 'title': 'Менеджмент', 'titleKk': 'Менеджмент',
     'duration': '2 г. 10 мес.', 'base': '11', 'form': 'очная', 'language': 'рус/каз', 'places': 25, 'price': '550 000 ₸/год'},
    {'id': 'tur', 'title': 'Туризм', 'titleKk': 'Туризм',
     'duration': '2 г. 10 мес.', 'base': '9,11', 'form': 'очная', 'language': 'рус/англ', 'places': 20, 'price': '580 000 ₸/год'},
]

SOURCES = {
    'STAND1': {'title': 'Стенд в ТРЦ «Keruen City»', 'channel': 'stand', 'welcomeCoins': 50, 'active': True, 'scans': 214,
               'note': 'Стойка у главного входа, по выходным'},
    'SCHOOL25': {'title': 'Школа-гимназия №25', 'channel': 'school', 'welcomeCoins': 30, 'active': True, 'scans': 96,
                 'note': 'Профориентация 11-х классов'},
    'OPENDAY': {'title': 'День открытых дверей 2026', 'channel': 'event', 'welcomeCoins': 100, 'active': True, 'scans': 158,
                'note': 'Актовый зал колледжа'},
    'INSTA': {'title': 'Instagram @caspian.college', 'channel': 'social', 'welcomeCoins': 0, 'active': True, 'scans': 341,
              'note': 'Ссылка в шапке профиля'},
    'FLYER': {'title': 'Листовки в автобусах', 'channel': 'print', 'welcomeCoins': 20, 'active': False, 'scans': 12,
              'note': 'Кампания завершена'},
}

FIRST_NAMES = ['Айгерим', 'Нурсултан', 'Дана', 'Алихан', 'Аружан', 'Тимур', 'Мадина', 'Ерасыл', 'Камила', 'Данияр',
               'Асель', 'Арсен', 'Жанель', 'Санжар', 'Алина', 'Бекзат', 'Томирис', 'Ильяс', 'Сабина', 'Максим']
LAST_NAMES = ['Сапарова', 'Жумабаев', 'Ким', 'Ахметов', 'Нурланова', 'Иванов', 'Тулегенова', 'Серикбаев', 'Оспанова',
              'Ли', 'Бекова', 'Мухамеджанов', 'Каримова', 'Петров', 'Абдрахманова', 'Шин', 'Ермекова', 'Касымов']
CITIES = ['Актау', 'Актау', 'Актау', 'Жанаозен', 'Атырау', 'Форт-Шевченко', 'Алматы', 'Бейнеу']
SCHOOLS = ['Школа-гимназия №25', 'СШ №8', 'Лицей №3 им. Абая', 'НИШ Актау', 'СШ №12', 'СШ №17']
GRADES = ['9', '10', '11', '11', '11', 'college', 'graduate']
CRM_STATUSES = ['new'] * 8 + ['contacted'] * 6 + ['applied'] * 4 + ['enrolled'] * 3 + ['declined'] * 2


# ==================== ПОМОЩНИКИ ====================

def _png(width, height, color, accent=None, stripes=6):
    """Простая PNG-картинка (полосы бренда) без Pillow — для фото новостей/товаров в эмуляторе."""
    accent = accent or tuple(min(255, c + 40) for c in color)
    rows = []
    band = max(1, height // (stripes * 2))
    for y in range(height):
        c = accent if (y // band) % 2 else color
        rows.append(b'\x00' + bytes(c) * width)
    raw = b''.join(rows)

    def chunk(tag, data):
        return struct.pack('>I', len(data)) + tag + data + struct.pack('>I', zlib.crc32(tag + data) & 0xffffffff)
    return (b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', width, height, 8, 2, 0, 0, 0))
            + chunk(b'IDAT', zlib.compress(raw, 9)) + chunk(b'IEND', b''))


class _File(io.BytesIO):
    """Минимальная замена werkzeug FileStorage для core.upload_image()."""

    def __init__(self, data, filename):
        super().__init__(data)
        self.filename = filename


def upload_demo_images(folder, colors, size=(800, 600)):
    files = [_File(_png(size[0], size[1], c), 'photo_%d.png' % i) for i, c in enumerate(colors)]
    return core.upload_images(files, folder)


def _now():
    return datetime.now(UTC)


def _ago(days=0, hours=0, minutes=0):
    return _now() - timedelta(days=days, hours=hours, minutes=minutes)


class _Batch:
    """Батч с авто-коммитом по 400 операций."""

    def __init__(self):
        self.db = core.db
        self.batch = self.db.batch()
        self.n = 0

    def set(self, ref, data):
        self.batch.set(ref, data)
        self.n += 1
        if self.n >= 400:
            self.commit()

    def commit(self):
        if self.n:
            self.batch.commit()
        self.batch = self.db.batch()
        self.n = 0


# ==================== ОЧИСТКА ====================

def reset_emulators():
    """Очистить Firestore, Auth и Storage эмулятора, сбросить кэши CMS."""
    host = os.environ['FIRESTORE_EMULATOR_HOST']
    auth_host = os.environ.get('FIREBASE_AUTH_EMULATOR_HOST')
    project = core.PROJECT_ID
    s = requests.Session()
    s.trust_env = False  # без прокси для localhost
    r = s.delete('http://%s/emulator/v1/projects/%s/databases/(default)/documents' % (host, project), timeout=60)
    r.raise_for_status()
    if auth_host:
        s.delete('http://%s/emulator/v1/projects/%s/accounts' % (auth_host, project), timeout=60).raise_for_status()
    try:
        for blob in core.bucket.list_blobs():
            blob.delete()
    except Exception as e:  # пустой бакет эмулятора может ещё не существовать
        if e.__class__.__name__ != 'NotFound':
            raise
    core.cache.invalidate()


# ==================== ПОЛЬЗОВАТЕЛИ ====================

def seed_people(b):
    users = core.db.collection('users')
    core.fb_auth.create_user(uid=ADMIN['uid'], email=ADMIN['email'], password=ADMIN['password'], display_name=ADMIN['name'])
    b.set(users.document(ADMIN['uid']), {'role': 'admin', 'name': ADMIN['name'], 'email': ADMIN['email'],
                                         'phone': '+77015550101', 'createdAt': _ago(400)})
    b.set(users.document('admin_2'), {'role': 'admin', 'name': 'Мухамбетов Ербол Кайратович', 'email': 'erbol@caspian.test',
                                      'createdAt': _ago(200)})
    for i, (uid, (name, subject, room)) in enumerate(TEACHERS.items()):
        email = '%s@caspian.test' % uid[2:]
        if uid == TEACHER_ACCOUNT['uid']:
            core.fb_auth.create_user(uid=uid, email=TEACHER_ACCOUNT['email'], password=TEACHER_ACCOUNT['password'], display_name=name)
            email = TEACHER_ACCOUNT['email']
        elif i % 3 == 0:
            core.fb_auth.create_user(uid=uid, email=email, password='Teacher123', display_name=name)
        b.set(users.document(uid), {'role': 'teacher', 'name': name, 'email': email,
                                    'phone': '+7701%07d' % (2000000 + i), 'subject': subject, 'createdAt': _ago(300 - i)})
    b.set(users.document(NAMESAKE[0]), {'role': 'teacher', 'name': NAMESAKE[1], 'email': 'kim.v2@caspian.test',
                                        'subject': 'Информатика', 'createdAt': _ago(90)})
    b.set(core.db.collection('teachers').document(LEGACY_TEACHER[0]), {'name': LEGACY_TEACHER[1]})

    rnd = random.Random(7)
    students = []
    for i in range(24):
        uid = 'st_%03d' % (i + 1)
        group = GROUPS[i % len(GROUPS)]
        name = '%s %s' % (rnd.choice(LAST_NAMES), rnd.choice(FIRST_NAMES))
        b.set(users.document(uid), {'role': 'student', 'name': name, 'email': 'student%02d@caspian.test' % (i + 1),
                                    'group': group, 'course': str(min(4, max(1, 26 - int(group.split()[1][:2])))),
                                    'phone': '+7702%07d' % (3000000 + i), 'createdAt': _ago(rnd.randint(30, 700))})
        students.append(uid)
    b.set(users.document('club_robotics'), {'role': 'club', 'name': 'Клуб робототехники', 'email': 'robotics@caspian.test',
                                            'createdAt': _ago(120)})
    for g in GROUPS:
        b.set(core.db.collection('groups_list').document(g), {'name': g, 'createdAt': _ago(365)})
    for s in SUBJECTS:
        b.set(core.db.collection('subjects').document(), {'name': s})
    return students


# ==================== РАСПИСАНИЕ ====================

def _slots(wd):
    return CALL_SCHEDULE['monday' if wd == 0 else 'other']


BASE_SLOTS = {0: [1, 2, 3, 6], 'other': [0, 1, 2, 3]}  # индексы в сетке звонков


def _lesson(group, wd, time, teacher, subject, room, week='always', schema='both', today=None):
    """Документ занятия. schema: 'index' (как быстрое добавление в приложении — только day_index),
    'both' (конструктор приложения — day_index + date), 'date' (старый веб — только date)."""
    doc = {'groupId': group, 'time': time, 'subject': subject, 'teacher': teacher, 'room': room,
           'week_type': week, 'createdAt': _ago(60)}
    date_str = next_weekday_date(wd, today).strftime('%Y-%m-%d')
    if schema in ('index', 'both'):
        doc['day_index'] = wd
    if schema in ('date', 'both'):
        doc['date'] = date_str
    return doc


# Описание заложенных накладок/ошибок (id документа → что должно найтись). Тесты CONFLICTS опираются на это.
CONFLICT_CASES = {
    'cf_petrov_a': 'преподаватель: Петров одновременно у двух групп (Ср 13.00, разные аудитории) — critical',
    'cf_petrov_b': 'пара к cf_petrov_a',
    'cf_akhm_a': 'преподаватель: частичное пересечение Ахметовой 14.05-14.55 × 14.30-14.50 (Чт)',
    'cf_akhm_b': 'пара к cf_akhm_a',
    'cf_seit_always': 'преподаватель: Сейтқали always × numerator (Пт 15.00) — накладка только в числитель',
    'cf_seit_num': 'пара к cf_seit_always',
    'ok_omar_num': 'НЕ накладка: Омарова числитель × знаменатель (Пт 15.55)',
    'ok_omar_den': 'пара к ok_omar_num',
    'cf_stream_a': 'поток: Ким, Спортзал, физкультура, две группы (Пн 15.20) — info',
    'cf_stream_b': 'пара к cf_stream_a',
    'cf_dup_a': 'дубль: одинаковое занятие ЭК 24-1 (Вт 13.00) — warning',
    'cf_dup_b': 'пара к cf_dup_a',
    'cf_room_a': 'аудитория: «204» и «Каб. 204» в Ср 13.00 у разных преподавателей — critical (нормализация)',
    'cf_room_b': 'пара к cf_room_a (Смирнова uid)',
    'cf_byname': 'преподаватель по ФИО текстом (Смирнова) пересекается с cf_room_b — накладка через справочник',
    'cf_grp_sub_a': 'группа ПО 25-1: два предмета одновременно у разных преподавателей (Чт 15.00) — возможны подгруппы',
    'cf_grp_sub_b': 'пара к cf_grp_sub_a',
    'cf_grp_a': 'группа ИС 23-1: частичное пересечение разных предметов (Пт 13.00 × 13.30) — critical',
    'cf_grp_b': 'пара к cf_grp_a',
    'cf_dated_a': 'Ли: рекуррентное (day_index) Пн 14.00 у ПО 23-1',
    'cf_dated_b': 'Ли: только date (Пн) 14.00 у ИС 23-1 — накладка с cf_dated_a',
    'q_ambiguous': 'качество: ФИО «Ким Виктор Александрович» — два однофамильца',
    'q_orphan': 'качество: uid преподавателя не найден',
    'q_legacy': 'качество: преподаватель из старой коллекции teachers',
    'q_bad_time': 'качество: время не разобрать («после обеда»)',
    'q_reverse': 'качество: конец раньше начала (10.50-10.00)',
    'q_mismatch': 'качество: day_index=1 (Вт), а date — среда',
    'q_sunday': 'качество: воскресенье',
    'q_no_teacher': 'качество: нет преподавателя',
    'q_no_room': 'качество: нет аудитории',
    'q_virtual': 'аудитория «онлайн» — не физическая (не участвует в накладках аудиторий)',
    'q_bad_week': 'качество: неизвестный week_type «odd»',
    'q_unknown_group': 'качество: группы нет в groups_list',
    'q_ts_date': 'качество: date хранится как Timestamp',
    'q_no_day': 'качество: нет ни day_index, ни date',
    'q_late': 'качество: вне учебного дня (21.30-22.20)',
    'q_offgrid': 'качество: время вне сетки звонков (09:00-10:20)',
    'q_no_end': 'качество: нет конца времени («08.00») — конец угадан по сетке',
}


def seed_schedule(b, today=None):
    coll = core.db.collection('schedules')
    teacher_ids = list(TEACHERS)
    n = 0
    for gi, group in enumerate(GROUPS):
        for wd in range(5):
            slots = _slots(wd)
            idxs = BASE_SLOTS[0] if wd == 0 else BASE_SLOTS['other']
            for si, slot_idx in enumerate(idxs):
                # Латинский квадрат: у каждого преподавателя в слоте ровно одна группа → базовая сетка без накладок.
                uid = teacher_ids[(gi + si + wd) % len(teacher_ids)]
                name, subject, room = TEACHERS[uid]
                schema = ('index', 'both', 'date')[(gi + wd + si) % 3]
                time = slots[slot_idx]
                if schema == 'index':
                    time = time.replace('.', ':')  # приложение пишет «08:00-08:50»
                b.set(coll.document('base_%02d_%d_%d' % (gi, wd, si)),
                      _lesson(group, wd, time, uid, subject, room, schema=schema, today=today))
                n += 1
    # Числитель/знаменатель без накладок (Ср, слот 12.05-12.55)
    alt = [('ПО 24-1', 't_akhmetova', 'Физика', '301', 't_li', 'Компьютерные сети', 'А-12'),
           ('ИС 24-1', 't_li', 'Компьютерные сети', 'А-12', 't_akhmetova', 'Физика', '301')]
    for k, (g, t1, s1, r1, t2, s2, r2) in enumerate(alt):
        b.set(coll.document('alt_num_%d' % k), _lesson(g, 2, '12.05-12.55', t1, s1, r1, 'numerator', today=today))
        b.set(coll.document('alt_den_%d' % k), _lesson(g, 2, '12.05-12.55', t2, s2, r2, 'denominator', today=today))
        n += 2

    L = lambda *a, **k: _lesson(*a, today=today, **k)  # noqa: E731
    special = {
        'cf_petrov_a': L('ВТ 24-1', 2, '13.00-13.50', 't_petrov', 'Программирование на Python', '204'),
        'cf_petrov_b': L('МЕН 25-1', 2, '13.00-13.50', 't_petrov', 'Веб-разработка', '205', schema='index'),
        'cf_akhm_a': L('ПО 23-1', 3, '14.05-14.55', 't_akhmetova', 'Физика', '301'),
        'cf_akhm_b': L('ИС 23-1', 3, '14.30-14.50', 't_akhmetova', 'Физика', '303', schema='date'),
        'cf_seit_always': L('ПО 24-2', 4, '15.00-15.50', 't_seitkali', 'Казахский язык', '102'),
        'cf_seit_num': L('ЭК 24-1', 4, '15.00-15.50', 't_seitkali', 'Казахский язык', '104', 'numerator'),
        'ok_omar_num': L('ПО 24-1', 4, '15.55-16.45', 't_omarova', 'Экономика организации', '302', 'numerator'),
        'ok_omar_den': L('ВТ 24-1', 4, '15.55-16.45', 't_omarova', 'Экономика организации', '302', 'denominator'),
        'cf_stream_a': L('ВТ 24-1', 0, '15.20-16.10', 't_kim', 'Физическая культура', 'Спортзал'),
        'cf_stream_b': L('ВТ 25-1п', 0, '15.20-16.10', 't_kim', 'Физическая культура', 'Спортзал'),
        'cf_dup_a': L('ЭК 24-1', 1, '13.00-13.50', 't_bekova', 'Бухгалтерский учёт', '303'),
        'cf_dup_b': L('ЭК 24-1', 1, '13.00-13.50', 't_bekova', 'Бухгалтерский учёт', '303'),
        'cf_room_a': L('ИС 24-1', 2, '13.00-13.50', 't_nurlanov', 'Базы данных', '204'),
        'cf_room_b': L('ПО 25-1', 2, '13.00-13.50', 't_smirnova', 'Английский язык', 'Каб. 204'),
        'cf_byname': L('ЭК 25-1', 2, '13.00-13.50', 'Смирнова Елена Викторовна', 'Английский язык', '103', schema='index'),
        'cf_grp_sub_a': L('ПО 25-1', 3, '15.00-15.50', 't_smirnova', 'Английский язык', '103'),
        'cf_grp_sub_b': L('ПО 25-1', 3, '15.00-15.50', 't_zhaksybekov', 'Английский язык', '104'),
        'cf_grp_a': L('ИС 23-1', 4, '13.00-13.50', 't_li', 'Компьютерные сети', 'А-12'),
        'cf_grp_b': L('ИС 23-1', 4, '13.30-14.20', 't_ivanova', 'Математика', '101'),
        'cf_dated_a': L('ПО 23-1', 0, '14.00-14.50', 't_li', 'Компьютерные сети', 'А-12', schema='index'),
        'cf_dated_b': L('ИС 23-1', 0, '14.00-14.50', 't_li', 'Компьютерные сети', 'А-12', schema='date'),
        'q_ambiguous': L('МЕН 25-1', 1, '15.00-15.50', 'Ким Виктор Александрович', 'Информатика', '201', schema='index'),
        'q_orphan': L('ВТ 25-1п', 3, '13.00-13.50', 'uid_deleted_teacher_7', 'Веб-разработка', '205'),
        'q_legacy': L('ЭК 25-1', 4, '15.55-16.45', LEGACY_TEACHER[0], 'Менеджмент', '104'),
        'q_bad_time': L('ИС 24-1', 1, 'после обеда', 't_nurlanov', 'Базы данных', '205'),
        'q_reverse': L('ПО 24-2', 1, '10.50-10.00', 't_ivanova', 'Математика', '101'),
        'q_no_teacher': L('ВТ 24-1', 3, '15.55-16.45', '', 'Информатика', '201'),
        'q_no_room': L('ЭК 25-1', 3, '15.55-16.45', 't_omarova', 'Экономика организации', ''),
        'q_virtual': L('МЕН 25-1', 3, '15.55-16.45', 't_bekova', 'Менеджмент', 'онлайн'),
        'q_bad_week': L('ПО 24-2', 3, '15.55-16.45', 't_seitkali', 'Казахский язык', '102', 'odd'),
        'q_unknown_group': L('XX 99-9', 2, '15.00-15.50', 't_zhaksybekov', 'История Казахстана', '104'),
        'q_late': L('ИС 24-1', 4, '21.30-22.20', 't_li', 'Компьютерные сети', 'А-12'),
        'q_offgrid': L('ЭК 24-1', 2, '09:00-10:20', 't_omarova', 'Экономика организации', '302', schema='index'),
        'q_no_end': L('МЕН 25-1', 4, '08.00', 't_zhaksybekov', 'История Казахстана', '104'),
    }
    mismatch = L('ПО 23-1', 1, '15.55-16.45', 't_petrov', 'Веб-разработка', '204')
    mismatch['date'] = next_weekday_date(2, today).strftime('%Y-%m-%d')  # среда при day_index=1
    special['q_mismatch'] = mismatch
    sunday = L('ИС 24-1', 6, '10.00-10.50', 't_li', 'Компьютерные сети', 'А-12', schema='index')
    special['q_sunday'] = sunday
    ts = L('ВТ 24-1', 1, '15.55-16.45', 't_kim', 'Физическая культура', 'Спортзал', schema='index')
    del ts['day_index']
    ts['date'] = datetime.combine(next_weekday_date(1, today), datetime.min.time()).replace(tzinfo=UTC)
    special['q_ts_date'] = ts
    noday = L('ЭК 24-1', 1, '16.50-17.40', 't_bekova', 'Бухгалтерский учёт', '303', schema='index')
    del noday['day_index']
    special['q_no_day'] = noday
    for doc_id, data in special.items():
        b.set(coll.document(doc_id), data)
        n += 1

    logs = core.db.collection('schedule_logs')
    entries = [
        ('add', 'cf_room_b', None, special['cf_room_b'], 'ПО 25-1', 3),
        ('update', 'base_00_1_0', {'room': '101'}, {'room': '102'}, 'ПО 23-1', 30),
        ('delete', 'old_lesson_1', {'subject': 'Черчение', 'groupId': 'ВТ 24-1', 'time': '10.00-10.50',
                                    'teacher': 't_li', 'room': '204', 'date': '2026-10-14'}, None, 'ВТ 24-1', 120),
        ('create', 'base_02_2_1', None, {'subject': 'Физика', 'groupId': 'ПО 24-2', 'teacher': 't_akhmetova',
                                         'time': '08.55-09.45', 'room': '301', 'date': '2026-10-21'}, 'ПО 24-2', 600),
    ]
    for i, (action, sid, old, new, group, minutes) in enumerate(entries):
        b.set(logs.document('log_%02d' % i), {
            'action': action, 'schedule_id': sid, 'group_id': group, 'old_data': old or {}, 'new_data': new or {},
            'changed_by': ADMIN['uid'], 'changed_at': _ago(minutes=minutes), 'timestamp': _ago(minutes=minutes)})
    b.set(core.db.collection('schedule_conflict_acks').document('ack_demo'),
          {'hash': 'ack_demo', 'note': 'Объединённая лекция, согласовано с завучем', 'by': ADMIN['email'], 'at': _ago(2)})
    return n


# ==================== АБИТУРИЕНТЫ, КОИНЫ, МАГАЗИН ====================

def seed_applicants(b):
    rnd = random.Random(42)
    users = core.db.collection('users')
    crm = core.db.collection('applicant_crm')
    wallets = core.db.collection('coin_wallets')
    txs = core.db.collection('coin_transactions')
    source_pool = ['STAND1'] * 6 + ['SCHOOL25'] * 4 + ['OPENDAY'] * 5 + ['INSTA'] * 4 + ['APP'] * 5 + ['WEB'] * 3 + ['DOD2026'] * 2 + ['FLYER']
    applicants = []
    ref_codes = {}
    alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
    for i in range(42):
        uid = 'ap_%03d' % (i + 1)
        first, last = rnd.choice(FIRST_NAMES), rnd.choice(LAST_NAMES)
        if i == 0:
            login, email = APPLICANT_ACCOUNT['login'], APPLICANT_ACCOUNT['email']
        elif i % 3 == 0:
            login = '%s.%s%d' % (('aigerim', 'nursultan', 'dana', 'alikhan', 'aruzhan', 'timur')[i % 6], 'k', i)
            email = login + '@' + DOMAIN
        elif i == 7:
            login = email = 'old.applicant@mail.ru'  # старый аккаунт по реальному email
        else:
            digits = '7%03d%07d' % (rnd.choice([701, 702, 705, 707, 747, 771, 775, 777]), rnd.randint(1000000, 9999999))
            login = '+' + digits
            email = digits + '@' + DOMAIN
        # регистрации чаще за последние недели
        days = int(rnd.triangular(0, 75, 3))
        created = _ago(days=days, hours=rnd.randint(0, 20), minutes=rnd.randint(0, 59))
        source = source_pool[i % len(source_pool)]
        filled = rnd.random() < 0.55
        doc = {'role': 'applicant', 'login': login, 'email': email, 'source': source,
               'consents': {'pd': True, 'version': 2, 'acceptedAt': created}, 'marketingConsent': rnd.random() < 0.6,
               'createdAt': created, 'updatedAt': created}
        if login.startswith('+'):
            doc['phone'] = login
        if filled or rnd.random() < 0.4:
            doc['name'] = '%s %s' % (first, last)
            doc['city'] = rnd.choice(CITIES)
        if filled:
            doc['phone'] = doc.get('phone') or '+7705%07d' % rnd.randint(1000000, 9999999)
            doc['school'] = rnd.choice(SCHOOLS)
            doc['grade'] = rnd.choice(GRADES)
            doc['specialties'] = rnd.sample([s['title'] for s in SPECIALTIES], rnd.randint(1, 2))
        if i % 5 == 0:
            code = ''.join(rnd.choice(alphabet) for _ in range(6))
            doc['refCode'] = code
            ref_codes[code] = uid
        if i in (11, 17, 23):
            doc['referredBy'] = 'ap_001'
        if i == 4:
            doc['mustChangePassword'] = True
            doc['passwordResetAt'] = _ago(1)
        if i in (0, 4, 9, 14, 19, 24, 29, 34):
            core.fb_auth.create_user(uid=uid, email=email, password=APPLICANT_ACCOUNT['password'] if i == 0 else 'secret12')
        b.set(users.document(uid), doc)
        status = CRM_STATUSES[i % len(CRM_STATUSES)]
        history = [{'at': created, 'by': 'system', 'action': 'created', 'details': 'Регистрация (%s)' % source}]
        if status != 'new':
            history.append({'at': created + timedelta(days=1), 'by': ADMIN['email'], 'action': 'status',
                            'details': 'new → %s' % status})
        b.set(crm.document(uid), {'status': status, 'notes': 'Перезвонить после 18:00' if status == 'contacted' else '',
                                  'source': source, 'login': login, 'createdAt': created,
                                  'updatedAt': created + timedelta(days=1), 'updatedBy': ADMIN['uid'], 'history': history,
                                  **({'lastContactAt': created + timedelta(days=1)} if status != 'new' else {})})
        earned, spent = 0, 0
        welcome = SOURCES.get(source, {}).get('welcomeCoins', 0)
        if welcome and SOURCES[source]['active']:
            b.set(txs.document('welcome_' + uid), {'uid': uid, 'amount': welcome, 'type': 'welcome', 'source': source,
                                                   'reason': 'Приветственные коины', 'applied': True, 'createdAt': created})
            earned += welcome
        if filled:
            b.set(txs.document('quest_profile_' + uid), {'uid': uid, 'amount': 50, 'type': 'quest', 'questId': 'profile',
                                                         'reason': 'Заполнен профиль', 'applied': True,
                                                         'createdAt': created + timedelta(hours=2)})
            earned += 50
        if source == 'DOD2026' or i % 4 == 1:
            b.set(txs.document('claim_DOD2026_' + uid), {'uid': uid, 'amount': 100, 'type': 'campaign', 'code': 'DOD2026',
                                                         'reason': 'День открытых дверей', 'applied': True,
                                                         'createdAt': created + timedelta(hours=3)})
            b.set(core.db.collection('coin_claims').document('DOD2026_' + uid), {'uid': uid, 'code': 'DOD2026',
                                                                              'createdAt': created + timedelta(hours=3)})
            earned += 100
        applicants.append({'uid': uid, 'earned': earned, 'spent': spent, 'created': created, 'filled': filled})
    # рефералы: ap_001 пригласил троих, двое заполнили профиль
    for friend in ('ap_012', 'ap_018'):
        b.set(txs.document('referral_' + friend), {'uid': 'ap_001', 'friendUid': friend, 'amount': 30, 'type': 'referral',
                                                    'reason': 'Друг заполнил профиль', 'applied': True, 'createdAt': _ago(5)})
    applicants[0]['earned'] += 60
    # ручное начисление и списание
    b.set(txs.document('manual_demo_1'), {'uid': 'ap_003', 'amount': 200, 'type': 'manual', 'reason': 'Победа в олимпиаде',
                                          'createdBy': ADMIN['uid'], 'applied': True, 'createdAt': _ago(4)})
    b.set(txs.document('manual_demo_2'), {'uid': 'ap_006', 'amount': -20, 'type': 'manual', 'reason': 'Корректировка',
                                          'createdBy': ADMIN['uid'], 'applied': True, 'createdAt': _ago(2)})
    for a in applicants:
        if a['uid'] == 'ap_003':
            a['earned'] += 200
        if a['uid'] == 'ap_006':
            a['spent'] += 20
    for code, uid in ref_codes.items():
        b.set(core.db.collection('referral_codes').document(code), {'uid': uid, 'createdAt': _ago(10)})
    for code, src in SOURCES.items():
        regs = sum(1 for i in range(42) if source_pool[i % len(source_pool)] == code)
        b.set(core.db.collection('applicant_sources').document(code), {
            **src, 'registrations': regs, 'createdAt': _ago(80), 'createdBy': ADMIN['email'], 'updatedAt': _ago(3)})
    return applicants


def seed_coins(b, applicants):
    camps = core.db.collection('coin_campaigns')
    b.set(camps.document('DOD2026'), {'title': 'День открытых дверей', 'amount': 100, 'startsAt': _ago(30),
                                      'endsAt': _now() + timedelta(days=30), 'active': True, 'maxClaims': 500,
                                      'claimsCount': 14, 'place': 'Актовый зал', 'createdAt': _ago(31), 'createdBy': ADMIN['uid']})
    b.set(camps.document('QUIZ2026'), {'title': 'Квиз по IT', 'amount': 50, 'startsAt': _ago(5),
                                       'endsAt': _now() + timedelta(days=10), 'active': False, 'maxClaims': 100,
                                       'claimsCount': 0, 'place': 'Аудитория 204', 'createdAt': _ago(6), 'createdBy': ADMIN['uid']})
    b.set(camps.document('SPRING25'), {'title': 'Весенняя ярмарка', 'amount': 30, 'startsAt': _ago(200),
                                       'endsAt': _ago(170), 'active': True, 'maxClaims': 300, 'claimsCount': 211,
                                       'place': 'ТРЦ', 'createdAt': _ago(201), 'createdBy': ADMIN['uid']})

    products = core.db.collection('coin_products')
    catalog = [
        ('prod_hoodie', 'Худи Caspian College', 'Caspian College худиі', 'Тёплое худи с вышитым логотипом.', 800, 12,
         ['S', 'M', 'L', 'XL'], 1, True, [(255, 98, 0), (26, 28, 30)]),
        ('prod_tshirt', 'Футболка «Я — студент Caspian»', None, 'Хлопок 100%, оранжевая.', 400, 30, ['S', 'M', 'L'], 2, True,
         [(255, 138, 61)]),
        ('prod_mug', 'Термокружка', 'Термокружка', 'Держит тепло 6 часов.', 300, 5, [], 1, True, [(21, 101, 192)]),
        ('prod_stickers', 'Стикерпак', None, '10 стикеров с маскотом колледжа.', 50, None, [], 0, True, [(46, 125, 50)]),
        ('prod_notebook', 'Блокнот A5', None, 'Твёрдая обложка, 96 листов.', 150, 0, [], 1, True, [(109, 74, 255)]),
        ('prod_shopper', 'Шоппер', None, 'Сумка из плотной ткани.', 250, 20, [], 1, False, [(0, 137, 123)]),
    ]
    for sort, (pid, title, title_kk, desc, price, stock, variants, limit, active, colors) in enumerate(catalog):
        images = upload_demo_images('coin_products/%s' % pid, colors, (600, 600)) if sort < 4 else []
        doc = {'title': title, 'description': desc, 'price': price, 'stock': stock, 'variants': variants,
               'limitPerUser': limit, 'active': active, 'sort': sort, 'createdAt': _ago(40 - sort), 'updatedAt': _ago(3)}
        doc.update(core.image_fields(images))
        if title_kk:
            doc['titleKk'] = title_kk
        b.set(products.document(pid), doc)

    orders = core.db.collection('coin_orders')
    txs = core.db.collection('coin_transactions')
    plan = [
        ('ord_001', 'ap_003', 'prod_hoodie', 'M', 'reserved', 800, 'Худи Caspian College', 2),
        ('ord_002', 'ap_006', 'prod_mug', None, 'reserved', 300, 'Термокружка', 1),
        ('ord_003', 'ap_010', 'prod_stickers', None, 'reserved', 50, 'Стикерпак', 0),
        ('ord_004', 'ap_002', 'prod_tshirt', 'L', 'issued', 400, 'Футболка «Я — студент Caspian»', 6),
        ('ord_005', 'ap_005', 'prod_stickers', None, 'issued', 50, 'Стикерпак', 9),
        ('ord_006', 'ap_014', 'prod_mug', None, 'cancelled', 300, 'Термокружка', 4),
        ('ord_007', 'ap_020', 'prod_hoodie', 'XL', 'rejected', 800, 'Худи Caspian College', 3),
    ]
    spent = {}
    for oid, uid, pid, variant, status, price, title, days in plan:
        created = _ago(days)
        doc = {'uid': uid, 'productId': pid, 'createdAt': created, 'status': status, 'price': price, 'title': title,
               'updatedAt': created + timedelta(hours=1)}
        if variant:
            doc['variant'] = variant
        if status == 'rejected':
            doc['reason'] = 'balance'
            doc['processedAt'] = created
        else:
            doc.update({'reservedAt': created, 'processedAt': created})
            b.set(txs.document('order_' + oid), {'uid': uid, 'amount': -price, 'type': 'purchase', 'orderId': oid,
                                                 'reason': title, 'applied': True, 'createdAt': created})
            spent[uid] = spent.get(uid, 0) + price
        if status == 'issued':
            doc.update({'issuedAt': created + timedelta(days=1), 'issuedBy': ADMIN['uid']})
        if status == 'cancelled':
            doc.update({'cancelledAt': created + timedelta(hours=5), 'cancelledBy': uid})
            b.set(txs.document('refund_' + oid), {'uid': uid, 'amount': price, 'type': 'refund', 'orderId': oid,
                                                  'reason': 'Отмена заказа', 'applied': True,
                                                  'createdAt': created + timedelta(hours=5)})
            spent[uid] -= price
        b.set(orders.document(oid), doc)

    wallets = core.db.collection('coin_wallets')
    for a in applicants:
        s = a['spent'] + spent.get(a['uid'], 0)
        earned = max(a['earned'], s)  # в тестовых данных баланс не уходит в минус
        b.set(wallets.document(a['uid']), {'balance': earned - s, 'earnedTotal': earned, 'spentTotal': s,
                                           'updatedAt': _ago(1)})


def seed_admissions(b):
    b.set(core.db.collection('app_config').document('admissions'), {
        'phone': '+7 7292 30-30-30', 'whatsapp': '+77015550199', 'email': 'priem@caspian.test',
        'address': 'г. Актау, 32 мкр., здание 1', 'privacyUrl': 'https://caspian.test/privacy',
        'about': 'Колледж Caspian University готовит IT-специалистов, экономистов и менеджеров с 1999 года.',
        'aboutKk': 'Caspian University колледжі 1999 жылдан бері IT-мамандарды, экономистер мен менеджерлерді даярлайды.',
        'specialties': [s['title'] for s in SPECIALTIES], 'catalog': SPECIALTIES,
        'deadline': _now() + timedelta(days=45), 'deadlineTitle': 'Приём документов до', 'deadlineTitleKk': 'Құжат қабылдау мерзімі',
        'documents': [{'title': 'Удостоверение личности (копия)', 'titleKk': 'Жеке куәлік (көшірме)'},
                      {'title': 'Аттестат или свидетельство', 'note': 'Оригинал'},
                      {'title': 'Фото 3×4', 'note': '6 штук'},
                      {'title': 'Медицинская справка 075-У'}],
        'faq': [{'q': 'Есть ли общежитие?', 'a': 'Да, для иногородних студентов.', 'qKk': 'Жатақхана бар ма?', 'aKk': 'Иә.'},
                {'q': 'Можно ли поступить после 9 класса?', 'a': 'Да, на специальности с базой «9».'}],
        'registerUrl': 'http://127.0.0.1/join', 'appStoreUrl': 'https://apps.apple.com/app/id000000',
        'playStoreUrl': 'https://play.google.com/store/apps/details?id=kz.caspian.college',
        'quests': {'profile': 50, 'referral': 30, 'referralCap': 10},
        'updatedAt': _ago(2)})


# ==================== КОНТЕНТ ====================

def seed_content(b, students):
    news = core.db.collection('news')
    items = [
        ('news_open_day', 'День открытых дверей 25 октября', 'Приглашаем абитуриентов и родителей! Экскурсия по лабораториям, мастер-классы и розыгрыш мерча.',
         'events', 'all', [(255, 98, 0), (217, 74, 0), (255, 138, 61)], 1),
        ('news_hackathon', 'Студенты ПО 24-1 — призёры хакатона', 'Команда колледжа заняла второе место на региональном хакатоне Mangystau IT.',
         'achievements', 'students', [(21, 101, 192), (46, 125, 50)], 3),
        ('news_schedule', 'Изменения в расписании на следующей неделе', 'Пары физкультуры переносятся в новый спортзал.',
         'academic', 'all', [], 6),
        ('news_teachers', 'Методический совет', 'Совещание преподавателей в пятницу в 15:00, аудитория 204.', 'academic', 'teachers',
         [(109, 74, 255)], 10),
    ]
    for nid, title, content, cat, aud, colors, days in items:
        images = upload_demo_images('news', colors) if colors else []
        doc = {'title': title, 'content': content, 'description': content[:90], 'category': cat, 'audience': aud,
               'createdAt': _ago(days), 'updatedAt': _ago(days), 'createdBy': ADMIN['uid']}
        doc.update(core.image_fields(images))
        b.set(news.document(nid), doc)
    b.set(news.document('news_legacy'), {'title': 'Старая новость (только imageUrl)', 'content': 'Опубликовано со старой версии веба.',
                                         'category': 'general', 'audience': 'all', 'createdAt': _ago(40),
                                         'imageUrl': 'https://storage.googleapis.com/%s/news/legacy.jpg' % core.STORAGE_BUCKET})
    an = core.db.collection('applicant_news')
    for k, (title, days) in enumerate([('Старт приёма документов 2026', 1), ('Как получить коины за профиль', 4),
                                       ('Специальность «Программное обеспечение»: что изучают', 9)]):
        images = upload_demo_images('applicant_news', [(255, 98, 0)]) if k == 0 else []
        doc = {'title': title, 'content': 'Подробности — во вкладке «Абитуриент» и в приёмной колледжа.', 'createdAt': _ago(days),
               'createdBy': ADMIN['uid']}
        doc.update(core.image_fields(images))
        b.set(an.document('an_%d' % k), doc)

    polls = core.db.collection('polls')
    b.set(polls.document('poll_canteen'), {'title': 'Оцените работу столовой', 'description': 'Анонимный опрос', 'audience': 'students',
                                           'isAnonymous': True, 'multipleChoice': False, 'isActive': True,
                                           'endTime': _now() + timedelta(days=7), 'createdAt': _ago(2), 'createdBy': ADMIN['uid']})
    for k, (text, votes) in enumerate([('Отлично', 14), ('Нормально', 21), ('Плохо', 6)]):
        b.set(polls.document('poll_canteen').collection('answers').document('a%d' % k), {'text': text, 'votes': votes, 'order': k})
    b.set(polls.document('poll_club'), {'title': 'Какой клуб открыть?', 'audience': 'all', 'isAnonymous': False,
                                        'multipleChoice': True, 'isActive': False, 'endTime': _ago(3), 'createdAt': _ago(20)})
    for k, text in enumerate(['Киберспорт', 'Дебаты', 'Фотография']):
        b.set(polls.document('poll_club').collection('answers').document('a%d' % k), {'text': text, 'votes': 3 + k, 'order': k})
    b.set(core.db.collection('poll_responses').document('poll_club_' + students[0]),
          {'pollId': 'poll_club', 'userId': students[0], 'answers': ['a0', 'a2'], 'votedAt': _ago(19)})

    events = core.db.collection('events')
    b.set(events.document('ev_openday'), {'title': 'День открытых дверей', 'description': 'Экскурсии и мастер-классы',
                                          'date': (_now() + timedelta(days=6)).strftime('%Y-%m-%d'), 'time': '11:00',
                                          'location': 'Актовый зал', 'organizer': 'Приёмная комиссия',
                                          'registrationsCount': 2, 'createdAt': _ago(10)})
    for uid in students[:2]:
        b.set(events.document('ev_openday').collection('registrations').document(uid), {'userId': uid, 'registeredAt': _ago(2)})
    b.set(events.document('ev_sport'), {'title': 'Спартакиада колледжа', 'description': 'Командные соревнования',
                                        'date': _ago(20).strftime('%Y-%m-%d'), 'time': '10:00', 'location': 'Спортзал',
                                        'registrationsCount': 0, 'createdAt': _ago(40)})
    clubs = core.db.collection('clubs')
    b.set(clubs.document('club_robotics'), {'name': 'Робототехника', 'description': 'Arduino и соревнования роботов',
                                            'category': 'tech', 'registrationsCount': 3, 'createdAt': _ago(100)})
    for uid in students[2:5]:
        b.set(clubs.document('club_robotics').collection('registrations').document(uid), {'userId': uid, 'registeredAt': _ago(5)})
    b.set(clubs.document('club_debate'), {'name': 'Дебатный клуб', 'description': 'Публичные выступления', 'category': 'community',
                                          'registrationsCount': 0, 'createdAt': _ago(60)})
    prods = core.db.collection('products')
    b.set(prods.document('p_book'), {'name': 'Учебник «Основы Python»', 'price': 4500.0, 'variants': [], 'url': 'https://example.com/book',
                                     'createdAt': _ago(30)})
    b.set(prods.document('p_badge'), {'name': 'Значок колледжа', 'price': 900.0,
                                      'variants': [{'filterName': 'Цвет', 'options': [{'value': 'Оранжевый', 'price': 900}]}],
                                      'createdAt': _ago(15)})
    fb = core.db.collection('feedback')
    for k, (topic, msg, status, days) in enumerate([
            ('Учёба', 'Можно ли перенести пересдачу по математике?', 'new', 0),
            ('Столовая', 'В столовой мало вегетарианских блюд.', 'new', 1),
            ('Линия доверия', 'Хочу сообщить о буллинге в группе.', 'new', 2),
            ('Расписание', 'В приложении не видно пары в субботу.', 'answered', 5),
            ('Общежитие', 'Не работает Wi-Fi на 3 этаже.', 'answered', 9)]):
        doc = {'topic': topic, 'subject': topic, 'message': msg, 'status': status, 'anonymousVisual': k == 2,
               'userId': students[k], 'createdAt': _ago(days, hours=k)}
        if status == 'answered':
            doc.update({'reply': 'Спасибо, передали ответственным.', 'repliedAt': _ago(days - 1), 'repliedBy': ADMIN['uid']})
        b.set(fb.document('fb_%d' % k), doc)
    mg = core.db.collection('management')
    for k, (name, pos) in enumerate([('Есенова Айжан Муратовна', 'Директор колледжа'),
                                     ('Тажибаев Ерлан Саматович', 'Заместитель директора по учебной работе'),
                                     ('Кулиева Наргиз Рашидовна', 'Заместитель директора по воспитательной работе'),
                                     ('Абенов Даурен Кайратович', 'Руководитель приёмной комиссии')]):
        b.set(mg.document('mg_%d' % k), {'name': name, 'position': pos, 'priority': k, 'image': '', 'createdAt': _ago(300)})
    notes = core.db.collection('notifications')
    b.set(notes.document('n_1'), {'userId': students[3], 'type': 'answer', 'title': 'Ответ на ваше обращение',
                                  'message': 'Спасибо, передали ответственным.', 'read': False, 'createdAt': _ago(4)})
    audit = core.db.collection('cms_audit')
    for k, (action, entity, eid, minutes) in enumerate([('login', 'session', ADMIN['uid'], 5), ('update', 'news', 'news_open_day', 30),
                                                         ('reset_password', 'user', 'ap_005', 90), ('create', 'coin_campaigns', 'DOD2026', 600)]):
        b.set(audit.document('audit_%d' % k), {'at': _ago(minutes=minutes), 'by': ADMIN['email'], 'byUid': ADMIN['uid'],
                                               'action': action, 'entity': entity, 'entityId': eid,
                                               'details': {'note': 'тестовая запись'}})


# ==================== ВСЁ ВМЕСТЕ ====================

def seed_all(today=None):
    """Засеять эмулятор. → dict с ключевыми id (см. SEED_IDS)."""
    core._init_firebase()
    b = _Batch()
    students = seed_people(b)
    lessons = seed_schedule(b, today)
    applicants = seed_applicants(b)
    seed_coins(b, applicants)
    seed_admissions(b)
    seed_content(b, students)
    b.commit()
    core.cache.invalidate()
    return {
        'admin': ADMIN, 'teacher_account': TEACHER_ACCOUNT, 'applicant_account': APPLICANT_ACCOUNT,
        'groups': list(GROUPS), 'teachers': list(TEACHERS), 'namesake': NAMESAKE[0], 'legacy_teacher': LEGACY_TEACHER[0],
        'students': students, 'applicants': [a['uid'] for a in applicants], 'lessons_count': lessons,
        'conflict_cases': dict(CONFLICT_CASES), 'sources': list(SOURCES),
        'campaigns': ['DOD2026', 'QUIZ2026', 'SPRING25'],
        'products': ['prod_hoodie', 'prod_tshirt', 'prod_mug', 'prod_stickers', 'prod_notebook', 'prod_shopper'],
        'orders': ['ord_001', 'ord_002', 'ord_003', 'ord_004', 'ord_005', 'ord_006', 'ord_007'],
        'news': ['news_open_day', 'news_hackathon', 'news_schedule', 'news_teachers', 'news_legacy'],
        'applicant_news': ['an_0', 'an_1', 'an_2'], 'polls': ['poll_canteen', 'poll_club'],
        'events': ['ev_openday', 'ev_sport'], 'clubs': ['club_robotics', 'club_debate'], 'old_products': ['p_book', 'p_badge'],
        'feedback': ['fb_%d' % k for k in range(5)], 'management': ['mg_%d' % k for k in range(4)],
        'lesson_sample': 'base_00_1_0',
    }


def reset_and_seed(today=None):
    reset_emulators()
    return seed_all(today)


if __name__ == '__main__':
    import json
    import time as _t
    t0 = _t.time()
    data = reset_and_seed()
    print(json.dumps({'lessons': data['lessons_count'], 'applicants': len(data['applicants']),
                      'seconds': round(_t.time() - t0, 2)}, ensure_ascii=False))
