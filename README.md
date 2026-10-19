# Caspian College — веб-админка

Flask-приложение, которое полностью управляет мобильным приложением колледжа (Flutter + Firebase):
расписание и накладки, абитуриенты и приёмная, Caspian Coin и магазин мерча, новости, опросы, события,
люди. Данные — Firestore/Storage проекта `student-caspian-lol` через Firebase Admin SDK.

## Структура

```
flask_app.py               точка входа (app = Flask(...); cms.register_all(app)); WSGI: from flask_app import app
cms/__init__.py            register_all: подключает модули из MODULES
cms/core.py                Firebase (прод/эмулятор), кэш, Routes, admin_required, CSRF, загрузки в Storage,
                           аудит, count/sum, пагинация, время Алматы, Jinja-фильтры, сайдбар, пуши, CSV
cms/schedule_model.py      модель расписания: день недели, время, чётность, ключи, снапшот занятий
cms/applicant_login.py     логин абитуриента → email Firebase Auth (общий контракт с приложением)
cms/conflicts.py           движок накладок
cms/routes_auth.py         вход/выход, страницы ошибок, /ui-kit
cms/routes_home.py         дашборд, аудит
cms/routes_people.py       пользователи, преподаватели, группы, дисциплины, администрация
cms/routes_content.py      новости, опросы, события, клубы, товары, обратная связь
cms/routes_schedule.py     расписание групп, конструктор, единая таблица, преподаватели, история, экспорт
cms/routes_rooms.py        аудиторный фонд, накладки, свободные аудитории
cms/routes_applicants.py   абитуриенты: аналитика, лиды, карточка, QR-источники
cms/routes_public.py       публичная регистрация по QR (/join)
cms/routes_coins.py        магазин коинов, заказы, кампании, журнал
cms/routes_admissions.py   настройки приёмной, новости абитуриентов
templates/base.html        layout админки (сайдбар, топбар, Ctrl+K, тёмная тема, тосты)
templates/_ui.html         Jinja-макросы компонентов; живой справочник — страница /ui-kit
templates/public/          публичные страницы (mobile-first)
static/css/app.css         дизайн-система (токены = мобильное приложение), static/js/app.js — общий JS
tests/                     тесты на эмуляторах Firebase, сид, скриншоты (см. tests/README.md)
DEPLOY.md                  переменные окружения, порядок деплоя, ротация ключа
```

## Быстрый старт

```bash
pip install -r requirements.txt
tests/run_tests.sh 18000            # все тесты на эмуляторах (Java + firebase-tools)
CMS_INSECURE_COOKIES=1 python flask_app.py
```

Разработчику модуля: начните с `tests/README.md` (API ядра, макросы, CSS/JS, тесты, скриншоты) и `/ui-kit`.
