# Деплой веб-админки Caspian College (PythonAnywhere)

Приложение — Flask (`flask_app.py` → пакет `cms/`), Python **3.10** (прод), Firebase Admin SDK.
Код совместим с 3.10: без `datetime.UTC`, `except*`, `typing.Self`, `tomllib`, `StrEnum`.

## 1. Переменные окружения

| переменная | обязательна | значение |
|---|---|---|
| `CMS_SECRET_KEY` | желательно | секрет подписи cookie-сессии (≥ 32 случайных символа: `python3 -c "import secrets; print(secrets.token_hex(32))"`). Если не задана — генерируется один раз в `instance/secret_key` (файл в `.gitignore`, права 600). **Старый ключ `change_this_to_a_strong_secret_key` больше не используется** — все старые сессии станут недействительны, админам нужно войти заново. |
| `FIREBASE_CREDENTIALS` | после ротации — да | путь к JSON-ключу сервис-аккаунта **вне репозитория** (например `/home/<user>/secrets/caspian-cms-sa.json`) или сам JSON (строка, начинающаяся с `{`). Синоним: `GOOGLE_APPLICATION_CREDENTIALS`. Если не задано — `<папка проекта>/firebase-service-account.json` (как раньше). |
| `FIREBASE_WEB_API_KEY` | нет | Web API key клиента Firebase для входа (REST Identity Toolkit). По умолчанию — текущий публичный ключ приложения (это не секрет). |
| `CMS_STORAGE_BUCKET` | нет | бакет Storage, по умолчанию `student-caspian-lol.firebasestorage.app`. |
| `CMS_PROJECT_ID` | нет | id проекта, по умолчанию `student-caspian-lol` (берётся и из `GCLOUD_PROJECT`). |
| `CMS_INSTANCE_DIR` | нет | каталог для `secret_key` и межпроцессных штампов кэша (по умолчанию `<проект>/instance`). Должен быть доступен на запись всем воркерам. |
| `CMS_MAX_UPLOAD_MB` | нет | лимит тела запроса, МБ (по умолчанию 64; одно фото ≤ 10 МБ, до 10 фото). |
| `CMS_INSECURE_COOKIES=1` | только локально | разрешить cookie без `Secure` (http). На проде не ставить: PythonAnywhere отдаёт сайт по HTTPS. |
| `CMS_DISABLE_PUSH=1` | нет | не отправлять FCM-пуши из CMS (отладка). |
| `CMS_STRICT=1` | нет | падать при ошибке импорта модуля (по умолчанию в проде сломанный модуль логируется, остальные разделы работают). |

На PythonAnywhere переменные задаются в WSGI-файле **до** импорта приложения (или через `.env`,
загружаемый там же):

```python
# /var/www/<user>_pythonanywhere_com_wsgi.py
import os, sys
os.environ['CMS_SECRET_KEY'] = '…'                                  # секрет — только здесь, не в git
os.environ['FIREBASE_CREDENTIALS'] = '/home/<user>/secrets/caspian-cms-sa.json'
path = '/home/<user>/caspiancms'
if path not in sys.path:
    sys.path.insert(0, path)
from flask_app import app as application  # noqa
```

Веб-вкладка: **Force HTTPS — включить** (cookie сессии с флагом `Secure`). Static files: URL `/static/` →
`/home/<user>/caspiancms/static/` (статика отдаётся сервером PythonAnywhere, версия в `?v=` обновляется
по времени изменения файла).

## 2. Порядок деплоя

Изменения затрагивают оба репозитория — выкатывать строго по порядку:

1. **Firestore rules и индексы** (репозиторий приложения):
   `firebase deploy --only firestore:rules,firestore:indexes` (+ `storage` при изменении `storage.rules`).
   Дождаться, пока индексы станут `READY` в консоли (иначе фильтры CMS вернут ошибку «нужен индекс»).
2. **Cloud Functions**: `firebase deploy --only functions` (обработка регистраций, заказов, `notify:false`
   для массовых правок расписания из CMS).
3. **CMS** (PythonAnywhere):
   ```bash
   cd ~/caspiancms && git pull
   pip3.10 install --user -r requirements.txt     # новых зависимостей нет
   ```
   затем **Reload** на вкладке Web. Проверить: `/login` открывается, вход работает, `/` и `/audit` открываются.
4. **Мобильное приложение** (сборки в сторы) — после того как backend и CMS на месте.

Откат CMS: `git checkout <предыдущий коммит>` + Reload (данные в Firestore совместимы с прежним кодом).

## 3. Ротация ключа сервис-аккаунта (обязательно)

Файл `firebase-service-account.json` **закоммичен в git** и был запушен в GitHub — ключ считается
скомпрометированным. В этой задаче файл из git **не удаляется** (иначе `git pull` на PythonAnywhere удалит
его и сайт упадёт). Ручные шаги (владелец проекта Firebase/GCP):

1. GCP Console → IAM → Service Accounts → аккаунт из `client_email` текущего ключа → **Keys → Add key → JSON**.
   Скачать новый ключ.
2. Загрузить ключ на PythonAnywhere **вне репозитория**: `~/secrets/caspian-cms-sa.json`, `chmod 600`.
3. В WSGI-файле задать `FIREBASE_CREDENTIALS` (см. выше), **Reload**, проверить вход и любую запись
   (например, ответ в «Обратной связи»).
4. В GCP **удалить старый ключ** (Keys → старый key id → Delete). Учтите: старые подписанные URL фото
   администрации (`management/`, подписаны старым ключом) перестанут открываться — перезагрузите эти фото
   в разделе «Администрация» (новые загрузки используют download-токены и от ключа не зависят).
5. Отдельным коммитом убрать файл из git: `git rm --cached firebase-service-account.json`, раскомментировать
   строку в `.gitignore`, запушить. **Перед** `git pull` на сервере убедиться, что сайт уже работает с
   `FIREBASE_CREDENTIALS` (шаг 3) — тогда удаление файла из рабочей копии ничего не сломает.
6. При желании — очистить историю git (`git filter-repo`) и/или сделать репозиторий приватным; ротация
   ключа всё равно обязательна, история уже могла быть скопирована.

Аналогично старый `secret_key` из кода больше не используется (шаг не нужен — новый ключ в env/`instance/`).

## 4. Что изменилось в безопасности (для админов)

* Сессия — 12 часов, cookie `HttpOnly`, `SameSite=Lax`, `Secure`; роль администратора перепроверяется
  каждые 10 минут (снятая роль = выход).
* Все изменения — только POST с CSRF-токеном; не более 10 неудачных входов с одного IP за 10 минут.
* Ошибки показываются понятным текстом, подробности — в error log PythonAnywhere (логгер `cms`).
* Действия админов пишутся в журнал `cms_audit` (страница «Аудит»).

## 5. Локальный запуск и тесты

```bash
pip install -r requirements.txt
CMS_INSECURE_COOKIES=1 python flask_app.py            # http://127.0.0.1:5001 (нужен ключ сервис-аккаунта)
tests/run_tests.sh 18000                              # тесты на эмуляторах Firebase (ключ не нужен)
tests/run_screens.sh 18000 /tmp/shots / /ui-kit       # скриншоты (Playwright)
```

Подробности — `tests/README.md`.
