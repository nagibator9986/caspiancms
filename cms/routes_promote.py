# cms/routes_promote.py — «Перевод на следующий курс» (владелец — PEOPLE): раз в год переводит студентов на +1 курс.
#
# Курс студента хранится в users.course строкой «1»–«4»; курс группы считается по году набора в её названии
# (routes_people._group_course: «ПО 25-1» в 2026/27 учебном году → 2 курс). Каждый сентябрь поле users.course
# устаревает — этот раздел его обновляет.
#
# Правила перевода (работает только то, что однозначно; всё сомнительное показывается и НЕ меняется):
#   * группа набора текущего года (первый курс) — не трогаем;
#   * курс по году набора группы > 4 (выпуск) — не трогаем;
#   * сохранённый курс уже равен курсу группы — не трогаем (уже переведён: повторный запуск ничего не сломает);
#   * сохранённый курс + 1 == курс группы — ПЕРЕВОДИМ (+1);
#   * иначе (отстаёт на 2 и больше, опережает группу, курс не задан, группа не распознана) — в список «проверить
#     вручную», не меняется.
#
# Безопасность: предпросмотр всегда по свежим данным (не из кэша); применение пересчитывает план заново и
# отказывается, если число переводимых не совпало с тем, что видел админ; переведённым ставится метка
# coursePromoted = <год набора первого курса> — по ней работает откат («Отменить перевод»: курс −1 и метка снимается).
# Резервная копия — CSV предпросмотра (кто, группа, было → стало); в журнал аудита пишутся итоги по группам.

import re

from flask import flash, render_template, request, url_for

from .core import (DELETE_FIELD, SERVER_TIMESTAMP, Routes, admin_required, audit, chunked, csv_response, db,
                   flash_error, fs_opts, log, today_almaty, where)
from .routes_people import _back, _invalidate_people

routes = Routes()

_YEAR_RE = re.compile(r'(\d{2,4})\s*-\s*\d')
MAX_COURSE = 4
MARK_FIELD = 'coursePromoted'
REVIEW_REASONS = {
    'behind': 'курс отстаёт от группы больше чем на 1',
    'ahead': 'курс опережает группу',
    'no_course': 'курс не задан',
    'no_group': 'группа не указана или не распознана',
}


def start_year_now():
    """Год набора нынешнего первого курса: с сентября — текущий год, до сентября — прошлый."""
    today = today_almaty()
    return today.year if today.month >= 9 else today.year - 1


def group_year(name):
    """Год набора из названия группы («ПО 25-1» → 2025) или None."""
    m = _YEAR_RE.search(str(name or ''))
    if not m:
        return None
    year = int(m.group(1))
    return year + 2000 if year < 100 else year


def _course_int(raw):
    s = str(raw or '').strip()
    return int(s) if s.isdigit() and 1 <= int(s) <= 9 else None


def promotion_plan(students, start_year):
    """План перевода. students — [{'id','name','group','course','promoted'}], start_year — год набора 1-го курса.

    → {'moves': [{id, name, group, old, new}], 'review': [{id, name, group, course, expected, reason}],
       'counts': {'total','move','up_to_date','first_year','graduated','review'}}."""
    moves, review = [], []
    counts = {'total': 0, 'move': 0, 'up_to_date': 0, 'first_year': 0, 'graduated': 0, 'review': 0}
    for s in students:
        counts['total'] += 1
        group = str(s.get('group') or '').strip()
        year = group_year(group)
        base = {'id': s['id'], 'name': s.get('name') or '', 'group': group}
        if year is None:
            review.append(dict(base, course=str(s.get('course') or ''), expected=None, reason='no_group'))
            continue
        if year >= start_year:
            counts['first_year'] += 1
            continue
        expected = start_year - year + 1
        if expected > MAX_COURSE:
            counts['graduated'] += 1
            continue
        course = _course_int(s.get('course'))
        if str(s.get('promoted') or '') == str(start_year) or course == expected:
            counts['up_to_date'] += 1
            continue
        if course is None:
            review.append(dict(base, course='', expected=expected, reason='no_course'))
        elif course + 1 == expected:
            moves.append(dict(base, old=course, new=expected))
        else:
            review.append(dict(base, course=str(course), expected=expected,
                               reason='ahead' if course > expected else 'behind'))
    counts['move'] = len(moves)
    counts['review'] = len(review)
    return {'moves': moves, 'review': review, 'counts': counts}


def _load_students():
    """Свежее чтение студентов (минуя кэш раздела): перед записью в базу данные не должны быть старыми."""
    rows = []
    for snap in where(db.collection('users'), 'role', '==', 'student') \
            .select(['name', 'group', 'course', MARK_FIELD]).stream(**fs_opts(True)):
        d = snap.to_dict() or {}
        rows.append({'id': snap.id, 'name': str(d.get('name') or '').strip(), 'group': str(d.get('group') or '').strip(),
                     'course': d.get('course'), 'promoted': d.get(MARK_FIELD)})
    return rows


def _by_group(moves):
    """[{group, old, new, count}] для таблицы предпросмотра: по группе и паре «было → стало»."""
    acc = {}
    for m in moves:
        key = (m['group'], m['old'], m['new'])
        acc[key] = acc.get(key, 0) + 1
    from .schedule_model import group_sort_key
    return [{'group': g, 'old': o, 'new': n, 'count': c}
            for (g, o, n), c in sorted(acc.items(), key=lambda kv: (group_sort_key(kv[0][0]), kv[0][0]))]


@routes.route('/promote-courses')
@admin_required
def promote_courses():
    """Предпросмотр перевода на следующий курс (?format=csv — список переводимых: резервная копия)."""
    start = start_year_now()
    try:
        students = _load_students()
        plan = promotion_plan(students, start)
        promoted_back = sum(1 for s in students if str(s.get('promoted') or '') == str(start))
    except Exception as e:
        flash_error('Не удалось загрузить студентов', e)
        return _back('admin_users')
    if request.args.get('format') == 'csv':
        audit('export', 'user', None, {'format': 'csv', 'rows': len(plan['moves']), 'promote_preview': True})
        rows = [[m['name'], m['group'], m['old'], m['new'], m['id']] for m in plan['moves']]
        return csv_response('перевод_на_следующий_курс_%d.csv' % start,
                            ['Имя', 'Группа', 'Курс сейчас', 'Курс после', 'uid'], rows)
    return render_template('promote_courses.html', plan=plan, start=start, groups=_by_group(plan['moves']),
                           promoted_back=promoted_back, reasons=REVIEW_REASONS, max_course=MAX_COURSE)


@routes.route('/promote-courses/apply', methods=['POST'])
@admin_required
def promote_courses_apply():
    """Применить перевод. Пересчитывает план по свежим данным; если число переводимых не совпало с показанным
    админу — отказ (данные изменились, нужно посмотреть предпросмотр заново)."""
    start = start_year_now()
    try:
        expected = int(request.form.get('expected') or -1)
        shown_year = int(request.form.get('year') or -1)
    except ValueError:
        expected, shown_year = -1, -1
    try:
        plan = promotion_plan(_load_students(), start)
    except Exception as e:
        flash_error('Не удалось загрузить студентов', e)
        return _back('promote_courses')
    moves = plan['moves']
    if shown_year != start or expected != len(moves):
        flash('Данные изменились с момента предпросмотра (было %s, сейчас %d) — проверьте список и подтвердите заново'
              % (expected if expected >= 0 else '—', len(moves)), 'warning')
        return _back('promote_courses')
    if not moves:
        flash('Переводить некого: все студенты уже на своих курсах', 'info')
        return _back('promote_courses')
    done = 0
    try:
        for part in chunked(moves, 450):
            batch = db.batch()
            for m in part:
                batch.update(db.collection('users').document(m['id']),
                             {'course': str(m['new']), MARK_FIELD: str(start), 'updatedAt': SERVER_TIMESTAMP})
            batch.commit()
            done += len(part)
    except Exception as e:
        log.exception('Перевод на следующий курс прерван после %d из %d', done, len(moves))
        _invalidate_people(['student'])
        flash_error('Перевод прерван: переведено %d из %d. Повторите — уже переведённые не изменятся' % (done, len(moves)), e)
        return _back('promote_courses')
    _invalidate_people(['student'])
    groups = {}
    for m in moves:
        key = '%s: %d→%d' % (m['group'], m['old'], m['new'])
        groups[key] = groups.get(key, 0) + 1
    audit('promote', 'user', None, {'year': start, 'moved': done, 'groups': groups})
    flash('Переведено студентов на следующий курс: %d' % done, 'success')
    return _back('promote_courses')


@routes.route('/promote-courses/rollback', methods=['POST'])
@admin_required
def promote_courses_rollback():
    """Отменить перевод этого года: у студентов с меткой coursePromoted == год набора — курс −1, метка снимается."""
    start = start_year_now()
    try:
        ids = [s for s in _load_students() if str(s.get('promoted') or '') == str(start)]
    except Exception as e:
        flash_error('Не удалось загрузить студентов', e)
        return _back('promote_courses')
    back = [s for s in ids if (_course_int(s.get('course')) or 0) > 1]
    if not back:
        flash('Нечего отменять: переведённых в этом году студентов нет', 'info')
        return _back('promote_courses')
    done = 0
    try:
        for part in chunked(back, 450):
            batch = db.batch()
            for s in part:
                batch.update(db.collection('users').document(s['id']),
                             {'course': str(_course_int(s.get('course')) - 1), MARK_FIELD: DELETE_FIELD,
                              'updatedAt': SERVER_TIMESTAMP})
            batch.commit()
            done += len(part)
    except Exception as e:
        log.exception('Откат перевода прерван после %d из %d', done, len(back))
        _invalidate_people(['student'])
        flash_error('Откат прерван: возвращено %d из %d. Повторите' % (done, len(back)), e)
        return _back('promote_courses')
    _invalidate_people(['student'])
    audit('promote_rollback', 'user', None, {'year': start, 'restored': done})
    flash('Перевод отменён: возвращено студентов — %d' % done, 'success')
    return _back('promote_courses')
