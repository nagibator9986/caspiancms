# tests/test_content.py — раздел «Контент»: новости, опросы, события, клубы, товары, обратная связь (CONTENT).
#
# Сначала — тесты на найденные баги (схема данных должна совпадать с мобильным приложением):
#   * новость: правка фото обновляет images[] + imageUrl, убранные файлы удаляются из Storage;
#   * товар: пустые варианты очищаются (раньше оставались старые), цена — число;
#   * опрос: поля как в lib/services/poll_service.dart (target_audience, poll_type, is_anonymous,
#     endTime по Алматы, ответы с text/votes/createdAt), createdBy не перезаписывается.
# Дальше — функциональные тесты страниц и действий.

import io
import json
from datetime import datetime, timedelta, timezone

from tests.base import CmsTestCase
from tests.seed import ADMIN, _png


def _blob_exists(path):
    from cms.core import bucket
    return bucket.blob(path).exists()


def _png_file(name='new.png', color=(30, 120, 200)):
    return (io.BytesIO(_png(64, 48, color)), name)


# ==================== БАГИ (сначала тесты) ====================

class NewsPhotoBugTest(CmsTestCase):
    """Веб писал только imageUrl: замена фото у новости с images[] в приложении не была видна."""

    def test_edit_updates_images_and_deletes_removed_files(self):
        before = self.doc('news', 'news_open_day')
        old = before['images']
        self.assertEqual(len(old), 3)
        state = [{'kind': 'existing', 'url': old[1]['url']}, {'kind': 'new', 'idx': 0}]
        r = self.post('/news/edit/news_open_day', {
            'title': 'День открытых дверей (обновлено)', 'content': 'Текст', 'description': 'Коротко',
            'category': 'academic', 'target_audience': 'all',
            'images__state': json.dumps(state), 'images': _png_file(),
        }, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/news')
        after = self.doc('news', 'news_open_day')
        self.assertEqual(len(after['images']), 2)
        self.assertEqual(after['images'][0]['url'], old[1]['url'])
        new_img = after['images'][1]
        self.assertTrue(new_img['path'].startswith('news/news_open_day/'), new_img['path'])
        self.assertIn('token=', new_img['url'])
        self.assertEqual((new_img['w'], new_img['h']), (64, 48))
        self.assertEqual(after['imageUrl'], after['images'][0]['url'])
        self.assertEqual(after['title'], 'День открытых дверей (обновлено)')
        self.assertIn('updatedAt', after)
        # убранные фото удалены из Storage, оставленное и новое — на месте
        self.assertFalse(_blob_exists(old[0]['path']))
        self.assertFalse(_blob_exists(old[2]['path']))
        self.assertTrue(_blob_exists(old[1]['path']))
        self.assertTrue(_blob_exists(new_img['path']))

    def test_legacy_news_gets_images_array(self):
        r = self.post('/news/edit/news_legacy', {
            'title': 'Старая новость', 'content': 'Текст', 'category': 'academic', 'target_audience': 'all',
            'images__state': json.dumps([{'kind': 'new', 'idx': 0}]), 'images': _png_file('фото.png'),
        }, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/news')
        d = self.doc('news', 'news_legacy')
        self.assertEqual(len(d['images']), 1)
        self.assertEqual(d['imageUrl'], d['images'][0]['url'])
        self.assertTrue(d['images'][0]['path'].endswith('.png'))  # кириллическое имя файла не ломает путь


class ProductVariantsBugTest(CmsTestCase):
    """Пустые варианты не записывались — у товара оставались старые."""

    def test_empty_variants_clear_old(self):
        self.assertTrue(self.doc('products', 'p_badge')['variants'])
        r = self.post('/products/edit/p_badge', {'name': 'Значок колледжа', 'price': '1 200', 'url': '',
                                                 'variants_json': '[]'})
        self.assertRedirectsTo(r, '/products')
        d = self.doc('products', 'p_badge')
        self.assertEqual(d['variants'], [])
        self.assertIsInstance(d['price'], (int, float))
        self.assertEqual(d['price'], 1200)

    def test_variants_normalized_like_app(self):
        raw = [{'filterName': ' Размер ', 'options': [{'name': 'M', 'price': '1500'}, {'name': '', 'price': 5},
                                                       {'name': 'L', 'price': 'abc'}]},
               {'filterName': '', 'options': [{'name': 'X', 'price': 1}]},
               {'filterName': 'Пусто', 'options': []}]
        r = self.post('/products/edit/p_book', {'name': 'Учебник', 'price': '4500', 'url': 'https://example.com/b',
                                                'variants_json': json.dumps(raw)})
        self.assertRedirectsTo(r, '/products')
        v = self.doc('products', 'p_book')['variants']
        # как читает shop_screen.dart: [{filterName, options: [{name, price(num)}]}]
        self.assertEqual(v, [{'filterName': 'Размер', 'options': [{'name': 'M', 'price': 1500}, {'name': 'L', 'price': 0}]}])

    def test_javascript_url_rejected(self):
        r = self.post('/products/edit/p_book', {'name': 'Учебник', 'price': '10', 'url': 'javascript:alert(1)',
                                                'variants_json': '[]'})
        self.assertEqual(r.status_code, 200)  # форма с ошибкой
        self.assertNotEqual(self.doc('products', 'p_book').get('url'), 'javascript:alert(1)')


class PollSchemaBugTest(CmsTestCase):
    """Веб писал опросы не в схеме приложения и трактовал endTime как UTC."""

    def test_create_poll_in_app_schema(self):
        r = self.post('/poll/add', {
            'title': 'Где провести выпускной?', 'description': 'Голосуем до пятницы', 'target_audience': 'students',
            'poll_type': 'single', 'is_anonymous': '1', 'endTime': '2026-12-01T18:00',
            'options': ['Актовый зал', 'Кафе', ' ', 'Набережная'],
        })
        self.assertRedirectsTo(r, '/polls')
        polls = self.docs('polls', title='Где провести выпускной?')
        self.assertEqual(len(polls), 1)
        p = polls[0]
        self.assertEqual(p['target_audience'], 'students')
        self.assertEqual(p['poll_type'], 'single')
        self.assertIs(p['is_anonymous'], True)
        self.assertEqual(p['endTime'], datetime(2026, 12, 1, 13, 0, tzinfo=timezone.utc))  # 18:00 Алматы = 13:00 UTC
        self.assertEqual(p['createdBy'], ADMIN['uid'])
        self.assertIn('createdAt', p)
        from cms.core import db
        answers = [a.to_dict() for a in db.collection('polls').document(p['id']).collection('answers')
                   .order_by('createdAt').stream()]  # так читает приложение (orderBy createdAt)
        self.assertEqual([a['text'] for a in answers], ['Актовый зал', 'Кафе', 'Набережная'])
        for a in answers:
            self.assertIsInstance(a['votes'], int)
            self.assertEqual(a['votes'], 0)

    def test_edit_keeps_creator_and_converts_time(self):
        from cms.core import db
        db.collection('polls').document('poll_canteen').update({'createdBy': 'someone_else'})
        r = self.post('/poll/edit/poll_canteen', {
            'title': 'Оцените столовую', 'description': '', 'target_audience': 'all', 'poll_type': 'multiple',
            'endTime': '2026-11-20T09:30',
            'answer_ids': ['a0', 'a1', 'a2'], 'answer_text_a0': 'Отлично!', 'answer_text_a1': 'Нормально',
            'answer_text_a2': 'Плохо', 'options': ['Не хожу'],
        })
        self.assertRedirectsTo(r, '/polls')
        p = self.doc('polls', 'poll_canteen')
        self.assertEqual(p['createdBy'], 'someone_else')
        self.assertIs(p['is_anonymous'], False)  # галочка снята
        self.assertEqual(p['poll_type'], 'multiple')
        self.assertEqual(p['endTime'], datetime(2026, 11, 20, 4, 30, tzinfo=timezone.utc))
        a0 = self.doc('polls/poll_canteen/answers', 'a0')
        self.assertEqual(a0['text'], 'Отлично!')
        self.assertEqual(a0['votes'], 14)  # голоса не сброшены
        texts = sorted(d['text'] for d in self.docs('polls/poll_canteen/answers'))
        self.assertIn('Не хожу', texts)

    def test_public_poll_pages_closed(self):
        anon = self.app.test_client()
        for url in ('/active-polls', '/poll/poll_canteen/vote', '/poll/poll_club/results'):
            with self.subTest(url=url):
                r = anon.get(url)
                self.assertEqual(r.status_code, 302)
                self.assertIn('/login', r.headers['Location'])
        r = anon.post('/poll/poll_canteen/submit', data={'answer': 'a0'})
        self.assertIn(r.status_code, (302, 400, 404, 405))
        self.assertEqual(self.doc('polls/poll_canteen/answers', 'a0')['votes'], 14)


# ==================== НОВОСТИ ====================

class NewsTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_filters_and_search(self):
        from cms.core import db
        # аудитория — как в приложении: поле target_audience (без него новость видят все)
        db.collection('news').document('news_teachers').update({'target_audience': 'teachers'})
        html = self.get_ok('/news')
        self.assertIn('День открытых дверей 25 октября', html)
        self.assertIn('Старая новость', html)
        html = self.get_ok('/news?category=achievements')
        self.assertNotIn('Методический совет', html)
        html = self.get_ok('/news?audience=teachers')
        self.assertIn('Методический совет', html)
        self.assertNotIn('Студенты ПО 24-1', html)
        html = self.get_ok('/news?q=хакатон')
        self.assertIn('Студенты ПО 24-1 — призёры хакатона', html)
        self.assertNotIn('Методический совет', html)

    def test_form_pages(self):
        html = self.get_ok('/news/add')
        self.assertIn('data-cc-uploader', html)
        html = self.get_ok('/news/edit/news_open_day')
        self.assertIn('День открытых дверей 25 октября', html)
        r = self.client.get('/news/edit/nope')
        self.assertEqual(r.status_code, 302)

    def test_create_with_photos(self):
        r = self.post('/news/add', {
            'title': 'Новая новость с фото', 'content': 'Текст новости', 'description': '', 'category': 'sports',
            'target_audience': 'students',
            'images__state': json.dumps([{'kind': 'new', 'idx': 1}, {'kind': 'new', 'idx': 0}]),
            'images': [_png_file('a.png', (200, 10, 10)), _png_file('b.png', (10, 200, 10))],
        }, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/news')
        items = self.docs('news', title='Новая новость с фото')
        self.assertEqual(len(items), 1)
        n = items[0]
        self.assertEqual(n['category'], 'sports')
        self.assertEqual(n['target_audience'], 'students')
        self.assertIsNone(n['description'])
        self.assertEqual(len(n['images']), 2)
        self.assertTrue(all(im['path'].startswith('news/%s/' % n['id']) for im in n['images']))
        self.assertTrue(n['images'][0]['path'].endswith('_0.png'))
        self.assertEqual(n['imageUrl'], n['images'][0]['url'])
        self.assertEqual(n['createdBy'], ADMIN['uid'])
        self.assertTrue(self.audit_entries('create'))

    def test_validation_keeps_form(self):
        r = self.post('/news/add', {'title': '', 'content': 'x', 'category': 'academic', 'target_audience': 'all'})
        self.assertEqual(r.status_code, 200)
        self.assertIn('Укажите заголовок', r.get_data(as_text=True))
        r = self.post('/news/add', {'title': 'x', 'content': 'x', 'category': 'hack', 'target_audience': 'all'})
        self.assertEqual(r.status_code, 200)

    def test_not_image_rejected(self):
        r = self.post('/news/add', {
            'title': 'С плохим файлом', 'content': 'x', 'category': 'academic', 'target_audience': 'all',
            'images': (io.BytesIO(b'not an image at all'), 'x.png'),
        }, content_type='multipart/form-data')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.docs('news', title='С плохим файлом'), [])

    def test_delete_removes_files(self):
        paths = [im['path'] for im in self.doc('news', 'news_hackathon')['images']]
        self.assertTrue(all(_blob_exists(p) for p in paths))
        r = self.post('/news/delete/news_hackathon')
        self.assertRedirectsTo(r, '/news')
        self.assertIsNone(self.doc('news', 'news_hackathon'))
        self.assertFalse(any(_blob_exists(p) for p in paths))
        self.assertTrue(self.audit_entries('delete'))

    def test_bulk_delete(self):
        r = self.post('/news/bulk', {'action': 'delete', 'ids': ['news_schedule', 'news_teachers']})
        self.assertRedirectsTo(r, '/news')
        self.assertIsNone(self.doc('news', 'news_schedule'))
        self.assertIsNone(self.doc('news', 'news_teachers'))

    def test_delete_requires_post(self):
        r = self.client.get('/news/delete/news_open_day')
        self.assertEqual(r.status_code, 405)


# ==================== ОПРОСЫ ====================

class PollsTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_and_tabs(self):
        html = self.get_ok('/polls')
        self.assertIn('Оцените работу столовой', html)
        self.assertIn('Какой клуб открыть?', html)
        html = self.get_ok('/polls?status=active')
        self.assertIn('Оцените работу столовой', html)
        self.assertNotIn('Какой клуб открыть?', html)
        html = self.get_ok('/polls?status=finished')
        self.assertIn('Какой клуб открыть?', html)
        html = self.get_ok('/polls?q=клуб')
        self.assertNotIn('Оцените работу столовой', html)
        self.get_ok('/poll/add')
        self.get_ok('/poll/edit/poll_club')

    def test_results_merge_schemas_and_hide_anonymous(self):
        from cms.core import db
        # старый веб-формат голоса (poll_id/answer_id/user_id) — тоже учитывается
        db.collection('poll_responses').add({'poll_id': 'poll_club', 'answer_id': 'a1', 'user_id': self.seed['students'][1]})
        html = self.get_ok('/poll/poll_club/results')
        self.assertIn('Киберспорт', html)
        name0 = self.doc('users', self.seed['students'][0])['name']
        name1 = self.doc('users', self.seed['students'][1])['name']
        self.assertIn(name0, html)
        self.assertIn(name1, html)
        # анонимный опрос — имён нет
        db.collection('poll_responses').document('poll_canteen_' + self.seed['students'][2]).set(
            {'pollId': 'poll_canteen', 'userId': self.seed['students'][2], 'answers': ['a0']})
        html = self.get_ok('/poll/poll_canteen/results')
        self.assertNotIn(self.doc('users', self.seed['students'][2])['name'], html)
        self.assertIn('Анонимный', html)

    def test_results_csv(self):
        r = self.client.get('/poll/poll_club/results?format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertIn('text/csv', r.headers['Content-Type'])
        body = r.get_data(as_text=True)
        self.assertIn('Киберспорт', body)

    def test_close_and_extend(self):
        r = self.post('/poll/poll_canteen/close')
        self.assertRedirectsTo(r, '/polls')
        end = self.doc('polls', 'poll_canteen')['endTime']
        self.assertLessEqual(end, datetime.now(timezone.utc) + timedelta(seconds=5))
        r = self.post('/poll/poll_canteen/extend', {'days': '7'})
        self.assertRedirectsTo(r, '/polls')
        end = self.doc('polls', 'poll_canteen')['endTime']
        self.assertGreater(end, datetime.now(timezone.utc) + timedelta(days=6))

    def test_legacy_answer_endpoints(self):
        r = self.post('/poll/poll_club/add-answer', {'answer': 'Шахматы'})
        self.assertRedirectsTo(r, '/polls/poll_club/edit')
        added = self.docs('polls/poll_club/answers', text='Шахматы')
        self.assertEqual(len(added), 1)
        self.assertIn('createdAt', added[0])
        r = self.post('/poll/poll_club/delete-answer/%s' % added[0]['id'])
        self.assertRedirectsTo(r, '/polls/poll_club/edit')
        self.assertEqual(self.docs('polls/poll_club/answers', text='Шахматы'), [])

    def test_delete_cascades(self):
        r = self.post('/poll/delete/poll_club')
        self.assertRedirectsTo(r, '/polls')
        self.assertIsNone(self.doc('polls', 'poll_club'))
        self.assertEqual(self.docs('polls/poll_club/answers'), [])
        self.assertEqual(self.docs('poll_responses', pollId='poll_club'), [])
        self.assertEqual(self.docs('poll_responses', poll_id='poll_club'), [])

    def test_validation(self):
        r = self.post('/poll/add', {'title': 'Один вариант', 'target_audience': 'all', 'poll_type': 'single',
                                    'options': ['Только один']})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.docs('polls', title='Один вариант'), [])

    def test_edit_validation_keeps_entered_state(self):
        r = self.post('/poll/edit/poll_canteen', {
            'title': 'Оцените столовую', 'target_audience': 'all', 'poll_type': 'single',
            'answer_ids': ['a2', 'a0', 'a1'], 'answer_text_a0': '', 'answer_text_a1': 'Сойдёт', 'answer_text_a2': 'Плохо',
            'delete_answers': ['a1'],
        })
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('Текст варианта не может быть пустым', html)
        self.assertIn('Сойдёт', html)          # введённый текст не потерян
        self.assertIn('is-deleted', html)      # отмеченный на удаление вариант так и показан
        self.assertEqual(self.doc('polls/poll_canteen/answers', 'a0')['text'], 'Отлично')  # ничего не записано


# ==================== СОБЫТИЯ ====================

class EventsTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_tabs(self):
        html = self.get_ok('/events')
        self.assertIn('День открытых дверей', html)
        html = self.get_ok('/events?when=upcoming')
        self.assertIn('День открытых дверей', html)
        self.assertNotIn('Спартакиада колледжа', html)
        html = self.get_ok('/events?when=past')
        self.assertIn('Спартакиада колледжа', html)
        self.get_ok('/events/add')
        self.get_ok('/events/edit/ev_openday')

    def test_create_event_like_app(self):
        r = self.post('/events/add', {
            'title': 'Ярмарка вакансий', 'description': 'Работодатели региона', 'date': '2026-11-05', 'time': '14:30',
            'location': 'Холл 1 этажа', 'organizer': 'Центр карьеры',
            'image__state': json.dumps([{'kind': 'new', 'idx': 0}]), 'image': _png_file(),
        }, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/events')
        ev = self.docs('events', title='Ярмарка вакансий')[0]
        # приложение читает date как Timestamp и сортирует по нему; полночь по Алматы
        self.assertEqual(ev['date'], datetime(2026, 11, 4, 19, 0, tzinfo=timezone.utc))
        self.assertEqual(ev['time'], '14:30')
        self.assertEqual(ev['location'], 'Холл 1 этажа')
        self.assertEqual(ev['organizer'], 'Центр карьеры')
        self.assertEqual(ev['registrationsCount'], 0)
        self.assertIn('token=', ev['imageUrl'])
        self.assertNotIn('images', ev)  # у событий одна картинка (imageUrl), как в приложении

    def test_registrations_names_and_csv(self):
        html = self.get_ok('/events/registrations/ev_openday')
        for uid in self.seed['students'][:2]:
            self.assertIn(self.doc('users', uid)['name'], html)
        r = self.client.get('/events/registrations/ev_openday?format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertIn(self.doc('users', self.seed['students'][0])['name'], r.get_data(as_text=True))

    def test_delete_registration_and_counter(self):
        uid = self.seed['students'][0]
        r = self.post('/events/registrations/delete/ev_openday/%s' % uid)
        self.assertRedirectsTo(r, '/events/registrations/ev_openday')
        self.assertIsNone(self.doc('events/ev_openday/registrations', uid))
        self.assertEqual(self.doc('events', 'ev_openday')['registrationsCount'], 1)
        # повторное удаление того же участника не уводит счётчик вниз
        self.post('/events/registrations/delete/ev_openday/%s' % uid)
        self.assertEqual(self.doc('events', 'ev_openday')['registrationsCount'], 1)

    def test_recount(self):
        from cms.core import db
        db.collection('events').document('ev_sport').update({'registrationsCount': -3})
        r = self.post('/events/recount/ev_sport')
        self.assertRedirectsTo(r, '/events/registrations/ev_sport')
        self.assertEqual(self.doc('events', 'ev_sport')['registrationsCount'], 0)

    def test_remind(self):
        from cms import core
        from cms.core import db
        db.collection('users').document(self.seed['students'][0]).update({'fcmToken': 'tok-123'})
        core.sent_pushes.clear()
        r = self.post('/events/remind/ev_openday', {'message': 'Ждём вас в 11:00'})
        self.assertRedirectsTo(r, '/events/registrations/ev_openday')
        self.assertEqual([p['token'] for p in core.sent_pushes], ['tok-123'])

    def test_delete_cascade(self):
        r = self.post('/events/delete/ev_openday')
        self.assertRedirectsTo(r, '/events')
        self.assertIsNone(self.doc('events', 'ev_openday'))
        self.assertEqual(self.docs('events/ev_openday/registrations'), [])


# ==================== КЛУБЫ ====================

class ClubsTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_and_forms(self):
        html = self.get_ok('/clubs')
        self.assertIn('Робототехника', html)  # у старых клубов название в поле name
        html = self.get_ok('/clubs?category=community')
        self.assertIn('Дебатный клуб', html)
        self.assertNotIn('Робототехника', html)
        self.get_ok('/clubs/add')
        self.get_ok('/clubs/edit/club_robotics')

    def test_registrations_show_user_names(self):
        html = self.get_ok('/clubs/registrations/club_robotics')
        for uid in self.seed['students'][2:5]:
            u = self.doc('users', uid)
            self.assertIn(u['name'], html)
            self.assertIn(u['group'], html)
        self.assertNotIn('Без имени', html)

    def test_create_and_edit(self):
        r = self.post('/clubs/add', {'title': 'Шахматный клуб', 'description': 'Турниры', 'category': 'creative',
                                     'meetingTime': 'Пт 16:00', 'location': '204', 'curator': 'Петров Д. И.'})
        self.assertRedirectsTo(r, '/clubs')
        c = self.docs('clubs', title='Шахматный клуб')[0]
        self.assertEqual(c['registrationsCount'], 0)
        self.assertEqual(c['category'], 'creative')
        r = self.post('/clubs/edit/%s' % c['id'], {'title': 'Шахматы', 'description': 'Турниры', 'category': 'creative',
                                                    'meetingTime': 'Пт 17:00', 'location': '204', 'curator': ''})
        self.assertRedirectsTo(r, '/clubs')
        c2 = self.doc('clubs', c['id'])
        self.assertEqual(c2['title'], 'Шахматы')
        self.assertEqual(c2['meetingTime'], 'Пт 17:00')
        self.assertIn('updatedAt', c2)

    def test_delete_registration_transactional(self):
        uid = self.seed['students'][2]
        self.post('/clubs/registration/delete/club_robotics/%s' % uid)
        self.assertEqual(self.doc('clubs', 'club_robotics')['registrationsCount'], 2)
        self.post('/clubs/registration/delete/club_robotics/%s' % uid)
        self.assertEqual(self.doc('clubs', 'club_robotics')['registrationsCount'], 2)

    def test_delete_cascade(self):
        r = self.post('/clubs/delete/club_robotics')
        self.assertRedirectsTo(r, '/clubs')
        self.assertIsNone(self.doc('clubs', 'club_robotics'))
        self.assertEqual(self.docs('clubs/club_robotics/registrations'), [])


# ==================== ТОВАРЫ ====================

class ProductsTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_and_forms(self):
        html = self.get_ok('/products')
        self.assertIn('Значок колледжа', html)
        self.get_ok('/products/add')
        self.get_ok('/products/edit/p_badge')
        html = self.get_ok('/products?q=учебник')
        self.assertNotIn('Значок колледжа', html)

    def test_create_with_image_and_delete(self):
        variants = [{'filterName': 'Цвет', 'options': [{'name': 'Синий', 'price': 1000}]}]
        r = self.post('/products/add', {
            'name': 'Блокнот', 'price': '1000', 'url': 'https://example.com/n', 'variants_json': json.dumps(variants),
            'image__state': json.dumps([{'kind': 'new', 'idx': 0}]), 'image': _png_file(),
        }, content_type='multipart/form-data')
        self.assertRedirectsTo(r, '/products')
        p = self.docs('products', name='Блокнот')[0]
        self.assertEqual(p['variants'], variants)
        self.assertIn('createdAt', p)
        path = p['imageUrl'].split('/o/')[1].split('?')[0].replace('%2F', '/')
        self.assertTrue(_blob_exists(path))
        self.post('/products/delete/%s' % p['id'])
        self.assertIsNone(self.doc('products', p['id']))
        self.assertFalse(_blob_exists(path))


# ==================== ОБРАТНАЯ СВЯЗЬ ====================

class FeedbackTest(CmsTestCase):
    fresh_seed_each_test = True  # тесты удаляют/меняют общие документы

    def test_list_filters(self):
        html = self.get_ok('/feedback')
        self.assertIn('Можно ли перенести пересдачу по математике?', html)
        html = self.get_ok('/feedback?status=answered')
        self.assertIn('Не работает Wi-Fi на 3 этаже.', html)
        self.assertNotIn('Можно ли перенести пересдачу', html)
        html = self.get_ok('/feedback?anon=1')
        self.assertIn('Хочу сообщить о буллинге в группе.', html)
        self.assertNotIn('Можно ли перенести пересдачу', html)
        # анонимное обращение не раскрывает автора
        self.assertNotIn(self.doc('users', self.seed['students'][2])['name'], html)
        html = self.get_ok('/feedback?q=столов')
        self.assertIn('вегетарианских', html)

    def test_reply_sets_status_and_notifies(self):
        from cms import core
        from cms.core import db
        uid = self.seed['students'][0]
        db.collection('users').document(uid).update({'fcmToken': 'tok-fb'})
        core.sent_pushes.clear()
        r = self.post('/feedback/reply/fb_0', {'reply_text': 'Да, напишите куратору.'})
        self.assertRedirectsTo(r, '/feedback')
        d = self.doc('feedback', 'fb_0')
        self.assertEqual(d['reply'], 'Да, напишите куратору.')
        self.assertEqual(d['status'], 'answered')
        self.assertEqual(d['repliedBy'], ADMIN['uid'])
        notes = self.docs('notifications', feedbackId='fb_0')
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]['userId'], uid)
        self.assertEqual(notes[0]['type'], 'answer')
        self.assertEqual([p['token'] for p in core.sent_pushes], ['tok-fb'])

    def test_reply_without_user_skips_notification(self):
        from cms.core import db
        db.collection('feedback').document('fb_1').update({'userId': None})
        self.post('/feedback/reply/fb_1', {'reply_text': 'Спасибо'})
        self.assertEqual(self.docs('notifications', feedbackId='fb_1'), [])
        self.assertEqual(self.doc('feedback', 'fb_1')['status'], 'answered')

    def test_empty_reply_rejected(self):
        self.post('/feedback/reply/fb_2', {'reply_text': '   '})
        self.assertNotIn('reply', self.doc('feedback', 'fb_2'))

    def test_status_and_bulk_and_csv(self):
        r = self.post('/feedback/status/fb_3', {'status': 'closed'})
        self.assertRedirectsTo(r, '/feedback')
        self.assertEqual(self.doc('feedback', 'fb_3')['status'], 'closed')
        r = self.post('/feedback/status/fb_3', {'status': 'hack'})
        self.assertEqual(self.doc('feedback', 'fb_3')['status'], 'closed')
        r = self.post('/feedback/bulk', {'action': 'delete', 'ids': ['fb_4']})
        self.assertRedirectsTo(r, '/feedback')
        self.assertIsNone(self.doc('feedback', 'fb_4'))
        r = self.client.get('/feedback?format=csv')
        self.assertEqual(r.status_code, 200)
        self.assertIn('Можно ли перенести', r.get_data(as_text=True))

    def test_nav_badge_counts_unanswered(self):
        from cms import routes_content
        self.assertEqual(routes_content.nav_badges()['feedback_list'], 3)


class XssTest(CmsTestCase):
    """Пользовательский текст не попадает в HTML без экранирования."""

    def test_feedback_and_news_escaped(self):
        from cms.core import db
        payload = '<script>alert(1)</script>'
        db.collection('feedback').document('fb_xss').set({'topic': payload, 'message': payload, 'status': 'new',
                                                          'createdAt': datetime.now(timezone.utc)})
        db.collection('news').document('news_xss').set({'title': payload, 'content': payload, 'category': 'academic',
                                                        'createdAt': datetime.now(timezone.utc)})
        from cms.core import cache
        cache.invalidate('content:feedback')
        cache.invalidate('content:news')
        for url in ('/feedback', '/news', '/news/edit/news_xss'):
            with self.subTest(url=url):
                html = self.get_ok(url)
                self.assertNotIn(payload, html)
