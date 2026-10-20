/* ==========================================================================
   Caspian College — общий JS веб-админки (FOUNDATION), версия 2 (UX_SPEC_v2 §4).
   Без зависимостей (Bootstrap 5.3.3 bundle — опционально). Модульные скрипты — static/js/<модуль>.js.
   Глобальный объект: window.CC (см. tests/README.md → «JS API»).
   Всё, что вешается на элементы, инициализируется через CC.init(root) — и при загрузке, и после
   вставки HTML (быстрый просмотр): document.dispatchEvent(new CustomEvent('cc:content-loaded', {detail: {root}})).
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
  function closest(el, sel) { return el && el.closest ? el.closest(sel) : null; }
  function isTyping(el) {
    if (!el) return false;
    return /^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName || '') || !!el.isContentEditable;
  }
  function isVisible(el) { return !!(el && (el.offsetWidth || el.offsetHeight || el.getClientRects().length)); }
  /** Склонение: CC.plural(3, ['новость', 'новости', 'новостей']) → 'новости'. */
  function plural(n, forms) {
    var a = Math.abs(n) % 100, b = a % 10;
    if (a > 10 && a < 20) return forms[2];
    if (b > 1 && b < 5) return forms[1];
    if (b === 1) return forms[0];
    return forms[2];
  }
  CC.qs = qs; CC.qsa = qsa; CC.onReady = onReady; CC.debounce = debounce; CC.norm = norm; CC.plural = plural;
  var IS_MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent || '');

  /* ----------------------------- localStorage / sessionStorage (безопасно) ----------------------------- */
  function makeStore(kind) {
    function s() { try { return window[kind]; } catch (e) { return null; } }
    return {
      get: function (k, d) { try { var v = s().getItem(k); return v === null ? d : v; } catch (e) { return d; } },
      set: function (k, v) { try { s().setItem(k, v); } catch (e) { /* приватный режим — не критично */ } },
      del: function (k) { try { s().removeItem(k); } catch (e) { /* не критично */ } }
    };
  }
  CC.store = makeStore('localStorage');
  CC.session = makeStore('sessionStorage');

  /* ----------------------------- Инициализация виджетов (CC.init) ----------------------------- */
  var initializers = [];
  /** Зарегистрировать инициализатор: CC.onInit(function (root) { … }) — вызывается для документа и для вставленного HTML. */
  CC.onInit = function (fn) { initializers.push(fn); if (CC._booted) { try { fn(doc); } catch (e) { console.error(e); } } };
  CC.init = function (root) {
    root = root || doc;
    initializers.forEach(function (fn) { try { fn(root); } catch (e) { if (window.console) console.error(e); } });
  };
  onReady(function () { CC._booted = true; CC.init(doc); });
  doc.addEventListener('cc:content-loaded', function (e) { CC.init((e.detail && e.detail.root) || doc); });

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
  // Формы, отправленные программно (form.submit()), не вызывают событие submit. Такая отправка — после своей
  // проверки/подтверждения модуля — уводит со страницы: «несохранённые изменения» снимаем (иначе — лишний вопрос).
  var nativeSubmit = HTMLFormElement.prototype.submit;
  HTMLFormElement.prototype.submit = function () {
    ensureCsrf(this);
    if (this._ccDirtyInit && CC.markClean && (!this.target || this.target === '_self')) CC.markClean(this);
    return nativeSubmit.call(this);
  };
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

  /** JSON-запрос к своему API: CC.fetchJSON('/api/x', {method:'POST', body:{a:1}, signal}) → Promise<data>. */
  CC.fetchJSON = function (url, opts) {
    opts = opts || {};
    var init = { method: opts.method || 'GET', headers: { 'Accept': 'application/json' }, credentials: 'same-origin' };
    if (opts.signal) init.signal = opts.signal;
    if (opts.body !== undefined) { init.headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(opts.body); }
    return fetch(url, init).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (data) {
        if (!r.ok) { var err = new Error((data && data.error) || ('HTTP ' + r.status)); err.status = r.status; err.data = data; throw err; }
        return data;
      });
    });
  };

  /* ----------------------------- Тосты (§4.10) ----------------------------- */
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
    if (el._ccArmed) return; el._ccArmed = true;
    var btn = qs('.cc-toast__close', el);
    if (btn) btn.addEventListener('click', function () { hideToast(el); });
    var cat = el.getAttribute('data-category') || 'info';
    var delay = TOAST_DELAY[cat];
    if (delay && el.hasAttribute('data-has-action')) delay = Math.max(delay, 8000);
    if (delay) {
      var t = setTimeout(function () { hideToast(el); }, delay);
      el.addEventListener('mouseenter', function () { clearTimeout(t); });
      el.addEventListener('focusin', function () { clearTimeout(t); });
      el.addEventListener('mouseleave', function () { clearTimeout(t); t = setTimeout(function () { hideToast(el); }, 3000); });
    }
  }
  /** Показать тост: CC.toast('Сохранено', 'success'); категории success | error | warning | info.
   *  С действием: CC.toast('Закрыто', 'success', {action: {label: 'Отменить', post: '/x', fields: {a: 1}}})
   *  или {action: {label: 'Открыть', href: '/news'}} или {action: {label: 'Отменить', onClick: fn}}. */
  CC.toast = function (message, category, opts) {
    opts = opts || {};
    var cat = normCategory(category);
    var stack = qs('#ccToasts');
    if (!stack) { stack = doc.createElement('div'); stack.id = 'ccToasts'; stack.className = 'cc-toasts'; stack.setAttribute('aria-live', 'polite'); doc.body.appendChild(stack); }
    var el = doc.createElement('div');
    el.className = 'cc-toast cc-toast--' + cat;
    el.setAttribute('role', cat === 'danger' ? 'alert' : 'status');
    el.setAttribute('data-category', cat);
    var icon = doc.createElement('i'); icon.className = 'cc-toast__icon bi bi-' + TOAST_ICONS[cat]; icon.setAttribute('aria-hidden', 'true');
    var text = doc.createElement('div'); text.className = 'cc-toast__text'; text.textContent = String(message);
    el.appendChild(icon); el.appendChild(text);
    var a = opts.action;
    if (a && a.label) {
      el.setAttribute('data-has-action', '');
      var act;
      if (a.post) {
        var form = doc.createElement('form'); form.method = 'post'; form.action = a.post; form.className = 'cc-toast__form';
        Object.keys(a.fields || {}).forEach(function (k) {
          var h = doc.createElement('input'); h.type = 'hidden'; h.name = k; h.value = a.fields[k] == null ? '' : String(a.fields[k]); form.appendChild(h);
        });
        act = doc.createElement('button'); act.type = 'submit'; act.className = 'cc-toast__action'; act.textContent = a.label;
        form.appendChild(act); el.appendChild(form); ensureCsrf(form);
      } else if (a.href) {
        act = doc.createElement('a'); act.className = 'cc-toast__action'; act.href = a.href; act.textContent = a.label; el.appendChild(act);
      } else {
        act = doc.createElement('button'); act.type = 'button'; act.className = 'cc-toast__action'; act.textContent = a.label;
        act.addEventListener('click', function () { hideToast(el); if (typeof a.onClick === 'function') a.onClick(); });
        el.appendChild(act);
      }
    }
    var close = doc.createElement('button'); close.type = 'button'; close.className = 'cc-toast__close'; close.setAttribute('aria-label', 'Закрыть');
    close.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
    el.appendChild(close);
    stack.appendChild(el);
    armToast(el);
    return el;
  };
  onReady(function () { qsa('#ccToasts .cc-toast').forEach(armToast); });

  /* ----------------------------- Подтверждение (§4.11) ----------------------------- */
  /** CC.confirm('Удалить?', {title, okText, danger:true}) → Promise<boolean>. */
  CC.confirm = function (message, opts) {
    opts = opts || {};
    var modalEl = qs('#ccConfirm');
    if (!modalEl || !window.bootstrap) return Promise.resolve(window.confirm(message));
    return new Promise(function (resolve) {
      qs('[data-cc-confirm-title]', modalEl).textContent = opts.title || (opts.danger === false ? 'Подтвердите действие' : 'Подтвердите удаление');
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
    var title = el.getAttribute('data-confirm-title');
    return { title: title || undefined, okText: el.getAttribute('data-confirm-ok') || undefined,
             danger: el.getAttribute('data-confirm-variant') !== 'primary' };
  }
  // {n} → число выбранных строк, {items} → «3 новости» (кнопки панели массовых действий).
  function confirmText(el) {
    var msg = el.getAttribute('data-confirm') || '';
    if (msg.indexOf('{n}') === -1 && msg.indexOf('{items}') === -1) return msg;
    var bar = closest(el, '[data-cc-bulkbar]');
    var fid = el.getAttribute('form') || (bar && bar.getAttribute('data-cc-bulkbar')) || (el.form && el.form.id) || '';
    var n = fid ? selectedBoxes(fid).length : 0;
    bar = bar || (fid && qs('[data-cc-bulkbar="' + fid + '"]'));
    var forms = ((bar && bar.getAttribute('data-noun')) || 'элемент|элемента|элементов').split('|');
    return msg.replace(/\{n\}/g, String(n)).replace(/\{items\}/g, n + ' ' + plural(n, forms));
  }
  // <form data-confirm="…"> — подтверждение перед отправкой.
  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form.hasAttribute || !form.hasAttribute('data-confirm')) return;
    if (form._ccConfirmed) { form._ccConfirmed = false; return; }
    e.preventDefault();
    var submitter = e.submitter;
    CC.confirm(confirmText(form), confirmOpts(form)).then(function (ok) {
      if (!ok) return;
      form._ccConfirmed = true;
      if (submitter && form.requestSubmit) form.requestSubmit(submitter);
      else if (form.requestSubmit) form.requestSubmit();
      else { form._ccConfirmed = false; form.submit(); }
    });
  });
  // <a|button data-confirm="…"> вне формы с data-confirm.
  doc.addEventListener('click', function (e) {
    var el = closest(e.target, 'a[data-confirm], button[data-confirm]');
    if (!el || el._ccConfirmed) { if (el) el._ccConfirmed = false; return; }
    e.preventDefault();
    CC.confirm(confirmText(el), confirmOpts(el)).then(function (ok) {
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
    if (IS_MAC) qsa('kbd[data-cc-mod]').forEach(function (k) { if (k.textContent === 'Ctrl') k.textContent = '⌘'; });
  });

  /* ----------------------------- Сайдбар: группы (§2.2) ----------------------------- */
  // Начальное состояние ставит inline-скрипт в base.html (без мигания); здесь — клики и запоминание.
  function navState() { try { return JSON.parse(CC.store.get('cc-nav', '{}')) || {}; } catch (e) { return {}; } }
  onReady(function () {
    qsa('#ccSidebar [data-nav-group]').forEach(function (g) {
      var key = g.getAttribute('data-nav-group');
      var btn = qs('.cc-nav-group__toggle', g);
      if (!btn) return;
      btn.setAttribute('aria-expanded', g.classList.contains('is-open') ? 'true' : 'false');
      btn.addEventListener('click', function () {
        var open = !g.classList.contains('is-open');
        g.classList.toggle('is-open', open);
        btn.setAttribute('aria-expanded', open ? 'true' : 'false');
        var st = navState(); st[key] = open ? 'open' : 'closed';
        CC.store.set('cc-nav', JSON.stringify(st));
        // раскрыли группу внизу меню (Абитуриенты, Caspian Coin) — докручиваем, чтобы пункты не остались за краем
        if (open && g.scrollIntoView) {
          var reduce = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
          g.scrollIntoView({ block: 'nearest', behavior: reduce ? 'auto' : 'smooth' });
        }
      });
    });
    var nav = qs('.cc-sidebar__nav'), active = qs('.cc-sidebar .cc-nav-link.active');
    if (nav && active) {
      var top = active.offsetTop - nav.offsetTop;
      if (top < nav.scrollTop || top + active.offsetHeight > nav.scrollTop + nav.clientHeight) nav.scrollTop = Math.max(0, top - nav.clientHeight / 2);
    }
    if (nav) {   // ниже есть пункты (Абитуриенты, Caspian Coin) — маска внизу
      var more = function () { nav.classList.toggle('is-more-below', nav.scrollHeight - nav.clientHeight - nav.scrollTop > 4); };
      more();
      nav.addEventListener('scroll', debounce(more, 50), { passive: true });
      window.addEventListener('resize', debounce(more, 150));
      qsa('.cc-nav-group__toggle', nav).forEach(function (b) { b.addEventListener('click', function () { setTimeout(more, 0); }); });
    }
  });

  /* ----------------------------- Полоса прогресса (§4.1) ----------------------------- */
  CC.progress = (function () {
    var timer = null, guard = null;
    function el() { return qs('#ccProgress'); }
    return {
      start: function () {
        if (timer || !el()) return;
        timer = setTimeout(function () { var e = el(); if (e) e.classList.add('is-active'); }, 300);
        guard = setTimeout(CC.progress.done, 12000);   // скачивание файла не уводит со страницы
      },
      done: function () {
        clearTimeout(timer); clearTimeout(guard); timer = null;
        var e = el(); if (e) e.classList.remove('is-active');
      }
    };
  })();
  window.addEventListener('pageshow', function () { CC.progress.done(); });
  window.addEventListener('submit', function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f || f.target === '_blank' || f.hasAttribute('data-cc-no-progress')) return;
    CC.progress.start();
  });
  doc.addEventListener('click', function (e) {
    if (e.defaultPrevented || e.button !== 0 || e.ctrlKey || e.metaKey || e.shiftKey || e.altKey) return;
    var a = closest(e.target, 'a[href]');
    if (!a || a.target || a.hasAttribute('download') || a.hasAttribute('data-cc-peek') || a.hasAttribute('data-bs-toggle')) return;
    var href = a.getAttribute('href') || '';
    if (!href || href.charAt(0) === '#' || /^(javascript|mailto|tel):/i.test(href) || !sameOrigin(a.href)) return;
    if (/[?&]format=/.test(href) || /\/export/.test(href)) return;
    if (a.pathname === location.pathname && a.search === location.search && a.hash) return;
    CC.progress.start();
  });

  /* ----------------------------- Горячие клавиши (§4.13) ----------------------------- */
  var hotkeys = [];
  /** Сочетание модуля: CC.hotkey('BracketLeft', fn, 'Предыдущая группа') — по e.code (работает в RU-раскладке),
   *  вне полей ввода; попадает в окно «?». opts: {shift: true, group: 'Расписание', preventDefault: false}.
   *  preventDefault: false — действие клавиши по умолчанию не отменяется (стрелки прокручивают страницу и таблицы);
   *  обработчик fn(e) сам вызывает e.preventDefault(), если сработал. */
  CC.hotkey = function (code, fn, description, opts) {
    opts = opts || {};
    hotkeys.push({ code: code, fn: fn, desc: description || code, shift: !!opts.shift, group: opts.group || 'На этой странице',
                   keepDefault: opts.preventDefault === false });
  };
  var KEY_LABELS = { BracketLeft: '[', BracketRight: ']', ArrowLeft: '←', ArrowRight: '→', ArrowUp: '↑', ArrowDown: '↓', Slash: '/', Period: '.', Comma: ',', Escape: 'Esc', Enter: 'Enter', Space: 'Пробел' };
  function keyLabel(code) { return KEY_LABELS[code] || String(code).replace(/^Key/, '').replace(/^Digit/, ''); }
  function kbdHtml(keys) {
    return keys.map(function (k) { var e = doc.createElement('kbd'); e.textContent = k; return e.outerHTML; }).join(' ');
  }
  CC.showHotkeys = function () {
    var modalEl = qs('#ccHotkeys'), list = modalEl && qs('[data-cc-hotkeys-list]', modalEl);
    if (!modalEl || !list || !window.bootstrap) return;
    var mod = IS_MAC ? '⌘' : 'Ctrl';
    var rows = [
      ['Везде', [[[mod, 'K'], 'Поиск: группы, преподаватели, люди, разделы'], [['/'], 'Поиск на странице (или общий поиск)'],
        [['n'], 'Создать (основная кнопка страницы)'], [['?'], 'Эта справка'], [['Esc'], 'Закрыть окно, панель; снять выделение строк']]],
      ['Формы', [[[mod, 'S'], 'Сохранить форму'], [[mod, 'Enter'], 'Отправить ответ или заметку']]],
      ['Списки', [[['Shift', 'клик'], 'Выделить диапазон строк'], [[mod, 'клик'], 'Открыть строку в новой вкладке']]]
    ];
    var groups = {};
    hotkeys.forEach(function (h) { (groups[h.group] = groups[h.group] || []).push([(h.shift ? ['Shift'] : []).concat([keyLabel(h.code)]), h.desc]); });
    Object.keys(groups).forEach(function (g) { rows.push([g, groups[g]]); });
    list.innerHTML = '';
    rows.forEach(function (grp) {
      var title = doc.createElement('div'); title.className = 'cc-hotkeys__group'; title.textContent = grp[0]; list.appendChild(title);
      grp[1].forEach(function (r) {
        var dt = doc.createElement('dt'); dt.innerHTML = kbdHtml(r[0]);
        var dd = doc.createElement('dd'); dd.textContent = r[1];
        list.appendChild(dt); list.appendChild(dd);
      });
    });
    window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
  };
  function anyOverlayOpen() { return !!qs('.modal.show, .offcanvas.show, .offcanvas-md.show, .dropdown-menu.show'); }
  function firstVisible(sel) { return qsa(sel).filter(isVisible)[0] || null; }

  doc.addEventListener('keydown', function (e) {
    var t = e.target, typing = isTyping(t), mod = e.ctrlKey || e.metaKey;
    if (mod && !e.altKey && e.code === 'KeyK') { e.preventDefault(); if (CC.openPalette) CC.openPalette(); return; }
    if (mod && !e.altKey && e.code === 'KeyS') { if (saveHotkey()) e.preventDefault(); return; }
    if (mod && e.key === 'Enter' && t && t.matches && t.matches('textarea[data-cc-ctrl-enter], [data-cc-ctrl-enter] textarea')) {
      var f = t.form || closest(t, 'form');
      if (f) { e.preventDefault(); submitForm(f); }
      return;
    }
    if (e.key === 'Escape') {
      // модалка, открытая с клавиатуры, ещё не получила фокус — Bootstrap не увидит Esc; закрываем сами
      var openModal = qs('.modal.show');
      if (openModal && window.bootstrap && !openModal.contains(doc.activeElement) && openModal.getAttribute('data-bs-keyboard') !== 'false') {
        e.preventDefault(); window.bootstrap.Modal.getOrCreateInstance(openModal).hide(); return;
      }
      if (!typing && !anyOverlayOpen() && clearAllSelections()) e.preventDefault();
      return;
    }
    if (typing || mod || e.altKey || anyOverlayOpen()) return;
    if (e.key === '?') { e.preventDefault(); CC.showHotkeys(); return; }
    if (e.code === 'Slash' && !e.shiftKey) {
      e.preventDefault();
      var s = firstVisible('[data-cc-search]');
      if (s) { s.focus(); if (s.select) s.select(); } else if (CC.openPalette) CC.openPalette();
      return;
    }
    if (e.code === 'KeyN' && !e.shiftKey) {
      var n = firstVisible('[data-cc-hotkey="n"]');
      if (n) { e.preventDefault(); n.click(); }
      return;
    }
    for (var i = 0; i < hotkeys.length; i++) {
      var h = hotkeys[i];
      if (h.code === e.code && h.shift === e.shiftKey) { if (!h.keepDefault) e.preventDefault(); h.fn(e); return; }
    }
  });

  /* ----------------------------- Поиск Ctrl+K: разделы, сущности, команды (§2.5) ----------------------------- */
  onReady(function () {
    var modalEl = qs('#ccPalette');
    var dataEl = qs('#cc-nav-data');
    if (!modalEl || !dataEl) return;
    var nav = [];
    try { nav = JSON.parse(dataEl.textContent) || []; } catch (e) { nav = []; }
    var input = qs('input', modalEl), list = qs('.cc-palette__list', modalEl);
    var current = [], active = 0, remote = [], remoteFor = '', loading = false, ctrl = null;
    var ORDER = ['Разделы', 'Группы', 'Преподаватели', 'Аудитории', 'Люди', 'Новости и опросы', 'Команды'];

    function match(it, words) {
      var hay = it._hay || (it._hay = norm(it.label + ' ' + (it.section || '') + ' ' + (it.keywords || '')));
      return words.every(function (w) { return hay.indexOf(w) !== -1; });
    }
    function local(q) {
      var words = norm(q).split(/\s+/).filter(Boolean);
      var sections = [], commands = [];
      nav.forEach(function (it) {
        if (words.length && !match(it, words)) return;
        var item = { label: it.label, url: it.url, icon: it.icon, side: it.section };
        if (it.kind === 'command') commands.push(item); else sections.push(item);
      });
      return { sections: sections.slice(0, words.length ? 8 : 40), commands: commands.slice(0, 6) };
    }
    function render() {
      var q = input.value.trim();
      var loc = local(q), groups = {};
      groups['Разделы'] = loc.sections;
      groups['Команды'] = loc.commands;
      if (q.length >= 2 && remoteFor === q) {
        remote.forEach(function (r) {
          var g = r.section || 'Другое';
          (groups[g] = groups[g] || []).push({ label: r.label, sub: r.sub, url: r.url, icon: r.icon, actions: r.actions || [] });
        });
      }
      current = []; list.innerHTML = '';
      var names = ORDER.concat(Object.keys(groups).filter(function (g) { return ORDER.indexOf(g) === -1; }));
      names.forEach(function (g) {
        var items = groups[g];
        if (!items || !items.length) return;
        var head = doc.createElement('li'); head.className = 'cc-palette__group'; head.textContent = g; head.setAttribute('role', 'presentation');
        list.appendChild(head);
        items.forEach(function (it) {
          var i = current.length; current.push(it);
          var li = doc.createElement('li'); li.setAttribute('role', 'presentation');
          var a = doc.createElement('a');
          a.className = 'cc-palette__item' + (i === active ? ' is-active' : '');
          a.href = it.url; a.setAttribute('role', 'option'); a.id = 'ccPal' + i;
          var ic = doc.createElement('i'); ic.className = 'bi bi-' + (it.icon || 'circle'); ic.setAttribute('aria-hidden', 'true');
          var lab = doc.createElement('span'); lab.className = 'cc-palette__label';
          var b = doc.createElement('b'); b.textContent = it.label; lab.appendChild(b);
          if (it.sub) { var sm = doc.createElement('small'); sm.textContent = it.sub; lab.appendChild(sm); }
          a.appendChild(ic); a.appendChild(lab);
          if (it.actions && it.actions.length) {
            var hint = doc.createElement('span'); hint.className = 'cc-palette__hint'; hint.textContent = 'Shift+Enter — ' + it.actions[0].label;
            a.appendChild(hint);
          }
          if (it.side) { var side = doc.createElement('small'); side.textContent = it.side; a.appendChild(side); }
          a.addEventListener('mousemove', function () { if (active !== i) { active = i; highlight(); } });
          li.appendChild(a); list.appendChild(li);
        });
      });
      if (loading) {
        var ld = doc.createElement('li'); ld.className = 'cc-palette__loading'; ld.textContent = 'Ищем группы, преподавателей, людей…'; list.appendChild(ld);
      }
      if (!current.length && !loading) {
        var empty = doc.createElement('li'); empty.className = 'cc-palette__empty';
        empty.textContent = q.length >= 2 ? 'Ничего не найдено по «' + q + '»' : 'Ничего не найдено';
        list.appendChild(empty);
      }
      active = Math.min(active, Math.max(current.length - 1, 0));
      highlight();
    }
    function highlight() {
      qsa('.cc-palette__item', list).forEach(function (a, i) {
        a.classList.toggle('is-active', i === active);
        if (i === active) { input.setAttribute('aria-activedescendant', a.id); if (a.scrollIntoView) a.scrollIntoView({ block: 'nearest' }); }
      });
    }
    var fetchRemote = debounce(function () {
      var q = input.value.trim();
      if (q.length < 2) { loading = false; render(); return; }
      if (ctrl && ctrl.abort) ctrl.abort();
      ctrl = window.AbortController ? new AbortController() : null;
      CC.fetchJSON('/api/search?q=' + encodeURIComponent(q), { signal: ctrl ? ctrl.signal : undefined }).then(function (data) {
        if (input.value.trim() !== q) return;
        remote = (data && data.results) || []; remoteFor = q; loading = false; render();
      }).catch(function (err) {
        if (err && err.name === 'AbortError') return;
        loading = false; remote = []; remoteFor = q; render();
      });
    }, 200);
    function open() {
      if (!window.bootstrap) return;
      input.value = ''; active = 0; remote = []; remoteFor = ''; loading = false; render();
      window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
    }
    function go(it, useAction) {
      if (!it) return;
      var url = useAction && it.actions && it.actions.length ? it.actions[0].url : it.url;
      CC.progress.start();
      window.location.href = url;
    }
    modalEl.addEventListener('shown.bs.modal', function () { input.focus(); });
    input.addEventListener('input', function () {
      active = 0;
      var q = input.value.trim();
      loading = q.length >= 2 && remoteFor !== q;
      render();
      if (loading) fetchRemote();
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(active + 1, current.length - 1); highlight(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(active - 1, 0); highlight(); }
      else if (e.key === 'Enter') { e.preventDefault(); go(current[active], e.shiftKey); }
    });
    qsa('[data-cc-palette-open]').forEach(function (b) { b.addEventListener('click', open); });
    CC.openPalette = open;
  });

  /* ----------------------------- Таблицы: поиск, сортировка, строка-ссылка (§4.5) ----------------------------- */
  // <input data-table-search="#tableId"> — фильтр строк tbody по тексту (debounce 150 мс).
  CC.onInit(function (root) {
    qsa('[data-table-search]', root).forEach(function (input) {
      if (input._ccTableSearch) return; input._ccTableSearch = true;
      var table = qs(input.getAttribute('data-table-search'));
      if (!table) return;
      var emptyRow = qs('[data-table-empty]', table);
      var counter = input.getAttribute('data-table-count') ? qs(input.getAttribute('data-table-count')) : null;
      var run = debounce(function () {
        var words = norm(input.value).split(/\s+/).filter(Boolean), shown = 0;
        qsa('tbody tr', table).forEach(function (tr) {
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
    qsa('table[data-sortable]', root).forEach(function (table) {
      if (table._ccSortable) return; table._ccSortable = true;
      qsa('th[data-sort]', table).forEach(function (th) {
        th.setAttribute('tabindex', '0');
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
          var empty = qs('[data-table-empty]', tbody); if (empty) tbody.appendChild(empty);
        }
        th.addEventListener('click', sort);
        th.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); sort(); } });
      });
    });
  });
  // <tr data-href="/x"> — строка-ссылка. Ctrl/⌘/Shift-клик и средняя кнопка — новая вкладка;
  // ссылка .cc-row-link с data-cc-peek — быстрый просмотр. Кнопки, ссылки, чекбоксы и меню строку не открывают.
  function rowTarget(e) {
    var tr = closest(e.target, 'tr[data-href]');
    if (!tr) return null;
    if (closest(e.target, 'a, button, input, select, textarea, label, form, .dropdown-menu, .dropdown, [data-no-row-link]')) return null;
    var sel = window.getSelection ? String(window.getSelection()) : '';
    if (sel.length > 2) return null;   // выделяют текст — не переходим
    return tr;
  }
  doc.addEventListener('click', function (e) {
    var cell = closest(e.target, 'td.cc-select');
    if (cell && !closest(e.target, 'input, label')) { var cb = qs('input[type="checkbox"]', cell); if (cb) { cb.click(); return; } }
    var tr = rowTarget(e);
    if (!tr) return;
    var href = tr.getAttribute('data-href');
    if (e.ctrlKey || e.metaKey || e.shiftKey) { window.open(href, '_blank', 'noopener'); return; }
    var peek = qs('a.cc-row-link[data-cc-peek]', tr);
    if (peek && canPeek(e)) { CC.peek(peek.href); return; }
    CC.progress.start();
    window.location.href = href;
  });
  doc.addEventListener('auxclick', function (e) {
    if (e.button !== 1) return;
    var tr = rowTarget(e);
    if (tr) { e.preventDefault(); window.open(tr.getAttribute('data-href'), '_blank', 'noopener'); }
  });

  /* ----------------------------- Широкие таблицы и колонки по ширине (§4.5.4) ----------------------------- */
  // Таблица без обёртки с горизонтальным скроллом растягивает всю страницу на телефоне —
  // оборачиваем её в .cc-table-wrap (только если ни один предок уже не скроллится по X).
  CC.onInit(function (root) {
    var main = qs('#cc-main');
    if (!main || (root !== doc && !main.contains(root))) return;
    qsa('table', root === doc ? main : root).forEach(function (t) {
      if (t.closest('.cc-table-wrap, .table-responsive, [data-no-wrap]')) return;
      for (var el = t.parentElement; el && el !== main && el !== root; el = el.parentElement) {
        var ox = getComputedStyle(el).overflowX;
        if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return;
      }
      var wrap = doc.createElement('div');
      wrap.className = 'cc-table-wrap';
      t.parentNode.insertBefore(wrap, t);
      wrap.appendChild(t);
    });
  });
  // .cc-col-xl / -lg / -md скрываются по ширине обёртки таблицы (< 1100 / 900 / 700 px), а не окна.
  function sizeWrap(wrap) {
    var w = wrap.clientWidth;
    var stacked = window.innerWidth <= 575 && !!qs('.cc-table--stack', wrap);
    wrap.classList.toggle('cc-lt-1100', !stacked && w < 1100);
    wrap.classList.toggle('cc-lt-900', !stacked && w < 900);
    wrap.classList.toggle('cc-lt-700', !stacked && w < 700);
  }
  var wrapObserver = window.ResizeObserver ? new ResizeObserver(function (entries) { entries.forEach(function (en) { sizeWrap(en.target); }); }) : null;
  CC.onInit(function (root) {
    qsa('.cc-table-wrap', root).forEach(function (w) {
      if (!qs('.cc-col-xl, .cc-col-lg, .cc-col-md', w)) return;
      sizeWrap(w);
      if (wrapObserver && !w._ccObserved) { w._ccObserved = true; wrapObserver.observe(w); }
    });
  });
  window.addEventListener('resize', debounce(function () { qsa('.cc-table-wrap').forEach(function (w) { if (qs('.cc-col-xl, .cc-col-lg, .cc-col-md', w)) sizeWrap(w); }); }, 150));
  // Строки-карточки (.cc-table--stack): мета-ячейка или ячейка с подписью, где одни пробелы, — пустая
  // (.cc-cell-empty: в карточке на телефоне не видна и не даёт лишний разделитель « · »).
  CC.onInit(function (root) {
    qsa('.cc-table--stack td.cc-cell-meta, .cc-table--stack td[data-label]', root).forEach(function (td) {
      if (!td.firstElementChild && !td.textContent.trim()) td.classList.add('cc-cell-empty');
    });
  });

  /* ----------------------------- Выбор строк и массовые действия (§4.5.5) ----------------------------- */
  function selectBoxes(fid) {
    return qsa('input[data-cc-select="' + fid + '"]').filter(function (b) { return !b.disabled && !closest(b, 'tr.is-hidden, .is-hidden'); });
  }
  function selectedBoxes(fid) { return qsa('input[data-cc-select="' + fid + '"]').filter(function (b) { return b.checked; }); }
  function updateBottomBar() {
    var on = !!qs('.cc-bulkbar.is-visible') || !!qsa('.cc-savebar').filter(isVisible).length;
    doc.body.classList.toggle('has-bottom-bar', on);
  }
  function updateBulk(fid) {
    var all = qsa('input[data-cc-select="' + fid + '"]');
    var visible = selectBoxes(fid);
    var n = 0;
    all.forEach(function (b) {
      if (b.checked) n++;
      var tr = closest(b, 'tr, [data-cc-row]');
      if (tr) tr.classList.toggle('is-selected', b.checked);
    });
    var vChecked = visible.filter(function (b) { return b.checked; }).length;
    qsa('[data-cc-select-all="' + fid + '"]').forEach(function (a) {
      a.checked = visible.length > 0 && vChecked === visible.length;
      a.indeterminate = vChecked > 0 && vChecked < visible.length;
    });
    qsa('[data-cc-bulkbar="' + fid + '"]').forEach(function (bar) {
      bar.classList.toggle('is-visible', n > 0);
      var c = qs('[data-cc-bulk-count]', bar); if (c) c.textContent = n;
      var forms = (bar.getAttribute('data-noun') || 'элемент|элемента|элементов').split('|');
      var it = qs('[data-cc-bulk-items]', bar); if (it) it.textContent = ': ' + n + ' ' + plural(n, forms);
    });
    updateBottomBar();
    doc.dispatchEvent(new CustomEvent('cc:selection', { detail: { form: fid, count: n } }));
  }
  /** Снять выделение строк формы (или всех форм): CC.clearSelection('newsBulk'). */
  CC.clearSelection = function (fid) {
    var ids = fid ? [fid] : bulkForms();
    ids.forEach(function (id) { qsa('input[data-cc-select="' + id + '"]').forEach(function (b) { b.checked = false; }); updateBulk(id); });
  };
  CC.selection = function (fid) { return selectedBoxes(fid).map(function (b) { return b.value; }); };
  function bulkForms() {
    var ids = {};
    qsa('[data-cc-select], [data-cc-select-all], [data-cc-bulkbar]').forEach(function (el) {
      ids[el.getAttribute('data-cc-select') || el.getAttribute('data-cc-select-all') || el.getAttribute('data-cc-bulkbar')] = 1;
    });
    return Object.keys(ids);
  }
  function clearAllSelections() {
    var had = false;
    bulkForms().forEach(function (id) { if (selectedBoxes(id).length) { had = true; CC.clearSelection(id); } });
    return had;
  }
  var lastSelect = {};
  doc.addEventListener('click', function (e) {
    var b = e.target;
    if (!b || !b.matches || !b.matches('input[data-cc-select]')) return;
    var fid = b.getAttribute('data-cc-select');
    var boxes = selectBoxes(fid);
    if (e.shiftKey && lastSelect[fid] && boxes.indexOf(lastSelect[fid]) !== -1) {
      var a = boxes.indexOf(lastSelect[fid]), z = boxes.indexOf(b);
      var from = Math.min(a, z), to = Math.max(a, z);
      for (var i = from; i <= to; i++) boxes[i].checked = b.checked;
    }
    lastSelect[fid] = b;
  });
  doc.addEventListener('change', function (e) {
    var t = e.target;
    if (!t || !t.matches) return;
    if (t.matches('[data-cc-select-all]')) {
      var fid = t.getAttribute('data-cc-select-all');
      selectBoxes(fid).forEach(function (b) { b.checked = t.checked; });
      updateBulk(fid);
    } else if (t.matches('[data-cc-select]')) {
      updateBulk(t.getAttribute('data-cc-select'));
    }
  });
  doc.addEventListener('click', function (e) {
    var c = closest(e.target, '[data-cc-bulk-clear]');
    if (!c) return;
    var bar = closest(c, '[data-cc-bulkbar]');
    CC.clearSelection(bar ? bar.getAttribute('data-cc-bulkbar') : null);
  });
  CC.onInit(function () { bulkForms().forEach(updateBulk); });   // браузер мог восстановить отметки после «Назад»

  /* ----------------------------- Фильтры (§4.4) ----------------------------- */
  var MOBILE = window.matchMedia ? window.matchMedia('(max-width: 767.98px)') : { matches: false };
  function inSheetMode(el) { return MOBILE.matches && !!closest(el, '.cc-filters__sheet'); }
  function submitFilters(form, input) {
    if (input) {
      CC.session.set('cc-refocus', JSON.stringify({ path: location.pathname, name: input.name,
        pos: typeof input.selectionStart === 'number' ? input.selectionStart : null }));
    }
    if (form.requestSubmit) form.requestSubmit(); else form.submit();
  }
  // <select data-autosubmit> / <input type=checkbox data-autosubmit> — отправить форму при изменении
  // (в шторке фильтров на телефоне — нет: там кнопка «Показать»).
  doc.addEventListener('change', function (e) {
    var el = e.target;
    if (!el || !el.hasAttribute || !el.hasAttribute('data-autosubmit') || !el.form) return;
    if (inSheetMode(el)) return;
    var field = closest(el, '.cc-fsel, .cc-fswitch');
    if (field) field.classList.toggle('is-active', el.type === 'checkbox' ? el.checked : !!el.value);
    if (el.form.requestSubmit) el.form.requestSubmit(); else el.form.submit();
  });
  // Пилюля «Группа: Все ▾» — по ширине выбранного значения, а не самого длинного варианта. Где браузер умеет
  // field-sizing: content (Chrome 123+) — это делает CSS; иначе (Firefox, Safari) ширину считаем по тексту.
  // На телефоне пилюли в шторке и поле во всю ширину — инлайн-ширину снимаем.
  var FIELD_SIZING = !!(window.CSS && CSS.supports && CSS.supports('field-sizing', 'content'));
  var fselMeasure = null;
  function fitSelect(sel) {
    var opt = sel.options[sel.selectedIndex] || sel.options[0];
    if (MOBILE.matches || !opt) { sel.style.width = ''; return; }
    var cs = getComputedStyle(sel);
    if (!fselMeasure) {
      fselMeasure = doc.createElement('span');
      fselMeasure.setAttribute('aria-hidden', 'true');
      fselMeasure.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden;white-space:pre;';
      doc.body.appendChild(fselMeasure);
    }
    ['fontStyle', 'fontVariant', 'fontWeight', 'fontSize', 'fontFamily', 'letterSpacing', 'textTransform'].forEach(function (p) { fselMeasure.style[p] = cs[p]; });
    fselMeasure.textContent = opt.text;
    var box = ['paddingLeft', 'paddingRight', 'borderLeftWidth', 'borderRightWidth'].reduce(function (s, p) { return s + (parseFloat(cs[p]) || 0); }, 0);
    sel.style.width = Math.ceil(fselMeasure.getBoundingClientRect().width + box + 4) + 'px';   // +4 — внутренний отступ текста select
  }
  function fitSelects(root) { qsa('.cc-filters--inline select.cc-fsel__select', root).forEach(fitSelect); }
  if (!FIELD_SIZING) {
    CC.onInit(fitSelects);
    doc.addEventListener('change', function (e) { if (e.target && e.target.matches && e.target.matches('.cc-filters--inline select.cc-fsel__select')) fitSelect(e.target); });
    var refit = function () { fitSelects(doc); };
    if (MOBILE.addEventListener) MOBILE.addEventListener('change', refit); else if (MOBILE.addListener) MOBILE.addListener(refit);
    if (doc.fonts && doc.fonts.ready) doc.fonts.ready.then(refit);   // Montserrat догрузился — текст стал шире
  }
  // Поиск: отправка через N мс после ввода (data-cc-autosubmit-debounce="600"); с data-cc-filter — мгновенно на клиенте.
  CC.onInit(function (root) {
    qsa('input[data-cc-autosubmit-debounce], [data-cc-filters] input[type="search"]', root).forEach(function (input) {
      if (input._ccDebounce || !input.form) return; input._ccDebounce = true;
      var initial = input.value;
      if (input.hasAttribute('data-cc-filter')) return;   // мгновенный фильтр — ниже
      var ms = parseInt(input.getAttribute('data-cc-autosubmit-debounce') || '600', 10);
      var run = debounce(function () {
        if (input.value === initial || inSheetMode(input)) return;
        submitFilters(input.form, input);
      }, ms);
      input.addEventListener('input', run);
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') CC.session.set('cc-refocus', JSON.stringify({ path: location.pathname, name: input.name, pos: input.value.length }));
      });
    });
    // Мгновенный фильтр на клиенте: <input data-cc-filter="#grid"> + элементы с data-search (или строки таблицы).
    qsa('input[data-cc-filter]', root).forEach(function (input) {
      if (input._ccFilter) return; input._ccFilter = true;
      var target = qs(input.getAttribute('data-cc-filter'));
      if (!target) return;
      var run = debounce(function () { CC.filterItems(target, input.value); }, 120);
      input.addEventListener('input', run);
      if (input.value) CC.filterItems(target, input.value);
    });
  });
  /** Показать элементы target, подходящие под запрос (все слова): строки tbody или потомки с data-search.
   *  Пустой результат: элемент [data-cc-filter-empty] внутри target (или с data-cc-filter-empty="#target"). → число видимых. */
  CC.filterItems = function (target, query) {
    var words = norm(query).split(/\s+/).filter(Boolean), shown = 0;
    var items = target.tagName === 'TABLE' || qs('tbody', target) ? qsa('tbody tr', target).filter(function (tr) { return !tr.hasAttribute('data-table-empty'); })
      : qsa('[data-search]', target);
    items.forEach(function (el) {
      var hay = el._ccText || (el._ccText = norm(el.getAttribute('data-search') || el.textContent));
      var ok = words.every(function (w) { return hay.indexOf(w) !== -1; });
      el.classList.toggle('is-hidden', !ok); if (ok) shown++;
    });
    var empty = qs('[data-cc-filter-empty], [data-table-empty]', target) || (target.id && qs('[data-cc-filter-empty="#' + target.id + '"]'));
    if (empty) { empty.classList.toggle('is-hidden', shown !== 0); empty.hidden = shown !== 0 ? true : false; }
    doc.dispatchEvent(new CustomEvent('cc:filtered', { detail: { target: target, shown: shown } }));
    return shown;
  };
  // Пустые поля GET-фильтров не уходят в URL (чище адрес и закладки).
  doc.addEventListener('submit', function (e) {
    var f = e.target;
    if (e.defaultPrevented || !f.hasAttribute || !f.hasAttribute('data-cc-filters')) return;
    qsa('input:not([type="hidden"]):not([type="checkbox"]):not([type="radio"]), select', f).forEach(function (el) {
      var sameName = el.name ? qsa('[name="' + (window.CSS && CSS.escape ? CSS.escape(el.name) : el.name) + '"]', f) : [];
      if (el.name && !el.disabled && el.value === '' && !sameName.some(function (o) { return o !== el && o.value !== ''; })) {
        el.disabled = true; el._ccTmpDisabled = true;
      }
    });
    setTimeout(function () { qsa('input, select', f).forEach(function (el) { if (el._ccTmpDisabled) { el.disabled = false; el._ccTmpDisabled = false; } }); }, 0);
  });
  // Возврат фокуса в поиск после перезагрузки по вводу.
  onReady(function () {
    var raw = CC.session.get('cc-refocus');
    if (!raw) return;
    CC.session.del('cc-refocus');
    try {
      var r = JSON.parse(raw);
      if (r.path !== location.pathname) return;
      var input = qsa('[data-cc-filters] [name="' + r.name + '"], input[data-cc-search][name="' + r.name + '"]').filter(isVisible)[0];
      if (!input) return;
      input.focus({ preventScroll: true });
      var pos = r.pos == null ? input.value.length : Math.min(r.pos, input.value.length);
      try { input.setSelectionRange(pos, pos); } catch (err) { /* type=search без selection API */ }
    } catch (err) { /* битые данные */ }
  });
  // Чипы активных фильтров (видны на телефоне, где пилюли спрятаны в шторку).
  CC.onInit(function (root) {
    qsa('form.cc-filters--inline', root).forEach(function (form) {
      if (form._ccTags) return; form._ccTags = true;
      var sheet = qs('.cc-filters__sheet', form);
      if (!sheet) return;
      var tags = [];
      qsa('select, input:not([type="hidden"])', sheet).forEach(function (el) {
        if (!el.name || el.disabled) return;
        var label = '', value = '';
        var lab = el.id ? qs('label[for="' + el.id + '"]', form) : null;
        label = lab ? lab.textContent.trim().replace(/:$/, '') : el.name;
        if (el.tagName === 'SELECT') {
          var first = el.options[0];
          if (!el.value) return;
          if (el.selectedIndex === 0 && first && first.value !== '') return;   // значение по умолчанию без «Все»
          value = el.options[el.selectedIndex] ? el.options[el.selectedIndex].text : el.value;
        } else if (el.type === 'checkbox') {
          if (!el.checked) return; value = ''; 
        } else {
          if (!el.value) return; value = el.value;
        }
        tags.push({ el: el, label: label, value: value });
      });
      if (!tags.length) return;
      var box = doc.createElement('div'); box.className = 'cc-filter-tags'; box.setAttribute('aria-label', 'Активные фильтры');
      tags.forEach(function (t) {
        var tag = doc.createElement('span'); tag.className = 'cc-filter-tag';
        var txt = doc.createElement('span');
        if (t.value) { txt.appendChild(doc.createTextNode(t.label + ': ')); var b = doc.createElement('b'); b.textContent = t.value; txt.appendChild(b); }
        else { var b2 = doc.createElement('b'); b2.textContent = t.label; txt.appendChild(b2); }
        var x = doc.createElement('button'); x.type = 'button'; x.setAttribute('aria-label', 'Убрать фильтр: ' + t.label);
        x.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
        x.addEventListener('click', function () {
          if (t.el.type === 'checkbox') t.el.checked = false; else t.el.value = '';
          if (form.requestSubmit) form.requestSubmit(); else form.submit();
        });
        tag.appendChild(txt); tag.appendChild(x); box.appendChild(tag);
      });
      form.parentNode.insertBefore(box, form.nextSibling);
    });
  });

  /* ----------------------------- Контекст списков: «назад» к тем же фильтрам (§4.4.4) ----------------------------- */
  onReady(function () {
    if (qs('[data-cc-filters], .cc-list, .cc-pagination, .pagination, table.cc-table')) {
      CC.session.set('cc-last:' + location.pathname, location.pathname + location.search);
    }
  });
  CC.onInit(function (root) {
    qsa('a[data-cc-back]', root).forEach(function (a) {
      if (a._ccBack) return; a._ccBack = true;
      if (a.search || !sameOrigin(a.href)) return;
      var saved = CC.session.get('cc-last:' + a.pathname);
      if (saved && saved.indexOf('?') !== -1 && saved.split('?')[0] === a.pathname) a.setAttribute('href', saved);
    });
  });
  // Телефон: «‹ Лиды» в топбаре и такая же ссылка «назад» в шапке страницы — дубль; шапочную прячем (CSS < 992 px),
  // а её точный адрес (с фильтрами или другим списком из next=) отдаём ссылке топбара, чтобы не потерять контекст.
  CC.onInit(function (root) {
    if (root !== doc) return;
    var top = qs('.cc-topbar__back'), head = qs('.cc-content .cc-page-header__back');
    if (!top || !head) return;
    var samePath = top.pathname === head.pathname;
    // одинаковая подпись — дубль, даже если шапка ведёт в другой список (карточка лида из «Пользователей»);
    // разные подписи и разные адреса («‹ ВТ 24-1» → неделя группы) — это другая цель, оставляем обе ссылки
    if (top.textContent.trim() !== head.textContent.trim() && !(samePath && top.search === head.search)) return;
    if (head.search || !samePath) top.setAttribute('href', head.getAttribute('href'));
    head.classList.add('cc-page-header__back--dup');
  });

  /* ----------------------------- Вкладки/чипы/сегменты: активный виден, маска по краям (§4.6) ----------------------------- */
  function updateFade(el) {
    var max = el.scrollWidth - el.clientWidth;
    el.classList.toggle('cc-fade-l', max > 2 && el.scrollLeft > 2);
    el.classList.toggle('cc-fade-r', max > 2 && el.scrollLeft < max - 2);
  }
  CC.onInit(function (root) {
    qsa('[data-cc-scroll-active], .cc-tabs, .cc-chips, .cc-segmented, .cc-day-switch, .cc-scroll-x, .cc-stat-strip', root).forEach(function (el) {
      if (el._ccScroll) return; el._ccScroll = true;
      var act = qs('.active, [aria-current="page"], [aria-current="true"]', el);
      if (act && el.scrollWidth > el.clientWidth) {
        el.scrollLeft = Math.max(0, act.offsetLeft - el.offsetLeft - (el.clientWidth - act.offsetWidth) / 2);
      }
      updateFade(el);
      el.addEventListener('scroll', debounce(function () { updateFade(el); }, 50), { passive: true });
    });
  });
  window.addEventListener('resize', debounce(function () { qsa('.cc-tabs, .cc-chips, .cc-segmented, .cc-day-switch, .cc-scroll-x, .cc-stat-strip').forEach(updateFade); }, 150));

  /* ----------------------------- Вид (cookie) и сворачиваемые блоки ----------------------------- */
  CC.view = {
    /** Запомнить вид страницы: cookie cc_view_<key>=<value> (1 год). Сервер: view_param(key, allowed, default). */
    remember: function (key, value) {
      doc.cookie = 'cc_view_' + encodeURIComponent(key) + '=' + encodeURIComponent(value) + '; path=/; max-age=31536000; SameSite=Lax';
    }
  };
  doc.addEventListener('click', function (e) {
    var a = closest(e.target, '[data-cc-view]');
    if (a) CC.view.remember(a.getAttribute('data-cc-view'), a.getAttribute('data-cc-view-value') || '');
  });
  CC.onInit(function (root) {
    qsa('details[data-cc-collapse-key]', root).forEach(function (d) {
      if (d._ccCollapse) return; d._ccCollapse = true;
      var key = 'cc-collapse:' + d.getAttribute('data-cc-collapse-key');
      var v = CC.store.get(key);
      if (v === 'open') d.open = true; else if (v === 'closed') d.open = false;
      d.addEventListener('toggle', function () { CC.store.set(key, d.open ? 'open' : 'closed'); });
    });
    // Подсказки с «×» (ui.hint(dismiss=…)): скрыть и запомнить.
    qsa('[data-cc-hint]', root).forEach(function (h) {
      if (CC.store.get('cc-hint:' + h.getAttribute('data-cc-hint')) === 'hidden') h.style.display = 'none';
    });
    // ui.modal_form(open=True): открыть при загрузке (после ошибки сервера).
    if (window.bootstrap) qsa('.modal[data-cc-open-on-load]', root).forEach(function (m) {
      if (m._ccOpened) return; m._ccOpened = true;
      window.bootstrap.Modal.getOrCreateInstance(m).show();
    });
    // Шапка страницы на телефоне: у второстепенных кнопок видна только иконка — подпись в title.
    qsa('.cc-page-header__actions .btn:not(.btn-primary), .cc-card__head .btn', root).forEach(function (b) {
      if (b.title || b.getAttribute('aria-label')) return;
      var s = qs('span', b); if (s && s.textContent.trim()) b.title = s.textContent.trim();
    });
  });
  doc.addEventListener('click', function (e) {
    var c = closest(e.target, '[data-cc-hint-close]');
    if (!c) return;
    var h = closest(c, '[data-cc-hint]');
    if (!h) return;
    CC.store.set('cc-hint:' + h.getAttribute('data-cc-hint'), 'hidden');
    h.style.display = 'none';
  });

  /* ----------------------------- Формы: проверка, несохранённые изменения, Ctrl+S (§4.8) ----------------------------- */
  function fieldMessage(f) {
    var v = f.validity;
    if (v.valueMissing) return f.type === 'checkbox' ? 'Отметьте это поле' : (f.tagName === 'SELECT' || f.type === 'radio' ? 'Выберите значение' : (f.type === 'file' ? 'Добавьте файл' : 'Заполните поле'));
    if (v.typeMismatch) return f.type === 'email' ? 'Проверьте email' : (f.type === 'url' ? 'Проверьте ссылку (https://…)' : 'Проверьте значение');
    if (v.tooLong) return 'Не длиннее ' + f.maxLength + ' символов';
    if (v.tooShort) return 'Не короче ' + f.minLength + ' символов';
    if (v.patternMismatch) return f.title || 'Проверьте формат';
    if (v.rangeUnderflow || v.rangeOverflow) {
      if (f.min !== '' && f.max !== '') return 'От ' + f.min + ' до ' + f.max;
      return f.min !== '' ? 'Не меньше ' + f.min : 'Не больше ' + f.max;
    }
    if (v.badInput) return f.type === 'number' ? 'Введите число' : 'Проверьте значение';
    if (v.stepMismatch) return 'Проверьте значение (шаг ' + (f.step || 1) + ')';
    if (v.customError) return f.validationMessage;
    return 'Проверьте поле';
  }
  function feedbackAnchor(f) {
    return closest(f, '.input-group, .cc-password, .cc-search, .form-check') || f;
  }
  function markInvalid(f, msg) {
    f.classList.add('is-invalid'); f.setAttribute('aria-invalid', 'true'); f._ccInvalid = true;
    var anchor = feedbackAnchor(f);
    var fb = anchor.nextElementSibling;
    if (!fb || !fb.hasAttribute('data-cc-feedback')) {
      fb = doc.createElement('div'); fb.className = 'invalid-feedback d-block'; fb.setAttribute('data-cc-feedback', '');
      fb.id = (f.id || ('f' + Math.random().toString(36).slice(2))) + '-cc-error';
      anchor.parentNode.insertBefore(fb, anchor.nextSibling);
      var desc = (f.getAttribute('aria-describedby') || '').split(/\s+/).filter(Boolean);
      if (desc.indexOf(fb.id) === -1) { desc.push(fb.id); f.setAttribute('aria-describedby', desc.join(' ')); }
    }
    fb.textContent = msg;
  }
  function clearInvalid(f) {
    if (!f._ccInvalid) return;
    f._ccInvalid = false; f.classList.remove('is-invalid'); f.removeAttribute('aria-invalid');
    var fb = feedbackAnchor(f).nextElementSibling;
    if (fb && fb.hasAttribute('data-cc-feedback')) fb.parentNode.removeChild(fb);
  }
  function reveal(f) {
    var d = closest(f, 'details'); if (d) d.open = true;
    var col = closest(f, '.collapse:not(.show)');
    if (col && window.bootstrap) window.bootstrap.Collapse.getOrCreateInstance(col, { toggle: false }).show();
    var pane = closest(f, '.tab-pane:not(.active)');
    if (pane && pane.id && window.bootstrap) { var tab = qs('[data-bs-target="#' + pane.id + '"], [href="#' + pane.id + '"]'); if (tab) window.bootstrap.Tab.getOrCreateInstance(tab).show(); }
    var target = isVisible(f) ? f : (closest(f, '[data-cc-uploader], .mb-3, .form-check') || f);
    setTimeout(function () {
      try { target.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (e) { target.scrollIntoView(); }
      try { f.focus({ preventScroll: true }); } catch (e) { /* скрытое поле */ }
    }, 60);
  }
  /** Проверить форму на клиенте (required, type, maxlength, pattern, min/max) → true, если всё верно. */
  CC.validateForm = function (form) {
    var first = null;
    Array.prototype.forEach.call(form.elements, function (f) {
      if (!f.willValidate || f.disabled || f.type === 'hidden' || f.type === 'submit' || f.type === 'button') return;
      if (f.checkValidity()) { clearInvalid(f); return; }
      markInvalid(f, fieldMessage(f));
      if (!first) first = f;
    });
    if (first) reveal(first);
    return !first;
  };
  // Перед отправкой POST-формы с novalidate (без data-cc-novalidate) — проверка на клиенте:
  // при ошибке форма не уходит, выбранные фото остаются в форме.
  doc.addEventListener('submit', function (e) {
    var form = e.target;
    if (!form || !form.hasAttribute || !form.hasAttribute('novalidate') || form.hasAttribute('data-cc-novalidate')) return;
    if ((form.getAttribute('method') || 'get').toLowerCase() !== 'post') return;
    var sub = e.submitter;
    if (sub && (sub.formNoValidate || sub.hasAttribute('formnovalidate'))) return;
    if (CC.validateForm(form)) return;
    e.preventDefault(); e.stopImmediatePropagation();
  }, true);
  function recheck(e) {
    var f = e.target;
    if (f && f._ccInvalid && f.checkValidity && f.checkValidity()) clearInvalid(f);
  }
  doc.addEventListener('input', recheck);
  doc.addEventListener('change', recheck);

  // Несохранённые изменения: form[data-dirty-guard] — предупреждение при уходе; формы с ui.savebar — индикатор.
  function savebarOf(form) {
    var bar = qs('[data-cc-savebar]', form);
    if (!bar && form.id) { var b = qs('[data-cc-savebar] [form="' + form.id + '"]'); bar = b ? closest(b, '[data-cc-savebar]') : null; }
    return bar;
  }
  var guarded = [];
  CC.onInit(function (root) {
    var forms = qsa('form[data-dirty-guard]', root);
    qsa('[data-cc-savebar]', root).forEach(function (bar) {
      var f = closest(bar, 'form') || (qs('[form]', bar) && doc.getElementById(qs('[form]', bar).getAttribute('form')));
      if (f && forms.indexOf(f) === -1) forms.push(f);
    });
    forms.forEach(function (form) {
      if (form._ccDirtyInit) return; form._ccDirtyInit = true;
      var bar = savebarOf(form);
      function set(v) { form._ccIsDirty = v; if (bar) bar.classList.toggle('is-dirty', v); }
      function onChange(e) { if (e && e.target && e.target.hasAttribute && e.target.hasAttribute('data-cc-no-dirty')) return; set(true); }
      form.addEventListener('input', onChange);
      form.addEventListener('change', onChange);
      form.addEventListener('cc:uploader-change', function () { set(true); });
      if (form.id) {   // поля вне формы с form="id"
        qsa('[form="' + form.id + '"]').forEach(function (el) { if (!form.contains(el)) { el.addEventListener('input', onChange); el.addEventListener('change', onChange); } });
      }
      if (form.hasAttribute('data-dirty-guard')) guarded.push(form);
    });
    updateBottomBar();
  });
  // Отправка снимает «несохранённые изменения», только если её никто не отменил. Проверяем на window — после
  // обработчиков формы и документа, в каком бы порядке модули их ни повесили. JSON-сохранение: модуль в своём
  // submit зовёт e.preventDefault() (форма остаётся «грязной»), после успешного ответа — CC.markClean(form).
  window.addEventListener('submit', function (e) {
    var form = e.target;
    if (form && form._ccDirtyInit && !e.defaultPrevented) CC.markClean(form);
  });
  window.addEventListener('beforeunload', function (e) {
    if (guarded.some(function (f) { return f._ccIsDirty && doc.contains(f); })) { e.preventDefault(); e.returnValue = ''; }
  });
  /** Отметить форму сохранённой (после успешного fetch-сохранения): CC.markClean(form). */
  CC.markClean = function (form) { form._ccIsDirty = false; var bar = savebarOf(form); if (bar) bar.classList.remove('is-dirty'); };
  /** Отметить форму изменённой (правка без событий input/change — перестановка, удаление строки): CC.markDirty(form). */
  CC.markDirty = function (form) { form._ccIsDirty = true; var bar = savebarOf(form); if (bar) bar.classList.add('is-dirty'); };

  function primarySubmit(form) {
    var btn = qs('[data-cc-save]', form) || (form.id && qs('[data-cc-save][form="' + form.id + '"]'));
    if (!btn) btn = qsa('button[type="submit"]:not([formaction]), input[type="submit"]:not([formaction])', form).filter(function (b) { return !b.disabled; })[0];
    return btn || null;
  }
  function submitForm(form) {
    var btn = primarySubmit(form);
    // у кнопки своё подтверждение (публикация с пушем и т. п.) — нажимаем её, чтобы оно не потерялось
    if (btn && btn.hasAttribute('data-confirm')) { btn.click(); return; }
    if (form.requestSubmit) form.requestSubmit(btn || undefined); else form.submit();
  }
  // Ctrl/⌘+S: [data-cc-hotkey-save] → форма с фокусом (data-dirty-guard/savebar) → единственная form[data-dirty-guard].
  function saveHotkey() {
    var explicit = firstVisible('[data-cc-hotkey-save]');
    if (explicit) { explicit.click(); return true; }
    var active = doc.activeElement;
    var f = active && (active.form || closest(active, 'form'));
    if (f && (f.hasAttribute('data-dirty-guard') || savebarOf(f))) { submitForm(f); return true; }
    if (anyOverlayOpen()) return false;
    var forms = qsa('form[data-dirty-guard]').filter(function (x) { return isVisible(x) || qs('[data-cc-savebar]', x); });
    if (forms.length === 1) { submitForm(forms[0]); return true; }
    return false;
  }

  /* ----------------------------- Черновики форм: form[data-cc-draft="news:new"] (P2) ----------------------------- */
  CC.onInit(function (root) {
    qsa('form[data-cc-draft]', root).forEach(function (form) {
      if (form._ccDraft) return; form._ccDraft = true;
      var key = 'cc-draft:' + form.getAttribute('data-cc-draft');
      function fields() {
        return Array.prototype.filter.call(form.elements, function (f) {
          return f.name && f.name !== 'csrf_token' && f.name !== 'next' && !f.disabled &&
            /^(text|search|email|url|tel|number|date|datetime-local|time|textarea|select-one)$/.test(f.type);
        });
      }
      function snapshot() { var o = {}; fields().forEach(function (f) { o[f.name] = f.value; }); return o; }
      var initial = JSON.stringify(snapshot());
      var raw = CC.store.get(key), saved = null;
      try { saved = raw ? JSON.parse(raw) : null; } catch (e) { saved = null; }
      if (saved && saved.values && JSON.stringify(saved.values) !== initial) {
        var t = new Date(saved.at || Date.now());
        var hint = doc.createElement('div'); hint.className = 'cc-hint cc-hint--info';
        var txt = doc.createElement('div'); txt.className = 'cc-hint__text';
        txt.textContent = 'Есть несохранённый черновик от ' + ('0' + t.getHours()).slice(-2) + ':' + ('0' + t.getMinutes()).slice(-2) + '. ';
        var restore = doc.createElement('button'); restore.type = 'button'; restore.className = 'btn btn-sm btn-light ms-1'; restore.textContent = 'Восстановить';
        var drop = doc.createElement('button'); drop.type = 'button'; drop.className = 'btn btn-sm btn-ghost ms-1'; drop.textContent = 'Удалить';
        txt.appendChild(restore); txt.appendChild(drop);
        var ic = doc.createElement('i'); ic.className = 'bi bi-file-earmark-text cc-hint__icon'; ic.setAttribute('aria-hidden', 'true');
        hint.appendChild(ic); hint.appendChild(txt);
        form.parentNode.insertBefore(hint, form);
        restore.addEventListener('click', function () {
          fields().forEach(function (f) { if (Object.prototype.hasOwnProperty.call(saved.values, f.name)) { f.value = saved.values[f.name]; f.dispatchEvent(new Event('input', { bubbles: true })); } });
          hint.parentNode.removeChild(hint);
        });
        drop.addEventListener('click', function () { CC.store.del(key); hint.parentNode.removeChild(hint); });
      }
      var save = debounce(function () {
        var cur = snapshot();
        if (JSON.stringify(cur) === initial) return;
        CC.store.set(key, JSON.stringify({ at: Date.now(), values: cur }));
      }, 2000);
      form.addEventListener('input', save);
      form.addEventListener('submit', function (e) { if (!e.defaultPrevented) CC.store.del(key); });
    });
  });

  /* ----------------------------- Мелочи: копирование, пароль, загрузка ----------------------------- */
  /** Копировать текст в буфер: CC.copy('текст') → Promise. */
  CC.copy = function (text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text);
    return new Promise(function (resolve, reject) {
      var ta = doc.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
      doc.body.appendChild(ta); ta.select();
      try { doc.execCommand('copy') ? resolve() : reject(new Error('copy')); } catch (e) { reject(e); } finally { doc.body.removeChild(ta); }
    });
  };
  doc.addEventListener('click', function (e) {
    var el = closest(e.target, '[data-copy]');
    if (el) {
      e.preventDefault();
      CC.copy(el.getAttribute('data-copy')).then(function () { CC.toast(el.getAttribute('data-copy-message') || 'Скопировано', 'success'); },
        function () { CC.toast('Не удалось скопировать', 'error'); });
      return;
    }
    // Кнопка «показать пароль»: <button data-password-toggle="#id">.
    var btn = closest(e.target, '[data-password-toggle]');
    if (btn) {
      var input = qs(btn.getAttribute('data-password-toggle'));
      if (!input) return;
      var show = input.type === 'password';
      input.type = show ? 'text' : 'password';
      btn.setAttribute('aria-pressed', show ? 'true' : 'false');
      btn.setAttribute('aria-label', show ? 'Скрыть пароль' : 'Показать пароль');
      var i = qs('i', btn); if (i) i.className = 'bi ' + (show ? 'bi-eye-slash' : 'bi-eye');
    }
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
  window.addEventListener('pageshow', function (e) {
    if (!e.persisted) return;   // вернулись «Назад» из bfcache — снять блокировку кнопок
    qsa('button[data-loading][disabled]').forEach(function (b) { b.disabled = false; var sp = qs('.spinner-border', b); if (sp) sp.parentNode.removeChild(sp); });
  });

  /* ----------------------------- Быстрый просмотр (§4.9) ----------------------------- */
  function canPeek(e) {
    return window.innerWidth >= 768 && !!qs('#ccPeek') && !!window.bootstrap && !(e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button));
  }
  var peekCtrl = null;
  /** Открыть страницу в правой панели: CC.peek('/applicants/ap_001?next=…'). Ответ — base.html с X-CC-Partial: 1. */
  CC.peek = function (url) {
    var el = qs('#ccPeek');
    if (!el || !window.bootstrap) { window.location.href = url; return; }
    var body = qs('[data-cc-peek-body]', el), title = qs('#ccPeekTitle', el), full = qs('[data-cc-peek-full]', el);
    body.innerHTML = '<div class="cc-skel" aria-hidden="true"><div class="cc-skeleton" style="width:60%;height:18px"></div><div class="cc-skeleton" style="width:90%"></div><div class="cc-skeleton" style="width:75%"></div><div class="cc-skeleton" style="width:84%"></div></div>';
    body.setAttribute('aria-busy', 'true');
    title.textContent = 'Загрузка…';
    full.href = url;
    window.bootstrap.Offcanvas.getOrCreateInstance(el).show();
    if (peekCtrl && peekCtrl.abort) peekCtrl.abort();
    peekCtrl = window.AbortController ? new AbortController() : null;
    fetch(url, { headers: { 'X-CC-Partial': '1', 'Accept': 'text/html' }, credentials: 'same-origin', signal: peekCtrl ? peekCtrl.signal : undefined })
      .then(function (r) {
        var path = ''; try { path = new URL(r.url).pathname; } catch (e) { path = ''; }
        if (path === '/login') { window.location.href = url; return null; }   // сессия истекла
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.text();
      })
      .then(function (html) {
        if (html == null) return;
        var tpl = doc.createElement('template'); tpl.innerHTML = html;
        var frag = tpl.content.querySelector('.cc-peek-fragment');
        if (!frag) { window.location.href = url; return; }
        body.innerHTML = ''; body.appendChild(frag); body.removeAttribute('aria-busy');
        var h1 = qs('h1', frag);
        title.textContent = (h1 && h1.textContent.trim()) || frag.getAttribute('data-cc-title') || 'Просмотр';
        qsa('form', frag).forEach(ensureCsrf);
        doc.dispatchEvent(new CustomEvent('cc:content-loaded', { detail: { root: frag } }));
      })
      .catch(function (err) {
        if (err && err.name === 'AbortError') return;
        body.removeAttribute('aria-busy');
        body.innerHTML = '';
        var box = doc.createElement('div'); box.className = 'cc-empty cc-peek__error';
        box.innerHTML = '<div class="cc-empty__icon"><i class="bi bi-cloud-slash" aria-hidden="true"></i></div><p class="cc-empty__title">Не удалось загрузить</p>';
        var a = doc.createElement('a'); a.className = 'btn btn-light'; a.href = url; a.textContent = 'Открыть страницу';
        box.appendChild(a); body.appendChild(box);
        title.textContent = 'Просмотр';
      });
  };
  doc.addEventListener('click', function (e) {
    var a = closest(e.target, 'a[data-cc-peek]');
    if (!a || !canPeek(e)) return;
    e.preventDefault();
    var dd = closest(a, '.dropdown-menu');
    if (dd && window.bootstrap) { var tg = dd.parentNode && qs('[data-bs-toggle="dropdown"]', dd.parentNode); if (tg) window.bootstrap.Dropdown.getOrCreateInstance(tg).hide(); }
    CC.peek(a.href);
  });

  /* ----------------------------- Загрузка фото (ui.image_uploader) ----------------------------- */
  // Хранит порядок в скрытом поле <name>__state (JSON), новые файлы — в <input type=file name=<name>>.
  // Сервер: cms.core.images_from_uploader(name, existing, folder).
  function initUploader(root) {
    if (root._ccUploader) return;
    root._ccUploader = true;
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
    function commit() { syncFiles(); syncState(); render(); root.dispatchEvent(new CustomEvent('cc:uploader-change', { bubbles: true, detail: { count: items.length } })); }
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
  CC.onInit(function (root) { qsa('[data-cc-uploader]', root).forEach(initUploader); });

  /* ----------------------------- Графики (Chart.js подключает модуль, §1.8) ----------------------------- */
  function cssVar(name) { return getComputedStyle(doc.documentElement).getPropertyValue(name).trim(); }
  CC.cssVar = cssVar;
  /** Палитра нескольких серий: первая — бренд-тёмный, остальные спокойные. */
  CC.chartColors = function () {
    return CC.theme() === 'dark'
      ? ['#FF8A3D', '#94A3B8', '#8AB4F8', '#81C995', '#B9A8FF', '#4DB6AC']
      : ['#C24B00', '#64748B', '#1565C0', '#2E7D32', '#6D4AFF', '#00897B'];
  };
  /** Цвет обычных столбиков/полос (нейтральный) и выделенного (сегодня, максимум, первое место). */
  CC.chartNeutral = function () { return cssVar('--cc-chart-neutral') || '#CBD2DB'; };
  CC.chartNeutralStrong = function () { return cssVar('--cc-chart-neutral-strong') || '#9AA3AF'; };
  CC.chartHighlight = function () { return cssVar('--cc-brand') || '#FF6200'; };
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
  doc.addEventListener('cc:theme', function () { CC.applyChartDefaults(); });

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
