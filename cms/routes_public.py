# cms/routes_public.py — публичная регистрация абитуриента по QR: /join, /join/<CODE> (+ ?ref=REFCODE)
# (владелец — APPLICANTS).
#
# Абитуриент наводит камеру на QR со стенда → эта страница (без входа, mobile-first, RU/KZ) →
# логин (телефон или латинский логин, SPEC §1) + пароль + повтор + согласие на ПДн → аккаунт.
# Создаётся Auth-пользователь с синтетическим email и users/{uid} — ровно как ApplicantRegistration
# в приложении (SPEC §2.1): role 'applicant', login, email, phone (если логин — телефон), source
# (код QR-источника или 'WEB'), consents {pd, version 2, acceptedAt}, marketingConsent, createdAt.
# Если запись профиля не удалась — Auth-аккаунт удаляется (откат). CRM-карточку, счётчик
# регистраций источника, приветственные коины и реферальный код делает Cloud Function onApplicantCreated.
#
# Защита: CSRF (core), honeypot-поле, минимальное время заполнения, лимит попыток и регистраций
# по IP (в памяти процесса). Сканы QR: +1 к applicant_sources/{CODE}.scans не чаще раза в 30 минут
# на браузер (cookie), ботов-превьюшек и ?preview=1 не считаем.

import re
import time

from flask import make_response, redirect, render_template, request, session, url_for

from .applicant_login import (PASSWORD_MAX, PASSWORD_MIN, RESET_PASSWORD, canonical_login, display_login,
                              login_kind, to_auth_email)
from .core import (Increment, SERVER_TIMESTAMP, RateLimiter, Routes, client_ip, db, fb_auth, log, public_lang,
                   safe_url)
from .routes_applicants import invalidate_applicants_cache, load_admissions, load_sources

routes = Routes()

CODE_RE = re.compile(r'^[A-Z0-9]{3,24}$')
REF_RE = re.compile(r'^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$')
CONSENT_VERSION = 2
WEB_SOURCE = 'WEB'

MIN_FILL_SECONDS = 2            # быстрее человек форму не заполнит (защита от ботов)
DONE_TTL = 3600                 # с — сколько показываем страницу «Готово» после регистрации
SCAN_COOKIE_TTL = 1800          # с — повторное открытие QR в течение 30 минут не считается сканом
BOT_UA_RE = re.compile(r'bot|crawl|spider|preview|facebookexternalhit|whatsapp|telegram|slack|discord|'
                       r'curl|wget|python-requests|headless', re.IGNORECASE)

# Лимиты на IP: на стенде/в школе многие сидят за одним Wi-Fi — лимиты щедрые.
_attempts = RateLimiter(30, 600)     # любые отправки формы: 30 за 10 минут
_created = RateLimiter(15, 600)      # успешные регистрации: 15 за 10 минут

# Тексты страницы (RU/KZ). Формулировки — как в приложении (assets/lang/*.json, applicant_*).
TEXTS = {
    'ru': {
        'title': 'Регистрация абитуриента',
        'hero_title': 'Добро пожаловать в Caspian!',
        'hero_sub': 'Новости приёмной комиссии, Caspian Coin за участие и мерч колледжа — в одном приложении. '
                    'Регистрация займёт минуту',
        'perk_news': 'Новости приёмной', 'perk_coins': 'Caspian Coin', 'perk_shop': 'Мерч за коины',
        'source_chip': 'QR-код: {title}',
        'welcome_banner': '+{n} Caspian Coin сразу после регистрации',
        'ref_banner': 'Вас пригласил друг. Заполните профиль в приложении — и он получит бонус',
        'form_title': 'Создайте аккаунт',
        'login_label': 'Телефон или логин',
        'login_placeholder': '+7 701 123 45 67 или aigerim.s',
        'login_rules': 'Номер телефона или логин латиницей: 3–24 символа, начинается с буквы (a–z, 0–9, точка, _)',
        'login_preview': 'Вы будете входить как: {login}',
        'password_label': 'Пароль',
        'password_rules': '6–64 символа, без пробелов в начале и в конце',
        'password_repeat': 'Повторите пароль',
        'show_password': 'Показать пароль',
        'consent_pd': 'Я соглашаюсь на обработку моих персональных данных, а если мне нет 18 лет — '
                      'с согласия родителя или законного представителя',
        'privacy': 'Политика конфиденциальности',
        'privacy_title': 'Обработка персональных данных',
        'privacy_text': 'Регистрируясь, вы даёте College of Caspian University согласие на сбор и обработку '
                        'персональных данных: логина (номера телефона или имени пользователя), а также данных, '
                        'которые вы по желанию укажете в профиле, — ФИО, телефона, города, школы, класса и '
                        'интересующих специальностей. Если вам нет 18 лет, согласие даётся с согласия родителя '
                        'или законного представителя. Данные нужны, чтобы приёмная комиссия могла рассказать вам '
                        'о поступлении, присылать новости и учитывать Caspian Coin. Мы не передаём их третьим лицам '
                        'для рекламы. В любой момент вы можете изменить данные или удалить аккаунт в разделе '
                        '«Профиль» приложения. Согласие даётся в соответствии с Законом Республики Казахстан '
                        '«О персональных данных и их защите».',
        'consent_marketing': 'Приёмная комиссия может связаться со мной по телефону или WhatsApp',
        'optional': 'необязательно',
        'submit': 'Зарегистрироваться',
        'submitting': 'Создаём аккаунт…',
        'remember': 'Запомните логин и пароль: письмо для восстановления не придёт — сбросить пароль можно только '
                    'через приёмную комиссию.',
        'profile_later': 'ФИО, школу и интересующие специальности укажете позже в профиле — за полностью '
                         'заполненный профиль можно получить Caspian Coin.',
        'have_account': 'Уже есть аккаунт? Войдите в приложении Caspian College во вкладке «Абитуриент».',
        'hp_label': 'Не заполняйте это поле',
        'err_check': 'Проверьте отмеченные поля',
        'err_login_required': 'Введите телефон или логин',
        'err_login': 'Введите казахстанский номер (+7 7XX XXX XX XX) или логин латиницей: 3–24 символа, '
                     'начинается с буквы (a–z, 0–9, точка, _)',
        'err_login_email': 'Email не нужен: укажите номер телефона или придумайте логин латиницей',
        'err_login_taken': 'Этот логин уже занят. Если это вы — войдите в приложении; забыли пароль — обратитесь '
                           'в приёмную комиссию',
        'err_pw_short': 'Слишком простой пароль — минимум 6 символов',
        'err_pw_long': 'Пароль слишком длинный — не больше 64 символов',
        'err_pw_spaces': 'Пароль не должен начинаться или заканчиваться пробелом',
        'err_pw_default': 'Этот пароль нельзя использовать — придумайте свой',
        'err_pw_repeat': 'Пароли не совпадают',
        'err_consent': 'Без согласия на обработку данных регистрация невозможна',
        'err_too_fast': 'Форма отправлена слишком быстро. Проверьте данные и нажмите «Зарегистрироваться» ещё раз',
        'err_too_many': 'Слишком много попыток с этого устройства. Попробуйте через 10 минут или зарегистрируйтесь '
                        'в приложении',
        'err_server': 'Не удалось создать аккаунт. Попробуйте ещё раз через минуту',
        'err_bot': 'Не удалось отправить форму. Обновите страницу и попробуйте ещё раз',
        'done_title': 'Аккаунт создан!',
        'done_sub': 'Осталось войти в приложение Caspian College',
        'your_login': 'Ваш логин',
        'password_note': 'Пароль — тот, что вы только что придумали',
        'copy_btn': 'Скопировать', 'copied': 'Скопировано',
        'welcome_done': '+{n} Caspian Coin уже на вашем счёте',
        'next_title': 'Что дальше',
        'step1': 'Установите приложение Caspian College',
        'step2': 'На экране входа откройте вкладку «Абитуриент»',
        'step3': 'Введите логин и пароль, затем заполните профиль — за это начислим ещё коины',
        'download_on': 'Загрузите в', 'get_it_on': 'Доступно в',
        'stores_missing': 'Найдите «Caspian College» в App Store или Google Play',
        'contacts': 'Приёмная комиссия', 'call': 'Позвонить',
        'again': 'Зарегистрировать ещё один аккаунт',
    },
    'kk': {
        'title': 'Талапкерді тіркеу',
        'hero_title': 'Caspian-ға қош келдіңіз!',
        'hero_sub': 'Қабылдау комиссиясының жаңалықтары, қатысқаны үшін Caspian Coin және колледж мерчі — бір '
                    'қосымшада. Тіркелу бір минут алады',
        'perk_news': 'Қабылдау жаңалықтары', 'perk_coins': 'Caspian Coin', 'perk_shop': 'Коинге мерч',
        'source_chip': 'QR-код: {title}',
        'welcome_banner': 'Тіркелгеннен кейін бірден +{n} Caspian Coin',
        'ref_banner': 'Сізді досыңыз шақырды. Қосымшада профильді толтырыңыз — ол бонус алады',
        'form_title': 'Аккаунт ашыңыз',
        'login_label': 'Телефон немесе логин',
        'login_placeholder': '+7 701 123 45 67 немесе aigerim.s',
        'login_rules': 'Телефон нөмірі немесе латын әріптерімен логин: 3–24 таңба, әріптен басталады '
                       '(a–z, 0–9, нүкте, _)',
        'login_preview': 'Сіз осылай кіресіз: {login}',
        'password_label': 'Құпиясөз',
        'password_rules': '6–64 таңба, басында және соңында бос орынсыз',
        'password_repeat': 'Құпиясөзді қайталаңыз',
        'show_password': 'Құпиясөзді көрсету',
        'consent_pd': 'Дербес деректерімді өңдеуге келісемін, ал 18 жасқа толмаған болсам — ата-анамның немесе '
                      'заңды өкілімнің келісімімен',
        'privacy': 'Құпиялылық саясаты',
        'privacy_title': 'Дербес деректерді өңдеу',
        'privacy_text': 'Тіркеле отырып, сіз College of Caspian University-ге дербес деректеріңізді жинауға және '
                        'өңдеуге келісім бересіз: логин (телефон нөмірі немесе пайдаланушы аты), сондай-ақ профильде '
                        'қалауыңыз бойынша көрсететін деректер — аты-жөні, телефон, қала, мектеп, сынып және '
                        'қызықтыратын мамандықтар. Егер сізге 18 жас толмаса, келісім ата-анаңыздың немесе заңды '
                        'өкіліңіздің келісімімен беріледі. Деректер қабылдау комиссиясы сізге түсу туралы айтып, '
                        'жаңалықтар жіберіп, Caspian Coin есебін жүргізуі үшін қажет. Біз оларды жарнама үшін үшінші '
                        'тұлғаларға бермейміз. Кез келген уақытта деректеріңізді өзгерте аласыз немесе қосымшаның '
                        '«Профиль» бөлімінде аккаунтты жоя аласыз. Келісім Қазақстан Республикасының «Дербес '
                        'деректер және оларды қорғау туралы» Заңына сәйкес беріледі.',
        'consent_marketing': 'Қабылдау комиссиясы маған телефон немесе WhatsApp арқылы хабарласа алады',
        'optional': 'міндетті емес',
        'submit': 'Тіркелу',
        'submitting': 'Аккаунт ашылуда…',
        'remember': 'Логин мен құпиясөзді есте сақтаңыз: қалпына келтіру хаты келмейді — құпиясөзді тек қабылдау '
                    'комиссиясы арқылы қалпына келтіруге болады.',
        'profile_later': 'Аты-жөніңізді, мектебіңізді және қызықтыратын мамандықтарды кейін профильде көрсетесіз — '
                         'толық толтырылған профиль үшін Caspian Coin алуға болады.',
        'have_account': 'Аккаунтыңыз бар ма? Caspian College қосымшасындағы «Талапкер» бөлімінде кіріңіз.',
        'hp_label': 'Бұл өрісті толтырмаңыз',
        'err_check': 'Белгіленген өрістерді тексеріңіз',
        'err_login_required': 'Телефонды немесе логинді енгізіңіз',
        'err_login': 'Қазақстандық нөмірді (+7 7XX XXX XX XX) немесе латын әріптерімен логинді енгізіңіз: '
                     '3–24 таңба, әріптен басталады (a–z, 0–9, нүкте, _)',
        'err_login_email': 'Email қажет емес: телефон нөмірін көрсетіңіз немесе латын әріптерімен логин ойлап табыңыз',
        'err_login_taken': 'Бұл логин бос емес. Егер бұл сіз болсаңыз — қосымшаға кіріңіз; құпиясөзді ұмытсаңыз — '
                           'қабылдау комиссиясына хабарласыңыз',
        'err_pw_short': 'Құпия сөз тым қарапайым — кемінде 6 таңба',
        'err_pw_long': 'Құпиясөз тым ұзын — 64 таңбадан аспауы керек',
        'err_pw_spaces': 'Құпиясөз бос орынмен басталмауы немесе аяқталмауы керек',
        'err_pw_default': 'Бұл құпиясөзді қолдануға болмайды — өзіңіз ойлап табыңыз',
        'err_pw_repeat': 'Құпиясөздер сәйкес келмейді',
        'err_consent': 'Деректерді өңдеуге келісімсіз тіркелу мүмкін емес',
        'err_too_fast': 'Форма тым жылдам жіберілді. Деректерді тексеріп, «Тіркелу» батырмасын қайта басыңыз',
        'err_too_many': 'Бұл құрылғыдан тым көп әрекет жасалды. 10 минуттан кейін қайталаңыз немесе қосымшада '
                        'тіркеліңіз',
        'err_server': 'Аккаунт ашылмады. Бір минуттан кейін қайталап көріңіз',
        'err_bot': 'Форма жіберілмеді. Бетті жаңартып, қайталап көріңіз',
        'done_title': 'Аккаунт ашылды!',
        'done_sub': 'Caspian College қосымшасына кіру ғана қалды',
        'your_login': 'Сіздің логиніңіз',
        'password_note': 'Құпиясөз — өзіңіз жаңа ғана ойлап тапқан',
        'copy_btn': 'Көшіру', 'copied': 'Көшірілді',
        'welcome_done': '+{n} Caspian Coin шотыңызда',
        'next_title': 'Әрі қарай',
        'step1': 'Caspian College қосымшасын орнатыңыз',
        'step2': 'Кіру экранында «Талапкер» бөлімін ашыңыз',
        'step3': 'Логин мен құпиясөзді енгізіп, профильді толтырыңыз — ол үшін тағы коин береміз',
        'download_on': 'Жүктеп алыңыз', 'get_it_on': 'Қолжетімді',
        'stores_missing': 'App Store немесе Google Play-ден «Caspian College» қосымшасын табыңыз',
        'contacts': 'Қабылдау комиссиясы', 'call': 'Қоңырау шалу',
        'again': 'Тағы бір аккаунт тіркеу',
    },
}


def reset_limits():
    """Сбросить лимиты по IP (для тестов)."""
    _attempts._hits.clear()
    _created._hits.clear()


def _norm_code(code):
    c = str(code or '').strip().upper()
    return c if CODE_RE.match(c) else None


def _norm_ref(ref):
    r = str(ref or '').strip().upper()
    return r if REF_RE.match(r) else None


def _referrer_uid(ref):
    """uid пригласившего по реферальному коду (referral_codes/{CODE}) или None."""
    if not ref:
        return None
    try:
        snap = db.collection('referral_codes').document(ref).get()
    except Exception:
        log.warning('Реферальный код %s не прочитан', ref, exc_info=True)
        return None
    uid = (snap.to_dict() or {}).get('uid') if snap.exists else None
    return uid if isinstance(uid, str) and uid else None


def password_error_key(password, repeat):
    """(поле, ключ ошибки) для пароля абитуриента (SPEC §1) или None. Пароль НЕ обрезается."""
    p = password or ''
    if len(p) < PASSWORD_MIN:
        return 'password', 'err_pw_short'
    if len(p) > PASSWORD_MAX:
        return 'password', 'err_pw_long'
    if p != p.strip():
        return 'password', 'err_pw_spaces'
    if p == RESET_PASSWORD:
        return 'password', 'err_pw_default'
    if repeat != p:
        return 'password2', 'err_pw_repeat'
    return None


def validate_registration(form):
    """Проверка формы регистрации → {поле: ключ ошибки} (пусто — всё верно). Как ApplicantRegistration.validate."""
    errors = {}
    raw = form.get('login') or ''
    kind = login_kind(raw) if len(raw) <= 100 else None
    if not raw.strip():
        errors['login'] = 'err_login_required'
    elif kind == 'email':
        errors['login'] = 'err_login_email'   # email не нужен: вход по телефону или логину
    elif kind is None:
        errors['login'] = 'err_login'
    pw = password_error_key(form.get('password') or '', form.get('password2') or '')
    if pw:
        errors[pw[0]] = pw[1]
    if form.get('consent') != '1':
        errors['consent'] = 'err_consent'
    return errors


def _context(code, lang):
    sources = {}
    try:
        sources = load_sources()
    except Exception:
        log.warning('QR-источники не прочитаны', exc_info=True)
    source = sources.get(code) if code else None
    welcome = 0
    if source and source.get('active') is True:
        try:
            welcome = max(0, min(int(source.get('welcomeCoins') or 0), 1000))
        except (TypeError, ValueError):
            welcome = 0
    adm = load_admissions()
    ua = request.headers.get('User-Agent', '')
    platform = 'ios' if re.search(r'iPhone|iPad|iPod', ua) else ('android' if 'Android' in ua else '')
    return {
        'lang': lang, 't': TEXTS[lang], 'code': code, 'source': source, 'welcome': welcome,
        'source_title': (str(source.get('title') or code) if source else ''),
        'privacy_url': safe_url(adm.get('privacyUrl'), default=''),
        'app_store': safe_url(adm.get('appStoreUrl'), default=''),
        'play_store': safe_url(adm.get('playStoreUrl'), default=''),
        'adm_phone': str(adm.get('phone') or '').strip(), 'adm_whatsapp': str(adm.get('whatsapp') or '').strip(),
        'platform': platform,
    }


def _count_scan(code, resp):
    """+1 скан источника (не чаще раза в 30 минут на браузер; не для ботов и предпросмотра)."""
    cookie = 'cc_scan_' + code
    if request.cookies.get(cookie) or request.args.get('preview') or BOT_UA_RE.search(
            request.headers.get('User-Agent', '')):
        return
    try:
        db.collection('applicant_sources').document(code).update({'scans': Increment(1),
                                                                  'lastScanAt': SERVER_TIMESTAMP})
    except Exception:
        log.warning('Скан источника %s не засчитан', code, exc_info=True)
        return
    resp.set_cookie(cookie, '1', max_age=SCAN_COOKIE_TTL, httponly=True, samesite='Lax',
                    secure=bool(request.is_secure))


@routes.route('/join', defaults={'code': None}, methods=['GET', 'POST'])
@routes.route('/join/<code>', methods=['GET', 'POST'])
def public_join(code):
    lang = public_lang()
    code = _norm_code(code)
    ref = _norm_ref(request.values.get('ref'))
    ctx = _context(code, lang)

    if request.method == 'POST':
        return _register(code, ref, ctx)

    if request.args.get('done'):
        info = session.get('join_done')
        if not info or time.time() - float(info.get('at') or 0) > DONE_TTL:
            return redirect(url_for('public_join', code=code))
        return render_template('public/join.html', mode='done', info=info, **ctx)

    session['join_ts'] = time.time()
    ctx['ref_ok'] = bool(_referrer_uid(ref)) if ref else False
    resp = make_response(render_template('public/join.html', mode='form', form={}, errors={}, ref=ref, **ctx))
    if ctx['source'] is not None:
        _count_scan(code, resp)
    return resp


def _form_error(ctx, ref, errors, status, general=None):
    form = {'login': (request.form.get('login') or '')[:100], 'consent': request.form.get('consent') == '1',
            'marketing': request.form.get('marketing') == '1'}
    ctx['ref_ok'] = bool(ref)
    if 'join_ts' not in session:
        session['join_ts'] = time.time()
    return render_template('public/join.html', mode='form', form=form, errors=errors, general=general, ref=ref,
                           **ctx), status


def _register(code, ref, ctx):
    ip = client_ip()
    if not _attempts.hit(ip):
        return _form_error(ctx, ref, {}, 429, 'err_too_many')
    if (request.form.get('website') or '').strip():
        log.warning('Регистрация /join: заполнено поле-ловушка (ip %s)', ip)
        return _form_error(ctx, ref, {}, 400, 'err_bot')
    started = session.get('join_ts')
    try:
        started = float(started)
    except (TypeError, ValueError):
        started = None
    if started is None or time.time() - started < MIN_FILL_SECONDS:
        session['join_ts'] = time.time()
        return _form_error(ctx, ref, {}, 400, 'err_too_fast')
    errors = validate_registration(request.form)
    if errors:
        return _form_error(ctx, ref, errors, 400, 'err_check')
    if _created.blocked(ip):
        return _form_error(ctx, ref, {}, 429, 'err_too_many')

    raw_login = request.form.get('login') or ''
    password = request.form.get('password') or ''      # НЕ обрезаем (SPEC §1)
    email = to_auth_email(raw_login)
    login = canonical_login(raw_login)
    try:
        user = fb_auth.create_user(email=email, password=password)
    except fb_auth.EmailAlreadyExistsError:
        return _form_error(ctx, ref, {'login': 'err_login_taken'}, 400, 'err_check')
    except Exception:
        log.exception('Регистрация /join: Auth-аккаунт не создан')
        return _form_error(ctx, ref, {}, 503, 'err_server')

    referrer = _referrer_uid(ref)
    doc = {
        'role': 'applicant', 'login': login, 'email': email,
        'source': code if ctx['source'] is not None else WEB_SOURCE,
        'consents': {'pd': True, 'version': CONSENT_VERSION, 'acceptedAt': SERVER_TIMESTAMP},
        'marketingConsent': request.form.get('marketing') == '1',
        'createdAt': SERVER_TIMESTAMP, 'updatedAt': SERVER_TIMESTAMP,
    }
    if login_kind(raw_login) == 'phone':
        doc['phone'] = login          # логин-телефон сразу становится телефоном профиля
    if referrer and referrer != user.uid:
        doc['referredBy'] = referrer
    try:
        db.collection('users').document(user.uid).create(doc)
    except Exception:
        log.exception('Регистрация /join: профиль %s не записан — откатываем Auth', user.uid)
        try:
            fb_auth.delete_user(user.uid)
        except Exception:
            log.exception('Регистрация /join: не удалось удалить Auth-аккаунт %s', user.uid)
        return _form_error(ctx, ref, {}, 503, 'err_server')

    _created.hit(ip)
    invalidate_applicants_cache()
    log.info('Регистрация /join: %s (источник %s, ip %s)', user.uid, doc['source'], ip)
    session.pop('join_ts', None)
    session['join_done'] = {'login': display_login(email), 'coins': ctx['welcome'], 'source': ctx['source_title'],
                            'at': time.time()}
    return redirect(url_for('public_join', code=code, done=1))
