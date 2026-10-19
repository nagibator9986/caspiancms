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
`public_lang()` (`ru`|`kk`), `get_teacher_name(uid)`, а также UI-хелперы v2 (`here`, `back_url`, `next_target`,
`view_param`, `pop_toasts`, `filter_state`, `set_nav_active`, фильтр `time_range`) — см. §4.

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

## 4. Шаблоны: `base.html`, `_ui.html`, публичный layout (дизайн-система v2)

Живой справочник всех компонентов — **`/ui-kit`** (исходник `templates/_ui_kit.html`, копируйте разметку оттуда).
Контракт v2 (`UX_SPEC_v2 §4.15`) опубликован: в начале `templates/_ui.html` стоит `{# CONTRACT v2 READY #}`. Сигнатуры
макросов не меняются — только добавляются необязательные параметры в конец.

```jinja
{% extends 'base.html' %}
{% import '_ui.html' as ui with context %}
{% block title %}Лиды{% endblock %}                       {# <title> и последняя крошка дочерней страницы #}
{% block styles %}{{ ui.css('applicants') }}{% endblock %} {# static/css/applicants.css #}
{% block content %} … {% endblock %}
{% block peek %} … {% endblock %}                          {# (необяз.) компактная версия для быстрого просмотра #}
{% block scripts %}{{ ui.lib('chart') }}{{ ui.js('applicants') }}{% endblock %}
```

**Каркас.** Блоки `base.html`: `title`, `page_title` (последняя крошка), `breadcrumbs`, `crumbs_extra` (доп. уровень:
`<li><a href="…">ПО 24-1</a></li>`), `page_actions` (кнопки в топбаре), `content`, `peek`, `styles`, `head`, `scripts`,
`body_class`. Топбар: на ≥ 992 px — крошки «Раздел › Пункт (ссылка) › Текущая страница», на телефоне — раздел или
«‹ Список» на дочерней странице (заголовок страницы — только h1 в контенте). Справа — «+ Создать», «Поиск Ctrl K», тема.
Под топбаром — полоса прогресса `#ccProgress` (появляется через 300 мс после перехода/отправки формы).

**Сайдбар** (`cms/core.py`, правит только FOUNDATION): `NAV_SECTIONS` (порядок = приоритет: Главная → Расписание →
Контент → Люди → Сервис → Абитуриенты → Caspian Coin), `NAV_COLLAPSED` (свёрнуты по умолчанию: Абитуриенты, Caspian
Coin; раскрываются сами, если в них текущая страница; выбор запоминается в `localStorage['cc-nav']`), `NAV_EXTRA`
(только для Ctrl+K: конструктор, свободные аудитории, команды «Новая новость»…, кампании и журнал коинов),
`NAV_PARENT` (дочерняя страница → пункт сайдбара: подсветка и крошки), `NAV_BADGE_TONES` (`danger` — Накладки,
`brand` — Обратная связь, остальные бейджи приглушённые). **Новая дочерняя страница** — добавьте её в `NAV_PARENT`
(запрос FOUNDATION) или вызовите во view `set_nav_active('applicants_leads')`.

**Быстрый просмотр.** Любая страница открывается в правой панели: `<a href="…" data-cc-peek>` / `ui.peek_link(href,
label)` / `CC.peek(url)`. Запрос идёт с заголовком `X-CC-Partial: 1` (в Python — `is_partial()`, в шаблоне —
`is_partial`), `base.html` отдаёт только `styles` + `{% block peek %}` (по умолчанию = `content`) без каркаса и
**не забирает** flash-сообщения. Скрипты из вставленного HTML не выполняются: после вставки `app.js` шлёт
`cc:content-loaded` (`detail.root`) и сам переинициализирует общие виджеты; модульный JS слушает это событие.
Формы в панели отправляются обычным POST (с `next` = URL списка). Проверка в тесте:
`self.client.get(url, headers={'X-CC-Partial': '1'})` — нет `cc-sidebar`, есть форма.

**Хелперы Python (`from .core import …`) и глобалы Jinja:**

| имя | что делает |
|---|---|
| `here()` | Jinja-глобал: текущий путь с query (`request.full_path` без `?`). Работает и как `{{ here }}` |
| `back_url(default_endpoint, **values)` | цель redirect после сохранения: безопасный `next` (форма/query), иначе `url_for(...)`. `return redirect(back_url('news'))`; в шаблоне — `back={'href': back_url('news'), 'label': 'Новости'}` |
| `next_target()` | безопасный `next` или `None` (его выводит `ui.next_field()`) |
| `flash_action(message, label, href=None, post=None, fields=None, category='success')` | тост с действием: обычный flash `(category, message)` (старые проверки `('success', '…')` работают) + служебный `('cc-action', json)`. `post` — маленькая POST-форма с CSRF (кнопка «Отменить»), `href` — ссылка («Открыть»). Небезопасный URL — действие не показывается |
| `pop_toasts()` | Jinja-глобал для `_toasts.html`: склеивает пары flash → `[{category, message, action}]` |
| `view_param(key, allowed, default)` | вид страницы: `?view=` → cookie `cc_view_<key>` (ставит `ui.segmented(remember=key)`) → `default` |
| `set_nav_active(endpoint)` | подсветить пункт сайдбара (страница не из `NAV_PARENT`) |
| `is_partial()` | запрос быстрого просмотра (`X-CC-Partial: 1`) |
| `filter_state(ignore, search_name)` | для `filters_bar`: число активных фильтров, URL «Сбросить», скрытые поля вкладок |
| фильтр `time_range` | `'13.00-13.50'`, `'8:30 – 9:50'` → `'13:00–13:50'`, `'08:30–09:50'`; одно время → `'09:20'`; мусор — как есть |

**Сохранение контекста списка.** Источник правды — URL. Ссылки на правку несут `next=here()`:
`url_for('news_form', news_id=n.id, next=here())`; формы кладут `{{ ui.next_field() }}`; после сохранения —
`redirect(back_url('news'))`. `app.js` помнит последний URL каждого списка в `sessionStorage['cc-last:<путь>']` и
подставляет его в ссылки `[data-cc-back]` без query (кнопка «‹ Назад» в шапке, крошка-пункт, «Отмена» в savebar).

### Макросы `_ui.html`

| макрос | сигнатура / назначение |
|---|---|
| `page_header` | `(title, subtitle=None, actions=None, icon=None, meta=None, back=None)` + caller. Кнопки `actions` по умолчанию **light**; основную (одну!) помечайте `'variant': 'primary'` (и `'attrs': {'data-cc-hotkey': 'n'}`). `{'menu': [...]}` в `actions` → ⋯. `icon` не рисуется (совместимость). `meta` — разметка рядом с h1 (`ui.week_now(...)`, бейдж). `back` — `{'href', 'label'}` → «‹ Новости» над h1 (на телефоне скрывается, если в топбаре та же ссылка «‹ Новости» — её адрес с фильтрами переходит в топбар). Подзаголовок — одна строка, на телефоне скрыт; на телефоне у второстепенных кнопок видна только иконка |
| `header_menu` | `(items, label='Ещё действия')` — ⋯ для редких действий. item: `{label, href, icon, attrs}` \| `{label, action, fields, confirm, ok, danger, icon}` (POST с CSRF) \| `{divider: True}` \| `{header: '…'}`. Опасные — последними, после разделителя |
| `summary_line` | `(items)` — `[{value, label, href, tone, title}]` или `[(value, label)]` → «5 новостей · 4 за 30 дней». Вместо рядов KPI на всех страницах, кроме Обзора, Аналитики приёма и Накладок |
| `stat_card` / `stat_strip` | `(label, value, icon=None, trend=None, href=None, meta=None, tone=None)` / `(items)` — KPI; иконка нейтральная, `tone` (`danger`/`warning`/`success`/`info`) — только состояние: цвет иконки и полоса слева. На телефоне ряд прокручивается |
| `list_card` | `(summary=None, title=None, actions=None, id=None, classes='')` + caller — карточка списка: фильтры, таблица, пагинация |
| `filters_bar` | `(action=None, reset=None, submit='Применить', search=None, inline=False, ignore=(), sheet_title='Фильтры', id=None)` + caller. Новый вид: `search={'name','value','placeholder','label','filter'}` всегда виден (Enter или 600 мс после ввода; с `filter='#css'` — мгновенно на клиенте по `[data-search]`/строкам), остальные поля — пилюли «Курс: Все ▾» (сразу отправляются), на < 768 px — в шторке снизу (кнопки «Сбросить»/«Показать»). `ignore` — параметры вкладок/чипов: не считаются фильтрами, сохраняются скрытыми полями и в «Сбросить». «Сбросить · N» видна только при фильтрах. Старый вызов без `search`/`inline` работает (поля с подписями) |
| `filter_search` / `filter_select` / `filter_input` | прежние сигнатуры; в новой панели — пилюли |
| `filter_switch` | `(name, label, value=False)` — булев фильтр (`value=1`), отправляется сразу |
| `filter_chips` | `(param, items, active, all_label='Все', all_count=None)` — items `[(value, label[, count])]`, ссылки `url_with(param=…, page=None)` |
| `tabs` | `(items, active, pills=False, label='Вкладки')` — подстраницы; активная видна на телефоне (автопрокрутка + маска) |
| `segmented` | `(items, active, label='Вид', remember=None)` — вид/период; items — как у `tabs` или `(key, label, href[, icon])`; `remember='key'` → cookie для `view_param` |
| `row_menu` | `(items, label='Действия', quick=None)` — ⋯ строки (popper `fixed`: не обрезается прокруткой) + одна быстрая иконка `quick` (ссылка или `{action, fields, confirm}`) |
| `select_all` / `select_row` | `(form_id, label=…)` / `(form_id, value, label)` — чекбоксы `name="ids" form="<id>"` |
| `bulkbar` | `(form_id, noun=('элемент','элемента','элементов'))` + caller (кнопки с `form=` и `name="action"`). Появляется при выборе ≥ 1, «N выбрано», «Снять выделение», Esc, Shift-диапазон; в `data-confirm` кнопок `{n}` → число, `{items}` → «3 новости» |
| `savebar` | `(submit='Сохранить', cancel_href=None, form=None, danger=None, note=None, submit_attrs={})` + caller — закреплённая панель сохранения длинной формы: «Есть несохранённые изменения», Ctrl+S. `danger={'label','action','confirm','icon'}` — кнопка с `formaction` (без вложенных форм) |
| `modal_form` | `(id, title, action, submit='Сохранить', variant='primary', enctype=None, open=False)` + caller (тело). Открывать: `data-bs-toggle="modal" data-bs-target="#id"`; `open=True` — открыть при загрузке (после ошибки) |
| `form_errors` | `(errors)` — сводка над формой: `{'поле': 'текст' или [...]}` со ссылками `#f-<поле>` (id из `ui.field`) |
| `next_field` | `()` — `<input type="hidden" name="next">` из безопасного `?next=` |
| `hint` | `(text=None, icon='info-circle', tone=None, dismiss=None)` + caller — строка-подсказка; `dismiss='key'` — «×», выбор запоминается |
| `details` | `(title, key=None, open=False, classes='')` + caller — сворачиваемый блок; `key` — открытость запоминается |
| `legend` | `(items)` — `[(kind, label)]`, kind: `always, numerator, denominator, conflict, now` |
| `week_now` / `week_badge` | `(current_week, week_number=None)` — нейтральный чип «Знаменатель · 8-я неделя» / `(week_type, short=False)` — нейтральный бейдж, цвет недели — только точка |
| `role_badge` | `(role)` — роль пользователя: нейтральный outline с иконкой |
| `badge` | `(status, label=None, variant=None, icon=None)` — таблица `STATUS` (цвет = состояние): новые лиды и типы транзакций — нейтрально, «Ждёт выдачи» — warning, роли — outline |
| `empty_state` / `empty_filtered` | `(icon, title, text, action)` (кнопка по умолчанию light) / `(reset_href, text=None)` — «Ничего не найдено по фильтрам» + «Сбросить фильтры» |
| `peek_link` | `(href, label, classes='', title=None)` — ссылка быстрого просмотра |
| `kbd` | `ui.kbd('Ctrl', 'S')` (на Mac — ⌘) |
| `skeleton` | `(lines=3)` |
| прежние | `button`, `card`, `pagination`, `confirm_button`, `field`, `image_uploader`, `data_table`, `kv`, `avatar`, `progress_bar` (полоса нейтральная, `tone='brand'` — выделенная), `section_title`, `lib`, `css`, `js`, `json_data`, `csrf_field` — без изменений API |

Пример страницы-списка:

```jinja
{% call ui.page_header('Новости', actions=[
    {'menu': [{'label': 'Экспорт CSV', 'href': url_with(format='csv'), 'icon': 'download'}]},
    {'label': 'Добавить новость', 'href': url_for('news_form'), 'icon': 'plus-lg', 'variant': 'primary', 'attrs': {'data-cc-hotkey': 'n'}},
]) %}{% endcall %}
{{ ui.summary_line([(stats.total, plural(stats.total, 'новость', 'новости', 'новостей')), (stats.month, 'за 30 дней')]) }}
<form id="newsBulk" method="post" action="{{ url_for('news_bulk') }}">{{ ui.next_field() }}</form>
{% call ui.list_card() %}
    {% call ui.filters_bar(search={'name': 'q', 'value': q, 'placeholder': 'Заголовок или текст'}, inline=True) %}
        {{ ui.filter_select('category', 'Категория', categories, category) }}
        {{ ui.filter_switch('photo', 'С фото', photo) }}
    {% endcall %}
    {% if rows %}
    <div class="cc-table-wrap cc-table-wrap--sticky">
      <table class="cc-table cc-table--stack" data-sortable>
        <thead><tr><th class="cc-select">{{ ui.select_all('newsBulk') }}</th><th>Новость</th>
                   <th class="cc-col-md">Категория</th><th class="cc-num-col cc-col-lg">Просмотры</th><th class="cc-actions"></th></tr></thead>
        <tbody>{% for n in rows %}
          <tr data-href="{{ url_for('news_form', news_id=n.id, next=here()) }}">
            <td class="cc-select">{{ ui.select_row('newsBulk', n.id, n.title) }}</td>
            <td class="cc-cell-primary"><a class="cc-row-link cc-cell-main" href="{{ url_for('news_form', news_id=n.id, next=here()) }}">{{ n.title }}</a></td>
            <td class="cc-cell-meta cc-col-md">{{ ui.badge('x', n.category_label, 'outline') }}</td>
            <td class="cc-num-col cc-col-lg" data-label="Просмотры">{{ n.views|num }}</td>
            <td class="cc-actions">{{ ui.row_menu([
                {'label': 'Редактировать', 'href': url_for('news_form', news_id=n.id, next=here()), 'icon': 'pencil'},
                {'divider': True},
                {'label': 'Удалить', 'action': url_for('news_delete', news_id=n.id), 'icon': 'trash', 'danger': True,
                 'confirm': 'Удалить новость «' ~ n.title ~ '»? Фото тоже удалятся.'}]) }}</td>
          </tr>{% endfor %}</tbody>
      </table>
    </div>
    {{ ui.pagination(page) }}
    {% elif q or category %}{{ ui.empty_filtered(url_for('news')) }}
    {% else %}{{ ui.empty_state('newspaper', 'Новостей пока нет', 'Первая новость появится в приложении сразу.') }}{% endif %}
{% endcall %}
{% call ui.bulkbar('newsBulk', noun=('новость', 'новости', 'новостей')) %}
    <button class="btn btn-sm btn-outline-danger" name="action" value="delete" form="newsBulk" data-confirm="Удалить {items}?">Удалить</button>
{% endcall %}
```

Пример формы (`.cc-form-layout` + `savebar`; клиентская проверка и Ctrl+S — автоматически):

```jinja
{% call ui.page_header('Новая новость', back={'href': back_url('news'), 'label': 'Новости'}) %}{{ ui.header_menu([...]) }}{% endcall %}
<form method="post" enctype="multipart/form-data" data-dirty-guard novalidate>
  {{ ui.csrf_field() }}{{ ui.next_field() }}
  <div class="cc-form-layout">
    <div class="cc-stack">{{ ui.form_errors(errors) }}{% call ui.card('Основное') %}…поля…{% endcall %}</div>
    <aside class="cc-form-aside">{% call ui.card('Превью') %}…{% endcall %}</aside>
  </div>
  {{ ui.savebar('Опубликовать', cancel_href=back_url('news'), submit_attrs={'data-confirm': 'Уведомление получат: студенты', 'data-confirm-variant': 'primary', 'data-confirm-ok': 'Опубликовать'}) }}
</form>
```
Сервер: `flash_action('Новость опубликована', 'Открыть', href=url_for('news_form', news_id=nid)); return redirect(back_url('news'))`.

### Правило: вкладки, сегменты, чипы (§4.6)

| задача | компонент | пример |
|---|---|---|
| переключение подстраниц/разделов | `ui.tabs(items, active)` | Coin: Магазин · Заказы · Кампании · Журнал; Аудитории: Фонд · Свободные; измерения накладок; роли пользователей |
| режим отображения, период | `ui.segmented(items, active, remember=key)` | Неделя / Список; Карточки / Таблица; 7 / 30 / 90 дней |
| фильтр по статусу/типу внутри списка | `ui.filter_chips(param, items, active)` | статусы обращений, заказов, лидов, транзакций |

### Политика подтверждений (§4.11)

| действие | как |
|---|---|
| удаление чего угодно, массовое удаление, очистка расписания группы | модальное подтверждение (красная кнопка): текст называет объект и последствия |
| сброс пароля, блокировка входа, завершение сеансов | подтверждение (`data-confirm-variant="primary"`), в тексте — новый пароль |
| публикация с пушем | подтверждение с аудиторией: «Уведомление получат: студенты» |
| массовая смена группы/курса/преподавателя (> 1 объекта) | подтверждение с числом объектов (`{items}`) |
| закрыть/вернуть обращение, принять накладку, сменить статус лида, вкл/выкл источник, кампанию, товар | **без подтверждения**, `flash_action('Готово', 'Отменить', post=…, fields={…прежнее значение…})` |
| выдать заказ | без модалки, тост со ссылкой «Журнал» |

### Чек-лист перевода страницы-списка (§5.0.3)

1. `page_header`: одна основная кнопка (`'variant': 'primary'`, `'attrs': {'data-cc-hotkey': 'n'}`), прочее — light
   или `header_menu`; иконки в h1 нет; подзаголовок ≤ 1 строки; `back=` вместо «К списку».
2. Ряд KPI → `summary_line` (кроме Обзора, Аналитики приёма и Накладок).
3. Фильтры → `list_card` + `filters_bar(search=…, inline=True)`; статусы → `filter_chips` или `tabs` (+ `ignore=`).
4. Таблица → `.cc-table--stack`, `.cc-cell-primary` / `.cc-cell-meta` / `data-label`, `.cc-col-xl|lg|md` вместо
   `d-*-table-cell`, `tr[data-href]` + `a.cc-row-link`, действия → `row_menu`, выбор → `select_*` + `bulkbar`.
5. Пустое: `empty_state` (данных нет) и `empty_filtered` (не нашлось).
6. Модульный CSS: убрать оранжевый hover, оранжевые числа/ссылки, свои bulk/chips/hint/legend/savebar.
7. Скриншоты 1440 и 390: первая строка данных ≤ 300 / ≤ 450 px, одна основная кнопка, нет горизонтальной прокрутки.

Публичные страницы: `{% extends 'public/base_public.html' %}` (блоки `title, styles, head, body_class, body, header,
brand_href, brand_title, brand_subtitle, lang_switch, content, footer, scripts`), язык `public_lang()` (`?lang=kk`
запоминается в cookie), шрифт полей 16px, шапка — тёмный градиент `--cc-gradient-brand-dark` (мелкий белый текст
≥ 4.88:1), карточка `.cc-public__card`, заголовок `.cc-public__hero`. Тосты всех страниц — `templates/_toasts.html`.

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

### Токены (светлая и тёмная тема; не хардкодить цвета)

| токен | роль |
|---|---|
| `--cc-brand` `#FF6200` | **только декор**: индикатор активного пункта/вкладки, «сегодня/сейчас», прогресс, фокус, выделенный столбик |
| `--cc-brand-strong` `#C24B00` (+`-hover`) | основная кнопка, бренд-текст ≥ 14px |
| `--cc-brand-ink` `#A84100` | текст на `--cc-brand-tint` / `--cc-brand-soft` (бейдж brand, активный чип, счётчик активной вкладки) |
| `--cc-brand-tint`, `--cc-brand-soft`, `--cc-brand-border` | фон выбранного, рамка активного чипа |
| `--cc-nav-badge-bg` | бейдж «brand» в сайдбаре (белый 4.88:1) |
| `--cc-text` / `--cc-text-muted` `#5B6370` / `--cc-text-subtle` `#6B7280` | основной / вторичный (шапки таблиц, подписи) / приглушённый (не на surface-2/3) |
| `--cc-icon-subtle` `#8A9099` | только иконки и декор (не текст) |
| `--cc-row-hover`, `--cc-row-selected` | наведение на строку (серое) / выбранная строка (тёплая + полоса 2px бренда) |
| `--cc-avatar-bg/fg` | нейтральные аватары |
| `--cc-chart-neutral`, `--cc-chart-neutral-strong`, `--cc-chart-highlight` | столбики/полосы: обычные нейтральные, выделенный — бренд |
| `--cc-week-num` `#1565C0`, `--cc-week-den` `#B45309` | цвет недели — **только** полоса карточки занятия и свотч легенды (+ старые `--cc-week-*-bg/fg`) |
| `--cc-success|warning|danger|info|violet|teal` (+`-soft`, `-fg`), `--cc-violet-fg`, `--cc-danger-border` | статусы |
| `--cc-bg`, `--cc-surface`, `--cc-surface-2/3`, `--cc-border`, `--cc-border-strong` | поверхности |
| `--cc-radius-xs|sm|md|lg|xl|pill` 6/8/12/16/20 | радиусы (карточки 12, контролы 8, модалки/панели 16) |
| `--cc-shadow-1` (почти плоско) / `--cc-shadow-2` (оверлеи) | тени: карточки / меню, модалки, панели |
| `--cc-control-h` 36px (40px на < 576 px), `--cc-row-h` 44px, `--cc-sidebar-w` 248px, `--cc-topbar-h` 56px | геометрия |
| `--cc-gradient-brand-dark` | градиент под белый текст (вход, публичные страницы) |
| `--cc-dur-fast|med`, `--cc-ease`, `--cc-font`, `--cc-space-1..7` | движение, шрифт, отступы |

### Классы

Каркас и общие: `.cc-page-header` (`__back`, `__row`, `__title`, `__meta`, `__sub`, `__actions`), `.cc-summary`
(`__item--danger|warning|success`), `.cc-card` (`__head/__title/__sub/__body/__body--flush/__foot`), `.cc-list`
(`__summary`, `__foot`), `.cc-grid .cc-grid-2|3|4`, `.cc-stack`, `.cc-stats`, `.cc-stat-strip`, `.cc-stat`
(`--danger|warning|success|info`), `.cc-badge--success|warning|danger|info|info-outline|neutral|outline|brand|violet|week-num|week-den|week-all|role`,
`.cc-chips` + `.cc-chip(.active)`, `.cc-tabs .cc-tab(.active)`, `.cc-segmented .cc-segmented__item(.active)`,
`.cc-day-switch` (сегменты дней на телефоне; `.active`, `.is-today`), `.cc-scroll-x` (горизонтальная прокрутка с маской),
`.cc-week-chip`, `.cc-legend` + `.cc-legend__swatch--always|numerator|denominator|conflict|now`, `.cc-hint(--info|warning|danger|success)`,
`.cc-details`, `.cc-avatar(--sm|--lg)`, `.cc-empty`, `.cc-skeleton`, `.cc-skel`, `.cc-kv`, `.cc-timeline`, `.cc-bar(--brand|success|info|warning|danger)`,
`.cc-uploader`, `.cc-kbd`, `.cc-link-quiet`, `.cc-public__*`, `.cc-error`, утилиты `.cc-muted .cc-subtle .cc-num .cc-truncate .cc-nowrap .cc-no-print .min-w-0`.

Фильтры: `.cc-filters` (старая панель), `.cc-filters--inline`, `.cc-filters__search`, `.cc-fsel(.is-active)` (пилюля),
`.cc-fswitch`, `.cc-filters__toggle`, `.cc-filters__sheet`, `.cc-filters__reset`, `.cc-filters__submit` (скрыта при JS),
`.cc-filter-tags` (чипы активных фильтров на телефоне — строит `app.js`).

Таблицы: `.cc-table-wrap(--sticky)`, `.cc-table(--compact|--stack)`, `.cc-cell-main`, `.cc-cell-sub`, `.cc-cell-primary`,
`.cc-cell-meta`, `td[data-label]`, `.cc-col-optional` (скрыта в стеке), `.cc-col-xl|lg|md` (скрыты, если **таблица** уже
1100/900/700 px — считает `app.js`; в стеке `.cc-table--stack` на телефоне они **не** скрываются — колонку, которая
дублирует данные карточки или пуста, помечайте ещё и `.cc-col-optional`), `.cc-num-col`, `.cc-select`, `.cc-sticky-col`, `.cc-row-link`, `.cc-row-menu`,
`.cc-row-quick`, `.cc-actions` (никогда не скрывать), `tr.is-selected`, `tr[data-href]`, `.cc-truncate-cell`, `.cc-table-empty`.

Действия и формы: `.cc-bulkbar(.is-visible)`, `.cc-savebar(.is-dirty)`, `.cc-form-layout` + `.cc-form-aside`, `.cc-form-errors`,
`.cc-peek`, `.cc-nav-sep`, `.cc-nav-badge--danger|brand|muted`. Модульные стили — `static/css/<модуль>.css`
(только токены `--cc-*`, префикс модуля: `sch- rm- cf- fr- ct- pp- ap- cn- hm-`).

### JS API (`window.CC`) и атрибуты

```js
CC.toast('Сохранено', 'success');                                         // success | error | warning | info
CC.toast('Обращение закрыто', 'success', {action: {label: 'Отменить', post: '/feedback/status/fb_1', fields: {status: 'new'}}});
CC.toast('Новость опубликована', 'success', {action: {label: 'Открыть', href: '/news/edit/x'}});
CC.toast('Готово', 'info', {action: {label: 'Отменить', onClick: function () { … }}});
CC.confirm('Удалить «Х»?', {title, okText: 'Удалить', danger: true}).then(function (ok) { … });
CC.peek('/applicants/ap_001?next=' + encodeURIComponent(location.pathname + location.search));
document.addEventListener('cc:content-loaded', function (e) { initMyWidgets(e.detail.root); });   // после вставки HTML
CC.onInit(function (root) { … });             // инициализатор для документа и для каждого вставленного фрагмента
CC.progress.start(); CC.progress.done();
CC.hotkey('BracketLeft', prevGroup, 'Предыдущая группа', {group: 'Расписание'});   // e.code, работает в RU-раскладке; видно в «?»
CC.view.remember('schedule_teachers', 'cards');   // cookie cc_view_<key>
CC.validateForm(form) → bool;  CC.markClean(form);  CC.clearSelection('newsBulk');  CC.selection('newsBulk') → ['id1', …]
CC.filterItems(targetEl, 'запрос') → число видимых;  CC.plural(3, ['новость', 'новости', 'новостей'])
CC.chartColors();  CC.chartNeutral();  CC.chartNeutralStrong();  CC.chartHighlight();  CC.applyChartDefaults();
CC.store.get/set/del(key);  CC.session.get/set/del(key);   // localStorage / sessionStorage в try/catch
CC.fetchJSON(url, {method, body, signal}) → Promise;  CC.copy(text);  CC.qrSvg(text, opts);  CC.svgToPng(svg, size);  CC.download(name, data, type)
CC.openPalette();  CC.showHotkeys();  CC.setTheme('dark');  CC.cssVar('--cc-brand');  CC.debounce(fn, ms);  CC.initUploader(el)
```

| атрибут | поведение |
|---|---|
| `a[data-cc-peek]` | быстрый просмотр в правой панели (≥ 768 px; Ctrl/⌘/средняя кнопка — обычный переход) |
| `[data-cc-select="formId"]`, `[data-cc-select-all="formId"]`, `[data-cc-bulkbar="formId"]` | выбор строк и панель массовых действий (обычно через макросы) |
| `[data-cc-hotkey="n"]` | основная кнопка «создать» (клавиша `n`) |
| `[data-cc-hotkey-save]` | что нажимать по Ctrl+S (если на странице не одна `form[data-dirty-guard]`) |
| `[data-cc-search]` | поле поиска страницы (клавиша `/`) |
| `input[data-cc-filter="#target"]` + элементы с `data-search` | мгновенный фильтр на клиенте; пусто → `[data-cc-filter-empty]` |
| `[data-cc-autosubmit-debounce="600"]` | отправка поиска через N мс после ввода; фокус и курсор вернутся после перезагрузки |
| `[data-cc-back]` | ссылка «назад к списку» — подставляется последний URL списка с фильтрами |
| `details[data-cc-collapse-key]`, `[data-cc-hint]` | запоминание открытости / скрытой подсказки |
| `[data-cc-view][data-cc-view-value]` | клик ставит cookie вида (`ui.segmented(remember=…)`) |
| `textarea[data-cc-ctrl-enter]` | Ctrl/⌘+Enter отправляет форму |
| `form[novalidate]` (POST) | клиентская проверка перед отправкой (русские тексты под полями, фокус на первое; фото не теряются); `form[data-cc-novalidate]` — отключить |
| `form[data-dirty-guard]` | предупреждение при уходе + индикатор в `savebar`; Ctrl+S сохраняет |
| `form[data-cc-draft="news:new"]` | черновик полей в браузере (раз в 2 с), плашка «Восстановить черновик» |
| `.modal[data-cc-open-on-load]` | открыть модалку при загрузке (`ui.modal_form(open=True)`) |
| прежние: `data-confirm` (+ `-title`, `-ok`, `-variant="primary"`), `data-table-search`, `data-sortable` + `th[data-sort]`, `tr[data-href]` (Ctrl/⌘/Shift/средняя кнопка — новая вкладка), `data-copy`, `data-autosubmit` (в шторке на телефоне — нет), `data-password-toggle`, `data-loading` | без изменений |

События: `cc:theme` (смена темы — перерисуйте графики), `cc:content-loaded` (`detail.root`), `cc:selection`
(`detail.form`, `detail.count`), `cc:filtered`, `cc:uploader-change`.

Горячие клавиши (по `e.code`, работают в RU-раскладке): Ctrl/⌘+K — поиск по разделам, группам, преподавателям,
аудиториям, людям, новостям и командам (`/api/search`); `/` — поиск страницы; `n` — создать; Ctrl/⌘+S — сохранить;
Ctrl/⌘+Enter — отправить ответ; Esc — закрыть/снять выделение; `?` — окно со списком (включая `CC.hotkey` модулей).
