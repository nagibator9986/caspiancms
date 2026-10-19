/* ==========================================================================
   schedule.js — раздел «Расписание» (владелец — SCHEDULE). Зависит от app.js (window.CC).
   • Редактор занятий: конструктор (обычная POST-форма) и редактор группы (сохранение JSON
     в /schedule/update_group — только изменённые строки, удаляются только отмеченные).
     Строка: смена дня (строка переезжает в секцию дня), время из сетки звонков этого дня
     или своё, дубль, удаление/возврат, подсветка изменений, пересечения внутри группы
     (локально) и накладки по всему колледжу (POST /api/schedule/check → cms.conflicts).
   • Правка одного занятия: сетка звонков по дню, своё время, живая проверка накладок.
   • Массовые действия в списке занятий группы; кнопки печати.
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
  function debounce(fn, ms) {
    var t;
    return function () { var a = arguments; clearTimeout(t); t = setTimeout(function () { fn.apply(null, a); }, ms); };
  }
  function readJSON(id, fallback) {
    var el = doc.getElementById(id);
    if (!el) return fallback;
    try { var v = JSON.parse(el.textContent || 'null'); return v === null ? fallback : v; } catch (e) { return fallback; }
  }
  function toast(text, cat) { if (CC.toast) CC.toast(text, cat); }

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
    var mode = root.getAttribute('data-editor');                // 'builder' | 'group'
    var CUSTOM = root.getAttribute('data-custom-time') || '__custom__';
    var CALL = readJSON('schCallSchedule', { monday: [], other: [] });
    var tpl = qs('template[data-row-template]', root);
    var form = root.tagName === 'FORM' ? root : null;
    var groupSelect = qs('[data-builder-group]', root);
    var existing = mode === 'builder' ? readJSON('schExisting', []) : [];
    var remote = {};          // key строки → [{severity, text}] от сервера
    var checkSeq = 0;
    var seq = 0;
    var dirty = false;
    var saving = false;

    function rows() { return qsa('[data-row]', root); }
    function field(row, f) { return qs('[data-f="' + f + '"]', row); }
    function val(row, f) { var el = field(row, f); return el ? String(el.value || '') : ''; }
    function group() { return mode === 'group' ? root.getAttribute('data-group') : (groupSelect ? groupSelect.value : ''); }
    function section(day) { return qs('[data-day-section="' + (day === null || day === undefined ? '' : day) + '"]', root); }

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
        if (cnt) cnt.textContent = n;
        var empty = qs('[data-empty]', sec);
        if (empty) empty.hidden = n > 0;
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
      dirty = true;
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
      changed(row);
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
      if (t.hasAttribute('data-row-duplicate') && row) {
        var v = values(row);
        var copy = newRow(v.day === null ? 0 : v.day, v);
        placeRow(copy, v.day === null ? 0 : v.day, row);
        changed(copy);
        return;
      }
      if (t.hasAttribute('data-row-delete') && row) {
        if (row.getAttribute('data-id')) { row.classList.add('is-deleted'); setIssues(row, 'remote', []); setIssues(row, 'local', []); }
        else { row.remove(); refreshSections(); }
        changed(null);
        return;
      }
      if (t.hasAttribute('data-row-restore') && row) { row.classList.remove('is-deleted'); changed(row); return; }
      if (t.hasAttribute('data-editor-save')) { save(t); }
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
      if (!group()) { var p0 = doc.createElement('p'); p0.className = 'cc-muted mb-0'; p0.textContent = 'Выберите группу — здесь появятся её текущие пары.'; box.appendChild(p0); return; }
      if (!existing.length) { var p1 = doc.createElement('p'); p1.className = 'cc-muted mb-0'; p1.textContent = 'У группы пока нет занятий.'; box.appendChild(p1); return; }
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
      if (!payload.lessons.length && !payload.deleted_ids.length) { toast('Изменений нет', 'info'); return; }
      function go() {
        saving = true;
        btn.disabled = true;
        var label = qs('span', btn), old = label ? label.textContent : '';
        if (label) label.textContent = 'Сохраняем…';
        CC.fetchJSON(root.getAttribute('data-save-url'), { method: 'POST', body: payload })
          .then(function (data) { dirty = false; window.location.href = data.redirect || root.getAttribute('data-return') || '/schedule'; })
          .catch(function (err) {
            saving = false;
            btn.disabled = false;
            if (label) label.textContent = old;
            if (err.data && err.data.errors) markErrors(err.data.errors);
            toast(err.message || 'Не удалось сохранить', 'error');
          });
      }
      var n = payload.deleted_ids.length;
      if (n && CC.confirm) {
        CC.confirm('Удалить ' + n + ' ' + (n % 10 === 1 && n % 100 !== 11 ? 'занятие' : (n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14) ? 'занятия' : 'занятий')) +
                   ' и сохранить остальные изменения?', { title: 'Сохранение расписания', okText: 'Сохранить', danger: true })
          .then(function (ok) { if (ok) go(); });
      } else {
        go();
      }
    }

    if (form) {
      form.addEventListener('submit', function (e) {
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
          if (t && t.value === CUSTOM) {
            var c = val(row, 'time_custom').trim();
            ensureOption(t, c);
          }
        });
        dirty = false;
      }, true);
    } else {
      window.addEventListener('beforeunload', function (e) {
        if (!dirty || saving) return;
        var st = refreshCounters();
        if (!st.changed && !st.added && !st.deleted) return;
        e.preventDefault();
        e.returnValue = '';
      });
    }

    // старт: подписи времени по дню строки, своё время, проверки
    rows().forEach(function (row) { toggleCustom(row, CUSTOM); });
    refreshSections();
    renderExisting();
    localCheck();
    refreshCounters();
    if (mode === 'builder' && rows().some(function (r) { return isFilled(values(r)); })) remoteCheck();
    dirty = false;
  }

  /* ----------------------------- Правка одного занятия ----------------------------- */
  function initLessonForm(formEl) {
    var CUSTOM = formEl.getAttribute('data-custom-time') || '__custom__';
    var CALL = readJSON('schCallSchedule', { monday: [], other: [] });
    var box = qs('[data-live-conflicts]');
    var my = 0;
    function v(name) { var el = formEl.elements[name]; return el ? String(el.value || '') : ''; }
    var check = debounce(function () {
      var url = formEl.getAttribute('data-check-url');
      if (!url || !box || !CC.fetchJSON) return;
      var time = v('time') === CUSTOM ? v('time_custom') : v('time');
      if (v('day') === '' || !parseRange(time)) return;
      var n = ++my;
      CC.fetchJSON(url, { method: 'POST', body: { lessons: [{ key: 'lesson', id: formEl.getAttribute('data-lesson-id'),
        groupId: v('groupId'), day: v('day'), time: time, subject: v('subject'), teacher: v('teacher'), room: v('room'),
        week_type: v('week_type') }] } })
        .then(function (data) {
          if (n !== my) return;
          var items = ((data.results || [])[0] || {}).conflicts || [];
          box.innerHTML = '';
          if (!items.length) {
            var p = doc.createElement('p');
            p.className = 'cc-muted mb-0';
            p.textContent = 'Пересечений не найдено.';
            box.appendChild(p);
            return;
          }
          var ul = doc.createElement('ul');
          ul.className = 'sch-conflicts';
          items.forEach(function (c) {
            var li = doc.createElement('li');
            li.className = 'sch-conflicts__item sch-conflicts__item--' + (c.severity || 'warning');
            li.textContent = c.text;
            ul.appendChild(li);
          });
          box.appendChild(ul);
        })
        .catch(function () { /* подсказка — не критично */ });
    }, 600);
    formEl.addEventListener('change', function (e) {
      var f = e.target.getAttribute('data-f');
      if (f === 'day') {
        var day = e.target.value === '' ? null : parseInt(e.target.value, 10);
        var sel = qs('[data-f="time"]', formEl);
        fillTime(sel, day, sel.value, CALL, CUSTOM);
      }
      if (f === 'time' || f === 'day') toggleCustom(formEl, CUSTOM);
      check();
    });
    formEl.addEventListener('input', function (e) { if (e.target.name === 'room' || e.target.name === 'time_custom') check(); });
    formEl.addEventListener('submit', function () {
      var sel = qs('[data-f="time"]', formEl);
      if (sel && sel.value === CUSTOM) ensureOption(sel, v('time_custom').trim());
    }, true);
    toggleCustom(formEl, CUSTOM);
  }

  /* ----------------------------- Массовые действия ----------------------------- */
  function initBulk(formEl) {
    var count = qs('[data-bulk-count]', formEl);
    var action = qs('[data-bulk-action]', formEl);
    function items() { return qsa('[data-bulk-item]'); }
    function update() {
      var all = items(), n = all.filter(function (i) { return i.checked; }).length;
      formEl.hidden = n === 0;
      if (count) count.textContent = n;
      all.forEach(function (i) { var tr = i.closest('tr'); if (tr) tr.classList.toggle('is-selected', i.checked); });
      qsa('[data-bulk-all]').forEach(function (box) {
        var own = qsa('[data-bulk-item]', box.closest('table'));
        var on = own.filter(function (i) { return i.checked; }).length;
        box.checked = own.length > 0 && on === own.length;
        box.indeterminate = on > 0 && on < own.length;
      });
    }
    function syncAction() {
      qsa('[data-bulk-for]', formEl).forEach(function (el) { el.hidden = el.getAttribute('data-bulk-for') !== action.value; });
    }
    doc.addEventListener('change', function (e) {
      var t = e.target;
      if (t.matches && t.matches('[data-bulk-all]')) {
        qsa('[data-bulk-item]', t.closest('table')).forEach(function (i) { i.checked = t.checked; });
        update();
      } else if (t.matches && t.matches('[data-bulk-item]')) {
        update();
      }
    });
    if (action) action.addEventListener('change', syncAction);
    var clear = qs('[data-bulk-clear]', formEl);
    if (clear) clear.addEventListener('click', function () { items().forEach(function (i) { i.checked = false; }); update(); });
    formEl.addEventListener('submit', function (e) {
      var n = items().filter(function (i) { return i.checked; }).length;
      if (!n) { e.preventDefault(); return; }
      if (action && action.value === 'delete' && CC.confirm) {
        e.preventDefault();
        CC.confirm('Удалить выбранные занятия (' + n + ')? Студенты получат одно уведомление.',
                   { title: 'Удаление занятий', okText: 'Удалить', danger: true })
          .then(function (ok) { if (ok) formEl.submit(); });
      }
    });
    syncAction();
    update();
  }

  onReady(function () {
    qsa('[data-editor]').forEach(initEditor);
    qsa('form[data-lesson-form]').forEach(initLessonForm);
    qsa('form[data-bulk-form]').forEach(initBulk);
    doc.addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('[data-print]') : null;
      if (b) { e.preventDefault(); window.print(); }
    });
  });
})();
