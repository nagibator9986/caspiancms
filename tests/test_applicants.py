# tests/test_applicants.py — раздел «Абитуриенты» и публичная регистрация /join (APPLICANTS).
#
# Публичная регистрация по QR (SPEC §1, §2.1, §6.5): минимальная регистрация, занятый логин, кириллица,
# несовпадение паролей, honeypot, слишком быстрая отправка, CSRF, source и ref пишутся, откат Auth
# при ошибке Firestore, счётчик сканов. Админка: аналитика на сид-данных, лиды (поиск/фильтры/CSV),
# карточка, сброс пароля до abiturient123 (D5), статус/заметки/контакт, коины, перевод в студенты,
# удаление, массовые действия, QR-источники (CRUD, плакат).

import csv
import io
import re
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.base import CmsTestCase
from tests.seed import ADMIN, APPLICANT_ACCOUNT

DOMAIN = 'abiturient.student-caspian-lol.web.app'
# Поля, которые правила разрешают владельцу при создании users/{uid} (SPEC §3, applicantCreateValid),
# + служебное referredBy (пишет только CMS через Admin SDK).
ALLOWED_CREATE_KEYS = {'role', 'login', 'name', 'email', 'phone', 'city', 'school', 'grade', 'specialties', 'source',
                       'consents', 'marketingConsent', 'createdAt', 'updatedAt', 'referredBy'}


def _auth_user_or_none(email):
    from cms.core import fb_auth
    try:
        return fb_auth.get_user_by_email(email)
    except fb_auth.UserNotFoundError:
        return None


class JoinMixin:
    """Помощники публичной формы: открыть страницу (сессия + CSRF) и «выждать» минимальное время."""

    def open_form(self, url='/join'):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:500])
        with self.client.session_transaction() as s:
            s['join_ts'] = time.time() - 30
        return r

    def submit(self, url='/join', open_first=True, **fields):
        if open_first:
            self.open_form(url)
        data = {'login': '', 'password': 'secret12', 'password2': 'secret12', 'consent': '1'}
        data.update({k: v for k, v in fields.items() if v is not None})
        for k in [k for k, v in fields.items() if v is None]:
            data.pop(k, None)
        return self.post(url, data)


# ==================== ПУБЛИЧНАЯ РЕГИСТРАЦИЯ ====================

class PublicJoinTest(JoinMixin, CmsTestCase):
    login_as_admin = False

    def setUp(self):
        super().setUp()
        from cms import routes_public
        routes_public.reset_limits()

    def test_minimal_registration_by_phone(self):
        r = self.submit(login='8 701 555 44 33')
        self.assertRedirectsTo(r, '/join')
        self.assertIn('done=1', r.headers['Location'])
        email = '77015554433@' + DOMAIN
        user = _auth_user_or_none(email)
        self.assertIsNotNone(user, 'Auth-аккаунт не создан')
        doc = self.doc('users', user.uid)
        self.assertEqual(doc['role'], 'applicant')
        self.assertEqual(doc['login'], '+77015554433')
        self.assertEqual(doc['email'], email)
        self.assertEqual(doc['phone'], '+77015554433')
        self.assertEqual(doc['source'], 'WEB')
        self.assertIs(doc['marketingConsent'], False)
        self.assertEqual(doc['consents']['pd'], True)
        self.assertEqual(doc['consents']['version'], 2)
        self.assertIn('acceptedAt', doc['consents'])
        self.assertIsInstance(doc['createdAt'], datetime)
        self.assertNotIn('referredBy', doc)
        self.assertTrue(set(doc) <= ALLOWED_CREATE_KEYS, set(doc) - ALLOWED_CREATE_KEYS)
        # Страница «Готово» показывает логин в удобном виде и инструкцию
        html = self.client.get(r.headers['Location']).get_data(as_text=True)
        self.assertIn('Аккаунт создан', html)
        self.assertNotIn('built-in method', html)    # ключи текстов не пересекаются с методами dict
        self.assertIn('+7 701 555 44 33', html)
        self.assertIn('«Абитуриент»', html)
        self.assertIn('apps.apple.com', html)
        self.assertIn('play.google.com', html)

    def test_username_with_source_and_referral(self):
        ref = self.doc('users', 'ap_001')['refCode']
        self.assertTrue(ref)
        r = self.submit('/join/STAND1?ref=' + ref.lower(), login='  Aigerim.New ', marketing='1')
        self.assertRedirectsTo(r, '/join/STAND1')
        user = _auth_user_or_none('aigerim.new@' + DOMAIN)
        self.assertIsNotNone(user)
        doc = self.doc('users', user.uid)
        self.assertEqual(doc['login'], 'aigerim.new')
        self.assertNotIn('phone', doc)
        self.assertEqual(doc['source'], 'STAND1')
        self.assertEqual(doc['referredBy'], 'ap_001')
        self.assertIs(doc['marketingConsent'], True)
        html = self.client.get(r.headers['Location']).get_data(as_text=True)
        self.assertIn('aigerim.new', html)
        self.assertIn('+50 Caspian Coin', html)   # приветственные коины активного источника

    def test_unknown_code_is_web_source(self):
        r = self.submit('/join/NOSUCHCODE', login='web.only1')
        self.assertEqual(r.status_code, 302)
        user = _auth_user_or_none('web.only1@' + DOMAIN)
        self.assertEqual(self.doc('users', user.uid)['source'], 'WEB')

    def test_taken_login(self):
        before = len(self.docs('users', role='applicant'))
        r = self.submit(login='+7 701 123 45 67')   # ap_001
        self.assertEqual(r.status_code, 400)
        self.assertIn('уже занят', r.get_data(as_text=True))
        self.assertEqual(len(self.docs('users', role='applicant')), before)

    def test_cyrillic_login_rejected(self):
        r = self.submit(login='Айгерим')
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn('логин латиницей', html)
        self.assertIn('aria-invalid="true"', html)
        self.assertIn('value="Айгерим"', html)      # введённый логин сохраняется
        self.assertNotIn('secret12', html)           # пароль обратно не выводится

    def test_email_login_rejected(self):
        r = self.submit(login='someone@mail.ru')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Email не нужен', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('someone@mail.ru'))

    def test_password_mismatch(self):
        r = self.submit(login='mismatch.user', password='secret12', password2='secret13')
        self.assertEqual(r.status_code, 400)
        self.assertIn('Пароли не совпадают', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('mismatch.user@' + DOMAIN))

    def test_password_rules(self):
        cases = [('12345', 'минимум 6'), ('abiturient123', 'нельзя использовать'), (' secret12', 'пробелом'),
                 ('x' * 65, 'не больше 64')]
        for pw, text in cases:
            with self.subTest(pw=pw):
                r = self.submit(login='pw.rules', password=pw, password2=pw)
                self.assertEqual(r.status_code, 400)
                self.assertIn(text, r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('pw.rules@' + DOMAIN))

    def test_password_not_trimmed(self):
        # пробел внутри пароля допустим и сохраняется как есть
        r = self.submit(login='spaced.pw', password='sec ret12', password2='sec ret12')
        self.assertEqual(r.status_code, 302)
        from cms.core import sign_in_with_password
        token = sign_in_with_password('spaced.pw@' + DOMAIN, 'sec ret12')
        self.assertTrue(token.get('uid'))

    def test_consent_required(self):
        r = self.submit(login='no.consent', consent=None)
        self.assertEqual(r.status_code, 400)
        self.assertIn('Без согласия', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('no.consent@' + DOMAIN))

    def test_honeypot(self):
        r = self.submit(login='bot.user', website='http://spam.example')
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(_auth_user_or_none('bot.user@' + DOMAIN))

    def test_too_fast(self):
        self.client.get('/join')   # join_ts = сейчас
        r = self.post('/join', {'login': 'fast.bot', 'password': 'secret12', 'password2': 'secret12', 'consent': '1'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('слишком быстро', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('fast.bot@' + DOMAIN))

    def test_without_page_open_is_rejected(self):
        # CSRF-токен есть, но форму не открывали (нет join_ts) — как бот
        r = self.submit(open_first=False, login='direct.post')
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(_auth_user_or_none('direct.post@' + DOMAIN))

    def test_csrf_required(self):
        self.open_form()
        r = self.client.post('/join', data={'login': 'csrf.user', 'password': 'secret12', 'password2': 'secret12',
                                            'consent': '1'})
        self.assertEqual(r.status_code, 400)
        self.assertIsNone(_auth_user_or_none('csrf.user@' + DOMAIN))

    def test_rate_limit(self):
        from cms import routes_public
        from cms.core import RateLimiter
        with mock.patch.object(routes_public, '_attempts', RateLimiter(2, 600)):
            for _ in range(2):
                self.assertEqual(self.submit(login='Айгерим').status_code, 400)
            r = self.submit(login='limit.user')
            self.assertEqual(r.status_code, 429)
            self.assertIn('Слишком много попыток', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('limit.user@' + DOMAIN))

    def test_rollback_auth_when_profile_write_fails(self):
        from cms import routes_public

        class _FailingDb:
            def collection(self, name):
                raise RuntimeError('Firestore недоступен')

        self.open_form()
        with mock.patch.object(routes_public, 'db', _FailingDb()):
            r = self.post('/join', {'login': 'rollback.user', 'password': 'secret12', 'password2': 'secret12',
                                    'consent': '1'})
        self.assertEqual(r.status_code, 503)
        self.assertIn('Не удалось создать аккаунт', r.get_data(as_text=True))
        self.assertIsNone(_auth_user_or_none('rollback.user@' + DOMAIN), 'Auth-аккаунт не откатился')

    def test_scan_counter(self):
        before = self.doc('applicant_sources', 'SCHOOL25')['scans']
        fresh = self.app.test_client()
        fresh.get('/join/SCHOOL25')
        fresh.get('/join/SCHOOL25')                 # повтор в течение 30 минут — cookie
        fresh.get('/join/SCHOOL25?lang=kk')
        self.app.test_client().get('/join/SCHOOL25?preview=1')   # предпросмотр админа
        self.app.test_client().get('/join/SCHOOL25', headers={'User-Agent': 'TelegramBot (like TwitterBot)'})
        self.assertEqual(self.doc('applicant_sources', 'SCHOOL25')['scans'], before + 1)
        self.app.test_client().get('/join/ZZZNOPE')
        self.assertIsNone(self.doc('applicant_sources', 'ZZZNOPE'))

    def test_page_ru_kk_and_accessibility(self):
        html = self.client.get('/join/OPENDAY').get_data(as_text=True)
        self.assertIn('Добро пожаловать в Caspian', html)
        self.assertIn('+100 Caspian Coin', html)
        self.assertIn('autocomplete="username"', html)
        self.assertEqual(html.count('autocomplete="new-password"'), 2)
        self.assertIn('for="jn-login"', html)
        self.assertIn('name="website"', html)
        self.assertIn('caspian.test/privacy', html)
        kk = self.client.get('/join/OPENDAY?lang=kk').get_data(as_text=True)
        self.assertIn('lang="kk"', kk)
        self.assertIn('Тіркелу', kk)
        self.assertIn('Талапкер', kk)
        # неактивный источник: регистрация работает, но коинов не обещаем
        flyer = self.client.get('/join/FLYER').get_data(as_text=True)
        self.assertNotIn('Caspian Coin сразу', flyer)

    def test_done_page_requires_registration(self):
        r = self.app.test_client().get('/join?done=1')
        self.assertRedirectsTo(r, '/join')


# ==================== АДМИНКА: АНАЛИТИКА, ЛИДЫ, КАРТОЧКА ====================

class ApplicantsAdminTest(CmsTestCase):

    def test_analytics_renders_on_seed(self):
        html = self.get_ok('/applicants')
        self.assertIn('Аналитика приёма', html)
        self.assertIn('Воронка приёма', html)
        self.assertIn('Стенд в ТРЦ «Keruen City»', html)        # QR-источник с названием
        self.assertIn('Приложение', html)                         # source APP
        self.assertIn('Кампания «День открытых дверей»', html)    # код кампании в source
        self.assertIn('Программное обеспечение', html)            # специальность из каталога
        self.assertIn('id="apDailyData"', html)
        self.assertIn('chart.js@4.4.1', html)
        for days in (90, 180):
            self.assertIn('id="apDailyChart"', self.get_ok('/applicants?days=%d' % days))

    def test_compute_analytics_numbers(self):
        from cms.routes_applicants import compute_analytics, load_snapshot
        from cms.core import today_almaty
        snap = load_snapshot(force=True)
        total = len(snap['leads'])
        self.assertEqual(total, 42)
        stats = compute_analytics(snap, 30, today_almaty())
        self.assertEqual(stats['total'], 42)
        self.assertEqual(sum(r['count'] for r in stats['sources']), 42)
        self.assertEqual(stats['funnel'][0]['count'], 42)
        enrolled = sum(1 for x in snap['leads'] if x['status'] == 'enrolled')
        self.assertEqual(stats['funnel'][-1]['count'], enrolled)
        self.assertEqual(len(stats['daily']), 30)
        stand = next(r for r in stats['qr'] if r['code'] == 'STAND1')
        self.assertEqual(stand['scans'], 214)
        self.assertEqual(stand['registrations'], sum(1 for x in snap['leads'] if x['source'] == 'STAND1'))
        top = stats['referrers'][0]
        self.assertEqual((top['uid'], top['count']), ('ap_001', 3))
        self.assertGreaterEqual(stats['coins']['earned'], stats['coins']['balance'])

    def test_compute_analytics_pure(self):
        from cms.routes_applicants import compute_analytics
        today = datetime(2026, 10, 19).date()
        mk = lambda uid, days, status='new', source='APP', complete=False, specs=(): {  # noqa: E731
            'uid': uid, 'title': uid, 'login': uid, 'created': datetime(2026, 10, 19, 6, tzinfo=timezone.utc) - timedelta(days=days),
            'status': status, 'source': source, 'source_title': source, 'source_kind': 'app', 'source_icon': 'phone',
            'complete': complete, 'specialties': list(specs), 'city': '', 'grade': '', 'name': '', 'phone': '',
            'school': '', 'marketing': False, 'referred_by': '', 'rewarded': 0, 'earned': 0, 'spent': 0, 'balance': 0}
        leads = [mk('a', 0, 'enrolled', complete=True, specs=['ПО']), mk('b', 3, 'applied'), mk('c', 10, 'contacted'),
                 mk('d', 40, 'declined')]
        stats = compute_analytics({'leads': leads, 'sources': {}, 'by_uid': {x['uid']: x for x in leads}}, 30, today)
        self.assertEqual((stats['today'], stats['week'], stats['month']), (1, 2, 3))
        self.assertEqual([f['count'] for f in stats['funnel']], [4, 3, 2, 1])
        self.assertEqual(stats['funnel'][1]['step'], 75.0)
        self.assertEqual(stats['complete_pct'], 25.0)
        self.assertEqual(stats['daily'][-1]['count'], 1)
        self.assertEqual(stats['week_trend'], 100.0)    # 2 против 1 на прошлой неделе
        self.assertEqual(stats['month_trend'], 200.0)
        empty = compute_analytics({'leads': [], 'sources': {}, 'by_uid': {}}, 30, today)
        self.assertEqual((empty['total'], empty['week_trend'], empty['complete_pct']), (0, None, 0.0))

    def test_leads_list_search_filters_pagination(self):
        html = self.get_ok('/applicants/leads')
        self.assertRegex(html, r'>42</b> абитуриента<')        # сводка под заголовком (summary_line)
        self.assertIn('+7 701 123 45 67', self.get_ok('/applicants/leads?per=100'))
        # поиск по телефону в любом формате
        html = self.get_ok('/applicants/leads?q=8 701 123 45 67')
        self.assertIn('/applicants/ap_001', html)
        self.assertIn('1 абитуриент', html)
        # фильтр по источнику и статусу, вкладки статусов с количеством
        html = self.get_ok('/applicants/leads?source=STAND1')
        self.assertIn('Новый', html)
        html = self.get_ok('/applicants/leads?status=enrolled')
        self.assertIn('Поступил', html)
        # пагинация
        html = self.get_ok('/applicants/leads?per=25&page=2')
        self.assertIn('Показано 17 из 42', html)
        # старый аккаунт по email ищется по email
        self.assertIn('old.applicant@mail.ru', self.get_ok('/applicants/leads?q=old.applicant'))
        # пустой результат
        self.assertIn('Никого не нашли', self.get_ok('/applicants/leads?q=nobody-zzz'))

    def test_filter_function(self):
        from cms.routes_applicants import filter_leads, load_snapshot, parse_filters
        leads = load_snapshot(force=True)['leads']
        f = parse_filters({'profile': 'complete', 'sort': 'name'})
        out = filter_leads(leads, f)
        self.assertTrue(out and all(x['complete'] for x in out))
        self.assertEqual([x['title'].lower() for x in out], sorted(x['title'].lower() for x in out))
        f = parse_filters({'period': '7'})
        week = filter_leads(leads, f)
        self.assertTrue(all(x['created'] >= datetime.now(timezone.utc) - timedelta(days=8) for x in week))
        self.assertEqual(parse_filters({'status': 'hacker', 'per': '999'})['status'], '')
        self.assertEqual(parse_filters({'per': '999'})['per'], 25)

    def test_csv_export(self):
        r = self.client.get('/applicants/leads.csv?source=STAND1')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['Content-Type'])
        body = r.get_data(as_text=True)
        self.assertTrue(body.startswith('﻿'))
        rows = list(csv.reader(io.StringIO(body.lstrip('﻿')), delimiter=';'))
        self.assertEqual(rows[0][0], 'UID')
        from cms.routes_applicants import load_snapshot
        expected = sum(1 for x in load_snapshot()['leads'] if x['source'] == 'STAND1')
        self.assertEqual(len(rows) - 1, expected)
        self.assertTrue(all(row[9] == 'STAND1' for row in rows[1:]))
        self.assertTrue(self.audit_entries('export'))

    def test_csv_injection_escaped(self):
        from cms.routes_applicants import csv_cell
        self.assertEqual(csv_cell('=HYPERLINK("http://x")'), '\'=HYPERLINK("http://x")')
        self.assertEqual(csv_cell('+7 701 123 45 67'), "'+7 701 123 45 67")
        self.assertEqual(csv_cell('Актау'), 'Актау')
        self.assertEqual(csv_cell(5), 5)
        self.assertEqual(csv_cell(True), 'да')

    def test_detail_page(self):
        html = self.get_ok('/applicants/ap_001')
        self.assertIn('+7 701 123 45 67', html)
        self.assertIn('Сбросить пароль', html)
        self.assertIn('Пароль станет «abiturient123»', html)   # новый пароль — в тексте подтверждения
        self.assertIn('Пригласил', html)
        self.assertIn('Зачислить в студенты', html)
        self.assertIn('ПО 24-1', html)                 # группы для зачисления
        self.assertEqual(self.client.get('/applicants/no_such_uid').status_code, 404)
        self.assertEqual(self.client.get('/applicants/st_001').status_code, 404)   # студент без CRM — не абитуриент

    def test_detail_xss_escaped(self):
        from cms.core import db
        db.collection('users').document('ap_002').update({'name': '<script>alert(1)</script>'})
        db.collection('applicant_crm').document('ap_002').update({'notes': '<img src=x onerror=alert(2)>'})
        html = self.get_ok('/applicants/ap_002')
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertNotIn('<img src=x onerror', html)

    def test_nav_badge_new_leads(self):
        html = self.get_ok('/applicants/leads')
        from cms.routes_applicants import nav_badges
        n = nav_badges()['applicants_leads']
        self.assertGreater(n, 0)
        self.assertRegex(html, r'Лиды</span>\s*<span class="cc-nav-badge[^"]*">%d<' % n)


class ApplicantActionsTest(CmsTestCase):
    fresh_seed_each_test = False

    def test_reset_password_to_default(self):
        from cms.core import LoginError, sign_in_with_password
        uid = APPLICANT_ACCOUNT['uid']
        r = self.post('/applicants/%s/reset-password' % uid)
        self.assertRedirectsTo(r, '/applicants/' + uid)
        doc = self.doc('users', uid)
        self.assertIs(doc['mustChangePassword'], True)
        self.assertIsInstance(doc['passwordResetAt'], datetime)
        # пароль действительно abiturient123, старый не подходит
        self.assertTrue(sign_in_with_password(APPLICANT_ACCOUNT['email'], 'abiturient123').get('uid'))
        with self.assertRaises(LoginError):
            sign_in_with_password(APPLICANT_ACCOUNT['email'], APPLICANT_ACCOUNT['password'])
        crm = self.doc('applicant_crm', uid)
        self.assertEqual(crm['history'][-1]['action'], 'password_reset')
        self.assertEqual(crm['history'][-1]['by'], ADMIN['email'])
        self.assertTrue(self.audit_entries('reset_password'))
        self.assertIn('Пароль сброшен', self.get_ok('/applicants/' + uid))

    def test_reset_password_without_auth_account(self):
        r = self.post('/applicants/ap_002/reset-password')   # у ap_002 нет Auth-аккаунта в сиде
        self.assertEqual(r.status_code, 302)
        self.assertNotIn('mustChangePassword', self.doc('users', 'ap_002'))
        self.assertIn('error', [c for c, _ in self.flashes()])

    def test_reset_requires_post_and_csrf(self):
        self.assertEqual(self.client.get('/applicants/ap_005/reset-password').status_code, 405)
        r = self.client.post('/applicants/ap_005/reset-password')
        self.assertEqual(r.status_code, 400)

    def test_status_notes_contact(self):
        uid = 'ap_009'
        self.post('/applicants/%s/status' % uid, {'status': 'applied'})
        crm = self.doc('applicant_crm', uid)
        self.assertEqual(crm['status'], 'applied')
        self.assertEqual(crm['updatedBy'], ADMIN['uid'])
        self.assertEqual(crm['history'][-1]['action'], 'status')
        self.post('/applicants/%s/notes' % uid, {'notes': 'Принесёт аттестат в понедельник'})
        self.assertEqual(self.doc('applicant_crm', uid)['notes'], 'Принесёт аттестат в понедельник')
        r = self.post('/applicants/%s/status' % uid, {'status': 'bogus'})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('applicant_crm', uid)['status'], 'applied')
        self.assertTrue(self.audit_entries('status'))

    def test_contact_moves_new_to_contacted(self):
        from cms.routes_applicants import load_snapshot
        uid = next(x['uid'] for x in load_snapshot(force=True)['leads'] if x['status'] == 'new' and not x['converted'])
        self.post('/applicants/%s/contact' % uid, {'channel': 'call', 'comment': 'Придёт на ДОД'})
        crm = self.doc('applicant_crm', uid)
        self.assertEqual(crm['status'], 'contacted')
        self.assertIn('lastContactAt', crm)
        self.assertIn('Звонок: Придёт на ДОД', [h['details'] for h in crm['history']])

    def test_award_and_deduct_coins(self):
        uid = 'ap_003'
        self.post('/applicants/%s/coins' % uid, {'amount': '150', 'reason': 'Олимпиада по информатике'})
        txs = [t for t in self.docs('coin_transactions', uid=uid, type='manual') if t['amount'] == 150]
        self.assertEqual(len(txs), 1)
        self.assertEqual(txs[0]['reason'], 'Олимпиада по информатике')
        self.assertEqual(txs[0]['createdBy'], ADMIN['uid'])
        self.assertIn('createdAt', txs[0])
        self.assertNotIn('applied', txs[0])   # применяет Cloud Function
        balance = self.doc('coin_wallets', uid)['balance']
        self.post('/applicants/%s/coins' % uid, {'amount': str(balance + 1), 'direction': 'minus', 'reason': 'Ошибка'})
        self.assertFalse([t for t in self.docs('coin_transactions', uid=uid) if t['amount'] == -(balance + 1)])
        self.post('/applicants/%s/coins' % uid, {'amount': '10', 'reason': ''})
        self.assertFalse([t for t in self.docs('coin_transactions', uid=uid) if t['amount'] == 10])

    def test_convert_to_student(self):
        uid = 'ap_015'
        r = self.post('/applicants/%s/convert' % uid, {'group': 'ПО 25-1', 'course': '1'})
        self.assertRedirectsTo(r, '/applicants/' + uid)
        doc = self.doc('users', uid)
        self.assertEqual((doc['role'], doc['group'], doc['course'], doc['attendance'], doc['grades']),
                         ('student', 'ПО 25-1', '1', '0%', 'N/A'))
        self.assertEqual(self.doc('applicant_crm', uid)['status'], 'enrolled')
        # зачисленный остаётся в аналитике и в списке (фильтр «Уже студенты»)
        html = self.get_ok('/applicants/leads?kind=converted')
        self.assertIn('/applicants/' + uid, html)
        self.assertIn('студент ПО 25-1', html)
        self.assertIn('Переведён в студенты', self.get_ok('/applicants/' + uid))
        # повторно зачислить нельзя
        self.post('/applicants/%s/convert' % uid, {'group': 'ИС 24-1', 'course': '2'})
        self.assertEqual(self.doc('users', uid)['group'], 'ПО 25-1')

    def test_convert_validation(self):
        self.post('/applicants/ap_016/convert', {'group': '', 'course': '1'})
        self.post('/applicants/ap_016/convert', {'group': 'ПО 25-1', 'course': '7'})
        self.assertEqual(self.doc('users', 'ap_016')['role'], 'applicant')

    def test_save_profile(self):
        uid = 'ap_017'
        self.post('/applicants/%s/profile' % uid, {
            'name': 'Тестова Айгерим', 'phone': '8 (705) 111-22-33', 'city': 'Актау', 'school': 'СШ №8',
            'grade': '11', 'specialties': ['Программное обеспечение', 'Туризм'], 'marketingConsent': '1'})
        doc = self.doc('users', uid)
        self.assertEqual(doc['name'], 'Тестова Айгерим')
        self.assertEqual(doc['phone'], '+77051112233')
        self.assertEqual(doc['specialties'], ['Программное обеспечение', 'Туризм'])
        self.assertIs(doc['marketingConsent'], True)
        self.assertEqual(self.doc('applicant_crm', uid)['history'][-1]['action'], 'profile')
        self.post('/applicants/%s/profile' % uid, {'name': 'Тестова Айгерим', 'phone': '12345'})
        self.assertEqual(self.doc('users', uid)['phone'], '+77051112233')   # неверный телефон не сохранён

    def test_delete_account(self):
        from cms.core import fb_auth
        uid = 'ap_020'    # есть Auth-аккаунт в сиде
        self.post('/applicants/%s/delete' % uid, {'confirm': 'нет'})
        self.assertIsNotNone(self.doc('users', uid))
        r = self.post('/applicants/%s/delete' % uid, {'confirm': 'Удалить'})
        self.assertRedirectsTo(r, '/applicants/leads')
        self.assertIsNone(self.doc('users', uid))
        with self.assertRaises(fb_auth.UserNotFoundError):
            fb_auth.get_user(uid)
        self.assertTrue(self.audit_entries('delete'))
        self.assertNotIn('/applicants/%s"' % uid, self.get_ok('/applicants/leads?per=100'))

    def test_bulk_status_and_coins(self):
        uids = ['ap_030', 'ap_031']
        r = self.post('/applicants/leads/bulk', {'action': 'status', 'status': 'declined', 'uids': uids,
                                                 'next': '/applicants/leads?status=declined'})
        self.assertRedirectsTo(r, '/applicants/leads?status=declined')
        for uid in uids:
            self.assertEqual(self.doc('applicant_crm', uid)['status'], 'declined')
        self.post('/applicants/leads/bulk', {'action': 'coins', 'amount': '25', 'reason': 'Квиз', 'uids': uids + ['ghost']})
        for uid in uids:
            self.assertTrue([t for t in self.docs('coin_transactions', uid=uid, type='manual') if t['amount'] == 25])
        self.assertFalse(self.docs('coin_transactions', uid='ghost'))
        self.assertTrue(self.audit_entries('bulk_award'))
        # без выбранных — ничего не делаем
        r = self.post('/applicants/leads/bulk', {'action': 'status', 'status': 'new'})
        self.assertEqual(r.status_code, 302)


# ==================== CRM: СПИСОК, КАНБАН, КАРТОЧКА, БЫСТРЫЙ ПРОСМОТР (UX v2) ====================

class LeadsCrmUxTest(CmsTestCase):
    fresh_seed_each_test = False

    def action_of_last_flash(self):
        """Служебный flash ('cc-action', json) — кнопка в тосте (flash_action)."""
        import json
        cat, payload = self.flashes()[-1]
        self.assertEqual(cat, 'cc-action')
        return json.loads(payload)

    def test_list_has_inline_status_peek_and_bulkbar(self):
        html = self.get_ok('/applicants/leads?per=100')
        self.assertIn('class="ap-status-form"', html)                 # статус прямо в строке
        self.assertIn('data-autosubmit', html)
        self.assertIn('data-cc-peek', html)                           # быстрый просмотр
        self.assertIn('data-cc-bulkbar="apBulk"', html)               # общая панель выбора
        self.assertIn('name="ids" value="ap_001" form="apBulk"', html)
        self.assertNotIn('data-ap-bulk', html.replace('data-ap-bulk-who', ''))   # своей панели больше нет
        self.assertIn('cc-table--stack', html)
        # ссылка на карточку несёт next — «‹ Лиды» вернёт в тот же список
        self.assertIn('/applicants/ap_001?next=/applicants/leads?per%3D100', html)
        # в шапке одна основная кнопка — нет (Аналитика — light), экспорт и обновление — в меню ⋯
        header = html.split('class="cc-page-header"', 1)[1].split('cc-summary', 1)[0]
        self.assertNotIn('btn-primary', header)
        self.assertIn('Экспорт CSV по фильтру', header)

    def test_inline_status_change_is_undoable(self):
        uid = 'ap_022'
        before = self.doc('applicant_crm', uid)['status']
        target = 'declined' if before != 'declined' else 'new'
        r = self.post('/applicants/%s/status' % uid, {'status': target, 'next': '/applicants/leads?status=new'})
        self.assertRedirectsTo(r, '/applicants/leads?status=new')
        self.assertEqual(self.doc('applicant_crm', uid)['status'], target)
        action = self.action_of_last_flash()
        self.assertEqual(action['label'], 'Отменить')
        self.assertEqual(action['post'], '/applicants/%s/status' % uid)
        self.assertEqual(action['fields']['status'], before)
        self.assertEqual(action['fields']['next'], '/applicants/leads?status=new')
        self.post(action['post'], action['fields'])
        self.assertEqual(self.doc('applicant_crm', uid)['status'], before)

    def test_status_json_for_kanban(self):
        uid = 'ap_023'
        old = self.doc('applicant_crm', uid)['status']
        new = 'applied' if old != 'applied' else 'contacted'
        r = self.post_json('/applicants/%s/status' % uid, {'status': new}, headers={'Accept': 'application/json'})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        data = r.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual((data['status'], data['old']), (new, old))
        self.assertEqual(data['undo']['fields'], {'status': old})
        self.assertEqual(self.doc('applicant_crm', uid)['status'], new)
        r = self.post_json('/applicants/%s/status' % uid, {'status': 'bogus'}, headers={'Accept': 'application/json'})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(r.get_json()['ok'])
        r = self.post_json('/applicants/no_such/status', {'status': 'new'}, headers={'Accept': 'application/json'})
        self.assertEqual(r.status_code, 404)

    def test_board_view(self):
        html = self.get_ok('/applicants/leads?view=board')
        self.assertIn('data-ap-board', html)
        for key in ('new', 'contacted', 'applied', 'enrolled', 'declined'):
            self.assertIn('data-ap-board-list="%s"' % key, html)
        self.assertIn('sortablejs@1.15.2', html)
        self.assertIn('class="ap-card"', html)
        # вид запоминается: cookie → без ?view снова канбан
        self.client.set_cookie('cc_view_applicants_leads', 'board')
        self.assertIn('data-ap-board', self.get_ok('/applicants/leads'))
        self.client.set_cookie('cc_view_applicants_leads', 'table')
        self.assertNotIn('data-ap-board', self.get_ok('/applicants/leads'))

    def test_next_step_save_overdue_filter_done_and_undo(self):
        from cms.core import ALMATY, db
        uid = 'ap_024'
        self.post('/applicants/%s/next-step' % uid, {'next_step': 'Позвонить после ДОД', 'next_step_at': '2026-10-01',
                                                      'action': 'save'})
        crm = self.doc('applicant_crm', uid)
        self.assertEqual(crm['nextStep'], 'Позвонить после ДОД')
        self.assertEqual(crm['nextStepAt'].astimezone(ALMATY).date().isoformat(), '2026-10-01')
        self.assertEqual(crm['history'][-1]['action'], 'next_step')
        if crm.get('status') in ('enrolled', 'declined'):
            db.collection('applicant_crm').document(uid).update({'status': 'contacted'})
        html = self.get_ok('/applicants/leads?step=overdue&per=100')
        self.assertIn('/applicants/%s?' % uid, html)
        self.assertIn('Позвонить после ДОД', html)
        self.assertIn('просрочен', html)
        self.assertNotIn('/applicants/ap_025?', html)
        # неверная дата и пустой шаг — ошибка, данные не тронуты
        self.post('/applicants/%s/next-step' % uid, {'next_step': '', 'next_step_at': '', 'action': 'save'})
        self.post('/applicants/%s/next-step' % uid, {'next_step': 'x', 'next_step_at': '31.02.2026', 'action': 'save'})
        self.assertEqual(self.doc('applicant_crm', uid)['nextStep'], 'Позвонить после ДОД')
        # выполнено: поля удалены, в истории — «Выполнен», в тосте — «Вернуть»
        self.post('/applicants/%s/next-step' % uid, {'action': 'done', 'next': '/applicants/%s' % uid})
        crm = self.doc('applicant_crm', uid)
        self.assertNotIn('nextStep', crm)
        self.assertNotIn('nextStepAt', crm)
        self.assertIn('Выполнен: Позвонить после ДОД', crm['history'][-1]['details'])
        action = self.action_of_last_flash()
        self.assertEqual(action['fields']['next_step'], 'Позвонить после ДОД')
        self.assertEqual(action['fields']['next_step_at'], '2026-10-01')
        self.post(action['post'], action['fields'])
        self.assertEqual(self.doc('applicant_crm', uid)['nextStep'], 'Позвонить после ДОД')
        self.assertTrue(self.audit_entries('next_step'))

    def test_step_helpers(self):
        from datetime import date
        from cms.routes_applicants import next_step_info, step_label
        today = date(2026, 10, 19)
        self.assertEqual(next_step_info({}, today)['state'], 'none')
        self.assertEqual(next_step_info({'nextStep': 'x'}, today)['state'], 'planned')
        at = datetime(2026, 10, 18, 19, 0, tzinfo=timezone.utc)   # 19.10 00:00 Алматы
        self.assertEqual(next_step_info({'nextStepAt': at}, today)['state'], 'today')
        self.assertEqual(next_step_info({'nextStepAt': at - timedelta(days=2)}, today)['state'], 'overdue')
        self.assertEqual(step_label('overdue', date(2026, 10, 17), today), 'просрочен на 2 дня')
        self.assertEqual(step_label('overdue', date(2026, 10, 14), today), 'просрочен на 5 дней')
        self.assertEqual(step_label('planned', date(2026, 10, 20), today), 'завтра')
        self.assertEqual(step_label('planned', date(2026, 10, 25), today), '25.10')

    def test_detail_feed_neighbors_and_layout(self):
        html = self.get_ok('/applicants/ap_001?next=/applicants/leads?sort%3Dname')
        self.assertIn('Лента активности', html)
        self.assertIn('Реферал +30', html)                      # автоматическое начисление — в ленте
        self.assertIn('data-ap-feed-filter="coins"', html)
        self.assertIn('Работа с лидом', html)
        self.assertIn('href="/applicants/leads?sort=name" data-cc-back', html)   # «‹ Лиды» с фильтрами
        self.assertIn('data-ap-next', html)                     # ‹ / › по текущему фильтру
        self.assertRegex(html, r'\d+ из 42')
        self.assertIn('data-cc-ctrl-enter', html)               # Ctrl+Enter в комментарии и заметке
        # одна основная кнопка на карточке — «Записать контакт»
        self.assertEqual(html.count('btn btn-primary'), 1)
        self.assertIn('Записать контакт', html)
        # формы карточки возвращают на карточку (с её ?next)
        self.assertIn('name="next" value="/applicants/ap_001?next=/applicants/leads?sort%3Dname"', html)

    def test_peek_fragment(self):
        r = self.client.get('/applicants/ap_002?next=/applicants/leads?status%3Dnew', headers={'X-CC-Partial': '1'})
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertNotIn('cc-sidebar', html)
        self.assertIn('cc-peek-fragment', html)
        self.assertIn('ap-peek', html)
        self.assertIn('action="/applicants/ap_002/status"', html)
        self.assertIn('action="/applicants/ap_002/contact"', html)
        self.assertIn('action="/applicants/ap_002/notes"', html)
        self.assertIn('Открыть карточку', html)
        # формы панели возвращают в список, из которого её открыли
        self.assertIn('name="next" value="/applicants/leads?status=new"', html)
        self.assertNotIn('Зачислить в студенты', html)          # в панели только быстрые действия

    def test_bulk_accepts_ids_and_exports_selected(self):
        r = self.post('/applicants/leads/bulk', {'action': 'export', 'ids': ['ap_003', 'ap_004', 'ghost']})
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['Content-Type'])
        rows = list(csv.reader(io.StringIO(r.get_data(as_text=True).lstrip('\ufeff')), delimiter=';'))
        self.assertEqual(sorted(row[0] for row in rows[1:]), ['ap_003', 'ap_004'])
        self.assertIn('Следующий шаг', rows[0])
        self.post('/applicants/leads/bulk', {'action': 'status', 'status': 'applied', 'ids': ['ap_026']})
        self.assertEqual(self.doc('applicant_crm', 'ap_026')['status'], 'applied')

    def test_dashboard_neutral_charts_and_details(self):
        html = self.get_ok('/applicants')
        self.assertIn('cc-stat-strip', html)                    # KPI здесь разрешены (§4.3)
        self.assertIn('data-cc-collapse-key="ap-more"', html)   # второстепенное — в «Подробнее»
        self.assertIn('class="is-top"', html)                   # бренд — только у первого места
        self.assertIn('cc-segmented', html)                     # период графика — сегменты
        # «Не указан» (класс) не подсвечивается как лидер, даже если таких больше всех
        unknown = re.search(r'<li[^>]*>\s*<a class="ap-bars__row" href="[^"]*grade=none"', html)
        self.assertIsNotNone(unknown)
        self.assertNotIn('is-top', unknown.group(0))

    def test_bars_never_highlight_unknown(self):
        from flask import render_template_string
        items = [{'key': '', 'label': 'Не указан', 'count': 9, 'width': 100, 'pct': 60.0},
                 {'key': '11', 'label': '11 класс', 'count': 6, 'width': 66, 'pct': 40.0}]
        tpl = "{% import 'applicants/_macros.html' as ap with context %}{{ ap.bars(items, highlight='max') }}"
        with self.app.test_request_context('/'):
            html = render_template_string(tpl, items=items)
        self.assertEqual(html.count('is-top'), 1)
        self.assertRegex(html, r'<li class="is-top">\s*<div class="ap-bars__row">\s*<span class="ap-bars__label" title="11 класс"')


# ==================== QR-ИСТОЧНИКИ ====================

class SourcesTest(CmsTestCase):

    def test_list_page(self):
        html = self.get_ok('/applicants/sources')
        self.assertIn('Стенд в ТРЦ «Keruen City»', html)
        self.assertIn('http://localhost/join/STAND1', html)
        self.assertIn('qrcode-generator@1.4.4', html)
        self.assertIn('data-ap-qr=', html)
        self.assertIn('id="new-source"', html)

    def test_create_validate_duplicate(self):
        r = self.post('/applicants/sources/new', {'code': 'expo26', 'title': 'Выставка «Образование 2026»',
                                                  'channel': 'event', 'welcomeCoins': '40', 'active': '1',
                                                  'note': 'Павильон 3'})
        self.assertRedirectsTo(r, '/applicants/sources')
        doc = self.doc('applicant_sources', 'EXPO26')
        self.assertEqual((doc['title'], doc['channel'], doc['welcomeCoins'], doc['active'], doc['scans'],
                          doc['registrations']), ('Выставка «Образование 2026»', 'event', 40, True, 0, 0))
        self.assertEqual(doc['createdBy'], ADMIN['email'])
        # дубликат, зарезервированный код, неверные значения
        self.post('/applicants/sources/new', {'code': 'EXPO26', 'title': 'Другое', 'channel': 'event'})
        self.assertEqual(self.doc('applicant_sources', 'EXPO26')['title'], 'Выставка «Образование 2026»')
        self.post('/applicants/sources/new', {'code': 'WEB', 'title': 'x', 'channel': 'event'})
        self.assertIsNone(self.doc('applicant_sources', 'WEB'))
        self.post('/applicants/sources/new', {'code': 'BAD-CODE', 'title': 'x'})
        self.assertIsNone(self.doc('applicant_sources', 'BAD-CODE'))
        self.post('/applicants/sources/new', {'code': 'COINS9', 'title': 'x', 'welcomeCoins': '5000'})
        self.assertIsNone(self.doc('applicant_sources', 'COINS9'))
        self.assertTrue(self.audit_entries('create'))
        # новый источник сразу виден публичной странице
        html = self.app.test_client().get('/join/EXPO26').get_data(as_text=True)
        self.assertIn('+40 Caspian Coin', html)

    def test_edit_toggle_delete(self):
        html = self.get_ok('/applicants/sources/INSTA/edit')
        self.assertIn('Instagram @caspian.college', html)
        r = self.post('/applicants/sources/INSTA/edit', {'title': 'Instagram', 'channel': 'social', 'welcomeCoins': '15',
                                                         'note': '', 'active': '1'})
        self.assertRedirectsTo(r, '/applicants/sources')
        self.assertEqual(self.doc('applicant_sources', 'INSTA')['welcomeCoins'], 15)
        r = self.post('/applicants/sources/INSTA/edit', {'title': '', 'channel': 'social', 'welcomeCoins': '1'})
        self.assertEqual(r.status_code, 400)
        self.post('/applicants/sources/INSTA/toggle')
        self.assertIs(self.doc('applicant_sources', 'INSTA')['active'], False)
        # по INSTA есть регистрации — удалить нельзя
        self.post('/applicants/sources/INSTA/delete')
        self.assertIsNotNone(self.doc('applicant_sources', 'INSTA'))
        self.post('/applicants/sources/new', {'code': 'TMP123', 'title': 'Временный', 'channel': 'other'})
        self.post('/applicants/sources/TMP123/delete')
        self.assertIsNone(self.doc('applicant_sources', 'TMP123'))
        self.assertEqual(self.client.get('/applicants/sources/NOPE42/edit').status_code, 404)

    def test_create_errors_reopen_modal_and_toggle_undo(self):
        r = self.post('/applicants/sources/new', {'code': 'BAD-CODE', 'title': 'Мой стенд', 'channel': 'event'})
        self.assertEqual(r.status_code, 400)
        html = r.get_data(as_text=True)
        self.assertIn('data-cc-open-on-load', html)              # модалка откроется сама
        self.assertIn('латинские заглавные буквы', html)
        self.assertIn('value="Мой стенд"', html)                 # введённое не теряется
        r = self.post('/applicants/sources/new', {'code': 'STAND1', 'title': 'Дубль', 'channel': 'event'})
        self.assertEqual(r.status_code, 400)
        self.assertIn('уже есть', r.get_data(as_text=True))
        # вкл/выкл — без подтверждения, с «Отменить»
        import json
        before = self.doc('applicant_sources', 'SCHOOL25')['active']
        self.post('/applicants/sources/SCHOOL25/toggle', {'next': '/applicants/sources'})
        self.assertIs(self.doc('applicant_sources', 'SCHOOL25')['active'], not before)
        cat, payload = self.flashes()[-1]
        action = json.loads(payload)
        self.assertEqual((cat, action['label']), ('cc-action', 'Отменить'))
        self.post(action['post'], action['fields'])
        self.assertIs(self.doc('applicant_sources', 'SCHOOL25')['active'], before)
        html = self.get_ok('/applicants/sources')
        self.assertIn('cc-row-menu', html)
        self.assertIn('data-bs-target="#new-source"', html)
        self.assertIn('data-cc-filter="#apSources"', html)
        self.assertIn('Keruen', self.get_ok('/applicants/sources?q=keruen'))
        self.assertNotIn('Instagram @caspian.college', self.get_ok('/applicants/sources?q=keruen'))

    def test_poster(self):
        html = self.get_ok('/applicants/sources/STAND1/poster')
        self.assertIn('Стань абитуриентом Caspian', html)
        self.assertIn('Caspian талапкері бол', html)
        self.assertIn('+50 Caspian Coin', html)
        self.assertIn('ap-poster-wrap--a4', html)
        self.assertIn('ap-poster-wrap--a5', self.get_ok('/applicants/sources/STAND1/poster?format=a5'))
        self.assertEqual(self.client.get('/applicants/sources/NOPE42/poster').status_code, 404)
