/* Раздел «Контент» (CONTENT): массовый выбор, превью новости, варианты опроса, срок опроса,
   конструктор вариантов товара. Без innerHTML с пользовательскими данными — только DOM/textContent. */
(function () {
  'use strict';
  var doc = document;
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState === 'loading') doc.addEventListener('DOMContentLoaded', fn); else fn(); }

  /* ----------------------------- Массовый выбор (bulk) ----------------------------- */
  // <input type=checkbox data-ct-check-all="formId">, строки — input[name=ids][form=formId],
  // панель действий — [data-ct-bulk="formId"] (показывается, когда что-то выбрано).
  function initBulk() {
    var forms = {};
    qsa('[data-ct-bulk]').forEach(function (bar) { forms[bar.getAttribute('data-ct-bulk')] = bar; });
    Object.keys(forms).forEach(function (formId) {
      var bar = forms[formId];
      var boxes = qsa('input[name="ids"][form="' + formId + '"]');
      var all = qsa('[data-ct-check-all="' + formId + '"]');
      var counter = qs('[data-ct-bulk-count]', bar);
      function sync() {
        var n = boxes.filter(function (b) { return b.checked; }).length;
        bar.hidden = n === 0;
        if (counter) counter.textContent = n;
        all.forEach(function (a) { a.checked = n > 0 && n === boxes.length; a.indeterminate = n > 0 && n < boxes.length; });
      }
      boxes.forEach(function (b) { b.addEventListener('change', sync); });
      all.forEach(function (a) {
        a.addEventListener('change', function () { boxes.forEach(function (b) { b.checked = a.checked; }); sync(); });
      });
      sync();
    });
  }

  /* ----------------------------- Превью новости ----------------------------- */
  function initNewsPreview() {
    var form = qs('form[data-ct-preview-form]');
    var card = qs('[data-ct-preview-card]');
    if (!form || !card) return;
    var labels = {};
    try { labels = JSON.parse((qs('#ctPreviewLabels') || {}).textContent || '{}'); } catch (e) { labels = {}; }
    var field = function (k) { return qs('[data-ct-preview="' + k + '"]', form); };
    var img = qs('[data-ct-preview-img]', card);
    var empty = qs('[data-ct-preview-empty]', card);
    var media = qs('[data-ct-preview-media]', card);
    var dots = qs('[data-ct-preview-dots]', card);
    var uploader = qs('[data-cc-uploader]', form);

    img.addEventListener('load', function () {
      // рамка как в приложении: по первому фото, но не уже 4:5 и не шире 1.91:1
      var w = img.naturalWidth, h = img.naturalHeight;
      var r = (w > 0 && h > 0) ? Math.min(1.91, Math.max(0.8, w / h)) : 16 / 9;
      media.style.aspectRatio = String(r);
    });

    function text(k) { var el = field(k); return el ? el.value.trim() : ''; }
    function update() {
      qs('[data-ct-preview-title]', card).textContent = text('title') || 'Заголовок новости';
      var desc = text('description') || text('content');
      qs('[data-ct-preview-text]', card).textContent = desc ? desc.slice(0, 220) : 'Краткое описание появится здесь.';
      qs('[data-ct-preview-chip]', card).textContent = (labels.category || {})[text('category')] || text('category') || 'Без категории';
      var aud = text('audience');
      qs('[data-ct-preview-aud]', card).textContent = aud && aud !== 'all' ? 'Только: ' + ((labels.audience || {})[aud] || aud).toLowerCase() : '';
      var thumbs = uploader ? qsa('.cc-uploader__item img', uploader) : [];
      if (thumbs.length) {
        if (img.getAttribute('src') !== thumbs[0].src) img.src = thumbs[0].src;
        img.hidden = false; empty.hidden = true;
      } else {
        img.hidden = true; img.removeAttribute('src'); empty.hidden = false; media.style.aspectRatio = '';
      }
      while (dots.firstChild) dots.removeChild(dots.firstChild);
      if (thumbs.length > 1) {
        thumbs.forEach(function (_, i) { var s = doc.createElement('span'); if (i === 0) s.className = 'is-active'; dots.appendChild(s); });
      }
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    if (uploader) uploader.addEventListener('cc:uploader-change', function () { setTimeout(update, 0); });
    update();
  }

  /* ----------------------------- Варианты ответа опроса ----------------------------- */
  function initPollOptions() {
    var box = qs('[data-ct-options]');
    if (!box) return;
    var max = parseInt(box.getAttribute('data-max') || '20', 10);
    var tpl = qs('[data-ct-option-template]');
    var addBtn = qs('[data-ct-option-add]');
    function liveCount() { return qsa('.ct-option:not(.is-deleted)', box).length; }
    function syncAdd() { if (addBtn) addBtn.disabled = liveCount() >= max; }

    box.addEventListener('click', function (e) {
      var btn = e.target.closest('button');
      if (!btn || !box.contains(btn)) return;
      var row = btn.closest('.ct-option');
      if (btn.hasAttribute('data-ct-option-remove')) {
        if (row.getAttribute('data-ct-option') === 'new') {
          row.parentNode.removeChild(row);
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
        syncAdd();
      } else if (btn.hasAttribute('data-ct-option-move')) {
        var dir = parseInt(btn.getAttribute('data-ct-option-move'), 10);
        var sib = dir < 0 ? row.previousElementSibling : row.nextElementSibling;
        if (!sib || sib.getAttribute('data-ct-option') !== 'existing') return;
        if (dir < 0) box.insertBefore(row, sib); else box.insertBefore(sib, row);
        btn.focus();
        row.closest('form').dispatchEvent(new Event('change', { bubbles: true }));
      }
    });
    if (addBtn && tpl) {
      addBtn.addEventListener('click', function () {
        if (liveCount() >= max) return;
        var node = tpl.content.firstElementChild.cloneNode(true);
        box.appendChild(node);
        var input = qs('input', node); if (input) input.focus();
        syncAdd();
      });
    }
    syncAdd();
  }

  /* ----------------------------- Срок опроса: быстрые кнопки ----------------------------- */
  function pad(n) { return (n < 10 ? '0' : '') + n; }
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
        var ms = Math.ceil((Date.now() + days * 24 * hour) / hour) * hour;  // до ближайшего целого часа
        input.value = almatyInput(ms);
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
      qs('[data-ct-value-name]', node).value = (opt && opt.name) || '';
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

    qs('[data-ct-variant-add]', form).addEventListener('click', function () { addGroup(null, true); });
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
    function serialize() {
      var out = [];
      qsa('.ct-variant', box).forEach(function (g) {
        var name = qs('[data-ct-variant-name]', g).value.trim();
        if (!name) return;
        var options = [];
        qsa('.ct-value', g).forEach(function (r) {
          var on = qs('[data-ct-value-name]', r).value.trim();
          if (!on) return;
          var raw = qs('[data-ct-value-price]', r).value.replace(/\s/g, '').replace(',', '.');
          var price = parseFloat(raw);
          options.push({ name: on, price: isNaN(price) || price < 0 ? 0 : price });
        });
        if (options.length) out.push({ filterName: name, options: options });
      });
      input.value = JSON.stringify(out);
    }
    form.addEventListener('submit', serialize);
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
    initBulk();
    initNewsPreview();
    initPollOptions();
    initEndTime();
    initVariants();
  });
})();
