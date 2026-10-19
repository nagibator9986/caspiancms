/* ==========================================================================
   Caspian College — общий JS веб-админки (FOUNDATION). Без зависимостей
   (Bootstrap 5.3.3 bundle — опционально). Модульные скрипты — static/js/<модуль>.js.
   Глобальный объект: window.CC (см. tests/README.md → «JS API»).
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC = window.CC || {};
  var doc = document;

  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onReady(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function norm(s) { return String(s || '').toLowerCase().replace(/ё/g, 'е').trim(); }
  function debounce(fn, ms) {
    var t; return function () { var a = arguments, self = this; clearTimeout(t); t = setTimeout(function () { fn.apply(self, a); }, ms); };
  }
  CC.qs = qs; CC.qsa = qsa; CC.onReady = onReady; CC.debounce = debounce; CC.norm = norm;

  /* ----------------------------- localStorage (безопасно) ----------------------------- */
  CC.store = {
    get: function (k, d) { try { var v = window.localStorage.getItem(k); return v === null ? d : v; } catch (e) { return d; } },
    set: function (k, v) { try { window.localStorage.setItem(k, v); } catch (e) { /* приватный режим — не критично */ } }
  };

  /* ----------------------------- CSRF ----------------------------- */
  CC.csrfToken = function () { var m = qs('meta[name="csrf-token"]'); return m ? m.getAttribute('content') : ''; };

  function sameOrigin(url) {
    try { return new URL(url, window.location.href).origin === window.location.origin; } catch (e) { return false; }
  }
  function ensureCsrf(form) {
    if (!form || !form.getAttribute) return;
    var method = (form.getAttribute('method') || 'get').toLowerCase();
    if (method !== 'post') return;
    if (!sameOrigin(form.getAttribute('action') || window.location.href)) return;
    var token = CC.csrfToken();
    if (!token) return;
    var input = form.querySelector('input[name="csrf_token"]');
    if (!input) {
      input = doc.createElement('input');
      input.type = 'hidden'; input.name = 'csrf_token';
      form.appendChild(input);
    }
    input.value = token;
  }
  CC.ensureCsrf = ensureCsrf;
  // Формы, отправленные программно (form.submit()), не вызывают событие submit.
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () { ensureCsrf(this); return nativeSubmit.call(this); };
  doc.addEventListener('submit', function (e) { ensureCsrf(e.target); }, true);
  onReady(function () {
    qsa('form').forEach(ensureCsrf);
    if (window.MutationObserver) {
      new MutationObserver(function (muts) {
        muts.forEach(function (m) {
          Array.prototype.forEach.call(m.addedNodes || [], function (n) {
            if (n.nodeType !== 1) return;
            if (n.tagName === 'FORM') ensureCsrf(n);
            if (n.querySelectorAll) qsa('form', n).forEach(ensureCsrf);
          });
        });
      }).observe(doc.body, { childList: true, subtree: true });
    }
  });
  // fetch / XHR: заголовок X-CSRF-Token для изменяющих запросов на свой домен.
  if (window.fetch) {
    var nativeFetch = window.fetch;
    window.fetch = function (input, init) {
      init = init || {};
      var url = typeof input === 'string' ? input : (input && input.url) || '';
      var method = (init.method || (input && input.method) || 'GET').toUpperCase();
      if (method !== 'GET' && method !== 'HEAD' && sameOrigin(url)) {
        var headers = new Headers(init.headers || (typeof input !== 'string' && input.headers) || {});
        if (!headers.has('X-CSRF-Token')) headers.set('X-CSRF-Token', CC.csrfToken());
        init.headers = headers;
      }
      return nativeFetch.call(this, input, init);
    };
  }
  var xhrOpen = XMLHttpRequest.prototype.open, xhrSend = XMLHttpRequest.prototype.send;
  XMLHttpRequest.prototype.open = function (method, url) { this._ccMethod = method; this._ccUrl = url; return xhrOpen.apply(this, arguments); };
  XMLHttpRequest.prototype.send = function () {
    var m = String(this._ccMethod || 'GET').toUpperCase();
    if (m !== 'GET' && m !== 'HEAD' && sameOrigin(this._ccUrl || '')) {
      try { this.setRequestHeader('X-CSRF-Token', CC.csrfToken()); } catch (e) { /* уже отправлено */ }
    }
    return xhrSend.apply(this, arguments);
  };

  /** JSON-запрос к своему API: CC.fetchJSON('/api/x', {method:'POST', body:{a:1}}) → Promise<data>. */
  CC.fetchJSON = function (url, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: { 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var err = new Error((data && data.error) || ('HTTP ' + r.status)); err.status = r.status; err.data = data; throw err; }
        return data;
      });
    });
  };

  /* ----------------------------- Тосты ----------------------------- */
  var TOAST_ICONS = { success: 'check-circle-fill', danger: 'exclamation-octagon-fill', warning: 'exclamation-triangle-fill', info: 'info-circle-fill' };
  var TOAST_DELAY = { success: 5000, info: 6000, warning: 9000, danger: 0 };

  function normCategory(c) {
    c = String(c || 'info').toLowerCase();
    if (c === 'error' || c === 'danger') return 'danger';
    if (c === 'success' || c === 'warning') return c;
    return 'info';
  }
  function hideToast(el) {
    if (!el || el._ccHiding) return;
    el._ccHiding = true; el.classList.add('is-hiding');
    setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, 300);
  }
  function armToast(el) {
    var btn = qs('.cc-toast__close', el);
    if (btn) btn.addEventListener('click', function () { hideToast(el); });
    var cat = el.getAttribute('data-category') || 'info';
    var delay = TOAST_DELAY[cat];
    if (delay) {
      var t = setTimeout(function () { hideToast(el); }, delay);
      el.addEventListener('mouseenter', function () { clearTimeout(t); });
      el.addEventListener('mouseleave', function () { t = setTimeout(function () { hideToast(el); }, 2500); });
    }
  }
  /** Показать тост: CC.toast('Сохранено', 'success'). Категории: success | error | warning | info. */
  CC.toast = function (message, category) {
    var cat = normCategory(category);
    var stack = qs('#ccToasts');
    if (!stack) { stack = doc.createElement('div'); stack.id = 'ccToasts'; stack.className = 'cc-toasts'; stack.setAttribute('aria-live', 'polite'); doc.body.appendChild(stack); }
    var el = doc.createElement('div');
    el.className = 'cc-toast cc-toast--' + cat;
    el.setAttribute('role', cat === 'danger' ? 'alert' : 'status');
    el.setAttribute('data-category', cat);
    var icon = doc.createElement('i'); icon.className = 'cc-toast__icon bi bi-' + TOAST_ICONS[cat]; icon.setAttribute('aria-hidden', 'true');
    var text = doc.createElement('div'); text.className = 'cc-toast__text'; text.textContent = String(message);
    var close = doc.createElement('button'); close.type = 'button'; close.className = 'cc-toast__close'; close.setAttribute('aria-label', 'Закрыть');
    close.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
    el.appendChild(icon); el.appendChild(text); el.appendChild(close);
    stack.appendChild(el);
    armToast(el);
    return el;
  };
  onReady(function () { qsa('#ccToasts .cc-toast').forEach(armToast); });

  /* ----------------------------- Подтверждение ----------------------------- */
  /** CC.confirm('Удалить?', {title, okText, danger:true}) → Promise<boolean>. */
  CC.confirm = function (message, opts) {
    opts = opts || {};
    var modalEl = qs('#ccConfirm');
    if (!modalEl || !window.bootstrap) return Promise.resolve(window.confirm(message));
    return new Promise(function (resolve) {
      qs('[data-cc-confirm-title]', modalEl).textContent = opts.title || 'Подтвердите действие';
      qs('[data-cc-confirm-text]', modalEl).textContent = message || 'Вы уверены?';
      var ok = qs('[data-cc-confirm-ok]', modalEl);
      ok.textContent = opts.okText || 'Подтвердить';
      ok.className = 'btn ' + (opts.danger === false ? 'btn-primary' : 'btn-danger');
      var result = false;
      var modal = window.bootstrap.Modal.getOrCreateInstance(modalEl);
      function onOk() { result = true; modal.hide(); }
      function onHidden() {
        ok.removeEventListener('click', onOk);
        modalEl.removeEventListener('hidden.bs.modal', onHidden);
        resolve(result);
      }
      ok.addEventListener('click', onOk);
      modalEl.addEventListener('hidden.bs.modal', onHidden);
      modal.show();
      setTimeout(function () { ok.focus(); }, 200);
    });
  };
  function confirmOpts(el) {
    return { title: el.getAttribute('data-confirm-title') || undefined, okText: el.getAttribute('data-confirm-ok') || undefined,
             danger: el.getAttribute('data-confirm-variant') !== 'primary' };
  }
  // <form data-confirm="…"> — подтверждение перед отправкой.
  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute('data-confirm')) return;
    if (form._ccConfirmed) { form._ccConfirmed = false; return; }
    e.preventDefault();
    var submitter = e.submitter;
    CC.confirm(form.getAttribute('data-confirm'), confirmOpts(form)).then(function (ok) {
      if (!ok) return;
      form._ccConfirmed = true;
      if (submitter && form.requestSubmit) form.requestSubmit(submitter);
      else if (form.requestSubmit) form.requestSubmit();
      else { form._ccConfirmed = false; form.submit(); }
    });
  });
  // <a|button data-confirm="…"> вне формы с data-confirm.
  doc.addEventListener('click', function (e) {
    var el = e.target.closest ? e.target.closest('a[data-confirm], button[data-confirm]') : null;
    if (!el || el._ccConfirmed) { if (el) el._ccConfirmed = false; return; }
    e.preventDefault();
    CC.confirm(el.getAttribute('data-confirm'), confirmOpts(el)).then(function (ok) {
      if (!ok) return;
      if (el.tagName === 'A') { window.location.href = el.href; return; }
      el._ccConfirmed = true; el.click();
    });
  });

  /* ----------------------------- Тема ----------------------------- */
  CC.setTheme = function (theme) {
    theme = theme === 'dark' ? 'dark' : 'light';
    doc.documentElement.setAttribute('data-bs-theme', theme);
    CC.store.set('cc-theme', theme);
    qsa('[data-cc-theme-toggle] i').forEach(function (i) { i.className = 'bi ' + (theme === 'dark' ? 'bi-sun' : 'bi-moon-stars'); });
    doc.dispatchEvent(new CustomEvent('cc:theme', { detail: { theme: theme } }));
  };
  CC.theme = function () { return doc.documentElement.getAttribute('data-bs-theme') === 'dark' ? 'dark' : 'light'; };
  onReady(function () {
    qsa('[data-cc-theme-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () { CC.setTheme(CC.theme() === 'dark' ? 'light' : 'dark'); });
    });
    CC.setTheme(CC.theme());
  });

  /* ----------------------------- Сайдбар: группы ----------------------------- */
  onReady(function () {
    var closed = {};
    try { closed = JSON.parse(CC.store.get('cc-nav-closed', '{}')) || {}; } catch (e) { closed = {}; }
    qsa('[data-nav-group]').forEach(function (g) {
      var key = g.getAttribute('data-nav-group');
      var btn = qs('.cc-nav-group__toggle', g);
      var hasActive = !!qs('.cc-nav-link.active', g);
      if (closed[key] && !hasActive) g.classList.remove('is-open');
      btn.setAttribute('aria-expanded', g.classList.contains('is-open') ? 'true' : 'false');
      btn.addEventListener('click', function () {
        var open = !g.classList.contains('is-open');
        g.classList.toggle('is-open', open);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        if (open) delete closed[key]; else closed[key] = 1;
        CC.store.set('cc-nav-closed', JSON.stringify(closed));
      });
    });
    var active = qs('.cc-sidebar .cc-nav-link.active');
    if (active && active.scrollIntoView) { try { active.scrollIntoView({ block: 'nearest' }); } catch (e) { /* старые браузеры */ } }
  });

  /* ----------------------------- Поиск по разделам (Ctrl+K) ----------------------------- */
  onReady(function () {
    var modalEl = qs('#ccPalette');
    var dataEl = qs('#cc-nav-data');
    if (!modalEl || !dataEl) return;
    var items = [];
    try { items = JSON.parse(dataEl.textContent) || []; } catch (e) { items = []; }
    var input = qs('input', modalEl), list = qs('.cc-palette__list', modalEl);
    var current = [], active = 0;
    function render() {
      var q = norm(input.value), words = q.split(/\s+/).filter(Boolean);
      current = items.filter(function (it) {
        var hay = norm(it.label + ' ' + it.section + ' ' + (it.keywords || ''));
        return words.every(function (w) { return hay.indexOf(w) !== -1; });
      });
      active = Math.min(active, Math.max(current.length - 1, 0));
      list.innerHTML = '';
      if (!current.length) {
        var empty = doc.createElement('li'); empty.className = 'cc-palette__empty'; empty.textContent = 'Ничего не найдено';
        list.appendChild(empty); return;
      }
      current.forEach(function (it, i) {
        var li = doc.createElement('li');
        var a = doc.createElement('a');
        a.className = 'cc-palette__item' + (i === active ? ' is-active' : '');
        a.href = it.url; a.setAttribute('role', 'option');
        var ic = doc.createElement('i'); ic.className = 'bi bi-' + (it.icon || 'circle');
        var label = doc.createElement('span'); label.textContent = it.label;
        var sec = doc.createElement('small'); sec.textContent = it.section;
        a.appendChild(ic); a.appendChild(label); a.appendChild(sec);
        a.addEventListener('mousemove', function () { if (active !== i) { active = i; highlight(); } });
        li.appendChild(a); list.appendChild(li);
      });
    }
    function highlight() {
      qsa('.cc-palette__item', list).forEach(function (a, i) { a.classList.toggle('is-active', i === active); if (i === active && a.scrollIntoView) a.scrollIntoView({ block: 'nearest' }); });
    }
    function open() {
      if (!window.bootstrap) return;
      input.value = ''; active = 0; render();
      window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
    modalEl.addEventListener('shown.bs.modal', function () { input.focus(); });
    input.addEventListener('input', function () { active = 0; render(); });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, current.length - 1); highlight(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
      else if (e.key === 'Enter') { e.preventDefault(); if (current[active]) window.location.href = current[active].url; }
    });
    qsa('[data-cc-palette-open]').forEach(function (b) { b.addEventListener('click', open); });
    doc.addEventListener('keydown', function (e) {
      var typing = /^(INPUT|TEXTAREA|SELECT)$/.test((e.target && e.target.tagName) || '') || (e.target && e.target.isContentEditable);
      if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K' || e.key === 'л' || e.key === 'Л')) { e.preventDefault(); open(); }
      else if (e.key === '/' && !typing && !e.ctrlKey && !e.metaKey && !e.altKey) { e.preventDefault(); open(); }
    });
    CC.openPalette = open;
  });

  /* ----------------------------- Таблицы: поиск и сортировка ----------------------------- */
  // <input data-table-search="#tableId"> — фильтр строк tbody по тексту (debounce 150 мс).
  onReady(function () {
    qsa('[data-table-search]').forEach(function (input) {
      var table = qs(input.getAttribute('data-table-search'));
      if (!table) return;
      var rows = qsa('tbody tr', table);
      var emptyRow = qs('[data-table-empty]', table);
      var counter = input.getAttribute('data-table-count') ? qs(input.getAttribute('data-table-count')) : null;
      var run = debounce(function () {
        var words = norm(input.value).split(/\s+/).filter(Boolean), shown = 0;
        rows.forEach(function (tr) {
          if (tr === emptyRow) return;
          var hay = tr._ccText || (tr._ccText = norm(tr.getAttribute('data-search') || tr.textContent));
          var ok = words.every(function (w) { return hay.indexOf(w) !== -1; });
          tr.classList.toggle('is-hidden', !ok); if (ok) shown++;
        });
        if (emptyRow) emptyRow.classList.toggle('is-hidden', shown !== 0);
        if (counter) counter.textContent = shown;
      }, 150);
      input.addEventListener('input', run);
      if (input.value) run();
    });
    // <table data-sortable> <th data-sort="text|num|date"> — сортировка по клику (значение — data-value ячейки или текст).
    qsa('table[data-sortable]').forEach(function (table) {
      qsa('th[data-sort]', table).forEach(function (th) {
        th.setAttribute('tabindex', '0'); th.setAttribute('role', 'columnheader');
        function sort() {
          var idx = Array.prototype.indexOf.call(th.parentNode.children, th);
          var type = th.getAttribute('data-sort') || 'text';
          var dir = th.getAttribute('aria-sort') === 'ascending' ? -1 : 1;
          qsa('th[data-sort]', table).forEach(function (o) { o.removeAttribute('aria-sort'); });
          th.setAttribute('aria-sort', dir === 1 ? 'ascending' : 'descending');
          var tbody = table.tBodies[0];
          var rows = qsa('tr', tbody).filter(function (r) { return !r.hasAttribute('data-table-empty'); });
          function val(r) {
            var c = r.children[idx]; if (!c) return '';
            var v = c.getAttribute('data-value'); if (v === null) v = c.textContent.trim();
            if (type === 'num') { var n = parseFloat(String(v).replace(/[^\d.,-]/g, '').replace(',', '.')); return isNaN(n) ? -Infinity : n; }
            return norm(v);
          }
          rows.sort(function (a, b) { var x = val(a), y = val(b); return (x > y ? 1 : x < y ? -1 : 0) * dir; });
          rows.forEach(function (r) { tbody.appendChild(r); });
        }
        th.addEventListener('click', sort);
        th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
      });
    });
    // <tr data-href="/x"> — строка-ссылка (клик, кроме кнопок/ссылок внутри).
    doc.addEventListener('click', function (e) {
      var tr = e.target.closest ? e.target.closest('tr[data-href]') : null;
      if (!tr || e.target.closest('a, button, input, select, label, form')) return;
      window.location.href = tr.getAttribute('data-href');
    });
  });

  /* ----------------------------- Широкие таблицы старых шаблонов ----------------------------- */
  // Таблица без обёртки с горизонтальным скроллом растягивает всю страницу на телефоне —
  // оборачиваем её в .cc-table-wrap (только если ни один предок уже не скроллится по X).
  onReady(function () {
    var main = qs('#cc-main');
    if (!main) return;
    qsa('table', main).forEach(function (t) {
      if (t.closest('.cc-table-wrap, .table-responsive, [data-no-wrap]')) return;
      for (var el = t.parentElement; el && el !== main; el = el.parentElement) {
        var ox = getComputedStyle(el).overflowX;
        if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return;
      }
      var wrap = doc.createElement('div');
      wrap.className = 'cc-table-wrap';
      t.parentNode.insertBefore(wrap, t);
      wrap.appendChild(t);
    });
  });

  /* ----------------------------- Мелочи: копирование, автоотправка, пароль ----------------------------- */
  /** Копировать текст в буфер: CC.copy('текст') → Promise. */
  CC.copy = function (text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = doc.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      doc.body.appendChild(ta); ta.select();
      try { doc.execCommand('copy') ? resolve() : reject(new Error('copy')); } catch (e) { reject(e); } finally { doc.body.removeChild(ta); }
    });
  };
  onReady(function () {
    doc.addEventListener('click', function (e) {
      var el = e.target.closest ? e.target.closest('[data-copy]') : null;
      if (!el) return;
      e.preventDefault();
      CC.copy(el.getAttribute('data-copy')).then(function () { CC.toast(el.getAttribute('data-copy-message') || 'Скопировано', 'success'); },
        function () { CC.toast('Не удалось скопировать', 'error'); });
    });
    // <select data-autosubmit> / <input type=checkbox data-autosubmit> — отправить форму при изменении.
    doc.addEventListener('change', function (e) {
      var el = e.target;
      if (el && el.hasAttribute && el.hasAttribute('data-autosubmit') && el.form) {
        if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
      }
    });
    // Кнопка «показать пароль»: <button data-password-toggle="#id">.
    qsa('[data-password-toggle]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        var input = qs(btn.getAttribute('data-password-toggle'));
        if (!input) return;
        var show = input.type === 'password';
        input.type = show ? 'text' : 'password';
        btn.setAttribute('aria-pressed', show ? 'true' : 'false');
        btn.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
        var i = qs('i', btn); if (i) i.className = 'bi ' + (show ? 'bi-eye-slash' : 'bi-eye');
      });
    });
    // Защита от ухода с несохранённой формой: <form data-dirty-guard>.
    qsa('form[data-dirty-guard]').forEach(function (form) {
      var dirty = false;
      form.addEventListener('input', function () { dirty = true; });
      form.addEventListener('change', function () { dirty = true; });
      form.addEventListener('submit', function () { dirty = false; });
      window.addEventListener('beforeunload', function (e) { if (dirty) { e.preventDefault(); e.returnValue = ''; } });
    });
    // Кнопки с data-loading: блокировка повторной отправки формы.
    doc.addEventListener('submit', function (e) {
      if (e.defaultPrevented) return;
      var btn = e.submitter;
      if (btn && btn.hasAttribute('data-loading')) {
        setTimeout(function () {
          btn.disabled = true;
          btn.insertAdjacentHTML('afterbegin', '<span class="spinner-border spinner-border-sm" aria-hidden="true"></span>');
          var t = btn.getAttribute('data-loading'); if (t) { var s = btn.querySelector('span:not(.spinner-border)'); if (s) s.textContent = t; }
        }, 0);
      }
    });
  });

  /* ----------------------------- Загрузка фото (ui.image_uploader) ----------------------------- */
  // Хранит порядок в скрытом поле <name>__state (JSON), новые файлы — в <input type=file name=<name>>.
  // Сервер: cms.core.images_from_uploader(name, existing, folder).
  function initUploader(root) {
    if (root._ccUploader) return;
    root._ccUploader = true;
    var name = root.getAttribute('data-name');
    var max = parseInt(root.getAttribute('data-max') || '10', 10);
    var input = qs('[data-cc-uploader-input]', root);
    var stateEl = qs('[data-cc-uploader-state]', root);
    var grid = qs('[data-cc-uploader-grid]', root);
    var addTile = qs('.cc-uploader__add', root);
    var counter = qs('[data-cc-uploader-count]', root);
    var canDT = typeof window.DataTransfer === 'function';
    var items = [];
    try {
      var existing = JSON.parse((qs('[data-cc-uploader-existing]', root) || {}).textContent || '[]');
      existing.forEach(function (im) { if (im && im.url) items.push({ kind: 'existing', url: im.url, src: im.url }); });
    } catch (e) { /* пусто */ }

    function syncFiles() {
      if (!canDT) return;
      var dt = new DataTransfer();
      items.forEach(function (it) { if (it.kind === 'new') dt.items.add(it.file); });
      input.files = dt.files;
    }
    function syncState() {
      var n = 0;
      stateEl.value = JSON.stringify(items.map(function (it) {
        return it.kind === 'existing' ? { kind: 'existing', url: it.url } : { kind: 'new', idx: n++ };
      }));
      if (counter) counter.textContent = items.length + ' / ' + max;
      addTile.style.display = items.length >= max ? 'none' : '';
    }
    function move(from, to) {
      if (to < 0 || to >= items.length || from === to) return;
      var it = items.splice(from, 1)[0]; items.splice(to, 0, it); commit();
    }
    function commit() { syncFiles(); syncState(); render(); root.dispatchEvent(new CustomEvent('cc:uploader-change', { detail: { count: items.length } })); }
    function render() {
      qsa('.cc-uploader__item', grid).forEach(function (el) { el.parentNode.removeChild(el); });
      items.forEach(function (it, i) {
        var el = doc.createElement('div');
        el.className = 'cc-uploader__item'; el.draggable = true; el.setAttribute('data-index', i);
        var img = doc.createElement('img'); img.src = it.src; img.alt = 'Фото ' + (i + 1); img.decoding = 'async';
        el.appendChild(img);
        if (i === 0) { var c = doc.createElement('span'); c.className = 'cc-uploader__cover'; c.textContent = 'Обложка'; el.appendChild(c); }
        if (it.kind === 'new') { var nb = doc.createElement('span'); nb.className = 'cc-uploader__new'; nb.textContent = 'Новое'; el.appendChild(nb); }
        var rm = doc.createElement('button'); rm.type = 'button'; rm.className = 'cc-uploader__remove'; rm.setAttribute('aria-label', 'Убрать фото ' + (i + 1));
        rm.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
        rm.addEventListener('click', function () {
          var removed = items.splice(i, 1)[0];
          if (removed && removed.kind === 'new' && removed.src) { try { URL.revokeObjectURL(removed.src); } catch (e) { /* ок */ } }
          commit();
        });
        el.appendChild(rm);
        var mv = doc.createElement('div'); mv.className = 'cc-uploader__move';
        [['arrow-left', -1, 'Левее'], ['arrow-right', 1, 'Правее']].forEach(function (d) {
          var b = doc.createElement('button'); b.type = 'button'; b.setAttribute('aria-label', d[2]);
          b.innerHTML = '<i class="bi bi-' + d[0] + '" aria-hidden="true"></i>';
          b.disabled = (d[1] < 0 && i === 0) || (d[1] > 0 && i === items.length - 1);
          b.addEventListener('click', function () { move(i, i + d[1]); });
          mv.appendChild(b);
        });
        el.appendChild(mv);
        el.addEventListener('dragstart', function (e) { el.classList.add('is-dragging'); e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/cc-index', String(i)); });
        el.addEventListener('dragend', function () { el.classList.remove('is-dragging'); });
        el.addEventListener('dragover', function (e) { if (Array.prototype.indexOf.call(e.dataTransfer.types, 'text/cc-index') !== -1) { e.preventDefault(); el.classList.add('is-over'); } });
        el.addEventListener('dragleave', function () { el.classList.remove('is-over'); });
        el.addEventListener('drop', function (e) {
          var from = e.dataTransfer.getData('text/cc-index');
          if (from === '') return;
          e.preventDefault(); e.stopPropagation(); el.classList.remove('is-over');
          move(parseInt(from, 10), i);
        });
        grid.insertBefore(el, addTile);
      });
    }
    function addFiles(fileList) {
      var skipped = 0;
      Array.prototype.forEach.call(fileList || [], function (f) {
        if (!/^image\/(jpeg|png|webp|gif)$/.test(f.type)) { skipped++; return; }
        if (f.size > 10 * 1024 * 1024) { CC.toast('«' + f.name + '» больше 10 МБ', 'warning'); return; }
        if (items.length >= max) { skipped++; return; }
        items.push({ kind: 'new', file: f, src: URL.createObjectURL(f) });
      });
      if (skipped) CC.toast('Можно до ' + max + ' фото (JPG, PNG, WEBP, GIF)', 'warning');
      commit();
    }
    input.addEventListener('change', function () {
      var picked = Array.prototype.slice.call(input.files || []);
      if (!canDT) { // старый браузер: только выбранные сейчас файлы, без перестановки новых
        items = items.filter(function (it) { return it.kind === 'existing'; });
        picked.forEach(function (f) { if (items.length < max) items.push({ kind: 'new', file: f, src: URL.createObjectURL(f) }); });
        syncState(); render(); return;
      }
      addFiles(picked);
    });
    root.addEventListener('dragover', function (e) { if (Array.prototype.indexOf.call(e.dataTransfer.types, 'Files') !== -1) { e.preventDefault(); root.classList.add('is-dropping'); } });
    root.addEventListener('dragleave', function (e) { if (!root.contains(e.relatedTarget)) root.classList.remove('is-dropping'); });
    root.addEventListener('drop', function (e) {
      if (!e.dataTransfer.files || !e.dataTransfer.files.length) return;
      e.preventDefault(); root.classList.remove('is-dropping'); addFiles(e.dataTransfer.files);
    });
    if (input.form) input.form.addEventListener('submit', function () { syncFiles(); syncState(); });
    syncState(); render();
    root.ccUploader = { items: items, move: move };
  }
  CC.initUploader = initUploader;
  onReady(function () { qsa('[data-cc-uploader]').forEach(initUploader); });

  /* ----------------------------- Графики (Chart.js подключает модуль) ----------------------------- */
  function cssVar(name) { return getComputedStyle(doc.documentElement).getPropertyValue(name).trim(); }
  CC.cssVar = cssVar;
  /** Палитра серий графиков (в порядке использования), из CSS-токенов. */
  CC.chartColors = function () {
    return ['--cc-brand', '--cc-info', '--cc-success', '--cc-warning', '--cc-violet', '--cc-teal', '--cc-danger', '--cc-brand-light']
      .map(cssVar);
  };
  /** Общие настройки Chart.js: шрифт Montserrat, цвета текста/сетки по теме. Вызывать после подключения Chart.js. */
  CC.applyChartDefaults = function () {
    if (!window.Chart) return;
    var C = window.Chart;
    C.defaults.font.family = cssVar('--cc-font') || 'Montserrat, sans-serif';
    C.defaults.font.size = 12;
    C.defaults.color = cssVar('--cc-text-muted');
    C.defaults.borderColor = cssVar('--cc-border');
    C.defaults.plugins.legend.labels.usePointStyle = true;
    C.defaults.plugins.tooltip.backgroundColor = '#1A1C1E';
    C.defaults.plugins.tooltip.padding = 10;
    C.defaults.plugins.tooltip.cornerRadius = 8;
    C.defaults.maintainAspectRatio = false;
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) C.defaults.animation = false;
  };
  onReady(function () { CC.applyChartDefaults(); });

  /* ----------------------------- QR-коды (qrcode-generator подключает модуль) ----------------------------- */
  /** SVG-строка QR: CC.qrSvg('https://…/join/STAND1', {cellSize: 8, margin: 2, ecc: 'M'}). */
  CC.qrSvg = function (text, opts) {
    opts = opts || {};
    if (typeof window.qrcode !== 'function') throw new Error('qrcode-generator не подключён ({{ ui.lib("qrcode") }})');
    var qr = window.qrcode(0, opts.ecc || 'M');
    qr.addData(String(text)); qr.make();
    return qr.createSvgTag({ cellSize: opts.cellSize || 8, margin: opts.margin === undefined ? 2 : opts.margin, scalable: true });
  };
  /** SVG-строка → PNG Blob заданного размера (для скачивания/печати). */
  CC.svgToPng = function (svg, size) {
    size = size || 1024;
    return new Promise(function (resolve, reject) {
      var img = new Image();
      var url = URL.createObjectURL(new Blob([svg], { type: 'image/svg+xml' }));
      img.onload = function () {
        var c = doc.createElement('canvas'); c.width = size; c.height = size;
        var ctx = c.getContext('2d'); ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, size, size);
        ctx.imageSmoothingEnabled = false; ctx.drawImage(img, 0, 0, size, size);
        URL.revokeObjectURL(url);
        c.toBlob(function (b) { b ? resolve(b) : reject(new Error('toBlob')); }, 'image/png');
      };
      img.onerror = function (e) { URL.revokeObjectURL(url); reject(e); };
      img.src = url;
    });
  };
  /** Скачать Blob/строку/URL как файл: CC.download('qr.svg', svgString, 'image/svg+xml'). */
  CC.download = function (filename, data, type) {
    var url, revoke = false;
    if (data instanceof Blob) { url = URL.createObjectURL(data); revoke = true; }
    else if (/^(data:|blob:|https?:)/.test(String(data))) { url = String(data); }
    else { url = URL.createObjectURL(new Blob([String(data)], { type: type || 'text/plain;charset=utf-8' })); revoke = true; }
    var a = doc.createElement('a'); a.href = url; a.download = filename; doc.body.appendChild(a); a.click(); doc.body.removeChild(a);
    if (revoke) setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
  };
})();
