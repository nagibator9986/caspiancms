# tests/test_coins.py — Caspian Coin (магазин, заказы, кампании, журнал), приёмная и новости абитуриентов (COINS).
#
# Контракт с Cloud Functions (SPEC §2.5–2.7, D8): CMS меняет у заказа только статус reserved → issued/cancelled
# (+ issuedAt/issuedBy | cancelledAt/cancelledBy, note, updatedAt) и НЕ пишет в coin_wallets, не создаёт
# purchase/refund-операции; ручная операция — ровно {uid, amount, type:'manual', reason, createdBy, createdAt};
# кампания: id = код ^[A-Z0-9]{4,16}$, claimsCount = 0 при создании и не меняется при правке; приёмная — merge
# в app_config/admissions, specialties = названия каталога; новости абитуриентов — схема news + createdAt.

import csv
import io
import json
import re
import unittest
from datetime import datetime, timezone

from tests.base import CmsTestCase
from tests.seed import ADMIN, _png

MANUAL_KEYS = {'uid', 'amount', 'type', 'reason', 'createdBy', 'createdAt'}


def _img(name='photo.png', color=(255, 98, 0)):
    return (io.BytesIO(_png(64, 48, color)), name)


def _blob_exists(path):
    from cms.core import bucket
    return bucket.blob(path).exists()


def _csv_rows(resp):
    text = resp.get_data(as_text=True)
    assert text.startswith('﻿'), 'CSV без BOM'
    return list(csv.reader(io.StringIO(text.lstrip('﻿')), delimiter=';'))


# ==================== МАГАЗИН ====================

class ShopTest(CmsTestCase):
    fresh_seed_each_test = True

    def product_form(self, **over):
        data = {'title': 'Бутылка для воды', 'titleKk': '', 'description': 'Стальная, 500 мл', 'descriptionKk': '',
                'price': '350', 'stock': '15', 'stock_orig': '', 'limitPerUser': '2', 'variants': '', 'active': '1'}
        data.update(over)
        return {k: v for k, v in data.items() if v is not None}

    def test_list_shows_products_states_and_stats(self):
        html = self.get_ok('/coins/shop')
        for title in ('Худи Caspian College', 'Термокружка', 'Блокнот A5', 'Шоппер'):
            self.assertIn(title, html)
        self.assertIn('Нет в наличии', html)          # prod_notebook: stock 0
        self.assertIn('Скрыт', html)                  # prod_shopper: active false
        self.assertIn('data-cn-sortable', html)       # порядок перетаскиванием
        self.assertIn('ждут: <b>1</b>', html)         # prod_hoodie: ord_001 reserved
        hidden = self.get_ok('/coins/shop?state=hidden')
        self.assertIn('Шоппер', hidden)
        self.assertNotIn('Термокружка', hidden)
        self.assertNotIn('data-cn-sortable', hidden)  # частичный список не сортируем

    def test_create_product_with_photos_and_variants(self):
        r = self.post('/coins/shop/new', dict(self.product_form(variants='S\nM\nm, L\n\n'),
                                              images=[_img('a.png'), _img('b.png', (21, 101, 192))]),
                      content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:500])
        docs = self.docs('coin_products', title='Бутылка для воды')
        self.assertEqual(len(docs), 1)
        p = docs[0]
        self.assertEqual(p['price'], 350)
        self.assertIsInstance(p['price'], int)
        self.assertEqual(p['stock'], 15)
        self.assertEqual(p['limitPerUser'], 2)
        self.assertEqual(p['variants'], ['S', 'M', 'L'])   # без повторов (регистр не важен)
        self.assertIs(p['active'], True)
        self.assertEqual(p['sort'], 6)                     # в конец витрины (в сиде sort 0..5)
        self.assertEqual(len(p['images']), 2)
        self.assertEqual(p['imageUrl'], p['images'][0]['url'])
        for im in p['images']:
            self.assertTrue(im['path'].startswith('coin_products/%s/' % p['id']), im['path'])
            self.assertEqual((im['w'], im['h']), (64, 48))
            self.assertTrue(_blob_exists(im['path']))
            self.assertIn('token=', im['url'])
        self.assertIsNotNone(p.get('createdAt'))
        self.assertTrue(self.audit_entries('create'))
        self.assertRedirectsTo(r, '/coins/shop')

    def test_create_unlimited_and_no_variants(self):
        self.post('/coins/shop/new', self.product_form(title='Стикер', unlimited='1', stock='', variants=''))
        p = self.docs('coin_products', title='Стикер')[0]
        self.assertIsNone(p['stock'])
        self.assertEqual(p['variants'], [])
        self.assertEqual(p['images'], [])
        self.assertIsNone(p['imageUrl'])

    def test_validation_errors_do_not_write(self):
        for bad in ({'price': '0'}, {'price': '100001'}, {'price': '12.5'}, {'title': ''}, {'stock': '-1'},
                    {'variants': 'X' * 41}, {'limitPerUser': '-3'}):
            with self.subTest(bad=bad):
                r = self.post('/coins/shop/new', self.product_form(**bad))
                self.assertEqual(r.status_code, 200)
                self.assertIn('Проверьте поля формы', r.get_data(as_text=True))
        self.assertEqual(self.docs('coin_products', title='Бутылка для воды'), [])

    def test_edit_applies_stock_difference_to_current_value(self):
        from cms.core import db
        images = self.doc('coin_products', 'prod_mug')['images']
        # форма открыта при остатке 5; пока она открыта, функция зарезервировала 1 шт. → 4
        db.collection('coin_products').document('prod_mug').update({'stock': 4})
        self.post('/coins/shop/prod_mug/edit', self.product_form(title='Термокружка', price='300', stock='8',
                                                                 stock_orig='5', limitPerUser='1'))
        self.assertEqual(self.doc('coin_products', 'prod_mug')['stock'], 7)   # 4 + (8 − 5)
        # поле не меняли — текущий остаток не перезаписывается
        db.collection('coin_products').document('prod_mug').update({'stock': 6})
        self.post('/coins/shop/prod_mug/edit', self.product_form(title='Термокружка 2', price='300', stock='7',
                                                                 stock_orig='7', limitPerUser='1'))
        d = self.doc('coin_products', 'prod_mug')
        self.assertEqual(d['stock'], 6)
        self.assertEqual(d['title'], 'Термокружка 2')
        self.assertEqual(d['images'], images)                  # без файлов в форме фото не меняются

    def test_edit_reorders_and_removes_photos(self):
        before = self.doc('coin_products', 'prod_hoodie')
        self.assertEqual(len(before['images']), 2)
        removed = before['images'][0]
        state = [{'kind': 'new', 'idx': 0}, {'kind': 'existing', 'url': before['images'][1]['url']}]
        r = self.post('/coins/shop/prod_hoodie/edit', dict(
            self.product_form(title='Худи', price='800', stock='12', stock_orig='12', variants='S\nM'),
            images__state=json.dumps(state), images=_img('new.png')), content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302)
        after = self.doc('coin_products', 'prod_hoodie')
        self.assertEqual(len(after['images']), 2)
        self.assertEqual(after['images'][1]['url'], before['images'][1]['url'])
        self.assertEqual(after['imageUrl'], after['images'][0]['url'])
        self.assertFalse(_blob_exists(removed['path']))
        self.assertTrue(_blob_exists(after['images'][0]['path']))

    def test_toggle_bulk_and_stock(self):
        self.post('/coins/shop/prod_shopper/toggle')
        self.assertIs(self.doc('coin_products', 'prod_shopper')['active'], True)
        self.post('/coins/shop/bulk', {'action': 'hide', 'ids': ['prod_mug', 'prod_tshirt', 'nope']})
        self.assertIs(self.doc('coin_products', 'prod_mug')['active'], False)
        self.assertIs(self.doc('coin_products', 'prod_tshirt')['active'], False)
        # склад: нельзя уйти в минус, пополнение, «без лимита»
        self.post('/coins/shop/prod_mug/stock', {'delta': '-6'})
        self.assertEqual(self.doc('coin_products', 'prod_mug')['stock'], 5)
        self.assertIn('Нельзя списать', ' '.join(t for _, t in self.flashes()))
        self.post('/coins/shop/prod_mug/stock', {'delta': '5'})
        self.assertEqual(self.doc('coin_products', 'prod_mug')['stock'], 10)
        self.post('/coins/shop/prod_stickers/stock', {'delta': '20'})   # был «без лимита» → 0 + 20
        self.assertEqual(self.doc('coin_products', 'prod_stickers')['stock'], 20)
        self.post('/coins/shop/prod_mug/stock', {'mode': 'unlimited'})
        self.assertIsNone(self.doc('coin_products', 'prod_mug')['stock'])
        self.assertTrue(self.audit_entries('stock'))

    def test_delete_blocked_when_product_has_orders(self):
        r = self.post('/coins/shop/prod_hoodie/delete')
        self.assertEqual(r.status_code, 302)
        self.assertIsNotNone(self.doc('coin_products', 'prod_hoodie'))
        self.assertIn('есть заказы', ' '.join(t for _, t in self.flashes()))

    def test_delete_removes_doc_and_photos(self):
        self.post('/coins/shop/new', dict(self.product_form(title='Удаляемый'), images=_img()),
                  content_type='multipart/form-data')
        p = self.docs('coin_products', title='Удаляемый')[0]
        path = p['images'][0]['path']
        self.assertTrue(_blob_exists(path))
        self.post('/coins/shop/%s/delete' % p['id'])
        self.assertIsNone(self.doc('coin_products', p['id']))
        self.assertFalse(_blob_exists(path))
        self.assertTrue(self.audit_entries('delete'))

    def test_reorder_json(self):
        order = ['prod_shopper', 'prod_notebook', 'prod_stickers', 'prod_mug', 'prod_tshirt', 'prod_hoodie']
        r = self.post_json('/coins/shop/reorder', {'ids': order + ['ghost']})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()['ok'])
        for pos, pid in enumerate(order):
            self.assertEqual(self.doc('coin_products', pid)['sort'], pos)
        self.assertEqual(self.post_json('/coins/shop/reorder', {'ids': []}).status_code, 400)
        # без CSRF-заголовка — отказ
        r = self.client.post('/coins/shop/reorder', json={'ids': order})
        self.assertEqual(r.status_code, 400)

    def test_clearing_optional_fields_removes_them(self):
        self.post('/coins/shop/prod_hoodie/edit', self.product_form(title='Худи', price='800', stock='12',
                                                                    stock_orig='12', description=''))
        d = self.doc('coin_products', 'prod_hoodie')
        self.assertNotIn('description', d)   # не null, а удалено
        self.assertNotIn('titleKk', d)
        self.post('/coins/shop/new', self.product_form(title='Без описаний', description=''))
        d = self.docs('coin_products', title='Без описаний')[0]
        self.assertFalse({'titleKk', 'description', 'descriptionKk'} & set(d))

    def test_user_data_is_escaped(self):
        from cms.core import db
        db.collection('coin_products').document('prod_mug').update({'title': '<img src=x onerror=alert(1)>'})
        db.collection('users').document('ap_006').update({'name': '<script>alert(1)</script>'})
        html = self.get_ok('/coins/shop')
        self.assertNotIn('<img src=x onerror', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html)
        html = self.get_ok('/coins/orders?status=all')
        self.assertNotIn('<script>alert(1)</script>', html)

    def test_product_form_pages(self):
        self.get_ok('/coins/shop/new')
        html = self.get_ok('/coins/shop/prod_hoodie/edit')
        self.assertIn('Худи Caspian College', html)
        self.assertIn('data-cc-uploader', html)
        self.assertIn('name="stock_orig" value="12"', html)
        self.assertEqual(self.client.get('/coins/shop/nope/edit').status_code, 302)


# ==================== ЗАКАЗЫ ====================

class OrdersTest(CmsTestCase):
    fresh_seed_each_test = True

    def wallets(self):
        return {d['id']: d for d in self.docs('coin_wallets')}

    def test_default_tab_is_reserved_queue(self):
        html = self.get_ok('/coins/orders')
        for oid in ('ord_001', 'ord_002', 'ord_003'):
            self.assertIn('o-' + oid, html)
        self.assertNotIn('o-ord_004', html)   # выдан
        self.assertIn('cnOrdersBulk', html)
        # очередь: сначала самые давние (ord_001 — 2 дня назад, ord_003 — сегодня)
        self.assertLess(html.index('o-ord_001'), html.index('o-ord_003'))
        all_html = self.get_ok('/coins/orders?status=all')
        for oid in ('ord_004', 'ord_006', 'ord_007'):
            self.assertIn('o-' + oid, all_html)
        self.assertIn('не хватило коинов', self.get_ok('/coins/orders?status=rejected'))

    def test_search_and_filters(self):
        html = self.get_ok('/coins/orders?status=all&q=термокружка')
        self.assertIn('o-ord_002', html)
        self.assertIn('o-ord_006', html)
        self.assertNotIn('o-ord_001', html)
        html = self.get_ok('/coins/orders?status=all&product=prod_hoodie')
        self.assertIn('o-ord_001', html)
        self.assertIn('o-ord_007', html)
        self.assertNotIn('o-ord_002', html)
        html = self.get_ok('/coins/orders?status=all&uid=ap_003')
        self.assertIn('o-ord_001', html)
        self.assertNotIn('o-ord_002', html)
        self.assertIn('показаны заказы этого абитуриента', html)

    def test_issue_changes_only_status_fields(self):
        wallets = self.wallets()
        txs = len(self.docs('coin_transactions'))
        product = self.doc('coin_products', 'prod_hoodie')
        r = self.post('/coins/orders/ord_001/issue', {'note': 'Выдано на стенде', 'next': '/coins/orders'})
        self.assertRedirectsTo(r, '/coins/orders')
        o = self.doc('coin_orders', 'ord_001')
        self.assertEqual(o['status'], 'issued')
        self.assertEqual(o['issuedBy'], ADMIN['uid'])
        self.assertIsNotNone(o['issuedAt'])
        self.assertEqual(o['note'], 'Выдано на стенде')
        self.assertEqual(o['price'], 800)                       # снимок не тронут
        self.assertEqual(self.wallets(), wallets)               # кошельки пишет только функция
        self.assertEqual(len(self.docs('coin_transactions')), txs)
        self.assertEqual(self.doc('coin_products', 'prod_hoodie')['stock'], product['stock'])
        self.assertEqual(self.audit_entries('issue')[0]['entityId'], 'ord_001')
        # повторно — не меняется
        self.post('/coins/orders/ord_001/issue')
        self.assertIn('уже выдан', ' '.join(t for _, t in self.flashes()))

    def test_cancel_does_not_refund_itself(self):
        wallets = self.wallets()
        self.post('/coins/orders/ord_002/cancel', {'note': 'Передумал'})
        o = self.doc('coin_orders', 'ord_002')
        self.assertEqual(o['status'], 'cancelled')
        self.assertEqual(o['cancelledBy'], ADMIN['uid'])
        self.assertIsNotNone(o['cancelledAt'])
        self.assertIsNone(self.doc('coin_transactions', 'refund_ord_002'))   # возврат — дело функции
        self.assertEqual(self.wallets(), wallets)
        # не reserved — отказ
        self.post('/coins/orders/ord_004/cancel')
        self.assertEqual(self.doc('coin_orders', 'ord_004')['status'], 'issued')
        self.post('/coins/orders/ord_007/issue')
        self.assertEqual(self.doc('coin_orders', 'ord_007')['status'], 'rejected')

    def test_note_set_and_clear(self):
        self.post('/coins/orders/ord_005/note', {'note': '  размер уточнить  '})
        self.assertEqual(self.doc('coin_orders', 'ord_005')['note'], 'размер уточнить')
        self.post('/coins/orders/ord_005/note', {'note': ''})
        self.assertNotIn('note', self.doc('coin_orders', 'ord_005'))
        self.post('/coins/orders/ord_005/note', {'note': 'x' * 501})
        self.assertNotIn('note', self.doc('coin_orders', 'ord_005'))

    def test_bulk_issue_skips_processed(self):
        r = self.post('/coins/orders/bulk', {'action': 'issue', 'ids': ['ord_001', 'ord_003', 'ord_004']})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(self.doc('coin_orders', 'ord_001')['status'], 'issued')
        self.assertEqual(self.doc('coin_orders', 'ord_003')['status'], 'issued')
        self.assertEqual(self.doc('coin_orders', 'ord_004')['status'], 'issued')
        msg = ' '.join(t for _, t in self.flashes())
        self.assertIn('Выдано заказов: 2', msg)
        self.assertIn('пропущено 1', msg)

    def test_sidebar_badge_follows_issue(self):
        self.assertRegex(self.get_ok('/coins/shop'), r'Заказы</span>\s*<span class="cc-nav-badge[^"]*">3<')
        self.post('/coins/orders/ord_001/issue')
        self.assertRegex(self.get_ok('/coins/shop'), r'Заказы</span>\s*<span class="cc-nav-badge[^"]*">2<')

    def test_csv_export_is_safe(self):
        from cms.core import db
        db.collection('coin_orders').document('ord_002').update({'note': '=HYPERLINK("http://x")'})
        r = self.client.get('/coins/orders.csv?status=all')
        self.assertEqual(r.status_code, 200)
        rows = _csv_rows(r)
        self.assertEqual(rows[0][0], 'Заказ')
        self.assertEqual(len(rows) - 1, 7)
        note = [row for row in rows if row[0] == 'ord_002'][0][12]
        self.assertTrue(note.startswith("'="), note)


# ==================== КАМПАНИИ ====================

class CampaignsTest(CmsTestCase):
    fresh_seed_each_test = True

    def form(self, **over):
        data = {'code': 'QUIZ27', 'title': 'Квиз по математике', 'amount': '40', 'startsAt': '2026-11-01T10:00',
                'endsAt': '2026-11-02T18:30', 'maxClaims': '50', 'place': 'Ауд. 204', 'active': '1'}
        data.update(over)
        return {k: v for k, v in data.items() if v is not None}

    def test_list_states(self):
        html = self.get_ok('/coins/campaigns')
        for code in ('DOD2026', 'QUIZ2026', 'SPRING25'):
            self.assertIn('c-' + code, html)
        self.assertIn('Идёт', html)
        self.assertIn('Выключена', html)
        self.assertIn('Завершена', html)
        self.assertIn('c-QUIZ2026', self.get_ok('/coins/campaigns?state=inactive'))
        self.assertNotIn('c-DOD2026', self.get_ok('/coins/campaigns?q=весенняя'))

    def test_create_normalizes_code_and_sets_claims_zero(self):
        # кириллические «О» и «С» → латиница, пробелы и дефисы убираются, регистр — верхний
        r = self.post('/coins/campaigns/new', self.form(code=' ос-2026 '))
        self.assertRedirectsTo(r, '/coins/campaigns/OC2026')
        c = self.doc('coin_campaigns', 'OC2026')
        self.assertEqual(c['claimsCount'], 0)
        self.assertEqual(c['amount'], 40)
        self.assertEqual(c['maxClaims'], 50)
        self.assertIs(c['active'], True)
        self.assertEqual(c['createdBy'], ADMIN['uid'])
        # ввод — время Алматы (UTC+5)
        self.assertEqual(c['startsAt'].astimezone(timezone.utc).replace(tzinfo=None), datetime(2026, 11, 1, 5, 0))
        self.assertEqual(c['endsAt'].astimezone(timezone.utc).replace(tzinfo=None), datetime(2026, 11, 2, 13, 30))
        self.assertTrue(any(e.get('entityId') == 'OC2026' for e in self.audit_entries('create')))

    def test_create_generates_code_when_empty(self):
        self.post('/coins/campaigns/new', self.form(code='', title='Сгенерированный'))
        docs = self.docs('coin_campaigns', title='Сгенерированный')
        self.assertEqual(len(docs), 1)
        self.assertRegex(docs[0]['id'], r'^[ABCDEFGHJKLMNPQRSTUVWXYZ23456789]{6}$')

    def test_duplicate_and_invalid_codes(self):
        r = self.post('/coins/campaigns/new', self.form(code='DOD2026'))
        self.assertEqual(r.status_code, 200)
        self.assertIn('уже занят', r.get_data(as_text=True))
        self.assertEqual(self.doc('coin_campaigns', 'DOD2026')['claimsCount'], 14)
        self.assertEqual(self.doc('coin_campaigns', 'DOD2026')['title'], 'День открытых дверей')
        for bad in ({'code': 'AB1'}, {'code': 'ДОД2026'}, {'code': 'A' * 17}, {'amount': '0'},
                    {'endsAt': '2026-10-31T10:00'}, {'title': ''}, {'maxClaims': '-1'}):
            with self.subTest(bad=bad):
                r = self.post('/coins/campaigns/new', self.form(**bad))
                self.assertEqual(r.status_code, 200)
                self.assertIn('Проверьте поля формы', r.get_data(as_text=True))
        self.assertIsNone(self.doc('coin_campaigns', 'QUIZ27'))

    def test_edit_never_touches_claims_count(self):
        r = self.post('/coins/campaigns/DOD2026/edit', self.form(code='HACK', title='ДОД — обновлено', amount='120',
                                                                  claimsCount='0'))
        self.assertRedirectsTo(r, '/coins/campaigns/DOD2026')
        c = self.doc('coin_campaigns', 'DOD2026')
        self.assertEqual(c['claimsCount'], 14)
        self.assertEqual(c['title'], 'ДОД — обновлено')
        self.assertEqual(c['amount'], 120)
        self.assertIsNone(self.doc('coin_campaigns', 'HACK'))

    def test_toggle_and_delete_rules(self):
        self.post('/coins/campaigns/QUIZ2026/toggle')
        self.assertIs(self.doc('coin_campaigns', 'QUIZ2026')['active'], True)
        self.post('/coins/campaigns/DOD2026/delete')
        self.assertIsNotNone(self.doc('coin_campaigns', 'DOD2026'))   # уже вводили — только выключить
        self.post('/coins/campaigns/QUIZ2026/delete')
        self.assertIsNone(self.doc('coin_campaigns', 'QUIZ2026'))
        self.assertEqual(self.client.post('/coins/campaigns/bad code/toggle',
                                          data={'csrf_token': self.csrf()}).status_code, 404)

    def test_detail_present_and_export(self):
        from cms.core import db
        db.collection('coin_claims').document('DOD2026_ap_002').set({'status': 'accepted', 'amount': 100}, merge=True)
        db.collection('coin_claims').document('DOD2026_x').set({
            'uid': 'ap_009', 'code': 'DOD2026', 'status': 'rejected', 'reason': 'limit',
            'createdAt': datetime.now(timezone.utc)})
        html = self.get_ok('/coins/campaigns/DOD2026')
        self.assertIn('DOD2026', html)
        # вводы из сида без статуса, но с операцией claim_<id> — засчитаны
        self.assertRegex(html, r'Начислено <span class="cc-chip__count">1[0-9]</span>')
        self.assertIn('лимит участников исчерпан', html)
        present = self.get_ok('/coins/campaigns/DOD2026/present')
        self.assertIn('cn-present-page', present)
        self.assertIn('data-cn-qr="http://127.0.0.1/join"', present)   # registerUrl из приёмной
        self.assertIn('qrcode-generator@1.4.4', present)
        self.assertIn('/join/STAND1', self.get_ok('/coins/campaigns/DOD2026/present?src=STAND1'))
        rows = _csv_rows(self.client.get('/coins/campaigns/DOD2026/claims.csv'))
        self.assertEqual(rows[0][:2], ['Дата', 'ФИО'])
        self.assertGreaterEqual(len(rows) - 1, 2)
        self.get_ok('/coins/campaigns/new')
        self.get_ok('/coins/campaigns/DOD2026/edit')
        self.assertEqual(self.client.get('/coins/campaigns/NOPE99').status_code, 404)


# ==================== ЖУРНАЛ И РУЧНЫЕ ОПЕРАЦИИ ====================

class JournalTest(CmsTestCase):
    fresh_seed_each_test = True

    def manual_docs(self):
        return [d for d in self.docs('coin_transactions', type='manual') if not d['id'].startswith('manual_demo')]

    def test_list_filters_and_search(self):
        html = self.get_ok('/coins/journal')
        self.assertIn('Победа в олимпиаде', html)
        html = self.get_ok('/coins/journal?type=purchase')
        self.assertIn('Худи Caspian College', html)
        self.assertNotIn('Победа в олимпиаде', html)
        html = self.get_ok('/coins/journal?q=%2B7%20701%20123%2045%2067')   # логин-телефон ap_001
        self.assertIn('Друг заполнил профиль', html)                        # referral_* у ap_001
        self.assertNotIn('Победа в олимпиаде', html)
        html = self.get_ok('/coins/journal?uid=ap_003')
        self.assertIn('Баланс', html)
        self.assertIn('Победа в олимпиаде', html)
        self.assertIn('Никого не нашли', self.get_ok('/coins/journal?q=zzzzzz-no-such'))

    def test_error_status_filter(self):
        from cms.core import db
        db.collection('coin_transactions').document('bad_tx').set({'uid': 'ap_002', 'amount': 0, 'type': 'manual',
                                                                    'reason': 'Битая операция', 'applied': False,
                                                                    'error': 'invalid',
                                                                    'createdAt': datetime.now(timezone.utc)})
        html = self.get_ok('/coins/journal?status=error')
        self.assertIn('Битая операция', html)
        self.assertNotIn('Победа в олимпиаде', html)

    def test_award_creates_exact_manual_transaction(self):
        wallets = {d['id']: d for d in self.docs('coin_wallets')}
        r = self.post('/coins/journal/award', {'recipients': '+7 701 123 45 67', 'amount': '25',
                                               'direction': 'plus', 'reason': 'Помощь на стенде',
                                               'next': '/coins/journal'})
        self.assertRedirectsTo(r, '/coins/journal')
        docs = self.manual_docs()
        self.assertEqual(len(docs), 1)
        t = docs[0]
        self.assertEqual(set(t) - {'id'}, MANUAL_KEYS)
        self.assertEqual((t['uid'], t['amount'], t['type'], t['reason'], t['createdBy']),
                         ('ap_001', 25, 'manual', 'Помощь на стенде', ADMIN['uid']))
        self.assertEqual({d['id']: d for d in self.docs('coin_wallets')}, wallets)   # кошелёк — только функция
        hist = self.doc('applicant_crm', 'ap_001')['history']
        self.assertEqual(hist[-1]['action'], 'coins')
        self.assertTrue(self.audit_entries('award'))

    def test_award_minus_and_multi(self):
        rich = sorted((d for d in self.docs('coin_wallets') if d.get('balance', 0) >= 20), key=lambda d: d['id'])[0]
        uid, balance = rich['id'], rich['balance']
        self.post('/coins/journal/award', {'recipients': uid, 'amount': '10', 'direction': 'minus',
                                           'reason': 'Корректировка'})
        self.assertEqual([(d['uid'], d['amount']) for d in self.manual_docs()], [(uid, -10)])
        # больше баланса — отказ
        self.post('/coins/journal/award', {'recipients': uid, 'amount': str(balance + 1), 'direction': 'minus',
                                           'reason': 'Слишком много'})
        self.assertEqual(len(self.manual_docs()), 1)
        # несколько получателей: логин, телефон с восьмёркой, uid; повтор не задваивает
        self.post('/coins/journal/award', {'recipients': 'alikhan.k3\n8 701 123 45 67, ap_002\nap_002',
                                           'amount': '5', 'reason': 'Квиз'})
        quiz = [d for d in self.manual_docs() if d['reason'] == 'Квиз']
        self.assertEqual(sorted(d['uid'] for d in quiz), ['ap_001', 'ap_002', 'ap_004'])

    def test_award_rejections(self):
        cases = [
            {'recipients': 'nobody.here', 'amount': '5', 'reason': 'x'},           # не найден
            {'recipients': 'st_001', 'amount': '5', 'reason': 'x'},                # студент без кошелька
            {'recipients': 'ap_001', 'amount': '0', 'reason': 'x'},
            {'recipients': 'ap_001', 'amount': '100001', 'reason': 'x'},
            {'recipients': 'ap_001', 'amount': '1.5', 'reason': 'x'},
            {'recipients': 'ap_001', 'amount': '5', 'reason': ''},
            {'recipients': '', 'amount': '5', 'reason': 'x'},
        ]
        for data in cases:
            with self.subTest(data=data):
                r = self.post('/coins/journal/award', data)
                self.assertEqual(r.status_code, 302)
                self.assertIn('error', [c for c, _ in self.flashes()])
        self.assertEqual(self.manual_docs(), [])

    def test_csv_export(self):
        rows = _csv_rows(self.client.get('/coins/journal.csv?type=manual'))
        self.assertEqual(rows[0][:3], ['Дата', 'Тип', 'Коины'])
        amounts = sorted(int(r[2]) for r in rows[1:])
        self.assertEqual(amounts, [-20, 200])


# ==================== ПРИЁМНАЯ ====================

class AdmissionsTest(CmsTestCase):
    fresh_seed_each_test = True

    def base_form(self, **over):
        data = {
            'phone': '+7 7292 30-30-30', 'whatsapp': '+77015550199', 'email': 'priem@caspian.test',
            'address': 'Актау', 'privacyUrl': 'https://caspian.test/privacy', 'about': 'О колледже',
            'aboutKk': 'Колледж туралы', 'registerUrl': '', 'appStoreUrl': '', 'playStoreUrl': '',
            'deadline': '2027-06-30T18:00', 'deadlineTitle': 'Приём до', 'deadlineTitleKk': '',
            'quest_profile': '60', 'quest_referral': '35', 'quest_referralCap': '0',
            'cat_key': ['po', 'kNEW'], 'cat_id': ['po', ''], 'cat_imageUrl': ['', ''],
            'cat_title': ['Программное обеспечение', 'Дизайн интерьера'], 'cat_titleKk': ['Бағдарламалық', ''],
            'cat_description': ['', 'Новая специальность'], 'cat_descriptionKk': ['', ''],
            'cat_duration': ['3 г. 10 мес.', ''], 'cat_base': ['9,11', 'bogus'], 'cat_form': ['очная', ''],
            'cat_language': ['', ''], 'cat_places': ['50', ''], 'cat_price': ['', ''],
            'doc_title': ['Аттестат', ''], 'doc_titleKk': ['', ''], 'doc_note': ['Оригинал', ''],
            'faq_q': ['Есть общежитие?'], 'faq_qKk': [''], 'faq_a': ['Да'], 'faq_aKk': [''],
        }
        data.update(over)
        return data

    def test_page_shows_current_settings(self):
        html = self.get_ok('/admissions')
        for text in ('Программное обеспечение', 'Туризм', 'Фото 3×4', 'Есть ли общежитие?', 'Готовность',
                     'data-cn-repeater="cat"', 'priem@caspian.test'):
            self.assertIn(text, html)

    def test_save_merges_and_syncs_specialties(self):
        from cms.core import db
        db.collection('app_config').document('admissions').update({'legacyField': 'keep me'})
        r = self.post('/admissions', self.base_form())
        self.assertRedirectsTo(r, '/admissions')
        d = self.doc('app_config', 'admissions')
        self.assertEqual(d['legacyField'], 'keep me')                    # merge, чужие поля живы
        self.assertEqual(d['specialties'], ['Программное обеспечение', 'Дизайн интерьера'])
        self.assertEqual([c['title'] for c in d['catalog']], d['specialties'])
        po, new = d['catalog']
        self.assertEqual(po['id'], 'po')
        self.assertEqual(po['places'], 50)
        self.assertEqual(po['base'], '9,11')
        self.assertNotIn('places', new)
        self.assertNotIn('base', new)                                    # мусор в select отброшен
        self.assertRegex(new['id'], r'^[a-z0-9_]+$')
        self.assertEqual(d['quests'], {'profile': 60, 'referral': 35, 'referralCap': 0})
        self.assertEqual(d['documents'], [{'title': 'Аттестат', 'note': 'Оригинал'}])
        self.assertEqual(d['faq'], [{'q': 'Есть общежитие?', 'a': 'Да'}])
        self.assertEqual(d['registerUrl'], 'http://localhost/join')       # по умолчанию <сайт>/join
        self.assertEqual(d['deadline'].astimezone(timezone.utc).replace(tzinfo=None), datetime(2027, 6, 30, 13, 0))
        self.assertEqual(d['updatedBy'], ADMIN['uid'])
        self.assertTrue(self.audit_entries('update'))

    def test_clear_deadline_removes_field(self):
        self.post('/admissions', self.base_form(deadline=''))
        self.assertNotIn('deadline', self.doc('app_config', 'admissions'))

    def test_validation_keeps_document(self):
        before = self.doc('app_config', 'admissions')
        for bad in ({'privacyUrl': 'javascript:alert(1)'}, {'email': 'no-at'},
                    {'faq_q': ['Вопрос без ответа'], 'faq_a': ['']},
                    {'quest_profile': '-1'}, {'cat_title': ['', 'Без названия?'], 'cat_duration': ['', '2 года']},
                    {'cat_title': ['Повтор', 'повтор']}, {'cat_places': ['много', '']}):
            with self.subTest(bad=bad):
                r = self.post('/admissions', self.base_form(**bad))
                self.assertEqual(r.status_code, 200)
                self.assertIn('Проверьте поля формы', r.get_data(as_text=True))
        self.assertEqual(self.doc('app_config', 'admissions'), before)

    def test_catalog_image_upload_and_remove(self):
        data = self.base_form()
        data['cat_image__po'] = _img('po.png')
        r = self.post('/admissions', data, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 302, r.get_data(as_text=True)[:800])
        url = self.doc('app_config', 'admissions')['catalog'][0]['imageUrl']
        from cms.core import storage_path_from_url
        path = storage_path_from_url(url)
        self.assertTrue(path.startswith('admissions/catalog/po/'), path)
        self.assertTrue(_blob_exists(path))
        # без новых файлов картинка сохраняется
        data = self.base_form(cat_imageUrl=[url, ''])
        self.post('/admissions', data)
        self.assertEqual(self.doc('app_config', 'admissions')['catalog'][0]['imageUrl'], url)
        # чужой URL подставить нельзя
        self.post('/admissions', self.base_form(cat_imageUrl=['https://evil.example/x.png', '']))
        self.assertNotIn('imageUrl', self.doc('app_config', 'admissions')['catalog'][0])
        self.assertFalse(_blob_exists(path))                            # убранная картинка удалена


# ==================== НОВОСТИ АБИТУРИЕНТОВ ====================

class ApplicantNewsTest(CmsTestCase):
    fresh_seed_each_test = True

    def test_list_and_filters(self):
        html = self.get_ok('/applicants/news')
        for title in ('Старт приёма документов 2026', 'Как получить коины за профиль'):
            self.assertIn(title, html)
        html = self.get_ok('/applicants/news?photo=with')
        self.assertIn('Старт приёма документов 2026', html)
        self.assertNotIn('Как получить коины за профиль', html)
        self.assertNotIn('Старт приёма', self.get_ok('/applicants/news?q=коины'))

    def test_create_with_photos_has_app_schema(self):
        r = self.post('/applicants/news/new', {
            'title': 'День открытых дверей', 'description': 'Суббота, 11:00', 'content': 'Ждём всех!',
            'category': 'cultural', 'images': [_img('1.png'), _img('2.png', (46, 125, 50))]},
            content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/applicants/news')
        n = self.docs('applicant_news', title='День открытых дверей')[0]
        self.assertEqual((n['content'], n['description'], n['category'], n['groupId']),
                         ('Ждём всех!', 'Суббота, 11:00', 'cultural', 'all'))
        self.assertIsNotNone(n['createdAt'])
        self.assertIsNotNone(n['updatedAt'])
        self.assertEqual(len(n['images']), 2)
        self.assertEqual(n['imageUrl'], n['images'][0]['url'])
        self.assertTrue(all(im['path'].startswith('applicant_news/%s/' % n['id']) for im in n['images']))

    def test_validation(self):
        for bad in ({'title': ''}, {'title': 'x' * 201}, {'category': 'politics'}, {'content': '', 'description': ''}):
            with self.subTest(bad=bad):
                data = {'title': 'Ок', 'description': '', 'content': 'Текст', 'category': 'academic'}
                data.update(bad)
                r = self.post('/applicants/news/new', data)
                self.assertEqual(r.status_code, 200)
        self.assertEqual(self.docs('applicant_news', title='Ок'), [])

    def test_edit_and_delete_remove_files(self):
        before = self.doc('applicant_news', 'an_0')
        path = before['images'][0]['path']
        self.assertTrue(_blob_exists(path))
        self.post('/applicants/news/an_0/edit', {'title': 'Старт приёма — обновлено', 'description': '',
                                                 'content': 'Текст', 'category': 'academic',
                                                 'images__state': '[]'})
        after = self.doc('applicant_news', 'an_0')
        self.assertEqual(after['title'], 'Старт приёма — обновлено')
        self.assertEqual(after['images'], [])
        self.assertIsNone(after['imageUrl'])
        self.assertEqual(after['createdAt'], before['createdAt'])
        self.assertFalse(_blob_exists(path))
        self.post('/applicants/news/an_1/delete')
        self.assertIsNone(self.doc('applicant_news', 'an_1'))
        self.post('/applicants/news/bulk', {'action': 'delete', 'ids': ['an_0', 'an_2']})
        self.assertEqual(self.docs('applicant_news'), [])
        self.get_ok('/applicants/news/new')
        self.assertEqual(self.client.get('/applicants/news/nope/edit').status_code, 302)


# ==================== ЧИСТЫЕ ФУНКЦИИ ====================

class PureHelpersTest(unittest.TestCase):

    def test_normalize_code_matches_app(self):
        from cms.routes_coins import is_valid_code, normalize_code
        self.assertEqual(normalize_code(' dod-2026 '), 'DOD2026')
        self.assertEqual(normalize_code('АВЕ_КМН.ОРС тху'), 'ABEKMHOPCTXY')
        self.assertEqual(normalize_code('Дод2026'), 'ДOД2026')          # «Д» не латиница — остаётся
        self.assertFalse(is_valid_code(normalize_code('Дод2026')))
        self.assertTrue(is_valid_code('AB12'))
        self.assertFalse(is_valid_code('AB1'))

    def test_apply_stock(self):
        from cms.routes_coins import apply_stock
        self.assertEqual(apply_stock(4, 8, '5', False), (True, 7))
        self.assertEqual(apply_stock(6, 7, '7', False), (False, 6))
        self.assertEqual(apply_stock(1, 0, '5', False), (True, 0))      # не ниже нуля
        self.assertEqual(apply_stock(None, 10, '', False), (True, 10))  # был без лимита → число
        self.assertEqual(apply_stock(3, None, '3', True), (True, None))
        self.assertEqual(apply_stock(None, None, '', True), (False, None))

    def test_csv_cell(self):
        from cms.routes_coins import csv_cell
        self.assertEqual(csv_cell('=1+1'), "'=1+1")
        self.assertEqual(csv_cell('-cmd'), "'-cmd")
        self.assertEqual(csv_cell(-20), -20)
        self.assertEqual(csv_cell('обычный'), 'обычный')
        self.assertTrue(re.match(r'^\d{2}\.\d{2}\.\d{4}', csv_cell(datetime(2026, 1, 2, tzinfo=timezone.utc))))
