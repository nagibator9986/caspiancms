# cms/routes_admissions.py — настройки приёмной комиссии и новости абитуриентов (владелец — COINS).
#
# app_config/admissions (SPEC §2.8, читается приложением без входа) — пишем merge, старые поля сохраняются:
#   phone, whatsapp, email, address, privacyUrl, about, aboutKk,
#   specialties [str]  = названия каталога (для старых версий приложения и фильтров), пишется синхронно с catalog,
#   catalog [{id, title, titleKk?, description?, descriptionKk?, duration?, base?, form?, language?, places?, price?,
#             imageUrl?}], deadline?, deadlineTitle?, deadlineTitleKk?, documents [{title, titleKk?, note?}],
#   faq [{q, qKk?, a, aKk?}], registerUrl (по умолчанию <сайт>/join), appStoreUrl?, playStoreUrl?,
#   quests {profile, referral, referralCap} (по умолчанию 50 / 30 / 10), updatedAt, updatedBy.
#   Фото специальностей — Storage admissions/catalog/<id>/<stamp>_0<ext>.
# applicant_news/{id} — схема как у news в приложении (lib/services/news_service.dart, postNews):
#   title, content, description?, category (academic|sports|cultural|curator), groupId 'all',
#   images [{url, path, w, h}] + imageUrl (обложка), createdAt (обязателен: лента сортируется по нему), updatedAt.
#   Фото — Storage applicant_news/<id>/…; пуш абитуриентам шлёт функция onApplicantNewsCreated (топик news_applicant).

import re
from datetime import timedelta
from urllib.parse import urlsplit

from firebase_admin import firestore
from flask import abort, flash, redirect, render_template, request, url_for

from .core import (DELETE_FIELD, SERVER_TIMESTAMP, Routes, UploadError, admin_required, audit, back_url, cache,
                   current_admin, db, delete_storage_paths, doc_dict, flash_action, flash_error, get_docs,
                   image_fields, images_from_uploader, images_of, local_input_value, log, now_utc, paginate_list,
                   parse_local_datetime, sanitize_topic, storage_path_from_url, to_almaty, to_datetime, upload_image)
from .routes_coins import _back, _field, _int, _norm, _safe_doc_id, _str, _ts, site_root

routes = Routes()

ADMISSIONS_REF = ('app_config', 'admissions')
QUEST_DEFAULTS = {'profile': 50, 'referral': 30, 'referralCap': 10}
BASES = [('', '—'), ('9', 'после 9 класса'), ('11', 'после 11 класса'), ('9,11', 'после 9 и 11 класса')]
MAX_ROWS = 50
TEXT_LIMITS = {'phone': 40, 'whatsapp': 40, 'email': 120, 'address': 200, 'privacyUrl': 500, 'registerUrl': 500,
               'appStoreUrl': 500, 'playStoreUrl': 500, 'about': 4000, 'aboutKk': 4000, 'deadlineTitle': 120,
               'deadlineTitleKk': 120}
URL_FIELDS = ('privacyUrl', 'registerUrl', 'appStoreUrl', 'playStoreUrl')
CATALOG_FIELDS = {  # поле → (подпись, лимит)
    'title': ('Название', 120), 'titleKk': ('Название (KZ)', 120), 'description': ('Описание', 2000),
    'descriptionKk': ('Описание (KZ)', 2000), 'duration': ('Срок обучения', 40), 'base': ('База', 10),
    'form': ('Форма обучения', 40), 'language': ('Язык обучения', 40), 'places': ('Мест', 6),
    'price': ('Стоимость', 60),
}
DOC_FIELDS = {'title': 200, 'titleKk': 200, 'note': 300}
FAQ_FIELDS = {'q': 300, 'qKk': 300, 'a': 3000, 'aKk': 3000}

NEWS_CATEGORIES = [('academic', 'Академические'), ('sports', 'Спортивные'), ('cultural', 'Культурные'),
                   ('curator', 'Кураторские')]   # как в редакторе новостей приложения (category_<код>)
NEWS_LIST_LIMIT = 500
NEWS_TTL = 120
NEWS_PER_PAGE = 20
NEWS_MAX_IMAGES = 10
_EMAIL_RE = re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')


def _invalidate_admissions():
    cache.invalidate('coins:admissions')
    cache.invalidate('applicants:admissions')   # каталог/registerUrl в разделе «Абитуриенты»


def _is_http(url):
    """Абсолютная ссылка http(s) с хостом (без пробелов): её откроет приложение (url_launcher)."""
    url = url or ''
    if not url or any(c.isspace() for c in url) or len(url) > 500:
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = parts.hostname or ''
    return parts.scheme.lower() in ('http', 'https') and bool(host) and ('.' in host or host == 'localhost')


def _norm_url(url):
    """Схема — в нижнем регистре (HTTPS://… → https://…), остальное как есть."""
    m = re.match(r'^(https?)://', url or '', re.I)
    return m.group(1).lower() + url[m.end(1):] if m else url


def default_register_url():
    return site_root() + 'join'


def register_url_of(doc):
    """registerUrl для приложения и ссылок: сохранённый абсолютный http(s), иначе <сайт>/join."""
    url = _str((doc or {}).get('registerUrl'))
    return _norm_url(url) if _is_http(url) else default_register_url()


def quests_of(doc):
    """Награды за квесты как в приложении: {profile, referral, referralCap} — всегда int (строки и мусор
    в старом документе → значение по умолчанию после проверки)."""
    raw = (doc or {}).get('quests')
    raw = raw if isinstance(raw, dict) else {}
    out = {}
    for k, dflt in QUEST_DEFAULTS.items():
        n = _int(raw.get(k))
        out[k] = n if n is not None and 0 <= n <= 100000 else dflt
    return out


def app_data_issues(doc):
    """Что в app_config/admissions не совпадает с контрактом приложения (UX_SPEC_v2 CRM-1) — подсказка
    «Сохраните настройки»: quests не int, registerUrl не http(s), specialties ≠ названиям каталога."""
    if not doc:
        return []
    issues = []
    raw = doc.get('quests')
    if raw is not None and (not isinstance(raw, dict) or any(
            k in raw and (isinstance(raw[k], bool) or not isinstance(raw[k], int)) for k in QUEST_DEFAULTS)):
        issues.append('награды за квесты записаны не числами')
    url = _str(doc.get('registerUrl'))
    if url and not _is_http(url):
        issues.append('ссылка регистрации не начинается с http(s)://')
    catalog = doc.get('catalog') if isinstance(doc.get('catalog'), list) else []
    if catalog:
        titles = [_str(c.get('title')) if isinstance(c, dict) else _str(c) for c in catalog]
        specs = doc.get('specialties') if isinstance(doc.get('specialties'), list) else None
        if specs is None or [_str(x) for x in specs] != titles or any(not isinstance(x, str) for x in specs):
            issues.append('список специальностей расходится с каталогом')
    return issues


# ==================== НАСТРОЙКИ ПРИЁМНОЙ ====================

def _load_admissions_doc():
    snap = db.collection(ADMISSIONS_REF[0]).document(ADMISSIONS_REF[1]).get()
    return (snap.to_dict() or {}) if snap.exists else {}


def _rows_from_doc(items, fields):
    out = []
    for it in items or []:
        if isinstance(it, dict):
            row = {k: _str(it.get(k)) for k in fields}
            row.update({k: v for k, v in it.items() if k in ('id', 'imageUrl')})
            out.append(row)
        elif isinstance(it, str) and _str(it):   # старый формат: specialties — строки
            out.append(dict({k: '' for k in fields}, title=_str(it)))
    return out


def catalog_from_doc(doc):
    """Каталог для формы: catalog[], иначе старый список specialties (строки)."""
    raw = doc.get('catalog') if isinstance(doc.get('catalog'), list) and doc.get('catalog') else doc.get('specialties')
    rows = _rows_from_doc(raw if isinstance(raw, list) else [], CATALOG_FIELDS)
    for i, r in enumerate(rows):
        r['key'] = r.get('id') or 'k%d' % i
        r['imageUrl'] = _str(r.get('imageUrl'))
    return rows


def _settings_values(doc):
    v = {k: _str(doc.get(k)) for k in TEXT_LIMITS}
    v['deadline'] = local_input_value(doc.get('deadline'))
    v['registerUrl'] = v['registerUrl'] or default_register_url()
    for k, n in quests_of(doc).items():
        v['quest_' + k] = str(n)
    v['catalog'] = catalog_from_doc(doc)
    v['documents'] = _rows_from_doc(doc.get('documents') if isinstance(doc.get('documents'), list) else [], DOC_FIELDS)
    v['faq'] = _rows_from_doc(doc.get('faq') if isinstance(doc.get('faq'), list) else [], FAQ_FIELDS)
    return v


def _form_rows(prefix, fields):
    """Повторяемые строки формы: списки <prefix>_<поле>[] одинаковой длины → [{поле: значение}]."""
    lists = {k: request.form.getlist('%s_%s' % (prefix, k)) for k in fields}
    n = max((len(v) for v in lists.values()), default=0)
    return [{k: _str(lists[k][i]) if i < len(lists[k]) else '' for k in fields} for i in range(n)]


def _slug(title, taken):
    base = re.sub(r'[^a-z0-9]+', '_', sanitize_topic(title).lower()).strip('_')[:32] or 'spec'
    slug, i = base, 2
    while slug in taken:
        slug, i = '%s_%d' % (base, i), i + 1
    taken.add(slug)
    return slug


def parse_settings_form(existing_doc):
    """Поля формы → (values для повторного показа, data для записи, errors, plan картинок каталога)."""
    errors = {}
    v = {k: _field(k) if k not in ('about', 'aboutKk') else (request.form.get(k) or '').strip() for k in TEXT_LIMITS}
    for k, limit in TEXT_LIMITS.items():
        if len(v[k]) > limit:
            errors[k] = 'Не длиннее %d символов' % limit
    for k in URL_FIELDS:
        if v[k] and not _is_http(v[k]):
            errors[k] = 'Нужна полная ссылка: https://… (адрес сайта целиком)'
        elif v[k]:
            v[k] = _norm_url(v[k])
    if v['email'] and not _EMAIL_RE.match(v['email']):
        errors['email'] = 'Проверьте email'
    v['registerUrl'] = v['registerUrl'] or default_register_url()
    v['deadline'] = _field('deadline')
    deadline = parse_local_datetime(v['deadline']) if v['deadline'] else None
    if v['deadline'] and deadline is None:
        errors['deadline'] = 'Проверьте дату'
    quests = {}   # приложение читает целые числа: строки/дроби сюда не попадают (старый документ нормализуется)
    for k in QUEST_DEFAULTS:
        raw = _field('quest_' + k)
        v['quest_' + k] = raw
        n = _int(raw) if raw != '' else QUEST_DEFAULTS[k]
        if n is None or not 0 <= n <= 100000:
            errors['quest_' + k] = 'Целое число от 0 до 100000'
        quests[k] = int(n) if n is not None else QUEST_DEFAULTS[k]

    # --- каталог специальностей ---
    keys = request.form.getlist('cat_key')
    rows = _form_rows('cat', ['key', 'id', 'imageUrl'] + list(CATALOG_FIELDS))
    remove_keys = set(request.form.getlist('cat_image_remove'))
    old_catalog = existing_doc.get('catalog') if isinstance(existing_doc.get('catalog'), list) else []
    old_images = {_str(c.get('imageUrl')) for c in old_catalog if isinstance(c, dict) and _str(c.get('imageUrl'))}
    taken_ids = {r['id'] for r in rows if r['id']}
    catalog, cat_errors, plan, titles = [], [], [], set()
    for i, r in enumerate(rows):
        r['key'] = r['key'] or (keys[i] if i < len(keys) else 'k%d' % i)
        has_file = bool(getattr(request.files.get('cat_image__' + r['key']), 'filename', ''))
        if not any(r[k] for k in CATALOG_FIELDS) and not has_file:
            continue   # пустая строка
        title = r['title']
        if not title:
            cat_errors.append('Строка %d: укажите название специальности' % (i + 1))
            continue
        for k, (label, limit) in CATALOG_FIELDS.items():
            if len(r[k]) > limit:
                cat_errors.append('«%s»: поле «%s» — не длиннее %d символов' % (title[:40], label, limit))
        if title.lower() in titles:
            cat_errors.append('Специальность «%s» указана дважды' % title)
        titles.add(title.lower())
        if r['base'] not in dict(BASES):
            r['base'] = ''
        places = None
        if r['places']:
            places = _int(r['places'])
            if places is None or not 0 <= places <= 100000:
                cat_errors.append('«%s»: количество мест — целое число' % title[:40])
        if not r['id'] or not re.fullmatch(r'[A-Za-z0-9_\-]{1,40}', r['id']):
            r['id'] = _slug(title, taken_ids)
        item = {'id': r['id'], 'title': title}
        for k in ('titleKk', 'description', 'descriptionKk', 'duration', 'base', 'form', 'language', 'price'):
            if r[k]:
                item[k] = r[k]
        if places is not None:
            item['places'] = places
        keep = r['imageUrl'] if r['imageUrl'] in old_images and r['key'] not in remove_keys else ''
        if keep:
            item['imageUrl'] = keep
        plan.append((item, r['key'], has_file))
        catalog.append(item)
    if len(catalog) > MAX_ROWS:
        cat_errors.append('Не больше %d специальностей' % MAX_ROWS)
    if cat_errors:
        errors['catalog'] = cat_errors

    # --- документы и FAQ ---
    documents, doc_errors = [], []
    for i, r in enumerate(_form_rows('doc', list(DOC_FIELDS))):
        if not any(r.values()):
            continue
        if not r['title']:
            doc_errors.append('Документ %d: укажите название' % (i + 1))
            continue
        for k, limit in DOC_FIELDS.items():
            if len(r[k]) > limit:
                doc_errors.append('«%s»: не длиннее %d символов' % (r['title'][:40], limit))
        documents.append({k: r[k] for k in DOC_FIELDS if r[k]})
    if len(documents) > MAX_ROWS:
        doc_errors.append('Не больше %d документов' % MAX_ROWS)
    if doc_errors:
        errors['documents'] = doc_errors
    faq, faq_errors = [], []
    for i, r in enumerate(_form_rows('faq', list(FAQ_FIELDS))):
        if not any(r.values()):
            continue
        if not r['q'] or not r['a']:
            faq_errors.append('Вопрос %d: нужны и вопрос, и ответ (на русском)' % (i + 1))
            continue
        for k, limit in FAQ_FIELDS.items():
            if len(r[k]) > limit:
                faq_errors.append('«%s»: не длиннее %d символов' % (r['q'][:40], limit))
        faq.append({k: r[k] for k in FAQ_FIELDS if r[k]})
    if len(faq) > MAX_ROWS:
        faq_errors.append('Не больше %d вопросов' % MAX_ROWS)
    if faq_errors:
        errors['faq'] = faq_errors

    data = {k: v[k] for k in TEXT_LIMITS}
    data.update({'deadline': deadline if deadline else DELETE_FIELD, 'quests': quests, 'catalog': catalog,
                 'specialties': [c['title'] for c in catalog], 'documents': documents, 'faq': faq})
    # значения для повторного показа формы
    v['catalog'] = [dict(r, places=r['places']) for r in rows if any(r[k] for k in CATALOG_FIELDS)]
    v['documents'] = [r for r in _form_rows('doc', list(DOC_FIELDS)) if any(r.values())]
    v['faq'] = [r for r in _form_rows('faq', list(FAQ_FIELDS)) if any(r.values())]
    return v, data, errors, plan


def readiness(doc):
    """Чек-лист «готово ли для абитуриентов»: [(ok, текст, якорь)]."""
    catalog = doc.get('catalog') if isinstance(doc.get('catalog'), list) else []
    deadline = to_datetime(doc.get('deadline'))
    return [
        (bool(_str(doc.get('phone')) or _str(doc.get('whatsapp'))), 'Телефон или WhatsApp приёмной', 'sec-contacts'),
        (bool(_str(doc.get('privacyUrl'))), 'Политика обработки персональных данных', 'sec-contacts'),
        (bool(_str(doc.get('about'))), 'Текст «О колледже»', 'sec-about'),
        (bool(_str(doc.get('appStoreUrl')) and _str(doc.get('playStoreUrl'))), 'Ссылки на App Store и Google Play',
         'sec-links'),
        (bool(deadline and deadline > now_utc()), 'Дедлайн приёма документов (в будущем)', 'sec-deadline'),
        (len(catalog) > 0, 'Каталог специальностей', 'sec-catalog'),
        (bool(doc.get('documents')), 'Список документов', 'sec-documents'),
        (bool(doc.get('faq')), 'Частые вопросы', 'sec-faq'),
    ]


@routes.route('/admissions', methods=['GET', 'POST'])
@admin_required
def admissions_settings():
    try:
        doc = _load_admissions_doc()
    except Exception as e:
        flash_error('Не удалось загрузить настройки приёмной. Обновите страницу', e)
        return render_template('admissions/settings.html', v=_settings_values({}), errors={}, checklist=[],
                               bases=BASES, updated=None, updated_by=None, load_failed=True, issues=[])
    errors = {}
    if request.method == 'POST':
        values, data, errors, plan = parse_settings_form(doc)
        if not errors:
            uploaded, old_paths = [], []
            try:
                for item, key, has_file in plan:
                    if has_file:
                        up = upload_image(request.files['cat_image__' + key], 'admissions/catalog/%s' % item['id'])
                        uploaded.append(up['path'])
                        item['imageUrl'] = up['url']
            except UploadError as e:
                delete_storage_paths(uploaded)
                errors['catalog'] = [str(e)]
            else:
                data.update({'updatedAt': SERVER_TIMESTAMP, 'updatedBy': (current_admin() or {}).get('uid')})
                try:
                    db.collection(ADMISSIONS_REF[0]).document(ADMISSIONS_REF[1]).set(data, merge=True)
                except Exception as e:
                    delete_storage_paths(uploaded)
                    flash_error('Не удалось сохранить настройки. Попробуйте ещё раз', e)
                else:
                    keep = {c.get('imageUrl') for c in data['catalog'] if c.get('imageUrl')}
                    for c in (doc.get('catalog') if isinstance(doc.get('catalog'), list) else []):
                        url = _str(c.get('imageUrl')) if isinstance(c, dict) else ''
                        path = storage_path_from_url(url) if url and url not in keep else None
                        if path and path.startswith('admissions/'):
                            old_paths.append(path)
                    delete_storage_paths(old_paths)
                    _invalidate_admissions()
                    changed = sorted(k for k in list(TEXT_LIMITS) + ['quests', 'catalog', 'documents', 'faq']
                                     if data.get(k) != doc.get(k))
                    if values['deadline'] != local_input_value(doc.get('deadline')):
                        changed.append('deadline')
                    audit('update', 'admissions', 'app_config/admissions',
                          {'changed': changed, 'specialties': len(data['catalog']), 'documents': len(data['documents']),
                           'faq': len(data['faq'])})
                    flash('Настройки приёмной сохранены — приложение покажет их в течение нескольких минут',
                          'success')
                    return redirect(url_for('admissions_settings'))
        if errors:
            flash('Проверьте поля формы', 'error')
        v = values
    else:
        v = _settings_values(doc)
    updated_by = None
    if doc.get('updatedBy'):
        u = get_docs('users', [_str(doc.get('updatedBy'))], ['name', 'email']).get(_str(doc.get('updatedBy'))) or {}
        updated_by = _str(u.get('name')) or _str(u.get('email')) or None
    deadline = to_datetime(doc.get('deadline'))
    days_left = (to_almaty(deadline).date() - to_almaty(now_utc()).date()).days if deadline else None
    return render_template('admissions/settings.html', v=v, errors=errors, checklist=readiness(doc), bases=BASES,
                           updated=to_datetime(doc.get('updatedAt')), updated_by=updated_by, days_left=days_left,
                           default_register=default_register_url(), load_failed=False, issues=app_data_issues(doc))


# ==================== НОВОСТИ АБИТУРИЕНТОВ ====================

def load_news():
    """Последние NEWS_LIST_LIMIT новостей абитуриентов (select, кэш 2 мин)."""
    def load():
        q = db.collection('applicant_news').select(
            ['title', 'description', 'content', 'category', 'createdAt', 'updatedAt', 'imageUrl', 'images'])
        return [dict(s.to_dict() or {}, id=s.id) for s in
                q.order_by('createdAt', direction=firestore.Query.DESCENDING).limit(NEWS_LIST_LIMIT).stream()]
    return cache.get_or_set('coins:anews', load, NEWS_TTL)


def news_row(n):
    imgs = images_of(n)
    return {
        'id': n['id'], 'title': _str(n.get('title')) or 'Без заголовка',
        'summary': _str(n.get('description')) or _str(n.get('content')), 'category': _str(n.get('category')),
        'category_label': dict(NEWS_CATEGORIES).get(_str(n.get('category')), ''),
        'cover': imgs[0]['url'] if imgs else None, 'photos': len(imgs), 'created': to_datetime(n.get('createdAt')),
        'updated': to_datetime(n.get('updatedAt')),
    }


@routes.route('/applicants/news')
@admin_required
def applicant_news():
    try:
        items = load_news()
    except Exception as e:
        flash_error('Не удалось загрузить новости. Обновите страницу', e)
        items = []
    q = _str(request.args.get('q'), 120)
    category = request.args.get('category', '')
    category = category if category in dict(NEWS_CATEGORIES) else ''
    photo = request.args.get('photo', '')
    words = _norm(q).split()
    rows = []
    for n in items:
        if category and _str(n.get('category')) != category:
            continue
        has_photo = bool(images_of(n))
        if (photo == 'with' and not has_photo) or (photo == 'without' and has_photo):
            continue
        if words:
            hay = _norm(' '.join([_str(n.get('title')), _str(n.get('description')), _str(n.get('content'))]))
            if not all(w in hay for w in words):
                continue
        rows.append(n)
    month_ago = now_utc() - timedelta(days=30)
    stats = {'total': len(items), 'month': sum(1 for n in items if _ts(n.get('createdAt')) > month_ago),
             'with_photo': sum(1 for n in items if images_of(n)),
             'last': to_datetime(items[0].get('createdAt')) if items else None}
    page = paginate_list(rows, request.args.get('page'), NEWS_PER_PAGE)
    page.items = [news_row(n) for n in page.items]
    return render_template('applicants/news.html', page=page, stats=stats, filtered=len(rows), q=q,
                           category=category, photo=photo, categories=NEWS_CATEGORIES,
                           truncated=len(items) >= NEWS_LIST_LIMIT)


def _news_values(doc=None):
    if request.method == 'POST':
        return {'title': _field('title'), 'description': _field('description'),
                'content': (request.form.get('content') or '').strip(), 'category': _field('category')}
    d = doc or {}
    cat = _str(d.get('category'))
    return {'title': _str(d.get('title')), 'description': _str(d.get('description')),
            'content': _str(d.get('content')), 'category': cat if cat in dict(NEWS_CATEGORIES) else 'academic'}


def _validate_news(v):
    errors = {}
    if not v['title']:
        errors['title'] = 'Укажите заголовок'
    elif len(v['title']) > 200:
        errors['title'] = 'Заголовок — не длиннее 200 символов'
    if len(v['description']) > 300:
        errors['description'] = 'Краткое описание — не длиннее 300 символов'
    if not v['content'] and not v['description']:
        errors['content'] = 'Напишите текст новости'
    elif len(v['content']) > 20000:
        errors['content'] = 'Текст слишком длинный (больше 20 000 символов)'
    if v['category'] not in dict(NEWS_CATEGORIES):
        errors['category'] = 'Выберите категорию'
    return errors


@routes.route('/applicants/news/new', methods=['GET', 'POST'])
@routes.route('/applicants/news/<news_id>/edit', methods=['GET', 'POST'])
@admin_required
def applicant_news_form(news_id=None):
    doc = None
    if news_id:
        if not _safe_doc_id(news_id):
            abort(404)
        doc = doc_dict(db.collection('applicant_news').document(news_id).get())
        if doc is None:
            flash('Новость не найдена — возможно, её уже удалили', 'warning')
            return redirect(url_for('applicant_news'))
    values = _news_values(doc)
    errors = {}
    if request.method == 'POST':
        errors = _validate_news(values)
        if not errors:
            coll = db.collection('applicant_news')
            ref = coll.document(news_id) if news_id else coll.document()
            try:
                res = images_from_uploader('images', images_of(doc) if doc else [], 'applicant_news/%s' % ref.id,
                                           max_count=NEWS_MAX_IMAGES)
            except UploadError as e:
                errors['images'] = str(e)
            else:
                data = {'title': values['title'], 'content': values['content'],
                        'description': values['description'] or None, 'category': values['category'],
                        **image_fields(res.images), 'updatedAt': SERVER_TIMESTAMP}
                try:
                    if news_id:
                        ref.update(data)
                    else:
                        # документ — ПОСЛЕ загрузки фото: пуш onApplicantNewsCreated берёт imageUrl
                        data.update({'groupId': 'all', 'createdAt': SERVER_TIMESTAMP,
                                     'createdBy': (current_admin() or {}).get('uid')})
                        ref.set(data)
                except Exception as e:
                    delete_storage_paths(res.uploaded_paths)
                    flash_error('Не удалось сохранить новость. Попробуйте ещё раз', e)
                else:
                    delete_storage_paths([p for p in res.removed_paths if p.startswith('applicant_news/')])
                    cache.invalidate('coins:anews')
                    audit('update' if news_id else 'create', 'applicant_news', ref.id,
                          {'title': values['title'], 'photos': len(res.images)})
                    flash_action('Новость обновлена' if news_id else
                                 'Новость опубликована — абитуриенты получат уведомление', 'Открыть',
                                 href=url_for('applicant_news_form', news_id=ref.id))
                    return redirect(back_url('applicant_news'))
        if errors:
            flash('Проверьте поля формы', 'error')
    return render_template('applicants/news_form.html', news=doc, values=values, errors=errors,
                           categories=NEWS_CATEGORIES, max_images=NEWS_MAX_IMAGES)


def _delete_news(news_id):
    """Удалить новость и её фото (как NewsService.deleteNews). → заголовок или None (уже нет)."""
    ref = db.collection('applicant_news').document(news_id)
    snap = ref.get()
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    ref.delete()
    paths = []
    for im in images_of(data):
        path = im.get('path') or storage_path_from_url(im.get('url'))
        if path and path.startswith('applicant_news/'):
            paths.append(path)
    delete_storage_paths(paths)
    return _str(data.get('title')) or news_id


@routes.route('/applicants/news/<news_id>/delete', methods=['POST'])
@admin_required
def applicant_news_delete(news_id):
    if not _safe_doc_id(news_id):
        abort(404)
    try:
        title = _delete_news(news_id)
    except Exception as e:
        flash_error('Не удалось удалить новость', e)
        return _back('applicant_news')
    cache.invalidate('coins:anews')
    if title is None:
        flash('Новость уже удалена', 'info')
    else:
        audit('delete', 'applicant_news', news_id, {'title': title})
        flash('Новость «%s» удалена вместе с фото' % title, 'success')
    return _back('applicant_news')


@routes.route('/applicants/news/bulk', methods=['POST'])
@admin_required
def applicant_news_bulk():
    ids = [i for i in dict.fromkeys(request.form.getlist('ids')) if _safe_doc_id(i)][:100]
    if request.form.get('action') != 'delete' or not ids:
        flash('Отметьте новости для удаления', 'warning')
        return _back('applicant_news')
    deleted = []
    for nid in ids:
        try:
            if _delete_news(nid) is not None:
                deleted.append(nid)
        except Exception:
            log.exception('Новость абитуриентов %s не удалена', nid)
    cache.invalidate('coins:anews')
    if deleted:
        audit('bulk_delete', 'applicant_news', None, {'ids': deleted})
    flash('Удалено новостей: %d' % len(deleted), 'success' if len(deleted) == len(ids) else 'warning')
    return _back('applicant_news')
