/* ERP Desk - app shell: navigation, splash, service status, toasts, quit.
   Exposes window.Desk for report.js. */
(function () {
  'use strict';

  var meta = function (n) { var m = document.querySelector('meta[name="' + n + '"]'); return m ? m.content : ''; };
  var TOKEN = meta('erp-token');
  var STREAMLIT_URL = meta('erp-streamlit-url');
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  // ---- tiny helpers shared with report.js --------------------------------
  function esc(v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function store(key, val) {                       // localStorage can throw / be empty: never depend on it
    try { if (val === undefined) return window.localStorage.getItem(key); window.localStorage.setItem(key, val); } catch (e) { /* ignore */ }
    return null;
  }
  var api = {
    get: function (path) {
      return fetch(path, { cache: 'no-store' }).then(function (r) { if (!r.ok) throw new Error(path + ' -> ' + r.status); return r.json(); });
    },
    post: function (path) {
      return fetch(path, { method: 'POST', headers: { 'X-ERP-Desk-Token': TOKEN } })
        .then(function (r) { if (!r.ok) throw new Error(path + ' -> ' + r.status); return r.json(); });
    }
  };

  var listeners = {};
  function on(evt, fn) { (listeners[evt] = listeners[evt] || []).push(fn); }
  function emit(evt, arg) { (listeners[evt] || []).forEach(function (fn) { try { fn(arg); } catch (e) { console.error(e); } }); }

  function toast(msg, kind, ms) {
    var box = $('#toasts');
    var t = document.createElement('div');
    t.className = 'toast ' + (kind || '');
    t.textContent = msg;
    box.appendChild(t);
    setTimeout(function () { t.classList.add('out'); setTimeout(function () { t.remove(); }, 380); }, ms || 4200);
    while (box.children.length > 4) box.firstChild.remove();
  }

  window.Desk = { api: api, esc: esc, store: store, toast: toast, on: on, emit: emit, token: TOKEN, view: function () { return current; },
                  go: function (name) { setView(name); } };

  // ---- navigation ---------------------------------------------------------
  var VIEWS = ['reporting', 'management', 'dataflow'];
  var current = null;
  var frameLoaded = false;

  function moveIndicator(name) {
    var idx = Math.max(0, VIEWS.indexOf(name));
    var item = $$('.nav-item')[idx];
    var ind = $('#navInd');
    if (!item) return;
    ind.style.height = item.offsetHeight + 'px';
    ind.style.transform = 'translateY(' + item.offsetTop + 'px)';
  }

  function setView(name, opts) {
    if (VIEWS.indexOf(name) < 0) name = 'reporting';
    if (name === current) return;
    current = name;
    $$('.view').forEach(function (v) { v.classList.toggle('active', v.id === 'view-' + name); });
    $$('.nav-item').forEach(function (a) {
      var on_ = a.getAttribute('data-view') === name;
      a.classList.toggle('active', on_);
      if (on_) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    moveIndicator(name);
    if (!(opts && opts.silent)) {
      // replaceState (not location.hash =) keeps a single history entry, which is also what lets
      // window.close() work in an --app window.
      try { history.replaceState(null, '', '#' + name); } catch (e) { /* ignore */ }
      store('erpdesk.view', name);
    }
    if (name === 'management') ensureFrame();
    emit('view', name);
  }

  function ensureFrame() {
    var f = $('#mgmtFrame');
    if (!f.getAttribute('src') && lastStatus && lastStatus.streamlit.state === 'ready') f.setAttribute('src', STREAMLIT_URL);
  }

  $$('.nav-item').forEach(function (a) {
    a.addEventListener('click', function (e) { e.preventDefault(); setView(a.getAttribute('data-view')); });
  });
  window.addEventListener('hashchange', function () { setView(location.hash.replace('#', '')); });
  window.addEventListener('resize', function () { moveIndicator(current); });

  document.addEventListener('keydown', function (e) {
    var tag = (e.target && e.target.tagName) || '';
    var typing = /INPUT|TEXTAREA|SELECT/.test(tag) || (e.target && e.target.isContentEditable);
    if ((e.ctrlKey || e.metaKey) && e.key === '1') { e.preventDefault(); setView('reporting'); }
    else if ((e.ctrlKey || e.metaKey) && e.key === '2') { e.preventDefault(); setView('management'); }
    else if ((e.ctrlKey || e.metaKey) && e.key === '3') { e.preventDefault(); setView('dataflow'); }
    else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 'r' || e.key === 'R') && (current === 'reporting' || current === 'dataflow')) { e.preventDefault(); emit('refresh-request'); }
    else if (e.key === 'Escape') hideQuit();
  });

  // ---- service status -----------------------------------------------------
  var lastStatus = null;
  var prevSt = null;
  var statusFails = 0;
  var steps = { services: false, report: false, mgmt: false };
  var splashStart = Date.now();
  var splashGone = false;

  function markStep(name) {
    if (steps[name]) return;
    steps[name] = true;
    var li = $('#splashSteps [data-step="' + name + '"]');
    if (li) { li.classList.remove('active'); li.classList.add('done'); }
    var done = Object.keys(steps).filter(function (k) { return steps[k]; }).length;
    $('#splashBar').style.width = Math.round(done / 3 * 100) + '%';
    var next = Object.keys(steps).filter(function (k) { return !steps[k]; })[0];
    if (next) { var nl = $('#splashSteps [data-step="' + next + '"]'); if (nl) nl.classList.add('active'); }
    if (!next) finishSplash();
  }
  function finishSplash() {
    if (splashGone) return;
    splashGone = true;
    var wait = Math.max(0, 1500 - (Date.now() - splashStart)); // let the intro animation play
    setTimeout(function () { $('#splash').classList.add('gone'); document.body.classList.add('ready'); emit('ready'); }, wait);
  }
  // Never trap the user behind the splash: give up waiting after 25 s.
  setTimeout(function () { ['services', 'report', 'mgmt'].forEach(markStep); }, 25000);

  function svcRow(name, state, text) {
    var row = $('.rs-row[data-svc="' + name + '"]');
    if (row) row.setAttribute('data-state', state);
    var val = $(name === 'report' ? '#rsReport' : '#rsMgmt');
    if (val) val.textContent = text;
  }

  function applyStatus(s) {
    lastStatus = s;
    statusFails = 0;
    markStep('services');
    var st = s.streamlit.state;
    if (st === 'ready' && (prevSt === 'restarting' || prevSt === 'down') && $('#mgmtFrame').getAttribute('src')) {
      $('#mgmtFrame').setAttribute('src', STREAMLIT_URL + '?r=' + Date.now());  // fresh session after a Streamlit restart
    }
    prevSt = st;
    svcRow('mgmt', st === 'ready' ? 'ok' : (st === 'down' ? 'bad' : 'warn'), st === 'ready' ? 'ready' : st);
    var r = s.report;
    svcRow('report', r.error ? 'warn' : (r.has_data ? 'ok' : 'warn'), r.error ? 'stale' : (r.has_data ? 'live' : 'loading'));
    if (st === 'ready') {
      var f = $('#mgmtFrame');
      if (!f.getAttribute('src') && (current === 'management' || !splashGone)) f.setAttribute('src', STREAMLIT_URL);
      if (!frameLoaded && f.getAttribute('src')) { /* wait for onload */ } else { markStep('mgmt'); }
    } else if (st === 'down') {
      markStep('mgmt');
    }
    var ov = $('#mgmtOverlay');
    if (st === 'ready' && frameLoaded) ov.classList.add('hide');
    else if (st === 'ready') { /* frame still loading: keep the overlay */ }
    else {
      ov.classList.remove('hide');
      $('#mgmtOverlayTitle').textContent = st === 'down' ? 'Script Center is not running' : (st === 'restarting' ? 'Restarting Script Center…' : 'Starting Script Center…');
      $('#mgmtOverlaySub').textContent = st === 'down'
        ? 'It stopped several times in a row. Check erp_desktop_streamlit.log, then restart ERP Desk.'
        : 'This takes a few seconds.';
      if (st !== 'ready') frameLoaded = false;
    }
    emit('status', s);
  }

  $('#mgmtFrame').addEventListener('load', function () {
    if (!this.getAttribute('src')) return;
    // Streamlit finishes booting its own UI shortly after the iframe's load event.
    setTimeout(function () {
      frameLoaded = true;
      $('#mgmtOverlay').classList.add('hide');
      markStep('mgmt');
    }, 900);
  });

  function pollStatus() {
    api.get('/api/status').then(applyStatus).catch(function () {
      statusFails++;
      if (statusFails >= 3) showGoodbye('ERP Desk has stopped', 'The background services are no longer running. You can close this window and start ERP Desk again from its shortcut.');
    });
  }
  setInterval(pollStatus, 3000);
  pollStatus();

  // ---- quit ---------------------------------------------------------------
  function showQuit() { $('#quitModal').hidden = false; $('#quitCancel').focus(); }
  function hideQuit() { $('#quitModal').hidden = true; }
  function showGoodbye(title, sub) {
    $('#goodbyeTitle').textContent = title;
    $('#goodbyeSub').textContent = sub;
    $('#goodbye').hidden = false;
    $('#quitModal').hidden = true;
  }
  $('#btnQuit').addEventListener('click', showQuit);
  $('#quitCancel').addEventListener('click', hideQuit);
  $('#quitModal').addEventListener('click', function (e) { if (e.target === this) hideQuit(); });
  $('#quitConfirm').addEventListener('click', function () {
    showGoodbye('ERP Desk is shutting down', 'Stopping services… this window closes by itself.');
    api.post('/api/quit').catch(function () { /* server may already be going away */ });
    setTimeout(function () { try { window.close(); } catch (e) { /* the launcher closes the window anyway */ } }, 2500);
  });
  $('#btnRefresh').addEventListener('click', function () { emit('refresh-request'); });

  // Browser-tab fallback only: lets the launcher notice a closed tab. In the normal app-window
  // mode the launcher watches the window itself and ignores this.
  setInterval(function () { api.post('/api/heartbeat').catch(function () { /* ignore */ }); }, 10000);

  // ---- boot ---------------------------------------------------------------
  on('report-loaded', function () { markStep('report'); });
  var first = $('#splashSteps li'); if (first) first.classList.add('active');
  var initial = (location.hash || '').replace('#', '') || store('erpdesk.view') || 'reporting';
  setView(initial, { silent: true });
  try { history.replaceState(null, '', '#' + current); } catch (e) { /* ignore */ }
  // Fonts change the row heights the indicator is measured from.
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(function () { moveIndicator(current); });
})();
