/* ==========================================================================
   Caspian Coin и приёмная (CRM): витрина (drag & drop), склад и заметки (модалки), форма товара
   (предпросмотр, варианты), кампании (код, QR, полный экран), журнал (сводка ручной операции), настройки
   приёмной (повторяемые строки), новости абитуриентов (предпросмотр). Зависит от static/js/app.js (window.CC).
   Пользовательские данные вставляются только через textContent/value/атрибуты.
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC || {};
  var doc = document;
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onReady(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function toast(text, cat) { if (CC.toast) CC.toast(text, cat); }
  function plural(n, one, few, many) {
    n = Math.abs(n) % 100; var n1 = n % 10;
    if (n > 10 && n < 20) return many;
    if (n1 > 1 && n1 < 5) return few;
    if (n1 === 1) return one;
    return many;
  }
  function fmt(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ' '); }
  function modal(el) { return window.bootstrap ? window.bootstrap.Modal.getOrCreateInstance(el) : null; }

  /* Выбор строк/карточек и панель массовых действий — общие (app.js: ui.select_row + ui.bulkbar);
     мгновенный поиск по витрине — data-cc-filter (app.js). Своих реализаций здесь больше нет. */

  /* ----------------------------- Заказы: фокус в поиске на вкладке «Ждут выдачи» ----------------------------- */
  onReady(function () {
    if (!qs('[data-cn-focus-search]')) return;
    // только с мышью: на телефоне фокус открыл бы клавиатуру поверх очереди
    if (!window.matchMedia || !window.matchMedia('(hover: hover) and (pointer: fine)').matches) return;
    var input = qs('.cc-list [data-cc-search]');
    if (input && !input.value && doc.activeElement === doc.body) input.focus({ preventScroll: true });
  });

  /* ----------------------------- Витрина: порядок перетаскиванием ----------------------------- */
  onReady(function () {
    var box = qs('[data-cn-sortable]');
    if (!box || !window.Sortable) return;
    var url = box.getAttribute('data-cn-sortable');
    var before = null;
    function ids() { return qsa('[data-id]', box).map(function (el) { return el.getAttribute('data-id'); }); }
    window.Sortable.create(box, {
      handle: '[data-cn-drag]', animation: 160, ghostClass: 'cn-sort-ghost', chosenClass: 'cn-sort-chosen',
      onStart: function () { before = ids(); },
      onEnd: function () {
        var now = ids();
        if (before && before.join('|') === now.join('|')) return;
        box.classList.add('is-saving');
        CC.fetchJSON(url, { method: 'POST', body: { ids: now } }).then(function (data) {
          toast((data && data.message) || 'Порядок сохранён', 'success');
        }).catch(function (err) {
          toast((err && err.message) || 'Не удалось сохранить порядок', 'error');
        }).then(function () { box.classList.remove('is-saving'); });
      }
    });
    // Клавиатура: стрелки на «ручке» двигают карточку (доступность без мыши).
    box.addEventListener('keydown', function (e) {
      var handle = e.target.closest ? e.target.closest('[data-cn-drag]') : null;
      if (!handle || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight' && e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
      e.preventDefault();
      var card = handle.closest('[data-id]');
      var back = e.key === 'ArrowLeft' || e.key === 'ArrowUp';
      var sib = back ? card.previousElementSibling : card.nextElementSibling;
      if (!sib) return;
      before = ids();
      if (back) box.insertBefore(card, sib); else box.insertBefore(sib, card);
      handle.focus();
      var now = ids();
      CC.fetchJSON(url, { method: 'POST', body: { ids: now } }).then(function () {
        toast('Порядок сохранён', 'success');
      }).catch(function (err) { toast((err && err.message) || 'Не удалось сохранить порядок', 'error'); });
    });
  });

  /* ----------------------------- Модалка «Склад» ----------------------------- */
  onReady(function () {
    var el = qs('#cnStockModal');
    if (!el) return;
    var form = qs('[data-cn-stock-form]', el);
    var input = qs('[name="delta"]', el);
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-cn-stock]') : null;
      if (!btn) return;
      e.preventDefault();   // пункт меню «Склад…» — ссылка-заглушка
      form.setAttribute('action', btn.getAttribute('data-cn-stock'));
      qs('[data-cn-stock-name]', el).textContent = btn.getAttribute('data-title') || '';
      var st = btn.getAttribute('data-stock');
      qs('[data-cn-stock-now]', el).textContent = st === '' || st === null ? 'без лимита' : (fmt(st) + ' шт.');
      input.value = '10';
      var m = modal(el); if (m) m.show();
      setTimeout(function () { input.focus(); input.select(); }, 250);
    });
    qsa('[data-cn-stock-step]', el).forEach(function (b) {
      b.addEventListener('click', function () {
        var v = parseInt(input.value || '0', 10) || 0;
        v += parseInt(b.getAttribute('data-cn-stock-step'), 10);
        input.value = String(v === 0 ? (parseInt(b.getAttribute('data-cn-stock-step'), 10)) : v);
      });
    });
    qsa('[data-cn-stock-set]', el).forEach(function (b) {
      b.addEventListener('click', function () { input.value = b.getAttribute('data-cn-stock-set'); input.focus(); });
    });
  });

  /* ----------------------------- Модалка «Заметка к заказу» ----------------------------- */
  onReady(function () {
    var el = qs('#cnNoteModal');
    if (!el) return;
    var form = qs('[data-cn-note-form]', el);
    var text = qs('textarea[name="note"]', el);
    doc.addEventListener('click', function (e) {
      var btn = e.target.closest ? e.target.closest('[data-cn-note]') : null;
      if (!btn) return;
      e.preventDefault();   // пункт меню «Заметка…» — ссылка-заглушка
      form.setAttribute('action', btn.getAttribute('data-cn-note'));
      qs('[data-cn-note-name]', el).textContent = btn.getAttribute('data-title') || '';
      text.value = btn.getAttribute('data-note') || '';
      var m = modal(el); if (m) m.show();
      setTimeout(function () { text.focus(); }, 250);
    });
  });

  /* ----------------------------- Форма товара: предпросмотр и варианты ----------------------------- */
  onReady(function () {
    var form = qs('#cnProductForm');
    if (!form) return;
    var title = qs('[data-cn-preview="title"]', form);
    var price = qs('[data-cn-preview="price"]', form);
    var stock = qs('[data-cn-preview="stock"]', form);
    var unlimited = qs('[data-cn-unlimited]', form);
    var active = qs('[data-cn-preview="active"]', form);
    var card = qs('[data-cn-preview-card]');
    var img = qs('[data-cn-preview-img]');
    var ph = qs('[data-cn-preview-ph]');
    var badge = qs('[data-cn-preview-badge]');
    var stockLabel = qs('[data-cn-preview-stock]');
    function update() {
      if (!card) return;
      qs('[data-cn-preview-title]').textContent = (title && title.value.trim()) || 'Название товара';
      qs('[data-cn-preview-price]').textContent = fmt(parseInt(price && price.value, 10) || 0);
      var unl = unlimited && unlimited.checked;
      var n = parseInt(stock && stock.value, 10);
      var label = '', badgeText = '';
      if (!unl && !isNaN(n)) {
        if (n <= 0) badgeText = 'Нет в наличии';
        else if (n <= 3) label = 'Осталось ' + n;
        else label = n + ' шт.';
      }
      if (active && !active.checked) badgeText = 'Скрыт';
      stockLabel.textContent = label;
      badge.textContent = badgeText;
      badge.hidden = !badgeText;
      card.classList.toggle('is-off', !!badgeText);
    }
    function updateCover() {
      if (!img) return;
      var first = qs('.cc-uploader__item img', form);
      if (first && first.src) { img.src = first.src; img.hidden = false; if (ph) ph.hidden = true; }
      else { img.removeAttribute('src'); img.hidden = true; if (ph) ph.hidden = false; }
    }
    if (unlimited && stock) {
      unlimited.addEventListener('change', function () {
        stock.disabled = unlimited.checked;
        if (!unlimited.checked && stock.value === '') stock.value = '10';
        update();
      });
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    qsa('[data-cc-uploader]', form).forEach(function (u) { u.addEventListener('cc:uploader-change', updateCover); });
    setTimeout(updateCover, 0);
    update();

    // Варианты: шаблоны и чипы-предпросмотр
    var area = qs('[data-cn-variants]', form);
    var preview = qs('[data-cn-variants-preview]', form);
    function renderVariants() {
      if (!area || !preview) return;
      preview.textContent = '';
      var seen = {};
      area.value.split(/[\n,;]+/).map(function (s) { return s.trim(); }).filter(Boolean).forEach(function (v) {
        if (seen[v.toLowerCase()]) return; seen[v.toLowerCase()] = true;
        var chip = doc.createElement('span'); chip.className = 'cn-variant' + (v.length > 40 ? ' is-bad' : ''); chip.textContent = v;
        preview.appendChild(chip);
      });
    }
    qsa('[data-cn-variant-preset]', form).forEach(function (b) {
      b.addEventListener('click', function () {
        area.value = b.getAttribute('data-cn-variant-preset');
        area.dispatchEvent(new Event('input', { bubbles: true }));
        renderVariants();
      });
    });
    if (area) area.addEventListener('input', renderVariants);
    renderVariants();
  });

  /* ----------------------------- Кампания: код, предпросмотр ----------------------------- */
  var LOOKALIKES = { 'А': 'A', 'В': 'B', 'Е': 'E', 'К': 'K', 'М': 'M', 'Н': 'H', 'О': 'O', 'Р': 'P', 'С': 'C', 'Т': 'T', 'Х': 'X', 'У': 'Y' };
  function normalizeCode(raw) {
    return String(raw || '').toUpperCase().split('').filter(function (ch) { return !/[\s\-_.]/.test(ch); })
      .map(function (ch) { return LOOKALIKES[ch] || ch; }).join('');
  }
  onReady(function () {
    var form = qs('[data-cn-campaign-form]');
    if (!form) return;
    var code = qs('[data-cn-code]', form);
    var codeOut = qs('[data-cn-preview-code]');
    var title = qs('[data-cn-preview="title"]', form);
    var amount = qs('[data-cn-preview="amount"]', form);
    function update() {
      if (code && codeOut) codeOut.textContent = normalizeCode(code.value) || code.getAttribute('placeholder') || '';
      var t = qs('[data-cn-preview-title]'); if (t && title) t.textContent = title.value.trim() || 'Название кампании';
      var a = qs('[data-cn-preview-amount]'); if (a && amount) a.textContent = fmt(parseInt(amount.value, 10) || 0);
    }
    if (code) {
      code.addEventListener('blur', function () { code.value = normalizeCode(code.value); update(); });
      var gen = qs('[data-cn-code-generate]', form);
      if (gen) {
        gen.addEventListener('click', function () {
          var abc = code.getAttribute('data-alphabet') || 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';
          var out = '', rnd = new Uint32Array(6);
          (window.crypto || window.msCrypto).getRandomValues(rnd);
          for (var i = 0; i < 6; i++) out += abc[rnd[i] % abc.length];
          code.value = out; code.dispatchEvent(new Event('input', { bubbles: true })); update(); code.focus();
        });
      }
    }
    form.addEventListener('input', update);
    update();
  });

  /* ----------------------------- «Показать на экране»: QR и полный экран ----------------------------- */
  onReady(function () {
    qsa('[data-cn-qr]').forEach(function (box) {
      try {
        box.innerHTML = CC.qrSvg(box.getAttribute('data-cn-qr'), { cellSize: 10, margin: 1, ecc: 'M' });
      } catch (e) {
        box.textContent = 'QR не загрузился — проверьте интернет';
      }
    });
    var btn = qs('[data-cn-fullscreen]');
    var root = qs('[data-cn-present]');
    if (btn && root) {
      if (!doc.fullscreenEnabled) { btn.hidden = true; return; }
      btn.addEventListener('click', function () {
        if (doc.fullscreenElement) doc.exitFullscreen(); else doc.documentElement.requestFullscreen().catch(function () {});
      });
      doc.addEventListener('fullscreenchange', function () {
        root.classList.toggle('is-fullscreen', !!doc.fullscreenElement);
        var s = btn.querySelector('span'); if (s) s.textContent = doc.fullscreenElement ? 'Свернуть' : 'Во весь экран';
      });
    }
  });

  /* ----------------------------- Журнал: ручная операция ----------------------------- */
  onReady(function () {
    var box = qs('[data-cn-award]');
    if (box && !box.hasAttribute('data-cn-keep-open') && window.matchMedia && window.matchMedia('(max-width: 1199.98px)').matches) {
      box.open = false;
    }
    var form = qs('[data-cn-award-form]');
    if (!form) return;
    var out = qs('[data-cn-award-summary]', form);
    function update() {
      var minus = qs('[name="direction"]:checked', form);
      minus = minus && minus.value === 'minus';
      var n = parseInt(qs('[name="amount"]', form).value, 10) || 0;
      var people = qs('[name="recipients"]', form).value.split(/[\n,;]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      var uniq = people.filter(function (p, i) { return people.indexOf(p) === i; }).length;
      var text = '';
      if (n > 0 && uniq > 0) {
        text = (minus ? 'Списать ' : 'Начислить ') + fmt(n) + ' ' + plural(n, 'коин', 'коина', 'коинов') + ' — ' +
          uniq + ' ' + plural(uniq, 'получателю', 'получателям', 'получателям') +
          (uniq > 1 ? ' (всего ' + fmt(n * uniq) + ')' : '');
      }
      out.textContent = text;
      form.setAttribute('data-confirm', text ? text + '?' : 'Провести операцию?');
      form.classList.toggle('is-minus', !!minus);
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    update();
  });

  /* ----------------------------- Приёмная: повторяемые строки ----------------------------- */
  function renumber(list) {
    qsa(':scope > [data-cn-row]', list).forEach(function (row, i) {
      var num = qs('[data-cn-row-num]', row); if (num) num.textContent = String(i + 1);
    });
  }
  function rowKey() {
    var abc = 'abcdefghijkmnpqrstuvwxyz23456789', out = 'n', rnd = new Uint32Array(8);
    (window.crypto || window.msCrypto).getRandomValues(rnd);
    for (var i = 0; i < 8; i++) out += abc[rnd[i] % abc.length];
    return out;
  }
  onReady(function () {
    var form = qs('[data-cn-adm-form]');
    if (!form) return;
    function markDirty() { form.dispatchEvent(new Event('change', { bubbles: true })); }
    qsa('[data-cn-repeater]', form).forEach(function (list) {
      renumber(list);
      if (window.Sortable) {
        window.Sortable.create(list, { handle: '[data-cn-row-drag]', animation: 160, ghostClass: 'cn-sort-ghost',
          onEnd: function () { renumber(list); markDirty(); } });
      }
    });
    form.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target : null;
      if (!t) return;
      var add = t.closest('[data-cn-row-add]');
      if (add) {
        var kind = add.getAttribute('data-cn-row-add');
        var list = qs('[data-cn-repeater="' + kind + '"]', form);
        var tpl = qs('template[data-cn-row-template="' + kind + '"]', form);
        if (!list || !tpl) return;
        var max = parseInt(list.getAttribute('data-cn-repeater-max') || '50', 10);
        if (qsa(':scope > [data-cn-row]', list).length >= max) { toast('Не больше ' + max + ' строк', 'warning'); return; }
        var frag = tpl.content.cloneNode(true);
        var key = rowKey();
        qsa('[id], [for], [name], [value]', frag).forEach(function (el) {
          ['id', 'for', 'name', 'value'].forEach(function (a) {
            var v = el.getAttribute(a);
            if (v && v.indexOf('__KEY__') !== -1) el.setAttribute(a, v.split('__KEY__').join(key));
          });
        });
        var row = frag.firstElementChild;
        list.appendChild(frag);
        if (row) {
          row.classList.add('is-new');
          var first = qs('input:not([type=hidden]), textarea', row);
          if (first) first.focus();
        }
        renumber(list); markDirty();
        return;
      }
      var row2 = t.closest('[data-cn-row]');
      if (!row2) return;
      var list2 = row2.parentNode;
      if (t.closest('[data-cn-row-remove]')) {
        var filled = qsa('input:not([type=hidden]), textarea', row2).some(function (i) { return i.value.trim(); });
        var go = function () { row2.parentNode.removeChild(row2); renumber(list2); markDirty(); };
        if (filled && CC.confirm) CC.confirm('Удалить строку? Изменение сохранится после «Сохранить».', { okText: 'Удалить' }).then(function (ok) { if (ok) go(); });
        else go();
      } else if (t.closest('[data-cn-row-up]')) {
        if (row2.previousElementSibling) { list2.insertBefore(row2, row2.previousElementSibling); renumber(list2); markDirty(); }
      } else if (t.closest('[data-cn-row-down]')) {
        if (row2.nextElementSibling) { list2.insertBefore(row2.nextElementSibling, row2); renumber(list2); markDirty(); }
      }
    });

    // Дедлайн: «осталось N дней»
    var dl = qs('[data-cn-deadline]', form);
    var dlOut = qs('[data-cn-deadline-preview]', form);
    if (dl && dlOut) {
      dl.addEventListener('change', function () {
        if (!dl.value) { dlOut.textContent = 'Карточки дедлайна не будет'; return; }
        var d = new Date(dl.value.slice(0, 10) + 'T00:00:00');
        var today = new Date(); today.setHours(0, 0, 0, 0);
        var days = Math.round((d - today) / 86400000);
        dlOut.textContent = days >= 0 ? 'Осталось ' + days + ' ' + plural(days, 'день', 'дня', 'дней')
          : 'Дата в прошлом — карточка покажет «приём завершён»';
      });
    }
  });

  /* ----------------------------- Новость абитуриентов: предпросмотр ----------------------------- */
  onReady(function () {
    var media = qs('[data-cn-preview-media]');
    var form = media ? media.closest('form') : null;
    if (!form) return;
    var map = { title: 'Заголовок новости', description: '' };
    function update() {
      Object.keys(map).forEach(function (k) {
        var input = qs('[data-cn-preview="' + k + '"]', form);
        var out = qs('[data-cn-preview-' + k + ']');
        if (input && out) out.textContent = input.value.trim() || map[k];
      });
      var cat = qs('[data-cn-preview="category"]', form);
      var catOut = qs('[data-cn-preview-category]');
      if (cat && catOut) {
        catOut.textContent = cat.options[cat.selectedIndex] ? cat.options[cat.selectedIndex].text : '';
        catOut.className = 'cn-cat cn-cat--' + String(cat.value).replace(/[^a-z]/g, '');
      }
    }
    function updateCover() {
      var img = qs('[data-cn-preview-img]');
      var first = qs('.cc-uploader__item img', form);
      if (first && first.src) { img.src = first.src; media.hidden = false; } else { media.hidden = true; img.removeAttribute('src'); }
    }
    form.addEventListener('input', update);
    form.addEventListener('change', update);
    qsa('[data-cc-uploader]', form).forEach(function (u) { u.addEventListener('cc:uploader-change', updateCover); });
    setTimeout(updateCover, 0);
    update();
  });
})();
