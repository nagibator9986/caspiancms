/* ==========================================================================
   Аудиторный фонд · Накладки · Свободные аудитории (владелец — CONFLICTS).
   Без зависимостей; общие функции — window.CC (static/js/app.js).
   ========================================================================== */
(function () {
  'use strict';

  var doc = document;
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function plural(n, one, few, many) {
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return one;
    if (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20)) return few;
    return many;
  }

  /* ----------------------------- Печать ----------------------------- */
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-print]') : null;
    if (!btn) return;
    e.preventDefault();
    // Формы «Принять» на бумаге не нужны — свернуть
    qsa('details.cf-ack').forEach(function (d) { d.removeAttribute('open'); });
    window.print();
  });

  /* ----------------------------- Массовый выбор накладок ----------------------------- */
  ready(function () {
    var bulk = doc.querySelector('[data-cf-bulk]');
    if (!bulk) return;
    var countEl = bulk.querySelector('[data-cf-count]');
    var buttons = qsa('[data-cf-bulk-btn]', bulk);
    var idle = countEl ? countEl.textContent : '';

    function boxes() { return qsa('input[data-cf-select]'); }

    function update() {
      var all = boxes();
      var n = all.filter(function (b) { return b.checked; }).length;
      bulk.classList.toggle('has-selection', n > 0);
      buttons.forEach(function (b) { b.disabled = n === 0; });
      if (countEl) {
        countEl.textContent = n ? 'Выбрано: ' + n + ' ' + plural(n, 'пункт', 'пункта', 'пунктов') : idle;
      }
      qsa('[data-cf-select-all]').forEach(function (a) {
        a.checked = n > 0 && n === all.length;
        a.indeterminate = n > 0 && n < all.length;
      });
      qsa('[data-cf-group]').forEach(function (g) {
        var own = qsa('input[data-cf-select]', g);
        var k = own.filter(function (b) { return b.checked; }).length;
        var toggle = g.querySelector('[data-cf-select-group]');
        if (toggle) { toggle.checked = k > 0 && k === own.length; toggle.indeterminate = k > 0 && k < own.length; }
      });
    }

    doc.addEventListener('change', function (e) {
      var el = e.target;
      if (!el || !el.matches) return;
      if (el.matches('[data-cf-select-all]')) {
        boxes().forEach(function (b) { b.checked = el.checked; });
      } else if (el.matches('[data-cf-select-group]')) {
        var group = el.closest('[data-cf-group]');
        qsa('input[data-cf-select]', group).forEach(function (b) { b.checked = el.checked; });
      } else if (!el.matches('[data-cf-select]')) {
        return;
      }
      update();
    });

    bulk.addEventListener('submit', function (e) {
      var n = boxes().filter(function (b) { return b.checked; }).length;
      if (!n) {
        e.preventDefault();
        if (window.CC && CC.toast) CC.toast('Сначала отметьте накладки', 'warning');
      }
    });

    update();
  });

  /* ----------------------------- «Принять»: фокус в поле комментария ----------------------------- */
  doc.addEventListener('toggle', function (e) {
    var d = e.target;
    if (!d || !d.classList || !d.classList.contains('cf-ack') || !d.open) return;
    var input = d.querySelector('input[name="note"]');
    if (input) setTimeout(function () { input.focus(); }, 30);
  }, true);

  /* ----------------------------- Вкладки на узком экране: активная — в поле зрения ----------------------------- */
  ready(function () {
    qsa('.cc-tabs').forEach(function (tabs) {
      var active = tabs.querySelector('.cc-tab.active');
      if (!active || tabs.scrollWidth <= tabs.clientWidth) return;
      var left = active.offsetLeft - tabs.offsetLeft;
      if (left + active.offsetWidth > tabs.clientWidth) tabs.scrollLeft = left - 16;
    });
  });

  /* ----------------------------- Неделя аудитории на узком экране: показать «сегодня» ----------------------------- */
  ready(function () {
    qsa('[data-rm-week]').forEach(function (week) {
      if (week.scrollWidth <= week.clientWidth) return;
      var today = week.querySelector('[data-rm-today]');
      if (today) week.scrollLeft = Math.max(0, today.offsetLeft - week.offsetLeft - 4);
    });
  });

  /* ----------------------------- Шахматка: прокрутка к выбранной паре ----------------------------- */
  ready(function () {
    var wrap = doc.querySelector('.fr-grid-wrap');
    var sel = wrap && wrap.querySelector('thead th.is-selected');
    if (wrap && sel && wrap.scrollWidth > wrap.clientWidth) {
      wrap.scrollLeft = Math.max(0, sel.offsetLeft - wrap.clientWidth / 2 + sel.offsetWidth / 2);
    }
  });
})();
