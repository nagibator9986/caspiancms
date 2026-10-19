# cms/applicant_login.py — логин абитуриента → email Firebase Auth (SPEC §1).
#
# Абитуриент входит по телефону или латинскому логину. В Firebase Auth это
# синтетический email <local>@abiturient.student-caspian-lol.web.app (почта туда
# не доставляется). Контракт общий с приложением (ApplicantLogin в
# lib/services/applicant_service.dart) — тест-векторы одинаковые в обоих репозиториях.

import re

DOMAIN = 'abiturient.student-caspian-lol.web.app'

# Пароль, до которого админ сбрасывает пароль абитуриента (SPEC D5).
RESET_PASSWORD = 'abiturient123'
PASSWORD_MIN = 6
PASSWORD_MAX = 64

_USERNAME_RE = re.compile(r'[a-z][a-z0-9._]{2,23}')
_PHONE_CHARS_RE = re.compile(r'[0-9 ()+\-]+')


def normalize_phone(raw):
    """Порт ApplicantValidators.normalizePhone (APP): цифры из строки; 11 цифр с первой 8 →
    8 меняется на 7; ровно 10 цифр → '7' + цифры; итог — 11 цифр с первой 7 → '+7XXXXXXXXXX'.
    Иначе None."""
    digits = re.sub(r'\D', '', str(raw or ''))
    if len(digits) == 11 and digits.startswith('8'):
        digits = '7' + digits[1:]
    if len(digits) == 10:
        digits = '7' + digits
    if len(digits) != 11 or not digits.startswith('7'):
        return None
    return '+' + digits


def login_kind(value):
    """'email' | 'phone' | 'username' | None (невалидный ввод)."""
    s = str(value or '').strip()
    if not s:
        return None
    if '@' in s:
        return 'email'
    # Телефоном считаем ТОЛЬКО строку из цифр, пробелов и ()+- (иначе 'a1234567890' стал бы телефоном).
    if _PHONE_CHARS_RE.fullmatch(s) and normalize_phone(s) is not None:
        return 'phone'
    if _USERNAME_RE.fullmatch(s.lower()):
        return 'username'
    return None


def canonical_login(value):
    """Каноничный логин: телефон → '+7XXXXXXXXXX', логин/email → lower(); None, если невалиден."""
    kind = login_kind(value)
    s = str(value or '').strip()
    if kind == 'phone':
        return normalize_phone(s)
    if kind in ('username', 'email'):
        return s.lower()
    return None


def to_auth_email(value):
    """Ввод «Телефон или логин» → email для Firebase Auth или None.

    '8 701 123 45 67' → '77011234567@abiturient…', 'Aigerim.S' → 'aigerim.s@abiturient…',
    'Old@Mail.RU' → 'old@mail.ru' (старые аккаунты по реальному email)."""
    kind = login_kind(value)
    s = str(value or '').strip()
    if kind == 'email':
        return s.lower()
    if kind == 'phone':
        return normalize_phone(s)[1:] + '@' + DOMAIN
    if kind == 'username':
        return s.lower() + '@' + DOMAIN
    return None


def is_synthetic_email(email):
    """True для служебного адреса абитуриента (…@abiturient.student-caspian-lol.web.app)."""
    return str(email or '').strip().lower().endswith('@' + DOMAIN)


def display_login(email):
    """Как показывать логин: синтетический телефон '77011234567@…' → '+7 701 123 45 67',
    синтетический логин → local-part, обычный email → сам email."""
    e = str(email or '').strip()
    if not is_synthetic_email(e):
        return e
    local = e.split('@', 1)[0]
    if len(local) == 11 and local.isdigit() and local.startswith('7'):
        d = local[1:]
        return '+7 %s %s %s %s' % (d[0:3], d[3:6], d[6:8], d[8:10])
    return local


def password_error(password, repeat=None, allow_reset_password=False):
    """Текст ошибки пароля абитуриента или None, если пароль подходит.

    6–64 символа, без пробелов по краям (НЕ обрезаем — отклоняем), не равен abiturient123
    (кроме сброса админом: allow_reset_password=True). repeat (если передан) должен совпадать."""
    p = '' if password is None else str(password)
    if len(p) < PASSWORD_MIN:
        return 'Пароль должен быть не короче %d символов' % PASSWORD_MIN
    if len(p) > PASSWORD_MAX:
        return 'Пароль должен быть не длиннее %d символов' % PASSWORD_MAX
    if p != p.strip():
        return 'Пароль не должен начинаться или заканчиваться пробелом'
    if not allow_reset_password and p == RESET_PASSWORD:
        return 'Этот пароль нельзя использовать — придумайте другой'
    if repeat is not None and p != str(repeat):
        return 'Пароли не совпадают'
    return None
