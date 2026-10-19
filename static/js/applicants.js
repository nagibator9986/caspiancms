/* ==========================================================================
   Раздел «Абитуриенты» (APPLICANTS): график регистраций (Chart.js 4.4.1), QR-коды
   (qrcode-generator 1.4.4, генерация в браузере), скачивание PNG/SVG, массовые действия
   в списке лидов, генератор кода источника, печать плаката. Использует window.CC (app.js).
   ========================================================================== */
(function () {
  'use strict';

  var CC = window.CC || {};
  var doc = document;

  function qs(sel, root) { return (root || doc).querySelector(sel); }
  function qsa(sel, root) { return Array.prototype.slice.call((root || doc).querySelectorAll(sel)); }
  function onReady(fn) { if (doc.readyState !== 'loading') fn(); else doc.addEventListener('DOMContentLoaded', fn); }
  function readJson(id) {
    var el = doc.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent); } catch (e) { return null; }
  }

  /* ----------------------------- График «Регистрации по дням» ----------------------------- */
  // Одна серия (столбцы в цвете бренда) + скользящее среднее за 7 дней (тонкая линия приглушённым
  // текстовым цветом). Одна ось Y, сетка — тонкая сплошная, таблица-двойник есть под графиком.
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
      var brand = css('--cc-brand');
      var muted = css('--cc-text-muted');
      var grid = css('--cc-border');
      chart = new window.Chart(canvas, {
        data: {
          labels: rows.map(function (r) { return r.label; }),
          datasets: [
            {
              type: 'bar', label: 'Регистрации', data: rows.map(function (r) { return r.count; }),
              backgroundColor: brand, hoverBackgroundColor: css('--cc-brand-deep') || brand,
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
                  return r ? r.date.split('-').reverse().join('.') : '';
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
    if (!svg) { if (CC.toast) CC.toast('Библиотека QR не загрузилась — обновите страницу', 'error'); return; }
    if (kind === 'svg') { CC.download(name + '.svg', svg, 'image/svg+xml'); return; }
    CC.svgToPng(svg, 1024).then(function (blob) { CC.download(name + '.png', blob); },
      function () { if (CC.toast) CC.toast('Не удалось подготовить PNG', 'error'); });
  }

  function initQr() {
    qsa('[data-ap-qr]').forEach(renderQr);

    var modalEl = doc.getElementById('apQrModal');
    var current = { url: '', code: '' };
    doc.addEventListener('click', function (e) {
      var opener = e.target.closest ? e.target.closest('[data-ap-qr-open]') : null;
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
      var dl = e.target.closest ? e.target.closest('[data-ap-qr-download]') : null;
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

  /* ----------------------------- Код нового источника ----------------------------- */
  var CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789';

  function initSourceForm() {
    var input = qs('[data-ap-code]');
    if (!input) return;
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
    var openNew = qs('[data-ap-open-new]');
    if (openNew) openNew.addEventListener('click', function () {
      setTimeout(function () { var t = qs('#new-source input[name="title"]'); if (t) t.focus(); }, 300);
    });
  }

  /* ----------------------------- Массовые действия в лидах ----------------------------- */
  function initBulk() {
    var bar = qs('[data-ap-bulk]');
    var all = qs('[data-ap-check-all]');
    var boxes = qsa('[data-ap-check]');
    if (!bar || !boxes.length) return;
    var counter = qs('[data-ap-bulk-count]', bar);

    function sync() {
      var n = 0;
      boxes.forEach(function (b) {
        if (b.checked) n += 1;
        var tr = b.closest('tr');
        if (tr) tr.classList.toggle('is-selected', b.checked);
      });
      bar.hidden = n === 0;
      if (counter) counter.textContent = String(n);
      if (all) {
        all.checked = n > 0 && n === boxes.length;
        all.indeterminate = n > 0 && n < boxes.length;
      }
      if (n === 0) {
        var coins = doc.getElementById('apBulkCoins');
        if (coins && coins.classList.contains('show') && window.bootstrap) window.bootstrap.Collapse.getOrCreateInstance(coins).hide();
      }
    }

    boxes.forEach(function (b) { b.addEventListener('change', sync); });
    if (all) all.addEventListener('change', function () {
      boxes.forEach(function (b) { b.checked = all.checked; });
      sync();
    });
    sync();
  }

  onReady(function () {
    initDailyChart();
    initQr();
    initSourceForm();
    initBulk();
  });
})();
