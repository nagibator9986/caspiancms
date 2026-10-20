/* ==========================================================================
   schedule.js — раздел «Расписание» (владелец — SCHEDULE). Зависит от app.js (window.CC).
   • Редактор занятий: конструктор (обычная POST-форма) и правка недели группы (сохранение JSON
     в /schedule/update_group — только изменённые строки, удаляются только отмеченные; Ctrl+S).
     Строка: день задаёт секция (перенос — «Перенести на…» в ⋯), время из сетки звонков дня или
     своё, дубль, удаление/возврат, подсветка изменений, пересечения внутри группы (локально) и
     накладки по всему колледжу (POST /api/schedule/check → cms.conflicts).
   • Правка одного занятия (страница и быстрый просмотр): сетка звонков по дню, своё время,
     живая проверка накладок, подсказка свободных аудиторий, подтверждение при критичных накладках.
   • Неделя: переключатель дня на телефоне (кнопки, свайп, ← →), соседние группы — [ и ].
   Данные из шаблона — только через <script type="application/json"> (без |safe).
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC || {};
  var doc = document;
  var DAY_SHORT = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
  var WEEKS = ['always', 'numerator', 'denominator'];

  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onReady(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function onInit(fn) {   // для документа и для HTML, вставленного в быстрый просмотр
    if (CC.onInit) CC.onInit(fn); else onReady(function () { fn(doc); });
  }
  function debounce(fn, ms) {
    var t;
    return function () { var a = arguments; clearTimeout(t); t = setTimeout(function () { fn.apply(null, a); }, ms); };
  }
  function readJSON(id, fallback, root) {
    var el = (root && qs('#' + id, root)) || doc.getElementById(id);
    if (!el) return fallback;
    try { var v = JSON.parse(el.textContent || 'null'); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function toast(text, cat, opts) { if (CC.toast) CC.toast(text, cat, opts); }
  function plural(n, forms) {
    if (CC.plural) return CC.plural(n, forms);
    var m10 = n % 10, m100 = n % 100;
    return forms[m10 === 1 && m100 !== 11 ? 0 : (m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? 1 : 2)];
  }
  function fire(el, type) { if (el) el.dispatchEvent(new Event(type, { bubbles: true })); }

  /* ----------------------------- Время ----------------------------- */
  // Как parse_time_range на сервере: первое ЧЧ[.:]ММ — начало, второе — конец.
  function parseRange(s) {
    var re = /(\d{1,2})[:.](\d{2})/g, out = [], m;
    while ((m = re.exec(String(s || ''))) && out.length < 2) {
      var h = +m[1], mi = +m[2];
      out.push(h > 23 || mi > 59 ? null : h * 60 + mi);
    }
    if (!out.length || out[0] === null) return null;
    var end = out.length > 1 ? out[1] : out[0] + 50;
    if (end === null || end <= out[0]) return null;
    return [out[0], end];
  }
  function fmtMin(m) { var h = Math.floor(m / 60), mi = m % 60; return (h < 10 ? '0' : '') + h + ':' + (mi < 10 ? '0' : '') + mi; }
  function slotLabel(s) { return String(s).replace(/\./g, ':').replace('-', '–'); }
  function normWeek(w) { return WEEKS.indexOf(w) >= 0 ? w : 'always'; }
  function weeksOverlap(a, b) { a = normWeek(a); b = normWeek(b); return a === 'always' || b === 'always' || a === b; }

  function addOption(select, value, label, selected) {
    var o = doc.createElement('option');
    o.value = value;
    o.textContent = label;
    if (selected) o.selected = true;
    select.appendChild(o);
    return o;
  }
  /** Список времени для дня: сетка звонков (Пн — своя) + текущее значение, если его нет в сетке, + «Другое…». */
  function fillTime(select, day, value, call, custom) {
    var slots = (call && call[day === 0 ? 'monday' : 'other']) || [];
    var cur = value === undefined ? select.value : value;
    var curRange = cur && cur !== custom ? parseRange(cur) : null;
    var matched = slots.indexOf(cur) >= 0;
    select.innerHTML = '';
    if (!cur) addOption(select, '', 'Время…', true);
    // «10:25-11:15» (так пишет приложение) = слот «10.25-11.15»: значение не меняем, чтобы не было лишней записи
    var values = slots.map(function (s) {
      var r = parseRange(s);
      if (!matched && curRange && r && r[0] === curRange[0] && r[1] === curRange[1]) { matched = true; return cur; }
      return s;
    });
    if (cur && cur !== custom && !matched) addOption(select, cur, cur + ' (не из сетки)', true);
    slots.forEach(function (s, i) { addOption(select, values[i], slotLabel(s), values[i] === cur); });
    addOption(select, custom, 'Другое время…', cur === custom);
  }
  function ensureOption(select, value) {
    if (!select || value === undefined || value === null) return;
    var has = qsa('option', select).some(function (o) { return o.value === value; });
    if (!has) addOption(select, value, value);
    select.value = value;
  }
  function toggleCustom(container, custom) {
    var sel = qs('[data-f="time"]', container), input = qs('[data-f="time_custom"]', container);
    if (!sel || !input) return;
    var on = sel.value === custom;
    input.hidden = !on;
    input.required = on;
    if (on && !input.value) setTimeout(function () { input.focus(); }, 0);
  }

  /* ----------------------------- Редактор занятий ----------------------------- */
  function initEditor(root) {
    if (root._schEditor) return;
    root._schEditor = true;
    var mode = root.getAttribute('data-editor');                // 'builder' | 'group'
    var CUSTOM = root.getAttribute('data-custom-time') || '__custom__';
    var CALL = readJSON('schCallSchedule', { monday: [], other: [] }, root);
    var tpl = qs('template[data-row-template]', root);
    var form = root.tagName === 'FORM' ? root : null;
    var groupSelect = qs('[data-builder-group]', root);
    var existing = mode === 'builder' ? readJSON('schExisting', [], root) : [];
    var remote = {};          // key строки → [{severity, text}] от сервера
    var checkSeq = 0;
    var seq = 0;
    var saving = false;

    function rows() { return qsa('[data-row]', root); }
    function field(row, f) { return qs('[data-f="' + f + '"]', row); }
    function val(row, f) { var el = field(row, f); return el ? String(el.value || '') : ''; }
    function group() { return mode === 'group' ? root.getAttribute('data-group') : (groupSelect ? groupSelect.value : ''); }
    function section(day) { return qs('[data-day-section="' + (day === null || day === undefined ? '' : day) + '"]', root); }
    function markDirty() { if (form) form.dispatchEvent(new Event('change')); }

    function values(row) {
      var time = val(row, 'time');
      if (time === CUSTOM) time = val(row, 'time_custom');
      var day = val(row, 'day');
      return {
        key: row.getAttribute('data-key'), id: row.getAttribute('data-id') || '',
        day: day === '' ? null : parseInt(day, 10), time: time.trim(), subject: val(row, 'subject'),
        teacher: val(row, 'teacher'), room: val(row, 'room').trim(), week_type: val(row, 'week_type') || 'always'
      };
    }
    function orig(row) {
      var raw = row.getAttribute('data-orig');
      if (!raw) return null;
      try { return JSON.parse(raw); } catch (e) { return null; }
    }
    function isDeleted(row) { return row.classList.contains('is-deleted'); }
    function isFilled(v) { return !!(v.subject || v.time); }
    function isChanged(row) {
      var o = orig(row);
      if (!o) return false;
      var v = values(row);
      if ((o.day === null ? null : +o.day) !== v.day) return true;
      return ['time', 'subject', 'teacher', 'room', 'week_type'].some(function (f) {
        var a = o[f] === null || o[f] === undefined ? (f === 'week_type' ? 'always' : '') : String(o[f]);
        return a.trim() !== String(v[f]).trim();
      });
    }
    function validate(v, row) {
      var errs = [];
      if (v.day === null || isNaN(v.day)) errs.push('Выберите день недели');
      if (!v.time) errs.push('Укажите время');
      else {
        var o = row ? orig(row) : null;
        if ((!o || String(o.time || '') !== v.time) && !parseRange(v.time)) errs.push('Время в формате ЧЧ.ММ-ЧЧ.ММ (например, 08.30-09.50)');
      }
      if (!v.subject) errs.push('Укажите дисциплину');
      return errs;
    }

    /* --- метки ошибок/предупреждений в строке --- */
    function setIssues(row, kind, items) {
      var box = qs('[data-issues]', row);
      if (!box) return;
      qsa('[data-issue="' + kind + '"]', box).forEach(function (n) { n.remove(); });
      (items || []).forEach(function (it) {
        var span = doc.createElement('span');
        var sev = it.severity || 'warning';
        span.className = 'sch-issue sch-issue--' + sev;
        span.setAttribute('data-issue', kind);
        var icon = doc.createElement('i');
        icon.className = 'bi bi-' + (sev === 'critical' || sev === 'danger' ? 'exclamation-octagon' : (sev === 'info' ? 'info-circle' : 'exclamation-triangle'));
        icon.setAttribute('aria-hidden', 'true');
        span.appendChild(icon);
        span.appendChild(doc.createTextNode(it.text));
        box.appendChild(span);
      });
      paintRow(row);
    }
    function paintRow(row) {
      var box = qs('[data-issues]', row);
      var hasErr = box && box.querySelector('[data-issue="error"], [data-server-error]');
      var hasWarn = box && box.querySelector('.sch-issue--warning[data-issue], .sch-issue--critical[data-issue]');
      row.classList.toggle('has-error', !!hasErr);
      row.classList.toggle('has-warning', !hasErr && !!hasWarn && !isDeleted(row));
    }

    /* --- секции и счётчики --- */
    function refreshSections() {
      qsa('[data-day-section]', root).forEach(function (sec) {
        var n = qsa('[data-row]', sec).length;
        var cnt = qs('[data-section-count]', sec);
        if (cnt) cnt.textContent = n || '';
        sec.classList.toggle('is-empty', n === 0);
        if (sec.getAttribute('data-day-section') === '' && n === 0) sec.hidden = true;
      });
    }
    function refreshCounters() {
      var changed = 0, added = 0, deleted = 0, filled = 0, issues = 0;
      rows().forEach(function (row) {
        var v = values(row);
        if (isDeleted(row)) { deleted += 1; return; }
        if (row.classList.contains('has-warning') || row.classList.contains('has-error')) issues += 1;
        if (!v.id) { if (isFilled(v)) { added += 1; filled += 1; } row.classList.toggle('is-new', mode === 'group' && isFilled(v)); return; }
        var ch = isChanged(row);
        row.classList.toggle('is-changed', ch);
        if (ch) changed += 1;
      });
      function put(sel, n) { var el = qs(sel, root); if (el) el.textContent = n; }
      put('[data-count-changed]', changed);
      put('[data-count-new]', added);
      put('[data-count-deleted]', deleted);
      put('[data-count-filled]', filled);
      var warn = qs('[data-count-issues]', root);
      if (warn) { warn.hidden = !issues; var b = qs('b', warn); if (b) b.textContent = issues; }
      return { changed: changed, added: added, deleted: deleted };
    }

    /* --- пересечения внутри группы (без сервера) --- */
    function localCheck() {
      var byDay = {};
      rows().forEach(function (row) {
        if (isDeleted(row)) return;
        var v = values(row), r = parseRange(v.time);
        if (v.day === null || !r || !isFilled(v)) return;
        (byDay[v.day] = byDay[v.day] || []).push({ row: row, v: v, s: r[0], e: r[1] });
      });
      var issues = new Map();
      function push(row, text, severity) { if (!issues.has(row)) issues.set(row, []); issues.get(row).push({ text: text, severity: severity }); }
      Object.keys(byDay).forEach(function (d) {
        var items = byDay[d];
        for (var i = 0; i < items.length; i++) {
          for (var j = i + 1; j < items.length; j++) {
            var a = items[i], b = items[j];
            if (a.s < b.e && b.s < a.e && weeksOverlap(a.v.week_type, b.v.week_type)) {
              var same = a.v.subject === b.v.subject && a.v.teacher === b.v.teacher && a.v.room === b.v.room;
              var sev = same ? 'warning' : (a.v.teacher !== b.v.teacher && a.v.subject === b.v.subject ? 'warning' : 'critical');
              var what = same ? 'Дубль: ' : (sev === 'warning' ? 'Возможно, подгруппы: ' : 'Пересекается с ');
              push(a.row, what + fmtMin(b.s) + '–' + fmtMin(b.e) + ' ' + b.v.subject, sev);
              push(b.row, what + fmtMin(a.s) + '–' + fmtMin(a.e) + ' ' + a.v.subject, sev);
            }
          }
          if (mode === 'builder') {
            existing.forEach(function (l) {
              if (l.day_index !== +d || l.start === null || l.end === null) return;
              var it = items[i];
              if (it.s < l.end && l.start < it.e && weeksOverlap(it.v.week_type, l.week_type)) {
                push(it.row, 'У группы уже есть пара: ' + (l.time_label || '') + ' ' + (l.subject || ''), 'critical');
              }
            });
          }
        }
      });
      rows().forEach(function (row) { setIssues(row, 'local', issues.get(row) || []); });
    }

    /* --- накладки по колледжу (сервер, cms.conflicts) --- */
    var remoteCheck = debounce(function () {
      var url = root.getAttribute('data-check-url');
      var g = group();
      if (!url || !g || !CC.fetchJSON) return;
      var payload = [], excl = [];
      rows().forEach(function (row) {
        var v = values(row);
        if (v.id) excl.push(v.id);
        if (isDeleted(row) || !isFilled(v) || v.day === null || !parseRange(v.time)) { setIssues(row, 'remote', []); return; }
        if (mode === 'group' && v.id && !isChanged(row)) { setIssues(row, 'remote', []); return; }
        payload.push({ key: v.key, id: v.id, groupId: g, day: v.day, time: v.time, subject: v.subject,
                       teacher: v.teacher, room: v.room, week_type: v.week_type });
      });
      if (!payload.length) { refreshCounters(); return; }
      var my = ++checkSeq;
      CC.fetchJSON(url, { method: 'POST', body: { lessons: payload, exclude_ids: mode === 'group' ? excl : [] } })
        .then(function (data) {
          if (my !== checkSeq) return;
          remote = {};
          (data.results || []).forEach(function (r) { remote[r.key] = r.conflicts || []; });
          rows().forEach(function (row) {
            var k = row.getAttribute('data-key');
            if (k in remote) setIssues(row, 'remote', remote[k]);
          });
          refreshCounters();
        })
        .catch(function () { /* проверка — подсказка; ошибка сети не мешает сохранить */ });
    }, 700);

    function changed(row) {
      if (row) { setIssues(row, 'error', []); qsa('[data-server-error]', row).forEach(function (n) { n.remove(); }); paintRow(row); }
      localCheck();
      refreshCounters();
      remoteCheck();
    }

    /* --- операции со строками --- */
    function newRow(day, from) {
      seq += 1;
      var key = 'n' + Date.now().toString(36) + seq;
      var wrap = doc.createElement('div');
      wrap.innerHTML = tpl.innerHTML.split('__KEY__').join(key).trim();
      var row = wrap.firstElementChild;
      row.setAttribute('data-key', key);
      row.setAttribute('data-id', '');
      row.removeAttribute('data-orig');
      field(row, 'day').value = String(day);
      fillTime(field(row, 'time'), day, from ? from.time : '', CALL, CUSTOM);
      if (from) {
        ensureOption(field(row, 'subject'), from.subject);
        ensureOption(field(row, 'teacher'), from.teacher);
        field(row, 'room').value = from.room || '';
        ensureOption(field(row, 'week_type'), from.week_type || 'always');
      }
      return row;
    }
    function placeRow(row, day, after) {
      var sec = section(day);
      if (!sec) return;
      sec.hidden = false;
      var list = qs('[data-rows]', sec);
      if (after && after.parentNode === list) list.insertBefore(row, after.nextSibling);
      else list.appendChild(row);
      refreshSections();
    }
    function addRow(day) {
      var row = newRow(day);
      placeRow(row, day);
      var t = field(row, 'time');
      if (t) t.focus();
      markDirty();
      changed(row);
    }
    function moveRow(row, day) {
      var sel = field(row, 'day');
      if (!sel || String(sel.value) === String(day)) return;
      if (!qsa('option', sel).some(function (o) { return o.value === String(day); })) addOption(sel, String(day), DAY_SHORT[day]);
      sel.value = String(day);
      fire(sel, 'change');        // дальше — как при выборе дня (обработчик change ниже)
      try { row.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (e) { row.scrollIntoView(); }
      toast('Занятие перенесено: ' + (DAY_SHORT[day] || ''), 'info');
    }

    root.addEventListener('click', function (e) {
      var t = e.target.closest ? e.target.closest('button') : null;
      if (!t || !root.contains(t)) return;
      var row = t.closest('[data-row]');
      if (t.hasAttribute('data-add-row')) { addRow(parseInt(t.getAttribute('data-add-row'), 10)); return; }
      if (t.hasAttribute('data-show-sunday')) {
        var sun = section(6);
        if (sun) { sun.hidden = false; sun.scrollIntoView({ behavior: 'smooth', block: 'center' }); }
        t.hidden = true;
        return;
      }
      if (t.hasAttribute('data-row-move') && row) { moveRow(row, parseInt(t.getAttribute('data-row-move'), 10)); return; }
      if (t.hasAttribute('data-row-duplicate') && row) {
        var v = values(row);
        var copy = newRow(v.day === null ? 0 : v.day, v);
        placeRow(copy, v.day === null ? 0 : v.day, row);
        markDirty();
        changed(copy);
        return;
      }
      if (t.hasAttribute('data-row-delete') && row) {
        if (row.getAttribute('data-id')) {
          row.classList.add('is-deleted'); setIssues(row, 'remote', []); setIssues(row, 'local', []);
          toast('Занятие удалится после сохранения', 'info', { action: { label: 'Вернуть', onClick: function () { row.classList.remove('is-deleted'); changed(row); } } });
        } else { row.remove(); refreshSections(); }
        markDirty();
        changed(null);
        return;
      }
      if (t.hasAttribute('data-row-restore') && row) { row.classList.remove('is-deleted'); markDirty(); changed(row); }
    });

    root.addEventListener('change', function (e) {
      var el = e.target;
      var row = el.closest ? el.closest('[data-row]') : null;
      if (!row) return;
      var f = el.getAttribute('data-f');
      if (f === 'day') {
        var day = el.value === '' ? null : parseInt(el.value, 10);
        var tsel = field(row, 'time');
        var keep = tsel.value;
        fillTime(tsel, day, keep, CALL, CUSTOM);
        if (day !== null) placeRow(row, day);
        toggleCustom(row, CUSTOM);
      }
      if (f === 'time') toggleCustom(row, CUSTOM);
      changed(row);
    });
    root.addEventListener('input', function (e) {
      var row = e.target.closest ? e.target.closest('[data-row]') : null;
      if (row && (e.target.getAttribute('data-f') === 'room' || e.target.getAttribute('data-f') === 'time_custom')) changed(row);
    });

    /* --- конструктор: существующие пары группы --- */
    function renderExisting() {
      var box = qs('[data-existing]', root);
      if (!box) return;
      box.innerHTML = '';
      function note(text) { var p = doc.createElement('p'); p.className = 'cc-muted mb-0'; p.textContent = text; box.appendChild(p); }
      if (!group()) { note('Выберите группу — здесь появятся её текущие пары.'); return; }
      if (!existing.length) { note('У группы пока нет занятий.'); return; }
      var byDay = {};
      existing.forEach(function (l) { var d = l.day_index === null || l.day_index === undefined ? 7 : l.day_index; (byDay[d] = byDay[d] || []).push(l); });
      Object.keys(byDay).sort().forEach(function (d) {
        var line = doc.createElement('div');
        line.className = 'sch-existing__day';
        var b = doc.createElement('b');
        b.textContent = DAY_SHORT[d] || '—';
        var list = doc.createElement('div');
        list.className = 'sch-existing__list';
        byDay[d].sort(function (x, y) { return (x.start || 0) - (y.start || 0); }).forEach(function (l) {
          var s = doc.createElement('span');
          s.className = 'sch-existing__item sch-existing__item--' + normWeek(l.week_type);
          s.textContent = (l.time_label || l.time || '') + ' ' + (l.subject || '');
          s.title = [l.teacher_name, l.room].filter(Boolean).join(' · ');
          list.appendChild(s);
        });
        line.appendChild(b);
        line.appendChild(list);
        box.appendChild(line);
      });
    }
    if (groupSelect) {
      groupSelect.addEventListener('change', function () {
        var g = groupSelect.value;
        existing = [];
        renderExisting();
        if (!g) { localCheck(); return; }
        var url = (root.getAttribute('data-group-url') || '').replace('__G__', encodeURIComponent(g));
        fetch(url, { credentials: 'same-origin', headers: { 'Accept': 'application/json' } })
          .then(function (r) { return r.json(); })
          .then(function (data) { if (groupSelect.value === g) { existing = data.schedule || []; renderExisting(); changed(null); } })
          .catch(function () { toast('Не удалось загрузить пары группы', 'warning'); });
      });
    }

    /* --- сохранение --- */
    function markErrors(errors) {
      var first = null;
      (errors || []).forEach(function (er) {
        var row = qs('[data-row][data-key="' + (window.CSS && CSS.escape ? CSS.escape(er.key) : er.key) + '"]', root);
        if (!row) return;
        setIssues(row, 'error', (er.messages || []).map(function (m) { return { text: m, severity: 'danger' }; }));
        first = first || row;
      });
      if (first) first.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
    function clientErrors(onlyFilled) {
      var errors = [];
      rows().forEach(function (row) {
        if (isDeleted(row)) return;
        var v = values(row);
        if (onlyFilled && !v.id && !isFilled(v)) { setIssues(row, 'error', []); return; }
        var errs = validate(v, row);
        setIssues(row, 'error', errs.map(function (m) { return { text: m, severity: 'danger' }; }));
        if (errs.length) errors.push({ key: v.key, messages: errs });
      });
      return errors;
    }

    function save(btn) {
      if (saving) return;
      var errors = clientErrors(true);
      if (errors.length) { markErrors(errors); toast('Проверьте отмеченные занятия', 'error'); return; }
      var payload = { group_id: group(), lessons: [], deleted_ids: [], next: root.getAttribute('data-return') || '' };
      rows().forEach(function (row) {
        var v = values(row);
        if (isDeleted(row)) { if (v.id) payload.deleted_ids.push(v.id); return; }
        if (!v.id && !isFilled(v)) return;
        if (v.id && !isChanged(row)) return;      // без изменений — не отправляем (сервер их и так не пишет)
        var item = { key: v.key, id: v.id, day: v.day, time: v.time, subject: v.subject, teacher: v.teacher,
                     room: v.room, week_type: v.week_type };
        var o = orig(row);
        if (o) item.orig = o;
        payload.lessons.push(item);
      });
      if (!payload.lessons.length && !payload.deleted_ids.length) {
        if (form && CC.markClean) CC.markClean(form);   // правки отменены вручную — сохранять нечего
        toast('Изменений нет', 'info');
        return;
      }
      function go() {
        saving = true;
        if (btn) btn.disabled = true;
        var label = btn ? qs('span', btn) : null, old = label ? label.textContent : '';
        if (label) label.textContent = 'Сохраняем…';
        if (CC.progress) CC.progress.start();
        CC.fetchJSON(root.getAttribute('data-save-url'), { method: 'POST', body: payload })
          .then(function (data) {
            if (form && CC.markClean) CC.markClean(form);
            window.location.href = data.redirect || root.getAttribute('data-return') || '/schedule';
          })
          .catch(function (err) {
            saving = false;
            if (CC.progress) CC.progress.done();
            if (btn) btn.disabled = false;
            if (label) label.textContent = old;
            if (err.data && err.data.errors) markErrors(err.data.errors);
            toast(err.message || 'Не удалось сохранить', 'error');
          });
      }
      var n = payload.deleted_ids.length;
      if (n && CC.confirm) {
        CC.confirm('Удалить ' + n + ' ' + plural(n, ['занятие', 'занятия', 'занятий']) + ' и сохранить остальные изменения? Удалённые занятия вернуть нельзя.',
                   { title: 'Сохранение недели', okText: 'Удалить и сохранить', danger: true })
          .then(function (ok) { if (ok) go(); });
      } else {
        go();
      }
    }

    if (form) {
      form.addEventListener('submit', function (e) {
        if (mode === 'group') {   // правка недели: JSON-сохранение (Ctrl+S и кнопка «Сохранить»)
          e.preventDefault();     // форма остаётся «несохранённой», пока сервер не ответил (CC.markClean в save)
          save(e.submitter || qs('[data-cc-save]', form));
          return;
        }
        if (!group()) {
          e.preventDefault();
          e.stopImmediatePropagation();
          toast('Выберите группу', 'error');
          if (groupSelect) groupSelect.focus();
          return;
        }
        var errors = clientErrors(true);
        if (errors.length) { e.preventDefault(); e.stopImmediatePropagation(); markErrors(errors); toast('Проверьте отмеченные занятия', 'error'); return; }
        // своё время → в поле time, чтобы сервер получил одно значение
        rows().forEach(function (row) {
          var t = field(row, 'time');
          if (t && t.value === CUSTOM) ensureOption(t, val(row, 'time_custom').trim());
        });
      });
    }

    // старт: подписи времени по дню строки, своё время, проверки
    rows().forEach(function (row) { toggleCustom(row, CUSTOM); });
    refreshSections();
    renderExisting();
    localCheck();
    refreshCounters();
    if (mode === 'builder' && rows().some(function (r) { return isFilled(values(r)); })) remoteCheck();
  }

  /* ----------------------------- Правка одного занятия ----------------------------- */
  function initLessonForm(formEl) {
    if (formEl._schLesson) return;
    formEl._schLesson = true;
    var CUSTOM = formEl.getAttribute('data-custom-time') || '__custom__';
    var CALL = readJSON('schCallSchedule', { monday: [], other: [] }, formEl);
    var box = qs('[data-live-conflicts]', formEl);
    var freeBox = qs('[data-free-rooms]', formEl);
    var critical = parseInt(formEl.getAttribute('data-critical') || '0', 10) || 0;
    var my = 0, myFree = 0;
    function v(name) { var el = formEl.elements[name]; return el ? String(el.value || '') : ''; }
    function timeValue() { return v('time') === CUSTOM ? v('time_custom') : v('time'); }

    function renderConflicts(items) {
      box.innerHTML = '';
      critical = items.filter(function (c) { return c.severity === 'critical'; }).length;
      formEl.setAttribute('data-critical', String(critical));
      if (!items.length) {
        var p = doc.createElement('p');
        p.className = 'sch-live__ok';
        var ok = doc.createElement('i'); ok.className = 'bi bi-check-circle'; ok.setAttribute('aria-hidden', 'true');
        p.appendChild(ok);
        p.appendChild(doc.createTextNode('Накладок нет: преподаватель, аудитория и группа в это время свободны.'));
        box.appendChild(p);
        return;
      }
      var ul = doc.createElement('ul');
      ul.className = 'sch-conflicts';
      items.forEach(function (c) {
        var li = doc.createElement('li');
        var sev = c.severity || 'warning';
        li.className = 'sch-conflicts__item sch-conflicts__item--' + sev;
        var i = doc.createElement('i');
        i.className = 'bi bi-' + (sev === 'critical' ? 'exclamation-octagon-fill' : (sev === 'info' ? 'info-circle-fill' : 'exclamation-triangle-fill'));
        i.setAttribute('aria-hidden', 'true');
        var span = doc.createElement('span'); span.textContent = c.text;
        li.appendChild(i); li.appendChild(span);
        ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    var check = debounce(function () {
      var url = formEl.getAttribute('data-check-url');
      if (!url || !box || !CC.fetchJSON) return;
      var time = timeValue();
      if (v('day') === '' || !parseRange(time)) return;
      var n = ++my;
      box.classList.add('is-loading');
      CC.fetchJSON(url, { method: 'POST', body: { lessons: [{ key: 'lesson', id: formEl.getAttribute('data-lesson-id'),
        groupId: v('groupId'), day: v('day'), time: time, subject: v('subject'), teacher: v('teacher'), room: v('room'),
        week_type: v('week_type') }] } })
        .then(function (data) {
          if (n !== my) return;
          box.classList.remove('is-loading');
          renderConflicts(((data.results || [])[0] || {}).conflicts || []);
        })
        .catch(function () { box.classList.remove('is-loading'); /* подсказка — не критично */ });
    }, 500);

    /* «Свободны в это время: 101 · 201 · 302» — клик подставляет аудиторию */
    var free = debounce(function () {
      var url = formEl.getAttribute('data-free-url');
      if (!url || !freeBox || !CC.fetchJSON) return;
      var r = parseRange(timeValue());
      if (v('day') === '' || !r) { freeBox.hidden = true; return; }
      var w = v('week_type');
      var week = w === 'numerator' || w === 'denominator' ? w : 'both';
      var n = ++myFree;
      CC.fetchJSON(url + '?day=' + encodeURIComponent(v('day')) + '&from=' + fmtMin(r[0]) + '&to=' + fmtMin(r[1]) + '&week=' + week)
        .then(function (data) {
          if (n !== myFree) return;
          var rooms = (data.free || []).map(function (x) { return x.room; });
          freeBox.innerHTML = '';
          var label = doc.createElement('span');
          label.className = 'sch-free-hint__label';
          label.textContent = rooms.length ? 'Свободны в это время:' : 'Свободных аудиторий в это время нет';
          freeBox.appendChild(label);
          var cur = v('room').trim().toLowerCase();
          rooms.slice(0, 12).forEach(function (name) {
            var b = doc.createElement('button');
            b.type = 'button';
            b.textContent = name;
            b.setAttribute('aria-pressed', name.toLowerCase() === cur ? 'true' : 'false');
            b.setAttribute('aria-label', 'Поставить аудиторию ' + name);
            b.addEventListener('click', function () {
              var input = formEl.elements.room;
              input.value = name;
              fire(input, 'input'); fire(input, 'change');
              qsa('button', freeBox).forEach(function (o) { o.setAttribute('aria-pressed', o === b ? 'true' : 'false'); });
            });
            freeBox.appendChild(b);
          });
          if (rooms.length > 12) {
            var more = doc.createElement('span');
            more.textContent = 'и ещё ' + (rooms.length - 12);
            freeBox.appendChild(more);
          }
          freeBox.hidden = false;
        })
        .catch(function () { freeBox.hidden = true; });
    }, 500);

    formEl.addEventListener('change', function (e) {
      var f = e.target.getAttribute('data-f');
      if (f === 'day') {
        var day = e.target.value === '' ? null : parseInt(e.target.value, 10);
        var sel = qs('[data-f="time"]', formEl);
        fillTime(sel, day, sel.value, CALL, CUSTOM);
      }
      if (f === 'time' || f === 'day') toggleCustom(formEl, CUSTOM);
      check();
      if (f === 'day' || f === 'time' || f === 'week_type') free();
    });
    formEl.addEventListener('input', function (e) {
      if (e.target.name === 'room') check();
      if (e.target.name === 'time_custom') { check(); free(); }
    });
    formEl.addEventListener('submit', function (e) {
      if (e.defaultPrevented) return;
      var sel = qs('[data-f="time"]', formEl);
      if (sel && sel.value === CUSTOM) ensureOption(sel, v('time_custom').trim());
      // критичные накладки не блокируют сохранение, но спрашиваем (UX_SPEC_v2 SCH-4)
      if (critical > 0 && !formEl._schConfirmed && CC.confirm) {
        e.preventDefault();
        e.stopImmediatePropagation();
        var submitter = e.submitter;
        CC.confirm('Есть ' + critical + ' ' + plural(critical, ['накладка', 'накладки', 'накладок']) + '. Сохранить всё равно?',
                   { title: 'Накладки в расписании', okText: 'Сохранить', danger: false })
          .then(function (ok) {
            if (!ok) return;
            formEl._schConfirmed = true;
            if (formEl.requestSubmit) formEl.requestSubmit(submitter || undefined); else formEl.submit();
          });
        return;
      }
      formEl._schConfirmed = false;
    });
    toggleCustom(formEl, CUSTOM);
    free();
  }

  /* ----------------------------- Переключатель дня (телефон) ----------------------------- */
  var keysBound = false;
  function initDaySwitch(nav) {
    if (nav._schSwitch) return;
    nav._schSwitch = true;
    var target = qs(nav.getAttribute('data-sch-dayswitch'));
    if (!target) return;
    var buttons = qsa('button[data-day]', nav);
    function current() { return buttons.filter(function (b) { return b.classList.contains('active'); })[0] || buttons[0]; }
    function show(btn) {
      if (!btn) return;
      var day = btn.getAttribute('data-day');
      buttons.forEach(function (b) { var on = b === btn; b.classList.toggle('active', on); b.setAttribute('aria-pressed', on ? 'true' : 'false'); });
      qsa('[data-day]', target).forEach(function (el) { el.classList.toggle('is-active-day', el.getAttribute('data-day') === day); });
      try { btn.scrollIntoView({ inline: 'center', block: 'nearest' }); } catch (e) { /* старый браузер */ }
    }
    function step(dir) {
      var i = buttons.indexOf(current()) + dir;
      if (i < 0 || i >= buttons.length) return false;
      show(buttons[i]);
      return true;
    }
    nav._schStep = step;
    buttons.forEach(function (b) { b.addEventListener('click', function () { show(b); }); });
    // свайп по сетке: влево — следующий день, вправо — предыдущий
    var x0 = null, y0 = null;
    target.addEventListener('touchstart', function (e) { if (e.touches.length === 1) { x0 = e.touches[0].clientX; y0 = e.touches[0].clientY; } }, { passive: true });
    target.addEventListener('touchend', function (e) {
      if (x0 === null || !e.changedTouches.length) return;
      var dx = e.changedTouches[0].clientX - x0, dy = e.changedTouches[0].clientY - y0;
      x0 = y0 = null;
      if (Math.abs(dx) > 60 && Math.abs(dx) > Math.abs(dy) * 1.5 && nav.offsetParent !== null) step(dx < 0 ? 1 : -1);
    }, { passive: true });
    // ← → — дни на телефоне. Действие стрелки отменяем, только если день переключился: иначе (полоска дней
    // скрыта — окно расширили — или день крайний) стрелки по-прежнему прокручивают страницу и таблицы.
    if (!keysBound && CC.hotkey && window.matchMedia && window.matchMedia('(max-width: 767.98px)').matches) {
      keysBound = true;
      var move = function (e, dir) {
        var n = qsa('[data-sch-dayswitch]').filter(function (x) { return x.offsetParent !== null; })[0];
        if (n && n._schStep && n._schStep(dir)) e.preventDefault();
      };
      CC.hotkey('ArrowLeft', function (e) { move(e, -1); }, 'Предыдущий день', { group: 'Расписание', preventDefault: false });
      CC.hotkey('ArrowRight', function (e) { move(e, 1); }, 'Следующий день', { group: 'Расписание', preventDefault: false });
    }
  }

  // ---------- Бланки для печати (/schedule/print, /schedule/print/teacher) ----------
  // Один владелец — всегда один лист A4: плотность (шрифт, высота строк) задаёт сервер по числу пар; если длинные
  // названия всё же не влезли — ужимаем содержимое листа (zoom; ширина остаётся во весь лист). Мерка — невидимый
  // .sch-sheet__probe высотой с печатную область: отношение высот не зависит от масштаба предпросмотра.
  function fitSheets() {
    qsa('.sch-sheet').forEach(function (sheet) {
      var page = qs('.sch-sheet__page', sheet), probe = qs('.sch-sheet__probe', sheet);
      if (!page || !probe) return;
      page.style.zoom = '';
      var avail = probe.getBoundingClientRect().height, h = page.getBoundingClientRect().height;
      if (avail > 0 && h > avail) page.style.zoom = String(Math.max(0.5, Math.floor(avail / h * 985) / 1000));
    });
  }
  // Предпросмотр на узком экране: листы шириной 210 мм уменьшаются до ширины контента (только экран — см. CSS).
  function previewSheets() {
    var wrap = qs('[data-sch-sheets]'), sheet = wrap && qs('.sch-sheet', wrap);
    if (!sheet) return;
    wrap.style.removeProperty('--sch-preview');
    var avail = wrap.clientWidth, w = sheet.offsetWidth;
    if (avail > 0 && w > avail) wrap.style.setProperty('--sch-preview', (avail / w).toFixed(3));
  }
  function initSheets() {
    if (!qs('[data-sch-sheets]')) return;
    fitSheets();
    previewSheets();
    window.addEventListener('load', fitSheets);
    window.addEventListener('beforeprint', fitSheets);
    window.addEventListener('resize', debounce(previewSheets, 150));
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(fitSheets);
  }

  onInit(function (root) {
    qsa('[data-editor]', root).forEach(initEditor);
    qsa('form[data-lesson-form]', root).forEach(initLessonForm);
    qsa('[data-sch-dayswitch]', root).forEach(initDaySwitch);
  });

  onReady(function () {
    initSheets();
    // соседние группы: [ и ]
    var page = qs('.sch-group-page');
    if (page && CC.hotkey) {
      var prev = page.getAttribute('data-sch-prev'), next = page.getAttribute('data-sch-next');
      if (prev) CC.hotkey('BracketLeft', function () { window.location.href = prev; }, 'Предыдущая группа', { group: 'Расписание' });
      if (next) CC.hotkey('BracketRight', function () { window.location.href = next; }, 'Следующая группа', { group: 'Расписание' });
    }
    doc.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-print]') : null;
      if (b) { e.preventDefault(); window.print(); }
    });
  });
})();
