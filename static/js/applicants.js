/* ==========================================================================
   Раздел «Абитуриенты» (CRM): график регистраций (Chart.js 4.4.1, нейтральные столбики, «сегодня» — бренд),
   QR-коды (qrcode-generator 1.4.4, генерация в браузере), скачивание PNG/SVG, генератор кода источника,
   канбан лидов (SortableJS 1.15.2: перенос карточки = смена статуса, «Отменить» в тосте), лента активности
   карточки (фильтр по типу), быстрые даты следующего шага, «‹ / ›» по лидам. Использует window.CC (app.js).
   Выбор строк и панель массовых действий — общие (app.js), своих здесь нет.
   Обработчики — делегированные на document: они работают и во фрагменте быстрого просмотра.
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC || {};
  var doc = document;

  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onReady(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function closest(el, sel) { return el && el.closest ? el.closest(sel) : null; }
  function toast(msg, cat, opts) { if (CC.toast) CC.toast(msg, cat, opts); }
  function plural(n, forms) {
    if (CC.plural) return CC.plural(n, forms);
    var a = Math.abs(n) % 100, b = a % 10;
    return forms[(a > 10 && a < 20) ? 2 : (b > 1 && b < 5) ? 1 : (b === 1) ? 0 : 2];
  }
  function readJson(id) {
    var el = doc.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  /* ----------------------------- График «Регистрации по дням» ----------------------------- */
  // Столбики нейтральные, последний (сегодня) — бренд; тонкая линия — среднее за 7 дней.
  function initDailyChart() {
    var canvas = doc.getElementById('apDailyChart');
    var rows = readJson('apDailyData');
    if (!canvas || !rows || !window.Chart) return;
    var chart = null;
    var many = rows.length > 60;

    function render() {
      if (chart) chart.destroy();
      if (CC.applyChartDefaults) CC.applyChartDefaults();
      var css = CC.cssVar || function () { return ''; };
      var neutral = CC.chartNeutral ? CC.chartNeutral() : css('--cc-chart-neutral');
      var strong = CC.chartNeutralStrong ? CC.chartNeutralStrong() : css('--cc-chart-neutral-strong');
      var highlight = CC.chartHighlight ? CC.chartHighlight() : css('--cc-brand');
      var muted = css('--cc-text-muted');
      var grid = css('--cc-border');
      var last = rows.length - 1;
      chart = new window.Chart(canvas, {
        data: {
          labels: rows.map(function (r) { return r.label; }),
          datasets: [
            {
              type: 'bar', label: 'Регистрации', data: rows.map(function (r) { return r.count; }),
              backgroundColor: rows.map(function (r, i) { return i === last ? highlight : neutral; }),
              hoverBackgroundColor: rows.map(function (r, i) { return i === last ? highlight : strong; }),
              borderRadius: { topLeft: 4, topRight: 4 }, borderSkipped: 'bottom',
              maxBarThickness: 24, barPercentage: many ? 0.95 : 0.8, categoryPercentage: 0.9, order: 2
            },
            {
              type: 'line', label: 'Среднее за 7 дней', data: rows.map(function (r) { return r.avg; }),
              borderColor: muted, backgroundColor: muted, borderWidth: 2, pointRadius: 0, pointHoverRadius: 4,
              pointHitRadius: 12, tension: 0.35, borderCapStyle: 'round', borderJoinStyle: 'round', order: 1
            }
          ]
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          plugins: {
            legend: { position: 'bottom', align: 'start', labels: { boxWidth: 10, boxHeight: 10 } },
            tooltip: {
              callbacks: {
                title: function (items) {
                  var r = rows[items[0].dataIndex];
                  return r ? r.date.split('-').reverse().join('.') + (items[0].dataIndex === last ? ' · сегодня' : '') : '';
                },
                label: function (item) {
                  var v = item.parsed.y;
                  return ' ' + item.dataset.label + ': ' + (item.datasetIndex === 1 ? v.toFixed(1) : v);
                }
              }
            }
          },
          scales: {
            x: { grid: { display: false }, border: { color: grid }, ticks: { maxRotation: 0, autoSkip: true, maxTicksLimit: many ? 12 : 10 } },
            y: { beginAtZero: true, grid: { color: grid }, border: { display: false }, ticks: { precision: 0, maxTicksLimit: 6 } }
          }
        }
      });
    }

    render();
    doc.addEventListener('cc:theme', render);
  }

  /* ----------------------------- QR-коды ----------------------------- */
  var QR_SIZES = { thumb: { cellSize: 4, margin: 1 }, big: { cellSize: 8, margin: 2 }, poster: { cellSize: 10, margin: 0 } };

  function qrSvg(url, size) {
    if (!CC.qrSvg || typeof window.qrcode !== 'function') return null;
    try { return CC.qrSvg(url, QR_SIZES[size] || QR_SIZES.big); } catch (e) { return null; }
  }

  function renderQr(el) {
    var url = el.getAttribute('data-ap-qr');
    var size = el.getAttribute('data-ap-qr-size') || (el.classList.contains('ap-qr-thumb') ? 'thumb' : 'big');
    var svg = url ? qrSvg(url, size) : null;
    if (svg) el.innerHTML = svg;  // SVG из библиотеки по нашему же URL — без пользовательской разметки
    else if (!el.textContent) el.textContent = 'QR';
  }

  /** SVG с явным размером — для файлов (PNG-растеризация и печать в других программах). */
  function sizedSvg(url, px) {
    var svg = qrSvg(url, 'big');
    return svg ? svg.replace('<svg ', '<svg width="' + px + '" height="' + px + '" ') : null;
  }

  function download(kind, url, code) {
    var name = 'qr-' + (code || 'caspian').replace(/[^A-Za-z0-9_-]/g, '');
    var svg = sizedSvg(url, 1024);
    if (!svg) { toast('Библиотека QR не загрузилась — обновите страницу', 'error'); return; }
    if (kind === 'svg') { CC.download(name + '.svg', svg, 'image/svg+xml'); return; }
    CC.svgToPng(svg, 1024).then(function (blob) { CC.download(name + '.png', blob); },
      function () { toast('Не удалось подготовить PNG', 'error'); });
  }

  function initQr() {
    qsa('[data-ap-qr]').forEach(renderQr);

    var modalEl = doc.getElementById('apQrModal');
    var current = { url: '', code: '' };
    doc.addEventListener('click', function (e) {
      var opener = closest(e.target, '[data-ap-qr-open]');
      if (opener && modalEl) {
        e.preventDefault();
        current.url = opener.getAttribute('data-ap-qr') || opener.getAttribute('data-ap-qr-url') || '';
        current.code = opener.getAttribute('data-code') || '';
        qs('[data-ap-qr-title]', modalEl).textContent = (opener.getAttribute('data-title') || 'QR-код') + ' · ' + current.code;
        var big = qs('[data-ap-qr-big]', modalEl);
        big.setAttribute('data-ap-qr', current.url);
        big.setAttribute('data-ap-qr-size', 'big');
        renderQr(big);
        var link = qs('[data-ap-qr-link]', modalEl);
        link.href = current.url + '?preview=1';
        link.textContent = current.url;
        qs('[data-ap-qr-preview]', modalEl).href = current.url + '?preview=1';
        var poster = qs('[data-ap-qr-poster]', modalEl);
        poster.href = poster.getAttribute('data-poster-base').replace('/CODE/', '/' + encodeURIComponent(current.code) + '/');
        if (window.bootstrap) window.bootstrap.Modal.getOrCreateInstance(modalEl).show();
        return;
      }
      var dl = closest(e.target, '[data-ap-qr-download]');
      if (dl) {
        e.preventDefault();
        var url = dl.getAttribute('data-ap-qr-url') || current.url;
        var code = dl.getAttribute('data-code') || current.code;
        if (url) download(dl.getAttribute('data-ap-qr-download'), url, code);
      }
    });

    var printBtn = qs('[data-ap-print]');
    if (printBtn) printBtn.addEventListener('click', function () { window.print(); });
  }

  /* ----------------------------- Новый источник: код, модалка по #new-source ----------------------------- */
  var CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

  function initSourceForm() {
    var input = qs('[data-ap-code]');
    if (input) {
      input.addEventListener('input', function () {
        var pos = input.selectionStart;
        input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, '');
        try { input.setSelectionRange(pos, pos); } catch (e) { /* поле не в фокусе */ }
      });
      var gen = qs('[data-ap-gen-code]');
      if (gen) gen.addEventListener('click', function () {
        var out = '';
        var buf = new Uint32Array(6);
        if (window.crypto && window.crypto.getRandomValues) window.crypto.getRandomValues(buf);
        for (var i = 0; i < 6; i++) out += CODE_ALPHABET.charAt((buf[i] || Math.floor(Math.random() * 1e9)) % CODE_ALPHABET.length);
        input.value = out;
        input.focus();
      });
    }
    // Старые ссылки «…/applicants/sources#new-source» (аналитика, пустые состояния) открывают модалку
    var modal = doc.getElementById('new-source');
    if (modal && location.hash === '#new-source' && window.bootstrap) {
      window.bootstrap.Modal.getOrCreateInstance(modal).show();
    }
    if (modal) modal.addEventListener('shown.bs.modal', function () {
      var t = qs('input[name="title"]', modal);
      if (t && !t.value) t.focus();
    });
  }

  /* ----------------------------- Следующий шаг: быстрые даты ----------------------------- */
  function isoDay(offset) {
    var d = new Date();
    d.setDate(d.getDate() + offset);
    var m = d.getMonth() + 1, day = d.getDate();
    return d.getFullYear() + '-' + (m < 10 ? '0' : '') + m + '-' + (day < 10 ? '0' : '') + day;
  }
  doc.addEventListener('click', function (e) {
    var b = closest(e.target, '[data-ap-date]');
    if (!b) return;
    var form = closest(b, 'form');
    var input = form ? qs(b.getAttribute('data-ap-date-target'), form) || qs('input[type="date"]', form) : null;
    if (!input) return;
    input.value = isoDay(parseInt(b.getAttribute('data-ap-date'), 10) || 0);
    input.dispatchEvent(new Event('change', { bubbles: true }));
    var text = qs('input[name="next_step"]', form);
    if (text && !text.value) text.focus();
  });

  /* ----------------------------- Лента активности: фильтр и «показать все» ----------------------------- */
  doc.addEventListener('click', function (e) {
    var chip = closest(e.target, '[data-ap-feed-filter]');
    if (chip) {
      var card = closest(chip, '.cc-card') || doc;
      var kind = chip.getAttribute('data-ap-feed-filter');
      qsa('[data-ap-feed-filter]', card).forEach(function (c) {
        var on = c === chip;
        c.classList.toggle('active', on);
        c.setAttribute('aria-pressed', on ? 'true' : 'false');
      });
      var feed = qs('[data-ap-feed]', card);
      if (!feed) return;
      if (kind) feed.classList.add('is-expanded');   // в отфильтрованной ленте показываем все события вида
      qsa('.ap-feed__item', feed).forEach(function (li) {
        li.classList.toggle('is-filtered', !!kind && li.getAttribute('data-kind') !== kind);
      });
      return;
    }
    var more = closest(e.target, '[data-ap-feed-more]');
    if (more) {
      var f = qs('[data-ap-feed]', closest(more, '.cc-card') || doc);
      if (f) f.classList.add('is-expanded');
      more.hidden = true;
    }
  });

  /* ----------------------------- Карточка: «‹ / ›» по лидам ([ и ]) ----------------------------- */
  function initPager() {
    var prev = qs('[data-ap-prev]'), next = qs('[data-ap-next]');
    if (!prev && !next) return;
    function go(a) { if (a && a.getAttribute('aria-disabled') !== 'true' && a.getAttribute('href') !== '#') { if (CC.progress) CC.progress.start(); location.href = a.href; } }
    if (CC.hotkey) {
      CC.hotkey('BracketLeft', function () { go(prev); }, 'Предыдущий лид', { group: 'Лиды', shift: false });
      CC.hotkey('BracketRight', function () { go(next); }, 'Следующий лид', { group: 'Лиды', shift: false });
    }
  }

  /* ----------------------------- Массовое начисление: кому ----------------------------- */
  doc.addEventListener('cc:selection', function (e) {
    if (!e.detail || e.detail.form !== 'apBulk') return;
    var out = qs('[data-ap-bulk-who]');
    if (out) {
      var n = e.detail.count;
      out.textContent = n ? 'Получат ' + n + ' ' + plural(n, ['абитуриент', 'абитуриента', 'абитуриентов']) + '. Каждому — указанная сумма.' : '';
    }
  });

  /* ----------------------------- Канбан лидов ----------------------------- */
  function initBoard() {
    var board = qs('[data-ap-board]');
    if (!board || !window.Sortable) return;
    var urlTpl = board.getAttribute('data-status-url');

    function refreshCol(col) {
      var list = qs('[data-ap-board-list]', col);
      var count = qs('[data-ap-col-count]', col);
      var cards = qsa('.ap-card', list).length;
      var empty = qs('.ap-board__empty', list);
      if (empty) empty.hidden = cards > 0;
      if (count) count.textContent = String(Math.max(0, parseInt(count.textContent, 10) + (col._apDelta || 0)));
      col._apDelta = 0;
    }
    function shift(fromCol, toCol) {
      fromCol._apDelta = (fromCol._apDelta || 0) - 1; toCol._apDelta = (toCol._apDelta || 0) + 1;
      refreshCol(fromCol); refreshCol(toCol);
    }
    function move(card, toList, before) {
      var empty = qs('.ap-board__empty', toList);
      var anchor = before || qs('.ap-board__more', toList) || empty;
      toList.insertBefore(card, anchor);
    }
    function setStatus(uid, status) {
      return CC.fetchJSON(urlTpl.replace('__UID__', encodeURIComponent(uid)), { method: 'POST', body: { status: status } });
    }

    qsa('[data-ap-board-list]', board).forEach(function (list) {
      window.Sortable.create(list, {
        group: 'ap-leads', animation: 150, draggable: '.ap-card', filter: '.dropdown, .dropdown-menu, button',
        preventOnFilter: false, ghostClass: 'is-ghost', chosenClass: 'is-chosen', delay: 180, delayOnTouchOnly: true,
        onEnd: function (evt) {
          if (evt.from === evt.to) return;
          var card = evt.item, uid = card.getAttribute('data-uid');
          var fromCol = closest(evt.from, '.ap-board__col'), toCol = closest(evt.to, '.ap-board__col');
          var status = toCol.getAttribute('data-status'), old = fromCol.getAttribute('data-status');
          var back = evt.from.children[evt.oldIndex] || null;
          shift(fromCol, toCol);
          evt.to.classList.add('is-saving');
          setStatus(uid, status).then(function (data) {
            var title = (qs('.ap-card__name', card) || {}).textContent || '';
            toast((title ? title + ': ' : '') + ((data && data.message) || 'Статус изменён'), 'success', {
              action: { label: 'Отменить', onClick: function () {
                setStatus(uid, old).then(function () {
                  move(card, evt.from, back && back.parentNode === evt.from ? back : null);
                  shift(toCol, fromCol);
                  toast('Статус возвращён', 'info');
                }).catch(function (err) { toast((err && err.message) || 'Не удалось вернуть статус', 'error'); });
              } }
            });
          }).catch(function (err) {
            move(card, evt.from, back && back.parentNode === evt.from ? back : null);
            shift(toCol, fromCol);
            toast((err && err.message) || 'Не удалось сменить статус', 'error');
          }).then(function () { evt.to.classList.remove('is-saving'); });
        }
      });
    });
  }

  onReady(function () {
    initDailyChart();
    initQr();
    initSourceForm();
    initPager();
    initBoard();
  });
  // Фрагмент быстрого просмотра: QR-коды и прочее, что рисуется при загрузке
  doc.addEventListener('cc:content-loaded', function (e) {
    var root = e.detail && e.detail.root;
    if (root) qsa('[data-ap-qr]', root).forEach(renderQr);
  });
})();
