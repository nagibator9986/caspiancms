/* Раздел «Академический календарь» (PEOPLE): модалка загрузки #calUpload.
   Выбор групп (поиск, «Все без календаря», «N курс», «Снять выбор», лимит), проверка файла на клиенте
   (PDF, не пустой, ≤ лимита), подтверждение пуша с числом групп, подстановка группы из меню строки («Заменить…»).
   Серверная проверка (routes_calendar) главная — здесь только удобство. Пользовательские данные — только через
   value/textContent (никакого innerHTML). «Выберите группу» — setCustomValidity у поля поиска: его показывает общая
   клиентская проверка форм app.js (CC.validateForm) под полем. */
(function () {
  'use strict';
  var doc = document;
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', fn); else fn(); }
  function toast(text, kind) { if (window.CC && CC.toast) CC.toast(text, kind || 'info'); }
  function plural(n, forms) {
    if (window.CC && CC.plural) return CC.plural(n, forms);
    var m10 = n % 10, m100 = n % 100;
    return forms[(m10 === 1 && m100 !== 11) ? 0 : (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) ? 1 : 2];
  }

  function initUpload(modal) {
    var form = qs('form', modal);
    var file = qs('[data-cal-file]', modal);
    var picker = qs('[data-cal-picker]', modal);
    var list = qs('#calGroupList', modal);
    var search = qs('[data-cal-search]', modal);
    var counter = qs('[data-cal-count]', modal);
    var note = qs('[data-cal-note]', modal);
    var notify = qs('input[name="notify"]', modal);
    var title = qs('.modal-title', modal);
    if (!form || !file || !picker || !list) return;
    var defaultTitle = title ? title.textContent : '';
    var max = parseInt(picker.getAttribute('data-max'), 10) || 40;
    var maxMb = parseInt(file.getAttribute('data-max-mb'), 10) || 20;

    function boxes() { return qsa('input[name="groups"]', list); }
    function selected() { return boxes().filter(function (b) { return b.checked; }); }

    // Подтверждение пуша (политика §4.11: публикация с пушем — с аудиторией). Атрибут data-confirm на форме
    // читает общий обработчик app.js в момент отправки — обновляем его при каждом изменении выбора.
    function syncConfirm(n) {
      if (notify && notify.checked && n) {
        form.setAttribute('data-confirm', 'Уведомление получат студенты: ' + n + ' ' + plural(n, ['группа', 'группы', 'групп']) +
          '. Загрузить календарь и отправить?');
        form.setAttribute('data-confirm-ok', 'Загрузить и уведомить');
        form.setAttribute('data-confirm-title', 'Уведомить студентов?');
        form.setAttribute('data-confirm-variant', 'primary');
      } else {
        ['data-confirm', 'data-confirm-ok', 'data-confirm-title', 'data-confirm-variant'].forEach(function (a) { form.removeAttribute(a); });
      }
    }

    function sync() {
      var sel = selected(), n = sel.length;
      var replaced = sel.filter(function (b) { return b.getAttribute('data-has') === '1'; }).length;
      counter.textContent = String(n);
      // лимит за один раз: когда набрано max, остальные галочки недоступны (выбранные снимаются свободно)
      boxes().forEach(function (b) { b.disabled = !b.checked && n >= max; });
      list.classList.remove('is-invalid');
      if (note) {
        note.hidden = !(replaced || n >= max);
        note.textContent = '';
        if (replaced) {
          var b = doc.createElement('b');
          b.textContent = 'У ' + replaced + ' ' + plural(replaced, ['группы', 'групп', 'групп']) + ' календарь уже есть';
          note.appendChild(b);
          note.appendChild(doc.createTextNode(' — он будет заменён новым файлом.'));
        }
        if (n >= max) note.appendChild(doc.createTextNode((replaced ? ' ' : '') + 'Достигнут лимит: ' + max + ' ' + plural(max, ['группа', 'группы', 'групп']) + ' за один раз.'));
      }
      var wasInvalid = !!search && !search.validity.valid;
      if (search) search.setCustomValidity(n ? '' : 'Выберите хотя бы одну группу');
      // общая проверка форм снимает ошибку только у поля, в котором произошло событие — сообщаем ей про поиск
      if (search && wasInvalid && n) search.dispatchEvent(new Event('input', { bubbles: true }));
      syncConfirm(n);
    }

    function resetSearch() {
      if (!search || !search.value) return;
      search.value = '';
      if (window.CC && CC.filterItems) CC.filterItems(list, '');
    }

    // быстрый выбор: «Все без календаря», «N курс» — добавляют к выбранному; «Снять выбор» — очищает
    qsa('[data-cal-pick]', picker).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var kind = btn.getAttribute('data-cal-pick');
        var cut = 0;
        resetSearch();   // список целиком на виду — видно, что именно выбрано
        if (kind === 'clear') {
          boxes().forEach(function (b) { b.checked = false; });
        } else {
          var left = max - selected().length;
          boxes().forEach(function (b) {
            if (b.checked) return;
            var ok = kind === 'missing' ? b.getAttribute('data-has') !== '1' : b.getAttribute('data-course') === kind.slice(7);
            if (!ok) return;
            if (left <= 0) { cut++; return; }
            b.checked = true;
            left--;
          });
        }
        sync();
        if (cut) toast('Выбрано ' + max + ' — это максимум за один раз. Остальные группы загрузите отдельным заходом (ещё ' + cut + ').', 'warning');
      });
    });
    list.addEventListener('change', sync);
    if (notify) notify.addEventListener('change', function () { syncConfirm(selected().length); });
    // Enter в поиске групп не должен отправлять форму
    if (search) search.addEventListener('keydown', function (e) { if (e.key === 'Enter') e.preventDefault(); });

    // Файл: PDF, не пустой, не больше лимита — иначе форма не уходит (как и на сервере). Сообщение — строка под полем
    // сразу после выбора (тост на телефоне перекрыл бы кнопки модалки); отправку блокируем сами.
    var fileMsg = null;
    function fileProblem() {
      var f = file.files && file.files[0];
      if (!f) return '';
      if (f.size === 0) return 'Файл пустой';
      if (f.size > maxMb * 1048576) return 'Файл больше ' + maxMb + ' МБ — сожмите PDF';
      if (!/\.pdf$/i.test(f.name) && f.type !== 'application/pdf') return 'Нужен файл в формате PDF';
      return '';
    }
    function showFileProblem(msg) {
      if (msg && !fileMsg) {
        fileMsg = doc.createElement('div');
        fileMsg.className = 'invalid-feedback d-block';
        fileMsg.setAttribute('role', 'alert');
        file.insertAdjacentElement('afterend', fileMsg);
      }
      if (fileMsg) fileMsg.textContent = msg;
      if (fileMsg) fileMsg.hidden = !msg;
      file.classList.toggle('is-invalid', !!msg);
    }
    file.addEventListener('change', function () { showFileProblem(fileProblem()); });
    form.addEventListener('submit', function (e) {
      var msg = fileProblem();
      if (!msg) return;
      e.preventDefault();
      e.stopImmediatePropagation();   // ни подтверждение пуша, ни «Загружаем…» не должны сработать
      showFileProblem(msg);
      file.focus();
    });

    // «Загрузить…» / «Заменить…» из меню строки: выбрана одна эта группа; кнопка в шапке — выбор как был
    modal.addEventListener('show.bs.modal', function (e) {
      var t = e.relatedTarget;
      var group = t && t.getAttribute ? t.getAttribute('data-cal-group') : null;
      resetSearch();
      if (group === null) {
        if (title) title.textContent = defaultTitle;
        sync();
        return;
      }
      boxes().forEach(function (b) { b.checked = b.value === group; });
      if (title) title.textContent = t.getAttribute('data-cal-has') === '1' ? 'Заменить календарь' : 'Загрузить календарь';
      file.value = '';
      showFileProblem('');
      sync();
      var mine = boxes().filter(function (b) { return b.checked; })[0];
      if (mine && mine.scrollIntoView) { try { mine.scrollIntoView({ block: 'nearest' }); } catch (err) { /* старый браузер */ } }
    });
    modal.addEventListener('shown.bs.modal', function () { file.focus(); });
    // «Уведомить студентов» открывает подтверждение поверх этой модалки; Bootstrap при его закрытии снимает modal-open
    // с body, хотя наша модалка осталась — возвращаем, иначе фон под ней перестанет блокироваться
    var confirmModal = doc.getElementById('ccConfirm');
    if (confirmModal) confirmModal.addEventListener('hidden.bs.modal', function () {
      if (modal.classList.contains('show')) doc.body.classList.add('modal-open');
    });
    sync();
  }

  ready(function () {
    var modal = qs('#calUpload');
    if (modal) initUpload(modal);
  });
})();
