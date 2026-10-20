# cms/routes_content.py — раздел «Контент»: новости, опросы, события, клубы, товары (магазин в тенге)
# и обратная связь («Линия доверия»). Владелец — CONTENT.
#
# Схема данных — как в мобильном приложении (lib/services/*), чтобы приложение показывало всё,
# что пишет веб:
#   news/{id}      title, content, description|null, category (academic|sports|cultural|curator),
#                  target_audience (all|students|teachers — по нему фильтруют лента и пуш onNewsCreate),
#                  groupId, images [{url, path, w, h}] (≤ 10) + imageUrl (= обложка, для старых версий),
#                  createdAt, updatedAt, createdBy. Фото: Storage news/<id>/<stamp>_<i><ext> (NewsMedia).
#   polls/{id}     title, description, target_audience, poll_type (single|multiple), is_anonymous, endTime,
#                  imageUrl, createdAt, createdBy. Варианты polls/{id}/answers/{a}: text, votes (int),
#                  createdAt (приложение сортирует по нему), order. Голоса poll_responses/{pollId}_{uid}
#                  {pollId, userId, answers[], votedAt}; старый веб-формат {poll_id, answer_id, user_id}
#                  только читаем и удаляем.
#   events/{id}    title, description, date (Timestamp — полночь по Алматы), time ('HH:MM'), location,
#                  organizer, imageUrl, registrationsCount, createdAt; регистрации events/{id}/registrations/{uid}.
#   clubs/{id}     title, description, category (physical|creative|tech|community), meetingTime, location,
#                  curator, imageUrl, registrationsCount, createdAt; регистрации clubs/{id}/registrations/{uid}.
#   products/{id}  name, price (₸, число), url, imageUrl, variants [{filterName, options: [{name, price}]}].
#   feedback/{id}  topic, subject, message, anonymousVisual, userId, status (new|answered|closed),
#                  reply, repliedAt, repliedBy.
#
# Пуши о новых новостях/опросах шлют Cloud Functions (onNewsCreate/onPollCreate) — веб их не дублирует.
# Списки читаются снапшотом с select() и кэшем (коллекции небольшие), запись сбрасывает кэш «content:*».

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from firebase_admin import firestore
from flask import flash, redirect, render_template, request, url_for

from .core import (ALMATY, SERVER_TIMESTAMP, Routes, UploadError, admin_required, aggregate_sum, audit, back_url, cache,
                   chunked, count, csv_response, current_admin, db, delete_storage_paths, doc_dict, flash_action,
                   flash_error, fs_opts, get_docs, image_fields, images_from_uploader, images_of, local_input_value,
                   log, next_target, now_utc, paginate_list, parse_local_datetime, plural, send_push, send_push_many,
                   storage_path_from_url, to_almaty, to_datetime, today_almaty, upload_image, where)

routes = Routes()

DESC = firestore.Query.DESCENDING
PER_PAGE = 20
LIST_LIMIT = 1000        # сколько последних документов держим в снапшоте списка
SNAPSHOT_TTL = 120       # с

# ---------- справочники (значения — как в приложении) ----------

NEWS_CATEGORIES = [('academic', 'Учёба'), ('sports', 'Спорт'), ('cultural', 'Культура'),
                   ('curator', 'Кураторский час')]
# Старые категории (прежний веб и ранние версии приложения): показываем по-русски, а не сырым ключом.
NEWS_LEGACY_CATEGORIES = {'events': 'События', 'achievements': 'Достижения', 'general': 'Общее'}
AUDIENCES = [('all', 'Все'), ('students', 'Студенты'), ('teachers', 'Преподаватели')]
POLL_TYPES = [('single', 'Один вариант'), ('multiple', 'Несколько вариантов')]
CLUB_CATEGORIES = [('physical', 'Физкультура'), ('creative', 'Творчество'), ('tech', 'Технологии'),
                   ('community', 'Сообщество')]
FEEDBACK_STATUSES = [('new', 'Новые'), ('answered', 'Отвечено'), ('closed', 'Закрыто')]
# «Срочно» (закрепляется наверху новых): тема «Линия доверия», анонимные и тревожные слова (основы, без регистра, ё=е).
FEEDBACK_URGENT_TOPICS = ('линия доверия',)
FEEDBACK_URGENT_WORDS = ('буллинг', 'булинг', 'травл', 'насили', 'угроз', 'суицид', 'самоубий', 'домогат')
# Шаблоны быстрого ответа (вставляются в поле ответа, текст можно поправить).
FEEDBACK_REPLY_TEMPLATES = [
    ('Передали ответственным', 'Спасибо за обращение! Мы передали вопрос ответственным и сообщим, когда будет решение.'),
    ('Вопрос решён', 'Спасибо, что сообщили. Вопрос решён — если проблема повторится, напишите нам снова.'),
    ('Нужны подробности', 'Спасибо за обращение. Уточните, пожалуйста, подробности: группу, дату и что именно произошло — '
                          'так мы быстрее поможем.'),
    ('Обратитесь к куратору', 'Спасибо за вопрос. По этой теме лучше всего поможет куратор группы или учебная часть — '
                              'подойдите к ним, они в курсе.'),
    ('Линия доверия', 'Спасибо за доверие. Мы отнесёмся к обращению серьёзно и примем меры. Если нужна поддержка прямо '
                      'сейчас — обратитесь к куратору или в администрацию колледжа.'),
]

NEWS_MAX_IMAGES = 10
POLL_MAX_OPTIONS = 20
TIME_RE = re.compile(r'^([01]?\d|2[0-3]):([0-5]\d)$')


def _label(options, value, default='—'):
    return dict(options).get(value, value or default)


def _news_category_label(key, default='Без категории'):
    """Подпись категории новости: категории приложения, затем старые ключи, иначе сам ключ."""
    if not key:
        return default
    return dict(NEWS_CATEGORIES).get(key) or NEWS_LEGACY_CATEGORIES.get(key) or key


# ==================== ОБЩИЕ ПОМОЩНИКИ ====================

def _norm(s):
    """Строка для поиска: нижний регистр, ё→е, без лишних пробелов."""
    return ' '.join(str(s or '').lower().replace('ё', 'е').split())


def _matches(query, *fields):
    words = _norm(query).split()
    if not words:
        return True
    hay = _norm(' '.join(str(f or '') for f in fields))
    return all(w in hay for w in words)


def _field(name, maxlen=None):
    """Текст из формы без пробелов по краям (None → '')."""
    v = (request.form.get(name) or '').strip()
    return v[:maxlen] if maxlen else v


def _as_int(v, default=0):
    if isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    try:
        return int(float(str(v).strip()))
    except (TypeError, ValueError):
        return default


def parse_number(raw):
    """'1 200', '1200,50', 1500 → число (int, если целое) или None."""
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        val = float(raw)
    else:
        s = str(raw).replace(' ', '').replace(' ', '').replace(',', '.').strip()
        if not s:
            return None
        try:
            val = float(s)
        except ValueError:
            return None
    if val != val or val in (float('inf'), float('-inf')):
        return None
    return int(val) if val.is_integer() else round(val, 2)


def _back(endpoint, **values):
    """Редирект на безопасный next из формы/query, иначе на url_for(endpoint) (core.back_url)."""
    return redirect(back_url(endpoint, **values))


def _undo_fields(endpoint, fields, **values):
    """Поля для POST «Отменить» в тосте: + next, чтобы отмена вернула в тот же отфильтрованный список."""
    return dict(fields, next=next_target() or url_for(endpoint, **values))


def _admin_uid():
    adm = current_admin() or {}
    return adm.get('uid')


def _invalidate(*kinds):
    for k in kinds:
        cache.invalidate('content:%s' % k)
    if 'feedback' in kinds:
        cache.invalidate('nav:badges')


def _snapshot(kind, query_factory, ttl=SNAPSHOT_TTL):
    """Кэшируемый список документов коллекции: [{'id', ...}]. Элементы НЕ менять (общий кэш)."""
    def load():
        return [doc_dict(s) for s in query_factory().stream(**fs_opts(True))]
    return cache.get_or_set('content:%s' % kind, load, ttl)


def _own_paths(items, prefix):
    """Пути Storage (из path или URL нашего бакета) только внутри папки prefix — чужие файлы не трогаем."""
    out = []
    for it in items or []:
        if isinstance(it, dict):
            it = it.get('path') or it.get('url')
        if not it:
            continue
        path = storage_path_from_url(it) if '://' in str(it) else str(it)
        if path and path.startswith(prefix) and path not in out:
            out.append(path)
    return out


def _single_image(name, current_url, folder):
    """Одна картинка из ui.image_uploader(max=1) (у событий, клубов, опросов, товаров в приложении — только imageUrl).

    → (новый imageUrl | None, путь загруженного файла | None, убранный старый URL | None).
    Без JS (нет состояния) выбранный файл заменяет текущую картинку. Бросает UploadError."""
    files = request.files.getlist(name)
    picked = next((f for f in files if getattr(f, 'filename', '')), None)
    keep, new_file = bool(current_url), picked
    raw = request.form.get(name + '__state')
    if raw:
        try:
            state = [it for it in json.loads(raw) if isinstance(it, dict)]
        except (ValueError, TypeError):
            state = None
        if state is not None:
            keep, new_file = False, None
            first = state[0] if state else None
            if first and first.get('kind') == 'existing' and current_url and first.get('url') == current_url:
                keep = True
            elif first and first.get('kind') == 'new':
                idx = _as_int(first.get('idx'), -1)
                if 0 <= idx < len(files) and getattr(files[idx], 'filename', ''):
                    new_file = files[idx]
    elif picked:
        keep = False
    if new_file is not None:
        up = upload_image(new_file, folder, 0)
        new_url, uploaded = up['url'], up['path']
    else:
        new_url, uploaded = (current_url if keep else None), None
    removed = current_url if current_url and current_url != new_url else None
    return new_url, uploaded, removed


def _delete_subcollection(ref, name, limit=450):
    """Удалить подколлекцию порциями. → сколько удалено."""
    total = 0
    coll = ref.collection(name)
    while True:
        snaps = list(coll.limit(limit).stream(**fs_opts(True)))
        if not snaps:
            return total
        batch = db.batch()
        for s in snaps:
            batch.delete(s.reference)
        batch.commit()
        total += len(snaps)
        if len(snaps) < limit:
            return total


def _delete_query(query, limit=450):
    total = 0
    while True:
        snaps = list(query.limit(limit).stream(**fs_opts(True)))
        if not snaps:
            return total
        batch = db.batch()
        for s in snaps:
            batch.delete(s.reference)
        batch.commit()
        total += len(snaps)
        if len(snaps) < limit:
            return total


def _is_http_url(url):
    return bool(re.match(r'^https?://[^\s/$.?#][^\s]*$', url or '', re.I))


# ==================== НОВОСТИ ====================

def _news_audience(n):
    """Аудитория так, как её понимает приложение: target_audience, иначе «все»."""
    aud = n.get('target_audience')
    return aud if aud in dict(AUDIENCES) else 'all'


def _news_snapshot():
    return _snapshot('news', lambda: db.collection('news').select(
        ['title', 'description', 'content', 'category', 'target_audience', 'createdAt', 'updatedAt', 'imageUrl',
         'images']).order_by('createdAt', direction=DESC).limit(LIST_LIMIT))


def _news_row(n):
    imgs = images_of(n)
    created = to_almaty(n.get('createdAt'))
    return {
        'id': n['id'], 'title': n.get('title') or 'Без заголовка',
        'summary': (n.get('description') or n.get('content') or '').strip(),
        'category': n.get('category') or '', 'category_label': _news_category_label(n.get('category')),
        'audience': _news_audience(n), 'audience_label': _label(AUDIENCES, _news_audience(n)),
        'cover': imgs[0]['url'] if imgs else None, 'photos': len(imgs), 'createdAt': n.get('createdAt'),
        'created_date': created.date() if created else None,
    }


def _news_category_filter(items):
    """Варианты фильтра «Категория»: категории приложения + старые ключи, которые реально есть в данных."""
    known = dict(NEWS_CATEGORIES)
    extra = sorted({n.get('category') for n in items if n.get('category') and n.get('category') not in known},
                   key=lambda k: _norm(_news_category_label(k)))
    return list(NEWS_CATEGORIES) + [(k, _news_category_label(k)) for k in extra]


@routes.route('/news')
@admin_required
def news():
    items = _news_snapshot()
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '')
    audience = request.args.get('audience', '')
    photo = request.args.get('photo', '')
    rows = []
    for n in items:
        if category and (n.get('category') or '') != category:
            continue
        if audience and _news_audience(n) != audience:
            continue
        has_photo = bool(images_of(n))
        if photo == 'with' and not has_photo or photo == 'without' and has_photo:
            continue
        if q and not _matches(q, n.get('title'), n.get('description'), n.get('content')):
            continue
        rows.append(n)
    month_ago = now_utc() - timedelta(days=30)
    stats = {
        'total': len(items),
        'month': sum(1 for n in items if (to_datetime(n.get('createdAt')) or month_ago) > month_ago),
        'with_photo': sum(1 for n in items if images_of(n)),
    }
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    page.items = [_news_row(n) for n in page.items]
    return render_template('news.html', page=page, stats=stats, filtered=len(rows), truncated=len(items) >= LIST_LIMIT,
                           categories=_news_category_filter(items), audiences=AUDIENCES, q=q, category=category,
                           audience=audience, photo=photo)


def _news_category_options(current=None):
    """Варианты категории в форме: категории приложения; у старой новости — её категория с пометкой."""
    opts = list(NEWS_CATEGORIES)
    if current and current not in dict(NEWS_CATEGORIES):
        opts.append((current, '%s (старая категория)' % _news_category_label(current)))
    return opts


# Кому уйдёт пуш onNewsCreate/onPollCreate (топики news_all|news_student|news_teacher) — текст подтверждения.
PUSH_AUDIENCE = {'all': 'все пользователи приложения', 'students': 'студенты', 'teachers': 'преподаватели'}


def _news_form_values(news_doc=None):
    if request.method == 'POST':
        return {'title': _field('title'), 'description': _field('description'), 'content': request.form.get('content', ''),
                'category': _field('category'), 'target_audience': _field('target_audience')}
    n = news_doc or {}
    return {'title': n.get('title', ''), 'description': n.get('description') or '', 'content': n.get('content', ''),
            'category': n.get('category') or 'academic', 'target_audience': _news_audience(n)}


def _validate_news(values, current_category=None):
    errors = {}
    if not values['title']:
        errors['title'] = 'Укажите заголовок'
    elif len(values['title']) > 200:
        errors['title'] = 'Заголовок — не длиннее 200 символов'
    if len(values['description']) > 300:
        errors['description'] = 'Краткое описание — не длиннее 300 символов'
    if len(values['content']) > 20000:
        errors['content'] = 'Текст слишком длинный (больше 20 000 символов)'
    if values['category'] not in dict(NEWS_CATEGORIES) and values['category'] != (current_category or None):
        errors['category'] = 'Выберите категорию'
    if values['target_audience'] not in dict(AUDIENCES):
        errors['target_audience'] = 'Выберите аудиторию'
    return errors


@routes.route('/news/add', methods=['GET', 'POST'])
@routes.route('/news/edit/<news_id>', methods=['GET', 'POST'])
@admin_required
def news_form(news_id=None):
    doc = None
    if news_id:
        doc = doc_dict(db.collection('news').document(news_id).get(**fs_opts()))
        if doc is None:
            flash('Новость не найдена — возможно, её уже удалили', 'warning')
            return redirect(url_for('news'))
    values = _news_form_values(doc)
    errors = {}
    if request.method == 'POST':
        errors = _validate_news(values, (doc or {}).get('category'))
        if not errors:
            ref = db.collection('news').document(news_id) if news_id else db.collection('news').document()
            existing = images_of(doc) if doc else []
            try:
                res = images_from_uploader('images', existing, 'news/%s' % ref.id, max_count=NEWS_MAX_IMAGES)
            except UploadError as e:
                errors['images'] = str(e)
            else:
                data = {
                    'title': values['title'], 'content': values['content'].strip(),
                    'description': values['description'] or None, 'category': values['category'],
                    'target_audience': values['target_audience'], **image_fields(res.images),
                    'updatedAt': SERVER_TIMESTAMP, 'updatedBy': _admin_uid(),
                }
                try:
                    if news_id:
                        ref.update(data)
                    else:
                        # документ пишется ПОСЛЕ загрузки фото: пуш onNewsCreate берёт imageUrl
                        data.update({'groupId': 'all', 'createdAt': SERVER_TIMESTAMP, 'createdBy': _admin_uid()})
                        ref.set(data)
                except Exception as e:
                    delete_storage_paths(res.uploaded_paths)
                    flash_error('Не удалось сохранить новость. Попробуйте ещё раз', e)
                else:
                    delete_storage_paths(_own_paths(res.removed, 'news/'))
                    _invalidate('news')
                    audit('update' if news_id else 'create', 'news', ref.id,
                          {'title': values['title'], 'photos': len(res.images)})
                    if news_id:
                        flash('Новость обновлена', 'success')
                    else:
                        flash_action('Новость опубликована — приложение пришлёт уведомление', 'Открыть',
                                     href=url_for('news_form', news_id=ref.id))
                    return redirect(back_url('news'))
    preview_images = images_of(doc) if doc else []
    return render_template('news_form.html', news=doc, values=values, errors=errors,
                           categories=_news_category_options((doc or {}).get('category')), audiences=AUDIENCES,
                           max_images=NEWS_MAX_IMAGES, preview_images=preview_images, push_audience=PUSH_AUDIENCE)


def _delete_news_doc(news_id):
    """Удалить новость и ВСЕ её фото (как NewsService.deleteNews). → заголовок или None."""
    ref = db.collection('news').document(news_id)
    snap = ref.get(**fs_opts())
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    ref.delete()
    delete_storage_paths(_own_paths(images_of(data), 'news/'))
    return data.get('title') or news_id


@routes.route('/news/delete/<news_id>', methods=['POST'])
@admin_required
def news_delete(news_id):
    try:
        title = _delete_news_doc(news_id)
    except Exception as e:
        flash_error('Не удалось удалить новость', e)
        return _back('news')
    _invalidate('news')
    if title is None:
        flash('Новость уже удалена', 'info')
    else:
        audit('delete', 'news', news_id, {'title': title})
        flash('Новость «%s» удалена вместе с фото' % title, 'success')
    return _back('news')


@routes.route('/news/bulk', methods=['POST'])
@admin_required
def news_bulk():
    ids = [i for i in request.form.getlist('ids') if i][:200]
    if request.form.get('action') != 'delete' or not ids:
        flash('Отметьте новости для удаления', 'warning')
        return _back('news')
    done = 0
    for nid in ids:
        try:
            if _delete_news_doc(nid) is not None:
                done += 1
        except Exception:
            log.exception('Массовое удаление: новость %s', nid)
    _invalidate('news')
    audit('bulk_delete', 'news', None, {'ids': ids, 'deleted': done})
    flash('Удалено %d %s' % (done, plural(done, 'новость', 'новости', 'новостей')), 'success')
    return _back('news')


# ==================== ОПРОСЫ ====================

def _poll_meta(p, now=None):
    """Нормализованные поля опроса так, как их читает приложение (poll_service.dart)."""
    now = now or now_utc()
    aud = p.get('target_audience') if p.get('target_audience') in dict(AUDIENCES) else 'all'
    ptype = p.get('poll_type') if p.get('poll_type') in dict(POLL_TYPES) else 'multiple'
    # анонимность: поле приложения; старые/чужие записи с isAnonymous — тоже скрываем голосовавших
    anon = bool(p['is_anonymous']) if 'is_anonymous' in p else bool(p.get('isAnonymous'))
    end = to_datetime(p.get('endTime'))
    active = end is None or end > now
    left = ''
    if end and active:
        sec = (end - now).total_seconds()
        days, hours = int(sec // 86400), int(sec // 3600)
        left = ('ещё %d %s' % (days, plural(days, 'день', 'дня', 'дней')) if days >= 1
                else ('ещё %d ч' % hours if hours >= 1 else 'меньше часа'))
    return {'audience': aud, 'audience_label': _label(AUDIENCES, aud), 'type': ptype,
            'type_label': _label(POLL_TYPES, ptype), 'anonymous': anon, 'end': end, 'active': active,
            'left_label': left}


def _polls_snapshot():
    return _snapshot('polls', lambda: db.collection('polls').select(
        ['title', 'description', 'target_audience', 'poll_type', 'is_anonymous', 'isAnonymous', 'endTime',
         'createdAt', 'imageUrl', 'createdBy']).order_by('createdAt', direction=DESC).limit(LIST_LIMIT))


def _poll_totals(ids):
    """Сумма голосов по вариантам для опросов страницы (aggregation sum, параллельно, кэш 60 с)."""
    out, missing = {}, []
    for pid in ids:
        v = cache.get('content:polls:votes:%s' % pid)
        if v is None:
            missing.append(pid)
        else:
            out[pid] = v

    def work(pid):
        q = db.collection('polls').document(pid).collection('answers')
        total = aggregate_sum(q, 'votes', default=None)
        if total is None:  # нет агрегаций — читаем только поле votes
            total = sum(_as_int((s.to_dict() or {}).get('votes')) for s in q.select(['votes']).stream(**fs_opts(True)))
        return pid, _as_int(total)

    if missing:
        with ThreadPoolExecutor(max_workers=min(8, len(missing))) as ex:
            for pid, total in ex.map(work, missing):
                out[pid] = total
                cache.set('content:polls:votes:%s' % pid, total, ttl=60)
    return out


@routes.route('/polls', methods=['GET', 'POST'])
@admin_required
def polls():
    if request.method == 'POST':  # старый URL принимал POST — показываем список
        return redirect(url_for('polls'))
    items = _polls_snapshot()
    now = now_utc()
    q = request.args.get('q', '').strip()
    status = request.args.get('status', '')
    audience = request.args.get('audience', '')
    rows = []
    counts = {'all': len(items), 'active': 0, 'finished': 0}
    for p in items:
        meta = _poll_meta(p, now)
        counts['active' if meta['active'] else 'finished'] += 1
        if status == 'active' and not meta['active'] or status == 'finished' and meta['active']:
            continue
        if audience and meta['audience'] != audience:
            continue
        if q and not _matches(q, p.get('title'), p.get('description')):
            continue
        rows.append(dict(p, meta=meta))
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    totals = _poll_totals([p['id'] for p in page.items])
    for p in page.items:
        p['votes'] = totals.get(p['id'], 0)
    return render_template('polls.html', page=page, counts=counts, status=status, audiences=AUDIENCES, q=q,
                           audience=audience, truncated=len(items) >= LIST_LIMIT)


def _load_answers(poll_id):
    snaps = list(db.collection('polls').document(poll_id).collection('answers').limit(200).stream(**fs_opts(True)))
    answers = [doc_dict(s) for s in snaps]

    def key(a):
        ts = to_datetime(a.get('createdAt'))
        return (_as_int(a.get('order'), 9999), ts.timestamp() if ts else 0, a['id'])
    answers.sort(key=key)
    return answers


def _poll_form_values(poll=None):
    if request.method == 'POST':
        return {'title': _field('title'), 'description': _field('description'),
                'target_audience': _field('target_audience') or 'all', 'poll_type': _field('poll_type') or 'multiple',
                'is_anonymous': bool(request.form.get('is_anonymous')), 'endTime': _field('endTime')}
    if poll:
        meta = _poll_meta(poll)
        return {'title': poll.get('title', ''), 'description': poll.get('description') or '',
                'target_audience': meta['audience'], 'poll_type': meta['type'], 'is_anonymous': meta['anonymous'],
                'endTime': local_input_value(poll.get('endTime'))}
    return {'title': '', 'description': '', 'target_audience': 'all', 'poll_type': 'single', 'is_anonymous': False,
            'endTime': local_input_value(now_utc() + timedelta(days=7))}


def _clean_options(raw_list):
    out, seen = [], set()
    for t in raw_list:
        t = (t or '').strip()[:200]
        if t and _norm(t) not in seen:
            seen.add(_norm(t))
            out.append(t)
    return out


@routes.route('/poll/add', methods=['GET', 'POST'])               # старые адреса (закладки)
@routes.route('/poll/edit/<poll_id>', methods=['GET', 'POST'])
@routes.route('/polls/new', methods=['GET', 'POST'])               # канонические: url_for строит их
@routes.route('/polls/<poll_id>/edit', methods=['GET', 'POST'])    # (внутренние декораторы регистрируются первыми)
@admin_required
def poll_form(poll_id=None):
    poll, answers = None, []
    if poll_id:
        poll = doc_dict(db.collection('polls').document(poll_id).get(**fs_opts()))
        if poll is None:
            flash('Опрос не найден — возможно, его уже удалили', 'warning')
            return redirect(url_for('polls'))
        answers = _load_answers(poll_id)
    values = _poll_form_values(poll)
    errors = {}
    new_options = _clean_options(request.form.getlist('options')) if request.method == 'POST' else []
    if request.method == 'POST':
        if not values['title']:
            errors['title'] = 'Укажите вопрос опроса'
        elif len(values['title']) > 200:
            errors['title'] = 'Не длиннее 200 символов'
        if len(values['description']) > 1000:
            errors['description'] = 'Описание — не длиннее 1000 символов'
        if values['target_audience'] not in dict(AUDIENCES):
            errors['target_audience'] = 'Выберите аудиторию'
        if values['poll_type'] not in dict(POLL_TYPES):
            errors['poll_type'] = 'Выберите тип'
        end_time = None
        if values['endTime']:
            end_time = parse_local_datetime(values['endTime'])
            if end_time is None:
                errors['endTime'] = 'Неверный формат даты'
            elif not poll_id and end_time <= now_utc():
                errors['endTime'] = 'Время окончания уже прошло'
        # варианты: существующие (правка/удаление) + новые
        by_id = {a['id']: a for a in answers}
        delete_ids = {i for i in request.form.getlist('delete_answers') if i in by_id}
        kept_ids = [i for i in dict.fromkeys(request.form.getlist('answer_ids')) if i in by_id and i not in delete_ids]
        kept_ids += [a['id'] for a in answers if a['id'] not in kept_ids and a['id'] not in delete_ids]
        texts = {}
        for aid in kept_ids + sorted(delete_ids):
            t = request.form.get('answer_text_%s' % aid)
            t = (t if t is not None else by_id[aid].get('text', '')).strip()[:200]
            if not t and aid not in delete_ids:
                errors['options'] = 'Текст варианта не может быть пустым (удалите вариант крестиком)'
            texts[aid] = t
        total = len(kept_ids) + len(new_options)
        if total < 2:
            errors['options'] = 'Нужно минимум 2 варианта ответа'
        elif total > POLL_MAX_OPTIONS:
            errors['options'] = 'Не больше %d вариантов' % POLL_MAX_OPTIONS
        if not errors:
            ref = db.collection('polls').document(poll_id) if poll_id else db.collection('polls').document()
            try:
                image_url, uploaded, removed = _single_image('image', (poll or {}).get('imageUrl'), 'polls')
            except UploadError as e:
                errors['image'] = str(e)
            else:
                data = {'title': values['title'], 'description': values['description'],
                        'target_audience': values['target_audience'], 'poll_type': values['poll_type'],
                        'is_anonymous': values['is_anonymous'], 'endTime': end_time, 'imageUrl': image_url,
                        'updatedAt': SERVER_TIMESTAMP}
                base = now_utc()
                batch = db.batch()
                if poll_id:
                    batch.update(ref, data)
                else:
                    data.update({'createdAt': SERVER_TIMESTAMP, 'createdBy': _admin_uid()})
                    batch.set(ref, data)
                answers_ref = ref.collection('answers')
                for pos, aid in enumerate(kept_ids):
                    upd = {'text': texts[aid], 'order': pos}
                    if not by_id[aid].get('createdAt'):  # без createdAt приложение вариант не покажет
                        upd['createdAt'] = base - timedelta(days=1) + timedelta(milliseconds=pos)
                    if not isinstance(by_id[aid].get('votes'), int):
                        upd['votes'] = _as_int(by_id[aid].get('votes'))
                    batch.update(answers_ref.document(aid), upd)
                for k, text in enumerate(new_options):
                    batch.set(answers_ref.document(), {'text': text, 'votes': 0, 'order': len(kept_ids) + k,
                                                       'createdAt': base + timedelta(milliseconds=k)})
                for aid in delete_ids:
                    batch.delete(answers_ref.document(aid))
                try:
                    batch.commit()
                except Exception as e:
                    if uploaded:
                        delete_storage_paths([uploaded])
                    flash_error('Не удалось сохранить опрос. Попробуйте ещё раз', e)
                else:
                    if removed:
                        delete_storage_paths(_own_paths([removed], 'polls/'))
                    _invalidate('polls')
                    audit('update' if poll_id else 'create', 'poll', ref.id,
                          {'title': values['title'], 'options': total, 'deleted_options': len(delete_ids)})
                    flash_action('Опрос сохранён' if poll_id else 'Опрос создан — приложение пришлёт уведомление',
                                 'Результаты', href=url_for('poll_results', poll_id=ref.id))
                    return redirect(back_url('polls'))
        if errors:
            # показать форму так, как её заполнили: тексты, порядок и отмеченные на удаление варианты
            order = {aid: i for i, aid in enumerate(kept_ids)}
            answers = [dict(a, text=texts.get(a['id'], a.get('text', '')), deleted=a['id'] in delete_ids)
                       for a in answers]
            answers.sort(key=lambda a: order.get(a['id'], len(order)))
    return render_template('poll_form.html', poll=poll, answers=answers, values=values, errors=errors,
                           new_options=new_options, audiences=AUDIENCES, poll_types=POLL_TYPES,
                           max_options=POLL_MAX_OPTIONS, push_audience=PUSH_AUDIENCE)


@routes.route('/poll/<poll_id>/add-answer', methods=['POST'])
@admin_required
def poll_add_answer(poll_id):
    """Добавить вариант к опросу (старый маршрут формы)."""
    text = (request.form.get('answer') or '').strip()[:200]
    ref = db.collection('polls').document(poll_id)
    if not text:
        flash('Введите текст варианта', 'error')
    elif not ref.get(**fs_opts()).exists:
        flash('Опрос не найден', 'warning')
        return redirect(url_for('polls'))
    else:
        try:
            n = count(ref.collection('answers'), default=0) or 0
            ref.collection('answers').add({'text': text, 'votes': 0, 'order': n, 'createdAt': SERVER_TIMESTAMP})
            _invalidate('polls')
            audit('add_answer', 'poll', poll_id, {'text': text})
            flash('Вариант добавлен', 'success')
        except Exception as e:
            flash_error('Не удалось добавить вариант', e)
    return redirect(url_for('poll_form', poll_id=poll_id))


@routes.route('/poll/<poll_id>/delete-answer/<answer_id>', methods=['POST'])
@admin_required
def poll_delete_answer(poll_id, answer_id):
    try:
        db.collection('polls').document(poll_id).collection('answers').document(answer_id).delete()
        _invalidate('polls')
        audit('delete_answer', 'poll', poll_id, {'answer': answer_id})
        flash('Вариант удалён', 'success')
    except Exception as e:
        flash_error('Не удалось удалить вариант', e)
    return redirect(url_for('poll_form', poll_id=poll_id))


def _end_value(end):
    """endTime → значение поля end для отмены (ISO UTC или 'none' — без срока)."""
    return end.astimezone(timezone.utc).isoformat() if end else 'none'


@routes.route('/poll/<poll_id>/close', methods=['POST'])
@admin_required
def poll_close(poll_id):
    """Завершить сейчас. Без подтверждения: тост «Отменить» возвращает прежний срок (poll_set_end)."""
    ref = db.collection('polls').document(poll_id)
    try:
        snap = ref.get(**fs_opts())
        if not snap.exists:
            flash('Опрос не найден', 'warning')
            return _back('polls')
        prev = to_datetime((snap.to_dict() or {}).get('endTime'))
        ref.update({'endTime': now_utc(), 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось завершить опрос', e)
        return _back('polls')
    _invalidate('polls')
    audit('close', 'poll', poll_id)
    flash_action('Опрос завершён — голосование закрыто', 'Отменить', post=url_for('poll_set_end', poll_id=poll_id),
                 fields=_undo_fields('polls', {'end': _end_value(prev)}))
    return _back('polls')


@routes.route('/poll/<poll_id>/end', methods=['POST'])
@admin_required
def poll_set_end(poll_id):
    """Вернуть срок опроса (кнопка «Отменить» после «Завершить»/«Продлить»): end = ISO-время или 'none'."""
    raw = (request.form.get('end') or '').strip()
    end = None
    if raw != 'none':
        try:
            end = datetime.fromisoformat(raw)
        except ValueError:
            flash('Не удалось вернуть срок опроса', 'error')
            return _back('polls')
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
    ref = db.collection('polls').document(poll_id)
    try:
        if not ref.get(**fs_opts()).exists:
            flash('Опрос не найден', 'warning')
            return _back('polls')
        ref.update({'endTime': end, 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось изменить срок опроса', e)
        return _back('polls')
    _invalidate('polls')
    audit('set_end', 'poll', poll_id, {'end': raw})
    flash('Срок опроса возвращён' + ((' — до ' + to_almaty(end).strftime('%d.%m.%Y %H:%M')) if end else ' — без срока'),
          'success')
    return _back('polls')


@routes.route('/poll/<poll_id>/extend', methods=['POST'])
@admin_required
def poll_extend(poll_id):
    days = max(1, min(_as_int(request.form.get('days'), 7), 90))
    ref = db.collection('polls').document(poll_id)
    try:
        snap = ref.get(**fs_opts())
        if not snap.exists:
            flash('Опрос не найден', 'warning')
            return _back('polls')
        now = now_utc()
        end = to_datetime((snap.to_dict() or {}).get('endTime'))
        new_end = max(end or now, now) + timedelta(days=days)
        ref.update({'endTime': new_end, 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось продлить опрос', e)
        return _back('polls')
    _invalidate('polls')
    audit('extend', 'poll', poll_id, {'days': days})
    flash_action('Опрос продлён на %d %s — до %s' % (days, plural(days, 'день', 'дня', 'дней'),
                                                   to_almaty(new_end).strftime('%d.%m %H:%M')),
                 'Отменить', post=url_for('poll_set_end', poll_id=poll_id),
                 fields=_undo_fields('polls', {'end': _end_value(end)}))
    return _back('polls')


@routes.route('/poll/delete/<poll_id>', methods=['POST'])
@admin_required
def poll_delete(poll_id):
    """Удалить опрос: варианты, голоса обеих схем, картинку и сам документ."""
    ref = db.collection('polls').document(poll_id)
    try:
        snap = ref.get(**fs_opts())
        data = snap.to_dict() or {} if snap.exists else {}
        _delete_subcollection(ref, 'answers')
        responses = db.collection('poll_responses')
        n = _delete_query(where(responses, 'pollId', '==', poll_id))
        n += _delete_query(where(responses, 'poll_id', '==', poll_id))
        ref.delete()
        delete_storage_paths(_own_paths([data.get('imageUrl')], 'polls/'))
    except Exception as e:
        flash_error('Не удалось удалить опрос', e)
        return _back('polls')
    _invalidate('polls')
    audit('delete', 'poll', poll_id, {'title': data.get('title'), 'responses': n})
    flash('Опрос удалён вместе с голосами', 'success')
    return _back('polls')


@routes.route('/active-polls')
@admin_required
def active_polls():
    """Старая публичная страница голосования закрыта: голосуют только в приложении."""
    return redirect(url_for('polls', status='active'))


@routes.route('/poll/<poll_id>/vote')
@admin_required
def poll_vote(poll_id):
    return redirect(url_for('poll_results', poll_id=poll_id))


def _poll_votes(poll_id):
    """Голоса опроса из poll_responses (схема приложения + старая веб-схема).

    → (voters: [{uid, answers: [id], at}], anonymous_legacy: [answer_id])."""
    responses = db.collection('poll_responses')
    voters, anon_legacy = {}, []
    for s in where(responses, 'pollId', '==', poll_id).select(['userId', 'answers', 'votedAt']).limit(5000) \
            .stream(**fs_opts(True)):
        d = s.to_dict() or {}
        uid = d.get('userId') or s.id
        ans = [str(a) for a in (d.get('answers') or []) if a]
        v = voters.setdefault(uid, {'uid': d.get('userId'), 'answers': [], 'at': d.get('votedAt')})
        v['answers'] += [a for a in ans if a not in v['answers']]
    for s in where(responses, 'poll_id', '==', poll_id).select(['user_id', 'answer_id', 'votedAt']).limit(5000) \
            .stream(**fs_opts(True)):
        d = s.to_dict() or {}
        aid = d.get('answer_id')
        uid = d.get('user_id')
        if not uid:
            if aid:
                anon_legacy.append(str(aid))
            continue
        v = voters.setdefault(uid, {'uid': uid, 'answers': [], 'at': d.get('votedAt')})
        if aid and aid not in v['answers']:
            v['answers'].append(str(aid))
    return list(voters.values()), anon_legacy


POLL_RESULTS_TTL = 60   # с: результаты опроса (админы обновляют страницу, следя за голосованием)
VOTERS_SHOWN = 200      # строк в таблице участников; полный список — CSV


def _poll_results_data(poll_id, anonymous):
    """Голоса для страницы результатов (кэш 60 с; сбрасывается правкой опроса — ключ под content:polls).
    Анонимный опрос: только число голосовавших — count() по схеме приложения (один ответ на человека), без
    чтения документов; есть голоса старой веб-схемы — читаем как раньше (там ответ на вариант, не на человека).
    → {'count'} или {'count', 'voters', 'anon_legacy', 'users'}."""
    def load():
        if anonymous:
            responses = db.collection('poll_responses')
            n = count(where(responses, 'pollId', '==', poll_id), default=None)
            legacy = n is None or count(where(responses, 'poll_id', '==', poll_id), default=1) > 0
            if not legacy:
                return {'count': n}
            voters, anon_legacy = _poll_votes(poll_id)
            return {'count': len(voters) + len(anon_legacy)}
        voters, anon_legacy = _poll_votes(poll_id)
        users = get_docs('users', [v['uid'] for v in voters if v['uid']], ['name', 'group', 'role'])
        return {'count': len(voters) + len(anon_legacy), 'voters': voters, 'anon_legacy': anon_legacy,
                'users': users}
    return cache.get_or_set('content:polls:results:%s' % poll_id, load, POLL_RESULTS_TTL)


@routes.route('/poll/<poll_id>/results')
@routes.route('/polls/<poll_id>/results')
@admin_required
def poll_results(poll_id):
    poll = doc_dict(db.collection('polls').document(poll_id).get(**fs_opts()))
    if poll is None:
        flash('Опрос не найден — возможно, его уже удалили', 'warning')
        return redirect(url_for('polls'))
    meta = _poll_meta(poll)
    answers = _load_answers(poll_id)
    data = _poll_results_data(poll_id, meta['anonymous'])
    total_votes = sum(_as_int(a.get('votes')) for a in answers)
    leader = max((_as_int(a.get('votes')) for a in answers), default=0)
    for a in answers:
        a['votes'] = _as_int(a.get('votes'))
        a['pct'] = round(100.0 * a['votes'] / total_votes, 1) if total_votes else 0
        a['leader'] = leader > 0 and a['votes'] == leader
        a['voters'] = []
    by_id = {a['id']: a for a in answers}
    people = []
    if not meta['anonymous']:
        users = data['users']
        for v in data['voters']:
            u = users.get(v['uid'] or '', {})
            person = {'uid': v['uid'], 'name': u.get('name') or ('Удалённый пользователь' if v['uid'] else 'Без входа'),
                      'group': u.get('group') or '', 'role': u.get('role') or '', 'at': v['at'],
                      'answers': [by_id[a]['text'] for a in v['answers'] if a in by_id]}
            people.append(person)
            for a in v['answers']:
                if a in by_id:
                    by_id[a]['voters'].append(person)
        people.sort(key=lambda p: _norm(p['name']))
    voters_count = data['count']
    # голоса есть, а списка голосовавших нет (старые опросы, данные до poll_responses) — число людей неизвестно
    voters_known = voters_count > 0 or total_votes == 0
    if request.args.get('format') == 'csv':
        audit('export', 'poll', poll_id, {'format': 'csv'})
        name = 'опрос_%s.csv' % re.sub(r'[^\w-]+', '_', (poll.get('title') or poll_id))[:40]
        if meta['anonymous']:
            rows = [[a['text'], a['votes'], '%s%%' % a['pct']] for a in answers]
            return csv_response(name, ['Вариант', 'Голосов', 'Доля'], rows)
        rows = [[p['name'], p['group'], p['role'], '; '.join(p['answers']), p['at']] for p in people]
        return csv_response(name, ['Участник', 'Группа', 'Роль', 'Ответы', 'Время голоса'], rows)
    eligible = _audience_size(meta['audience']) if voters_known else None
    return render_template('poll_results.html', poll=poll, meta=meta, answers=answers, total_votes=total_votes,
                           voters_count=voters_count, voters_known=voters_known, eligible=eligible,
                           people=people[:VOTERS_SHOWN], people_total=len(people))


AUDIENCE_ROLES = {'all': ('student', 'teacher'), 'students': ('student',), 'teachers': ('teacher',)}


def _audience_size(audience):
    """Сколько аккаунтов в аудитории опроса (count() по ролям, кэш 10 мин). None — посчитать не удалось."""
    def load():
        total = 0
        for role in AUDIENCE_ROLES.get(audience, AUDIENCE_ROLES['all']):
            n = count(where(db.collection('users'), 'role', '==', role), default=None)
            if n is None:
                return None
            total += n
        return total
    return cache.get_or_set('content:audience:%s' % audience, load, ttl=600)


# ==================== СОБЫТИЯ И КЛУБЫ: ОБЩЕЕ ====================

def _event_date(value):
    """Дата события (date по Алматы) из Timestamp или строки 'YYYY-MM-DD' (старые записи)."""
    if value is None or value == '':
        return None
    if isinstance(value, str) and re.match(r'^\d{4}-\d{2}-\d{2}', value.strip()):
        try:
            return datetime.strptime(value.strip()[:10], '%Y-%m-%d').date()
        except ValueError:
            return None
    dt = to_almaty(value)
    return dt.date() if dt else None


ENTITY = {
    'events': {'label': 'событие', 'list': 'events', 'regs': 'event_registrations', 'arg': 'event_id',
               'folder': 'events/'},
    'clubs': {'label': 'клуб', 'list': 'clubs', 'regs': 'club_registrations', 'arg': 'club_id', 'folder': 'clubs/'},
}


def _registrations(coll, doc_id):
    """Участники события/клуба с данными из users (одно пакетное чтение вместо N+1)."""
    regs = [doc_dict(s) for s in db.collection(coll).document(doc_id).collection('registrations').limit(5000)
            .stream(**fs_opts(True))]
    users = get_docs('users', [r['id'] for r in regs], ['name', 'group', 'phone', 'phoneNumber', 'email', 'role'])
    rows = []
    for r in regs:
        u = users.get(r['id'])
        rows.append({
            'id': r['id'],
            'name': (u or {}).get('name') or r.get('userName') or r.get('name') or 'Удалённый пользователь',
            'group': (u or {}).get('group') or r.get('groupId') or r.get('group') or '',
            'phone': (u or {}).get('phone') or (u or {}).get('phoneNumber') or '',
            'email': (u or {}).get('email') or '', 'role': (u or {}).get('role') or '',
            'registeredAt': r.get('registeredAt') or r.get('createdAt'), 'missing': u is None,
        })
    rows.sort(key=lambda x: (to_datetime(x['registeredAt']) or datetime.min.replace(tzinfo=timezone.utc)),
              reverse=True)
    return rows


def _registrations_page(coll, doc_id, template):
    cfg = ENTITY[coll]
    item = doc_dict(db.collection(coll).document(doc_id).get(**fs_opts()))
    if item is None:
        flash('Событие не найдено' if coll == 'events' else 'Клуб не найден', 'warning')
        return redirect(url_for(cfg['list']))
    rows = _registrations(coll, doc_id)
    q = request.args.get('q', '').strip()
    if request.args.get('format') == 'csv':
        audit('export', coll, doc_id, {'format': 'csv', 'rows': len(rows)})
        title = item.get('title') or item.get('name') or doc_id
        return csv_response('участники_%s.csv' % re.sub(r'[^\w-]+', '_', title)[:40],
                            ['ФИО', 'Группа', 'Роль', 'Телефон', 'Email', 'Зарегистрирован'],
                            [[r['name'], r['group'], r['role'], r['phone'], r['email'], r['registeredAt']] for r in rows])
    groups = {}
    for r in rows:
        groups[r['group'] or 'Без группы'] = groups.get(r['group'] or 'Без группы', 0) + 1
    shown = [r for r in rows if _matches(q, r['name'], r['group'], r['phone'], r['email'])]
    stored = _as_int(item.get('registrationsCount'))
    return render_template(template, kind=coll, item=item, title=item.get('title') or item.get('name') or 'Без названия',
                           rows=shown, total=len(rows), stored_count=stored, q=q,
                           top_groups=sorted(groups.items(), key=lambda kv: -kv[1])[:6],
                           event_date=_event_date(item.get('date')) if coll == 'events' else None)


def _delete_registration(coll, doc_id, uid):
    """Удалить регистрацию и уменьшить счётчик в одной транзакции (не ниже 0). → True, если была."""
    parent = db.collection(coll).document(doc_id)
    reg = parent.collection('registrations').document(uid)

    @firestore.transactional
    def run(txn):
        reg_snap = reg.get(transaction=txn)
        parent_snap = parent.get(transaction=txn)
        if not reg_snap.exists:
            return False
        txn.delete(reg)
        if parent_snap.exists:
            cnt = _as_int((parent_snap.to_dict() or {}).get('registrationsCount'))
            txn.update(parent, {'registrationsCount': max(0, cnt - 1)})
        return True
    return run(db.transaction())


def _recount(coll, doc_id):
    ref = db.collection(coll).document(doc_id)
    n = count(ref.collection('registrations'), default=None)
    if n is None:
        n = sum(1 for _ in ref.collection('registrations').select([]).stream(**fs_opts(True)))
    ref.update({'registrationsCount': int(n)})
    return n


def _remind(coll, doc_id, title, message):
    """Пуш участникам по их fcmToken. → (отправлено, всего участников)."""
    regs = [s.id for s in db.collection(coll).document(doc_id).collection('registrations').select([]).limit(2000)
            .stream(**fs_opts(True))]
    users = get_docs('users', regs, ['fcmToken'])
    data = {'type': 'event' if coll == 'events' else 'club', 'id': doc_id}
    tokens = dict.fromkeys(t for t in ((users.get(uid) or {}).get('fcmToken') for uid in regs) if t)
    # одной пачкой (send_each по 500), а не HTTPS-запросом на каждый токен — сотни участников не держат воркер
    sent = send_push_many([{'title': title, 'body': message, 'token': t, 'data': data} for t in tokens])
    return sent, len(regs)


def _delete_entity(coll, doc_id):
    """Удалить событие/клуб с регистрациями и картинкой. → название или None."""
    ref = db.collection(coll).document(doc_id)
    snap = ref.get(**fs_opts())
    if not snap.exists:
        return None
    data = snap.to_dict() or {}
    _delete_subcollection(ref, 'registrations')
    ref.delete()
    delete_storage_paths(_own_paths([data.get('imageUrl')], ENTITY[coll]['folder']))
    return data.get('title') or data.get('name') or doc_id


# ==================== СОБЫТИЯ ====================

def _events_snapshot():
    return _snapshot('events', lambda: db.collection('events').select(
        ['title', 'description', 'date', 'time', 'location', 'organizer', 'imageUrl', 'registrationsCount',
         'createdAt']).limit(LIST_LIMIT))


@routes.route('/events')
@admin_required
def events():
    today = today_almaty()
    q = request.args.get('q', '').strip()
    when = request.args.get('when', '')
    rows, counts = [], {'all': 0, 'upcoming': 0, 'past': 0}
    for e in _events_snapshot():
        d = _event_date(e.get('date'))
        upcoming = d is None or d >= today
        counts['all'] += 1
        counts['upcoming' if upcoming else 'past'] += 1
        if when == 'upcoming' and not upcoming or when == 'past' and upcoming:
            continue
        if q and not _matches(q, e.get('title'), e.get('description'), e.get('location'), e.get('organizer')):
            continue
        rows.append(dict(e, day=d, upcoming=upcoming, days_left=(d - today).days if d else None,
                         regs=max(0, _as_int(e.get('registrationsCount')))))
    # ближайшие — по возрастанию даты, прошедшие — сначала свежие (как в приложении)
    future = sorted([r for r in rows if r['upcoming']], key=lambda r: (r['day'] is None, r['day'] or today))
    past = sorted([r for r in rows if not r['upcoming']], key=lambda r: r['day'], reverse=True)
    page = paginate_list(future + past, request.args.get('page'), PER_PAGE)
    return render_template('events.html', page=page, counts=counts, when=when, q=q, today=today)


def _event_form_values(ev=None):
    if request.method == 'POST':
        return {k: _field(k) for k in ('title', 'description', 'date', 'time', 'location', 'organizer')}
    ev = ev or {}
    d = _event_date(ev.get('date'))
    return {'title': ev.get('title', ''), 'description': ev.get('description') or '', 'date': d.isoformat() if d else '',
            'time': ev.get('time') or '', 'location': ev.get('location') or '', 'organizer': ev.get('organizer') or ''}


@routes.route('/events/add', methods=['GET', 'POST'])
@routes.route('/events/edit/<event_id>', methods=['GET', 'POST'])
@admin_required
def event_form(event_id=None):
    ev = None
    if event_id:
        ev = doc_dict(db.collection('events').document(event_id).get(**fs_opts()))
        if ev is None:
            flash('Событие не найдено — возможно, его уже удалили', 'warning')
            return redirect(url_for('events'))
    values = _event_form_values(ev)
    errors = {}
    if request.method == 'POST':
        if not values['title']:
            errors['title'] = 'Укажите название'
        elif len(values['title']) > 200:
            errors['title'] = 'Не длиннее 200 символов'
        if len(values['description']) > 5000:
            errors['description'] = 'Описание — не длиннее 5000 символов'
        day = None
        try:
            day = datetime.strptime(values['date'], '%Y-%m-%d').date() if values['date'] else None
        except ValueError:
            day = None
        if day is None:
            errors['date'] = 'Укажите дату события'
        if values['time']:
            m = TIME_RE.match(values['time'])
            if not m:
                errors['time'] = 'Время в формате ЧЧ:ММ'
            else:
                values['time'] = '%02d:%s' % (int(m.group(1)), m.group(2))
        for k in ('location', 'organizer'):
            if len(values[k]) > 200:
                errors[k] = 'Не длиннее 200 символов'
        if not errors:
            ref = db.collection('events').document(event_id) if event_id else db.collection('events').document()
            try:
                image_url, uploaded, removed = _single_image('image', (ev or {}).get('imageUrl'), 'events')
            except UploadError as e:
                errors['image'] = str(e)
            else:
                data = {'title': values['title'], 'description': values['description'],
                        # полночь по Алматы: в приложении (toDate() на телефоне в Казахстане) — тот же день
                        'date': datetime(day.year, day.month, day.day, tzinfo=ALMATY),
                        'time': values['time'], 'location': values['location'], 'organizer': values['organizer'],
                        'imageUrl': image_url, 'updatedAt': SERVER_TIMESTAMP}
                try:
                    if event_id:
                        ref.update(data)
                    else:
                        data.update({'createdAt': SERVER_TIMESTAMP, 'registrationsCount': 0,
                                     'createdBy': _admin_uid()})
                        ref.set(data)
                except Exception as e:
                    if uploaded:
                        delete_storage_paths([uploaded])
                    flash_error('Не удалось сохранить событие. Попробуйте ещё раз', e)
                else:
                    if removed:
                        delete_storage_paths(_own_paths([removed], 'events/'))
                    _invalidate('events')
                    audit('update' if event_id else 'create', 'event', ref.id, {'title': values['title']})
                    flash_action('Событие сохранено' if event_id else 'Событие создано — студенты увидят его в приложении',
                                 'Участники', href=url_for('event_registrations', event_id=ref.id))
                    return redirect(back_url('events'))
    return render_template('event_form.html', event=ev, values=values, errors=errors)


@routes.route('/events/delete/<event_id>', methods=['POST'])
@admin_required
def event_delete(event_id):
    try:
        title = _delete_entity('events', event_id)
    except Exception as e:
        flash_error('Не удалось удалить событие', e)
        return _back('events')
    _invalidate('events')
    if title is None:
        flash('Событие уже удалено', 'info')
    else:
        audit('delete', 'event', event_id, {'title': title})
        flash('Событие «%s» удалено вместе с регистрациями' % title, 'success')
    return _back('events')


@routes.route('/events/registrations/<event_id>')
@admin_required
def event_registrations(event_id):
    return _registrations_page('events', event_id, 'event_registrations.html')


@routes.route('/events/registrations/delete/<event_id>/<user_id>', methods=['POST'])
@admin_required
def delete_event_registration(event_id, user_id):
    try:
        removed = _delete_registration('events', event_id, user_id)
    except Exception as e:
        flash_error('Не удалось удалить участника', e)
    else:
        _invalidate('events')
        if removed:
            audit('delete_registration', 'event', event_id, {'user': user_id})
            flash('Участник удалён из списка', 'success')
        else:
            flash('Этот участник уже не зарегистрирован', 'info')
    return redirect(url_for('event_registrations', event_id=event_id))


@routes.route('/events/recount/<event_id>', methods=['POST'])
@admin_required
def event_recount(event_id):
    try:
        n = _recount('events', event_id)
    except Exception as e:
        flash_error('Не удалось пересчитать участников', e)
    else:
        _invalidate('events')
        audit('recount', 'event', event_id, {'count': n})
        flash('Счётчик участников обновлён: %d' % n, 'success')
    return _back('event_registrations', event_id=event_id)


@routes.route('/events/remind/<event_id>', methods=['POST'])
@admin_required
def event_remind(event_id):
    ev = doc_dict(db.collection('events').document(event_id).get(**fs_opts()))
    if ev is None:
        flash('Событие не найдено', 'warning')
        return redirect(url_for('events'))
    message = (request.form.get('message') or '').strip()[:300]
    if not message:
        d = _event_date(ev.get('date'))
        message = 'Напоминаем: %s%s%s' % (ev.get('title') or 'событие', (' — ' + d.strftime('%d.%m')) if d else '',
                                          (' в ' + ev['time']) if ev.get('time') else '')
    try:
        sent, total = _remind('events', event_id, ev.get('title') or 'Событие', message)
    except Exception as e:
        flash_error('Не удалось отправить напоминание', e)
    else:
        audit('remind', 'event', event_id, {'sent': sent, 'participants': total})
        if not total:
            flash('На событие пока никто не записался — напоминать некому', 'info')
        else:
            flash('Напоминание отправлено: %d из %d (у остальных нет приложения с уведомлениями)' % (sent, total)
                  if sent < total else 'Напоминание отправлено всем участникам (%d)' % sent,
                  'success' if sent else 'warning')
    return _back('event_registrations', event_id=event_id)


# ==================== КЛУБЫ ====================

def _clubs_snapshot():
    return _snapshot('clubs', lambda: db.collection('clubs').select(
        ['title', 'name', 'description', 'category', 'meetingTime', 'location', 'curator', 'imageUrl',
         'registrationsCount', 'createdAt']).limit(LIST_LIMIT))


@routes.route('/clubs')
@admin_required
def clubs():
    q = request.args.get('q', '').strip()
    category = request.args.get('category', '')
    items = _clubs_snapshot()
    by_cat = {}
    rows = []
    for c in items:
        by_cat[c.get('category') or ''] = by_cat.get(c.get('category') or '', 0) + 1
        if category and (c.get('category') or '') != category:
            continue
        if q and not _matches(q, c.get('title'), c.get('name'), c.get('description'), c.get('curator'), c.get('location')):
            continue
        rows.append(dict(c, display_title=c.get('title') or c.get('name') or 'Без названия',
                         category_label=_label(CLUB_CATEGORIES, c.get('category'), 'Без категории'),
                         regs=max(0, _as_int(c.get('registrationsCount')))))
    rows.sort(key=lambda c: (-c['regs'], _norm(c['display_title'])))
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    stats = {'total': len(items), 'members': sum(max(0, _as_int(c.get('registrationsCount'))) for c in items),
             'no_cover': sum(1 for c in items if not c.get('imageUrl'))}
    return render_template('clubs.html', page=page, stats=stats, by_cat=by_cat, categories=CLUB_CATEGORIES,
                           category=category, q=q)


def _club_form_values(club=None):
    keys = ('title', 'description', 'category', 'meetingTime', 'location', 'curator')
    if request.method == 'POST':
        return {k: _field(k) for k in keys}
    club = club or {}
    vals = {k: club.get(k) or '' for k in keys}
    vals['title'] = club.get('title') or club.get('name') or ''
    vals['category'] = club.get('category') or 'creative'
    return vals


@routes.route('/clubs/add', methods=['GET', 'POST'])
@routes.route('/clubs/edit/<club_id>', methods=['GET', 'POST'])
@admin_required
def club_form(club_id=None):
    club = None
    if club_id:
        club = doc_dict(db.collection('clubs').document(club_id).get(**fs_opts()))
        if club is None:
            flash('Клуб не найден — возможно, его уже удалили', 'warning')
            return redirect(url_for('clubs'))
    values = _club_form_values(club)
    errors = {}
    if request.method == 'POST':
        if not values['title']:
            errors['title'] = 'Укажите название клуба'
        elif len(values['title']) > 200:
            errors['title'] = 'Не длиннее 200 символов'
        if values['category'] not in dict(CLUB_CATEGORIES):
            errors['category'] = 'Выберите категорию (от неё зависит вкладка в приложении)'
        if len(values['description']) > 5000:
            errors['description'] = 'Описание — не длиннее 5000 символов'
        for k, n in (('meetingTime', 100), ('location', 200), ('curator', 200)):
            if len(values[k]) > n:
                errors[k] = 'Не длиннее %d символов' % n
        if not errors:
            ref = db.collection('clubs').document(club_id) if club_id else db.collection('clubs').document()
            try:
                image_url, uploaded, removed = _single_image('image', (club or {}).get('imageUrl'), 'clubs')
            except UploadError as e:
                errors['image'] = str(e)
            else:
                data = dict(values, imageUrl=image_url, updatedAt=SERVER_TIMESTAMP)
                try:
                    if club_id:
                        ref.update(data)
                    else:
                        data.update({'createdAt': SERVER_TIMESTAMP, 'registrationsCount': 0})
                        ref.set(data)
                except Exception as e:
                    if uploaded:
                        delete_storage_paths([uploaded])
                    flash_error('Не удалось сохранить клуб. Попробуйте ещё раз', e)
                else:
                    if removed:
                        delete_storage_paths(_own_paths([removed], 'clubs/'))
                    _invalidate('clubs')
                    audit('update' if club_id else 'create', 'club', ref.id, {'title': values['title']})
                    flash_action('Клуб сохранён' if club_id else 'Клуб создан — студенты смогут вступить в приложении',
                                 'Участники', href=url_for('club_registrations', club_id=ref.id))
                    return redirect(back_url('clubs'))
    return render_template('club_form.html', club=club, values=values, errors=errors, categories=CLUB_CATEGORIES)


@routes.route('/clubs/delete/<club_id>', methods=['POST'])
@admin_required
def club_delete(club_id):
    try:
        title = _delete_entity('clubs', club_id)
    except Exception as e:
        flash_error('Не удалось удалить клуб', e)
        return _back('clubs')
    _invalidate('clubs')
    if title is None:
        flash('Клуб уже удалён', 'info')
    else:
        audit('delete', 'club', club_id, {'title': title})
        flash('Клуб «%s» удалён вместе с участниками' % title, 'success')
    return _back('clubs')


@routes.route('/clubs/registrations/<club_id>')
@admin_required
def club_registrations(club_id):
    return _registrations_page('clubs', club_id, 'club_registrations.html')


@routes.route('/clubs/registration/delete/<club_id>/<reg_id>', methods=['POST'])
@admin_required
def delete_club_registration(club_id, reg_id):
    try:
        removed = _delete_registration('clubs', club_id, reg_id)
    except Exception as e:
        flash_error('Не удалось удалить участника', e)
    else:
        _invalidate('clubs')
        if removed:
            audit('delete_registration', 'club', club_id, {'user': reg_id})
            flash('Участник удалён из клуба', 'success')
        else:
            flash('Этот участник уже не в клубе', 'info')
    return redirect(url_for('club_registrations', club_id=club_id))


@routes.route('/clubs/recount/<club_id>', methods=['POST'])
@admin_required
def club_recount(club_id):
    try:
        n = _recount('clubs', club_id)
    except Exception as e:
        flash_error('Не удалось пересчитать участников', e)
    else:
        _invalidate('clubs')
        audit('recount', 'club', club_id, {'count': n})
        flash('Счётчик участников обновлён: %d' % n, 'success')
    return _back('club_registrations', club_id=club_id)


@routes.route('/clubs/remind/<club_id>', methods=['POST'])
@admin_required
def club_remind(club_id):
    club = doc_dict(db.collection('clubs').document(club_id).get(**fs_opts()))
    if club is None:
        flash('Клуб не найден', 'warning')
        return redirect(url_for('clubs'))
    title = club.get('title') or club.get('name') or 'Клуб'
    message = (request.form.get('message') or '').strip()[:300]
    if not message:
        message = 'Напоминаем о встрече клуба%s' % ((': ' + club['meetingTime']) if club.get('meetingTime') else '')
    try:
        sent, total = _remind('clubs', club_id, title, message)
    except Exception as e:
        flash_error('Не удалось отправить сообщение', e)
    else:
        audit('remind', 'club', club_id, {'sent': sent, 'participants': total})
        if not total:
            flash('В клубе пока нет участников — писать некому', 'info')
        else:
            flash('Сообщение отправлено: %d из %d участников' % (sent, total), 'success' if sent else 'warning')
    return _back('club_registrations', club_id=club_id)


# ==================== ТОВАРЫ (магазин в тенге, коллекция products) ====================

def parse_variants(raw):
    """variants_json формы → [{filterName, options: [{name, price}]}] как читает shop_screen.dart.

    Пустые фильтры/значения отбрасываются, цена — число (мусор → 0). Невалидный JSON → ValueError."""
    data = json.loads(raw) if (raw or '').strip() else []
    if not isinstance(data, list):
        raise ValueError('variants: ожидался список')
    out = []
    for f in data[:10]:
        if not isinstance(f, dict):
            continue
        name = str(f.get('filterName') or '').strip()[:60]
        if not name:
            continue
        opts = []
        for o in (f.get('options') or [])[:50]:
            if not isinstance(o, dict):
                continue
            oname = str(o.get('name') or o.get('value') or '').strip()[:60]
            if not oname:
                continue
            price = parse_number(o.get('price'))
            opts.append({'name': oname, 'price': price if price is not None and price >= 0 else 0})
        if opts:
            out.append({'filterName': name, 'options': opts})
    return out


def _products_snapshot():
    return _snapshot('products', lambda: db.collection('products').select(
        ['name', 'price', 'url', 'imageUrl', 'variants', 'createdAt', 'updatedAt']).limit(LIST_LIMIT))


@routes.route('/products')
@admin_required
def products():
    q = request.args.get('q', '').strip()
    items = _products_snapshot()
    rows = []
    for p in items:
        if q and not _matches(q, p.get('name')):
            continue
        variants = p.get('variants') if isinstance(p.get('variants'), list) else []
        rows.append(dict(p, price_num=parse_number(p.get('price')) or 0,
                         variant_summary=', '.join('%s: %d' % (v.get('filterName') or '—', len(v.get('options') or []))
                                                   for v in variants if isinstance(v, dict))))
    rows.sort(key=lambda p: to_datetime(p.get('createdAt')) or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    page = paginate_list(rows, request.args.get('page'), PER_PAGE)
    return render_template('products.html', page=page, total=len(items), q=q)


def _product_form_values(product=None):
    if request.method == 'POST':
        return {'name': _field('name'), 'price': _field('price'), 'url': _field('url'),
                'variants_json': request.form.get('variants_json', '[]')}
    p = product or {}
    variants = p.get('variants') if isinstance(p.get('variants'), list) else []
    try:
        variants = parse_variants(json.dumps(variants, default=str))
    except ValueError:
        variants = []
    price = parse_number(p.get('price'))
    return {'name': p.get('name', ''), 'price': '' if price is None else price, 'url': p.get('url') or '',
            'variants_json': json.dumps(variants, ensure_ascii=False)}


@routes.route('/products/add', methods=['GET', 'POST'])
@routes.route('/products/edit/<product_id>', methods=['GET', 'POST'])
@admin_required
def product_form(product_id=None):
    product = None
    if product_id:
        product = doc_dict(db.collection('products').document(product_id).get(**fs_opts()))
        if product is None:
            flash('Товар не найден — возможно, его уже удалили', 'warning')
            return redirect(url_for('products'))
    values = _product_form_values(product)
    errors = {}
    variants = []
    if request.method == 'POST':
        if not values['name']:
            errors['name'] = 'Укажите название'
        elif len(values['name']) > 200:
            errors['name'] = 'Не длиннее 200 символов'
        price = parse_number(values['price'])
        if price is None or price < 0 or price > 10000000:
            errors['price'] = 'Цена — число от 0 до 10 000 000 ₸'
        if values['url'] and not _is_http_url(values['url']):
            errors['url'] = 'Ссылка должна начинаться с http:// или https://'
        try:
            variants = parse_variants(values['variants_json'])
        except (ValueError, TypeError):
            errors['variants'] = 'Не удалось прочитать варианты — обновите страницу и попробуйте ещё раз'
        if not errors:
            ref = db.collection('products').document(product_id) if product_id else db.collection('products').document()
            try:
                image_url, uploaded, removed = _single_image('image', (product or {}).get('imageUrl'), 'products')
            except UploadError as e:
                errors['image'] = str(e)
            else:
                # variants пишем ВСЕГДА (в т.ч. []): иначе у товара оставались старые варианты
                data = {'name': values['name'], 'price': price, 'url': values['url'], 'variants': variants,
                        'imageUrl': image_url, 'updatedAt': SERVER_TIMESTAMP}
                try:
                    if product_id:
                        ref.update(data)
                    else:
                        data['createdAt'] = SERVER_TIMESTAMP
                        ref.set(data)
                except Exception as e:
                    if uploaded:
                        delete_storage_paths([uploaded])
                    flash_error('Не удалось сохранить товар. Попробуйте ещё раз', e)
                else:
                    if removed:
                        delete_storage_paths(_own_paths([removed], 'products/'))
                    _invalidate('products')
                    audit('update' if product_id else 'create', 'product', ref.id,
                          {'name': values['name'], 'price': price, 'variants': len(variants)})
                    flash('Товар сохранён' if product_id else 'Товар добавлен — он уже в магазине приложения', 'success')
                    return redirect(back_url('products'))
    return render_template('product_form.html', product=product, values=values, errors=errors)


@routes.route('/products/delete/<product_id>', methods=['POST'])
@admin_required
def product_delete(product_id):
    ref = db.collection('products').document(product_id)
    try:
        snap = ref.get(**fs_opts())
        if not snap.exists:
            flash('Товар уже удалён', 'info')
            return _back('products')
        data = snap.to_dict() or {}
        ref.delete()
        delete_storage_paths(_own_paths([data.get('imageUrl')], 'products/'))
    except Exception as e:
        flash_error('Не удалось удалить товар', e)
        return _back('products')
    _invalidate('products')
    audit('delete', 'product', product_id, {'name': data.get('name')})
    flash('Товар «%s» удалён' % (data.get('name') or product_id), 'success')
    return _back('products')


# ==================== ОБРАТНАЯ СВЯЗЬ («Линия доверия») ====================

def _feedback_status(f):
    """Фактический статус: ответ из мобильной админки статус не меняет — считаем такие отвеченными."""
    st = f.get('status')
    if st == 'closed':
        return 'closed'
    if f.get('reply'):
        return 'answered'
    return 'answered' if st == 'answered' else 'new'


def _feedback_items():
    """Обращения с автором (кэш 60 с). Cloud Function хранит только последние 50 — список маленький."""
    def load():
        docs = [doc_dict(s) for s in db.collection('feedback').order_by('createdAt', direction=DESC)
                .limit(500).stream(**fs_opts(True))]
        uids = [d.get('userId') for d in docs if d.get('userId') and not d.get('anonymousVisual')]
        users = get_docs('users', uids, ['name', 'group', 'role'])
        for d in docs:
            d['_status'] = _feedback_status(d)
            d['_anon'] = bool(d.get('anonymousVisual'))
            u = {} if d['_anon'] else users.get(d.get('userId') or '', {})
            d['_author'] = 'Аноним' if d['_anon'] else (u.get('name') or ('Удалённый пользователь'
                                                                           if d.get('userId') else 'Без входа'))
            d['_group'] = '' if d['_anon'] else (u.get('group') or '')
            d['_role'] = '' if d['_anon'] else (u.get('role') or '')
            d['_topic'] = (d.get('topic') or d.get('subject') or 'Без темы').strip()
            d['_urgent'] = _feedback_urgency(d)
        return docs
    return cache.get_or_set('content:feedback', load, ttl=60)


def _feedback_urgency(f):
    """Почему обращение срочное (строка для подсказки) или None: «Линия доверия», анонимное, тревожные слова."""
    topic = _norm(f.get('topic') or f.get('subject'))
    if any(t in topic for t in FEEDBACK_URGENT_TOPICS):
        return 'тема «Линия доверия»'
    text = _norm(' '.join(str(f.get(k) or '') for k in ('topic', 'subject', 'message')))
    for w in FEEDBACK_URGENT_WORDS:
        if w in text:
            return 'в тексте: «%s…»' % w
    if f.get('anonymousVisual'):
        return 'анонимное обращение'
    return None


def _wait_label(created, now):
    """«ждёт 3 дн.» / «ждёт 5 ч» / «ждёт меньше часа» и число дней ожидания."""
    dt = to_datetime(created)
    if not dt:
        return '', 0
    sec = max(0, (now - dt).total_seconds())
    days, hours = int(sec // 86400), int(sec // 3600)
    if days >= 1:
        return 'ждёт %d дн.' % days, days
    return ('ждёт %d ч' % hours if hours >= 1 else 'ждёт меньше часа'), 0


def _feedback_sort(rows):
    """Новые — сверху: сначала срочные, внутри — кто дольше ждёт; остальные — свежие сверху."""
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    new = [f for f in rows if f['_status'] == 'new']
    rest = [f for f in rows if f['_status'] != 'new']
    new.sort(key=lambda f: (not f['_urgent'], to_datetime(f.get('createdAt')) or epoch))
    rest.sort(key=lambda f: to_datetime(f.get('createdAt')) or epoch, reverse=True)
    return new + rest


@routes.route('/feedback')
@admin_required
def feedback_list():
    items = _feedback_items()
    q = request.args.get('q', '').strip()
    raw_status = request.args.get('status', '')
    topic = request.args.get('topic', '')
    anon = request.args.get('anon') == '1'
    stats = {k: sum(1 for f in items if f['_status'] == k) for k, _ in FEEDBACK_STATUSES}
    stats.update(total=len(items), anon=sum(1 for f in items if f['_anon']),
                 urgent=sum(1 for f in items if f['_status'] == 'new' and f['_urgent']))
    # Входящие: без статуса в URL и без других фильтров открываются «Новые» (если есть); «Все» — явный status=all.
    if raw_status in dict(FEEDBACK_STATUSES) or raw_status == 'all':
        status = raw_status
    elif not (q or topic or anon) and stats['new']:
        status = 'new'
    else:
        status = 'all'
    rows = [f for f in items
            if (status == 'all' or f['_status'] == status) and (not topic or f['_topic'] == topic)
            and (not anon or f['_anon'])
            and (not q or _matches(q, f.get('message'), f['_topic'], f.get('subject'), f['_author'], f['_group'],
                                   f.get('reply')))]
    if request.args.get('format') == 'csv':
        audit('export', 'feedback', None, {'format': 'csv', 'rows': len(rows)})
        return csv_response('обратная_связь.csv',
                            ['Дата', 'Тема', 'Сообщение', 'Автор', 'Группа', 'Статус', 'Ответ', 'Дата ответа'],
                            [[f.get('createdAt'), f['_topic'], f.get('message') or '', f['_author'], f['_group'],
                              _label(FEEDBACK_STATUSES, f['_status']), f.get('reply') or '', f.get('repliedAt')]
                             for f in rows])
    topics = sorted({f['_topic'] for f in items}, key=_norm)
    page = paginate_list(_feedback_sort(rows), request.args.get('page'), PER_PAGE)
    now = now_utc()
    page.items = [dict(f) for f in page.items]  # элементы общего кэша не меняем
    for f in page.items:
        f['_wait'], f['_wait_days'] = _wait_label(f.get('createdAt'), now) if f['_status'] == 'new' else ('', 0)
    oldest = min((to_datetime(f.get('createdAt')) for f in items if f['_status'] == 'new' and to_datetime(f.get('createdAt'))),
                 default=None)
    return render_template('feedback.html', page=page, stats=stats, topics=topics, statuses=FEEDBACK_STATUSES,
                           status=status, explicit_status=bool(raw_status), topic=topic, anon=anon, q=q,
                           filtered=len(rows), oldest_wait=_wait_label(oldest, now)[0] if oldest else '',
                           reply_templates=FEEDBACK_REPLY_TEMPLATES)


@routes.route('/feedback/reply/<feedback_id>', methods=['POST'])
@admin_required
def feedback_reply(feedback_id):
    reply_text = (request.form.get('reply_text') or '').strip()
    if not reply_text:
        flash('Текст ответа не может быть пустым', 'error')
        return _back('feedback_list')
    if len(reply_text) > 4000:
        flash('Ответ слишком длинный (больше 4000 символов)', 'error')
        return _back('feedback_list')
    ref = db.collection('feedback').document(feedback_id)
    snap = ref.get(**fs_opts())
    if not snap.exists:
        flash('Обращение не найдено — возможно, его уже удалили', 'warning')
        return _back('feedback_list')
    fb = snap.to_dict() or {}
    user_id = fb.get('userId')
    edited = bool(fb.get('reply'))
    batch = db.batch()
    batch.update(ref, {'reply': reply_text, 'repliedAt': SERVER_TIMESTAMP, 'repliedBy': _admin_uid(),
                       'status': 'answered', 'updatedAt': SERVER_TIMESTAMP})
    if user_id:
        # как в мобильной админке (feedback_screen.dart): уведомление type 'answer' со ссылкой на обращение
        batch.set(db.collection('notifications').document(), {
            'userId': user_id, 'type': 'answer', 'title': 'Ответ на ваше обращение', 'message': reply_text,
            'feedbackId': feedback_id, 'topic': fb.get('topic') or 'Обращение', 'createdAt': SERVER_TIMESTAMP,
            'read': False,
        })
    try:
        batch.commit()
    except Exception as e:
        flash_error('Не удалось отправить ответ. Попробуйте ещё раз', e)
        return _back('feedback_list')
    pushed = False
    if user_id:
        try:
            token = (get_docs('users', [user_id], ['fcmToken']).get(user_id) or {}).get('fcmToken')
            if token:
                pushed = send_push('Ответ на ваше обращение', reply_text[:180], token=token,
                                   data={'type': 'answer', 'feedbackId': feedback_id})
        except Exception:
            log.exception('Пуш об ответе на обращение %s не отправлен', feedback_id)
    _invalidate('feedback')
    audit('reply_edit' if edited else 'reply', 'feedback', feedback_id, {'length': len(reply_text), 'push': pushed})
    if not user_id:
        flash('Ответ сохранён. Автор не указан, поэтому уведомление не отправлено', 'warning')
    else:
        flash('Ответ отправлен — пользователь увидит его в приложении', 'success')
    return _back('feedback_list')


@routes.route('/feedback/status/<feedback_id>', methods=['POST'])
@admin_required
def feedback_status(feedback_id):
    status = request.form.get('status')
    if status not in dict(FEEDBACK_STATUSES):
        flash('Неизвестный статус', 'error')
        return _back('feedback_list')
    ref = db.collection('feedback').document(feedback_id)
    try:
        snap = ref.get(**fs_opts())
        if not snap.exists:
            flash('Обращение не найдено', 'warning')
            return _back('feedback_list')
        prev = _feedback_status(snap.to_dict() or {})
        ref.update({'status': status, 'updatedAt': SERVER_TIMESTAMP})
    except Exception as e:
        flash_error('Не удалось изменить статус', e)
        return _back('feedback_list')
    _invalidate('feedback')
    audit('status', 'feedback', feedback_id, {'status': status, 'prev': prev})
    # обратимо — без подтверждения, с «Отменить» в тосте (UX_SPEC_v2 §4.11)
    message = {'closed': 'Обращение закрыто', 'new': 'Обращение снова в работе',
               'answered': 'Обращение снова в работе'}[status]
    flash_action(message, 'Отменить', post=url_for('feedback_status', feedback_id=feedback_id),
                 fields=_undo_fields('feedback_list', {'status': prev}))
    return _back('feedback_list')


@routes.route('/feedback/delete/<feedback_id>', methods=['POST'])
@admin_required
def delete_feedback(feedback_id):
    try:
        db.collection('feedback').document(feedback_id).delete()
    except Exception as e:
        flash_error('Не удалось удалить обращение', e)
        return _back('feedback_list')
    _invalidate('feedback')
    audit('delete', 'feedback', feedback_id)
    flash('Обращение удалено', 'success')
    return _back('feedback_list')


@routes.route('/feedback/bulk', methods=['POST'])
@admin_required
def feedback_bulk():
    """Массово: close (с «Отменить»), delete (с подтверждением в интерфейсе), restore — отмена закрытия:
    поле restore = JSON {id: прежний статус}."""
    action = request.form.get('action')
    restore = {}
    if action == 'restore':
        try:
            raw = json.loads(request.form.get('restore') or '{}')
        except (TypeError, ValueError):
            raw = {}
        if isinstance(raw, dict):
            restore = {str(k): v for k, v in list(raw.items())[:200] if v in dict(FEEDBACK_STATUSES)}
        ids = list(restore)
    else:
        ids = [i for i in request.form.getlist('ids') if i][:200]
    if action not in ('close', 'delete', 'restore') or not ids:
        flash('Отметьте обращения и выберите действие', 'warning')
        return _back('feedback_list')
    coll = db.collection('feedback')
    try:
        found = get_docs('feedback', ids, ['status', 'reply'])
        ids = [i for i in ids if i in found]  # несуществующие id не создаём
        prev = {i: _feedback_status(found[i]) for i in ids}
        for part in chunked(ids, 400):
            batch = db.batch()
            for fid in part:
                if action == 'delete':
                    batch.delete(coll.document(fid))
                else:
                    batch.update(coll.document(fid), {'status': restore[fid] if action == 'restore' else 'closed',
                                                      'updatedAt': SERVER_TIMESTAMP})
            batch.commit()
    except Exception as e:
        flash_error('Не удалось выполнить действие', e)
        return _back('feedback_list')
    _invalidate('feedback')
    audit('bulk_' + action, 'feedback', None, {'ids': ids})
    n = len(ids)
    noun = plural(n, 'обращение', 'обращения', 'обращений')
    restore_json = json.dumps(prev, separators=(',', ':'))
    if action == 'close' and len(restore_json) <= 1500:  # flash живёт в cookie сессии (~4 КБ) — большой отмены не будет
        flash_action('Закрыто %d %s' % (n, noun), 'Отменить', post=url_for('feedback_bulk'),
                     fields=_undo_fields('feedback_list', {'action': 'restore', 'restore': restore_json}))
    elif action == 'close':
        flash('Закрыто %d %s' % (n, noun), 'success')
    elif action == 'restore':
        flash('Вернули в работу %d %s' % (n, noun), 'success')
    else:
        flash('Удалено %d %s' % (n, noun), 'success')
    return _back('feedback_list')


def nav_badges():
    """Бейдж сайдбара: обращения без ответа (тот же снапшот, что и у страницы, кэш 60 с)."""
    return {'feedback_list': sum(1 for f in _feedback_items() if f['_status'] == 'new')}
