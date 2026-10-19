# Тест-векторы логина абитуриента (SPEC §1) — одинаковые в CMS и приложении.
# Чистая логика: Firebase/эмулятор не нужны.

import unittest

from cms.applicant_login import (DOMAIN, canonical_login, display_login, is_synthetic_email,
                                 login_kind, normalize_phone, password_error, to_auth_email)

D = '@' + DOMAIN

VECTORS = [
    # вход, email, kind, canonical
    ('8 701 123 45 67', '77011234567' + D, 'phone', '+77011234567'),
    ('+7 (701) 123-45-67', '77011234567' + D, 'phone', '+77011234567'),
    ('7011234567', '77011234567' + D, 'phone', '+77011234567'),
    ('Aigerim.S', 'aigerim.s' + D, 'username', 'aigerim.s'),
    (' aigerim_2008 ', 'aigerim_2008' + D, 'username', 'aigerim_2008'),
    ('Old@Mail.RU', 'old@mail.ru', 'email', 'old@mail.ru'),
    ('Айгерим', None, None, None),
    ('12345', None, None, None),
    ('ab', None, None, None),
    ('1abc', None, None, None),
    ('a-b-c', None, None, None),
    ('a1234567890', 'a1234567890' + D, 'username', 'a1234567890'),
    ('+1 202 555 0100', None, None, None),
]


class ApplicantLoginVectorsTest(unittest.TestCase):

    def test_domain(self):
        self.assertEqual(DOMAIN, 'abiturient.student-caspian-lol.web.app')

    def test_vectors(self):
        for raw, email, kind, canonical in VECTORS:
            with self.subTest(raw=raw):
                self.assertEqual(to_auth_email(raw), email)
                self.assertEqual(login_kind(raw), kind)
                self.assertEqual(canonical_login(raw), canonical)

    def test_display_login(self):
        self.assertEqual(display_login('77011234567' + D), '+7 701 123 45 67')
        self.assertEqual(display_login('aigerim.s' + D), 'aigerim.s')
        self.assertEqual(display_login('old@mail.ru'), 'old@mail.ru')

    def test_is_synthetic(self):
        self.assertTrue(is_synthetic_email('77011234567' + D))
        self.assertTrue(is_synthetic_email('AIGERIM.S@ABITURIENT.STUDENT-CASPIAN-LOL.WEB.APP'))
        self.assertFalse(is_synthetic_email('old@mail.ru'))
        self.assertFalse(is_synthetic_email(None))

    def test_round_trip_display(self):
        for raw, email, kind, canonical in VECTORS:
            if email and kind == 'phone':
                self.assertEqual(display_login(email).replace(' ', ''), canonical)

    def test_empty_and_none(self):
        for raw in (None, '', '   '):
            self.assertIsNone(to_auth_email(raw))
            self.assertIsNone(login_kind(raw))
            self.assertIsNone(canonical_login(raw))

    def test_username_limits(self):
        self.assertEqual(login_kind('abc'), 'username')            # 3 символа — минимум
        self.assertEqual(login_kind('a' * 24), 'username')         # 24 — максимум
        self.assertIsNone(login_kind('a' * 25))
        self.assertIsNone(login_kind('_abc'))
        self.assertIsNone(login_kind('ab c'))

    def test_normalize_phone(self):
        self.assertEqual(normalize_phone('87011234567'), '+77011234567')
        self.assertEqual(normalize_phone('+7 701 123 45 67'), '+77011234567')
        self.assertIsNone(normalize_phone('97011234567'))
        self.assertIsNone(normalize_phone('123'))


class ApplicantPasswordTest(unittest.TestCase):

    def test_ok(self):
        self.assertIsNone(password_error('secret1', 'secret1'))
        self.assertIsNone(password_error('a' * 64))

    def test_length(self):
        self.assertIsNotNone(password_error('12345'))
        self.assertIsNotNone(password_error('a' * 65))

    def test_spaces_rejected_not_trimmed(self):
        self.assertIsNotNone(password_error(' secret1'))
        self.assertIsNotNone(password_error('secret1 '))
        self.assertIsNone(password_error('sec ret1'))

    def test_reset_password_forbidden_for_user(self):
        self.assertIsNotNone(password_error('abiturient123'))
        self.assertIsNone(password_error('abiturient123', allow_reset_password=True))

    def test_repeat(self):
        self.assertEqual(password_error('secret1', 'secret2'), 'Пароли не совпадают')


if __name__ == '__main__':
    unittest.main()
