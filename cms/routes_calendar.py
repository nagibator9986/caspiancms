# cms/routes_calendar.py — «Академический календарь»: PDF-календарь учебного года на каждую группу
# (владелец — PEOPLE). Студент видит его в профиле мобильного приложения (карточка «Академический календарь»).
#
# Контракт данных (общий с приложением, groups_list/{группа} — id документа = название группы как в users.group):
#   academicCalendar: {url, path, fileName, title, size, version, uploadedAt, uploadedBy}
#     url        download-URL Firebase Storage с токеном (core.download_url) — открывается без авторизации;
#     path       academic_calendars/<slug>/<unixMillis>.pdf — один объект на группу (путь не зависит от названия
#                группы, поэтому переименование группы календарь не ломает);
#     fileName   исходное имя без пути, ≤ 120 символов; title — подпись для студентов, ≤ 80, '' если нет;
#     version    1, 2, 3… (+1 при замене — приложение сбрасывает по нему кэш).
#   Календаря нет — поля нет (DELETE_FIELD). Правила Firestore не меняются: groups_list читают члены колледжа.
#
# Загрузка: один PDF → одной или нескольким группам (до 40). В Storage файл уходит ОДИН раз, на остальные группы
# копируется на стороне Storage (bucket.copy_blob, новый токен на каждую копию): 20 МБ не гоняем через
# единственный воркер N раз. Всё или ничего: объекты создаются до записи в Firestore, при сбое посередине уже
# созданные удаляются, документы не трогаются. Замена: новый объект → запись (version + 1) → только потом
# удаление старого (сбой удаления — в лог). Пуш («Уведомить студентов») — после записи и ничего не ломает.
#
# Файл не доверяем: размер читаем с лимитом (max + 1, а не целиком), сигнатура %PDF- в первых 1024 байтах, имя
# файла санитизируется (путь, управляющие и невидимые символы), путь объекта строится только из ASCII-slug
# названия группы и числа — ни юникода, ни «..» в нём не бывает.

import re
import time
import unicodedata
import uuid
from urllib.parse import quote

from firebase_admin import firestore
from flask import flash, render_template, request
from google.cloud.storage.retry import DEFAULT_RETRY

from .core import (DELETE_FIELD, SERVER_TIMESTAMP, Routes, UploadError, admin_required, audit, bucket, current_admin,
                   db, delete_storage_paths, download_url, flash_error, fs_opts, log, plural, safe_url,
                   sanitize_topic, send_push, send_push_many, to_datetime, topic_variants)
from .routes_people import (CALENDAR_FOLDER, _back, _calendar_paths, _clean, _group_name_problem, _group_rows,
                            _groups_directory, _matches)
from .schedule_model import invalidate_directory_caches, norm_text

routes = Routes()

MAX_PDF_BYTES = 20 * 1024 * 1024          # лимит CMS; в storage.rules потолок выше (25 МБ) — запас на будущее
MAX_GROUPS = 40                           # групп за одну загрузку: на каждую — объект Storage и документ
PDF_SIGNATURE_WINDOW = 1024               # %PDF- допустим со сдвигом (мусор перед заголовком бывает у сканеров)
TITLE_MAX = 80
FILE_NAME_MAX = 120
DEFAULT_FILE_NAME = 'academic-calendar.pdf'
PDF_CACHE_CONTROL = 'public, max-age=3600'    # умеренный: замена файла = новый путь, но старый URL живёт в кэшах
UPLOAD_TIMEOUT = 120                          # с на загрузку/копирование одного объекта (по умолчанию 60 — мало для 20 МБ)
# Повтор на временных сбоях (5xx, 429, обрыв связи): путь объекта уникален, загрузка/копия/patch с теми же данными
# идемпотентны — без повтора один сбой из 40 копий откатывал бы всю загрузку и заставлял гнать 20 МБ заново.
TOKEN_KEY = 'firebaseStorageDownloadTokens'   # как в core.upload_image (getDownloadURL в приложении)
PUSH_TITLE = 'Академический календарь'
PUSH_TYPE = 'academic_calendar'
STATES = (('has', 'Загружен'), ('none', 'Не загружен'))


# ==================== ПОМОЩНИКИ: ИМЕНА, РАЗМЕРЫ, ПУТИ ====================

def _plain(value, limit):
    """Строка из формы: управляющие и невидимые символы (Cc/Cf: \\x00, RLO-«разворот» имени файла, нулевая
    ширина) → пробел, затем как routes_people._clean (схлопнутые пробелы, обрезка по длине)."""
    text = ''.join(' ' if unicodedata.category(ch)[0] == 'C' else ch for ch in str(value or ''))
    return _clean(unicodedata.normalize('NFC', text), limit)


def _slug(value):
    """ASCII-slug для пути в Storage: транслит как у топиков (sanitize_topic), только [a-z0-9_-], непустой."""
    raw = str(value or '').strip()
    s = sanitize_topic(raw).lower()
    if s == 'default' and raw.lower() != 'default':   # sanitize_topic отдаёт 'default' для пустого/нетранслитерируемого
        s = ''
    s = re.sub(r'_+', '_', re.sub(r'[^a-z0-9_-]', '_', s)).strip('_-')[:60].rstrip('_-')
    return s or 'group'


def _safe_file_name(raw):
    """Имя файла для показа и Content-Disposition: без пути клиента (/ и \\), без управляющих символов,
    ≤ 120 символов, всегда с расширением .pdf; пустое/«.pdf» → academic-calendar.pdf."""
    name = _plain(str(raw or '').replace('\\', '/').split('/')[-1], 400)
    if name.lower().endswith('.pdf'):
        name = name[:-4]
    name = name.strip(' .')[:FILE_NAME_MAX - 4].strip(' .')
    return (name + '.pdf') if name else DEFAULT_FILE_NAME


def _content_disposition(file_name):
    """inline; filename="<ascii>"; filename*=UTF-8''<urlenc> — PDF открывается в браузере, а при сохранении
    получает исходное имя. В заголовок попадает только ASCII-slug и percent-encoding (инъекции нет)."""
    stem = file_name[:-4] if file_name.lower().endswith('.pdf') else file_name
    return 'inline; filename="%s.pdf"; filename*=UTF-8\'\'%s' % (_slug(stem), quote(file_name, safe=''))


def _fmt_size(size):
    """Размер файла для людей: 2516582 → '2,4 МБ', 840000 → '820 КБ' (1024-based, как в приложении)."""
    try:
        n = int(size)
    except (TypeError, ValueError):
        return ''
    if n <= 0:
        return ''
    if n >= 1024 * 1024:
        return ('%.1f МБ' % (n / 1048576.0)).replace('.', ',')
    return '%d КБ' % max(1, round(n / 1024.0))


def _int(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _version_of(data):
    """Текущая version календаря в документе группы (0 — календаря нет или поле битое)."""
    cal = (data or {}).get('academicCalendar')
    return max(0, _int(cal.get('version'))) if isinstance(cal, dict) else 0


def _object_path(group, stamp, taken):
    """academic_calendars/<slug>/<stamp>.pdf; занятый путь (та же группа/slug в ту же миллисекунду) сдвигает
    метку: два разных названия с одним slug или замена в ту же миллисекунду не должны затереть чужой объект."""
    slug = _slug(group)
    while True:
        path = '%s/%s/%d.pdf' % (CALENDAR_FOLDER, slug, stamp)
        if path not in taken:
            taken.add(path)
            return path
        stamp += 1


# ==================== ДАННЫЕ ДЛЯ СТРАНИЦЫ ====================

def _calendar_card(data):
    """Карточка календаря из документа группы для списка: dict или None (поля нет или нет рабочей ссылки).
    Допускает «грязные» типы (документ могли править руками в консоли)."""
    cal = (data or {}).get('academicCalendar')
    if not isinstance(cal, dict):
        return None
    url = safe_url(cal.get('url'), '')
    if not url:
        return None
    file_name = str(cal.get('fileName') or '').strip() or DEFAULT_FILE_NAME
    size = _int(cal.get('size'))
    uploaded = to_datetime(cal.get('uploadedAt'))
    return {
        'url': url, 'file_name': file_name, 'title': str(cal.get('title') or '').strip(),
        'size': size, 'size_label': _fmt_size(size), 'version': max(1, _int(cal.get('version'), 1)),
        'uploaded_at': uploaded, 'uploaded_by': str(cal.get('uploadedBy') or '').strip(),
    }


def _calendar_rows():
    """Все группы (справочник + студенты + расписание) с карточкой календаря: общие строки раздела «Группы»
    (_group_rows: курс, специальность, студенты) + cal. Данные кэшируются снапшотами people/справочника."""
    directory = _groups_directory()
    rows = []
    for r in _group_rows():
        row = dict(r)
        row['cal'] = _calendar_card(directory.get(row['name']))
        if row['cal']:
            row['hay'] = '%s %s' % (row['hay'], norm_text('%s %s' % (row['cal']['file_name'], row['cal']['title'])))
        rows.append(row)
    return rows


def _render_page(values=None, errors=None):
    """Страница раздела. values/errors — форма загрузки после ошибки (модалка открывается сама, значения на месте;
    файл браузер повторно не подставит — его выбирают заново)."""
    try:
        rows = _calendar_rows()
    except Exception as e:
        flash_error('Не удалось загрузить группы', e)
        rows = []
    q = (request.args.get('q') or '').strip()[:60]
    major = request.args.get('major') or ''
    course = request.args.get('course') or ''
    state = request.args.get('state') or ''
    items = [r for r in rows if _matches(q, r['hay'])
             and (not major or r['major'] == major)
             and (not course or str(r['course'] or '') == course)
             and (not state or (state == 'has' and r['cal']) or (state == 'none' and not r['cal']))]
    with_cal = sum(1 for r in rows if r['cal'])
    stats = {'groups': len(rows), 'with': with_cal, 'without': len(rows) - with_cal}
    course_numbers = sorted({r['course'] for r in rows if r['course']})
    values = values or {'title': '', 'groups': [], 'notify': False}
    return render_template(
        'academic_calendar.html', rows=items, all_rows=rows, stats=stats, q=q, major=major, course=course,
        state=state, states=STATES, majors=sorted({r['major'] for r in rows if r['major']}),
        courses=[(str(c), '%d курс' % c) for c in course_numbers], course_numbers=course_numbers,
        values=values, errors=errors or {}, open_modal=bool(errors), max_groups=MAX_GROUPS,
        max_mb=MAX_PDF_BYTES // (1024 * 1024), title_max=TITLE_MAX)


@routes.route('/academic-calendar', methods=['GET', 'POST'])
@admin_required
def academic_calendar():
    """Академический календарь: сводка, фильтры (поиск, специальность, курс, «есть / нет календаря»), таблица
    групп с действиями (открыть, заменить, удалить) и модалка загрузки. POST — форма загрузки: при ошибке страница
    рендерится по адресу списка (ссылки фильтров и сортировки остаются рабочими), как у /teachers."""
    if request.method == 'POST':
        return academic_calendar_upload()
    return _render_page()


# ==================== ЗАГРУЗКА ====================

def _read_pdf(file_storage):
    """→ (байты, имя, ошибка). Размер читаем с лимитом MAX+1 — в память не уходит ничего сверх лимита."""
    if file_storage is None or not getattr(file_storage, 'filename', ''):
        return None, '', 'Выберите PDF-файл'
    name = _safe_file_name(file_storage.filename)
    declared_pdf = (file_storage.filename.strip().lower().endswith('.pdf')
                    or (getattr(file_storage, 'mimetype', '') or '').lower() == 'application/pdf')
    if not declared_pdf:
        return None, name, 'Файл «%s» — не PDF (нужен файл с расширением .pdf)' % name
    data = file_storage.read(MAX_PDF_BYTES + 1)
    if not data:
        return None, name, 'Файл «%s» пустой' % name
    if len(data) > MAX_PDF_BYTES:
        return None, name, 'Файл «%s» больше %d МБ — сожмите PDF и загрузите снова' % (name, MAX_PDF_BYTES // 1048576)
    if b'%PDF-' not in data[:PDF_SIGNATURE_WINDOW]:
        return None, name, 'Файл «%s» — не PDF (нужен документ в формате PDF)' % name
    return data, name, None


def _group_errors(names, known):
    """Проверка выбранных групп → текст ошибки или None. known: {название: строка группы}."""
    if not names:
        return 'Выберите хотя бы одну группу'
    if len(names) > MAX_GROUPS:
        return 'За один раз — не больше %d групп (выбрано %d). Загрузите календарь в несколько заходов' % (
            MAX_GROUPS, len(names))
    unknown = [n for n in names if n not in known]
    if unknown:
        shown = ', '.join('«%s»' % n[:40] for n in unknown[:3])
        return 'Такой группы нет: %s%s — обновите страницу или создайте группу в разделе «Группы»' % (
            shown, ' и ещё %d' % (len(unknown) - 3) if len(unknown) > 3 else '')
    for n in names:   # группа есть в расписании/у студентов, но не в справочнике: id документа должен быть допустим
        if not known[n]['in_directory'] and _group_name_problem(n):
            return 'Для группы «%s» нельзя создать запись справочника: %s' % (n[:40], _group_name_problem(n))
    return None


def _fresh_groups(names):
    """groups_list/{название} → {название: dict} свежим чтением (не из кэша: нужны version и старый путь).
    Ошибка чтения не глотается — без неё версия и чистка старых файлов были бы неверными."""
    refs = [db.collection('groups_list').document(n) for n in names]
    return {snap.id: (snap.to_dict() or {}) for snap in db.get_all(refs, **fs_opts()) if snap.exists}


def _commit_cards(names, stored, file_name, size, title, by):
    """Записать карточки календаря в groups_list одной транзакцией Firestore → {группа: {'version', 'old', 'had'}}.
    version и «старый» объект берутся из ТОГО ЖЕ чтения, по которому пишем: два админа, меняющие календарь одной
    группы одновременно, не получат одинаковую version (по ней приложение сбрасывает кэш) и не оставят
    осиротевший объект — каждый удаляет именно тот файл, который затёр. Поле заменяется целиком (старые ключи
    карты не остаются), остальные поля группы не трогаем. Группа не из справочника (она есть в расписании или у
    студентов) получает запись groups_list — как кнопка «Добавить в справочник»."""
    refs = {n: db.collection('groups_list').document(n) for n in names}

    @firestore.transactional
    def run(txn):
        snaps = {snap.id: snap for snap in db.get_all(list(refs.values()), transaction=txn, **fs_opts())}
        result = {}
        for n in names:
            snap = snaps.get(n)
            exists = snap is not None and snap.exists
            data = (snap.to_dict() or {}) if exists else {}
            version = _version_of(data) + 1
            card = {'url': stored[n]['url'], 'path': stored[n]['path'], 'fileName': file_name, 'title': title,
                    'size': size, 'version': version, 'uploadedAt': SERVER_TIMESTAMP, 'uploadedBy': by}
            if exists:
                txn.set(refs[n], {'academicCalendar': card, 'updatedAt': SERVER_TIMESTAMP},
                        merge=['academicCalendar', 'updatedAt'])
            else:
                txn.set(refs[n], {'name': n, 'createdAt': SERVER_TIMESTAMP, 'updatedAt': SERVER_TIMESTAMP,
                                  'academicCalendar': card}, merge=True)
            result[n] = {'version': version, 'old': _calendar_paths(data), 'had': 'academicCalendar' in data}
        return result
    return run(db.transaction())


def _copy_object(source_path, dest_path, token, data, disposition):
    """Копия объекта на стороне Storage (bucket.copy_blob) с новым токеном загрузки. Эмулятор Storage не умеет
    copyTo (501) — там те же байты грузятся ещё раз; в проде это не срабатывает."""
    src = bucket.blob(source_path)
    try:
        dst = bucket.copy_blob(src, src.bucket, dest_path, timeout=UPLOAD_TIMEOUT, retry=DEFAULT_RETRY)
    except Exception as e:
        if getattr(e, 'code', None) != 501:
            raise
        dst = bucket.blob(dest_path)
        dst.metadata = {TOKEN_KEY: token}
        dst.cache_control = PDF_CACHE_CONTROL
        dst.content_disposition = disposition
        dst.upload_from_string(data, content_type='application/pdf', timeout=UPLOAD_TIMEOUT, retry=DEFAULT_RETRY)
        return
    dst.metadata = {TOKEN_KEY: token}   # токен источника скопировался вместе с объектом — у каждой копии свой
    dst.patch(timeout=UPLOAD_TIMEOUT, retry=DEFAULT_RETRY)


def _store_pdf(data, file_name, plan):
    """Положить PDF в Storage по плану [(группа, путь)]: первый объект — загрузка, остальные — копии.
    → {группа: {'path', 'url'}}. Всё или ничего: при ошибке удаляются все созданные (и «в полёте») объекты."""
    disposition = _content_disposition(file_name)
    attempted, stored = [], {}
    try:
        for index, (group, path) in enumerate(plan):
            token = str(uuid.uuid4())
            attempted.append(path)
            if index == 0:
                blob = bucket.blob(path)
                blob.metadata = {TOKEN_KEY: token}
                blob.cache_control = PDF_CACHE_CONTROL
                blob.content_disposition = disposition
                blob.upload_from_string(data, content_type='application/pdf', timeout=UPLOAD_TIMEOUT,
                                        retry=DEFAULT_RETRY)
            else:
                _copy_object(plan[0][1], path, token, data, disposition)
            stored[group] = {'path': path, 'url': download_url(path, token)}
    except Exception:
        log.exception('Загрузка календаря в Storage не удалась (%d из %d объектов)', len(stored), len(plan))
        delete_storage_paths(attempted)
        raise UploadError('Не удалось сохранить файл в хранилище. Ничего не изменено — попробуйте ещё раз')
    return stored


def _notify_students(groups):
    """Пуш студентам групп по топикам расписания (как routes_schedule.send_pushes; группе с «ъ/ь» — в оба топика).
    → True, если ушло всё. Ошибки не бросает: пуш не должен ломать уже сохранённую загрузку."""
    messages = [{'title': PUSH_TITLE, 'body': 'Для группы «%s» загружен новый календарь' % name, 'topic': topic,
                 'data': {'type': PUSH_TYPE, 'group': name}}
                for name in groups for topic in topic_variants('schedule_group_%s' % name)]
    try:
        if len(messages) == 1:
            return bool(send_push(**messages[0]))
        return send_push_many(messages) >= len(messages)
    except Exception:
        log.exception('Пуш об академическом календаре не отправлен')
        return False


@routes.route('/academic-calendar/upload', methods=['POST'])
@admin_required
def academic_calendar_upload():
    """Загрузить PDF одной или нескольким группам (поля: file, groups[], title, notify). Новая загрузка и замена —
    одно действие: у группы уже есть календарь → version + 1, старый объект удаляется после записи."""
    names = list(dict.fromkeys(g.strip() for g in request.form.getlist('groups') if g and g.strip()))
    title = _plain(request.form.get('title'), TITLE_MAX + 1)
    values = {'title': title[:TITLE_MAX], 'groups': names, 'notify': request.form.get('notify') == '1'}
    errors = {}
    try:
        known = {r['name']: r for r in _calendar_rows()}
    except Exception as e:
        flash_error('Не удалось загрузить группы', e)
        return _render_page(values, {'_form': 'Календарь не загружен: список групп недоступен'})
    problem = _group_errors(names, known)
    if problem:
        errors['groups'] = problem
    if len(title) > TITLE_MAX:
        errors['title'] = 'Подпись — не длиннее %d символов' % TITLE_MAX
    data, file_name, problem = _read_pdf(request.files.get('file'))
    if problem:
        errors['file'] = problem
    if errors:
        return _render_page(values, errors)

    try:
        existing = _fresh_groups(names)
    except Exception as e:
        flash_error('Не удалось прочитать группы — календарь не загружен', e)
        return _render_page(values, {'_form': 'Календарь не загружен: база недоступна, попробуйте ещё раз'})
    taken = {p for n in names for p in _calendar_paths(existing.get(n))}
    stamp = int(time.time() * 1000)
    plan = [(n, _object_path(n, stamp + i, taken)) for i, n in enumerate(names)]
    try:
        stored = _store_pdf(data, file_name, plan)
    except UploadError as e:
        return _render_page(values, {'_form': str(e)})

    admin = current_admin() or {}
    by = admin.get('email') or admin.get('name') or ''
    try:
        committed = _commit_cards(names, stored, file_name, len(data), title, by)
    except Exception as e:
        delete_storage_paths([p['path'] for p in stored.values()])   # документы не изменились — объекты не нужны
        flash_error('Не удалось сохранить календарь — ничего не изменено', e)
        return _render_page(values, {'_form': 'Календарь не загружен: изменения не сохранены. Попробуйте ещё раз'})
    versions = {n: committed[n]['version'] for n in names}
    replaced = [n for n in names if committed[n]['had']]

    # новый объект записан — старый больше не нужен (сбой удаления — в лог, на данные не влияет)
    delete_storage_paths([p for n in names for p in committed[n]['old'] if p != stored[n]['path']])
    invalidate_directory_caches()
    created = [n for n in names if n not in replaced]
    for action, group_list in (('upload', created), ('replace', replaced)):
        if not group_list:
            continue
        # группы — строкой: список из двух элементов журнал показал бы как «было → стало»
        details = {'file': file_name, 'size': len(data), 'title': title, 'notify': values['notify'],
                   'groups': ', '.join(group_list)}
        if len(group_list) == 1:
            details['version'] = versions[group_list[0]]
        audit(action, 'academic_calendar', group_list[0] if len(group_list) == 1 else None, details)
    notified = _notify_students(names) if values['notify'] else None

    if len(names) == 1:
        msg = ('Календарь группы «%s» заменён (версия %d)' % (names[0], versions[names[0]]) if replaced
               else 'Календарь загружен для группы «%s»' % names[0])
    else:
        msg = 'Календарь загружен для %d %s' % (len(names), plural(len(names), 'группы', 'групп', 'групп'))
        if replaced:
            msg += '; у %d из них заменён прежний' % len(replaced)
    if notified:
        msg += '. Студенты уведомлены'
    flash(msg, 'success')
    if notified is False:
        flash('Календарь сохранён, но уведомление студентам отправить не удалось', 'warning')
    return _back('academic_calendar')


# ==================== УДАЛЕНИЕ ====================

@routes.route('/academic-calendar/delete/<group_name>', methods=['POST'])
@admin_required
def academic_calendar_delete(group_name):
    """Удалить календарь группы: сначала поле в Firestore (приложение перестаёт его показывать), потом объект
    в Storage (нет объекта — не беда). Сама группа остаётся."""
    ref = db.collection('groups_list').document(group_name)

    @firestore.transactional
    def run(txn):
        # чтение и удаление поля — в одной транзакции: объект удаляем тот, который действительно сняли с группы
        # (параллельная замена другим админом не оставит осиротевший файл)
        snap = ref.get(transaction=txn, **fs_opts())
        data = (snap.to_dict() or {}) if snap.exists else {}
        if 'academicCalendar' not in data:
            return None
        txn.update(ref, {'academicCalendar': DELETE_FIELD, 'updatedAt': SERVER_TIMESTAMP})
        return data
    try:
        data = run(db.transaction())
    except Exception as e:
        flash_error('Не удалось удалить календарь', e)
        return _back('academic_calendar')
    if data is None:
        flash('У группы «%s» нет календаря — удалять нечего' % group_name, 'warning')
        return _back('academic_calendar')
    card = _calendar_card(data) or {}
    delete_storage_paths(_calendar_paths(data))
    invalidate_directory_caches()
    audit('delete', 'academic_calendar', group_name, {'file': card.get('file_name'), 'groups': group_name})
    flash('Календарь группы «%s» удалён' % group_name, 'success')
    return _back('academic_calendar')
