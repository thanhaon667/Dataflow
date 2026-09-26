/* ERP Desk - Data Flow page.
   Draws the pipeline described in desktop/flow_definition.py (served, together with live numbers, health and
   "is it really automated?" state, by GET /api/flow). Nothing about the system is hard-coded here: the map,
   the lanes, the triggers and the wording all come from the feed, so extending the map is a Python-only edit.

   Layers inside one design-space canvas that is scaled to fit the window:
     lanes (div)  ->  edges (svg)  ->  particles (canvas, glowing dots that travel along the edges)  ->  nodes (buttons)
   Every string that comes from the feed is escaped before it reaches innerHTML.

   This file also owns the 2D | 3D switch (default: 3D). The 3D scene (static/flow3d/*.js, window.Flow3D) draws the
   same feed; it is handed the data, the details drawer, the tooltip builders and flowParams() through the small
   `api3d` object below, so nothing about the data pipeline is duplicated. If WebGL is missing, the module fails to
   start, or the OS asks for reduced motion, the 2D map below is used instead. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var esc = D.esc;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var reduced = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : { matches: false };

  // ------------------------------------------------------------------ vocabulary
  var ICONS = {
    form: '<rect x="4" y="3" width="16" height="18" rx="3"/><path d="M8 8h8M8 12h8M8 16h5"/>',
    webhook: '<circle cx="12" cy="12" r="2.6"/><path d="M4 12h5M15 12h5M12 4v5M12 15v5"/>',
    ticket: '<path d="M3 7h18v3a2 2 0 0 0 0 4v3H3v-3a2 2 0 0 0 0-4z"/><path d="M14 7v10" stroke-dasharray="2 2"/>',
    logic: '<circle cx="6" cy="6" r="2"/><circle cx="6" cy="18" r="2"/><circle cx="18" cy="12" r="2"/><path d="M6 8v8M8 6h3a5 5 0 0 1 5 5M8 18h3a5 5 0 0 0 5-5"/>',
    db: '<ellipse cx="12" cy="6" rx="7" ry="3"/><path d="M5 6v6c0 1.7 3.1 3 7 3s7-1.3 7-3V6M5 12v6c0 1.7 3.1 3 7 3s7-1.3 7-3v-6"/>',
    views: '<path d="M2 12s3.6-6 10-6 10 6 10 6-3.6 6-10 6S2 12 2 12z"/><circle cx="12" cy="12" r="2.6"/>',
    brain: '<path d="M11 3l1.8 4.7L17.5 9.5l-4.7 1.8L11 16l-1.8-4.7L4.5 9.5l4.7-1.8z"/><path d="M18 14l.8 2.2L21 17l-2.2.8L18 20l-.8-2.2L15 17l2.2-.8z"/>',
    task: '<rect x="4" y="4" width="16" height="16" rx="3"/><path d="M8 12l3 3 5-6"/>',
    clickup: '<path d="M5 15l4-3 3 2.5L15 12l4 3"/><path d="M6.5 9L12 5l5.5 4"/>',
    pull: '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
    users: '<circle cx="9" cy="8" r="3"/><path d="M3 20c0-3.3 2.7-6 6-6s6 2.7 6 6"/><circle cx="17" cy="9" r="2.4"/><path d="M17 14c2.4 0 4 1.8 4 4"/>',
    report: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h7"/>',
    digest: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M7 9h5M7 13h10M7 16h6"/>',
    mail: '<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>',
    check: '<rect x="3" y="3" width="18" height="18" rx="4"/><path d="M8 12l3 3 5-6"/>',
    file: '<path d="M7 3h7l4 4v14H7z"/><path d="M14 3v5h4"/>',
    chart: '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    chart2: '<path d="M3 20h18M6 16l4-5 3 3 5-7"/>',
    terminal: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M7 10l3 2-3 2M12 15h5"/>',
    inbox: '<path d="M3 13l3-8h12l3 8v6H3z"/><path d="M3 13h5l1 3h6l1-3h5"/>',
    dash: '<rect x="3" y="3" width="8" height="8" rx="2"/><rect x="13" y="3" width="8" height="5" rx="2"/><rect x="13" y="10" width="8" height="11" rx="2"/><rect x="3" y="13" width="8" height="8" rx="2"/>',
    pbi: '<rect x="5" y="12" width="4" height="8" rx="1"/><rect x="10" y="6" width="4" height="14" rx="1"/><rect x="15" y="3" width="4" height="17" rx="1"/>',
    play: '<path d="M7 4l13 8-13 8z"/>', stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    x: '<path d="M6 6l12 12M18 6L6 18"/>', open: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
    gantt: '<path d="M4 6h9M8 12h11M6 18h8"/>',
    warn: '<path d="M12 3l10 18H2z"/><path d="M12 10v5M12 18h.01"/>', ok: '<path d="M5 12l4 4 10-10"/>'
  };
  function svgIcon(name, cls) { return '<svg viewBox="0 0 24 24" aria-hidden="true"' + (cls ? ' class="' + cls + '"' : '') + '>' + (ICONS[name] || ICONS.file) + '</svg>'; }

  var TRIG = {
    event: { label: 'Event-driven', short: 'Event', color: '#ff6a48', glyph: '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>' },
    scheduled: { label: 'Scheduled (Task Scheduler)', short: 'Scheduled', color: '#6f86ff', glyph: '<circle cx="12" cy="12" r="8"/><path d="M12 7v5l3 2"/>' },
    background: { label: 'Background (ERP Desk loop)', short: 'Background', color: '#a88fff', glyph: '<path d="M17 7a6 6 0 1 0 1.5 5M17 3v4h-4"/>' },
    manual: { label: 'Manual', short: 'Manual', color: '#aeb3bb', glyph: '<path d="M6 3l12 6-5 2-2 5z"/>' },
    passive: { label: 'Read on demand', short: 'On read', color: '#7d828b', glyph: '<circle cx="12" cy="12" r="3"/><circle cx="12" cy="12" r="8"/>' },
    external: { label: 'Outside this machine', short: 'External', color: '#aeb3bb', glyph: '<path d="M9 5h10v10M19 5L8 16"/>' }
  };
  function trigGlyph(k) { return '<svg viewBox="0 0 24 24" aria-hidden="true">' + (TRIG[k] || TRIG.manual).glyph + '</svg>'; }

  var STATE = {
    healthy: 'HEALTHY', stale: 'STALE', error: 'ERROR', 'never-run': 'NO RUN YET', mocked: 'MOCKED', offline: 'OFFLINE',
    idle: 'IDLE', unavailable: 'UNAVAILABLE', info: 'STATIC'
  };
  var STATE_LONG = {
    healthy: 'Healthy', stale: 'Stale - older than expected', error: 'Error - last attempt failed', 'never-run': 'Never run / no record',
    mocked: 'Mocked - running on a fallback, not the real service', offline: 'Offline - not running right now', idle: 'Idle - on-demand tool, not open',
    unavailable: 'Unavailable - could not read its data', info: 'Static description'
  };
  var AUTO_TEXT = {
    active: 'Automated - running by itself right now', partial: 'Partly automated - only some of its jobs run by themselves right now', available: 'Automatable - but NOT running by itself right now',
    manual: 'Manual - a person has to start it', passive: 'No process - read whenever something needs it', external: 'Happens outside this machine'
  };
  var CHARS = { event: '#ff6a48', scheduled: '#6f86ff', background: '#a88fff', manual: '#c3c8cf', passive: '#8f949c', err: '#ff5a5f', white: '#ffffff' };

  // ------------------------------------------------------------------ state
  var S = {
    snap: null, data: null, version: null, refreshedAt: null, serverError: null, live: 'connecting', fails: 0, pollTimer: null,
    built: false, activated: false, N: {}, E: {}, order: [], L: null, geom: null, scale: 1, H: 0,
    sel: null, hover: null, prevSummary: null, sig: {}, lastChangeAt: null,
    raf: null, lastTs: 0, fade: 0, comets: []
  };
  var T = { running: false, i: 0, timer: null, steps: [], edgesOn: [] };
  var MODE_KEY = 'erpdesk.flow.mode';           // '2d' | '3d' (only a real choice is remembered, never an automatic fallback)
  S.mode = '2d';
  function F3() { return window.Flow3D || null; }

  // ------------------------------------------------------------------ formatting
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function absTime(iso) { var d = new Date(Date.parse(iso)); return isNaN(d) ? '' : d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()); }
  function rel(iso) {
    var t = Date.parse(iso); if (isNaN(t)) return '-';
    var s = Math.round((Date.now() - t) / 1000);
    if (s < 45) return 'just now';
    if (s < 5400) return Math.max(1, Math.round(s / 60)) + 'm ago';
    if (s < 172800) return Math.round(s / 3600) + 'h ago';
    return Math.round(s / 86400) + 'd ago';
  }
  function num(v, dec) { return Number(v).toLocaleString('en-US', { minimumFractionDigits: dec || 0, maximumFractionDigits: dec || 0 }); }
  function fmtVal(v, fmt) {            // -> [text, class]
    if (v === null || v === undefined || v === '') return ['-', ''];
    switch (fmt) {
      case 'int': return [num(v), ''];
      case 'dec1': return [num(v, 1), ''];
      case 'time': return [rel(v), 'sm'];
      case 'bool_up': return [v ? 'up' : 'down', v ? 'yes' : 'no'];
      case 'bool_yes': return [v ? 'yes' : 'no', v ? 'yes' : 'no'];
      case 'bool_on': return [v ? 'on' : 'off', v ? 'yes' : 'no'];
      case 'bool_ok': return [v ? 'ok' : 'missing', v ? 'yes' : 'no'];
      default: return [String(v), 'sm'];
    }
  }
  function headline(n) {
    var h = n.headline;
    if (!h) return '';
    if (h.text) return h.text;
    var v = fmtVal(h.value, h.fmt)[0];
    if (v === '-') return 'no data';
    return h.short ? ((h.fmt === 'time' || /^bool/.test(h.fmt || '')) ? h.short + ' ' + v : v + ' ' + h.short) : v;
  }
  function tween(el, to) {
    var from = el._v == null ? 0 : el._v; el._v = to;
    if (el._raf) cancelAnimationFrame(el._raf);
    if (from === to || document.hidden || reduced.matches) { el.textContent = num(to); return; }
    var t0 = null;
    (function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / 800, 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = num(Math.round(from + (to - from) * e));
      if (p < 1) el._raf = requestAnimationFrame(step);
    })(performance.now());
  }

  // ------------------------------------------------------------------ polling / live pill
  function pollSec() {
    var v = parseInt(D.store('erpdesk.poll') || '', 10);           // same setting as the Reporting page
    if (isNaN(v)) v = parseInt((document.querySelector('meta[name="erp-refresh-seconds"]') || {}).content || '10', 10) || 10;
    return [0, 5, 10, 30, 60].indexOf(v) < 0 ? 10 : v;
  }
  function setLive(state) {
    S.live = state;
    $('#flowPill').setAttribute('data-state', state);
    $('#flowLiveLabel').textContent = { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', paused: 'PAUSED', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#flowAgo'), pill = $('#flowPill');
    if (!S.refreshedAt) { el.textContent = ''; return; }
    var s = Math.max(0, Math.round((Date.now() - S.refreshedAt) / 1000));
    el.textContent = 'synced ' + (s < 60 ? s + 's' : Math.floor(s / 60) + 'm') + ' ago';
    var sec = pollSec();
    pill.title = 'Feed refreshed ' + absTime(new Date(S.refreshedAt).toISOString()) + (S.serverError ? ' - problem: ' + S.serverError : '') +
      (sec ? ' - page checks every ' + sec + 's (same setting as Reporting)' : ' - auto-refresh paused');
  }
  function active() { return D.view() === 'dataflow'; }
  function schedule() {
    clearTimeout(S.pollTimer);
    var sec = pollSec();
    if (sec <= 0) { setLive('paused'); return; }
    S.pollTimer = setTimeout(function () { if (active()) poll(false); else schedule(); }, sec * 1000);
  }
  function poll(force) {
    clearTimeout(S.pollTimer);
    setLive('syncing');
    var req = force ? D.api.post('/api/flow/refresh') : D.api.get('/api/flow?since=' + encodeURIComponent(S.version || ''));
    return req.then(function (res) {
      S.fails = 0;
      S.refreshedAt = Date.parse(res.refreshed_at) || Date.now();
      S.serverError = res.error || null;
      if (!res.data) {
        showEmpty('Mapping the pipeline…', res.error ? 'The feed reported: ' + res.error : 'Querying the database and the logs.', !res.error);
        setLive(res.error ? 'stale' : 'connecting');
        return;
      }
      var changed = res.changed || S.version !== res.version;
      S.version = res.version;
      apply(res.data, changed);
      setLive(res.error ? 'stale' : 'live');
      if (force) D.toast(changed ? 'Data flow refreshed · something changed' : 'Data flow refreshed · no changes', changed ? 'ok' : '');
    }).catch(function () {
      S.fails++;
      setLive(S.data && S.fails < 3 ? 'stale' : 'offline');
      if (!S.data) showEmpty('Data flow feed unreachable', 'The app server did not answer. Retrying…', false);
      if (S.fails === 1 && force) D.toast('Could not reach the data-flow feed', 'bad');
    }).then(function () { schedule(); });
  }
  function refreshNow() {
    var b = $('#flowRefresh'), rail = $('#btnRefresh');
    b.classList.add('spinning'); rail.classList.add('spinning'); b.disabled = true;
    return poll(true).then(function () { setTimeout(function () { b.classList.remove('spinning'); rail.classList.remove('spinning'); b.disabled = false; }, 500); });
  }
  function showEmpty(title, msg, spin) {
    var box = $('#flowEmpty'); box.hidden = false;
    $('#flowEmptyTitle').textContent = title; $('#flowEmptyMsg').textContent = msg;
    var sp = $('.spinner', box); if (sp) sp.style.display = spin === false ? 'none' : '';
  }

  // ------------------------------------------------------------------ apply a snapshot
  function apply(data, changed) {
    var first = !S.built;
    S.data = data;
    if (first) build(data);
    updateAll(data, first);
    if (first) { $('#flowEmpty').hidden = true; }
    if (changed && !first) S.lastChangeAt = Date.now();
    detectNewLeads(data);
    renderSub(data);
    renderCover(data);
    renderSync(data);
    if (S.mode === '3d' && F3() && F3().mounted()) F3().setData(data);
    if (S.sel && S.N[S.sel]) renderDrawer(S.sel, true);
    $('#flowTrace').disabled = !(data.trace && data.trace.steps && data.trace.steps.length);
  }

  function detectNewLeads(data) {
    var n = data.summary ? data.summary.leads : null;
    if (S.prevSummary != null && n != null && n > S.prevSummary) {
      var d = n - S.prevSummary;
      D.toast('+' + d + ' new lead' + (d > 1 ? 's' : '') + ' entered the pipeline', 'ok');
      if (active() && !T.running && S.mode !== '3d') setTimeout(startTrace, 600);   // 3D shows a shockwave on the edge instead of grabbing the camera
    }
    S.prevSummary = n;
  }

  function renderSub(data) {
    var cov = data.coverage, s = data.summary || {}, bits = [];
    if (!data.db.ok) {
      bits.push('The <b>database is unreachable</b> (' + esc(data.db.error || 'no details') + '), so boxes that depend on it show UNAVAILABLE.');
    } else {
      bits.push('<b>' + num(s.leads || 0) + ' lead' + (s.leads === 1 ? '' : 's') + '</b> have travelled this pipeline.');
    }
    bits.push('<b>' + cov.active + ' of ' + cov.total + '</b> stages run by themselves right now' + (cov.partial ? ' (<b>' + cov.partial + '</b> only partly)' : '') + '; <b>' + cov.available + '</b> could but are not running, and <b>' + cov.manual + '</b> are manual by design.');
    if (!data.probe.webhook) bits.push('The lead webhook is <b>not listening</b>, so new leads would not be picked up.');
    $('#flowSub').innerHTML = bits.join(' ');
  }

  // ------------------------------------------------------------------ summary cards
  function renderCover(data) {
    var cov = data.coverage, tot = Math.max(1, cov.total), s = data.summary || {}, sch = data.scheduler || {};
    var r = 46, c = 2 * Math.PI * r;
    var segs = [{ n: cov.active, col: '#12b886' }, { n: cov.partial || 0, col: '#7fd6b8' }, { n: cov.available, col: '#f2b705' }, { n: cov.manual, col: '#b9bdc4' }], off = 0, circles = '';
    segs.forEach(function (sg) {
      var len = c * sg.n / tot;
      circles += '<circle cx="56" cy="56" r="' + r + '" stroke="' + sg.col + '" stroke-dasharray="' + Math.max(0, len - 2).toFixed(1) + ' ' + c.toFixed(1) + '" stroke-dashoffset="' + (-off).toFixed(1) + '"/>';
      off += len;
    });
    var webhookUp = data.probe.webhook;
    var card1 = '<article class="fc" id="fcCov"><h3>Automation coverage<span class="hint">' + cov.total + ' stages</span></h3><div class="cov">' +
      '<div class="cov-ring"><svg viewBox="0 0 112 112"><circle class="track" cx="56" cy="56" r="' + r + '"/>' + circles + '</svg>' +
      '<div class="center"><b>' + cov.active + '/' + cov.total + '</b><span>AUTOMATED</span></div></div>' +
      '<div class="cov-rows">' +
      covRow('#12b886', 'Running by itself now', cov.active, 'background loops, live listeners') +
      (cov.partial ? covRow('#7fd6b8', 'Partly running', cov.partial, 'one job runs, another does not') : '') +
      covRow('#f2b705', 'Automatable, not running', cov.available, webhookUp ? 'not scheduled yet' : 'webhook off, jobs not scheduled') +
      covRow('#b9bdc4', 'Manual by design', cov.manual, 'button, .bat or command') + '</div></div></article>';

    var todo = [];
    if (!data.probe.webhook) todo.push({ id: 'webhook', title: 'Lead webhook is not running', sub: 'Start run_webhook.bat - until then new leads are not received', icon: 'webhook' });
    (cov.not_set_up || []).forEach(function (t) { todo.push({ id: t.node, title: t.label + ' is not scheduled', sub: 'Recommended: ' + t.hint, icon: 'pull' }); });
    var todoHtml = todo.length ? '<ul class="todo">' + todo.map(function (t) {
      return '<li data-node="' + esc(t.id) + '" tabindex="0" role="button"><i>' + svgIcon(t.icon) + '</i><div><b>' + esc(t.title) + '</b><span>' + esc(t.sub) + '</span></div></li>';
    }).join('') + '</ul>' : '<div class="todo-empty"><b>Everything that can run by itself is running.</b><br>Nothing recommended is missing.</div>';
    var schedNote;
    if (sch.pending) schedNote = 'Task Scheduler: checking&hellip;';
    else if (sch.available === false) schedNote = 'Task Scheduler could not be queried' + (sch.error ? ' (' + esc(sch.error) + ')' : '') + ' - scheduled triggers show as unknown.';
    else schedNote = 'Task Scheduler: ' + ((sch.tasks || []).length ? (sch.tasks.length + ' task(s) belong to this project: ' + sch.tasks.map(function (t) { return esc(t.name); }).join(', ')) : '0 tasks belong to this project') + (sch.checked_at ? ' &middot; checked ' + rel(sch.checked_at) : '');
    var card2 = '<article class="fc"><h3>Recommended, not set up<span class="hint">honest check</span></h3>' + todoHtml + '<div class="sched-note">' + schedNote + '</div></article>';

    var items = [['Leads in', s.leads, '#ff5a36'], ['Assigned', s.assigned, '#ff9a4c'], ['AI-analyzed', s.analyzed, '#7c5cff'], ['ClickUp tasks', s.tasks, '#3452eb'], ['Comments back', s.updates, '#12b886']];
    var gaps = [];
    if (s.leads) { if (s.assigned < s.leads) gaps.push((s.leads - s.assigned) + ' without a rep'); if (s.analyzed < s.leads) gaps.push((s.leads - s.analyzed) + ' not AI-analyzed'); if (s.tasks < s.leads) gaps.push((s.leads - s.tasks) + ' without a ClickUp task'); }
    var card3 = '<article class="fc"><h3>What has moved<span class="hint">from the database</span></h3><div class="tally">' + items.map(function (it, i) {
      return '<div class="tally-item" style="--c:' + it[2] + '"><b data-tally="' + i + '">' + (it[1] == null ? '-' : num(it[1])) + '</b><span>' + esc(it[0]) + '</span></div>';
    }).join('') + '<div class="tally-arrow">' + (!data.db.ok ? 'database unavailable' : (gaps.length ? 'Stuck: ' + esc(gaps.join(' · ')) : 'no lead is stuck between stages · comments depend on the Sales rep')) + '</div></div></article>';

    var box = $('#flowCover'), fresh = !box.firstChild;
    var prevVals = fresh ? [] : $$('[data-tally]', box).map(function (b) { return b._v; });
    box.innerHTML = card1 + card2 + card3;
    $$('[data-tally]', box).forEach(function (b, i) { var v = items[i][1]; if (v == null) return; b._v = prevVals[i] == null ? 0 : prevVals[i]; tween(b, v); });
    $$('.todo li', box).forEach(function (li) {
      var go = function () { select(li.getAttribute('data-node')); };
      li.addEventListener('click', go);
      li.addEventListener('keydown', function (e) { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } });
    });
  }
  function covRow(color, label, n, sub) { return '<div class="cov-row" style="--c:' + color + '"><i></i><div>' + esc(label) + '<small>' + esc(sub) + '</small></div><b>' + n + '</b></div>'; }

  // ------------------------------------------------------------------ map sync check
  // The map is declared by hand (desktop/flow_definition.py). The server compares it with the project on every
  // refresh and reports what was forgotten or has gone stale; this draws the answer. All text is escaped.
  var SYNC_MAX_ROWS = 30;
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function syncRows(list, fn) {
    var shown = list.slice(0, SYNC_MAX_ROWS).map(fn).join('');
    if (list.length > SYNC_MAX_ROWS) shown += '<li class="more">&hellip; and ' + (list.length - SYNC_MAX_ROWS) + ' more</li>';
    return '<ul class="fs-list">' + shown + '</ul>';
  }
  function syncGroup(title, n, hint, listHtml) {
    return '<section class="fs-grp"><h4>' + esc(title) + '<em>' + n + '</em></h4><p class="fs-hint">' + hint + '</p>' + listHtml + '</section>';
  }
  function syncProblems(mc) {
    return (mc.unmapped || []).length + (mc.stale_nodes || []).length + (mc.stale_ignores || []).length + (mc.integrity || []).length;
  }
  function renderSync(data) {
    var mc = data.map_check, box = $('#flowSync'), pill = $('#flowMapPill');
    if (!mc) { box.hidden = true; pill.hidden = true; S.syncSig = null; return; }
    var problems = syncProblems(mc);
    var sig = JSON.stringify([mc.available, mc.error, mc.in_sync, mc.mapped, mc.scanned, mc.unmapped, mc.stale_nodes, mc.stale_ignores, mc.integrity, mc.ignored, mc.definition_changed]);

    // header pill: only when something needs attention (or the check itself could not run)
    pill.hidden = !(mc.available === false || problems > 0);
    if (mc.available === false) {
      pill.className = 'map-pill na'; pill.textContent = 'map check unavailable';
      pill.title = 'The check that compares the map with the project could not run' + (mc.error ? ' (' + mc.error + ')' : '') + '. Click to see details.';
    } else if (problems > 0) {
      pill.className = 'map-pill'; pill.textContent = 'map out of date · ' + problems;
      pill.title = plural(problems, 'thing') + ' on the map ' + (problems === 1 ? 'is' : 'are') + ' out of date. Click to see what to fix.';
    }
    if (S.syncSig === sig) return;           // unchanged: leave the DOM (and an open <details>) alone
    S.syncSig = sig;
    var oldDetails = box.querySelector('details'), wasOpen = !!(oldDetails && oldDetails.open);
    box.hidden = false;
    var restart = mc.definition_changed ? '<p class="fs-restart"><b>desktop/flow_definition.py was edited after ERP Desk started.</b> Restart ERP Desk (Quit, then open it again) to load the new map.</p>' : '';

    if (mc.available === false) {
      box.innerHTML = '<div class="fs-ok na">' + svgIcon('warn') + '<b>Map check unavailable</b><span>' + esc(mc.error || 'The project could not be scanned.') + ' The rest of this page is not affected.</span></div>';
      return;
    }
    if (problems === 0) {
      var ign = mc.ignored || [];
      box.innerHTML = '<div class="fs-ok">' + svgIcon('ok') + '<b>Map is in sync</b><span>' + esc(plural(mc.mapped, 'script') + ' mapped') +
        (ign.length ? ' &middot; ' + esc(ign.length + ' ignored on purpose') : '') + '</span>' +
        (ign.length ? '<details class="fs-ign"><summary>see the IGNORE list</summary>' + syncRows(ign, function (x) {
          return '<li><code>' + esc(x.path) + '</code><span>' + esc(x.reason) + '</span></li>';
        }) + '</details>' : '') + restart + '</div>';
      var d0 = box.querySelector('details'); if (d0 && wasOpen) d0.open = true;
      return;
    }

    var groups = '';
    if ((mc.unmapped || []).length) groups += syncGroup('Scripts that are not on the map', mc.unmapped.length,
      'No node covers these files. Add a node (and its edges) for each one, or add it to <code>IGNORE</code> with a reason.',
      syncRows(mc.unmapped, function (x) { return '<li><code>' + esc(x.path) + '</code><span>' + (x.doc ? esc(x.doc) : '<i>no docstring</i>') + '</span></li>'; }));
    if ((mc.stale_nodes || []).length) groups += syncGroup('Nodes that point to a missing file', mc.stale_nodes.length,
      'The file was moved, renamed or deleted. Fix the path in <code>NODES</code>, or remove the node.',
      syncRows(mc.stale_nodes, function (x) { return '<li><code>' + esc(x.path) + '</code><span>node &ldquo;' + esc(x.label || x.node) + '&rdquo; &middot; ' + esc(x.reason) + '</span></li>'; }));
    if ((mc.stale_ignores || []).length) groups += syncGroup('IGNORE list needs tidying', mc.stale_ignores.length,
      'Entries in <code>IGNORE</code> that no longer make sense.',
      syncRows(mc.stale_ignores, function (x) { return '<li><code>' + esc(x.path) + '</code><span>' + esc(x.reason) + '</span></li>'; }));
    if ((mc.integrity || []).length) groups += syncGroup('The map contradicts itself', mc.integrity.length,
      'Fix the entry named on each row in <code>NODES</code> / <code>EDGES</code>; until then the page may show it as &ldquo;unknown&rdquo;.',
      syncRows(mc.integrity, function (x) { return '<li><span class="full">' + esc(x.message) + '</span></li>'; }));

    box.innerHTML = '<article class="fc fs-card"><h3><span class="fs-ttl"><i>' + svgIcon('warn') + '</i>The map is out of date</span>' +
      '<span class="hint">' + esc(plural(problems, 'thing') + ' to fix') + ' &middot; ' + esc(mc.mapped + ' of ' + mc.scanned + ' scripts mapped') + '</span></h3>' +
      '<p class="fs-lead">This map is drawn by hand, so it only knows what it has been told. New or renamed scripts do not appear on it by themselves &mdash; this check compares it with the project every time the page refreshes.</p>' +
      '<div class="fs-cols">' + groups + '</div>' +
      '<div class="fs-fix"><b>What to do</b><span>' + esc(mc.hint || 'Edit desktop/flow_definition.py, then restart ERP Desk.') + '</span></div>' + restart + '</article>';
  }
  $('#flowMapPill').addEventListener('click', function () {
    var box = $('#flowSync'); if (box.hidden) return;
    try { box.scrollIntoView({ behavior: reduced.matches ? 'auto' : 'smooth', block: 'start' }); } catch (e) { box.scrollIntoView(); }
  });

  // ------------------------------------------------------------------ geometry + build
  function build(data) {
    var L = data.layout; S.L = L;
    var nodes = data.nodes, edges = data.edges;
    var colPitch = (L.width - L.pad_x - L.pad_r - L.node_w) / (L.cols - 1);
    var maxBottom = 0; nodes.forEach(function (n) { maxBottom = Math.max(maxBottom, L.top + n.row * L.row_pitch + (n.h || L.node_h)); });
    var H = Math.ceil(maxBottom + 46);
    S.geom = { colPitch: colPitch, H: H }; S.H = H;
    var X = function (col) { return L.pad_x + col * colPitch; };
    var Y = function (row) { return L.top + row * L.row_pitch; };
    S.X = X; S.Y = Y;
    var canvas = $('#flowCanvas');
    canvas.style.width = L.width + 'px'; canvas.style.height = H + 'px';

    // lanes
    var html = '';
    data.lanes.forEach(function (ln, i) {
      var c0 = Math.min.apply(null, ln.cols), c1 = Math.max.apply(null, ln.cols);
      var left = X(c0) - 14, width = X(c1) + L.node_w + 14 - left;
      html += '<div class="lane" style="--lc:' + esc(ln.color) + ';--i:' + i + ';left:' + left + 'px;width:' + width + 'px;height:' + (H - 24) + 'px"><div class="lane-n">' + esc(ln.n) + '</div><div class="lane-head"><div class="lane-t">' + esc(ln.title) + '</div><div class="lane-s">' + esc(ln.tag) + '</div></div></div>';
    });

    // node lookup + ports
    S.N = {}; S.order = [];
    nodes.forEach(function (n) { S.N[n.id] = { d: n, x: X(n.col), y: Y(n.row), w: L.node_w, h: n.h || L.node_h }; S.order.push(n.id); });
    var ports = {};
    edges.forEach(function (e) {
      var a = S.N[e.from], b = S.N[e.to]; if (!a || !b) return;
      e.mode2 = 'h';
      if (a.d.col === b.d.col) {   // same column: a straight vertical link unless another node sits in between, then a side loop
        var lo = Math.min(a.y, b.y), hi = Math.max(a.y, b.y), blocked = false;
        Object.keys(S.N).forEach(function (k) { var o = S.N[k]; if (k !== e.from && k !== e.to && o.d.col === a.d.col && o.y > lo && o.y < hi) blocked = true; });
        e.mode2 = blocked ? 'loop' : 'v';
        e.sa = e.sb = blocked ? 'r' : (a.y < b.y ? 'b' : 't');
        if (!blocked) { e.sb = a.y < b.y ? 't' : 'b'; return; }
      } else {
        e.sa = a.d.col < b.d.col ? 'r' : 'l'; e.sb = a.d.col < b.d.col ? 'l' : 'r';
      }
      var v = e.via || [];
      var refA = v.length ? Y(v[0][1]) : b.y + b.h / 2, refB = v.length ? Y(v[v.length - 1][1]) : a.y + a.h / 2;
      (ports[e.from + '|' + e.sa] = ports[e.from + '|' + e.sa] || []).push({ e: e, end: 'a', ref: refA });
      (ports[e.to + '|' + e.sb] = ports[e.to + '|' + e.sb] || []).push({ e: e, end: 'b', ref: refB });
    });
    Object.keys(ports).forEach(function (k) {
      var list = ports[k].sort(function (p, q) { return p.ref - q.ref; }), nd = S.N[k.split('|')[0]], n = list.length;
      list.forEach(function (p, i) { p.e['y' + p.end] = nd.y + 9 + (nd.h - 18) * (i + 1) / (n + 1); });
    });

    // edges (svg)
    var defs = '<defs>' + Object.keys(CHARS).filter(function (k) { return k !== 'white'; }).map(function (k) {
      return '<marker id="arr-' + k + '" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="8" markerHeight="8" markerUnits="userSpaceOnUse" orient="auto"><path d="M0 0.5L7.5 4 0 7.5z" fill="' + CHARS[k] + '"/></marker>';
    }).join('') + '</defs>';
    var svg = '<svg class="edges" width="' + L.width + '" height="' + H + '" viewBox="0 0 ' + L.width + ' ' + H + '">' + defs;
    S.E = {};
    edges.forEach(function (e, i) {
      var a = S.N[e.from], b = S.N[e.to]; if (!a || !b) return;
      var d = pathFor(e, a, b, L, colPitch);
      S.E[e.id] = { d: e, path: d, i: i };
      svg += '<g class="edge drawing" data-id="' + esc(e.id) + '" style="--d:' + (650 + i * 55) + 'ms"><path class="e-glow" d="' + d + '"/><path class="e-base" d="' + d + '"/><path class="e-hit" d="' + d + '"/></g>';
    });
    svg += '</svg>';

    // particles canvas
    var cv = '<canvas class="particles" id="flowParticles"></canvas>';

    // nodes
    var nhtml = '';
    nodes.forEach(function (n) {
      var g = S.N[n.id];
      var delay = 260 + (['intake', 'storage', 'integrations', 'reporting'].indexOf(n.lane)) * 170 + n.row * 45;
      nhtml += '<button class="fnode" id="fn-' + esc(n.id) + '" data-id="' + esc(n.id) + '" data-kind="' + esc(n.kind) + '" style="left:' + g.x + 'px;top:' + g.y + 'px;width:' + g.w + 'px;height:' + g.h + 'px;--d:' + Math.round(delay) + 'ms" aria-label="' + esc(n.label) + '">' +
        '<span class="fn-ic">' + svgIcon(n.icon) + '<i class="fn-dot"></i></span><span class="fn-txt"><b>' + esc(n.label) + '</b><em></em></span><span class="fn-badge"></span><span class="fn-tag"></span></button>';
    });
    canvas.innerHTML = html + svg + cv + nhtml;
    canvas.classList.add('pre');

    // sample paths for the particles
    $$('.edge', canvas).forEach(function (g) {
      var rec = S.E[g.getAttribute('data-id')]; rec.el = g; rec.base = $('.e-base', g); rec.glow = $('.e-glow', g);
      var p = rec.base, len = p.getTotalLength(), step = 3, n = Math.max(2, Math.ceil(len / step)), xs = new Float32Array(n + 1), ys = new Float32Array(n + 1);
      for (var i = 0; i <= n; i++) { var pt = p.getPointAtLength(len * i / n); xs[i] = pt.x; ys[i] = pt.y; }
      rec.len = len; rec.xs = xs; rec.ys = ys; rec.ps = []; rec.flowSig = '';
      rec.base.setAttribute('pathLength', '1'); rec.glow.setAttribute('pathLength', '1');
      rec.base.addEventListener('animationend', function () { finishDraw(rec); });
      if (reduced.matches) finishDraw(rec);   // no draw-in animation to wait for
    });
    S.built = true;
    bindCanvas(canvas);
    fit();
    if (active()) activate();
    syncMode();
  }

  function finishDraw(rec) {
    rec.el.classList.remove('drawing'); rec.base.removeAttribute('pathLength'); rec.glow.removeAttribute('pathLength');
    rec.base.setAttribute('marker-end', 'url(#arr-' + (rec.d.driver_health === 'error' ? 'err' : rec.d.trigger) + ')');
  }

  function pathFor(e, a, b, L, colPitch) {
    var f = function (v) { return Math.round(v * 10) / 10; };
    if (e.mode2 === 'v') {
      var cx = a.x + a.w / 2, y1 = e.sa === 'b' ? a.y + a.h : a.y, y2 = e.sb === 't' ? b.y : b.y + b.h;
      return 'M' + f(cx) + ' ' + f(y1) + 'L' + f(cx) + ' ' + f(y2);
    }
    var ax = e.sa === 'r' ? a.x + a.w : a.x, ay = e.ya != null ? e.ya : a.y + a.h / 2;
    var bx = e.sb === 'l' ? b.x : b.x + b.w, by = e.yb != null ? e.yb : b.y + b.h / 2;
    if (e.mode2 === 'loop') {
      var bl = 34 + Math.min(20, Math.abs(by - ay) * 0.05);
      return 'M' + f(ax) + ' ' + f(ay) + 'C' + f(ax + bl) + ' ' + f(ay) + ' ' + f(bx + bl) + ' ' + f(by) + ' ' + f(bx) + ' ' + f(by);
    }
    var pts = [[ax, ay]];
    (e.via || []).forEach(function (v) { pts.push([L.pad_x + L.node_w / 2 + v[0] * colPitch, S.Y(v[1])]); });
    pts.push([bx, by]);
    var d = 'M' + f(pts[0][0]) + ' ' + f(pts[0][1]);
    for (var i = 1; i < pts.length; i++) {
      var p0 = pts[i - 1], p1 = pts[i], dx = (p1[0] - p0[0]) * 0.5;
      d += 'C' + f(p0[0] + dx) + ' ' + f(p0[1]) + ' ' + f(p1[0] - dx) + ' ' + f(p1[1]) + ' ' + f(p1[0]) + ' ' + f(p1[1]);
    }
    return d;
  }

  function fit() {
    if (!S.built) return;
    var vp = $('#flowViewport'), L = S.L;
    var avail = vp.clientWidth - 28;
    var s = Math.max(0.66, Math.min(1.12, avail / L.width));
    S.scale = s;
    var fitEl = $('#flowFit');
    fitEl.style.width = Math.round(L.width * s) + 'px'; fitEl.style.height = Math.round(S.H * s) + 'px';
    $('#flowCanvas').style.transform = 'scale(' + s + ')';
    var cv = $('#flowParticles'), dpr = Math.min(2, window.devicePixelRatio || 1);
    cv.style.width = L.width + 'px'; cv.style.height = S.H + 'px';
    cv.width = Math.round(L.width * s * dpr); cv.height = Math.round(S.H * s * dpr);
    S.ctx = cv.getContext('2d'); S.ctx.setTransform(s * dpr, 0, 0, s * dpr, 0, 0);
    if (reduced.matches) drawFrame(0);
  }
  window.addEventListener('resize', function () { requestAnimationFrame(fit); });
  if (window.ResizeObserver) new ResizeObserver(function () { requestAnimationFrame(fit); }).observe($('#flowViewport'));

  // ------------------------------------------------------------------ live update of an existing build
  function updateAll(data, first) {
    var byId = {}; data.nodes.forEach(function (n) { byId[n.id] = n; });
    data.nodes.forEach(function (n) {
      var g = S.N[n.id]; if (!g) return;
      g.d = n;
      var el = $('#fn-' + n.id), h = n.health || {}, hl = headline(n);
      var pulse = h.state === 'healthy' && ((h.last_ok_at && Date.now() - Date.parse(h.last_ok_at) < 20 * 60000) || n.id === 'live_report');
      var tk = n.trigger_kind, armedNow = n.automation === 'active' || n.automation === 'partial';
      var sig = [h.state, n.automation, tk, hl, pulse].join('|');
      if (S.sig[n.id] === sig) return;
      var prev = S.sig[n.id]; S.sig[n.id] = sig;
      el.setAttribute('data-state', h.state || 'info'); el.setAttribute('data-auto', n.automation); el.setAttribute('data-tk', tk);
      $('em', el).textContent = hl;
      el.classList.toggle('pulse', !!pulse && !reduced.matches);
      var bd = $('.fn-badge', el);
      bd.className = 'fn-badge badge-' + tk + (armedNow || n.automation === 'manual' || n.automation === 'passive' || n.automation === 'external' ? '' : ' off');
      bd.innerHTML = trigGlyph(tk);
      bd.title = TRIG[tk] ? TRIG[tk].label + (n.automation === 'available' ? ' (not running now)' : (n.automation === 'partial' ? ' (some jobs not running now)' : '')) : '';
      $('.fn-tag', el).textContent = STATE[h.state] || '';
      el.title = '';
      if (prev && S.activated) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
    });
    data.edges.forEach(function (e) {
      var rec = S.E[e.id]; if (!rec) return;
      rec.d = e;
      var g = rec.el, err = e.driver_health === 'error';
      g.setAttribute('data-mode', e.mode); g.setAttribute('data-tr', e.trigger);
      if (err) g.setAttribute('data-err', '1'); else g.removeAttribute('data-err');
      if (!g.classList.contains('drawing')) rec.base.setAttribute('marker-end', 'url(#arr-' + (err ? 'err' : e.trigger) + ')');
      configureParticles(rec);
    });
    applyFocus();
    if (reduced.matches) drawFrame(0);   // no animation loop: paint the dots once, at rest
  }

  // ------------------------------------------------------------------ particles
  function edgeActivity(e) {
    var act = 0;
    if (e.last_at) { var h = (Date.now() - Date.parse(e.last_at)) / 36e5; act = Math.exp(-h / 18); }
    if (e.n24h) act = Math.max(act, Math.min(1, 0.5 + e.n24h / 10));
    return act;
  }
  function flowParams(e) {
    var act = edgeActivity(e), none = e.total === 0;
    var p = { count: 0, speed: 0, r: 2, alpha: 1, color: e.driver_health === 'error' ? CHARS.err : (CHARS[e.trigger] || CHARS.manual) };
    if (none) return p;
    var spacing;
    if (e.mode === 'live') { spacing = 40; p.speed = 58 + 72 * act; p.alpha = .95; p.r = 2.5; }
    else if (e.mode === 'dormant' || e.mode === 'unknown') { spacing = 110; p.speed = 24 + 30 * act; p.alpha = .55; p.r = 2.1; }
    else if (e.mode === 'passive') { spacing = 150; p.speed = 22; p.alpha = .38; p.r = 1.7; }
    else { spacing = 180; p.speed = 16 + 24 * act; p.alpha = .6; p.r = 1.9; }
    p.spacing = spacing;
    return p;
  }
  function configureParticles(rec) {
    var fp = flowParams(rec.d); rec.fp = fp;
    var want = fp.spacing ? Math.max(1, Math.min(16, Math.round(rec.len / fp.spacing))) : 0;
    if (rec.ps.length !== want) {
      rec.ps = [];
      for (var i = 0; i < want; i++) rec.ps.push({ t: (i + Math.random() * 0.5) / want });
    }
  }
  var sprites = {};
  function sprite(color) {
    if (sprites[color]) return sprites[color];
    var c = document.createElement('canvas'); c.width = c.height = 48;
    var x = c.getContext('2d'), g = x.createRadialGradient(24, 24, 0, 24, 24, 24);
    g.addColorStop(0, '#fff'); g.addColorStop(.16, color); g.addColorStop(.42, color + '66'); g.addColorStop(1, color + '00');
    x.fillStyle = g; x.fillRect(0, 0, 48, 48);
    return (sprites[color] = c);
  }
  function drawFrame(dt) {
    var ctx = S.ctx; if (!ctx) return;
    var L = S.L;
    ctx.clearRect(0, 0, L.width, S.H);
    ctx.globalCompositeOperation = 'lighter';
    var fade = reduced.matches ? 1 : Math.min(1, S.fade);
    Object.keys(S.E).forEach(function (id) {
      var rec = S.E[id]; if (!rec.fp || !rec.ps.length || rec.el.classList.contains('drawing')) return;
      var dim = rec.el.classList.contains('dim') ? .08 : 1, hot = rec.el.classList.contains('hot') ? 1.35 : 1;
      var sp = sprite(rec.fp.color), n = rec.xs.length - 1, r = rec.fp.r * 3.1 * (hot > 1 ? 1.25 : 1);
      for (var i = 0; i < rec.ps.length; i++) {
        var p = rec.ps[i];
        if (dt) { p.t += rec.fp.speed * dt / rec.len; if (p.t > 1) p.t -= 1; }
        var k = Math.min(n, Math.floor(p.t * n)), env = Math.min(1, p.t * 9, (1 - p.t) * 9);
        ctx.globalAlpha = Math.max(0, Math.min(1, rec.fp.alpha * env * dim * fade * hot));
        ctx.drawImage(sp, rec.xs[k] - r, rec.ys[k] - r, r * 2, r * 2);
      }
    });
    // trace comets
    var now = performance.now();
    S.comets = S.comets.filter(function (c) { return now - c.t0 < c.dur + 100; });
    S.comets.forEach(function (c) {
      var rec = S.E[c.edge]; if (!rec) return;
      var n = rec.xs.length - 1, p = Math.min(1, (now - c.t0) / c.dur);
      for (var j = 0; j < 14; j++) {
        var tp = Math.max(0, p - j * 0.012), k = Math.min(n, Math.floor(tp * n)), rr = (13 - j * 0.7) * 1.15;
        ctx.globalAlpha = Math.max(0, (1 - j / 14) * .95);
        ctx.drawImage(sprite(CHARS.white), rec.xs[k] - rr, rec.ys[k] - rr, rr * 2, rr * 2);
      }
    });
    ctx.globalAlpha = 1; ctx.globalCompositeOperation = 'source-over';
  }
  function frame(ts) {
    S.raf = requestAnimationFrame(frame);
    var dt = Math.min(0.05, (ts - S.lastTs) / 1000); S.lastTs = ts;
    S.fade += dt / 1.6;
    drawFrame(dt);
  }
  function startLoop() {
    if (reduced.matches || S.raf || !S.built || !active() || document.hidden || S.mode === '3d') return;
    S.lastTs = performance.now(); S.raf = requestAnimationFrame(frame);
  }
  function stopLoop() { if (S.raf) cancelAnimationFrame(S.raf); S.raf = null; }
  document.addEventListener('visibilitychange', function () { if (document.hidden) stopLoop(); else startLoop(); });
  var onReducedChange = function () {
    if (reduced.matches && S.mode === '3d') setMode('2d', false, 'Reduced motion is on, so the calm 2D map is shown.');
    else if (!reduced.matches && S.mode === '2d' && D.store(MODE_KEY) !== '2d') setMode('3d', false);
    stopLoop(); if (reduced.matches) { $$('.fnode').forEach(function (n) { n.classList.remove('pulse'); }); S.sig = {}; if (S.data) updateAll(S.data, false); drawFrame(0); } else startLoop();
  };
  if (reduced.addEventListener) reduced.addEventListener('change', onReducedChange);

  // first time the page is actually visible: play the entrance
  function activate() {
    if (S.activated || !S.built) return;
    S.activated = true; S.fade = -0.4;
    requestAnimationFrame(function () { $('#flowCanvas').classList.remove('pre'); });
    startLoop();
  }

  // ------------------------------------------------------------------ hover / focus / tooltip
  function neighbours(id) {
    var ns = {}, es = {}; ns[id] = 1;
    Object.keys(S.E).forEach(function (k) { var e = S.E[k].d; if (e.from === id || e.to === id) { es[k] = 1; ns[e.from] = 1; ns[e.to] = 1; } });
    return { n: ns, e: es };
  }
  function applyFocus() {
    var f = S.hover || S.sel, canvas = $('#flowCanvas'); if (!canvas) return;
    var nb = f && S.N[f] ? neighbours(f) : null, edgeFocus = f && S.E[f] ? f : null;
    $$('.fnode', canvas).forEach(function (el) {
      var id = el.getAttribute('data-id');
      el.classList.toggle('dim', !!(nb && !nb.n[id]) || !!(edgeFocus && S.E[edgeFocus].d.from !== id && S.E[edgeFocus].d.to !== id));
      el.classList.toggle('sel', S.sel === id);
    });
    Object.keys(S.E).forEach(function (k) {
      var g = S.E[k].el; if (!g) return;
      var hot = !!(nb && nb.e[k]) || edgeFocus === k;
      g.classList.toggle('hot', hot);
      g.classList.toggle('dim', !!((nb || edgeFocus) && !hot));
    });
  }
  var tip = $('#tip');
  function moveTip(e) {
    var x = e.clientX + 16, y = e.clientY + 16;
    if (x + 290 > window.innerWidth) x = e.clientX - 296;
    if (y + 170 > window.innerHeight) y = Math.max(8, e.clientY - 150);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function nodeTip(n) {
    var h = n.health || {}, trig = n.triggers.map(function (t) { return (TRIG[t.kind] || {}).short + ': ' + t.label; })[0] || '';
    return '<b>' + esc(n.label) + '</b><br><span class="meta">' + esc(n.file || n.kind) + '</span><br>' +
      esc(AUTO_TEXT[n.automation] || '') + '<br><span class="meta">' + esc(STATE[h.state] || '') + ' &middot; ' + esc(h.detail || '') + '</span><br><span class="meta">click for details</span>';
  }
  function edgeTip(e) {
    var bits = [];
    if (e.total != null) bits.push(num(e.total) + ' so far');
    if (e.last_at) bits.push('last ' + rel(e.last_at));
    var modeText = { live: 'running now', dormant: 'automated by design, not running now', unknown: 'automated by design, cannot be verified from here', manual: 'moves when a person runs it', passive: 'read on demand' }[e.mode] || '';
    return '<b>' + esc(e.label) + '</b> <span class="meta">(' + esc((TRIG[e.trigger] || {}).short || e.trigger) + ')</span><br>' + esc(e.data) + '<br><span class="meta">' + esc(S.N[e.from].d.label) + ' &rarr; ' + esc(S.N[e.to].d.label) + '<br>' + esc(modeText) + (bits.length ? ' &middot; ' + esc(bits.join(' · ')) : '') + (e.mode_why && (e.mode === 'live' || e.mode === 'dormant' || e.mode === 'unknown') ? '<br>' + esc(e.mode_why) : '') + '</span>';
  }
  function bindCanvas(canvas) {
    canvas.addEventListener('mouseover', function (e) {
      var n = e.target.closest('.fnode'), g = e.target.closest('.edge');
      if (n) { S.hover = n.getAttribute('data-id'); tip.innerHTML = nodeTip(S.N[S.hover].d); tip.classList.add('show'); moveTip(e); applyFocus(); }
      else if (g) { var id = g.getAttribute('data-id'); S.hover = id; tip.innerHTML = edgeTip(S.E[id].d); tip.classList.add('show'); moveTip(e); applyFocus(); }
    });
    canvas.addEventListener('mousemove', function (e) { if (tip.classList.contains('show')) moveTip(e); });
    canvas.addEventListener('mouseout', function (e) {
      var to = e.relatedTarget;
      if (to && to.closest && (to.closest('.fnode') || to.closest('.edge')) && to.closest('.fnode') === e.target.closest('.fnode') && to.closest('.edge') === e.target.closest('.edge')) return;
      if (S.hover) { S.hover = null; tip.classList.remove('show'); applyFocus(); }
    });
    canvas.addEventListener('focusin', function (e) { var n = e.target.closest && e.target.closest('.fnode'); if (n) { S.hover = n.getAttribute('data-id'); applyFocus(); } });
    canvas.addEventListener('focusout', function () { S.hover = null; applyFocus(); });
    canvas.addEventListener('click', function (e) {
      var n = e.target.closest('.fnode');
      if (n) { select(n.getAttribute('data-id')); return; }
      var g = e.target.closest('.edge');
      if (g) return;
    });
  }
  $('#flowViewport').addEventListener('click', function (e) { if (e.target.closest('.fnode') || e.target.closest('.edge')) return; closeDrawer(); });

  // ------------------------------------------------------------------ details drawer
  function select(id) {
    if (!S.N[id]) return;
    S.sel = id; renderDrawer(id, false); applyFocus();
    if (D.view() !== 'dataflow') D.go('dataflow');
    D.emit('flow-select', id);
  }
  function closeDrawer() {
    var dr = $('#flowDrawer'); if (!dr.classList.contains('open')) { var had = S.sel != null; S.sel = null; if (had) D.emit('flow-select', null); return; }
    var prev = S.sel; S.sel = null;
    D.emit('flow-select', null);
    dr.classList.remove('open'); dr.setAttribute('aria-hidden', 'true');
    applyFocus();
    if (prev) { var el = $('#fn-' + prev); if (el && document.activeElement === document.body) { try { el.focus({ preventScroll: true }); } catch (e) { /* ignore */ } } }
  }
  function neighbourChips(id) {
    var out = [];
    Object.keys(S.E).forEach(function (k) {
      var e = S.E[k].d;
      if (e.to === id) out.push('<button data-node="' + esc(e.from) + '">' + esc(S.N[e.from].d.label) + ' <em>in &middot; ' + esc(e.label) + '</em></button>');
      if (e.from === id) out.push('<button data-node="' + esc(e.to) + '">' + esc(S.N[e.to].d.label) + ' <em>out &middot; ' + esc(e.label) + '</em></button>');
    });
    return out.join('');
  }
  function renderDrawer(id, keepScroll) {
    var g = S.N[id], n = g.d, h = n.health || {}, dr = $('#flowDrawer');
    var body = $('.fd-body', dr), scroll = keepScroll && body ? body.scrollTop : 0;
    var tk = n.trigger_kind, color = (TRIG[tk] || TRIG.manual).color;
    var trigHtml = n.triggers.map(function (t) {
      var cls = t.armed === true ? 'ok' : (t.armed === false ? 'no' : 'unk');
      var on = t.armed === true || t.kind === 'manual' || t.kind === 'passive' || t.kind === 'external';
      return '<div class="fd-trig"><span class="bd badge-' + esc(t.kind) + (on ? '' : ' off') + '">' + trigGlyph(t.kind) + '</span><div><b>' + esc((TRIG[t.kind] || {}).label || t.kind) + ' &middot; ' + esc(t.label) + '</b>' +
        '<div class="ts ' + cls + '">' + esc(t.state) + (t.recommended ? ' &middot; recommended by the README' : '') + '</div>' + (t.detail ? '<div class="td">' + esc(t.detail) + '</div>' : '') + '</div></div>';
    }).join('');
    var stats = (n.stats || []).map(function (s) {
      var f = fmtVal(s.value, s.fmt); var title = s.fmt === 'time' && s.value ? ' title="' + esc(absTime(s.value)) + '"' : '';
      return '<div class="fd-stat"' + title + '><b class="' + f[1] + '">' + esc(f[0]) + '</b><span>' + esc(s.label) + '</span></div>';
    }).join('');
    var fileHtml = (n.file ? '<div class="fd-file"><code>' + esc(n.file) + '</code>' + (n.script ? '<button class="fd-btn" data-open-script="' + esc(n.script) + '">' + svgIcon('open') + 'Open in Script Center</button>' : '') + '</div>' : '');
    var notes = (h.notes || []).length ? '<ul class="fd-notes">' + h.notes.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '';
    dr.innerHTML =
      '<div class="fd-head" style="--c:' + color + '"><button class="fd-x" aria-label="Close details">' + svgIcon('x') + '</button>' +
      '<div class="fd-top"><span class="fd-ic">' + svgIcon(n.icon) + '</span><div><div class="fd-name">' + esc(n.label) + '</div><div class="fd-kind">' + esc(n.kind) + (n.stage ? ' &middot; pipeline stage' : '') + '</div></div></div>' +
      '<div class="fd-chips"><span class="chip2 st-' + esc(h.state) + '"><i></i>' + esc(STATE[h.state] || h.state) + '</span>' +
      '<span class="chip2" style="background:rgba(255,255,255,.16)">' + trigGlyphInline(tk) + esc((TRIG[tk] || {}).short || tk) + '</span></div></div>' +
      '<div class="fd-body">' +
      '<div class="fd-sec"><h4>What it is</h4><p>' + esc(n.summary) + '</p>' + fileHtml + '</div>' +
      '<div class="fd-sec"><h4>Data in &amp; out</h4><div class="fd-io"><div><ul>' + (n.inputs || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div><div class="out"><ul>' + (n.outputs || []).map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul></div></div>' +
      '<div class="fd-nb">' + neighbourChips(id) + '</div></div>' +
      '<div class="fd-sec"><h4>How it is triggered</h4><span class="fd-auto ' + esc(n.automation) + '">' + esc(AUTO_TEXT[n.automation] || n.automation) + '</span>' + trigHtml + '</div>' +
      (stats ? '<div class="fd-sec"><h4>Live numbers</h4><div class="fd-stats">' + stats + '</div></div>' : '') +
      '<div class="fd-sec"><h4>Health</h4><p><b>' + esc(STATE_LONG[h.state] || h.state) + '.</b> ' + esc(h.detail || '') + '</p>' +
      '<div class="fd-times"><div><b>LAST SUCCESS</b>' + (h.last_ok_at ? esc(rel(h.last_ok_at)) + ' &middot; ' + esc(absTime(h.last_ok_at)) : 'none recorded') + '</div>' +
      '<div><b>LAST ERROR</b>' + (h.last_error_at ? esc(rel(h.last_error_at)) + ' &middot; ' + esc(absTime(h.last_error_at)) : 'none recorded') + '</div></div>' + notes + '</div>' +
      '</div>';
    if (!keepScroll) dr.classList.toggle('left', S.mode === '3d' && F3() && F3().mounted() ? F3().drawerLeft(id) : g.x + g.w / 2 > S.L.width * 0.56);   // open on the side that does not cover the node
    dr.classList.add('open'); dr.setAttribute('aria-hidden', 'false');
    var nb = $('.fd-body', dr); if (nb) nb.scrollTop = scroll;
    if (!keepScroll) { var x = $('.fd-x', dr); if (x) { try { x.focus({ preventScroll: true }); } catch (e) { /* ignore */ } } }
  }
  function trigGlyphInline(k) { return '<svg viewBox="0 0 24 24" style="width:12px;height:12px;stroke-width:2.2" aria-hidden="true">' + (TRIG[k] || TRIG.manual).glyph + '</svg>'; }
  $('#flowDrawer').addEventListener('click', function (e) {
    if (e.target.closest('.fd-x')) return closeDrawer();
    var nb = e.target.closest('[data-node]'); if (nb) return select(nb.getAttribute('data-node'));
    var os = e.target.closest('[data-open-script]');
    if (os) {
      var rel_ = os.getAttribute('data-open-script');
      var say = function (copied) { D.toast('Script Center: choose “' + rel_ + '” in Editor & test runner' + (copied ? ' (path copied)' : ''), 'ok', 6500); };
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(rel_).then(function () { say(true); }, function () { say(false); });
        else say(false);
      } catch (er) { say(false); }
      D.go('management');
    }
  });

  // ------------------------------------------------------------------ trace the newest lead
  function setCap(html, pct, show) {
    var cap = $('#traceCap');
    if (html != null) cap.innerHTML = html + '<div class="tc-bar"><span style="width:' + (pct || 0) + '%"></span></div>';
    cap.classList.toggle('show', !!show);
  }
  function flashNode(id) { var el = $('#fn-' + id); if (!el) return; el.classList.remove('trace-on'); void el.offsetWidth; el.classList.add('trace-on'); }
  function tracing() { return T.running || !!(F3() && F3().touring()); }
  function stopAnyTrace() { if (T.running) stopTrace(); if (F3() && F3().touring()) F3().stopTour(); }
  function toggleTrace() { if (tracing()) stopAnyTrace(); else startTrace(); }
  function setTraceButton(playing) {
    var b = $('#flowTrace'), three = S.mode === '3d';
    b.classList.toggle('playing', playing);
    $('span', b).textContent = playing ? (three ? 'Stop tour' : 'Stop trace') : (three ? 'Cinematic tour' : 'Trace latest lead');
    $('svg', b).innerHTML = playing ? ICONS.stop : ICONS.play;
    b.title = three ? "Fly the camera along the newest lead's real journey through the pipeline (T)" : "Replay the newest lead's real journey through the pipeline (T)";
  }
  D.on('flow3d-tour', function (on) { setTraceButton(!!on); });
  function startTrace() {
    var tr = S.data && S.data.trace; if (!tr || T.running || !S.built) return;
    if (D.view() !== 'dataflow') D.go('dataflow');
    if (S.mode === '3d') {                                                       // the same real TRACE steps, flown by the 3D camera
      syncMode();                                                                // arriving from another page: make sure the scene is up first
      if (F3() && F3().mounted()) { F3().tour(); return; }
    }
    if (S.mode === '3d') return;                                                 // (never reached: a failed start switches S.mode to 2d first)
    T.running = true; T.i = 0; T.steps = tr.steps; T.edgesOn = [];
    setTraceButton(true);
    closeDrawer();
    var first = tr.steps[0];
    setCap('<div class="tc-top">TRACING LEAD #' + esc(tr.lead_id) + ' &middot; ' + esc(String(tr.source || '').replace(/_/g, ' ')) + '</div><div class="tc-text">Follow the real journey of the newest lead…</div><div class="tc-at">' + esc(absTime(tr.created_at)) + '</div>', 0, true);
    if (first) flashNode(S.E[first.edge] ? S.E[first.edge].d.from : '');
    T.timer = setTimeout(stepTrace, reduced.matches ? 200 : 900);
  }
  function stepTrace() {
    var tr = S.data && S.data.trace, st = T.steps[T.i];
    if (!T.running || !tr) return;
    if (!st) { setCap('<div class="tc-top">LEAD #' + esc(tr.lead_id) + ' &middot; DONE</div><div class="tc-text">Journey complete: every stage above really happened for this lead.</div><div class="tc-at">' + esc(absTime(tr.created_at)) + '</div>', 100, true); T.timer = setTimeout(stopTrace, 3800); return; }
    var rec = S.E[st.edge];
    var pct = Math.round((T.i + 1) / T.steps.length * 100), head = 'STEP ' + (T.i + 1) + ' / ' + T.steps.length;
    if (!st.reached) {
      setCap('<div class="tc-top">LEAD #' + esc(tr.lead_id) + ' &middot; ' + head + '</div><div class="tc-text">The journey stops here: this lead never reached this step.</div><div class="tc-at">' + esc(st.text) + '</div>', pct, true);
      T.timer = setTimeout(stopTrace, 3800); return;
    }
    var dur = rec ? Math.max(500, Math.min(1300, rec.len / 0.32)) : 600;
    if (reduced.matches) dur = 350;
    if (rec) { rec.el.classList.add('trace-on'); T.edgesOn.push(rec); if (!reduced.matches) S.comets.push({ edge: st.edge, t0: performance.now(), dur: dur }); }
    setCap('<div class="tc-top">LEAD #' + esc(tr.lead_id) + ' &middot; ' + head + '</div><div class="tc-text">' + esc(st.text) + '</div><div class="tc-at">' + (st.at ? esc(absTime(st.at)) + ' &middot; from the database' : '') + '</div>', pct, true);
    T.i++;
    T.timer = setTimeout(function () { if (rec) flashNode(rec.d.to); stepTrace(); }, dur + 160);
  }
  function stopTrace() {
    clearTimeout(T.timer); T.running = false; S.comets = [];
    T.edgesOn.forEach(function (r) { r.el.classList.remove('trace-on'); }); T.edgesOn = [];
    $$('.fnode.trace-on').forEach(function (n) { n.classList.remove('trace-on'); });
    setCap(null, 0, false);
    setTraceButton(false);
  }
  $('#flowTrace').addEventListener('click', toggleTrace);
  $('#flowRefresh').addEventListener('click', refreshNow);
  D.on('refresh-request', function () { if (active()) refreshNow(); });

  // ------------------------------------------------------------------ legend
  function buildLegend() {
    var lines = ['event', 'scheduled', 'background', 'manual', 'passive'].map(function (k) {
      var sty = k === 'manual' ? 'stroke-dasharray="1.5 5" stroke-linecap="round"' : (k === 'passive' ? 'opacity=".55"' : '');
      return '<span class="lg-i"><span class="bd badge-' + k + '">' + trigGlyph(k) + '</span><svg class="ln" viewBox="0 0 30 8"><path d="M0 4H30" stroke="' + TRIG[k].color + '" stroke-width="2.2" ' + sty + '/></svg>' + esc(TRIG[k].label) + '</span>';
    }).join('');
    var modes = '<span class="lg-i"><svg class="ln" viewBox="0 0 30 8"><path d="M0 4H30" stroke="#fff" stroke-width="2.4"/></svg>bright + fast dots = running now</span>' +
      '<span class="lg-i"><svg class="ln" viewBox="0 0 30 8"><path d="M0 4H30" stroke="#fff" stroke-width="1.8" stroke-dasharray="6 4" opacity=".6"/></svg>dashed = automated by design, not running</span>' +
      '<span class="lg-i"><svg class="ln" viewBox="0 0 30 8"><path d="M0 4H30" stroke="#fff" stroke-width="1.8" stroke-dasharray="1.5 5" stroke-linecap="round" opacity=".7"/></svg>dotted = a person starts it</span>';
    var nodes = '<span class="lg-i"><span class="nd" style="border-color:#ff6a48"></span>automated &amp; running</span><span class="lg-i"><span class="nd partial"></span>partly running</span><span class="lg-i"><span class="nd dashed"></span>automatable, not running</span><span class="lg-i"><span class="nd"></span>manual / passive</span><span class="lg-i"><span class="nd dotted"></span>outside this machine</span>';
    var states = [['healthy', 'solid'], ['stale', 'half'], ['error', 'solid'], ['never-run', 'hollow'], ['mocked', 'hatch'], ['offline', 'hollow'], ['idle', 'hollow']].map(function (s) {
      return '<span class="lg-i" style="--hc:var(--h-' + (s[0] === 'never-run' ? 'never' : s[0]) + ')"><span class="sw ' + s[1] + '"></span>' + esc((STATE[s[0]] || s[0]).toLowerCase()) + '</span>';
    }).join('');
    $('#flowLegend').innerHTML =
      '<div class="lg"><span class="lg-t">HOW IT STARTS</span>' + lines + '</div>' +
      '<div class="lg"><span class="lg-t">LINES</span>' + modes + '</div>' +
      '<div class="lg"><span class="lg-t">BOX BORDER</span>' + nodes + '</div>' +
      '<div class="lg"><span class="lg-t">STATE DOT</span>' + states + '</div>';
  }
  buildLegend();

  // ------------------------------------------------------------------ keyboard + view wiring
  document.addEventListener('keydown', function (e) {
    if (!active()) return;
    var tag = (e.target && e.target.tagName) || '', typing = /INPUT|TEXTAREA|SELECT/.test(tag);
    if (e.key === 'Escape') { if (tracing()) stopAnyTrace(); else closeDrawer(); }
    else if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && (e.key === 't' || e.key === 'T')) { e.preventDefault(); toggleTrace(); }
  });
  setInterval(tickAgo, 1000);
  setInterval(function () { if (active() && S.data && !document.hidden) { S.sig = {}; updateAll(S.data, false); } }, 30000);   // let "3m ago" style text and recency-based pulses age

  D.on('view', function (name) {
    if (name === 'dataflow') {
      requestAnimationFrame(function () { fit(); activate(); startLoop(); syncMode(); });
      poll(false);
    } else { stopLoop(); stopAnyTrace(); syncMode(); }    // leaving the page frees every GPU object of the 3D scene
  });

  // ------------------------------------------------------------------ 2D | 3D mode
  var api3d = {
    data: function () { return S.data; }, selected: function () { return S.sel; },
    select: function (id) { select(id); }, closeDrawer: closeDrawer,
    icon: function (n) { return svgIcon(n); }, trigGlyph: trigGlyph, headline: headline, trig: TRIG, state: STATE,
    tipNode: function (id) { return S.N[id] ? nodeTip(S.N[id].d) : ''; },
    tipEdge: function (id) { return S.E[id] ? edgeTip(S.E[id].d) : ''; },
    flowParams: flowParams, edgeActivity: edgeActivity,
    fail: function (why) { if (S.mode === '3d') fallBack2d(why || 'the 3D view stopped.'); }
  };
  var LEGEND_TXT = { '2d': {}, '3d': { 'BOX BORDER': 'PANEL FRAME', 'STATE DOT': 'STATE BEACON' } };
  function applyModeUI(note) {
    var st = $('#flowStage'), n = $('#flowModeNote');
    st.setAttribute('data-mode', S.mode);
    ['2d', '3d'].forEach(function (m) { $('#flowMode' + m).setAttribute('aria-pressed', S.mode === m ? 'true' : 'false'); });
    n.hidden = !note; n.textContent = note || '';
    $('#flowHint').textContent = S.mode === '3d' ? 'drag to orbit · click a node to fly to it · Esc closes' : 'hover for details · click a node to open it · Esc closes';
    $$('.lg-t', $('#flowLegend')).forEach(function (el) {
      var k = el.getAttribute('data-k') || el.textContent; el.setAttribute('data-k', k); el.textContent = (LEGEND_TXT[S.mode] || {})[k] || k;
    });
    if (!tracing()) setTraceButton(false);
  }
  function fallBack2d(reason) {
    S.mode = '2d';          // automatic fallback: deliberately NOT remembered, so a working machine keeps its 3D preference
    applyModeUI('3D is off: ' + reason + ' Showing the 2D map.');
    syncMode(); requestAnimationFrame(function () { fit(); startLoop(); });
  }
  // make the real state match S.mode + "is the page on screen": 3D is mounted only while Data Flow is the visible page
  function syncMode() {
    var f3 = F3(), want = S.mode === '3d' && active() && S.built;
    if (want) {
      if (!f3 || !f3.mounted()) {
        var ok = false;
        try { ok = !!(f3 && f3.available() && f3.mount($('#flow3d'), api3d)); } catch (e) { ok = false; console.warn('3D start failed', e); }
        if (!ok) { fallBack2d(f3 && f3.lastError() ? f3.lastError() + '.' : 'the 3D module is not available.'); return; }
        stopLoop();
      }
    } else if (f3 && f3.mounted()) f3.unmount();
  }
  function setMode(m, user, note) {
    m = m === '2d' ? '2d' : '3d';
    if (m === '3d' && !note) {
      if (reduced.matches) note = 'Reduced motion is on, so the calm 2D map is shown.';
      else if (!F3()) note = '3D is off: the 3D module did not load. Showing the 2D map.';
      else if (!F3().available()) note = '3D is off: ' + (F3().lastError() || 'this device has no WebGL2') + '. Showing the 2D map.';
    }
    if (note) m = '2d';
    if (user && !note) D.store(MODE_KEY, m);
    if (m === '2d' && S.mode === '3d') stopAnyTrace();
    S.mode = m;
    applyModeUI(note || '');
    if (m === '3d') { stopLoop(); syncMode(); }
    else { syncMode(); requestAnimationFrame(function () { fit(); startLoop(); }); }
  }
  $('#flowMode2d').addEventListener('click', function () { setMode('2d', true); });
  $('#flowMode3d').addEventListener('click', function () { setMode('3d', true); });

  // ------------------------------------------------------------------ boot
  setMode(D.store(MODE_KEY) === '2d' ? '2d' : '3d', false);        // 3D is the default on this branch
  setLive(pollSec() === 0 ? 'paused' : 'connecting');
  poll(false);
})();
