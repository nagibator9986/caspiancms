/* Раздел «Контент» (CONTENT): превью «как в приложении» (новость, опрос, событие, клуб, товар), подтверждение
   публикации с аудиторией уведомления, варианты опроса (Enter — следующий вариант, минимум 2), срок опроса,
   конструктор вариантов товара, вставка фото из буфера, обратная связь (шаблоны ответа, черновик ответа).
   Без innerHTML с пользовательскими данными — только DOM/textContent. Общие виджеты (выбор строк, savebar,
   Ctrl+S, проверка форм) — в app.js. */
(function () {
  'use strict';
  var doc = document;
  var CC = window.CC || {};
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', fn); else fn(); }
  function debounce(fn, ms) {
    if (CC.debounce) return CC.debounce(fn, ms);
    var t; return function () { var a = arguments, s = this; clearTimeout(t); t = setTimeout(function () { fn.apply(s, a); }, ms); };
  }
  function labels() {
    try { return JSON.parse((qs('#ctPreviewLabels') || {}).textContent || '{}') || {}; } catch (e) { return {}; }
  }
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  var MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек'];
  var WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

  /* ----------------------------- Превью «как в приложении» ----------------------------- */
  // Поля формы → элементы [data-ct-out="name|запасное"] (textContent). Атрибуты вывода:
  //   data-ct-map="ключ ctPreviewLabels"  data-ct-format="date|datetime|money"  data-ct-bool="Текст, если отмечено"
  //   data-ct-skip="значение → пусто"  data-ct-tpl="до {}"  data-ct-lower  data-ct-clip="220"  data-ct-empty="заглушка"
  function format(kind, raw) {
    var m;
    if (kind === 'date' && (m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(raw))) {
      var d = new Date(+m[1], +m[2] - 1, +m[3]);
      return d.getDate() + ' ' + MONTHS[d.getMonth()] + ', ' + WEEKDAYS[d.getDay()];
    }
    if (kind === 'datetime' && (m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(raw))) {
      return m[3] + '.' + m[2] + ' ' + m[4] + ':' + m[5];
    }
    if (kind === 'money') {
      var n = parseFloat(String(raw).replace(/[\s ]/g, '').replace(',', '.'));
      if (isNaN(n)) return '';
      return (Math.round(n * 100) / 100).toLocaleString('ru-RU').replace(/ /g, ' ');
    }
    return raw;
  }
  function fieldValue(form, name) {
    var el = form.elements[name];
    if (!el) return '';
    if (typeof RadioNodeList !== 'undefined' && el instanceof RadioNodeList) el = el[0];
    if (!el) return '';
    if (el.type === 'checkbox') return el.checked ? '1' : '';
    return String(el.value || '').trim();
  }
  function initPreview(form) {
    var lbl = labels();
    var outs = qsa('[data-ct-out]');
    var card = qs('[data-ct-preview-card]');
    var media = card && qs('[data-ct-preview-media]', card);
    var img = media && qs('[data-ct-preview-img]', media);
    var empty = media && qs('[data-ct-preview-empty]', media);
    var dots = media && qs('[data-ct-preview-dots]', media);
    var optionsOut = qs('[data-ct-preview-options]');
    var uploader = qs('[data-cc-uploader]', form);
    var fixedRatio = media && media.hasAttribute('data-ct-fixed-ratio');

    if (img && !fixedRatio) {
      img.addEventListener('load', function () {
        // рамка как в приложении: по первому фото, но не уже 4:5 и не шире 1.91:1
        var w = img.naturalWidth, h = img.naturalHeight;
        media.style.aspectRatio = String((w > 0 && h > 0) ? Math.min(1.91, Math.max(0.8, w / h)) : 16 / 9);
      });
    }
    function renderOut(out) {
      var keys = out.getAttribute('data-ct-out').split('|'), raw = '';
      for (var i = 0; i < keys.length && !raw; i++) raw = fieldValue(form, keys[i]);
      var text = raw;
      if (out.hasAttribute('data-ct-bool')) text = raw ? out.getAttribute('data-ct-bool') : '';
      if (out.hasAttribute('data-ct-skip') && raw === out.getAttribute('data-ct-skip')) text = '';
      var map = out.getAttribute('data-ct-map');
      if (map && text) text = (lbl[map] || {})[raw] || raw;
      var fmt = out.getAttribute('data-ct-format');
      if (fmt && text) text = format(fmt, raw);
      if (text && out.hasAttribute('data-ct-lower')) text = text.toLowerCase();
      var clip = parseInt(out.getAttribute('data-ct-clip') || '0', 10);
      if (clip && text.length > clip) text = text.slice(0, clip).replace(/\s+\S*$/, '') + '…';
      var tpl = out.getAttribute('data-ct-tpl');
      if (tpl && text) text = tpl.replace('{}', text);
      out.textContent = text || out.getAttribute('data-ct-empty') || '';
    }
    function renderImages() {
      if (!media || !img) return;
      var thumbs = uploader ? qsa('.cc-uploader__item img', uploader) : [];
      if (thumbs.length) {
        if (img.getAttribute('src') !== thumbs[0].src) img.src = thumbs[0].src;
        img.hidden = false; if (empty) empty.hidden = true;
      } else {
        img.hidden = true; img.removeAttribute('src'); if (empty) empty.hidden = false;
        if (!fixedRatio) media.style.aspectRatio = '';
      }
      if (dots) {
        while (dots.firstChild) dots.removeChild(dots.firstChild);
        if (thumbs.length > 1) thumbs.forEach(function (_, i) { var s = doc.createElement('span'); if (i === 0) s.className = 'is-active'; dots.appendChild(s); });
      }
    }
    function renderOptions() {
      if (!optionsOut) return;
      var multiple = fieldValue(form, 'poll_type') === 'multiple';
      var texts = qsa('.ct-option:not(.is-deleted) input.form-control', form).map(function (i) { return i.value.trim(); });
      while (optionsOut.firstChild) optionsOut.removeChild(optionsOut.firstChild);
      var shown = texts.slice(0, 6);
      shown.forEach(function (t, k) {
        var li = doc.createElement('li');
        var ic = doc.createElement('i'); ic.className = 'bi ' + (multiple ? 'bi-square' : 'bi-circle'); ic.setAttribute('aria-hidden', 'true');
        var sp = doc.createElement('span'); sp.textContent = t || ('Вариант ' + (k + 1));
        if (!t) li.className = 'is-placeholder';
        li.appendChild(ic); li.appendChild(sp); optionsOut.appendChild(li);
      });
      if (texts.length > shown.length) {
        var more = doc.createElement('li'); more.className = 'is-placeholder';
        more.textContent = 'и ещё ' + (texts.length - shown.length);
        optionsOut.appendChild(more);
      }
    }
    var render = function () { outs.forEach(renderOut); renderImages(); renderOptions(); };
    form.addEventListener('input', render);
    form.addEventListener('change', render);
    form.addEventListener('click', function (e) { if (e.target.closest && e.target.closest('[data-ct-option-remove], [data-ct-option-move], [data-ct-option-add]')) setTimeout(render, 0); });
    form.addEventListener('cc:uploader-change', function () { setTimeout(render, 0); });
    form.addEventListener('ct:options-change', render);
    render();
  }

  /* ----------------------------- Подтверждение публикации с аудиторией пуша ----------------------------- */
  // form[data-ct-publish-confirm="Опубликовать новость?"]: после проверки полей (app.js, фаза захвата на document) —
  // «Уведомление получат: студенты». Слушатель на форме в фазе захвата — раньше «грязного» индикатора и черновика,
  // поэтому при «Отмене» форма остаётся несохранённой.
  function initPublishConfirm(form) {
    var question = form.getAttribute('data-ct-publish-confirm') || 'Опубликовать?';
    form.addEventListener('submit', function (e) {
      if (form._ctConfirmed) { form._ctConfirmed = false; return; }
      if (e.defaultPrevented || !CC.confirm) return;
      var sub = e.submitter;
      if (sub && sub.hasAttribute('formaction')) return;  // не основная кнопка (например, «Удалить»)
      e.preventDefault(); e.stopImmediatePropagation();
      var aud = (labels().push || {})[fieldValue(form, 'target_audience')] || 'все пользователи приложения';
      CC.confirm(question + ' Уведомление получат: ' + aud + '.', { title: 'Публикация с уведомлением', okText: 'Опубликовать', danger: false })
        .then(function (ok) {
          if (!ok) return;
          form._ctConfirmed = true;
          if (form.requestSubmit) form.requestSubmit(sub && sub.form === form ? sub : undefined);
          else { form._ctConfirmed = false; form.submit(); }
        });
    }, true);
  }

  /* ----------------------------- Варианты ответа опроса ----------------------------- */
  function initPollOptions() {
    var box = qs('[data-ct-options]');
    if (!box) return;
    var form = box.closest('form');
    var max = parseInt(box.getAttribute('data-max') || '20', 10);
    var tpl = qs('[data-ct-option-template]');
    var addBtn = qs('[data-ct-option-add]');
    function rows() { return qsa('.ct-option:not(.is-deleted)', box); }
    function changed() { syncValidity(); if (form) form.dispatchEvent(new CustomEvent('ct:options-change')); }
    // минимум 2 варианта: подсказка на первом пустом новом поле — клиентская проверка (app.js) покажет её до отправки
    function syncValidity() {
      var live = rows(), filled = 0, firstEmpty = null;
      live.forEach(function (r) {
        var input = qs('input.form-control', r);
        if (!input) return;
        if (r.getAttribute('data-ct-option') === 'existing' || input.value.trim()) filled++;
        else if (!firstEmpty) firstEmpty = input;
      });
      qsa('input[name="options"]', box).forEach(function (i) { i.setCustomValidity(''); });
      if (filled < 2) {
        if (!firstEmpty && live.length < max) firstEmpty = addOption(false);
        if (firstEmpty) firstEmpty.setCustomValidity('Нужно минимум 2 варианта ответа');
      }
      if (addBtn) addBtn.disabled = live.length >= max;
    }
    function addOption(focus) {
      if (!tpl || rows().length >= max) return null;
      var node = tpl.content.firstElementChild.cloneNode(true);
      var input = qs('input', node);
      if (input) input.placeholder = 'Вариант ' + (rows().length + 1);
      box.appendChild(node);
      if (focus && input) input.focus();
      return input;
    }

    box.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn || !box.contains(btn)) return;
      var row = btn.closest('.ct-option');
      if (btn.hasAttribute('data-ct-option-remove')) {
        if (row.getAttribute('data-ct-option') === 'new') {
          var next = row.nextElementSibling || row.previousElementSibling;
          row.parentNode.removeChild(row);
          var ni = next && qs('input.form-control', next); if (ni) ni.focus();
        } else {
          var del = qs('[data-ct-option-delete]', row);
          var on = !row.classList.contains('is-deleted');
          row.classList.toggle('is-deleted', on);
          del.disabled = !on;
          btn.setAttribute('aria-pressed', on ? 'true' : 'false');
          btn.setAttribute('aria-label', on ? 'Вернуть вариант' : 'Удалить вариант');
          btn.title = on ? 'Вернуть вариант' : 'Удалить вариант';
          var icon = qs('i', btn); if (icon) icon.className = 'bi ' + (on ? 'bi-arrow-counterclockwise' : 'bi-x-lg');
        }
        form.dispatchEvent(new Event('change', { bubbles: true }));
        changed();
      } else if (btn.hasAttribute('data-ct-option-move')) {
        var dir = parseInt(btn.getAttribute('data-ct-option-move'), 10);
        var sib = dir < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (!sib || sib.getAttribute('data-ct-option') !== 'existing') return;
        if (dir < 0) box.insertBefore(row, sib); else box.insertBefore(sib, row);
        btn.focus();
        form.dispatchEvent(new Event('change', { bubbles: true }));
        changed();
      }
    });
    // Enter в варианте — к следующему варианту, в последнем — новый вариант (форма не отправляется)
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.metaKey || e.isComposing) return;
      var input = e.target;
      if (!input.matches || !input.matches('input.form-control')) return;
      e.preventDefault();
      var live = rows(), idx = live.indexOf(input.closest('.ct-option'));
      var next = live[idx + 1];
      if (next) { qs('input.form-control', next).focus(); return; }
      if (!input.value.trim()) return;
      if (addOption(true)) { form.dispatchEvent(new Event('change', { bubbles: true })); changed(); }
    });
    box.addEventListener('input', syncValidity);
    if (addBtn) addBtn.addEventListener('click', function () { if (addOption(true)) changed(); });
    syncValidity();
  }

  /* ----------------------------- Срок опроса: быстрые кнопки ----------------------------- */
  function almatyInput(ms) {
    // поле трактуется сервером как время Алматы (UTC+5) независимо от пояса браузера
    var d = new Date(ms + 5 * 3600 * 1000);
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate()) + 'T' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
  }
  function initEndTime() {
    var input = qs('[data-ct-endtime]');
    if (!input) return;
    qsa('[data-ct-endtime-add]').forEach(function (b) {
      b.addEventListener('click', function () {
        var days = parseInt(b.getAttribute('data-ct-endtime-add'), 10) || 1;
        var hour = 3600 * 1000;
        input.value = almatyInput(Math.ceil((Date.now() + days * 24 * hour) / hour) * hour);  // до целого часа
        input.dispatchEvent(new Event('change', { bubbles: true }));
      });
    });
    var clear = qs('[data-ct-endtime-clear]');
    if (clear) clear.addEventListener('click', function () { input.value = ''; input.dispatchEvent(new Event('change', { bubbles: true })); });
  }

  /* ----------------------------- Варианты товара ----------------------------- */
  function initVariants() {
    var form = qs('form[data-ct-variants-form]');
    if (!form) return;
    var input = qs('[data-ct-variants-input]', form);
    var box = qs('[data-ct-variants]', form);
    var empty = qs('[data-ct-variants-empty]', form);
    var gTpl = qs('[data-ct-variant-template]', form);
    var vTpl = qs('[data-ct-value-template]', form);
    var data = [];
    try { data = JSON.parse(input.value || '[]'); } catch (e) { data = []; }
    if (!Array.isArray(data)) data = [];

    function syncEmpty() { empty.hidden = box.children.length > 0; }
    function addValue(group, opt, focus) {
      var node = vTpl.content.firstElementChild.cloneNode(true);
      qs('[data-ct-value-name]', node).value = (opt && (opt.name || opt.value)) || '';
      qs('[data-ct-value-price]', node).value = opt && opt.price !== undefined && opt.price !== null ? String(opt.price) : '';
      qs('[data-ct-variant-options]', group).appendChild(node);
      if (focus) qs('[data-ct-value-name]', node).focus();
    }
    function addGroup(v, focus) {
      var g = gTpl.content.firstElementChild.cloneNode(true);
      qs('[data-ct-variant-name]', g).value = (v && v.filterName) || '';
      var opts = (v && Array.isArray(v.options) && v.options.length) ? v.options : [{}];
      opts.forEach(function (o) { addValue(g, o, false); });
      box.appendChild(g);
      if (focus) qs('[data-ct-variant-name]', g).focus();
      syncEmpty();
    }
    data.forEach(function (v) { addGroup(v, false); });
    syncEmpty();

    qs('[data-ct-variant-add]', form).addEventListener('click', function () { addGroup(null, true); form.dispatchEvent(new Event('change', { bubbles: true })); });
    box.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn) return;
      if (btn.hasAttribute('data-ct-variant-remove')) {
        var g = btn.closest('.ct-variant'); g.parentNode.removeChild(g); syncEmpty();
      } else if (btn.hasAttribute('data-ct-option-add-value')) {
        addValue(btn.closest('.ct-variant'), null, true);
      } else if (btn.hasAttribute('data-ct-value-remove')) {
        var row = btn.closest('.ct-value'); row.parentNode.removeChild(row);
      } else { return; }
      form.dispatchEvent(new Event('change', { bubbles: true }));
    });
    // Enter в значении — новое значение того же параметра (форма не отправляется)
    box.addEventListener('keydown', function (e) {
      if (e.key !== 'Enter' || e.isComposing || !e.target.closest) return;
      var group = e.target.closest('.ct-variant');
      if (!group) return;
      e.preventDefault();
      if (e.target.closest('.ct-value') && e.target.value.trim()) addValue(group, null, true);
    });
    function serialize() {
      var out = [];
      qsa('.ct-variant', box).forEach(function (g) {
        var name = qs('[data-ct-variant-name]', g).value.trim();
        if (!name) return;
        var options = [];
        qsa('.ct-value', g).forEach(function (r) {
          var on = qs('[data-ct-value-name]', r).value.trim();
          if (!on) return;
          var raw = qs('[data-ct-value-price]', r).value.replace(/[\s ]/g, '').replace(',', '.');
          var price = parseFloat(raw);
          options.push({ name: on, price: isNaN(price) || price < 0 ? 0 : price });
        });
        if (options.length) out.push({ filterName: name, options: options });
      });
      input.value = JSON.stringify(out);
    }
    form.addEventListener('submit', serialize, true);
  }

  /* ----------------------------- Фото из буфера обмена (Ctrl+V) ----------------------------- */
  // Скриншот или скопированная картинка добавляется в загрузчик формы (ui.image_uploader). Если в поле
  // вставляют текст (в буфере есть и текст), ничего не перехватываем.
  doc.addEventListener('paste', function (e) {
    var cd = e.clipboardData;
    if (!cd || typeof window.DataTransfer !== 'function') return;
    var files = Array.prototype.filter.call(cd.files || [], function (f) { return /^image\/(jpeg|png|webp|gif)$/.test(f.type); });
    if (!files.length) return;
    var t = e.target;
    if (t && t.matches && t.matches('input, textarea, [contenteditable]') && (cd.getData('text/plain') || '').trim()) return;
    var form = t && t.closest ? t.closest('form') : null;
    var root = (form && qs('[data-cc-uploader]', form)) || qs('form [data-cc-uploader]');
    var input = root && qs('[data-cc-uploader-input]', root);
    if (!input) return;
    e.preventDefault();
    var dt = new DataTransfer();
    files.forEach(function (f, i) {
      var name = f.name && f.name !== 'image.png' ? f.name : 'вставка-' + Date.now() + '-' + i + '.' + (f.type.split('/')[1] || 'png');
      try { dt.items.add(new File([f], name, { type: f.type })); } catch (err) { dt.items.add(f); }
    });
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
    if (CC.toast) CC.toast(files.length > 1 ? 'Фото добавлены из буфера' : 'Фото добавлено из буфера', 'success');
  });

  /* ----------------------------- Обратная связь: шаблоны и черновики ответа ----------------------------- */
  function initFeedback() {
    var forms = qsa('form[data-ct-reply]');
    if (!forms.length) return;
    forms.forEach(function (form) {
      var ta = qs('textarea', form);
      if (!ta) return;
      var key = 'cc-fb-reply:' + form.getAttribute('data-ct-reply');
      var initial = ta.value;
      var saved = CC.session ? CC.session.get(key) : null;
      var box = form.closest('.collapse');
      if (saved && saved !== initial) {
        ta.value = saved;
        if (box && window.bootstrap) window.bootstrap.Collapse.getOrCreateInstance(box, { toggle: false }).show();
        var note = doc.createElement('div');
        note.className = 'form-text mt-1';
        note.textContent = 'Восстановлен несохранённый черновик ответа.';
        ta.parentNode.insertBefore(note, ta.nextSibling);
      }
      ta.addEventListener('input', debounce(function () {
        if (!CC.session) return;
        if (ta.value.trim() && ta.value !== initial) CC.session.set(key, ta.value); else CC.session.del(key);
      }, 400));
      form.addEventListener('submit', function (e) { if (!e.defaultPrevented && CC.session) CC.session.del(key); });
      if (box) box.addEventListener('shown.bs.collapse', function () { ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length); });
    });
    doc.addEventListener('click', function (e) {
      var b = e.target.closest && e.target.closest('[data-ct-template]');
      if (!b) return;
      var form = b.closest('form'), ta = form && qs('textarea', form);
      if (!ta) return;
      var t = b.getAttribute('data-ct-template');
      ta.value = ta.value.trim() ? ta.value.replace(/\s+$/, '') + '\n\n' + t : t;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      ta.focus(); ta.setSelectionRange(ta.value.length, ta.value.length);
    });
  }

  /* ----------------------------- Битые обложки ----------------------------- */
  // Картинка из старой записи могла пропасть из Storage — показываем значок вместо «битого» img.
  doc.addEventListener('error', function (e) {
    var img = e.target;
    if (img && img.tagName === 'IMG' && img.parentNode && img.parentNode.classList && img.parentNode.classList.contains('ct-cover')) {
      var box = img.parentNode;
      if (box.classList.contains('is-broken')) return;
      box.classList.add('is-broken');
      var i = doc.createElement('i'); i.className = 'bi bi-image'; i.setAttribute('aria-hidden', 'true');
      box.insertBefore(i, img);
    }
  }, true);

  ready(function () {
    qsa('form[data-ct-publish-confirm]').forEach(initPublishConfirm);
    initPollOptions();
    initEndTime();
    initVariants();
    qsa('form[data-ct-preview-form]').forEach(initPreview);
    initFeedback();
  });
})();
