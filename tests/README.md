# Тесты и справочник разработчика CMS

Всё, что нужно, чтобы писать модуль веб-админки: как запускать тесты на эмуляторах,
как делать скриншоты, API ядра (`cms/core.py`, `cms/schedule_model.py`), макросы
`templates/_ui.html`, классы `static/css/app.css` и JS `window.CC`.
Живой справочник компонентов — страница **`/ui-kit`** (после входа).

## 1. Запуск тестов (эмуляторы Firebase)

```bash
tests/run_tests.sh PORT_BASE [модули unittest…]
tests/run_tests.sh 18000                              # все тесты (discover)
tests/run_tests.sh 18400 tests.test_applicants        # один модуль
tests/run_tests.sh 18400 tests.test_smoke tests.test_applicants
```

* Поднимает Firestore + Auth + Storage эмуляторы (`firebase emulators:exec`, проект `demo-caspian`)
  на портах `PORT_BASE..PORT_BASE+5` (hub, logging, firestore, firestore-ws, auth, storage; Flask для
  скриншотов — `PORT_BASE+6`). UI эмулятора выключен. Временный `firebase.json` и правила
  «разрешить всё» создаются во временном каталоге и удаляются.
* Прод-ключ **не используется**: при `FIRESTORE_EMULATOR_HOST` `cms.core` работает с анонимными кредами.
  Без эмулятора `tests/base.py` пропускает интеграционные тесты, а `tests/seed.py` отказывается работать.
* Пути к интерпретатору/CLI: `CMS_PYTHON`, `FIREBASE_BIN`, `CMS_CHROMIUM` (или файл `tests/.env.local`,
  он в `.gitignore`; переменные окружения важнее файла).
* Внутри: `CMS_STRICT=1` (ошибка импорта любого модуля роняет тесты), `CMS_SECRET_KEY` тестовый,
  `CMS_INSTANCE_DIR` во временном каталоге, пуши не отправляются (`core.sent_pushes`).
* Порты агентов: FOUNDATION 18000, SCHEDULE 18100, CONTENT 18200, PEOPLE 18300, APPLICANTS 18400,
  COINS 18500, CONFLICTS 18600, финальная проверка 18700.
* Чистые модули тестируются и без эмулятора: `python -m unittest tests.test_applicant_login tests.test_schedule_model`.

### Сид (`tests/seed.py`)

`reset_and_seed()` очищает эмуляторы и пишет реалистичные данные (на русском, id детерминированные):

| что | id / значения |
|---|---|
| админ (Auth + users) | `admin_main`, `admin@caspian.test` / `Admin12345` (`seed.ADMIN`) |
| преподаватель с паролем (не админ) | `t_ivanova`, `ivanova@caspian.test` / `Teacher123` |
| преподаватели (users role=teacher) | `t_ivanova, t_seitkali, t_petrov, t_akhmetova, t_kim, t_nurlanov, t_smirnova, t_zhaksybekov, t_omarova, t_li, t_bekova`; полный тёзка `t_kim2`; легаси `teachers/legacy_t1` |
| группы (`groups_list`) | `ПО 23-1, ПО 24-1, ПО 24-2, ПО 25-1, ИС 23-1, ИС 24-1, ВТ 24-1, ВТ 25-1п, ЭК 24-1, ЭК 25-1, МЕН 25-1` |
| студенты | `st_001…st_024` (+ `club_robotics` role=club) |
| расписание | базовая сетка `base_<группа>_<день>_<слот>` без накладок (схемы: только `day_index`, `day_index+date`, только `date`; время `08.00-08.50` и `08:00-08:50`) + специальные случаи `seed.CONFLICT_CASES` (`cf_*` — накладки, `ok_*` — НЕ накладки, `q_*` — качество данных) |
| абитуриенты | `ap_001…ap_042` (логины-телефоны, логины, один старый email); `ap_001` — Auth `+77011234567` / `secret12`; источники `STAND1, SCHOOL25, OPENDAY, INSTA, FLYER(неактивен), APP, WEB, DOD2026`; `applicant_crm` со статусами и историей; рефералы от `ap_001` |
| коины | `coin_wallets/*`, `coin_transactions` (welcome/quest/campaign/referral/manual/purchase/refund), кампании `DOD2026, QUIZ2026(выкл.), SPRING25(истекла)`, `coin_claims` |
| магазин | `coin_products`: `prod_hoodie, prod_tshirt, prod_mug, prod_stickers, prod_notebook(stock 0), prod_shopper(неактивен)` с фото в Storage; `coin_orders`: `ord_001–003 reserved, ord_004–005 issued, ord_006 cancelled, ord_007 rejected` |
| приёмная | `app_config/admissions` по SPEC §2.8 (каталог, документы, FAQ, дедлайн, квесты) |
| контент | новости `news_*` (с `images[]` в Storage, `news_legacy` — только `imageUrl`), `applicant_news an_0..2`, опросы `poll_canteen, poll_club`, события `ev_*`, клубы `club_*`, старые товары `p_*`, обратная связь `fb_0..4` (3 status=new), администрация `mg_0..3`, `cms_audit`, `schedule_logs`, `schedule_conflict_acks/ack_demo` |

`seed_all()` возвращает dict с этими id (`self.seed` в тестах).

### Как написать тест модуля

```python
# tests/test_coins.py
from tests.base import CmsTestCase


class OrdersTest(CmsTestCase):                 # свежий сид на класс, вход админом в setUp

    def test_list(self):
        html = self.get_ok('/coins/orders')    # 200 + текст ответа
        self.assertIn('Худи Caspian College', html)

    def test_issue(self):
        r = self.post('/coins/orders/ord_001/issue', {'note': 'Выдано на стенде'})   # CSRF подставится сам
        self.assertRedirectsTo(r, '/coins/orders')
        self.assertEqual(self.doc('coin_orders', 'ord_001')['status'], 'issued')
        self.assertTrue(self.audit_entries('issue'))
        self.assertIn('success', [c for c, _ in self.flashes()] or ['success'])

    def test_json_api(self):
        r = self.post_json('/api/coins/x', {'a': 1})                # заголовок X-CSRF-Token
        self.assertEqual(r.status_code, 200)


class PublicTest(CmsTestCase):
    login_as_admin = False                     # анонимный клиент
```

Хелперы `CmsTestCase`: `login(email, password, expect_ok)`, `logout()`, `csrf()`, `post(url, data, **kw)`
(файлы: `{'images': (io.BytesIO(png), 'a.png')}` + `content_type='multipart/form-data'`), `post_json`,
`get_ok(url, status=200)`, `assertRedirectsTo(resp, '/path')`, `flashes()`, `doc(coll, id)`,
`docs(coll, field=value)`, `audit_entries(action)`. PNG для загрузок: `tests.seed._png(w, h, (r, g, b))`,
файл-заглушка: `tests.seed._File(bytes, 'name.png')`. `fresh_seed_each_test = True` — пересев перед каждым тестом.
`tests/test_smoke.py` обходит ВСЕ GET-маршруты без параметров (200/302) — новые страницы попадают туда сами;
для маршрутов с параметрами добавьте пример в `PARAM_SAMPLES` (или попросите FOUNDATION).

## 2. Скриншоты (Playwright)

```bash
tests/run_screens.sh PORT_BASE OUT_DIR /path1 /path2 …
tests/run_screens.sh 18400 $S/shots/applicants /applicants /applicants/leads '!/join/STAND1'
SCREENS_THEME=dark tests/run_screens.sh 18400 $S/shots/applicants/dark /applicants
tests/run_screens.sh 18500 $S/shots/coins "/coins/orders|name:orders_confirm|click:form[data-confirm] button"
```

Эмуляторы + сид + Flask на `PORT_BASE+6`, вход админом через форму. Для каждого пути — `<slug>_1440.png`
и `<slug>_390.png` (полная страница). `!/path` — без входа. Шаги перед снимком (через `|`):
`name:<имя файла>`, `click:<css>`, `type:<css>=<текст>`, `key:Control+k`, `wait:<мс>`, `eval:<js>`,
`hover:<css>`, `files:<css input>=/abs/a.png,/abs/b.png` (тогда снимается только экран). В выводе — статус
ответа и JS-ошибки страницы. CDN (Bootstrap, иконки, шрифты) грузятся через Python requests (TLS проверяется)
и кэшируются в `OUT_DIR/.cdn-cache`. Смотрите PNG (Read) и правьте видимые проблемы.

## 3. API ядра — `cms/core.py` (только чтение для модулей)

```python
from .core import (Routes, admin_required, csrf_exempt, db, bucket, fb_auth, cache, audit, count, aggregate_sum,
                   where, paginate, paginate_list, get_docs, doc_dict, chunked, upload_image, upload_images,
                   images_from_uploader, image_fields, images_of, delete_storage_paths, UploadError,
                   now_almaty, today_almaty, to_almaty, fmt_dt, parse_local_datetime, local_input_value,
                   send_push, sanitize_topic, csv_response, RateLimiter, client_ip, safe_next_url, redirect_back,
                   json_safe, safe_url, flash_error, log, SERVER_TIMESTAMP, Increment, ArrayUnion, DELETE_FIELD)
```

**Маршруты.** В модуле `routes = Routes()`, декоратор `@routes.route(rule, methods=[...])` (+ `.get`, `.post`,
`.template_filter(name)`, `.template_global(name)`), затем `@admin_required`. Имя endpoint = имя функции
(без префикса блюпринта). Необязательно: `setup(app)` и `nav_badges() -> {endpoint: int}` (бейдж в сайдбаре,
кэш 60 с, ошибка не ломает страницу; после изменения данных можно `cache.invalidate('nav:badges')`).

| функция | что делает |
|---|---|
| `admin_required(f)` | нет сессии → `/login?next=…` (для `/api/*` и JSON — 401); роль перечитывается раз в 10 мин; разжалованный → 403 + выход; `g.admin` |
| `current_admin()` | `{'uid','email','name'}` |
| CSRF | все POST/PUT/PATCH/DELETE проверяются автоматически: поле `csrf_token` или заголовок `X-CSRF-Token`. В шаблоне `{{ csrf_token() }}` / `{{ ui.csrf_field() }}`; `app.js` сам добавляет поле в `form[method=post]` и заголовок в `fetch`/XHR. `@csrf_exempt` — только для вебхуков |
| `db`, `bucket`, `fb_auth` | Firestore client, Storage bucket, `firebase_admin.auth` (ленивая инициализация; эмулятор/прод — по env) |
| `where(q, field, op, value)` | `q.where(filter=FieldFilter(...))` без предупреждений |
| `cache` | `get(key, default)`, `set(key, value, ttl=None)`, `get_or_set(key, factory, ttl=None)` (кэширует и `None`/пустое), `invalidate(key=None)` (ключ и `key:*`), `invalidate_prefix('ns:')`. TTL по умолчанию 300 с. Межпроцессная инвалидация по пространству имён (часть ключа до `:`) — другие воркеры PythonAnywhere сбрасывают в течение 1 с. Ключи модулей называйте `<модуль>:<что>` (`applicants:snapshot`) |
| `audit(action, entity, entity_id=None, details=None)` | запись в `cms_audit` (`at, by, byUid, action, entity, entityId, details`); никогда не бросает |
| `count(query, default=None)` / `aggregate_sum(query, field, default=None)` | aggregation count()/sum(); ошибка → лог + default |
| `paginate(query, page_size=25, cursor=None)` | курсорная пагинация (нужен `order_by`), `Page`: `.items` (снапшоты), `.dicts()`, `.next_cursor`, `.has_next`, `.has_prev`, `.number` (из `?p=`). В шаблоне `{{ ui.pagination(page) }}` (ссылки `?cursor=…&p=…`) |
| `paginate_list(items, page, per_page=25)` | нумерованная пагинация списка из кэша → `ListPage` (`.items .page .pages .total`); `?page=` |
| `get_docs(collection, ids, field_paths=None)` | `db.get_all` пачками → `{id: dict}` (вместо N+1) |
| `doc_dict(snapshot)` | `{'id', **data}` или `None` |
| `chunked(seq, 450)` | порции для batch (лимит 500) |
| `upload_image(file, folder, index=0)` | файл → Storage `<folder>/<stamp>_<i><ext>`, проверка сигнатуры (jpg/png/webp/gif) и размера ≤ 10 МБ, `content_type`, `Cache-Control: public, max-age=604800`, токен `firebaseStorageDownloadTokens` → `{'url','path','w','h'}` (URL `firebasestorage.googleapis.com/v0/b/<bucket>/o/<path>?alt=media&token=…`) |
| `upload_images(files, folder, max_count=10)` | несколько, всё-или-ничего |
| `images_from_uploader(name, existing, folder, max_count=10)` | разбор поля `ui.image_uploader` → `UploaderResult(.images, .removed_paths, .uploaded_paths, .changed)`; старые фото принимаются только из `existing` |
| `image_fields(images)` | `{'images': [...], 'imageUrl': обложка|None}` — писать в документ |
| `images_of(doc)` | как `NewsMedia.imagesOf`: `images[]`, иначе `imageUrl` → `[{url, path:'', w:0, h:0}]` |
| `delete_storage_paths(paths)` | удалить по путям/URL нашего бакета (NotFound игнорируется) |
| `UploadError` | понятный текст — можно во flash |
| время | `ALMATY` (UTC+5), `now_almaty()`, `today_almaty()`, `to_datetime(v)`, `to_almaty(v)`, `fmt_dt(v, fmt, default)`, `parse_local_datetime('2026-10-20T09:00')` (ввод по Алматы → UTC), `local_input_value(v)`, `humanize_ago(v)` |
| форматирование | `plural(n, 'голос', 'голоса', 'голосов')`, `fmt_num`, `fmt_pct(part, total)`, `fmt_phone`, `safe_url` (только http/https/mailto/tel/относительные), `json_safe` |
| `send_push(title, body, topic=None, token=None, data=None)` | FCM; топик через `sanitize_topic` (порт `functions/index.js`). В эмуляторе пишет в `core.sent_pushes` |
| `csv_response(filename, header, rows, delimiter=';')` | CSV с BOM для Excel |
| `RateLimiter(limit, window)` | `.hit(key)` → False при превышении, `.blocked(key)`, `.reset(key)`; `client_ip()` |
| `safe_next_url(url)`, `redirect_back(default_endpoint)` | защита от open redirect |
| `flash_error(message, exc)` | flash('…', 'error') + исключение в лог (пользователю `str(e)` не показываем) |
| `stub_page(...)` | временная заглушка «раздел в разработке» (удалите при реализации) |

**Jinja-фильтры:** `dt` (`dd.mm.yyyy HH:MM` по Алматы), `dateonly`, `timeonly`, `ago`, `num`, `phone`,
`safe_url`, `plural(one, few, many)`, `input_dt`, `images_of`. **Глобалы:** `csrf_token()`, `url_with(**changes)`
(текущий URL с изменёнными query-параметрами, `None` — удалить), `nav_url(endpoint, **kw)` (не падает на
неизвестном endpoint), `static_url('css/x.css')` (с `?v=mtime`), `pct(part, total)`, `plural(...)`,
`public_lang()` (`ru`|`kk`), `get_teacher_name(uid)`.

### `cms/schedule_model.py` (только чтение)

| функция | что делает |
|---|---|
| `lesson_weekday(lesson)` | **0=Пн…6=Вс** или None; приоритет `day_index` (int или строка-число 0..6), иначе день из `date` — зеркало `ScheduleService.lessonWeekday` (APP возвращает ISO 1..7 → `lesson_iso_weekday`) |
| `weekday_info(lesson)` | `(день, 'day_index'|'date'|None, day_index≠date)` |
| `day_fields(weekday, today=None)` | **для записи**: `{'day_index': wd, 'date': ближайшая дата}` — как конструктор APP |
| `parse_time_range(raw)` | `'08.30-09.50'`/`'8:30 – 9:50'` → `(510, 590)`, нет конца → `(510, None)`, мусор → `(None, None)` |
| `lesson_interval(lesson, wd=None)` | `(start, end, end_assumed)`; нет конца → конец слота сетки с тем же началом, иначе +50 мин |
| `intervals_overlap(a0, a1, b0, b1)` | строгое пересечение (касание — не накладка) |
| `call_slots(wd)`, `slot_end_for`, `in_call_grid`, `CALL_SCHEDULE`, `DAY_NAMES`, `DAY_NAMES_FULL`, `DAY_SHORT` | сетка звонков (Пн — своя) |
| `is_numerator_week(d=None)`, `current_week_type(d)`, `academic_week_number(d)`, `get_week_parity()` | чётность недели = `isNumeratorWeek` APP, по Алматы (`get_week_parity()` → True = числитель) |
| `norm_week_type(v)` → `(week, unknown)`, `weeks_overlap(a, b)`, `week_occurs(week, d)`, `WEEK_LABELS` | `always` совместим со всеми; числитель × знаменатель — нет |
| `room_key(raw)`, `is_virtual_room(raw)` | ключ аудитории: «ауд.»/«каб.»/«№», регистр, пробелы/дефисы, латиница-двойник; пусто/«онлайн»/«-» → None |
| `group_key(raw)`, `norm_name(s)`, `norm_text(s)`, `looks_like_name(s)` | нормализация |
| `build_teacher_index()`, `resolve_teacher(raw, index)` | `{'key': 'uid:…'|'name:…'|None, 'uid', 'name', 'flags'}`; flags: `no_teacher, by_name, ambiguous, legacy, orphan, unknown_name` |
| `normalize_lesson(id, data, index)` | нормализованный dict занятия (ключи см. docstring: `weekday, start, end, time_ok, week, teacher_key, room_key, group_key, kind, day_mismatch, in_grid, raw` …) |
| `load_lessons_snapshot(force=False)` | **все занятия одним чтением**, кэш 120 с: `.lessons`, `.by_id`, `.by_group`, `.by_teacher`, `.by_room`, `.by_day`, `.groups()`, `.rooms()`, `.teachers()`, `.teacher_index` |
| `invalidate_schedule_caches()` | вызывать после **любой** записи в `schedules` (снапшот, группы, аудитории, `conflicts:*`, бейджи) |
| `invalidate_directory_caches()` | после правок преподавателей/дисциплин/групп |
| `get_all_groups()`, `get_all_teachers()`, `get_all_subjects()`, `get_all_rooms()`, `get_teacher_name()`, `group_sort_key`, `room_sort_key`, `lesson_sort_key` | справочники с кэшем (как раньше) |

`cms/applicant_login.py`: `to_auth_email`, `login_kind`, `canonical_login`, `display_login`, `is_synthetic_email`,
`normalize_phone`, `password_error(password, repeat=None, allow_reset_password=False)`, `DOMAIN`, `RESET_PASSWORD='abiturient123'`.
`cms/conflicts.py` (заглушка до CONFLICTS): `summary()` → `None|{'critical','warning','teachers'}`, `check_lesson(lesson, exclude_id=None)` → `[]`.

## 4. Шаблоны: `base.html`, `_ui.html`, публичный layout

```jinja
{% extends 'base.html' %}
{% import '_ui.html' as ui with context %}
{% block title %}Лиды{% endblock %}                       {# <title> #}
{% block styles %}{{ ui.css('applicants') }}{% endblock %} {# static/css/applicants.css #}
{% block content %} … {% endblock %}
{% block scripts %}{{ ui.lib('chart') }}{{ ui.js('applicants') }}{% endblock %}
```

Блоки `base.html`: `title`, `page_title` (заголовок в топбаре; по умолчанию — пункт сайдбара),
`breadcrumbs`, `page_actions` (кнопки в топбаре), `content`, `styles`, `head` (старый), `scripts`,
`body_class`. Активный пункт сайдбара определяется сам (endpoint или самый длинный префикс URL).
Контейнер `.container` в контенте не нужен (у старых шаблонов он обнуляется).
Публичные страницы: `{% extends 'public/base_public.html' %}` (блоки `title, styles, head, body_class, body,
header, brand_href, brand_title, brand_subtitle, lang_switch, content, footer, scripts`), язык `public_lang()`
(`?lang=kk` запоминается в cookie), шрифт полей 16px, карточка `.cc-public__card`, заголовок `.cc-public__hero`.

Макросы `_ui.html` (все примеры — на `/ui-kit`, исходник `templates/_ui_kit.html`):

```jinja
{{ ui.page_header('Лиды', 'Абитуриенты и статусы CRM', [{'label': 'Экспорт CSV', 'href': url_with(format='csv'), 'icon': 'download', 'variant': 'light'}], icon='person-lines-fill') }}
{% call ui.page_header('Магазин') %}{{ ui.button('Добавить товар', href=url_for('coins_shop'), icon='plus-lg') }}{% endcall %}
{{ ui.button('Сохранить', type='submit', icon='check2', attrs={'data-loading': ''}) }}   {# variant: primary|light|outline-primary|ghost|danger|outline-danger; size='sm'; icon_only=True #}
<div class="cc-stats">{{ ui.stat_card('За 7 дней', 42, 'person-plus', trend=12.5, href=url_for('applicants_leads'), meta='к прошлой неделе', tone='success') }}</div>
{% call ui.card('Регистрации по дням', icon='graph-up', subtitle='30 дней', actions=[{'label': '90 дней', 'href': url_with(days=90)}]) %}<canvas id="chart"></canvas>{% endcall %}
{{ ui.empty_state('inbox', 'Заказов нет', 'Подсказка…', {'label': 'Добавить', 'href': '#', 'icon': 'plus-lg'}) }}
{{ ui.badge('reserved') }} {{ ui.badge(lead.status) }} {{ ui.badge('x', 'Своя подпись', 'success') }} {{ ui.week_badge(lesson.week_type, short=True) }}
{{ ui.tabs([{'key': 'rooms', 'label': 'Аудиторный фонд', 'href': url_for('schedule_rooms'), 'icon': 'door-open'}, {'key': 'conflicts', 'label': 'Накладки', 'href': url_for('schedule_conflicts'), 'count': 3}], 'conflicts') }}   {# pills=True — сегменты #}
{% call ui.filters_bar() %}{{ ui.filter_search('q', 'Поиск', request.args.get('q')) }}{{ ui.filter_select('status', 'Статус', [('new', 'Новый')], request.args.get('status')) }}{{ ui.filter_input('from', 'С даты', request.args.get('from'), 'date') }}{% endcall %}
{% call ui.data_table('leadsTable', search=True, placeholder='Поиск…', title='Лиды', actions=[...]) %}<table class="cc-table" id="leadsTable" data-sortable>…</table>{% endcall %}
{{ ui.pagination(page) }}                                          {# Page (курсор) или ListPage (номера) #}
{{ ui.confirm_button(url_for('coins_order_cancel', order_id=o.id), 'Отменить', 'Отменить заказ и вернуть коины?', icon='x-lg', ok='Отменить заказ') }}
{{ ui.field('title', 'Название', value=p.title, required=True, attrs={'maxlength': 120}) }}   {# type: text|email|number|date|datetime-local|select|textarea|checkbox|switch; options=[(v, label)]; help; error #}
<form method="post" enctype="multipart/form-data">{{ ui.csrf_field() }}{{ ui.image_uploader('images', product, max=10, label='Фото') }}…</form>
{{ ui.kv([('Логин', lead.login), ('Телефон', lead.phone|phone)]) }} {{ ui.avatar(name, size='sm') }} {{ ui.progress_bar(37, 100, 'success') }} {{ ui.section_title('Источники') }}
{{ ui.lib('chart') }} {{ ui.lib('qrcode') }} {{ ui.lib('sortable') }}   {# Chart.js 4.4.1, qrcode-generator 1.4.4, SortableJS 1.15.2 (SRI) #}
{{ ui.json_data('leadsData', rows) }}                               {# данные для JS без XSS #}
```

Сервер для `image_uploader`:
```python
res = images_from_uploader('images', images_of(doc), 'coin_products/%s' % pid)   # UploadError → flash
try:
    ref.update({**image_fields(res.images), 'updatedAt': SERVER_TIMESTAMP})
except Exception:
    delete_storage_paths(res.uploaded_paths); raise
delete_storage_paths(res.removed_paths)
```

## 5. CSS (`static/css/app.css`) и JS (`static/js/app.js`)

Токены (не хардкодить цвета; в тёмной теме переопределены): `--cc-brand #FF6200` (акценты, графики),
`--cc-brand-strong #C24B00` (кнопки, мелкий текст/ссылки — контраст AA), `--cc-brand-deep`, `--cc-brand-light`,
`--cc-brand-soft`, `--cc-brand-tint`, `--cc-success|warning|danger|info|violet|teal` (+ `-soft`, `-fg`),
`--cc-bg`, `--cc-surface`, `--cc-surface-2/3`, `--cc-text`, `--cc-text-muted`, `--cc-text-subtle`, `--cc-border`,
`--cc-border-strong`, `--cc-week-num-bg/fg`, `--cc-week-den-bg/fg`, `--cc-week-all-bg/fg`, радиусы
`--cc-radius-xs|sm|md|lg|xl|pill` (6/10/14/18/24), отступы `--cc-space-1..7` (4…48), `--cc-shadow-1/2`,
`--cc-dur-fast|med` (160/260 мс), `--cc-ease`, `--cc-font` (Montserrat), `--cc-gradient-brand`.
Bootstrap 5.3.3 перекрашен через `--bs-*` (классы Bootstrap можно использовать как есть).

Классы: `.cc-page-header`, `.cc-card` (`__head/__title/__sub/__body/__body--flush/__foot`), `.cc-grid .cc-grid-2|3|4`,
`.cc-stack`, `.cc-section`, `.cc-section-title`, `.cc-stats` + `.cc-stat` (`--success|warning|danger|info|violet`),
`.cc-trend--up|down|flat`, `.cc-badge--brand|success|warning|danger|info|violet|neutral|outline|week-num|week-den|week-all`,
`.cc-chip(.active)`, `.cc-avatar(--sm|--lg)`, `.cc-tabs(--pills) .cc-tab(.active)`, `.cc-filters`, `.cc-search`,
`.cc-table-wrap`, `.cc-table(--compact)` (`.cc-cell-main/.cc-cell-sub/.cc-actions/.cc-thumb/.cc-table-empty`),
`.cc-empty`, `.cc-skeleton`, `.cc-kv`, `.cc-timeline`, `.cc-bar`, `.cc-uploader`, `.cc-week-pill--numerator|denominator`,
`.cc-public__*`, `.cc-error`, утилиты `.cc-muted .cc-subtle .cc-num .cc-truncate .cc-nowrap .cc-no-print .min-w-0`.
Модульные стили — `static/css/<модуль>.css` (только токены `--cc-*`, префикс классов модуля, например `.ap-`).

`window.CC`: `CC.toast(text, 'success'|'error'|'warning'|'info')`, `CC.confirm(text, {title, okText, danger}) → Promise<bool>`,
`CC.fetchJSON(url, {method, body}) → Promise` (JSON + CSRF), `CC.copy(text)`, `CC.qrSvg(text, {cellSize, margin, ecc})`,
`CC.svgToPng(svg, size) → Promise<Blob>`, `CC.download(filename, blob|string|url, type)`, `CC.chartColors()`,
`CC.applyChartDefaults()`, `CC.cssVar('--cc-brand')`, `CC.setTheme('dark')`, `CC.store.get/set` (localStorage в try/catch),
`CC.debounce`, `CC.initUploader(el)`, `CC.openPalette()`. Атрибуты: `form[data-confirm="…"]` / `button[data-confirm]`
(+ `data-confirm-title`, `data-confirm-ok`, `data-confirm-variant="primary"`), `input[data-table-search="#id"]`,
`table[data-sortable] th[data-sort="text|num"]` (значение — `data-value` ячейки), `tr[data-href]`, `[data-copy="…"]`,
`select[data-autosubmit]`, `[data-password-toggle="#id"]`, `form[data-dirty-guard]`, `button[data-loading]`.
Событие `cc:theme` (смена темы — перерисуйте графики).
