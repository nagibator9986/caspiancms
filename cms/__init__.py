# cms — пакет веб-админки Caspian College.
#
# register_all(app) подключает все модули (список фиксирован, см. SPEC §6.1).
# Каждый модуль cms/routes_*.py экспортирует:
#   routes        — cms.core.Routes с маршрутами (обязательно);
#   setup(app)    — дополнительная настройка (необязательно);
#   nav_badges()  — {endpoint: число} для бейджей сайдбара (необязательно, кэш 60 с).
#
# Импорт пакета НЕ инициализирует Firebase: модули импортируются внутри register_all,
# поэтому чистые части (cms.applicant_login, cms.schedule_model) тестируются без сети.

import importlib
import logging

MODULES = [
    'routes_auth',        # вход/выход, страницы ошибок (FOUNDATION)
    'routes_home',        # дашборд, аудит (PEOPLE)
    'routes_people',      # пользователи, преподаватели, группы, дисциплины, администрация (PEOPLE)
    'routes_calendar',    # академический календарь: PDF на группу (PEOPLE; зависит от routes_people)
    'routes_promote',     # перевод студентов на следующий курс (PEOPLE; зависит от routes_people)
    'routes_content',     # новости, опросы, события, клубы, товары, обратная связь (CONTENT)
    'routes_schedule',    # расписание (SCHEDULE)
    'routes_rooms',       # аудиторный фонд, накладки, свободные аудитории (CONFLICTS)
    'routes_substitutions',  # замены преподавателей: кто кого заменяет, статистика, свободные (SCHEDULE; после routes_schedule)
    'routes_applicants',  # абитуриенты: аналитика, лиды, карточка, QR-источники (APPLICANTS)
    'routes_public',      # публичная регистрация /join (APPLICANTS)
    'routes_coins',       # магазин коинов, заказы, кампании, журнал (COINS)
    'routes_admissions',  # приёмная, новости абитуриентов (COINS)
]

log = logging.getLogger('cms')


def register_all(app):
    """Настроить приложение и зарегистрировать маршруты всех модулей."""
    from . import core
    from .schedule_model import get_teacher_name

    core.init_app(app)
    app.add_template_global(get_teacher_name, 'get_teacher_name')

    loaded = []
    for name in MODULES:
        try:
            module = importlib.import_module('.' + name, __name__)
            module.routes.register(app)
            setup = getattr(module, 'setup', None)
            if callable(setup):
                setup(app)
        except Exception:
            # В проде сломанный модуль не должен ронять всю админку: остальные разделы
            # работают, ссылки на его страницы в сайдбаре ведут на '#'. В тестах — падаем.
            if core.STRICT_MODULES or app.testing:
                raise
            log.exception('Модуль cms.%s не подключён', name)
            continue
        loaded.append(module)
    core._nav_modules[:] = loaded
    app.extensions['cms_modules'] = [m.__name__ for m in loaded]
    return app
