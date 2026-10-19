/* Раздел «Люди» (PEOPLE): поля по роли, генератор пароля, модалки «Перевести в группу / курс» (одного или
   выбранных), переименование группы и дисциплины, «Передать занятия…», порядок администрации, превью карточки.
   Выбор строк и панель массовых действий — общие (app.js: ui.select_* + ui.bulkbar).
   Виджеты внутри быстрого просмотра инициализируются через CC.onInit (событие cc:content-loaded).
   Пользовательские данные — только через value/textContent (никакого innerHTML). */
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
  // инициализатор для страницы и для фрагментов быстрого просмотра
  function onInit(fn) {
    if (window.CC && CC.onInit) { CC.onInit(fn); return; }
    ready(function () { fn(doc); });
    doc.addEventListener('cc:content-loaded', function (e) { fn((e.detail && e.detail.root) || doc); });
  }

  /* ----------------------------- Поля по роли ----------------------------- */
  // select[data-pp-role-select] управляет [data-pp-role-only="student"] / "teacher,admin,club" в своей форме.
  onInit(function (root) {
    qsa('select[data-pp-role-select]', root).forEach(function (select) {
      if (select._ppRole) return;
      select._ppRole = true;
      var form = select.form || root;
      function sync() {
        qsa('[data-pp-role-only]', form).forEach(function (el) {
          el.hidden = el.getAttribute('data-pp-role-only').split(',').indexOf(select.value) === -1;
        });
      }
      select.addEventListener('change', sync);
      sync();
    });
  });

  /* ----------------------------- Генератор пароля ----------------------------- */
  // [data-pp-generate="#inputId"] — 10 символов без похожих (0/O, 1/l/I), видимым текстом, сразу в буфер.
  var ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789';
  function randomPassword(len) {
    var out = '';
    var buf = new Uint32Array(len);
    if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(buf);
    else for (var i = 0; i < len; i++) buf[i] = Math.floor(Math.random() * 4294967296);
    for (var j = 0; j < len; j++) out += ALPHABET.charAt(buf[j] % ALPHABET.length);
    // хотя бы одна цифра — так пароль легче продиктовать и он заметнее «не слово»
    if (!/\d/.test(out)) out = out.slice(0, -1) + String(buf[0] % 8 + 2);
    return out;
  }
  onInit(function (root) {
    qsa('[data-pp-generate]', root).forEach(function (btn) {
      if (btn._ppGen) return;
      btn._ppGen = true;
      btn.addEventListener('click', function () {
        var sel = btn.getAttribute('data-pp-generate');
        var input = qs(sel);
        if (!input) return;
        input.value = randomPassword(10);
        input.type = 'text';
        var toggle = qs('[data-password-toggle="' + sel + '"]');
        if (toggle) {
          toggle.setAttribute('aria-pressed', 'true');
          toggle.setAttribute('aria-label', 'Скрыть пароль');
          var i = qs('i', toggle); if (i) i.className = 'bi bi-eye-slash';
        }
        input.dispatchEvent(new Event('input', { bubbles: true }));
        input.focus(); input.select();
        if (window.CC && CC.copy) {
          CC.copy(input.value).then(function () { toast('Пароль создан и скопирован — передайте его лично', 'success'); },
            function () { toast('Пароль создан — скопируйте его', 'info'); });
        }
      });
    });
  });

  /* ----------------------------- «Перевести в группу» / «Назначить курс» ----------------------------- */
  // Модалки #usersGroupModal / #usersCourseModal шлют POST admin_users_bulk. Кто открыл:
  //   пункт меню строки [data-pp-user] — один студент; кнопка панели [data-pp-bulk="formId"] — выбранные строки.
  // id кладём скрытыми полями ids, в тексте модалки — кого затронет (изменятся только студенты).
  function initIdsModal(modal) {
    var box = qs('[data-pp-ids]', modal);
    var who = qs('[data-pp-who]', modal);
    var submit = qs('button[type="submit"]', modal);
    var groupSelect = qs('select[name="group"]', modal);
    modal.addEventListener('show.bs.modal', function (e) {
      var trigger = e.relatedTarget;
      var ids = [];
      var students = 0;
      box.textContent = '';
      if (trigger && trigger.hasAttribute('data-pp-user')) {
        ids = [trigger.getAttribute('data-pp-user')];
        students = 1;
        who.textContent = 'Студент: ' + (trigger.getAttribute('data-pp-user-name') || '');
        if (groupSelect) groupSelect.value = trigger.getAttribute('data-pp-user-group') || '';
      } else {
        var fid = (trigger && trigger.getAttribute('data-pp-bulk')) || 'usersBulk';
        ids = window.CC && CC.selection ? CC.selection(fid) : [];
        ids.forEach(function (id) {
          var cb = qsa('input[data-cc-select="' + fid + '"]').filter(function (b) { return b.value === id; })[0];
          var tr = cb && cb.closest('tr');
          if (tr && tr.getAttribute('data-pp-role') === 'student') students++;
        });
        var n = ids.length;
        var text = 'Выбрано: ' + n + ' ' + plural(n, ['пользователь', 'пользователя', 'пользователей']);
        if (!students) text += ' — среди них нет студентов, менять нечего.';
        else if (students < n) text += ', из них студентов: ' + students + ' — изменятся только они.';
        else text += '. Изменятся все ' + students + ' ' + plural(students, ['студент', 'студента', 'студентов']) + '.';
        who.textContent = text;
        if (groupSelect) groupSelect.value = '';
      }
      ids.forEach(function (id) {
        var input = doc.createElement('input');
        input.type = 'hidden'; input.name = 'ids'; input.value = id;
        box.appendChild(input);
      });
      if (submit) submit.disabled = !students;
    });
    modal.addEventListener('shown.bs.modal', function () { var s = qs('select', modal); if (s) s.focus(); });
  }

  /* ----------------------------- Переименование группы / дисциплины ----------------------------- */
  // Кто открыл модалку (пункт меню строки) — в его data-атрибутах старое название и URL формы.
  function initGroupRename(modal) {
    var old = qs('[data-pp-rename-old]', modal);
    var input = qs('[data-pp-rename-input]', modal);
    var label = qs('[data-pp-rename-label]', modal);
    modal.addEventListener('show.bs.modal', function (e) {
      var t = e.relatedTarget;
      if (!t || !t.hasAttribute('data-pp-rename')) return;
      var name = t.getAttribute('data-pp-rename') || '';
      old.value = name; input.value = name;
      if (label) label.textContent = 'Сейчас: «' + name + '»';
    });
    modal.addEventListener('shown.bs.modal', function () { input.focus(); input.select(); });
  }
  function initSubjectRename(modal) {
    var form = qs('form', modal);
    var input = qs('[data-pp-subject-input]', modal);
    var usage = qs('[data-pp-subject-usage]', modal);
    modal.addEventListener('show.bs.modal', function (e) {
      var t = e.relatedTarget;
      if (!t || !t.hasAttribute('data-pp-subject-action')) return;
      form.setAttribute('action', t.getAttribute('data-pp-subject-action'));
      input.value = t.getAttribute('data-pp-subject-name') || '';
      var n = parseInt(t.getAttribute('data-pp-subject-lessons') || '0', 10);
      usage.hidden = !n;
      usage.textContent = n ? 'В расписании занятий с этой дисциплиной: ' + n + '.' : '';
    });
    modal.addEventListener('shown.bs.modal', function () { input.focus(); input.select(); });
  }

  /* ----------------------------- «Передать занятия…» (карточка преподавателя) ----------------------------- */
  // Галочки дней [data-pp-day="N"] включают/выключают скрытые ids [data-pp-day-of="N"]; счётчик и кнопка.
  function initTransfer(modal) {
    var counter = qs('[data-pp-transfer-count]', modal);
    var submit = qs('button[type="submit"]', modal);
    function sync() {
      var n = 0;
      qsa('[data-pp-day]', modal).forEach(function (cb) {
        qsa('[data-pp-day-of="' + cb.getAttribute('data-pp-day') + '"]', modal).forEach(function (inp) {
          inp.disabled = !cb.checked;
          if (cb.checked) n++;
        });
      });
      if (counter) counter.textContent = n + ' ' + plural(n, ['занятие', 'занятия', 'занятий']);
      if (submit) submit.disabled = !n;
    }
    qsa('[data-pp-day]', modal).forEach(function (cb) { cb.addEventListener('change', sync); });
    sync();
  }

  /* ----------------------------- Порядок администрации ----------------------------- */
  function initOrder() {
    var list = qs('[data-pp-order-list]');
    var bar = qs('[data-pp-order-bar]');
    if (!list || !bar) return;
    var saveBtn = qs('[data-pp-order-save]', bar);
    var initial = order();
    var dirty = false;
    function order() { return qsa('[data-id]', list).map(function (li) { return li.getAttribute('data-id'); }); }
    function renumber() { qsa('[data-pp-pos]', list).forEach(function (el, i) { el.textContent = String(i + 1); }); }
    function changed() {
      renumber();
      dirty = order().join('|') !== initial.join('|');
      bar.hidden = !dirty;
    }
    if (window.Sortable) {
      window.Sortable.create(list, { handle: '[data-pp-handle]', animation: 160, onEnd: changed });
    }
    qsa('[data-pp-move]', list).forEach(function (btn) {
      btn.addEventListener('click', function () {
        var li = btn.closest('[data-id]');
        var dir = parseInt(btn.getAttribute('data-pp-move'), 10);
        if (dir < 0 && li.previousElementSibling) list.insertBefore(li, li.previousElementSibling);
        else if (dir > 0 && li.nextElementSibling) list.insertBefore(li.nextElementSibling, li);
        btn.focus();
        changed();
      });
    });
    qs('[data-pp-order-reset]', bar).addEventListener('click', function () {
      initial.forEach(function (id) {
        var li = qs('[data-id="' + (window.CSS && CSS.escape ? CSS.escape(id) : id) + '"]', list);
        if (li) list.appendChild(li);
      });
      changed();
    });
    saveBtn.addEventListener('click', function () {
      saveBtn.disabled = true;
      CC.fetchJSON(saveBtn.getAttribute('data-url'), { method: 'POST', body: { order: order() } }).then(function () {
        initial = order(); changed();
        toast('Порядок сохранён — в приложении он обновится в течение 10 минут', 'success');
      }, function (err) {
        toast((err && err.message) || 'Не удалось сохранить порядок', 'error');
      }).then(function () { saveBtn.disabled = false; });
    });
    window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
  }

  /* ----------------------------- Превью карточки администрации ----------------------------- */
  function initMgmtPreview() {
    var form = qs('[data-pp-mgmt-form]');
    if (!form) return;
    var name = qs('[data-pp-preview-name]');
    var pos = qs('[data-pp-preview-position]');
    var img = qs('[data-pp-preview-img]');
    var empty = qs('[data-pp-preview-empty]');
    var input = qs('[data-pp-photo-input]', form);
    var remove = qs('[data-pp-photo-remove]', form);
    var original = img ? img.getAttribute('src') : '';
    var objectUrl = null;
    function showImage(src) {
      if (!img) return;
      if (src) { img.src = src; img.hidden = false; empty.hidden = true; }
      else { img.hidden = true; img.removeAttribute('src'); empty.hidden = false; }
    }
    qsa('[data-pp-preview]', form).forEach(function (field) {
      field.addEventListener('input', function () {
        var target = field.getAttribute('data-pp-preview') === 'name' ? name : pos;
        target.textContent = field.value.trim() || (target === name ? 'ФИО' : 'Должность');
      });
    });
    if (input) {
      input.addEventListener('change', function () {
        if (objectUrl) { URL.revokeObjectURL(objectUrl); objectUrl = null; }
        var f = input.files && input.files[0];
        if (f && /^image\//.test(f.type)) {
          objectUrl = URL.createObjectURL(f);
          showImage(objectUrl);
          if (remove) remove.checked = false;
        } else {
          showImage(remove && remove.checked ? '' : original);
        }
      });
    }
    if (remove) {
      remove.addEventListener('change', function () {
        if (remove.checked) { if (input) input.value = ''; showImage(''); } else showImage(original);
      });
    }
  }

  /* ----------------------------- Тёзка при добавлении преподавателя ----------------------------- */
  // «Всё равно добавить» относится к конкретному ФИО: изменили имя — сервер снова проверит тёзок.
  function initDupConfirm(inp) {
    var name = inp.form && inp.form.elements.namedItem('name');
    if (!name) return;
    function norm(v) { return String(v || '').trim().toLowerCase().replace(/\s+/g, ' '); }
    var orig = norm(inp.getAttribute('data-name'));
    name.addEventListener('input', function () { inp.disabled = norm(name.value) !== orig; });
  }

  ready(function () {
    qsa('[data-pp-dup-confirm]').forEach(initDupConfirm);
    ['#usersGroupModal', '#usersCourseModal'].forEach(function (sel) { var m = qs(sel); if (m) initIdsModal(m); });
    var g = qs('#groupRename'); if (g) initGroupRename(g);
    var s = qs('#subjectRename'); if (s) initSubjectRename(s);
    var t = qs('#teacherTransfer'); if (t) initTransfer(t);
    initOrder();
    initMgmtPreview();
  });
})();
