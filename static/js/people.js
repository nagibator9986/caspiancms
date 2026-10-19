/* Разделы «Главная» и «Люди» (PEOPLE): массовый выбор пользователей, поля по роли, генератор
   пароля, модалки переименования группы/дисциплины, порядок администрации, превью карточки.
   Пользовательские данные — только через value/textContent (никакого innerHTML). */
(function () {
  'use strict';
  var doc = document;
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', fn); else fn(); }
  function toast(text, kind) { if (window.CC && CC.toast) CC.toast(text, kind || 'info'); }

  /* ----------------------------- Массовый выбор ----------------------------- */
  // [data-pp-check-all="formId"] — «выбрать все»; строки — input[name=ids][form=formId];
  // панель [data-pp-bulk="formId"] видна, когда что-то выбрано.
  function initBulk() {
    qsa('[data-pp-bulk]').forEach(function (bar) {
      var formId = bar.getAttribute('data-pp-bulk');
      var boxes = qsa('input[name="ids"][form="' + formId + '"]');
      var all = qsa('[data-pp-check-all="' + formId + '"]');
      var counter = qs('[data-pp-bulk-count]', bar);
      function sync() {
        var n = boxes.filter(function (b) { return b.checked; }).length;
        bar.hidden = n === 0;
        if (counter) counter.textContent = String(n);
        all.forEach(function (a) { a.checked = n > 0 && n === boxes.length; a.indeterminate = n > 0 && n < boxes.length; });
      }
      boxes.forEach(function (b) { b.addEventListener('change', sync); });
      all.forEach(function (a) {
        a.addEventListener('change', function () { boxes.forEach(function (b) { b.checked = a.checked; }); sync(); });
      });
      sync();
    });
    // клик по ячейке с галочкой — переключить галочку, а не открыть строку
    qsa('td.pp-check').forEach(function (td) {
      td.addEventListener('click', function (e) {
        e.stopPropagation();
        if (e.target.tagName === 'INPUT') return;
        var box = qs('input[type="checkbox"]', td);
        if (box) { box.checked = !box.checked; box.dispatchEvent(new Event('change', { bubbles: true })); }
      });
    });
  }

  /* ----------------------------- Поля по роли ----------------------------- */
  // select[data-pp-role-select] управляет [data-pp-role-only="student"] / "teacher,admin,club".
  function initRoleFields() {
    qsa('select[data-pp-role-select]').forEach(function (select) {
      var form = select.form || doc;
      function sync() {
        var role = select.value;
        qsa('[data-pp-role-only]', form).forEach(function (el) {
          var roles = el.getAttribute('data-pp-role-only').split(',');
          el.hidden = roles.indexOf(role) === -1;
        });
      }
      select.addEventListener('change', sync);
      sync();
    });
  }

  /* ----------------------------- Генератор пароля ----------------------------- */
  // [data-pp-generate="#inputId"] — 10 символов без похожих (0/O, 1/l/I), видимым текстом.
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
  function initGenerate() {
    qsa('[data-pp-generate]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var input = qs(btn.getAttribute('data-pp-generate'));
        if (!input) return;
        input.value = randomPassword(10);
        input.type = 'text';
        var toggle = qs('[data-password-toggle="' + btn.getAttribute('data-pp-generate') + '"]');
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
  }

  /* ----------------------------- Переименование группы ----------------------------- */
  function initGroupRename() {
    var modal = qs('#groupRename');
    if (!modal) return;
    var old = qs('[data-pp-rename-old]', modal);
    var input = qs('[data-pp-rename-input]', modal);
    var label = qs('[data-pp-rename-label]', modal);
    function fill(name) {
      old.value = name; input.value = name; label.textContent = '«' + name + '»';
    }
    qsa('[data-pp-rename]').forEach(function (btn) {
      btn.addEventListener('click', function () { fill(btn.getAttribute('data-pp-rename') || ''); });
    });
    modal.addEventListener('shown.bs.modal', function () { input.focus(); input.select(); });
  }

  /* ----------------------------- Переименование дисциплины ----------------------------- */
  function initSubjectRename() {
    var modal = qs('#subjectRename');
    if (!modal) return;
    var form = qs('[data-pp-subject-form]', modal);
    var input = qs('[data-pp-subject-input]', modal);
    var usage = qs('[data-pp-subject-usage]', modal);
    qsa('[data-pp-subject-action]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        form.setAttribute('action', btn.getAttribute('data-pp-subject-action'));
        input.value = btn.getAttribute('data-pp-subject-name') || '';
        var n = parseInt(btn.getAttribute('data-pp-subject-lessons') || '0', 10);
        usage.hidden = !n;
        usage.textContent = n ? 'В расписании занятий с этой дисциплиной: ' + n + '.' : '';
      });
    });
    modal.addEventListener('shown.bs.modal', function () { input.focus(); input.select(); });
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

  ready(function () {
    initBulk();
    initRoleFields();
    initGenerate();
    initGroupRename();
    initSubjectRename();
    initOrder();
    initMgmtPreview();
  });
})();
