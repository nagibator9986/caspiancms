/* ==========================================================================
   Публичная регистрация абитуриента /join (APPLICANTS). Минимум JS, без зависимостей:
   — подсказка «Вы будете входить как…» (та же логика, что cms/applicant_login.py, SPEC §1);
   — проверка совпадения паролей на лету;
   — фокус на сводке ошибок после неудачной отправки.
   Вся проверка — на сервере; без JS форма работает так же.
   ========================================================================== */
(function () {
  'use strict';

  function normalizePhone(raw) {
    var d = String(raw || '').replace(/\D/g, '');
    if (d.length === 11 && d.charAt(0) === '8') d = '7' + d.slice(1);
    if (d.length === 10) d = '7' + d;
    if (d.length !== 11 || d.charAt(0) !== '7') return null;
    return '+' + d;
  }

  /** Логин для показа или null (ввод не подходит). Email в регистрации не принимаем. */
  function displayLogin(input) {
    var s = String(input || '').trim();
    if (!s || s.indexOf('@') >= 0) return null;
    if (/^[0-9 ()+\-]+$/.test(s)) {
      var p = normalizePhone(s);
      return p ? '+7 ' + p.slice(2, 5) + ' ' + p.slice(5, 8) + ' ' + p.slice(8, 10) + ' ' + p.slice(10, 12) : null;
    }
    var l = s.toLowerCase();
    return /^[a-z][a-z0-9._]{2,23}$/.test(l) ? l : null;
  }

  function init() {
    var login = document.querySelector('[data-jn-login]');
    var preview = document.querySelector('[data-jn-preview]');
    if (login && preview) {
      var tpl = login.getAttribute('data-preview-text') || '{login}';
      var update = function () {
        var shown = displayLogin(login.value);
        preview.hidden = !shown;
        preview.textContent = shown ? tpl.replace('{login}', shown) : '';
      };
      login.addEventListener('input', update);
      update();
    }

    var pw = document.getElementById('jn-password');
    var repeat = document.querySelector('[data-jn-repeat]');
    var repeatErr = document.querySelector('[data-jn-repeat-error]');
    if (pw && repeat && repeatErr) {
      var touched = false;
      var check = function () {
        var bad = repeat.value !== '' && (touched || repeat.value.length >= pw.value.length) && repeat.value !== pw.value;
        repeat.classList.toggle('is-invalid', bad);
        if (bad) {
          repeat.setAttribute('aria-invalid', 'true');
          repeat.setAttribute('aria-describedby', repeatErr.id);
          repeatErr.textContent = repeat.getAttribute('data-mismatch') || '';
          repeatErr.classList.add('d-block');
        } else if (repeat.getAttribute('aria-invalid') === 'true' || !repeat.value) {
          repeat.removeAttribute('aria-invalid');
          repeatErr.classList.remove('d-block');
        }
      };
      repeat.addEventListener('input', check);
      pw.addEventListener('input', check);
      repeat.addEventListener('blur', function () { touched = true; check(); });
    }

    var summary = document.getElementById('jnErrors');
    if (summary) {
      summary.focus();
    } else {
      var firstBad = document.querySelector('[data-jn-form] [aria-invalid="true"]');
      if (firstBad) firstBad.focus();
    }
  }

  if (document.readyState !== 'loading') init(); else document.addEventListener('DOMContentLoaded', init);
})();
