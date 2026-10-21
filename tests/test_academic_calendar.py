# tests/test_academic_calendar.py — «Академический календарь»: PDF на группу (cms/routes_calendar.py, PEOPLE).
#
# Что проверяем (эмуляторы Firestore + Auth + Storage, tests/run_tests.sh):
#   * страница: список, сводка, фильтры, пункт сайдбара, ссылка из «Групп»;
#   * загрузка на одну и на несколько групп (один upload + copy_blob, свой объект и токен на группу), замена
#     (version + 1, старый объект удалён), удаление, группа не из справочника;
#   * валидации (не PDF, пустой, > 20 МБ, сигнатура со сдвигом, нет групп, > 40, чужое название, подпись), CSRF,
#     не-админ; «всё или ничего» при сбое Storage и Firestore;
#   * переименование группы сохраняет календарь, удаление группы чистит Storage;
#   * пуш (notify): топики расписания, ъ/ь, сбой пуша не ломает загрузку;
#   * XSS в подписи и имени файла, имя файла и путь объекта без path traversal и юникода.
# Эмулятор Storage не умеет copyTo (501): в проде копии делает bucket.copy_blob, в эмуляторе код падает обратно на
# повторную загрузку тех же байтов — путь copy_blob проверяется подменой бакета (_SpyBucket).

import io
import re
import time
import unittest
from unittest import mock
from urllib.parse import quote

import requests
from google.cloud.storage.retry import DEFAULT_RETRY

from tests import seed as S
from tests.base import CmsTestCase

PDF = b'%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<<>>\n%%EOF\n'
TOKEN_KEY = 'firebaseStorageDownloadTokens'
G1, G2, G3, G4 = 'ПО 24-1', 'ПО 24-2', 'ИС 23-1', 'ЭК 25-1'
PATH_RE = re.compile(r'^academic_calendars/[a-z0-9_-]+/\d+\.pdf$')


def _pdf(size):
    """Корректный PDF заданного размера (заголовок + заполнитель + %%EOF)."""
    head, tail = b'%PDF-1.4\n', b'\n%%EOF\n'
    return head + b'0' * (size - len(head) - len(tail)) + tail


class _SpyBlob:
    """Обёртка над blob: считает upload_from_string, остальное — настоящему объекту."""

    def __init__(self, spy, blob):
        object.__setattr__(self, '_spy', spy)
        object.__setattr__(self, '_blob', blob)

    def __getattr__(self, name):
        return getattr(self._blob, name)

    def __setattr__(self, name, value):
        setattr(self._blob, name, value)

    def upload_from_string(self, data, **kw):
        self._spy.uploads.append((self._blob.name, len(data)))
        return self._blob.upload_from_string(data, **kw)


class _CopiedBlob:
    """Результат copy_blob: объект с метаданными источника (токен тоже копируется — код обязан его заменить).
    У эмулятора Storage нет copyTo и PATCH метаданных, поэтому «копию» кладём руками, а patch() применяет новый
    токен перезаписью объекта теми же байтами — для проверки, что код меняет токен у каждой копии."""

    def __init__(self, real_dst, real_src):
        self._dst = real_dst
        self._data = real_src.download_as_bytes()
        self._content_type = real_src.content_type
        self._cache_control = real_src.cache_control
        self._disposition = real_src.content_disposition
        self.metadata = dict(real_src.metadata or {})
        self._apply()

    def _apply(self):
        self._dst.metadata = dict(self.metadata)
        self._dst.cache_control = self._cache_control
        self._dst.content_disposition = self._disposition
        self._dst.upload_from_string(self._data, content_type=self._content_type)

    def patch(self, **kw):
        self._apply()


class _SpyBucket:
    """Бакет, у которого copy_blob «работает» (эмулятор отдаёт 501): считает загрузки и копии, умеет упасть на
    N-й копии. Копию делает руками — читает источник и пишет объект с его метаданными, как настоящий copyTo."""

    def __init__(self, real, fail_copy_at=None):
        self._real = real
        self.uploads, self.copies = [], []
        self.fail_copy_at = fail_copy_at

    def __getattr__(self, name):
        return getattr(self._real, name)

    def blob(self, name):
        return _SpyBlob(self, self._real.blob(name))

    def copy_blob(self, src, destination, new_name, **kw):
        self.copies.append(new_name)
        if self.fail_copy_at == len(self.copies):
            raise RuntimeError('copy_blob: сбой Storage')
        real_src = src._blob if isinstance(src, _SpyBlob) else src
        real_src.reload()
        dst = _CopiedBlob(self._real.blob(new_name), real_src)
        return dst


class CalendarCase(CmsTestCase):
    """База: чистое состояние календарей перед каждым тестом (сид общий на класс — быстрее пересева)."""

    def setUp(self):
        super().setUp()
        self.clean_calendars()

    @classmethod
    def clean_calendars(cls):
        from cms import core
        for blob in core.bucket.list_blobs(prefix='academic_calendars/'):
            blob.delete()
        for snap in core.db.collection('groups_list').stream():
            if snap.id not in S.GROUPS:   # группы, созданные тестами (не из сида)
                snap.reference.delete()
            elif 'academicCalendar' in (snap.to_dict() or {}):
                snap.reference.update({'academicCalendar': core.DELETE_FIELD})
        for snap in core.db.collection('cms_audit').stream():
            snap.reference.delete()
        core.sent_pushes.clear()
        core.cache.invalidate()

    # ---------- хелперы ----------
    def upload(self, groups, data=None, name='calendar.pdf', title='', notify=False, **extra):
        form = {'groups': list(groups), 'title': title, **extra}
        if data is not False:
            form['file'] = (io.BytesIO(PDF if data is None else data), name)
        if notify:
            form['notify'] = '1'
        return self.post('/academic-calendar', form, content_type='multipart/form-data')

    def card(self, group):
        doc = self.doc('groups_list', group) or {}
        return doc.get('academicCalendar')

    def blobs(self):
        from cms import core
        return sorted(b.name for b in core.bucket.list_blobs(prefix='academic_calendars/'))

    def blob(self, path):
        from cms import core
        return core.bucket.get_blob(path)

    def stored_calendars(self):
        return {d['id']: d['academicCalendar'] for d in self.docs('groups_list') if 'academicCalendar' in d}

    def assertRejected(self, response, text):
        """Страница с ошибкой (200, модалка открыта, значения на месте), ничего не загружено и не записано."""
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn(text, html)
        self.assertIn('data-cc-open-on-load', html)
        self.assertEqual(self.blobs(), [])
        self.assertEqual(self.stored_calendars(), {})
        return html


class HelpersTest(CmsTestCase):
    """Чистые помощники модуля (без Firestore/Storage)."""

    login_as_admin = False

    def test_safe_file_name(self):
        from cms.routes_calendar import DEFAULT_FILE_NAME, _safe_file_name as f
        self.assertEqual(f('План 2026.pdf'), 'План 2026.pdf')
        self.assertEqual(f('C:\\Users\\a\\План.PDF'), 'План.pdf')
        self.assertEqual(f('../../etc/passwd.pdf'), 'passwd.pdf')
        self.assertEqual(f('календарь'), 'календарь.pdf')
        self.assertEqual(f('a\x00b\u202ec\u200d.pdf'), 'a b c.pdf')       # управляющие и невидимые символы
        for empty in ('', '.pdf', '..', ' . ', '/', '\\'):
            self.assertEqual(f(empty), DEFAULT_FILE_NAME, repr(empty))
        long_name = f('я' * 300 + '.pdf')
        self.assertEqual(len(long_name), 120)
        self.assertTrue(long_name.endswith('.pdf'))

    def test_slug(self):
        from cms.routes_calendar import _slug
        self.assertEqual(_slug('ПО 24-1'), 'po_24-1')
        self.assertEqual(_slug('ВТ 25-1п'), 'vt_25-1p')
        self.assertEqual(_slug(''), 'group')
        self.assertEqual(_slug('Қ ғ ү'), 'group')                         # казахские буквы вне транслита → пусто → group
        self.assertEqual(_slug('../..//x'), 'x')
        self.assertTrue(re.fullmatch(r'[a-z0-9_-]{1,60}', _slug('Ёж ' * 80)))

    def test_fmt_size(self):
        from cms.routes_calendar import _fmt_size
        self.assertEqual(_fmt_size(2516582), '2,4 МБ')
        self.assertEqual(_fmt_size(840000), '820 КБ')
        self.assertEqual(_fmt_size(1), '1 КБ')
        for bad in (0, -5, None, 'x'):
            self.assertEqual(_fmt_size(bad), '')

    def test_content_disposition(self):
        from cms.routes_calendar import _content_disposition
        self.assertEqual(_content_disposition('План 2026.pdf'),
                         'inline; filename="plan_2026.pdf"; filename*=UTF-8\'\'%D0%9F%D0%BB%D0%B0%D0%BD%202026.pdf')
        evil = _content_disposition('a"\r\nX-Evil: 1;.pdf')
        self.assertNotIn('\r', evil)
        self.assertNotIn('\n', evil)
        self.assertEqual(evil.count('"'), 2)

    def test_object_path_unique(self):
        from cms.routes_calendar import _object_path
        taken = {'academic_calendars/po_24-1/100.pdf'}
        self.assertEqual(_object_path('ПО 24-1', 100, taken), 'academic_calendars/po_24-1/101.pdf')
        # другое название с тем же slug и та же метка — свой путь
        self.assertEqual(_object_path('PO 24-1', 100, taken), 'academic_calendars/po_24-1/102.pdf')


class ProductionCopyPathTest(CmsTestCase):
    """Ветка copy_blob на настоящей библиотеке google-cloud-storage (эмулятор copyTo не умеет): HTTP-слой заменён
    заглушкой, которая отвечает как GCS на copyTo и PATCH — проверяем последовательность вызовов и параметры."""

    login_as_admin = False

    def test_copy_then_patch_new_token(self):
        from cms import core, routes_calendar
        real_bucket = core.bucket._get()
        connection = real_bucket.client._connection
        calls = []

        def api_request(method=None, path=None, data=None, query_params=None, **kw):
            calls.append((method, path, data, kw.get('timeout'), kw.get('retry')))
            name = path.split('/o/')[-1] if method == 'PATCH' else path.split('/copyTo/b/')[1].split('/o/')[1]
            body = {'name': name, 'bucket': real_bucket.name, 'contentType': 'application/pdf',
                    'cacheControl': 'public, max-age=3600',
                    'metadata': {'firebaseStorageDownloadTokens': 'token-of-source'}}
            if method == 'PATCH':
                body['metadata'] = data['metadata']
            return body

        with mock.patch.object(connection, 'api_request', side_effect=api_request):
            routes_calendar._copy_object('academic_calendars/po_24-1/1.pdf', 'academic_calendars/po_24-2/2.pdf',
                                         'brand-new-token', b'%PDF-1.4', 'inline; filename="a.pdf"')
        self.assertEqual([c[0] for c in calls], ['POST', 'PATCH'])
        post, patch = calls
        self.assertIn('/o/academic_calendars%2Fpo_24-1%2F1.pdf/copyTo/b/' + real_bucket.name
                      + '/o/academic_calendars%2Fpo_24-2%2F2.pdf', post[1])
        self.assertIsNone(post[2])                                             # тело не нужно: копируется всё
        self.assertIn('academic_calendars%2Fpo_24-2%2F2.pdf', patch[1])
        self.assertEqual(patch[2], {'metadata': {'firebaseStorageDownloadTokens': 'brand-new-token'}})
        self.assertEqual({post[3], patch[3]}, {routes_calendar.UPLOAD_TIMEOUT})   # таймаут 120 с, а не 60 по умолчанию
        self.assertEqual([post[4], patch[4]], [DEFAULT_RETRY, DEFAULT_RETRY])    # повтор на временных сбоях


class PageTest(CalendarCase):

    def test_page_empty_state(self):
        html = self.get_ok('/academic-calendar')
        self.assertIn('Академический календарь', html)
        self.assertIn(G1, html)
        self.assertIn('Не загружен', html)
        self.assertIn('Календари ещё не загружены', html)       # подсказка первого запуска
        self.assertIn('id="calUpload"', html)
        self.assertIn('/static/js/calendar.js', html)
        self.assertIn('/static/css/calendar.css', html)
        self.assertNotIn('onclick=', html)

    def test_sidebar_item_and_groups_menu_link(self):
        home = self.get_ok('/')
        self.assertIn('href="/academic-calendar"', home)
        groups = self.get_ok('/groups')
        self.assertIn('/academic-calendar?q=%D0%9F%D0%9E+24-1', groups)       # пункт меню строки → страница с поиском

    def test_filters_and_summary(self):
        from cms.routes_people import _group_course
        self.assertEqual(self.upload([G1, G3]).status_code, 302)
        html = self.get_ok('/academic-calendar')
        self.assertIn('с календарём', html)
        self.assertIn('без календаря', html)
        self.assertNotIn('Календари ещё не загружены', html)
        def rows(query):
            html = self.get_ok('/academic-calendar?' + query)
            table = html.split('id="calTable"')[1].split('</table>')[0] if 'id="calTable"' in html else ''
            return html, re.findall(r'<span class="cc-cell-main">([^<]+)</span>', table)

        _html, has = rows('state=has')
        self.assertEqual(sorted(has), sorted([G1, G3]))
        _html, none = rows('state=none')
        self.assertIn(G2, none)
        self.assertNotIn(G1, none)
        self.assertNotIn(G3, none)
        _html, by_q = rows('q=%s' % quote(G3))
        self.assertEqual(by_q, [G3])
        _html, by_course = rows('course=%s&state=has' % _group_course(G1))
        self.assertEqual(by_course, [G1])
        _html, by_major = rows('major=%s' % quote('ИС'))
        self.assertIn(G3, by_major)
        self.assertNotIn(G1, by_major)
        html, nothing = rows('q=zzzzzz')
        self.assertEqual(nothing, [])
        self.assertIn('Ничего не найдено', html)

    def test_no_groups_state(self):
        from cms import routes_calendar
        with mock.patch.object(routes_calendar, '_calendar_rows', return_value=[]):
            html = self.get_ok('/academic-calendar')
        self.assertIn('Групп пока нет', html)
        self.assertNotIn('id="calTable"', html)
        self.assertNotIn('Календари ещё не загружены', html)                    # подсказка — только когда группы есть

    def test_dirty_calendar_field_tolerated(self):
        from cms import core
        core.db.collection('groups_list').document(G1).update({'academicCalendar': 'мусор'})
        core.db.collection('groups_list').document(G2).update({'academicCalendar': {'url': 'javascript:alert(1)'}})
        core.cache.invalidate()
        html = self.get_ok('/academic-calendar')
        self.assertNotIn('javascript:alert', html)
        self.assertNotIn('class="cal-file"', html)                              # ни у одной группы календарь не показан
        self.assertGreaterEqual(html.count('Не загружен</span>'), 2)            # битые поля = «не загружен»


class UploadTest(CalendarCase):

    def test_upload_single_group(self):
        r = self.upload([G1], title='2026–2027 учебный год', name='План календаря.pdf')
        self.assertRedirectsTo(r, '/academic-calendar')
        card = self.card(G1)
        self.assertEqual(sorted(card), ['fileName', 'path', 'size', 'title', 'uploadedAt', 'uploadedBy', 'url',
                                        'version'])
        self.assertEqual((card['fileName'], card['title'], card['size'], card['version']),
                         ('План календаря.pdf', '2026–2027 учебный год', len(PDF), 1))
        self.assertEqual(card['uploadedBy'], S.ADMIN['email'])
        self.assertIsNotNone(card['uploadedAt'])
        self.assertRegex(card['path'], PATH_RE)
        self.assertTrue(card['path'].startswith('academic_calendars/po_24-1/'))
        # остальные поля группы на месте, у других групп календаря нет
        self.assertEqual(self.doc('groups_list', G1)['name'], G1)
        self.assertEqual(list(self.stored_calendars()), [G1])
        # объект в Storage: тип, кэш, Content-Disposition, токен; URL с этим токеном
        blob = self.blob(card['path'])
        self.assertEqual(blob.content_type, 'application/pdf')
        self.assertEqual(blob.cache_control, 'public, max-age=3600')
        self.assertIn('inline; filename="plan_kalendarya.pdf"', blob.content_disposition)
        self.assertIn("filename*=UTF-8''%D0%9F%D0%BB%D0%B0%D0%BD", blob.content_disposition)
        token = blob.metadata[TOKEN_KEY]
        self.assertIn('token=' + token, card['url'])
        self.assertIn(quote(card['path'], safe=''), card['url'])
        self.assertEqual(blob.download_as_bytes(), PDF)
        s = requests.Session()
        s.trust_env = False
        got = s.get(card['url'], timeout=20)
        self.assertEqual((got.status_code, got.content), (200, PDF))
        # без «Уведомить» пуша нет; журнал и сообщение
        self.assertEqual(self.sent_pushes(), [])
        entries = self.audit_entries('upload')
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['entityId'], G1)
        details = entries[0]['details']
        self.assertEqual((details['file'], details['size'], details['groups'], details['version'], details['notify']),
                         ('План календаря.pdf', len(PDF), G1, 1, False))
        self.assertIn('success', [c for c, _ in self.flashes()])

    def sent_pushes(self):
        from cms import core
        return list(core.sent_pushes)

    def test_page_shows_loaded_calendar(self):
        self.upload([G1], title='2026–2027 учебный год', name='План.pdf')
        html = self.get_ok('/academic-calendar')
        card = self.card(G1)
        self.assertIn('2026–2027 учебный год', html)
        self.assertIn('План.pdf', html)
        self.assertIn('1 КБ', html)
        self.assertIn('href="%s"' % card['url'].replace('&', '&amp;'), html)
        self.assertIn('target="_blank" rel="noopener noreferrer"', html)
        self.assertIn(S.ADMIN['email'], html)
        audit = self.get_ok('/audit')
        self.assertIn('Академический календарь', audit)
        self.assertIn('Загрузка', audit)

    def test_upload_many_groups_own_object_and_token(self):
        groups = [G1, G2, G3, G4]
        r = self.upload(groups)
        self.assertRedirectsTo(r, '/academic-calendar')
        cards = self.stored_calendars()
        self.assertEqual(sorted(cards), sorted(groups))
        paths = [cards[g]['path'] for g in groups]
        self.assertEqual(len(set(paths)), len(groups))                       # на группу — свой объект
        self.assertEqual(self.blobs(), sorted(paths))
        tokens = []
        for g in groups:
            self.assertRegex(cards[g]['path'], PATH_RE)
            blob = self.blob(cards[g]['path'])
            tokens.append(blob.metadata[TOKEN_KEY])
            self.assertIn('token=' + tokens[-1], cards[g]['url'])
            self.assertEqual(blob.download_as_bytes(), PDF)
            self.assertEqual((cards[g]['version'], cards[g]['size']), (1, len(PDF)))
        self.assertEqual(len(set(tokens)), len(groups))                      # новый токен на каждую копию
        entries = self.audit_entries('upload')
        self.assertEqual(len(entries), 1)
        self.assertIsNone(entries[0]['entityId'])
        self.assertEqual(set(entries[0]['details']['groups'].split(', ')), set(groups))
        self.assertNotIn('version', entries[0]['details'])
        flash_text = ' '.join(m for _c, m in self.flashes())
        self.assertIn('для 4 групп', flash_text)

    def test_many_groups_one_upload_rest_copied_in_storage(self):
        from cms import core, routes_calendar
        spy = _SpyBucket(core.bucket)
        groups = [G1, G2, G3, G4]
        with mock.patch.object(routes_calendar, 'bucket', spy):
            r = self.upload(groups, data=_pdf(300 * 1024))
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertEqual(len(spy.uploads), 1)                                  # в Storage файл ушёл один раз
        self.assertEqual(spy.uploads[0][1], 300 * 1024)
        self.assertEqual(len(spy.copies), len(groups) - 1)                     # остальные — copy_blob
        cards = self.stored_calendars()
        self.assertEqual(spy.uploads[0][0], cards[groups[0]]['path'])
        self.assertEqual(sorted(spy.copies), sorted(cards[g]['path'] for g in groups[1:]))
        tokens = {self.blob(c['path']).metadata[TOKEN_KEY] for c in cards.values()}
        self.assertEqual(len(tokens), len(groups))                             # токен источника заменён у копий
        for c in cards.values():
            self.assertIn('token=' + self.blob(c['path']).metadata[TOKEN_KEY], c['url'])

    def test_copy_failure_rolls_everything_back(self):
        from cms import core, routes_calendar
        spy = _SpyBucket(core.bucket, fail_copy_at=2)
        with mock.patch.object(routes_calendar, 'bucket', spy):
            r = self.upload([G1, G2, G3, G4])
        html = self.assertRejected(r, 'Не удалось сохранить файл в хранилище')
        self.assertIn('Ничего не изменено', html)
        self.assertIn('value="%s"' % G3, html)                                 # выбор групп не потерян

    def test_firestore_failure_removes_new_objects_and_keeps_old(self):
        from cms import routes_calendar
        self.assertEqual(self.upload([G1]).status_code, 302)
        old = self.card(G1)
        with mock.patch.object(routes_calendar, '_commit_cards', side_effect=RuntimeError('firestore: commit не удался')):
            r = self.upload([G1, G2], title='новый')
        self.assertEqual(r.status_code, 200)
        self.assertIn('изменения не сохранены', r.get_data(as_text=True))
        self.assertEqual(self.blobs(), [old['path']])                          # новые объекты удалены, старый цел
        self.assertEqual(self.stored_calendars().keys(), {G1})
        self.assertEqual(self.card(G1)['version'], 1)
        self.assertEqual(self.card(G1)['title'], '')

    def test_transaction_commit_failure_writes_nothing(self):
        """Настоящая транзакция падает на commit: ни один документ не изменён, новые объекты удалены."""
        from cms import core, routes_calendar

        class _FailingTxnDb:
            def __init__(self, real):
                self._real = real

            def __getattr__(self, name):
                return getattr(self._real, name)

            def transaction(self, **kw):
                txn = self._real.transaction(**kw)

                def broken_commit():
                    raise RuntimeError('firestore: commit не удался')
                txn._commit = broken_commit
                return txn

        self.assertEqual(self.upload([G1]).status_code, 302)
        old = self.card(G1)
        with mock.patch.object(routes_calendar, 'db', _FailingTxnDb(core.db)):
            r = self.upload([G1, G2, G3], title='новый')
        self.assertEqual(r.status_code, 200)
        self.assertIn('изменения не сохранены', r.get_data(as_text=True))
        self.assertEqual(self.blobs(), [old['path']])
        self.assertEqual(self.stored_calendars().keys(), {G1})
        self.assertEqual(self.card(G1), old)

    def test_concurrent_replace_does_not_repeat_version_or_orphan_object(self):
        """Второй админ начал загрузку по устаревшему чтению (календаря у группы ещё не было), а первый успел
        записать свой. version берётся внутри транзакции: 2, а не снова 1; объект первого удалён, не осиротел."""
        from cms import routes_calendar
        self.upload([G1], title='первый админ')
        first = self.card(G1)
        time.sleep(0.01)
        with mock.patch.object(routes_calendar, '_fresh_groups', return_value={G1: {'name': G1}}):
            r = self.upload([G1], title='второй админ')
        self.assertRedirectsTo(r, '/academic-calendar')
        second = self.card(G1)
        self.assertEqual((second['version'], second['title']), (2, 'второй админ'))
        self.assertIsNone(self.blob(first['path']))
        self.assertEqual(self.blobs(), [second['path']])
        self.assertEqual(len(self.audit_entries('replace')), 1)

    def test_delete_after_replace_leaves_no_objects(self):
        """Удаление берёт путь из той же транзакции, в которой снимает поле: после замены уходит именно новый объект."""
        self.upload([G1])
        first = self.card(G1)
        time.sleep(0.01)
        self.upload([G1], title='новая')
        second = self.card(G1)
        self.assertNotEqual(first['path'], second['path'])
        r = self.post('/academic-calendar/delete/%s' % G1)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.card(G1))
        self.assertEqual(self.blobs(), [])

    def test_replace_bumps_version_and_removes_old_object(self):
        self.upload([G1], title='старый', name='old.pdf')
        old = self.card(G1)
        time.sleep(0.01)
        new_pdf = PDF + '% новая версия\n'.encode('utf-8')
        r = self.upload([G1], data=new_pdf, title='2026–2027', name='new.pdf')
        self.assertRedirectsTo(r, '/academic-calendar')
        new = self.card(G1)
        self.assertEqual((new['version'], new['title'], new['fileName'], new['size']),
                         (2, '2026–2027', 'new.pdf', len(new_pdf)))
        self.assertNotEqual(new['path'], old['path'])
        self.assertNotEqual(new['url'], old['url'])
        self.assertIsNone(self.blob(old['path']))                              # старый объект удалён после записи
        self.assertEqual(self.blobs(), [new['path']])
        self.assertEqual(self.blob(new['path']).download_as_bytes(), new_pdf)
        self.assertEqual(len(self.audit_entries('upload')), 1)
        replaced = self.audit_entries('replace')
        self.assertEqual((len(replaced), replaced[0]['entityId']), (1, G1))
        self.assertIn('заменён', ' '.join(m for _c, m in self.flashes()))
        self.assertIn('версия 2', self.get_ok('/academic-calendar'))

    def test_replace_one_of_many_and_create_others(self):
        self.upload([G1])
        old = self.card(G1)
        self.upload([G1, G2, G3])
        cards = self.stored_calendars()
        self.assertEqual((cards[G1]['version'], cards[G2]['version'], cards[G3]['version']), (2, 1, 1))
        self.assertEqual(self.blobs(), sorted(c['path'] for c in cards.values()))
        self.assertIsNone(self.blob(old['path']))
        self.assertEqual(len(self.audit_entries('upload')), 2)                 # 1-я загрузка + новые группы
        replaced = self.audit_entries('replace')
        self.assertEqual([e['entityId'] for e in replaced], [G1])

    def test_replace_keeps_other_group_fields(self):
        from cms import core
        core.db.collection('groups_list').document(G1).update({'curator': 'Иванова А.'})
        created = self.doc('groups_list', G1)['createdAt']
        self.upload([G1])
        self.upload([G1], title='ещё раз')
        doc = self.doc('groups_list', G1)
        self.assertEqual((doc['name'], doc['curator'], doc['createdAt']), (G1, 'Иванова А.', created))
        self.assertEqual(doc['academicCalendar']['title'], 'ещё раз')

    def test_replace_drops_stale_keys_of_old_map(self):
        from cms import core
        self.upload([G1])
        core.db.collection('groups_list').document(G1).update({'academicCalendar.legacy': 'старое поле'})
        self.assertIn('legacy', self.card(G1))
        self.upload([G1])
        self.assertNotIn('legacy', self.card(G1))                               # карта заменяется целиком
        self.assertEqual(self.card(G1)['version'], 2)

    def test_delete_calendar(self):
        self.upload([G1, G2])
        cards = self.stored_calendars()
        self.assertIn('class="cal-file"', self.get_ok('/academic-calendar?q=%s' % quote(G1)))   # кэш справочника прогрет
        r = self.post('/academic-calendar/delete/%s' % G1, {'next': '/academic-calendar?q=x'})
        self.assertRedirectsTo(r, '/academic-calendar?q=x')
        self.assertIsNone(self.card(G1))
        self.assertEqual(self.doc('groups_list', G1)['name'], G1)              # сама группа осталась
        self.assertIsNone(self.blob(cards[G1]['path']))
        self.assertIsNotNone(self.blob(cards[G2]['path']))                     # у соседней группы объект цел
        self.assertEqual(self.card(G2)['path'], cards[G2]['path'])
        entries = self.audit_entries('delete')
        self.assertEqual((len(entries), entries[0]['entity'], entries[0]['entityId']),
                         (1, 'academic_calendar', G1))
        after = self.get_ok('/academic-calendar?q=%s' % quote(G1))
        self.assertIn('Не загружен', after)
        self.assertNotIn('class="cal-file"', after)                            # страница не залипла на старом кэше

    def test_delete_without_calendar_is_harmless(self):
        r = self.post('/academic-calendar/delete/%s' % G1)
        self.assertEqual(r.status_code, 302)
        self.assertIn('warning', [c for c, _ in self.flashes()])
        r = self.post('/academic-calendar/delete/%s' % 'Нет-такой-группы')
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('groups_list', 'Нет-такой-группы'))         # документ не создаётся

    def test_delete_when_object_already_gone(self):
        from cms import core
        self.upload([G1])
        core.bucket.blob(self.card(G1)['path']).delete()
        r = self.post('/academic-calendar/delete/%s' % G1)
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.card(G1))
        self.assertIn('success', [c for c, _ in self.flashes()])

    def test_group_not_in_directory_gets_directory_doc(self):
        unknown = 'XX 99-9'                                                    # есть в расписании (сид), нет в groups_list
        self.assertIsNone(self.doc('groups_list', unknown))
        self.assertIn('нет в справочнике', self.get_ok('/academic-calendar'))
        r = self.upload([unknown])
        self.assertRedirectsTo(r, '/academic-calendar')
        doc = self.doc('groups_list', unknown)
        self.assertEqual(doc['name'], unknown)
        self.assertIsNotNone(doc['createdAt'])
        self.assertEqual(doc['academicCalendar']['version'], 1)
        self.assertNotIn('нет в справочнике', self.get_ok('/academic-calendar?q=XX'))

    def test_default_file_name(self):
        self.upload([G1], name='.pdf')
        self.assertEqual(self.card(G1)['fileName'], 'academic-calendar.pdf')

    def test_exactly_limit_size_is_accepted(self):
        data = _pdf(20 * 1024 * 1024)
        self.assertEqual(len(data), 20 * 1024 * 1024)
        r = self.upload([G1], data=data)
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertEqual(self.card(G1)['size'], 20 * 1024 * 1024)
        self.assertIn('20,0 МБ', self.get_ok('/academic-calendar'))

    def test_pdf_header_with_offset_accepted(self):
        self.upload([G1], data=b'\x00junk' * 100 + PDF)                        # сдвиг 500 байт
        self.assertIsNotNone(self.card(G1))


class ValidationTest(CalendarCase):

    def test_not_a_pdf(self):
        png = S._png(8, 8, (10, 20, 30))
        html = self.assertRejected(self.upload([G1], data=png, name='скан.pdf'), 'не PDF')
        self.assertIn('скан.pdf', html)
        self.assertRejected(self.upload([G1], data=b'Just text', name='notes.txt'), 'не PDF')

    def test_signature_beyond_window_rejected(self):
        self.assertRejected(self.upload([G1], data=b'\x00' * 1100 + PDF), 'не PDF')

    def test_wrong_extension_rejected_even_with_pdf_bytes(self):
        self.assertRejected(self.upload([G1], data=PDF, name='calendar.bin'), 'не PDF')
        self.assertRejected(self.upload([G1], data=PDF, name='calendar'), 'не PDF')

    def test_pdf_mimetype_without_extension_accepted(self):
        r = self.post('/academic-calendar', {'groups': [G1], 'file': (io.BytesIO(PDF), 'календарь', 'application/pdf')},
                      content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertEqual(self.card(G1)['fileName'], 'календарь.pdf')
        r = self.post('/academic-calendar', {'groups': [G2], 'file': (io.BytesIO(PDF), 'КАЛЕНДАРЬ.PDF')},
                      content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/academic-calendar')                          # регистр расширения не важен
        self.assertEqual(self.card(G2)['fileName'], 'КАЛЕНДАРЬ.pdf')

    def test_empty_file(self):
        self.assertRejected(self.upload([G1], data=b'', name='empty.pdf'), 'пустой')

    def test_too_big(self):
        data = _pdf(20 * 1024 * 1024 + 1)
        self.assertRejected(self.upload([G1], data=data, name='big.pdf'), 'больше 20 МБ')

    def test_no_file(self):
        self.assertRejected(self.upload([G1], data=False), 'Выберите PDF-файл')

    def test_no_groups(self):
        self.assertRejected(self.upload([]), 'Выберите хотя бы одну группу')

    def test_unknown_group(self):
        html = self.assertRejected(self.upload([G1, 'Нет такой 1-1']), 'Такой группы нет')
        self.assertIn('Нет такой 1-1', html)
        self.assertRejected(self.upload(['../..']), 'Такой группы нет')
        self.assertRejected(self.upload(['А/Б']), 'Такой группы нет')

    def test_more_than_40_groups(self):
        names = ['Группа %d-1' % i for i in range(41)]
        self.assertRejected(self.upload(names), 'не больше 40 групп')

    def test_exactly_40_groups_ok(self):
        from cms import core
        names = ['ТЕСТ %02d-1' % i for i in range(40)]
        core.db.collection('groups_list')
        batch = core.db.batch()
        for n in names:
            batch.set(core.db.collection('groups_list').document(n), {'name': n})
        batch.commit()
        core.cache.invalidate()
        r = self.upload(names)
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertEqual(len(self.stored_calendars()), 40)
        self.assertEqual(len(self.blobs()), 40)

    def test_title_too_long(self):
        self.assertRejected(self.upload([G1], title='я' * 81), 'не длиннее 80')
        self.upload([G1], title='  ' + 'я' * 80 + '  ')
        self.assertEqual(self.card(G1)['title'], 'я' * 80)

    def test_duplicate_groups_collapse(self):
        self.upload([G1, G1, ' %s ' % G1])
        self.assertEqual(len(self.blobs()), 1)
        self.assertEqual(self.card(G1)['version'], 1)

    def test_values_kept_after_error(self):
        r = self.upload([G1, G2], data=False, title='Мой заголовок', notify=True)
        html = r.get_data(as_text=True)
        self.assertIn('data-cc-open-on-load', html)
        self.assertIn('value="Мой заголовок"', html)
        self.assertRegex(html, r'name="groups" value="%s"[^>]*checked' % G1)
        self.assertRegex(html, r'name="groups" value="%s"[^>]*checked' % G2)
        self.assertNotRegex(html, r'name="groups" value="%s"[^>]*checked' % G3)
        self.assertRegex(html, r'name="notify"[^>]*checked')

    def test_csrf_required(self):
        before = self.blobs()
        r = self.client.post('/academic-calendar', data={'groups': [G1], 'file': (io.BytesIO(PDF), 'a.pdf')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        r = self.client.post('/academic-calendar/upload', data={'groups': [G1], 'file': (io.BytesIO(PDF), 'a.pdf')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.blobs(), before)
        self.upload([G1])
        r = self.client.post('/academic-calendar/delete/%s' % G1)
        self.assertEqual(r.status_code, 400)
        self.assertIsNotNone(self.card(G1))

    def test_upload_endpoint_alias(self):
        r = self.post('/academic-calendar/upload', {'groups': [G1], 'file': (io.BytesIO(PDF), 'a.pdf')},
                      content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertIsNotNone(self.card(G1))

    def test_anonymous_cannot_upload_or_delete(self):
        self.upload([G1])
        anon = self.app.test_client()
        with anon.session_transaction() as s:
            s['_csrf'] = 'anon-token'
        r = anon.post('/academic-calendar', data={'csrf_token': 'anon-token', 'groups': [G2],
                                                  'file': (io.BytesIO(PDF), 'a.pdf')},
                      content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/login')
        r = anon.post('/academic-calendar/delete/%s' % G1, data={'csrf_token': 'anon-token'})
        self.assertRedirectsTo(r, '/login')
        self.assertEqual(list(self.stored_calendars()), [G1])
        self.assertRedirectsTo(anon.get('/academic-calendar'), '/login')

    def test_demoted_admin_forbidden(self):
        from cms import core
        core.db.collection('users').document(S.ADMIN['uid']).update({'role': 'teacher'})
        try:
            with self.client.session_transaction() as s:
                s['role_checked_at'] = time.time() - 11 * 60
            r = self.upload([G1])
            self.assertEqual(r.status_code, 403)
        finally:
            core.db.collection('users').document(S.ADMIN['uid']).update({'role': 'admin'})
        self.assertEqual(self.blobs(), [])
        self.assertEqual(self.stored_calendars(), {})


class SecurityTest(CalendarCase):

    def test_xss_in_title_and_file_name(self):
        self.upload([G1], title='"><script>alert(1)</script>', name='<img src=x onerror=alert(1)>.pdf')
        html = self.get_ok('/academic-calendar')
        self.assertNotIn('<script>alert(1)</script>', html)
        self.assertNotIn('<img src=x', html)
        self.assertIn('&lt;script&gt;alert(1)&lt;/script&gt;', html)
        self.assertIn('&lt;img src=x onerror=alert(1)&gt;.pdf', html)
        # заголовок Content-Disposition — только ASCII-slug и percent-encoding
        blob = self.blob(self.card(G1)['path'])
        self.assertNotIn('<', blob.content_disposition)
        self.assertRegex(blob.content_disposition, r'^inline; filename="[a-z0-9_.-]+"; filename\*=UTF-8\'\'[A-Za-z0-9%_.~-]+$')
        audit = self.get_ok('/audit')
        self.assertNotIn('<script>alert(1)</script>', audit)

    def test_xss_in_error_page_and_values(self):
        evil = 'x"><script>alert(2)</script>'
        r = self.upload([G1], data=b'plain text', name='<b>hack</b>.txt', title=evil)
        html = r.get_data(as_text=True)
        self.assertNotIn('<script>alert(2)</script>', html)
        self.assertNotIn('<b>hack</b>', html)
        r = self.upload([evil], title=evil)
        self.assertNotIn('<script>alert(2)</script>', r.get_data(as_text=True))

    def test_file_name_cannot_traverse_or_inject(self):
        self.upload([G1], name='../../etc/passwd.pdf')
        card = self.card(G1)
        self.assertEqual(card['fileName'], 'passwd.pdf')
        self.assertRegex(card['path'], PATH_RE)
        self.upload([G2], name='a\u202efdp.exe\x00.pdf')
        self.assertNotIn('\u202e', self.card(G2)['fileName'])
        self.assertNotIn('\x00', self.card(G2)['fileName'])
        self.upload([G3], name='я' * 400 + '.pdf')
        self.assertEqual(len(self.card(G3)['fileName']), 120)

    def test_object_path_ascii_for_exotic_group_names(self):
        names = ['ҚАЗ 24-1', 'Связь 24-1', 'ГРУППА\u202e 25-1', 'Ёё 26-1 ъь']
        for n in names:
            self.assertEqual(self.post('/groups/create', {'group_name': n}).status_code, 302)
        r = self.upload(names)
        self.assertRedirectsTo(r, '/academic-calendar')
        cards = self.stored_calendars()
        for n in names:
            self.assertRegex(cards[n]['path'], PATH_RE, n)
            self.assertTrue(cards[n]['path'].isascii())
        self.assertEqual(len(set(c['path'] for c in cards.values())), len(names))   # одинаковый slug не склеивает пути

    def test_next_is_not_an_open_redirect(self):
        for evil in ('//evil.example/x', 'https://evil.example/', '/\\evil.example', 'javascript:alert(1)', '/\t/evil.example'):
            r = self.upload([G1], next=evil)
            self.assertRedirectsTo(r, '/academic-calendar')
            r = self.post('/academic-calendar/delete/%s' % G1, {'next': evil})
            self.assertRedirectsTo(r, '/academic-calendar')
        r = self.post('/academic-calendar/delete/%s' % G1, {'next': '/academic-calendar?state=none'})
        self.assertRedirectsTo(r, '/academic-calendar?state=none')                # свой путь — допустим

    def test_markup_in_group_name_is_escaped_everywhere(self):
        evil = '<img src=x onerror=alert(1)>'
        self.assertEqual(self.post('/groups/create', {'group_name': evil}).status_code, 302)
        self.assertEqual(self.upload([evil], title='подпись').status_code, 302)
        for url in ('/academic-calendar', '/academic-calendar?q=%s' % quote(evil), '/groups'):
            html = self.get_ok(url)
            self.assertNotIn('<img src=x', html, url)
            self.assertIn('&lt;img src=x onerror=alert(1)&gt;', html, url)
        self.assertNotIn('<img src=x', self.get_ok('/audit'))
        self.assertEqual(self.post('/academic-calendar/delete/%s' % quote(evil)).status_code, 302)
        self.assertIsNone(self.card(evil))

    def test_xss_in_query_filters(self):
        """q/major/course/state отражаются в поле поиска и в скрытом next строк — только экранированными."""
        self.upload([G1])
        evil = '"><script>alert(3)</script>'
        html = self.get_ok('/academic-calendar?q=%s&major=%s&course=%s&state=%s' % ((quote(evil),) * 4))
        self.assertNotIn('<script>alert(3)</script>', html)

    def test_huge_group_name_is_rejected_without_echoing_it_all(self):
        html = self.assertRejected(self.upload(['Я' * 5000]), 'Такой группы нет')
        self.assertNotIn('Я' * 100, html)

    def test_title_control_chars_stripped(self):
        self.upload([G1], title='Календарь\x00\n\t2026\u202e')
        self.assertEqual(self.card(G1)['title'], 'Календарь 2026')


class PushTest(CalendarCase):

    def pushes(self):
        from cms import core
        return list(core.sent_pushes)

    def test_notify_single_group(self):
        self.upload([G1], notify=True)
        pushes = self.pushes()
        self.assertEqual(len(pushes), 1)
        p = pushes[0]
        self.assertEqual(p['topic'], 'schedule_group_po_24-1')
        self.assertEqual(p['title'], 'Академический календарь')
        self.assertEqual(p['body'], 'Для группы «ПО 24-1» загружен новый календарь')
        self.assertEqual(p['data'], {'type': 'academic_calendar', 'group': G1})
        self.assertIn('Студенты уведомлены', ' '.join(m for _c, m in self.flashes()))

    def test_notify_many_groups(self):
        self.upload([G1, G2, G3], notify=True)
        pushes = self.pushes()
        self.assertEqual(sorted(p['topic'] for p in pushes),
                         ['schedule_group_is_23-1', 'schedule_group_po_24-1', 'schedule_group_po_24-2'])
        self.assertEqual({p['data']['group'] for p in pushes}, {G1, G2, G3})
        self.assertTrue(all(p['data']['type'] == 'academic_calendar' for p in pushes))

    def test_notify_off_by_default(self):
        self.upload([G1, G2])
        self.assertEqual(self.pushes(), [])

    def test_soft_sign_group_gets_both_topics(self):
        # топики функций и приложения расходятся, когда «ь» стоит перед дефисом: svyaz_-24-1 и svyaz-24-1
        self.assertEqual(self.post('/groups/create', {'group_name': 'СВЯЗЬ-24-1'}).status_code, 302)
        self.upload(['СВЯЗЬ-24-1'], notify=True)
        topics = sorted(p['topic'] for p in self.pushes())
        self.assertEqual(len(topics), 2)                                       # топик функций и топик приложения
        self.assertEqual(len(set(topics)), 2)

    def test_single_uses_send_push_many_uses_send_push_many(self):
        from cms import routes_calendar
        with mock.patch.object(routes_calendar, 'send_push', return_value=True) as one, \
                mock.patch.object(routes_calendar, 'send_push_many', return_value=3) as many:
            self.upload([G1], notify=True)
            self.assertEqual((one.call_count, many.call_count), (1, 0))
            kw = one.call_args.kwargs
            self.assertEqual((kw['title'], kw['topic']), ('Академический календарь', 'schedule_group_po_24-1'))
            self.upload([G1, G2, G3], notify=True)
            self.assertEqual((one.call_count, many.call_count), (1, 1))
            self.assertEqual(len(many.call_args.args[0]), 3)

    def test_push_failure_does_not_break_upload(self):
        from cms import routes_calendar
        with mock.patch.object(routes_calendar, 'send_push', side_effect=RuntimeError('fcm down')):
            r = self.upload([G1], notify=True)
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertIsNotNone(self.card(G1))
        self.assertEqual(len(self.blobs()), 1)
        flashes = self.flashes()
        self.assertIn('success', [c for c, _ in flashes])
        self.assertIn('warning', [c for c, _ in flashes])
        with mock.patch.object(routes_calendar, 'send_push_many', side_effect=RuntimeError('fcm down')):
            r = self.upload([G2, G3], notify=True)
        self.assertRedirectsTo(r, '/academic-calendar')
        self.assertEqual({G1, G2, G3}, set(self.stored_calendars()))

    def test_no_push_when_upload_rejected(self):
        self.upload([G1], data=b'not pdf', notify=True)
        self.assertEqual(self.pushes(), [])


class GroupLifecycleTest(CalendarCase):
    """Переименование и удаление группы затрагивают users/schedules — свежий сид на каждый тест."""

    fresh_seed_each_test = True

    def test_rename_group_keeps_calendar(self):
        old, new = 'ПО 25-1', 'ПО 25-1а'
        self.upload([old], title='календарь')
        card = self.card(old)
        r = self.post('/groups/edit_name', {'old_name': old, 'new_name': new, 'schedules': '1'})
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('groups_list', old))
        moved = self.card(new)
        self.assertEqual((moved['path'], moved['url'], moved['version'], moved['title']),
                         (card['path'], card['url'], card['version'], 'календарь'))
        self.assertIsNotNone(self.blob(card['path']))                          # путь в Storage от названия не зависит
        # замена под новым названием удаляет объект, лежащий под старым slug
        self.upload([new])
        self.assertEqual(self.card(new)['version'], 2)
        self.assertIsNone(self.blob(card['path']))
        self.assertEqual(self.blobs(), [self.card(new)['path']])
        self.assertTrue(self.card(new)['path'].startswith('academic_calendars/po_25-1a/'))

    def test_delete_group_cleans_storage(self):
        self.upload(['ЭК 25-1', G1])
        path = self.card('ЭК 25-1')['path']
        other = self.card(G1)['path']
        r = self.post('/groups/delete/%s' % 'ЭК 25-1')
        self.assertEqual(r.status_code, 302)
        self.assertIsNone(self.doc('groups_list', 'ЭК 25-1'))
        self.assertIsNone(self.blob(path))                                     # PDF ушёл вместе с группой
        self.assertIsNotNone(self.blob(other))                                 # чужие объекты не тронуты
        self.assertEqual(len(self.audit_entries('delete')), 1)

    def test_delete_group_without_object_or_calendar(self):
        from cms import core
        self.upload([G2])
        core.bucket.blob(self.card(G2)['path']).delete()                       # объекта уже нет
        self.assertEqual(self.post('/groups/delete/%s' % G2).status_code, 302)
        self.assertIsNone(self.doc('groups_list', G2))
        self.assertEqual(self.post('/groups/delete/%s' % G3).status_code, 302)   # группа без календаря
        self.assertIsNone(self.doc('groups_list', G3))

    def test_delete_group_ignores_foreign_paths(self):
        """Поле с путём вне academic_calendars/ не заставляет удалять чужие файлы."""
        from cms import core
        blob = core.bucket.blob('news/keep_me.txt')
        blob.upload_from_string(b'x', content_type='text/plain')
        try:
            core.db.collection('groups_list').document(G4).update(
                {'academicCalendar': {'url': 'https://x.test/a.pdf', 'path': 'news/keep_me.txt', 'version': 1}})
            self.assertEqual(self.post('/groups/delete/%s' % G4).status_code, 302)
            self.assertTrue(core.bucket.blob('news/keep_me.txt').exists())
            core.db.collection('groups_list').document(G3).update(
                {'academicCalendar': {'url': 'https://x.test/a.pdf', 'path': 'academic_calendars/../news/keep_me.txt'}})
            self.assertEqual(self.post('/groups/delete/%s' % G3).status_code, 302)
            self.assertTrue(core.bucket.blob('news/keep_me.txt').exists())
        finally:
            try:
                blob.delete()
            except Exception:
                pass

    def test_groups_page_indicator(self):
        self.upload([G1])
        html = self.get_ok('/groups')
        self.assertEqual(html.count('class="bi bi-file-earmark-pdf pp-cal-ind"'), 1)       # только у группы с календарём
        self.assertIn('Загружен академический календарь', html)


if __name__ == '__main__':
    unittest.main()
