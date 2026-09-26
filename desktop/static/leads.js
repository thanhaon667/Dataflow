/* ERP Desk - Leads page: a funnel + SLA explorer for data analysts, and a second tab for lead sources + arrival cohorts.
   Draws the JSON of GET /api/leads/analysis (desktop/leads_data.py) and, on the second tab, GET /api/leads/sources
   (desktop/sources_data.py, drawn by static/sources.js). Filters live here as plain state and are sent to the server on
   every change: the SERVER filters, so every panel, the paged table, the cohort curves and both CSV files always describe
   the same set of leads. Only the tab on screen polls, so the page still has exactly one poller.
   Nothing is computed in the browser except layout and wording of what the server already decided. All server text goes
   through esc() / textContent. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  // The filter bar's breakpoint/fade/scroll-preservation math lives in static/leads_filterbar.js (loaded just before
  // this file) so tests/leads_scenarios.py can run the exact same functions under Node - see that file's header.
  var FB = D.filterBar;
  if (!FB) return;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = D.esc;

  var POLL_MS = 30000;
  var FETCH_TIMEOUT_MS = 20000;          // above the server's worst case (connect timeout + query budget), see lesson L-094
  var OLD_AFTER_MS = POLL_MS * 3;
  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  var kit = D.chartKit || null;
  var C = (kit && kit.colors) || { ink: '#1c1d22', dim: '#6b6f76', blue: '#3452eb', coral: '#ff5a36', green: '#12b886', violet: '#7c5cff', amber: '#f2b705', red: '#e0393e', line: '#e7e2d8', grid: '#f2efe8' };
  var STATE_COLOR = { on_time: C.green, late: C.amber, past_sla: C.red, pending: C.blue, no_deadline: '#b9b5aa' };
  // The compact SLA-outcome word for a pill/chip/table cell (full form: `.label` below). The server is the only
  // place that writes these five words (desktop/sla_words.py SHORT) - syncStateShort() copies them off every
  // payload's `options.statuses` so this page and static/sources.js can never say something different for the
  // same state; these five are only the pre-first-payload fallback.
  var STATE_SHORT = { on_time: 'On time', late: 'Answered late', past_sla: 'Past SLA', pending: 'Waiting', no_deadline: 'No deadline' };
  function syncStateShort(o) {
    (o && o.statuses || []).forEach(function (s) { if (s.short) STATE_SHORT[s.value] = s.short; });
  }
  var STAGE_COLOR = { received: C.coral, assigned: '#ff7a4a', analyzed: C.violet, task: C.blue, update: C.green };
  var SORT_COLS = [                      // table columns: label, sort key (null = not sortable), cell key
    ['ID', 'lead_id'], ['Arrived', 'created_at'], ['Lead', 'name'], ['Source', 'source'], ['Sales rep', 'rep'],
    ['Progress', null], ['SLA outcome', 'state'], ['First reply after', 'hours']
  ];

  var TABS = {
    funnel: { eyebrow: 'LEADS · FUNNEL &amp; SLA EXPLORER', title: 'Where leads stall, and how fast we answer',
              url: '/api/leads/analysis', panels: '#ldGrid' },
    sources: { eyebrow: 'LEADS · SOURCES &amp; ARRIVAL COHORTS', title: 'Where leads come from, and how each arrival behaves',
               url: '/api/leads/sources', panels: '#ldSources' }
  };

  var S = {
    data: null, at: 0, timer: null, fails: 0, busy: false, again: false, inflight: null, played: false, live: 'connecting', old: false, err: '',
    tab: 'funnel',
    f: { from: null, to: null, rep: [], source: [], status: null, sync: null }, sort: 'created_at', dir: 'desc', page: 1, size: 25,
    // the "Sources & cohorts" tab's own view state; `bucket: null` means "let the server pick the finest one the span carries"
    v: { bucket: null, metric: 'replied', cohort: null, sort: 'leads', dir: 'desc' },
    sig: {}, debounce: null, defsOpen: false, csvBusy: false, badLoops: 0
  };

  // ------------------------------------------------------------------ small helpers
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function fmtWhen(iso) {
    if (!iso) return '-';
    var d = new Date(iso);
    return isNaN(d) ? '-' : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }) + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function fmtDay(k) {
    if (!k) return '';
    var p = k.split('-'); var d = new Date(+p[0], +p[1] - 1, +p[2]);
    return isNaN(d) ? k : d.toLocaleDateString('en-GB', { day: 'numeric', month: 'short', year: 'numeric' });
  }
  function fmtShortDay(k) { var p = k.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); }
  function fmtTime(iso) { var d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return 'just now';
    if (s < 90) return s + 's ago';
    return Math.round(s / 60) + 'm ago';
  }
  function num(v) { return Number(v).toLocaleString('en-US'); }
  function pretty(s) { return String(s == null ? '' : s).replace(/_/g, ' '); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function addDays(key, delta) {         // 'YYYY-MM-DD' arithmetic without time-zone surprises
    var p = key.split('-'); var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + delta));
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
  }
  function rateText(r) {                 // the server decides whether a percentage is allowed (denominator >= LOW_N)
    if (!r) return '-';
    return r.pct == null ? r.n + ' of ' + r.of : (Math.round(r.pct) + '%');
  }
  function hasFilter() { var f = S.f; return !!(f.from || f.to || f.rep.length || f.source.length || f.status || f.sync || (S.tab === 'sources' && S.v.cohort)); }

  // ------------------------------------------------------------------ request state
  function sharedParams(q) {                 // the filters both tabs share, in the order the server whitelists them
    var f = S.f;
    if (f.from) q.append('from', f.from);
    if (f.to) q.append('to', f.to);
    f.rep.forEach(function (v) { q.append('rep', v); });
    f.source.forEach(function (v) { q.append('source', v); });
    if (f.status) q.append('status', f.status);
    if (f.sync) q.append('sync', f.sync);
    return q;
  }
  function params(forExport) {               // the Funnel & SLA tab
    var q = sharedParams(new URLSearchParams());
    q.append('sort', S.sort); q.append('dir', S.dir);
    if (!forExport) { q.append('page', String(S.page)); q.append('page_size', String(S.size)); }
    return q.toString();
  }
  function sourceParams() {                  // the Sources & cohorts tab
    var q = sharedParams(new URLSearchParams());
    if (S.v.bucket) q.append('bucket', S.v.bucket);          // omitted = the server picks (and says which and why)
    q.append('metric', S.v.metric);
    if (S.v.cohort) q.append('cohort', S.v.cohort);
    q.append('sort', S.v.sort); q.append('dir', S.v.dir);
    return q.toString();
  }
  function query(forExport) { return S.tab === 'sources' ? sourceParams() : params(forExport); }

  function fetchAnalysis(fresh) {
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer;
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () { var e = new Error('timeout'); e.timeout = true; reject(e); if (ctl) ctl.abort(); }, FETCH_TIMEOUT_MS);
    });
    var req = fetch(TABS[S.tab].url + '?' + query(false) + (fresh ? '&fresh=1' : ''), { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }, function () { return { status: r.status, body: null }; }); });
    return Promise.race([req, timeout]).then(function (x) { clearTimeout(timer); return x; }, function (e) { clearTimeout(timer); throw e; });
  }

  // ------------------------------------------------------------------ live pill
  function setLive(state, label) {
    S.live = state;
    var p = $('#ldPill');
    p.setAttribute('data-state', state);
    S.old = (state === 'stale' && label !== 'PARTIAL') || state === 'offline';
    $('#ldLive').textContent = label || { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#ldAgo');
    if (!S.at) { el.textContent = ''; return; }
    el.textContent = 'updated ' + ago(S.at);
    // L-093: when a re-read fails and the old numbers stay on screen, the numbers themselves must look old, not just the pill
    var stale = !!(S.old && S.data), badge = $('#ldStale');
    $('#ld').classList.toggle('stale', stale);
    badge.hidden = !stale;
    if (stale) badge.textContent = 'These numbers were read ' + ago(S.at) + (S.err ? ' (latest attempt: ' + S.err.replace(/[.\s]+$/, '') + ')' : '') + ' and are shown until the next successful read.';
    $('#ldPill').title = (S.old && S.err ? S.err + '. Showing the numbers read at ' + fmtTime(S.data && S.data.generated_at) + '. ' : 'Read from the database at ' + fmtTime(S.data && S.data.generated_at) + '. ') +
      'This page re-reads every ' + (POLL_MS / 1000) + ' s while it is open, and at once when a filter changes';
  }

  // ------------------------------------------------------------------ tabs (one page, two views of the same leads)
  function moveTabInd() {
    var on = $('.ld-tabs button.on'), ind = $('#ldTabsInd');
    if (!on || !ind) return;
    ind.style.width = on.offsetWidth + 'px';
    ind.style.transform = 'translateX(' + on.offsetLeft + 'px)';
  }
  function setTab(name, opts) {
    if (!TABS[name] || name === S.tab) { moveTabInd(); return; }
    S.tab = name;
    $$('.ld-tabs button').forEach(function (b) {
      var on = b.getAttribute('data-tab') === name;
      b.classList.toggle('on', on);
      b.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    moveTabInd();
    Object.keys(TABS).forEach(function (k) { $(TABS[k].panels).hidden = (k !== name); });
    $('#ldEyebrow').innerHTML = TABS[name].eyebrow;
    $('#ldTitle').textContent = TABS[name].title;
    // the two tabs are two different payloads: drop what the other one drew, never show its numbers under this title
    S.data = null; S.at = 0; S.sig = {}; S.err = '';
    if (D.pages && D.pages.sources) D.pages.sources.clear();
    $('#ldKpis').innerHTML = ''; $('#ldWarn').hidden = true;
    setLive('syncing');
    load(true);
    if (!(opts && opts.silent)) {
      var btn = $('.ld-tabs button.on');
      if (btn) btn.focus({ preventScroll: true });
    }
  }
  $('.ld-tabs').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-tab]') : null;
    if (b) setTab(b.getAttribute('data-tab'));
  });
  $('.ld-tabs').addEventListener('keydown', function (e) {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    var names = Object.keys(TABS), i = names.indexOf(S.tab);
    e.preventDefault();
    setTab(names[(i + (e.key === 'ArrowRight' ? 1 : names.length - 1)) % names.length]);
  });
  window.addEventListener('resize', moveTabInd);

  // What sources.js calls back into when something on that tab is clicked. Every change lands in S, so the filter bar,
  // both tabs and both CSV links keep describing one and the same set of leads.
  var sourcesCtx = {
    setMetric: function (v) { S.v.metric = v; changed(false); },
    setBucket: function (v) { S.v.bucket = (v === 'auto' ? null : v); S.v.cohort = null; changed(false); },  // a cohort key belongs to one bucket
    setCohort: function (v) { S.v.cohort = v || null; changed(false); },
    setSort: function (k) {
      if (S.v.sort === k) S.v.dir = S.v.dir === 'asc' ? 'desc' : 'asc';
      else { S.v.sort = k; S.v.dir = (k === 'name') ? 'asc' : 'desc'; }
      changed(false);
    },
    toggleSource: function (v) { toggle(S.f.source, v); changed(); }
  };

  // ------------------------------------------------------------------ header / caveat / warnings
  function renderHead(d) {
    var o = d.options, k = (d.kpis || [])[0];
    var n = d.in_view != null ? d.in_view : (k && k.state === 'ok' && k.id === 'leads' ? k.value : null);
    var parts = [];
    if (n != null) parts.push('<b>' + num(n) + '</b> of ' + num(o.total) + ' lead' + (o.total === 1 ? '' : 's') + ' in view');
    if (o.first_day) parts.push('arrived ' + esc(o.first_day === o.last_day ? fmtDay(o.first_day) : fmtDay(o.first_day) + ' to ' + fmtDay(o.last_day)));
    parts.push(d.filters.active ? plural(d.filters.active, 'filter') + ' applied' : 'no filter applied');
    $('#ldSub').innerHTML = parts.join(' &middot; ');
    var c = $('#ldCaveat');
    c.hidden = !d.caveat;
    var t = $('#ldCaveatText');
    t.textContent = '';
    if (d.caveat) {
      var m = /^([^:]{3,60}:)\s*([\s\S]*)$/.exec(String(d.caveat));
      if (m) { var b = document.createElement('b'); b.textContent = m[1] + ' '; t.appendChild(b); t.appendChild(document.createTextNode(m[2])); } else t.textContent = d.caveat;
    }
    var w = $('#ldWarn');
    w.hidden = !(d.warnings && d.warnings.length);
    w.innerHTML = (d.warnings || []).map(function (x) { return '<div>' + esc(x) + '</div>'; }).join('');
  }

  // ------------------------------------------------------------------ filter bar
  function presetOf(o) {
    var f = S.f;
    if (!f.from && !f.to) return 'all';
    if (f.from && !f.to && o.today) {
      var days = { '7d': 7, '30d': 30, '90d': 90 };
      for (var k in days) if (f.from === addDays(o.today, -(days[k] - 1))) return k;
    }
    return 'custom';
  }
  // Rebuilding .ldFilters' innerHTML on every filter change (below) destroys each .fg-scroll strip's scrollLeft
  // (lesson L-100: an innerHTML rebuild wipes scroll/focus/select state). Audit finding (2026-09-24): clicking a
  // pill scrolled off to the right snapped the strip back to the start and left focus off-screen, making the far
  // pills of a long REP/SOURCE list impractical to multi-select. Each scroller is keyed by its `data-group` (stable
  // across rebuilds, unlike DOM order) so its position survives the rebuild that its own click just triggered.
  function updateFade(el) {
    var group = el.closest('.fg-multi');
    if (!group) return;
    var r = FB.computeFade(el.scrollLeft, el.clientWidth, el.scrollWidth);
    group.classList.toggle('fade-start', r.fadeStart);
    group.classList.toggle('fade-end', r.fadeEnd);
  }
  function renderFilters(d) {
    var o = d.options, f = S.f, box = $('#ldFilters');
    var inViewNow = d.in_view != null ? d.in_view : (d.panels && d.panels.sla && d.panels.sla.total != null ? d.panels.sla.total : null);
    var sig = JSON.stringify([o, f, inViewNow, S.tab, S.v.cohort, (d.view || {}).cohort_label]);
    if (S.sig.filters === sig) return;
    S.sig.filters = sig;
    var active = document.activeElement, focusKey = active && box.contains(active) ? active.getAttribute('data-k') : null;
    var scrollPos = FB.buildScrollMap($$('.fg-scroll', box).map(function (el) {
      return { group: el.getAttribute('data-group'), scrollLeft: el.scrollLeft };
    }));
    var pre = presetOf(o), html = '';
    html += '<div class="fgroup"><span class="fgroup-label">RANGE</span><div class="seg" data-seg="range">' +
      [['all', 'All time'], ['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days']].map(function (p) {
        return '<button type="button" data-k="range-' + p[0] + '" data-v="' + p[0] + '" class="' + (pre === p[0] ? 'on' : '') + '">' + p[1] + '</button>'; }).join('') + '</div>' +
      '<label class="ld-date"><span>from</span><input type="date" data-k="from" data-date="from" value="' + esc(f.from || '') + '" min="2000-01-01" max="2100-12-31"></label>' +
      '<label class="ld-date"><span>to</span><input type="date" data-k="to" data-date="to" value="' + esc(f.to || '') + '" min="2000-01-01" max="2100-12-31"></label></div>';
    // REP and SOURCE lists have no fixed size (a busy team can have a dozen reps and as many sources): wrap their pills
    // in a one-row horizontally-scrolling strip (.fg-scroll) instead of letting them wrap the whole filter bar to two
    // or three rows. The label stays put; only the pill strip scrolls, so every filter is still one scroll + one click
    // away regardless of window width (verified at 1920/1440/820/390 with a synthetic 8-rep/12-source set).
    html += '<div class="fgroup fg-multi"><span class="fgroup-label">SALES REP</span><div class="fg-scroll" data-group="rep">' + o.reps.map(function (r, i) {
      return '<button type="button" class="pill ' + (f.rep.indexOf(r.value) >= 0 ? 'on' : '') + '" data-k="rep-' + i + '" data-multi="rep" data-v="' + esc(r.value) + '" aria-pressed="' + (f.rep.indexOf(r.value) >= 0) + '"><i style="background:' + C.blue + '"></i>' + esc(r.value) + ' <span class="n">' + r.n + '</span></button>'; }).join('') + '</div></div>';
    html += '<div class="fgroup fg-multi"><span class="fgroup-label">SOURCE</span><div class="fg-scroll" data-group="source">' + o.sources.map(function (s, i) {
      return '<button type="button" class="pill ' + (f.source.indexOf(s.value) >= 0 ? 'on' : '') + '" data-k="source-' + i + '" data-multi="source" data-v="' + esc(s.value) + '" aria-pressed="' + (f.source.indexOf(s.value) >= 0) + '"><i style="background:' + C.violet + '"></i>' + esc(pretty(s.value)) + ' <span class="n">' + s.n + '</span></button>'; }).join('') + '</div></div>';
    html += '<div class="fgroup"><span class="fgroup-label">STATUS</span><div class="seg" data-seg="status">' +
      '<button type="button" data-k="status-all" data-v="" class="' + (!f.status ? 'on' : '') + '">All</button>' +
      o.statuses.map(function (s) { return '<button type="button" data-k="status-' + s.value + '" data-v="' + s.value + '" title="' + esc(s.label + ': ' + s.meaning) + '" class="' + (f.status === s.value ? 'on' : '') + '">' + esc(STATE_SHORT[s.value] || s.label) + '</button>'; }).join('') + '</div></div>';
    html += '<div class="fgroup"><span class="fgroup-label">CLICKUP SYNC</span><label class="ld-select"><select data-k="sync" data-sync aria-label="ClickUp sync status"><option value="">Any</option>' +
      o.syncs.map(function (s) { return '<option value="' + esc(s.value) + '"' + (f.sync === s.value ? ' selected' : '') + '>' + esc(s.value) + ' (' + s.n + ')</option>'; }).join('') + '</select></label></div>';
    var inView = inViewNow;
    html += '<div class="fcount"><span>' + (inView == null ? '' : '<b>' + num(inView) + '</b> of ' + num(o.total) + ' leads in view') + '</span>' +
      (hasFilter() ? '<button type="button" class="linkbtn" data-reset>Reset all</button>' : '') + '</div>';
    var chips = [];
    if (f.from || f.to) chips.push(['range', 'DATE', (f.from ? fmtShortDay(f.from) : 'start') + ' to ' + (f.to ? fmtShortDay(f.to) : 'today')]);
    f.rep.forEach(function (v) { chips.push(['rep:' + v, 'REP', v]); });
    f.source.forEach(function (v) { chips.push(['source:' + v, 'SOURCE', pretty(v)]); });
    if (f.status) chips.push(['status', 'STATUS', STATE_SHORT[f.status] || f.status]);
    if (f.sync) chips.push(['sync', 'SYNC', f.sync]);
    if (S.tab === 'sources' && S.v.cohort) chips.push(['cohort', 'COHORT', (d.view && d.view.cohort_label) || S.v.cohort]);
    html += '<div class="chips">' + chips.map(function (c) { return '<span class="chip"><em>' + c[1] + '</em>' + esc(c[2]) + '<button type="button" data-clear="' + esc(c[0]) + '" aria-label="Remove the ' + esc(c[1].toLowerCase()) + ' filter">×</button></span>'; }).join('') + '</div>';
    box.innerHTML = html;
    $$('.fg-scroll', box).forEach(function (el) {
      var was = FB.scrollLeftFor(scrollPos, el.getAttribute('data-group'));
      if (was) el.scrollLeft = was;
      updateFade(el);
    });
    // preventScroll: true only stops the PAGE from jumping to the refocused element (lesson L-099); the strip's own
    // scrollLeft was already restored above, so the pill regains focus exactly where it was, not off-screen.
    if (focusKey) { var again = box.querySelector('[data-k="' + focusKey + '"]'); if (again) again.focus({ preventScroll: true }); }
  }

  function changed(resetPage) {
    if (resetPage !== false) S.page = 1;
    S.sig.filters = null;
    if (S.data) renderFilters(S.data);              // the pills / chips answer at once; the numbers follow the fetch
    clearTimeout(S.debounce);
    // clear the handle when it fires: downloadCsv() refuses to export while a change is still pending, and a handle
    // left set for ever would disable the CSV button after the first filter change.
    S.debounce = setTimeout(function () { S.debounce = null; load(true); }, 160);
  }
  function toggle(list, v) { var i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v); }

  $('#ldFilters').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-reset')) { S.f = { from: null, to: null, rep: [], source: [], status: null, sync: null }; S.v.cohort = null; return changed(); }
    if (b.hasAttribute('data-clear')) {
      var k = b.getAttribute('data-clear');
      if (k === 'range') { S.f.from = null; S.f.to = null; }
      else if (k === 'cohort') S.v.cohort = null;
      else if (k.indexOf('rep:') === 0) toggle(S.f.rep, k.slice(4));
      else if (k.indexOf('source:') === 0) toggle(S.f.source, k.slice(7));
      else S.f[k] = null;
      return changed();
    }
    if (b.hasAttribute('data-multi')) { toggle(S.f[b.getAttribute('data-multi')], b.getAttribute('data-v')); return changed(); }
    var seg = b.closest('[data-seg]');
    if (!seg) return;
    var kind = seg.getAttribute('data-seg'), v = b.getAttribute('data-v');
    if (kind === 'status') S.f.status = v || null;
    else if (kind === 'range') {
      var today = S.data && S.data.options.today;
      if (v === 'all') { S.f.from = null; S.f.to = null; }
      else if (today) { S.f.from = addDays(today, -(parseInt(v, 10) - 1)); S.f.to = null; }
    }
    changed();
  });
  $('#ldFilters').addEventListener('change', function (e) {
    var t = e.target;
    if (t.hasAttribute('data-date')) { S.f[t.getAttribute('data-date')] = t.value || null; return changed(); }
    if (t.hasAttribute('data-sync')) { S.f.sync = t.value || null; return changed(); }
  });
  $('#leadsScroll').addEventListener('scroll', function () { $('#ldFilters').classList.toggle('stuck', this.scrollTop > $('#ldFilters').offsetTop + 6); }, { passive: true });
  // scroll events do not bubble, so listen in the CAPTURE phase on the stable filter bar (never rebuilt itself, only
  // its innerHTML is) to catch scrolling inside any .fg-scroll strip, present or future.
  $('#ldFilters').addEventListener('scroll', function (e) {
    if (e.target && e.target.classList && e.target.classList.contains('fg-scroll')) updateFade(e.target);
  }, { passive: true, capture: true });
  // Crossing the ~1100px container-query breakpoint (leads.css) changes whether a strip even overflows, same as a
  // plain window resize. Both a ResizeObserver on the bar itself AND a window 'resize' listener are wired (belt and
  // braces: neither is guaranteed to fire for every possible cause - verified that at least one measurement method
  // for an emulated/automated viewport change can skip both without a later interaction), plus a cheap 1 s poll as
  // the reliable fallback: it is the ONLY one of the three proven to always catch a resize in every environment
  // tested (headless/emulated viewport changes included), and the cost is a few property reads a second only while
  // the Leads page exists in the DOM.
  function refreshAllFades() { var box = $('#ldFilters'); if (box) $$('.fg-scroll', box).forEach(updateFade); }
  if (window.ResizeObserver) new ResizeObserver(refreshAllFades).observe($('#ldFilters'));
  window.addEventListener('resize', refreshAllFades, { passive: true });
  setInterval(refreshAllFades, 1000);

  // ------------------------------------------------------------------ KPI tiles
  function renderKpis(d) {
    var box = $('#ldKpis'), tones = { blue: C.blue, red: C.red, green: C.green, amber: C.amber, violet: C.violet, grey: '#b9b5aa' };
    var html = (d.kpis || []).map(function (t) {
      var na = t.state !== 'ok';
      var v = na ? '<div class="kpi-value txt dash">n/a</div>' :
        (t.text != null ? '<div class="kpi-value txt' + (t.text === '-' ? ' dash' : '') + '">' + esc(t.text) + '</div>' :
          '<div class="kpi-value">' + esc(num(t.value)) + (t.id === 'rate' ? '<small>%</small>' : '') + '</div>');
      return '<div class="kpi' + (na ? ' na' : '') + '" data-k="' + esc(t.id) + '" style="--kc:' + (tones[t.tone] || C.blue) + '" title="' + esc(t.hint) + '">' +
        '<div class="kpi-label">' + esc(t.label) + '</div>' + v + '<div class="kpi-cap">' + esc(t.sub) + '</div></div>';
    }).join('');
    if (S.sig.kpis !== html) {
      var prev = {}; $$('.kpi', box).forEach(function (el) { prev[el.getAttribute('data-k')] = el.querySelector('.kpi-value') && el.querySelector('.kpi-value').textContent; });
      box.innerHTML = html;
      if (S.played) $$('.kpi', box).forEach(function (el) {
        var was = prev[el.getAttribute('data-k')], now = el.querySelector('.kpi-value').textContent;
        if (was != null && was !== now) { el.classList.add('flash'); setTimeout(function () { el.classList.remove('flash'); }, 1300); }
      });
      S.sig.kpis = html;
    }
  }

  // ------------------------------------------------------------------ panel plumbing
  function chartsOk() { return !!(window.Plotly && kit); }
  function na(id, p) {
    $(id).innerHTML = '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>This panel could not be read (' + esc(p.error || 'unknown error') + '). The other panels keep working.</span></div>';
  }
  function empty(id, title, sub) { $(id).innerHTML = '<div class="ld-empty"><div><b>' + esc(title) + '</b><span>' + sub + '</span></div></div>'; }
  function purge(id) { var el = $(id + ' .ld-plot'); if (el && window.Plotly && el.data) { try { Plotly.purge(el); } catch (e) { /* ignore */ } } }
  function plot(id, traces, layout, onClick) {
    var host = $(id);
    if (!window.Plotly || !kit) { host.innerHTML = '<div class="ld-empty"><div><b>Charts unavailable</b><span>plotly.js could not be loaded.</span></div></div>'; return; }
    var el = host.querySelector('.ld-plot');
    if (!el) { host.innerHTML = '<div class="ld-plot"></div>'; el = host.querySelector('.ld-plot'); }
    if (reduceMotion) layout.transition = { duration: 0 };
    if (!D.fontsDone()) { D.fontsReady(function () { plot(id, traces, layout, onClick); }); return; }   // Plotly measures text once: wait for the font
    Plotly.react(el, traces, layout, { displayModeBar: false, responsive: true });
    if (onClick && !el._bound) { el._bound = true; el.on('plotly_click', onClick); }
  }
  var ro = window.ResizeObserver ? new ResizeObserver(function (entries) {
    entries.forEach(function (en) {
      var el = en.target; clearTimeout(el._rz);
      el._rz = setTimeout(function () { if (window.Plotly && el.data && el.offsetWidth) { try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ } } }, 120);
    });
  }) : null;
  function watch(id) { var el = $(id + ' .ld-plot'); if (ro && el && !el._ro) { el._ro = true; ro.observe(el); } }

  // ------------------------------------------------------------------ funnel
  var ARROW = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v13M6.5 13 12 18.5 17.5 13"/></svg>';
  function renderFunnel(p) {
    if (p.state === 'unavailable') return na('#ldFunnel', p);
    if (p.state === 'empty') return empty('#ldFunnel', 'No leads in this view', 'Loosen a filter (or press Reset all) to bring leads back into the funnel.');
    var total = p.total || 1, html = '<div class="ld-fn">';
    p.stages.forEach(function (s, i) {
      if (i) {
        var st = s.step, keep = st.pct == null ? (st.n + ' of ' + st.of) : (Math.round(st.pct) + '%');
        html += '<div class="ld-fn-step">' + ARROW + '<span class="keep">' + esc(keep) + ' continue</span>' +
          (s.dropped ? '<span class="lost">' + plural(s.dropped, 'lead') + ' stop' + (s.dropped === 1 ? 's' : '') + ' here</span>' : '<span>none lost</span>') + '</div>';
      }
      var w = Math.max(0.6, s.count / total * 100);
      html += '<div class="ld-fn-row" data-stage="' + esc(s.key) + '" style="--fc:' + (STAGE_COLOR[s.key] || C.blue) + '" title="' + esc(s.evidence) + '">' +
        '<div class="ld-fn-meta"><span class="ld-fn-label">' + esc(s.label) + '</span><span class="ld-fn-nums"><b>' + num(s.count) + '</b>' + esc(rateText(s.overall)) + ' of all</span></div>' +
        '<div class="ld-fn-track"><i style="width:' + w + '%"></i></div></div>';
    });
    html += '</div>';
    var chips = [];
    if (p.biggest_drop) chips.push('<span class="ld-chip">biggest drop <b>' + esc(p.biggest_drop.label) + '</b> · ' + plural(p.biggest_drop.n, 'lead') + ' stop here</span>');
    else chips.push('<span class="ld-chip">no lead is lost between stages</span>');
    if (p.sync && p.sync.length) chips.push('<span class="ld-chip">ClickUp sync</span>' + p.sync.map(function (x) { return '<span class="ld-chip"><i style="background:' + (x.value === 'failed' ? C.red : x.value === 'pending' ? C.amber : C.green) + '"></i>' + esc(x.value) + ' <b>' + x.n + '</b></span>'; }).join(''));
    html += '<div class="ld-chips">' + chips.join('') + '</div>';
    $('#ldFunnel').innerHTML = html;
  }

  // ------------------------------------------------------------------ SLA outcome
  function renderSla(p) {
    if (p.state === 'unavailable') return na('#ldSla', p);
    if (p.state === 'empty') return empty('#ldSla', 'No leads in this view', 'Loosen a filter to see how the SLA is doing.');
    var c = p.counts, sel = S.f.status, seg = '';
    var order = ['on_time', 'late', 'past_sla', 'pending', 'no_deadline'];
    seg = order.filter(function (k) { return c[k] > 0; }).map(function (k) {
      return '<button type="button" data-status="' + k + '" class="' + (sel === k ? 'on' : '') + '" style="--c:' + STATE_COLOR[k] + ';flex:' + c[k] + ' 1 0" title="' + esc(STATE_SHORT[k] + ': ' + c[k]) + '" aria-label="' + esc(STATE_SHORT[k] + ', ' + plural(c[k], 'lead') + '. Click to filter.') + '"></button>';
    }).join('');
    var html = '<div class="ld-tri">' +
      '<div class="ld-tri-c" style="--c:' + C.green + '"><b>' + num(c.on_time) + '</b><span>Answered on time</span></div>' +
      '<div class="ld-tri-c" style="--c:' + C.red + '"><b>' + num(p.breached) + '</b><span>Breached</span><small>' + num(c.late) + ' answered late · ' + num(c.past_sla) + ' still waiting</small></div>' +
      '<div class="ld-tri-c" style="--c:' + C.blue + '"><b>' + num(c.pending) + '</b><span>Inside deadline</span>' + (c.no_deadline ? '<small>+ ' + c.no_deadline + ' with no deadline</small>' : '') + '</div></div>';
    html += '<div class="ld-stack' + (sel ? ' filtered' : '') + '" role="group" aria-label="SLA outcome of the leads in view">' + seg + '</div><ul class="ld-legend">' +
      p.shares.map(function (s) {
        return '<li tabindex="0" role="button" data-status="' + s.state + '" class="' + (sel === s.state ? 'on' : '') + '" style="--c:' + STATE_COLOR[s.state] + '" aria-pressed="' + (sel === s.state) + '"><span class="sw"></span><span>' + esc(s.label) + '</span><span class="n">' + num(s.n) + '</span><span class="p">' + esc(rateText(s.rate)) + '</span></li>';
      }).join('') + '</ul>';
    var r = p.on_time_rate;
    html += '<p class="ld-note">' + (p.decided ? '<b>On-time rate ' + esc(rateText(r)) + '</b>' + (r.pct == null ? ' <span>(too few leads for a percentage)</span>' : '') + ' of the ' + plural(p.decided, 'lead') + ' whose outcome is known. ' : 'No lead has a known outcome yet. ') +
      'Past SLA now: <b>' + num(p.past_sla_now) + '</b>' + (S.data && S.data.filters && S.data.filters.active ? ' in this view (with no filter set it is the number on the Today page).' : ' (the number on the Today page).') + '</p>';
    $('#ldSla').innerHTML = html;
  }
  function slaClick(e) {
    var t = e.target.closest ? e.target.closest('[data-status]') : null;
    if (!t || !$('#ldSla').contains(t)) return;
    var v = t.getAttribute('data-status');
    S.f.status = S.f.status === v ? null : v;
    changed();
  }
  $('#ldSla').addEventListener('click', slaClick);
  $('#ldSla').addEventListener('keydown', function (e) { if ((e.key === 'Enter' || e.key === ' ') && e.target.hasAttribute('data-status')) { e.preventDefault(); slaClick(e); } });

  // ------------------------------------------------------------------ time to first reply
  function renderTtfr(p, sla) {
    if (p.state === 'unavailable') { purge('#ldTtfr'); return na('#ldTtfr', p); }
    if (p.state === 'empty') {
      purge('#ldTtfr');
      var waiting = sla && sla.state === 'ok' ? sla.waiting : null;
      return empty('#ldTtfr', 'No reply on record yet',
        (waiting ? plural(waiting, 'lead') + (waiting === 1 ? ' is' : ' are') + ' waiting for a first reply. ' : '') +
        'A reply shows up here once someone comments on the lead&rsquo;s ClickUp task and the comment-pull job (<code>python -m erp.clickup_pull</code>) brings that comment into the database.');
    }
    if (!chartsOk()) return empty('#ldTtfr', 'Charts unavailable', 'plotly.js could not be loaded.');
    var bins = p.bins;
    var host = $('#ldTtfr');
    if (!host.querySelector('.ld-plot')) host.innerHTML = '<div class="ld-plot"></div><div class="ld-stats" id="ldTtfrStats"></div>';
    var ys = bins.map(function (b) { return b.n; }), max = Math.max.apply(null, ys.concat([1]));
    var lay = kit.baseLayout(250, { margin: { l: 34, r: 8, t: 22, b: 34 }, bargap: 0.22, yaxis: { tickformat: 'd', dtick: max > 6 ? undefined : 1, range: [0, max + Math.max(1, max * 0.18)] }, xaxis: { tickangle: 0, tickfont: { size: 11 } } });
    plot('#ldTtfr', [{ type: 'bar', x: bins.map(function (b) { return b.short; }), customdata: bins.map(function (b) { return b.label; }), y: ys, text: ys.map(function (v) { return v ? String(v) : ''; }), textposition: 'outside', cliponaxis: false,
      textfont: { family: D.font, size: 11 }, marker: { color: bins.map(function (b) { return b.n ? C.violet : kit.rgba(C.violet, 0.2); }), cornerradius: 6 },
      hovertemplate: '%{customdata}<br>%{y} lead(s)<extra></extra>' }], lay);
    watch('#ldTtfr');
    var stat = function (label, val, dim) { return '<div class="ld-stat' + (dim ? ' dim' : '') + '"><b>' + esc(val) + '</b><span>' + esc(label) + '</span></div>'; };
    var cfg = (S.data && S.data.config) || {};
    $('#ldTtfrStats').innerHTML = stat('MEDIAN · n = ' + p.n, p.median_text || '-') + stat('FASTEST', p.fastest_text || '-') + stat('SLOWEST', p.slowest_text || '-') +
      (p.p90_text ? stat('90TH PERCENTILE', p.p90_text) : stat('90TH PERCENTILE', 'needs ' + (cfg.p90_min_n || 10) + ' replies', true));
    $$('#ldTtfr .ld-note').forEach(function (el) { el.remove(); });
    if (p.n < (cfg.low_n || 5)) host.insertAdjacentHTML('beforeend', '<p class="ld-note">Only <b>' + plural(p.n, 'reply', 'replies') + '</b> stand behind this chart: too few to read a pattern into the shape.</p>');
  }

  // ------------------------------------------------------------------ leads per day
  function renderTrend(p) {
    if (p.state === 'unavailable') { purge('#ldTrend'); return na('#ldTrend', p); }
    if (p.state === 'empty') { purge('#ldTrend'); return empty('#ldTrend', 'No leads in this view', 'The daily trend appears as soon as a lead matches.'); }
    if (!chartsOk()) return empty('#ldTrend', 'Charts unavailable', 'plotly.js could not be loaded.');
    var ok = p.n.map(function (v, i) { return v - p.breached[i]; });
    var span = p.days.length, max = Math.max.apply(null, p.n.concat([1]));
    var lay = kit.baseLayout(250, { barmode: 'stack', bargap: 0.28, margin: { l: 30, r: 8, t: 10, b: 36 }, showlegend: false,
      xaxis: { type: 'date', tickformat: '%d %b', tickangle: 0, nticks: span > 14 ? 8 : span }, yaxis: { tickformat: 'd', dtick: max > 6 ? undefined : 1 } });
    plot('#ldTrend', [
      { type: 'bar', x: p.days, y: ok, name: 'Not breached', marker: { color: kit.rgba(C.blue, 0.85), cornerradius: 5 }, hovertemplate: '%{x|%a %d %b}<br>%{y} not breached<extra></extra>' },
      { type: 'bar', x: p.days, y: p.breached, name: 'Breached', marker: { color: C.red, cornerradius: 5 }, hovertemplate: '%{x|%a %d %b}<br>%{y} breached<extra></extra>' }
    ], lay, function (ev) {
      var pt = ev.points && ev.points[0]; if (!pt) return;
      var day = String(pt.x).slice(0, 10);
      if (S.f.from === day && S.f.to === day) { S.f.from = null; S.f.to = null; } else { S.f.from = day; S.f.to = day; }
      changed();
    });
    watch('#ldTrend');
    $$('#ldTrend .ld-note').forEach(function (el) { el.remove(); });
    var note = trendNote(p);
    if (note) $('#ldTrend').insertAdjacentHTML('beforeend', note);
  }
  function trendNote(p) {
    var quiet = 0;
    for (var i = p.n.length - 1; i >= 0 && p.n[i] === 0; i--) quiet++;
    if (quiet >= 2 && quiet < p.n.length) return '<p class="ld-note">No lead has arrived in the last <b>' + plural(quiet, 'day') + '</b> of this range.' + (p.clipped ? ' Showing the newest ' + p.days.length + ' days.' : '') + '</p>';
    return p.clipped ? '<p class="ld-note">Showing the newest ' + p.days.length + ' days of the range.</p>' : '';
  }

  // ------------------------------------------------------------------ by rep / by source
  function renderBreakdown(id, key, p, kind) {
    if (p.state === 'unavailable') return na(id, p);
    if (p.state === 'empty') return empty(id, 'No leads in this view', 'Loosen a filter to compare ' + kind + '.');
    var sel = S.f[key];
    var html = '<div class="ld-bt"><div class="ld-bt-head"><span>' + kind.toUpperCase() + '</span><span>LEADS</span><span>SLA OUTCOME</span><span>ON-TIME</span><span>MEDIAN REPLY</span></div>' +
      p.rows.map(function (r) {
        var on = sel.indexOf(r.name) >= 0;
        var mini = ['on_time', 'late', 'past_sla', 'pending', 'no_deadline'].filter(function (k) { return r.counts[k] > 0; }).map(function (k) {
          return '<i style="--c:' + STATE_COLOR[k] + ';flex:' + r.counts[k] + ' 1 0" title="' + esc(STATE_SHORT[k] + ': ' + r.counts[k]) + '"></i>'; }).join('');
        var rt = r.decided ? esc(rateText(r.on_time_rate)) + (r.on_time_rate.low_n ? '<small>too few</small>' : '<small>of ' + r.decided + '</small>') : '-<small>none decided</small>';
        var md = r.median_known === false ? 'n/a' : (r.median_text ? esc(r.median_text) + '<small>n = ' + r.replied + '</small>' : '-<small>no reply yet</small>');
        return '<button type="button" class="ld-bt-row' + (on ? ' on' : '') + '" data-multi="' + key + '" data-v="' + esc(r.name) + '" aria-pressed="' + on + '"><span class="nm" title="' + esc(r.name) + '">' + esc(kind === 'source' ? pretty(r.name) : r.name) + '</span>' +
          '<span class="num">' + num(r.n) + '</span><span class="ld-mini">' + mini + '</span><span class="rt">' + rt + '</span><span class="md">' + md + '</span></button>';
      }).join('') + (p.more ? '<div class="ld-bt-more">+ ' + p.more + ' more not shown (the ' + p.rows.length + ' largest are listed)</div>' : '') + '</div>';
    $(id).innerHTML = html;
  }
  $('#ldPSource').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-open-tab]') : null;
    if (b) setTab(b.getAttribute('data-open-tab'));
  });
  ['#ldRep', '#ldSource'].forEach(function (id) {
    $(id).addEventListener('click', function (e) {
      var b = e.target.closest ? e.target.closest('.ld-bt-row') : null;
      if (!b) return;
      toggle(S.f[b.getAttribute('data-multi')], b.getAttribute('data-v'));
      changed();
    });
  });

  // ------------------------------------------------------------------ detail table
  function renderTable(p) {
    var host = $('#ldTable');
    if (p.state === 'unavailable') return na('#ldTable', p);
    if (p.state === 'empty') { host.innerHTML = '<div class="ld-empty"><div><b>No lead matches these filters</b><span>Loosen a filter or press Reset all.</span></div></div>'; return; }
    var head = SORT_COLS.map(function (c) {
      if (!c[1]) return '<th class="nosort" scope="col">' + esc(c[0]) + '</th>';
      var on = p.sort === c[1];
      return '<th scope="col"' + (on ? ' aria-sort="' + (p.dir === 'asc' ? 'ascending' : 'descending') + '"' : '') + '><button type="button" data-sort="' + c[1] + '">' + esc(c[0]) + '<span class="ar" aria-hidden="true">' + (on ? (p.dir === 'asc' ? '▲' : '▼') : '↕') + '</span></button></th>';
    }).join('');
    var rows = p.rows.map(function (r) {
      var stages = [['assigned', 'Assigned to a rep'], ['analyzed', 'Analysed by AI'], ['task', 'ClickUp task'], ['update', 'Reply on record']];
      var dots = stages.map(function (s) { return '<i class="' + (r.stages[s[0]] ? 'on' : '') + '" title="' + esc(s[1] + (r.stages[s[0]] ? ': yes' : ': no')) + '"></i>'; }).join('');
      var label = stages.map(function (s) { return s[1] + (r.stages[s[0]] ? ' yes' : ' no'); }).join(', ');
      return '<tr><td class="id" data-label="ID">#' + r.lead_id + '</td><td class="when" data-label="Arrived">' + esc(fmtWhen(r.created_at)) + '</td>' +
        '<td class="who" data-label="Lead"><div><b>' + esc(r.name) + '</b>' + (r.company ? '<span>' + esc(r.company) + '</span>' : '') + '</div></td>' +
        '<td data-label="Source"><button type="button" class="ld-row-btn" data-multi="source" data-v="' + esc(r.source) + '" title="Filter by this source">' + esc(pretty(r.source)) + '</button></td>' +
        '<td data-label="Sales rep"><button type="button" class="ld-row-btn" data-multi="rep" data-v="' + esc(r.rep) + '" title="Filter by this rep">' + esc(r.rep) + '</button></td>' +
        '<td data-label="Progress"><span class="ld-dots" role="img" aria-label="' + esc(label) + '">' + dots + '</span></td>' +
        '<td data-label="SLA outcome"><div><span class="ld-badge" style="--c:' + (STATE_COLOR[r.state] || C.blue) + '"><i></i>' + esc(STATE_SHORT[r.state] || r.state_label) + '</span>' + (r.note ? '<span class="sub">' + esc(r.note) + '</span>' : '') + '</div></td>' +
        '<td class="hrs" data-label="First reply after">' + (r.hours == null ? '-' : r.hours < 0 ? '<span title="The reply is time-stamped before the lead arrived (data anomaly)">before arrival</span>' : esc(fmtHours(r.hours))) + '</td></tr>';
    }).join('');
    var sizes = (S.data.config.page_sizes || [10, 25, 50, 100]).map(function (n) { return '<option value="' + n + '"' + (n === p.page_size ? ' selected' : '') + '>' + n + '</option>'; }).join('');
    host.innerHTML = '<div class="ld-tbl-wrap"><table class="ld-tbl"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>' +
      '<div class="ld-pager"><span>Showing <b>' + p.from + '–' + p.to + '</b> of <b>' + num(p.total) + '</b></span><span class="sp"></span>' +
      '<label class="ld-sortsel">sort <select data-sortsel aria-label="Sort the leads by">' + SORT_COLS.filter(function (c) { return c[1]; }).map(function (c) { return '<option value="' + c[1] + '"' + (p.sort === c[1] ? ' selected' : '') + '>' + esc(c[0]) + '</option>'; }).join('') + '</select>' +
      '<button type="button" data-dir aria-label="Reverse the sort order">' + (p.dir === 'asc' ? '▲ asc' : '▼ desc') + '</button></label>' +
      '<label>per page <select data-size aria-label="Rows per page">' + sizes + '</select></label>' +
      '<button type="button" data-page="' + (p.page - 1) + '"' + (p.page <= 1 ? ' disabled' : '') + '>‹ Prev</button><span>page <b>' + p.page + '</b> of ' + p.pages + '</span>' +
      '<button type="button" data-page="' + (p.page + 1) + '"' + (p.page >= p.pages ? ' disabled' : '') + '>Next ›</button></div>';
  }
  function fmtHours(h) {
    var m = Math.round(h * 60);
    if (m < 1) return '< 1 min';
    if (m < 60) return m + ' min';
    var hh = Math.floor(m / 60), mm = m % 60;
    if (hh < 24) return hh + ' h' + (mm ? ' ' + pad(mm) + ' min' : '');
    return Math.floor(hh / 24) + ' d ' + (hh % 24) + ' h';
  }
  $('#ldTable').addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t) return;
    if (t.hasAttribute('data-sort')) {
      var k = t.getAttribute('data-sort');
      if (S.sort === k) S.dir = S.dir === 'asc' ? 'desc' : 'asc'; else { S.sort = k; S.dir = (k === 'created_at' || k === 'lead_id') ? 'desc' : 'asc'; }
      S.page = 1; return load(true);
    }
    if (t.hasAttribute('data-dir')) { S.dir = S.dir === 'asc' ? 'desc' : 'asc'; S.page = 1; return load(true); }
    if (t.hasAttribute('data-page') && !t.disabled) { S.page = Math.max(1, parseInt(t.getAttribute('data-page'), 10) || 1); return load(true); }
    if (t.hasAttribute('data-multi')) { var list = S.f[t.getAttribute('data-multi')]; toggle(list, t.getAttribute('data-v')); changed(); }
  });
  $('#ldTable').addEventListener('change', function (e) {
    if (e.target.hasAttribute('data-size')) { S.size = parseInt(e.target.value, 10) || 25; S.page = 1; load(true); }
    else if (e.target.hasAttribute('data-sortsel')) { S.sort = e.target.value; S.dir = (S.sort === 'created_at' || S.sort === 'lead_id') ? 'desc' : 'asc'; S.page = 1; load(true); }
  });

  // ------------------------------------------------------------------ definitions drawer
  function renderDefs(d) {
    var sig = JSON.stringify(d.definitions || []);
    if (S.sig.defs === sig) return;
    S.sig.defs = sig;
    $('#ldDefsBody').innerHTML = '<dl>' + (d.definitions || []).map(function (x) { return '<dt>' + esc(x.term) + '</dt><dd>' + esc(x.text) + '</dd>'; }).join('') + '</dl>' +
      '<p class="ld-dr-foot">Generated by the server from the same constants and settings the numbers use (LEAD_SLA_HOURS = ' + esc(String((d.config || {}).sla_hours)) + '), so this text cannot drift from the calculation.</p>';
  }
  function openDefs(open) {
    S.defsOpen = open;
    var dr = $('#ldDrawer');
    dr.classList.toggle('open', open);
    dr.setAttribute('aria-hidden', open ? 'false' : 'true');
    $('#ldScrim').hidden = !open;
    $('#ldDefsBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) setTimeout(function () { $('#ldDefsClose').focus({ preventScroll: true }); }, 60); else $('#ldDefsBtn').focus({ preventScroll: true });
  }
  $('#ldDefsBtn').addEventListener('click', function () { openDefs(!S.defsOpen); });
  $('#ldDefsClose').addEventListener('click', function () { openDefs(false); });
  $('#ldScrim').addEventListener('click', function () { openDefs(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.defsOpen) { e.stopPropagation(); openDefs(false); } });

  // ------------------------------------------------------------------ CSV download
  // L-101: the link is built from the export_url the SERVER put in the payload now on screen, never from live filter
  // state, so the file can never describe a set of leads the charts above it are not showing.
  function exportUrl(which) {
    var d = S.data;
    if (d) {
      if (which === 'cohorts' && d.export_cohort_url) return d.export_cohort_url;
      if (which !== 'cohorts' && d.export_url) return d.export_url;
    }
    return S.tab === 'sources' ? ('/api/leads/sources.csv?' + sourceParams() + '&part=' + (which === 'cohorts' ? 'cohorts' : 'sources'))
                               : ('/api/leads/export.csv?' + params(true));
  }
  function downloadCsv(which, btn) {
    if (S.csvBusy || S.busy || S.debounce) return;     // never export while a fetch or a debounced filter change is pending
    S.csvBusy = true;
    btn = btn || $('#ldCsv');
    btn.disabled = true;
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 60000);
    fetch(exportUrl(which), { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error((j && j.error) || ('HTTP ' + r.status)); }, function () { throw new Error('HTTP ' + r.status); });
      var name = /filename="?([^";]+)"?/.exec(r.headers.get('Content-Disposition') || '');
      var rows = r.headers.get('X-Row-Count'), cut = r.headers.get('X-Export-Truncated') === 'true';
      return r.blob().then(function (b) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(b); a.download = name ? name[1] : 'leads.csv';
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
        var unit = S.tab === 'sources' ? (which === 'cohorts' ? 'cohort day' : 'source') : 'lead';
        D.toast('Downloaded ' + (rows || '') + ' ' + unit + (rows === '1' ? '' : 's') + ' as ' + (name ? name[1] : 'leads.csv') + (cut ? ' - the file was cut at the export limit' : ''), cut ? 'warn' : 'ok', 5200);
      });
    }).catch(function (e) {
      D.toast('The CSV could not be downloaded: ' + (e && e.name === 'AbortError' ? 'it took too long' : (e && e.message) || 'unknown error'), 'bad', 6000);
    }).then(function () { clearTimeout(timer); S.csvBusy = false; btn.disabled = false; });
  }
  $('#ldCsv').addEventListener('click', function () { downloadCsv(null, this); });
  $('#sxCsvCohort').addEventListener('click', function () { if (!this.disabled) downloadCsv('cohorts', this); });

  // ------------------------------------------------------------------ render + load
  function renderAll(d) {
    S.sig.cur = d;
    syncStateShort(d.options);
    renderHead(d);
    renderFilters(d);
    renderKpis(d);
    if (S.tab === 'sources') return renderSourcesTab(d);
    var P = d.panels;
    renderFunnel(P.funnel);
    renderSla(P.sla);
    renderTtfr(P.ttfr, P.sla);
    renderTrend(P.trend);
    renderBreakdown('#ldRep', 'rep', P.by_rep, 'rep');
    renderBreakdown('#ldSource', 'source', P.by_source, 'source');
    renderTable(P.table);
    renderDefs(d);
    finish(d);
  }
  function renderSourcesTab(d) {
    var P = d.panels;
    if (D.pages && D.pages.sources) D.pages.sources.render(d, sourcesCtx);
    else $('#sxTable').innerHTML = '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>static/sources.js did not load, so this tab cannot be drawn.</span></div>';
    renderDefs(d);
    finish(d);
  }
  function finish(d) {
    var P = d.panels || {};
    var bad = Object.keys(P).filter(function (k) { return P[k].state === 'unavailable'; });
    $('#ldFoot').textContent = 'erp_support · PostgreSQL · read ' + fmtTime(d.generated_at) + ' · read-only' +
      (d.time_zone ? ' · day boundaries in ' + d.time_zone : '') + (bad.length ? ' · ' + bad.length + ' panel(s) unavailable' : '');
    $$('.ld-panel').forEach(function (el) { el.classList.remove('busy'); });
  }
  function showFailure(msg, offline) {
    setLive(offline ? 'offline' : 'stale');
    if (S.data) { tickAgo(); return; }
    $('#ldSub').textContent = msg;
    if (S.tab === 'sources') {
      if (D.pages && D.pages.sources) D.pages.sources.failure(msg);
      return;
    }
    ['#ldFunnel', '#ldSla', '#ldTtfr', '#ldTrend', '#ldRep', '#ldSource', '#ldTable'].forEach(function (id) {
      $(id).innerHTML = '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>' + esc(msg) + ' This page keeps trying by itself.</span></div>';
    });
  }
  function load(fresh) {
    if (S.busy) { S.again = true; return S.inflight || Promise.resolve(); }
    S.busy = true; S.again = false;
    clearTimeout(S.timer);
    setLive('syncing');
    $$('.ld-panel').forEach(function (el) { el.classList.add('busy'); });
    S.inflight = fetchAnalysis(fresh).then(function (x) {
      var d = x.body;
      if (x.status === 400 && d && d.problems) {                       // a stale / unknown filter value: drop exactly that filter and say so
        var names = {};
        d.problems.forEach(function (p) {
          names[p.param] = 1;
          if (p.param === 'rep' || p.param === 'source') S.f[p.param] = S.f[p.param].filter(function (v) { return v !== p.value && v.slice(0, 57) + '...' !== p.value; });
          else if (p.param === 'status' || p.param === 'sync') S.f[p.param] = null;
          else if (p.param === 'from' || p.param === 'to') S.f[p.param] = null;
          else if (p.param === 'page') S.page = 1;
          else if (p.param === 'cohort') S.v.cohort = null;            // a cohort that no longer exists (or belongs to another bucket)
          else if (p.param === 'bucket') S.v.bucket = null;            // fall back to the bucket the server picks
          else if (p.param === 'metric') S.v.metric = 'replied';
          else if (p.param === 'sort' || p.param === 'dir') { if (S.tab === 'sources') { S.v.sort = 'leads'; S.v.dir = 'desc'; } else { S.sort = 'created_at'; S.dir = 'desc'; } }
        });
        D.toast('A filter was not valid and was removed: ' + d.error, 'warn', 5200);
        S.sig.filters = null;
        if (++S.badLoops <= 3) S.again = true; else showFailure(d.error, false);
        return;
      }
      S.badLoops = 0;
      if (x.status !== 200 || !d || !d.panels) {
        var msg = (d && d.error) || ('The Leads feed answered ' + x.status);
        S.err = msg; S.fails++;
        showFailure(msg, false);
        if (fresh) D.toast(msg, 'bad', 5200);
        return;
      }
      var gen = Date.parse(d.generated_at);
      S.data = d; S.fails = 0; S.err = '';
      S.page = d.panels.table && d.panels.table.page ? d.panels.table.page : S.page;
      S.at = isNaN(gen) ? Date.now() : Math.min(Date.now(), gen);
      var old = Date.now() - S.at > OLD_AFTER_MS;
      if (old) S.err = 'The database read is taking too long';
      if (d.ok && !old) setLive('live'); else if (old) setLive('stale'); else setLive('stale', 'PARTIAL');
      renderAll(d);
      tickAgo();
    }).catch(function (e) {
      var slow = !!(e && (e.timeout || e.name === 'AbortError'));
      S.fails++;
      S.err = slow ? 'The data service did not answer within ' + (FETCH_TIMEOUT_MS / 1000) + ' s' : 'Could not reach the data service';
      showFailure(S.err + '.', !slow && !S.data && S.fails >= 3);
      if (fresh) D.toast(S.err, 'bad', 5200);
    }).then(function () {
      S.busy = false;
      $$('.ld-panel').forEach(function (el) { el.classList.remove('busy'); });
      if (S.again) { S.again = false; return load(true); }
      schedule();
    });
    return S.inflight;
  }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { if (active() && !document.hidden) load(false); else schedule(); }, POLL_MS);
  }
  function active() { return D.view() === 'leads'; }
  function play() { if (S.played) return; S.played = true; $('#ld').classList.add('play'); }

  $('#ldRefresh').addEventListener('click', function () {
    var b = this; b.classList.add('spinning'); b.disabled = true;
    load(true).then(function () { setTimeout(function () { b.classList.remove('spinning'); b.disabled = false; }, 400); });
  });
  D.on('refresh-request', function () { if (active()) $('#ldRefresh').click(); });
  D.on('ready', play);
  D.on('view', function (name) {
    if (name !== 'leads') return;
    play();
    moveTabInd();
    if (!S.data || Date.now() - S.at > 5000) load(false);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && active() && Date.now() - S.at > POLL_MS) load(false); });
  setInterval(tickAgo, 1000);
  setTimeout(play, 6000);                                            // safety net: never leave the page invisible if the splash event was missed
  moveTabInd();
  D.fontsReady(moveTabInd);                                          // the web font changes the tab widths the pill is measured from
  setLive('connecting');
  if (active()) load(false);                                         // L-080: opened straight on #leads - this script missed the 'view' event
})();
