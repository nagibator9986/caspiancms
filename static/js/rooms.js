/* ==========================================================================
   Аудиторный фонд · Накладки · Свободные аудитории (владелец — SCHEDULE+ROOMS).
   Без зависимостей; общие функции — window.CC (static/js/app.js): выбор строк и панель
   массовых действий (форма cfBulk), быстрый просмотр, тосты, подтверждения.
   Здесь — только своё: «выбрать все накладки» у сущности, печать, прокрутка недель и
   шахматок к «сегодня/сейчас».
   ========================================================================== */
(function () {
  'use strict';

  var doc = document;
  var CC = window.CC || {};
  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function ready(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }

  /* ----------------------------- Печать ----------------------------- */
  doc.addEventListener('click', function (e) {
    var btn = e.target.closest ? e.target.closest('[data-print]') : null;
    if (!btn) return;
    e.preventDefault();
    // занятия накладок на бумаге — раскрытыми
    qsa('details.cf-cluster__body').forEach(function (d) { d.setAttribute('open', ''); });
    window.print();
  });

  /* ----------------------------- «Выбрать все накладки» сущности ----------------------------- */
  function syncGroups() {
    qsa('.cf-entity').forEach(function (sec) {
      var toggle = qs('[data-cf-select-group]', sec);
      if (!toggle) return;
      var own = qsa('input[data-cc-select="cfBulk"]', sec);
      var k = own.filter(function (b) { return b.checked; }).length;
      toggle.checked = k > 0 && k === own.length;
      toggle.indeterminate = k > 0 && k < own.length;
    });
  }
  doc.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.matches || !el.matches('[data-cf-select-group]')) return;
    var sec = el.closest('.cf-entity');
    var own = qsa('input[data-cc-select="cfBulk"]', sec);
    own.forEach(function (b) { b.checked = el.checked; });
    // app.js пересчитает панель выбора по событию change у чекбокса строки
    if (own.length) own[own.length - 1].dispatchEvent(new Event('change', { bubbles: true }));
  });
  doc.addEventListener('cc:selection', function (e) { if (e.detail && e.detail.form === 'cfBulk') syncGroups(); });

  /* ----------------------------- Прокрутка к «сегодня» / «сейчас» ----------------------------- */
  ready(function () {
    // неделя аудитории на узком экране: показать «сегодня»
    qsa('[data-rm-week]').forEach(function (week) {
      if (week.scrollWidth <= week.clientWidth) return;
      var today = week.querySelector('[data-rm-today]');
      if (today) week.scrollLeft = Math.max(0, today.offsetLeft - week.offsetLeft - 4);
    });
    // шахматки: к выбранной паре или к идущей сейчас
    qsa('.fr-grid-wrap, .rm-matrix-wrap').forEach(function (wrap) {
      var sel = wrap.querySelector('thead th.is-selected') || wrap.querySelector('thead th.is-now');
      if (sel && wrap.scrollWidth > wrap.clientWidth) {
        wrap.scrollLeft = Math.max(0, sel.offsetLeft - wrap.clientWidth / 2 + sel.offsetWidth / 2);
      }
    });
    // накладка из ссылки #c-<hash> — раскрыть
    if (location.hash && /^#c-/.test(location.hash)) {
      var target = doc.getElementById(location.hash.slice(1));
      var det = target && qs('details', target);
      if (det) det.setAttribute('open', '');
    }
    syncGroups();
    if (CC.hotkey) {
      // На «Накладках»: x — раскрыть/свернуть все занятия
      var all = qsa('details.cf-cluster__body');
      if (all.length) {
        CC.hotkey('KeyX', function () {
          var open = all.some(function (d) { return !d.open; });
          all.forEach(function (d) { if (open) d.setAttribute('open', ''); else d.removeAttribute('open'); });
        }, 'Раскрыть или свернуть все накладки', { group: 'Накладки' });
      }
    }
  });
})();
