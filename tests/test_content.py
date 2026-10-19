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
        # входящие: без параметров — только «Новые» (в сиде их 3), отвеченное не показывается; «Все» — явный status=all
        self.assertNotIn('Не работает Wi-Fi на 3 этаже.', html)
        html = self.get_ok('/feedback?status=all')
        self.assertIn('Не работает Wi-Fi на 3 этаже.', html)
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


# ==================== UX v2 (UX_SPEC_v2 §5.3: CNT-1…CNT-9) ====================

def _main(html):
    """Только содержимое страницы (без сайдбара: в нём те же слова, что и в контенте)."""
    start = html.find('<main id="cc-main"')
    end = html.find('</main>', start)
    return html[start:end] if start != -1 else html


def _actions(flashes):
    """Действия тостов flash_action(): [dict] из служебных ('cc-action', json)."""
    return [json.loads(m) for c, m in flashes if c == 'cc-action']


class NewsUxTest(CmsTestCase):
    fresh_seed_each_test = True

    def test_list_patterns_and_legacy_categories(self):
        html = self.get_ok('/news')
        main = _main(html)
        # CNT-1: старые ключи категорий — по-русски и нейтрально (outline), без «сырых» events/achievements
        self.assertIn('Достижения', main)
        self.assertIn('Общее', main)
        self.assertNotIn('cc-badge--outline">achievements', main)
        self.assertNotIn('cc-badge--info', main)
        # одна основная кнопка (n), сводка вместо KPI, массовые действия — общая панель, строка-ссылка с next
        header = main[:main.find('cc-list')]   # шапка страницы (в шторке фильтров на телефоне — своя «Показать»)
        self.assertEqual(header.count('btn-primary'), 1)
        self.assertIn('data-cc-hotkey="n"', header)
        self.assertIn('cc-summary', main)
        self.assertNotIn('cc-stat__value', main)
        self.assertIn('data-cc-bulkbar="newsBulk"', main)
        self.assertNotIn('ct-bulk', main)
        self.assertIn('/news/edit/news_open_day?next=', main)
        self.assertNotIn('Новости абитуриентов', main)  # перекрёстная кнопка убрана (раздел второстепенный)
        # фильтр по старой категории есть в списке вариантов и работает
        self.assertIn('value="achievements"', main)
        html = _main(self.get_ok('/news?category=achievements'))
        self.assertIn('призёры хакатона', html)
        self.assertNotIn('Методический совет', html)
        # «не нашлось» отличается от «пусто»
        self.assertIn('Ничего не найдено по фильтрам', _main(self.get_ok('/news?q=несуществующее-слово')))

    def test_form_legacy_category_label_and_layout(self):
        html = self.get_ok('/news/edit/news_open_day?next=/news%3Fcategory%3Devents')
        self.assertIn('События (старая категория)', html)
        self.assertNotIn('events (старая категория)', html)
        self.assertIn('cc-form-layout', html)
        self.assertIn('data-cc-savebar', html)
        self.assertIn('name="next" value="/news?category=events"', html)
        self.assertIn('data-cc-back', html)
        # фото — сразу после текста, до категории
        self.assertLess(html.find('name="content"'), html.find('data-cc-uploader'))
        self.assertLess(html.find('data-cc-uploader'), html.find('name="category"'))
        # новая новость: подтверждение публикации с аудиторией уведомления и черновик в браузере
        html = self.get_ok('/news/add')
        self.assertIn('data-ct-publish-confirm', html)
        self.assertIn('data-cc-draft="news:new"', html)
        self.assertIn('все пользователи приложения', html)

    def test_create_returns_to_list_with_open_action(self):
        r = self.post('/news/add', {'title': 'Тост с действием', 'content': 'Текст', 'category': 'academic',
                                    'target_audience': 'students', 'next': '/news?audience=students'})
        self.assertRedirectsTo(r, '/news?audience=students')
        nid = self.docs('news', title='Тост с действием')[0]['id']
        self.assertIn(('success', 'Новость опубликована — приложение пришлёт уведомление'), self.flashes())
        self.assertEqual(_actions(self.flashes()), [{'label': 'Открыть', 'href': '/news/edit/%s' % nid}])

    def test_edit_returns_to_filtered_list_and_rejects_foreign_next(self):
        data = {'title': 'День открытых дверей 25 октября', 'content': 'x', 'category': 'academic', 'target_audience': 'all'}
        r = self.post('/news/edit/news_open_day', dict(data, next='/news?category=events&page=1'))
        self.assertRedirectsTo(r, '/news?category=events&page=1')
        r = self.post('/news/edit/news_open_day', dict(data, next='https://evil.example/x'))
        self.assertRedirectsTo(r, '/news')
        self.assertNotIn('evil', r.headers['Location'])

    def test_delete_from_menu_returns_to_list(self):
        r = self.post('/news/delete/news_schedule', {'next': '/news?photo=without'})
        self.assertRedirectsTo(r, '/news?photo=without')
        self.assertIsNone(self.doc('news', 'news_schedule'))


class PollsUxTest(CmsTestCase):
    fresh_seed_each_test = True

    def test_list_chips_and_menu(self):
        main = _main(self.get_ok('/polls'))
        self.assertIn('cc-chips', main)
        self.assertEqual(main[:main.find('cc-list')].count('btn-primary'), 1)
        self.assertIn('/poll/poll_canteen/close', main)      # «Завершить» — в меню ⋯ без модалки
        self.assertIn('/poll/poll_club/extend', main)
        self.assertIn('/polls/poll_canteen/edit?next=', main)
        main = _main(self.get_ok('/polls?status=finished'))
        self.assertIn('Какой клуб открыть?', main)
        self.assertNotIn('Оцените работу столовой', main)

    def test_close_has_undo_that_restores_end_time(self):
        before = self.doc('polls', 'poll_canteen')['endTime']
        r = self.post('/poll/poll_canteen/close', {'next': '/polls?status=active'})
        self.assertRedirectsTo(r, '/polls?status=active')
        self.assertLess(self.doc('polls', 'poll_canteen')['endTime'], before)
        action = _actions(self.flashes())[-1]
        self.assertEqual(action['label'], 'Отменить')
        self.assertEqual(action['post'], '/poll/poll_canteen/end')
        self.assertEqual(action['fields']['next'], '/polls?status=active')
        r = self.post(action['post'], action['fields'])
        self.assertRedirectsTo(r, '/polls?status=active')
        restored = self.doc('polls', 'poll_canteen')['endTime']
        self.assertLess(abs((restored - before).total_seconds()), 1)

    def test_extend_undo_and_set_end_validation(self):
        self.post('/poll/poll_club/extend', {'days': '3'})
        action = _actions(self.flashes())[-1]
        self.post(action['post'], action['fields'])
        self.assertLess(self.doc('polls', 'poll_club')['endTime'], datetime.now(timezone.utc))
        # без срока → 'none'; мусор не меняет срок
        self.post('/poll/poll_club/end', {'end': 'none'})
        self.assertIsNone(self.doc('polls', 'poll_club')['endTime'])
        self.post('/poll/poll_club/end', {'end': 'вчера'})
        self.assertIsNone(self.doc('polls', 'poll_club')['endTime'])
        self.assertEqual(self.client.get('/poll/poll_club/end').status_code, 405)

    def test_results_voters_known_or_unknown(self):
        # CNT-7: голоса есть, списка голосовавших нет → «неизвестно», а не «0»; KPI «Вариантов» убран
        main = _main(self.get_ok('/poll/poll_canteen/results'))
        self.assertIn('Проголосовали: неизвестно (анонимный опрос)', main)
        self.assertNotIn('Вариантов', main)
        self.assertIn('41', main)
        # неанонимный: «N из M (X%)», M — пользователи аудитории (студенты + преподаватели)
        from cms.core import cache
        cache.invalidate('content:audience')
        users = self.docs('users')
        eligible = sum(1 for u in users if u.get('role') in ('student', 'teacher'))
        from cms.core import fmt_num, fmt_pct
        main = _main(self.get_ok('/poll/poll_club/results'))
        self.assertGreater(eligible, 0)
        self.assertIn('из %s проголосовали (%s)' % (fmt_num(eligible), fmt_pct(1, eligible)), main)

    def test_create_toast_links_results(self):
        r = self.post('/polls/new', {'title': 'Опрос с тостом', 'target_audience': 'all', 'poll_type': 'single',
                                     'options': ['Да', 'Нет'], 'next': '/polls?status=active'})
        self.assertRedirectsTo(r, '/polls?status=active')
        pid = self.docs('polls', title='Опрос с тостом')[0]['id']
        actions = _actions(self.flashes())
        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]['label'], 'Результаты')
        self.assertTrue(actions[0]['href'].endswith('/%s/results' % pid), actions[0])


class EventsClubsProductsUxTest(CmsTestCase):
    fresh_seed_each_test = True

    def test_events_list_without_kpi_and_with_menu(self):
        main = _main(self.get_ok('/events'))
        self.assertNotIn('cc-stat__value', main)
        self.assertIn('cc-chips', main)
        self.assertIn('/events/remind/ev_openday', main)     # «Напомнить» в меню ⋯ (с подтверждением и аудиторией)
        self.assertIn('Уведомление получат участники: 2', main)
        self.assertIn('/events/recount/ev_openday', main)

    def test_event_create_toast_and_remind_back_to_list(self):
        r = self.post('/events/add', {'title': 'Субботник', 'date': '2026-11-07', 'time': '10:00', 'location': 'Двор',
                                      'organizer': '', 'description': ''})
        self.assertRedirectsTo(r, '/events')
        eid = self.docs('events', title='Субботник')[0]['id']
        self.assertEqual(_actions(self.flashes()), [{'label': 'Участники', 'href': '/events/registrations/%s' % eid}])
        r = self.post('/events/remind/ev_openday', {'next': '/events?when=upcoming'})
        self.assertRedirectsTo(r, '/events?when=upcoming')
        r = self.post('/events/recount/ev_openday', {'next': '/events'})
        self.assertRedirectsTo(r, '/events')

    def test_registrations_page_patterns(self):
        main = _main(self.get_ok('/events/registrations/ev_openday'))
        self.assertIn('data-cc-filter="#regsTable"', main)
        self.assertIn('ctRemind', main)
        self.assertNotIn('cc-stat__value', main)
        # без участников некого уведомлять — кнопки «Написать участникам» нет
        self.assertNotIn('ctRemind', _main(self.get_ok('/events/registrations/ev_sport')))

    def test_clubs_without_contradicting_kpi(self):
        main = _main(self.get_ok('/clubs'))
        self.assertNotIn('Творчество и IT', main)
        self.assertNotIn('cc-stat__value', main)
        self.assertIn('data-cc-filter="#clubGrid"', main)
        main = _main(self.get_ok('/clubs?category=tech'))
        self.assertIn('Робототехника', main)
        self.assertNotIn('Дебатный клуб', main)
        r = self.post('/clubs/add', {'title': 'Хор', 'category': 'creative', 'description': '', 'meetingTime': '',
                                     'location': '', 'curator': '', 'next': '/clubs?category=creative'})
        self.assertRedirectsTo(r, '/clubs?category=creative')
        cid = self.docs('clubs', title='Хор')[0]['id']
        self.assertEqual(_actions(self.flashes()), [{'label': 'Участники', 'href': '/clubs/registrations/%s' % cid}])

    def test_products_title_and_no_coins_link(self):
        main = _main(self.get_ok('/products'))
        self.assertIn('<h1>Товары колледжа</h1>', main)
        self.assertNotIn('Магазин коинов', main)
        self.assertNotIn('/coins/', main)
        self.assertIn('/products/edit/p_badge?next=', main)


class FeedbackInboxTest(CmsTestCase):
    fresh_seed_each_test = True

    def test_urgent_pinned_and_waiting_order(self):
        main = _main(self.get_ok('/feedback'))
        # «Линия доверия» / анонимное / тревожные слова — «Срочно» и наверху; дальше — кто дольше ждёт
        self.assertIn('Срочно', main)
        bullying = main.find('Хочу сообщить о буллинге')
        canteen = main.find('мало вегетарианских')
        retake = main.find('Можно ли перенести пересдачу')
        self.assertTrue(0 < bullying < canteen < retake, (bullying, canteen, retake))
        self.assertIn('ждёт 2 дн.', main)
        self.assertIn('data-cc-ctrl-enter', main)
        self.assertIn('data-ct-template=', main)                     # шаблоны ответа
        self.assertNotIn('ct-fb--new', main)                         # без цветных рамок — статус только бейджем
        self.assertEqual(main[:main.find('cc-list')].count('btn-primary'), 0)   # основная — только «Отправить» в ответе

    def test_urgent_keywords_normalized(self):
        from cms import routes_content as rc
        self.assertTrue(rc._feedback_urgency({'topic': 'Учёба', 'message': 'Меня ТРАВЯТ… это травля'}))
        self.assertTrue(rc._feedback_urgency({'topic': 'Другое', 'message': 'Угрозы в чате'}))
        self.assertTrue(rc._feedback_urgency({'topic': 'Другое', 'message': 'x', 'anonymousVisual': True}))
        self.assertIsNone(rc._feedback_urgency({'topic': 'Столовая', 'message': 'Мало блюд'}))

    def test_default_is_all_when_no_new_or_when_searching(self):
        main = _main(self.get_ok('/feedback?q=Wi-Fi'))      # поиск идёт по всем статусам
        self.assertIn('Не работает Wi-Fi', main)
        from cms.core import cache, db
        for k in range(3):
            db.collection('feedback').document('fb_%d' % k).update({'status': 'closed'})
        cache.invalidate('content:feedback')
        main = _main(self.get_ok('/feedback'))
        self.assertIn('Не работает Wi-Fi', main)            # новых нет — открываются все
        self.assertIn('Все обращения разобраны', main)

    def test_close_without_confirm_and_undo(self):
        main = _main(self.get_ok('/feedback'))
        form_start = main.find('action="/feedback/status/fb_0"')
        self.assertNotIn('data-confirm', main[main.rfind('<form', 0, form_start):form_start + 200])
        r = self.post('/feedback/status/fb_0', {'status': 'closed', 'next': '/feedback?topic=%D0%A3%D1%87%D1%91%D0%B1%D0%B0'})
        self.assertRedirectsTo(r, '/feedback?topic=')
        self.assertEqual(self.doc('feedback', 'fb_0')['status'], 'closed')
        action = _actions(self.flashes())[-1]
        self.assertEqual((action['label'], action['post'], action['fields']['status']), ('Отменить', '/feedback/status/fb_0', 'new'))
        self.post(action['post'], action['fields'])
        self.assertEqual(self.doc('feedback', 'fb_0')['status'], 'new')
        self.assertTrue(self.audit_entries('status'))

    def test_bulk_close_undo_restores_each_status(self):
        r = self.post('/feedback/bulk', {'action': 'close', 'ids': ['fb_1', 'fb_3', 'nope']})
        self.assertRedirectsTo(r, '/feedback')
        self.assertEqual(self.doc('feedback', 'fb_1')['status'], 'closed')
        self.assertEqual(self.doc('feedback', 'fb_3')['status'], 'closed')
        self.assertIsNone(self.doc('feedback', 'nope'))
        action = _actions(self.flashes())[-1]
        self.assertEqual(json.loads(action['fields']['restore']), {'fb_1': 'new', 'fb_3': 'answered'})
        self.post(action['post'], action['fields'])
        self.assertEqual(self.doc('feedback', 'fb_1')['status'], 'new')
        self.assertEqual(self.doc('feedback', 'fb_3')['status'], 'answered')
        # подделанный restore со статусом вне справочника ничего не меняет
        self.post('/feedback/bulk', {'action': 'restore', 'restore': json.dumps({'fb_1': 'hacked'})})
        self.assertEqual(self.doc('feedback', 'fb_1')['status'], 'new')
