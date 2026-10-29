/* ==========================================================================
   substitutions.js — раздел «Замены» (владелец — SCHEDULE). Зависит от app.js (window.CC) и Bootstrap (окно выбора).
   • Шаг 1 оформления: быстрые даты «Сегодня / Завтра».
   • Шаг 2 и правка: у каждой пары — кнопка «Подобрать замену» → окно со списком преподавателей: свободные в это время
     (лучшие первыми, с пометками «ведёт дисциплину», «знает группу», «пара рядом»), ниже — занятые (выбрать можно: потоки,
     объединённые группы — получится накладка, о ней предупреждаем). «Один преподаватель на выбранные» показывает только тех,
     кто свободен на ВСЕ отмеченные пары. Решение пары: uid преподавателя | open (решим позже) | cancel (пара снята).
   Данные — <script type="application/json" id="subPickerData">: {teachers: {uid: ФИО}, badges: {код: подпись},
   lessons: {ключ пары: {label, known, free: [{u, s, b, l, n}], busy: [{u, n, a}]}}}; текст в DOM — только textContent.
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC || {};
  var doc = document;

  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onInit(fn) { if (CC.onInit) CC.onInit(fn); else if (doc.readyState !== 'loading') fn(doc); else doc.addEventListener('DOMContentLoaded', function () { fn(doc); }); }
  function readJSON(id, fallback) {
    var el = doc.getElementById(id);
    if (!el) return fallback;
    try { var v = JSON.parse(el.textContent || 'null'); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function plural(n, forms) {
    var m10 = n % 10, m100 = n % 100;
    return forms[m10 === 1 && m100 !== 11 ? 0 : (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? 1 : 2)];
  }
  function el(tag, cls, text) {
    var n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined && text !== null) n.textContent = text;
    return n;
  }
  function initials(name) {
    var p = String(name || '?').split(/\s+/);
    return (p[0].charAt(0) + (p[1] ? p[1].charAt(0) : '')).toUpperCase();
  }
  function shortName(name) {
    var p = String(name || '').split(/\s+/).filter(Boolean);
    if (p.length < 2) return p.join(' ');
    return p[0] + ' ' + p.slice(1, 3).map(function (x) { return x.charAt(0).toUpperCase() + '.'; }).join('');
  }

  // ---------- Шаг 1: быстрые даты ----------
  function initStep1(root) {
    qsa('[data-sub-date]', root).forEach(function (btn) {
      if (btn._subBound) return;
      btn._subBound = true;
      btn.addEventListener('click', function () {
        var from = qs('[data-sub-from]'), to = qs('[data-sub-to]');
        if (from) from.value = btn.getAttribute('data-sub-date');
        if (to) to.value = '';
      });
    });
  }

  // ---------- Шаг 2 и правка: выбор преподавателя ----------
  function initForm(form) {
    if (form._subInit) return;
    form._subInit = true;
    var data = readJSON('subPickerData', { teachers: {}, badges: {}, lessons: {} });
    var modalEl = doc.getElementById('subPicker');
    var modal = window.bootstrap && modalEl ? window.bootstrap.Modal.getOrCreateInstance(modalEl) : null;
    var rows = qsa('[data-sub-row]', form);
    var lessonEl = qs('[data-sub-picker-lesson]', modalEl), searchEl = qs('[data-sub-picker-search]', modalEl);
    var freeBox = qs('[data-sub-picker-free]', modalEl), busyBox = qs('[data-sub-picker-busy]', modalEl);
    var freeN = qs('[data-sub-picker-free-n]', modalEl), busyN = qs('[data-sub-picker-busy-n]', modalEl);
    var busyDetails = qs('[data-sub-picker-busy-box]', modalEl);
    var current = [];

    function nameOf(uid) { return data.teachers[uid] || uid; }
    // «Лучший вариант» — только если он строго лучше следующего (при равных баллах выбор за человеком).
    function bestUid(free) { return free.length && free[0].s > 0 && (!free[1] || free[0].s > free[1].s) ? free[0].u : ''; }
    function find(list, uid) { for (var i = 0; i < list.length; i++) if (list[i].u === uid) return list[i]; return null; }
    function badgeText(codes) { return (codes || []).map(function (c) { return data.badges[c] || c; }); }
    function rowOf(key) { return rows.filter(function (r) { return r.getAttribute('data-sub-row') === key; })[0]; }
    function isOn(r) { var c = qs('[data-sub-pick]', r); return !c || c.checked; }

    function render(r) {
      var key = r.getAttribute('data-sub-row'), input = qs('[data-sub-to]', r);
      if (!input) return;
      var v = input.value || 'open', cand = data.lessons[key] || { free: [], busy: [], known: true };
      var label = qs('[data-sub-label]', r), btn = qs('[data-sub-choose]', r), hint = qs('[data-sub-hint]', r);
      btn.classList.remove('is-assigned', 'is-cancelled', 'is-open', 'is-warn');
      hint.className = 'sub-lesson__hint';
      if (v === 'cancel') {
        label.textContent = 'Пара снята';
        btn.classList.add('is-cancelled');
        hint.textContent = 'Пара не состоится — преподавателя не назначаем.';
      } else if (v === 'open') {
        label.textContent = 'Подобрать замену';
        btn.classList.add('is-open');
        if (cand.free.length) {
          hint.textContent = 'Свободны: ' + cand.free.length + (bestUid(cand.free) ? ' · лучший вариант — ' + shortName(nameOf(bestUid(cand.free))) : '');
        } else if (cand.known === false) {
          hint.textContent = 'Время пары не распознано — занятость не проверить.';
        } else {
          hint.textContent = 'Свободных преподавателей нет — выберите занятого или снимите пару.';
          hint.classList.add('is-bad');
        }
      } else {
        label.textContent = nameOf(v);
        btn.classList.add('is-assigned');
        var f = find(cand.free, v), b = find(cand.busy, v);
        if (b) {
          btn.classList.add('is-warn');
          hint.classList.add('is-bad');
          hint.textContent = b.a ? 'Отсутствует в этот день — замена не получится.' : 'Накладка: ' + b.n.replace(/^занят: /, '');
        } else if (f) {
          hint.textContent = badgeText(f.b).concat(f.n ? [f.n] : []).join(' · ');
        } else {
          hint.textContent = '';
        }
      }
      var on = isOn(r);
      r.classList.toggle('is-off', !on);
      btn.disabled = !on;
    }

    // Кандидаты для набора пар: одна пара — как есть; несколько — только свободные на ВСЕ (баллы суммируются).
    function listsFor(keys) {
      if (keys.length === 1) {
        var c = data.lessons[keys[0]] || { free: [], busy: [] };
        return { free: c.free, busy: c.busy };
      }
      var by = {};
      keys.forEach(function (k) {
        var c = data.lessons[k];
        if (!c) return;
        c.free.forEach(function (x) {
          var e = by[x.u] || (by[x.u] = { u: x.u, s: 0, free: 0, busy: 0, b: {}, l: x.l, a: 0, bn: '' });
          e.free++; e.s += x.s;
          x.b.forEach(function (code) { e.b[code] = true; });
        });
        c.busy.forEach(function (x) {
          var e = by[x.u] || (by[x.u] = { u: x.u, s: 0, free: 0, busy: 0, b: {}, l: 0, a: 0, bn: '' });
          e.busy++; e.a = e.a || x.a; e.bn = e.bn || x.n.replace(/^занят: /, '');
        });
      });
      var free = [], busy = [];
      Object.keys(by).forEach(function (u) {
        var e = by[u];
        if (!e.busy && e.free === keys.length) free.push({ u: u, s: e.s, b: Object.keys(e.b), l: e.l, n: 'свободен на все ' + keys.length + ' ' + plural(keys.length, ['пару', 'пары', 'пар']) });
        else busy.push({ u: u, a: e.a, n: e.a ? 'отсутствует в этот день' : 'занят на ' + (e.busy || keys.length - e.free) + ' из ' + keys.length + ': ' + e.bn });
      });
      free.sort(function (a, b) { return b.s - a.s || nameOf(a.u).localeCompare(nameOf(b.u), 'ru'); });
      busy.sort(function (a, b) { return (a.a - b.a) || nameOf(a.u).localeCompare(nameOf(b.u), 'ru'); });
      return { free: free, busy: busy };
    }

    function candidate(x, isBusy, best, selected) {
      var b = el('button', 'sub-cand' + (isBusy ? ' is-busy' : '') + (selected ? ' is-selected' : ''));
      b.type = 'button';
      b.setAttribute('data-uid', x.u);
      b.setAttribute('data-name', nameOf(x.u).toLowerCase());
      b.appendChild(el('span', 'cc-avatar', initials(nameOf(x.u))));
      var main = el('span', 'sub-cand__main');
      var top = el('span', 'sub-cand__top');
      top.appendChild(el('span', 'sub-cand__name', nameOf(x.u)));
      if (best) top.appendChild(el('span', 'sub-chip sub-chip--best', 'лучший вариант'));
      main.appendChild(top);
      if (!isBusy) {
        var chips = el('span', 'sub-cand__chips');
        badgeText(x.b).forEach(function (t, i) { chips.appendChild(el('span', 'sub-chip sub-chip--' + x.b[i], t)); });
        if (x.l) chips.appendChild(el('span', 'sub-chip sub-chip--load', 'замен в месяце: ' + x.l));
        if (chips.childNodes.length) main.appendChild(chips);
      }
      if (x.n) main.appendChild(el('span', 'sub-cand__note' + (isBusy ? ' is-bad' : ''), x.n));
      b.appendChild(main);
      b.appendChild(el('i', 'bi bi-check2 sub-cand__tick'));
      b.addEventListener('click', function () { choose(x.u); });
      return b;
    }

    function open(keys) {
      if (!modal || !keys.length) return;
      current = keys;
      var one = keys.length === 1 && data.lessons[keys[0]];
      lessonEl.textContent = one ? one.label : keys.length + ' ' + plural(keys.length, ['пара', 'пары', 'пар']) + ' · один преподаватель на все';
      var lists = listsFor(keys);
      var inputs = keys.map(function (k) { var r = rowOf(k); return r && qs('[data-sub-to]', r) ? qs('[data-sub-to]', r).value : ''; });
      var same = keys.length === 1 ? inputs[0] : '';
      freeBox.textContent = '';
      busyBox.textContent = '';
      var best = bestUid(lists.free);
      lists.free.forEach(function (x) { freeBox.appendChild(candidate(x, false, x.u === best, same === x.u)); });
      if (!lists.free.length) freeBox.appendChild(el('p', 'sub-muted sub-picker__empty', 'Свободных преподавателей в это время нет.'));
      lists.busy.forEach(function (x) { busyBox.appendChild(candidate(x, true, false, same === x.u)); });
      freeN.textContent = lists.free.length ? '· ' + lists.free.length : '';
      busyN.textContent = lists.busy.length ? '· ' + lists.busy.length : '';
      busyDetails.hidden = !lists.busy.length;
      busyDetails.open = !lists.free.length && lists.busy.length > 0;
      qsa('[data-sub-value]', modalEl).forEach(function (c) { c.classList.toggle('active', same === c.getAttribute('data-sub-value')); });
      searchEl.value = '';
      modal.show();
    }

    function choose(value) {
      current.forEach(function (k) {
        var r = rowOf(k), input = r && qs('[data-sub-to]', r);
        if (!input) return;
        input.value = value;
        input.dispatchEvent(new Event('change', { bubbles: true }));
        render(r);
      });
      if (modal) modal.hide();
    }

    function filter() {
      var q = searchEl.value.trim().toLowerCase();
      qsa('.sub-cand', modalEl).forEach(function (c) { c.hidden = q && c.getAttribute('data-name').indexOf(q) === -1; });
    }

    rows.forEach(function (r) {
      var btn = qs('[data-sub-choose]', r), pick = qs('[data-sub-pick]', r);
      if (btn) btn.addEventListener('click', function () { open([r.getAttribute('data-sub-row')]); });
      if (pick) pick.addEventListener('change', function () { render(r); });
      render(r);
    });
    qsa('[data-sub-all]', form).forEach(function (b) {
      b.addEventListener('click', function () {
        var on = b.getAttribute('data-sub-all') === '1';
        rows.forEach(function (r) { var c = qs('[data-sub-pick]', r); if (c && !c.disabled) { c.checked = on; render(r); } });
      });
    });
    var bulk = qs('[data-sub-bulk]', form);
    if (bulk) bulk.addEventListener('click', function () {
      var keys = rows.filter(function (r) { var c = qs('[data-sub-pick]', r); return c && !c.disabled && c.checked && qs('[data-sub-to]', r); })
        .map(function (r) { return r.getAttribute('data-sub-row'); });
      if (!keys.length) { if (CC.toast) CC.toast('Сначала отметьте пары, которые нужно заменить.', 'warning'); return; }
      open(keys);
    });
    if (modalEl) {
      qsa('[data-sub-value]', modalEl).forEach(function (c) { c.addEventListener('click', function () { choose(c.getAttribute('data-sub-value')); }); });
      searchEl.addEventListener('input', filter);
      searchEl.addEventListener('keydown', function (e) {
        if (e.key !== 'Enter') return;
        e.preventDefault();
        var first = qsa('.sub-cand', modalEl).filter(function (c) { return !c.hidden && !c.classList.contains('is-busy'); })[0];
        if (first) choose(first.getAttribute('data-uid'));
      });
      modalEl.addEventListener('shown.bs.modal', function () { if (window.matchMedia && window.matchMedia('(min-width: 768px)').matches) searchEl.focus(); });
    }
  }

  onInit(function (root) {
    initStep1(root);
    qsa('form[data-sub-form]', root).forEach(initForm);
  });
})();
