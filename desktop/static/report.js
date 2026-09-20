/* ERP Desk - live Reporting page.
   Pulls one JSON snapshot from /api/report/data (kept fresh by the server-side refresher),
   and renders linked dashboards from it. Every panel reads the same filter state, so clicking
   a rep / source / day / score band / funnel stage anywhere re-filters and highlights the rest.
   Rule used by every chart ("cross-filter"): a chart is computed with all filters EXCEPT its own
   dimension, and shows its own selection as a highlight - so you can always see the alternatives. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var esc = D.esc;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };

  var C = { ink: '#1c1d22', dim: '#6b6f76', blue: '#3452eb', coral: '#ff5a36', green: '#12b886', violet: '#7c5cff', amber: '#f2b705', red: '#e0393e', line: '#e7e2d8', grid: '#f2efe8' };
  var PALETTE = [C.blue, C.coral, C.green, C.violet, C.amber, '#0ea5c6', '#c2417d', '#8a8f98'];
  var DAY = 864e5;
  var RANGES = { all: { label: 'All time' }, '30d': { label: '30 days', ms: 30 * DAY }, '7d': { label: '7 days', ms: 7 * DAY }, '24h': { label: '24 hours', ms: DAY } };
  var RANGE_ORDER = ['all', '30d', '7d', '24h'];
  var STAGES = [
    { key: 'received', label: 'Lead received', flag: null, color: C.coral },
    { key: 'assigned', label: 'Assigned to Sales', flag: 'st_assigned', color: '#ff7a4a' },
    { key: 'analyzed', label: 'Analyzed by AI', flag: 'st_analyzed', color: C.violet },
    { key: 'task', label: 'ClickUp task created', flag: 'st_task', color: C.blue },
    { key: 'update', label: 'Follow-up logged', flag: 'st_update', color: C.green }
  ];
  var BANDS = [
    { label: '0-19', color: C.red }, { label: '20-39', color: '#ff8a4c' }, { label: '40-59', color: C.amber },
    { label: '60-79', color: C.blue }, { label: '80-100', color: C.green }
  ];
  var SVG_FONT_SERIF = "'Fraunces', Georgia, serif";

  // ------------------------------------------------------------------ state
  var S = {
    d: null, version: null, generatedAt: null, refreshedAt: null, serverError: null,
    f: { range: 'all', rep: null, source: null, sla: 'all', day: null, band: null, stage: null },
    focus: null, hover: null,
    pollSec: parseInt(D.store('erpdesk.poll') || '', 10), live: 'connecting', fails: 0, pollTimer: null, lastChangeAt: null,
    digest: null, digestTimer: null, rendered: false, dotsSig: ''
  };
  if (isNaN(S.pollSec)) S.pollSec = parseInt((document.querySelector('meta[name="erp-refresh-seconds"]') || {}).content || '10', 10) || 10;
  if ([0, 5, 10, 30, 60].indexOf(S.pollSec) < 0) S.pollSec = 10;

  // ---------------------------------------------------------------- helpers
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function dayKey(ms) { var d = new Date(ms); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); }
  function localStr(ms) { var d = new Date(ms); return dayKey(ms) + ' ' + pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function fmtDay(k) { var p = k.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString('en-GB', { day: 'numeric', month: 'short' }); }
  function fmtLongDay(k) { var p = k.split('-'); return new Date(+p[0], +p[1] - 1, +p[2]).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short' }); }
  function fmtTime(ms) { var d = new Date(ms); return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds()); }
  function fmtNum(v, dec) { return Number(v).toLocaleString('en-US', { minimumFractionDigits: dec, maximumFractionDigits: dec }); }
  function humanDur(ms) {
    ms = Math.abs(ms);
    var m = Math.round(ms / 60000), h = Math.floor(m / 60), d = Math.floor(h / 24);
    if (d >= 1) return d + 'd ' + (h % 24) + 'h';
    if (h >= 1) return h + 'h ' + (m % 60) + 'm';
    return Math.max(1, m) + 'm';
  }
  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 60) return s + 's ago';
    if (s < 3600) return Math.floor(s / 60) + 'm ago';
    return Math.floor(s / 3600) + 'h ago';
  }
  function bandOf(score) { return score == null ? -1 : Math.min(4, Math.max(0, Math.floor(score / 20))); }
  function initials(name) { return String(name).split(/\s+/).filter(Boolean).map(function (w) { return w[0]; }).slice(0, 2).join('').toUpperCase() || '?'; }
  function rgba(hex, a) { var n = parseInt(hex.slice(1), 16); return 'rgba(' + (n >> 16) + ',' + ((n >> 8) & 255) + ',' + (n & 255) + ',' + a + ')'; }
  function md(text) { return esc(text).replace(/\*\*(.+?)\*\*/g, '<b>$1</b>'); }   // escape first, then allow bold only
  function has(ex, dim) { return ex && ex.indexOf(dim) >= 0; }

  // ------------------------------------------------------------ data prep
  function prep(raw) {
    var leads = raw.leads.map(function (l) {
      var t = Date.parse(l.created_at);
      return {
        id: l.lead_id, name: l.full_name || 'Unnamed', company: l.company || '', src: l.source || 'unknown',
        rep: l.assigned_sales_rep || 'Unassigned', t: t, day: dayKey(t), score: l.potential_score == null ? null : l.potential_score,
        band: bandOf(l.potential_score), scale: l.scale_estimate || '', breached: !!l.sla_breached, due: Date.parse(l.sla_due_at) || null,
        st_assigned: !!l.st_assigned, st_analyzed: !!l.st_analyzed, st_task: !!l.st_task, st_update: !!l.st_update
      };
    });
    var tickets = raw.tickets.map(function (k) {
      var t = Date.parse(k.created_at);
      return { code: k.ticket_code, dept: k.department_name || '', status: k.status || 'open', priority: k.priority || '', rep: k.assignee_name || 'Unassigned',
               t: t, day: dayKey(t), breached: !!k.sla_breached, open: !/^(resolved|closed)$/.test(k.status || '') };
    });
    var timeline = raw.timeline.map(function (r) {
      var s = Date.parse(r.start_time);
      return { type: r.task_type, name: r.task_name, rep: r.employee || 'Unassigned', s: s, e: Date.parse(r.end_time) || s + DAY, breached: !!r.sla_breached, t: s, day: dayKey(s) };
    });
    var repSet = {}, srcSet = {};
    leads.forEach(function (l) { repSet[l.rep] = 1; srcSet[l.src] = (srcSet[l.src] || 0) + 1; });
    timeline.forEach(function (r) { repSet[r.rep] = 1; });
    tickets.forEach(function (k) { repSet[k.rep] = 1; });
    var reps = Object.keys(repSet).sort();
    var sources = Object.keys(srcSet).sort();
    return { leads: leads, tickets: tickets, timeline: timeline, insight: raw.insight, reps: reps, sources: sources, srcCount: srcSet };
  }
  function repColor(rep) { var i = S.d.reps.indexOf(rep); return PALETTE[(i < 0 ? 0 : i) % PALETTE.length]; }
  function srcColor(src) { var i = S.d.sources.indexOf(src); return PALETTE[(i < 0 ? 0 : i) % PALETTE.length]; }
  function prettySrc(s) { return String(s).replace(/_/g, ' '); }

  // --------------------------------------------------------------- filters
  function passRange(t) { var r = S.f.range; return r === 'all' || t == null || t >= Date.now() - RANGES[r].ms; }
  function passCommon(o, ex) {
    if (!has(ex, 'range') && !passRange(o.t)) return false;
    if (!has(ex, 'rep') && S.f.rep && o.rep !== S.f.rep) return false;
    if (!has(ex, 'sla') && S.f.sla !== 'all' && (S.f.sla === 'breached') !== o.breached) return false;
    if (!has(ex, 'day') && S.f.day && o.day !== S.f.day) return false;
    return true;
  }
  function passLead(l, ex) {
    if (!passCommon(l, ex)) return false;
    if (!has(ex, 'source') && S.f.source && l.src !== S.f.source) return false;
    if (!has(ex, 'band') && S.f.band !== null && l.band !== S.f.band) return false;
    if (!has(ex, 'stage') && S.f.stage) {
      var st = STAGES.filter(function (s) { return s.key === S.f.stage; })[0];
      if (st && st.flag && !l[st.flag]) return false;
    }
    return true;
  }
  function leadsF(ex) { return S.d.leads.filter(function (l) { return passLead(l, ex); }); }
  function ticketsF(ex) { return S.d.tickets.filter(function (k) { return passCommon(k, ex); }); }
  function timelineF(ex) { return S.d.timeline.filter(function (r) { return passCommon(r, ex); }); }
  function anyFilter() { var f = S.f; return f.range !== 'all' || f.rep || f.source || f.sla !== 'all' || f.day || f.band !== null || f.stage; }

  function setFilter(dim, val) {
    var f = S.f;
    if (dim === 'range' || dim === 'sla') f[dim] = (f[dim] === val && val !== 'all') ? 'all' : val;
    else f[dim] = (f[dim] === val) ? null : val;
    if (dim === 'stage' && val === 'received') f.stage = null;
    renderAll(true);
  }
  function resetFilters() { S.f = { range: 'all', rep: null, source: null, sla: 'all', day: null, band: null, stage: null }; renderAll(true); }

  // ---------------------------------------------------------------- header / banner / live
  function setLive(state) {
    S.live = state;
    var pill = $('#livePill');
    pill.setAttribute('data-state', state);
    $('#liveLabel').textContent = { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', paused: 'PAUSED', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#liveAgo'), pill = $('#livePill');
    if (S.refreshedAt) {
      el.textContent = 'synced ' + ago(S.refreshedAt);
      pill.title = 'DB checked ' + fmtTime(S.refreshedAt) + (S.lastChangeAt ? ' · data last changed ' + fmtTime(S.lastChangeAt) : '') +
        (S.serverError ? ' · database error: ' + S.serverError : '') + (S.pollSec ? ' · page checks every ' + S.pollSec + 's' : ' · auto-refresh paused');
    } else el.textContent = '';
  }
  function insightFor(L) {
    var d = S.d;
    if (!anyFilter()) return [d.insight.headline, d.insight.sub];
    if (!L.length) return ['Nothing matches this view.', 'Loosen a filter (or press Reset) to bring leads back into the picture.'];
    var breached = L.filter(function (l) { return l.breached; }).length, pct = breached / L.length * 100;
    var counts = {}; L.forEach(function (l) { counts[l.rep] = (counts[l.rep] || 0) + 1; });
    var top = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; })[0];
    var head = pct >= 99.5 ? 'Every lead in this view has breached its SLA.' : (pct === 0 ? 'Zero SLA breaches in this view.' : Math.round(pct) + '% of the leads in this view have breached SLA.');
    return [head, 'In this filtered view: ' + breached + ' of ' + L.length + ' lead(s) are overdue. ' + top + ' carries the most with ' + counts[top] + '.'];
  }
  function renderBanner(L) {
    var pair = insightFor(L), b = $('#banner'), h = $('#bannerHead'), s = $('#bannerSub');
    if (h.textContent === pair[0] && s.textContent === pair[1]) return;
    var first = !S.rendered;
    if (first) { h.textContent = pair[0]; s.textContent = pair[1]; return; }
    b.classList.add('swap');
    setTimeout(function () { h.textContent = pair[0]; s.textContent = pair[1]; b.classList.remove('swap'); }, 260);
  }

  // ---------------------------------------------------------------- filter bar
  function renderFilterbar() {
    var d = S.d, f = S.f, html = '';
    html += '<div class="fgroup"><span class="fgroup-label">RANGE</span><div class="seg" data-seg="range">' +
      RANGE_ORDER.map(function (k) { return '<button data-v="' + k + '" class="' + (f.range === k ? 'on' : '') + '">' + RANGES[k].label + '</button>'; }).join('') + '</div></div>';
    html += '<div class="fgroup"><span class="fgroup-label">SALES REP</span>' + d.reps.map(function (r) {
      return '<button class="pill ' + (f.rep === r ? 'on' : '') + '" data-pill="rep" data-v="' + esc(r) + '" style="--c:' + repColor(r) + '"><i></i>' + esc(r) + '</button>'; }).join('') + '</div>';
    html += '<div class="fgroup"><span class="fgroup-label">SOURCE</span>' + d.sources.slice(0, 8).map(function (s) {
      return '<button class="pill ' + (f.source === s ? 'on' : '') + '" data-pill="source" data-v="' + esc(s) + '" style="--c:' + srcColor(s) + '"><i></i>' + esc(prettySrc(s)) + '</button>'; }).join('') + '</div>';
    html += '<div class="fgroup"><span class="fgroup-label">SLA</span><div class="seg" data-seg="sla">' +
      [['all', 'All'], ['breached', 'Overdue'], ['ok', 'On time']].map(function (p) { return '<button data-v="' + p[0] + '" class="' + (f.sla === p[0] ? 'on' : '') + '">' + p[1] + '</button>'; }).join('') + '</div></div>';
    var shown = leadsF().length;
    html += '<div class="fcount"><span><b>' + shown + '</b> of ' + d.leads.length + ' leads shown</span>' + (anyFilter() ? '<button class="linkbtn" data-reset>Reset all</button>' : '') + '</div>';
    var chips = [];
    if (f.range !== 'all') chips.push(['range', 'RANGE', RANGES[f.range].label]);
    if (f.rep) chips.push(['rep', 'REP', f.rep]);
    if (f.source) chips.push(['source', 'SOURCE', prettySrc(f.source)]);
    if (f.sla !== 'all') chips.push(['sla', 'SLA', f.sla === 'breached' ? 'Overdue' : 'On time']);
    if (f.day) chips.push(['day', 'DAY', fmtLongDay(f.day)]);
    if (f.band !== null) chips.push(['band', 'SCORE', BANDS[f.band].label]);
    if (f.stage) chips.push(['stage', 'STAGE', STAGES.filter(function (s) { return s.key === f.stage; })[0].label]);
    html += '<div class="chips">' + chips.map(function (c) { return '<span class="chip"><em>' + c[1] + '</em>' + esc(c[2]) + '<button data-clear="' + c[0] + '" aria-label="Clear ' + c[1] + ' filter">×</button></span>'; }).join('') + '</div>';
    $('#filterbar').innerHTML = html;
  }
  $('#filterbar').addEventListener('click', function (e) {
    var t = e.target.closest('button'); if (!t) return;
    if (t.hasAttribute('data-reset')) return resetFilters();
    if (t.hasAttribute('data-clear')) {
      var dim = t.getAttribute('data-clear');
      S.f[dim] = (dim === 'range' || dim === 'sla') ? (dim === 'range' ? 'all' : 'all') : null;
      return renderAll(true);
    }
    var seg = t.closest('[data-seg]');
    if (seg) return setFilter(seg.getAttribute('data-seg'), t.getAttribute('data-v'));
    if (t.hasAttribute('data-pill')) setFilter(t.getAttribute('data-pill'), t.getAttribute('data-v'));
  });
  $('#reportScroll').addEventListener('scroll', function () {
    var fb = $('#filterbar');
    fb.classList.toggle('stuck', this.scrollTop > fb.offsetTop + 6);
  }, { passive: true });

  // ---------------------------------------------------------------- KPIs
  var KPI_DEFS = [
    { id: 'leads', label: 'Leads in view', color: C.blue, dec: 0, suffix: '' },
    { id: 'score', label: 'Avg. potential score', color: C.violet, dec: 1, suffix: '' },
    { id: 'breach', label: 'SLA breach rate', color: C.red, dec: 0, suffix: '%' },
    { id: 'tasks', label: 'ClickUp tasks', color: C.green, dec: 0, suffix: '' },
    { id: 'tickets', label: 'Open tickets', color: C.amber, dec: 0, suffix: '' }
  ];
  function buildKpis() {
    $('#kpis').innerHTML = KPI_DEFS.map(function (k, i) {
      return '<div class="kpi" data-k="' + k.id + '" style="--kc:' + k.color + ';animation-delay:' + (i * 70) + 'ms">' +
        '<div class="kpi-label">' + k.label + '</div><div class="kpi-value" data-v>0</div><div class="kpi-cap" data-cap>&nbsp;</div><div class="kpi-viz" data-viz></div></div>';
    }).join('');
  }
  function tween(el, to, dec, suffix, dur) {
    var from = el._v == null ? 0 : el._v;
    el._v = to;
    if (el._raf) cancelAnimationFrame(el._raf);
    if (from === to || document.hidden) { el.textContent = fmtNum(to, dec) + suffix; return; }   // rAF is paused in hidden/minimized windows
    var t0 = null;
    (function step(ts) {
      if (!t0) t0 = ts;
      var p = Math.min((ts - t0) / (dur || 800), 1), e = 1 - Math.pow(1 - p, 3);
      el.textContent = fmtNum(from + (to - from) * e, dec) + suffix;
      if (p < 1) el._raf = requestAnimationFrame(step); else el.textContent = fmtNum(to, dec) + suffix;
    })(performance.now());
  }
  function sparkSvg(vals) {
    var w = 74, h = 44, n = vals.length, max = Math.max.apply(null, vals.concat([1]));
    if (n < 2) return '';
    var pts = vals.map(function (v, i) { return [i * (w / (n - 1)), h - 4 - (v / max) * (h - 10)]; });
    var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(1) + ' ' + p[1].toFixed(1); }).join(' ');
    var last = pts[n - 1];
    return '<svg viewBox="0 0 ' + w + ' ' + h + '"><path class="spark-area" d="' + line + ' L' + w + ' ' + h + ' L0 ' + h + ' Z"/><path class="spark-line" pathLength="1" d="' + line + '"/><circle class="spark-dot" cx="' + last[0] + '" cy="' + last[1] + '" r="3"/></svg>';
  }
  function ringSvg(frac) {
    var r = 18, c = 2 * Math.PI * r;
    return '<svg viewBox="0 0 44 44"><circle class="ring-bg" cx="22" cy="22" r="' + r + '"/><circle class="ring-fg" cx="22" cy="22" r="' + r + '" stroke-dasharray="' + c.toFixed(2) + '" stroke-dashoffset="' + (c * (1 - Math.max(0, Math.min(1, frac)))).toFixed(2) + '"/></svg>';
  }
  function renderKpis(L) {
    var d = S.d;
    var scores = L.filter(function (l) { return l.score != null; }).map(function (l) { return l.score; });
    var avg = scores.length ? scores.reduce(function (a, b) { return a + b; }, 0) / scores.length : 0;
    var nb = L.filter(function (l) { return l.breached; }).length;
    var pct = L.length ? nb / L.length * 100 : 0;
    var tasks = L.filter(function (l) { return l.st_task; }).length;
    var openT = ticketsF().filter(function (k) { return k.open; }).length;
    // sparkline: leads per day over the last 14 days (all filters except time)
    var base = leadsF(['range', 'day']), days = [], end = new Date(); end.setHours(0, 0, 0, 0);
    var per = {}; base.forEach(function (l) { per[l.day] = (per[l.day] || 0) + 1; });
    for (var i = 13; i >= 0; i--) days.push(per[dayKey(new Date(end.getFullYear(), end.getMonth(), end.getDate() - i).getTime())] || 0);
    var vals = {
      leads: [L.length, 'of ' + d.leads.length + ' total in the pipeline', sparkSvg(days)],
      score: [avg, scores.length + ' AI-scored lead(s), 0-100', ringSvg(avg / 100)],
      breach: [pct, nb + ' overdue of ' + L.length + ' active', ringSvg(pct / 100)],
      tasks: [tasks, 'created from leads', ringSvg(L.length ? tasks / L.length : 0)],
      tickets: [openT, 'of ' + d.tickets.length + ' ticket(s) in the system', '']
    };
    KPI_DEFS.forEach(function (k) {
      var tile = $('.kpi[data-k="' + k.id + '"]'), v = vals[k.id], valEl = $('[data-v]', tile);
      if (S.rendered && valEl._v != null && valEl._v !== v[0] && !tile._flashing) {
        tile.classList.add('flash'); tile._flashing = true;
        setTimeout(function () { tile.classList.remove('flash'); tile._flashing = false; }, 1300);
      }
      tween(valEl, v[0], k.dec, k.suffix);
      $('[data-cap]', tile).textContent = v[1];
      var viz = $('[data-viz]', tile);
      if (k.id === 'leads') { if (viz._sig !== v[2]) { viz.innerHTML = v[2]; viz._sig = v[2]; } }
      else if (viz.firstChild && viz.querySelector('.ring-fg') && v[2]) {           // animate the existing ring instead of replacing it
        var tmp = document.createElement('div'); tmp.innerHTML = v[2];
        viz.querySelector('.ring-fg').setAttribute('stroke-dashoffset', tmp.querySelector('.ring-fg').getAttribute('stroke-dashoffset'));
      } else {
        viz.innerHTML = v[2];
        var fg = viz.querySelector('.ring-fg');
        if (fg) {                                                                     // first paint: sweep the ring in from empty
          var target = fg.getAttribute('stroke-dashoffset');
          fg.setAttribute('stroke-dashoffset', fg.getAttribute('stroke-dasharray'));
          setTimeout(function () { fg.setAttribute('stroke-dashoffset', target); }, 80);
        }
      }
    });
  }

  // ---------------------------------------------------------------- funnel
  function renderFunnel() {
    var L = leadsF(['stage']), max = Math.max(1, L.length), box = $('#pFunnel');
    if (!box._built) {
      box.innerHTML = '<div class="funnel">' + STAGES.map(function (s) {
        return '<button class="f-row" data-stage="' + s.key + '" style="--fc:' + s.color + '"><div class="f-meta"><span class="f-label">' + s.label +
          '</span><span class="f-nums"><b data-n>0</b><span data-p></span></span></div><div class="f-track"><div class="f-bar"></div></div></button>';
      }).join('') + '</div>';
      box.addEventListener('click', function (e) { var r = e.target.closest('.f-row'); if (r) setFilter('stage', r.getAttribute('data-stage')); });
      box._built = true;
    }
    var counts = STAGES.map(function (s) { return s.flag ? L.filter(function (l) { return l[s.flag]; }).length : L.length; });
    STAGES.forEach(function (s, i) {
      var row = $('.f-row[data-stage="' + s.key + '"]', box), c = counts[i];
      row.classList.toggle('sel', S.f.stage === s.key);
      row.classList.toggle('dim', !!S.f.stage && S.f.stage !== s.key);
      tween($('[data-n]', row), c, 0, '', 700);
      $('[data-p]', row).textContent = L.length ? Math.round(c / L.length * 100) + '%' : '—';
      var bar = $('.f-bar', row);
      requestAnimationFrame(function () { bar.style.width = (c / max * 100) + '%'; });
    });
  }

  // ---------------------------------------------------------------- Plotly plumbing
  function baseLayout(h, extra) {
    var lay = {
      paper_bgcolor: 'rgba(0,0,0,0)', plot_bgcolor: 'rgba(0,0,0,0)', height: h, showlegend: false,
      font: { family: "'Public Sans', sans-serif", color: C.ink, size: 12 }, margin: { l: 40, r: 14, t: 8, b: 34 },
      hoverlabel: { bgcolor: C.ink, bordercolor: C.ink, font: { family: "'IBM Plex Mono', monospace", size: 11.5, color: '#fff' } },
      xaxis: { showgrid: false, zeroline: false, showline: true, linecolor: C.line, ticks: 'outside', tickcolor: C.line, ticklen: 5, fixedrange: true },
      yaxis: { showgrid: true, gridcolor: C.grid, zeroline: false, showline: false, fixedrange: true, rangemode: 'tozero' },
      transition: { duration: 450, easing: 'cubic-in-out' }
    };
    Object.keys(extra || {}).forEach(function (k) {
      lay[k] = (typeof extra[k] === 'object' && !Array.isArray(extra[k]) && lay[k]) ? Object.assign({}, lay[k], extra[k]) : extra[k];
    });
    return lay;
  }
  function draw(id, traces, layout, onClick) {
    var el = document.getElementById(id);
    if (!window.Plotly) { el.innerHTML = '<div class="empty"><div><b>Charts unavailable</b>plotly.js could not be loaded.</div></div>'; return; }
    if (el._empty) { el.innerHTML = ''; el._empty = false; }
    Plotly.react(el, traces, layout, { displayModeBar: false, responsive: true });
    if (onClick && !el._bound) { el._bound = true; el.on('plotly_click', onClick); }
    observeResize(el);
  }
  function emptyPlot(id, title, sub) {
    var el = document.getElementById(id);
    if (window.Plotly && el.data) { try { Plotly.purge(el); } catch (e) { /* ignore */ } }
    el._bound = false; el._empty = true;
    el.innerHTML = '<div class="empty"><div><b>' + esc(title) + '</b>' + esc(sub || '') + '</div></div>';
  }
  var ro = window.ResizeObserver ? new ResizeObserver(function (entries) {
    entries.forEach(function (en) {
      var el = en.target; clearTimeout(el._rz);
      el._rz = setTimeout(function () { if (window.Plotly && el.data && el.offsetWidth) { try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ } } }, 120);
    });
  }) : null;
  function observeResize(el) { if (ro && !el._ro) { el._ro = true; ro.observe(el); } }

  // ---------------------------------------------------------------- source donut
  function renderSource() {
    var L = leadsF(['source']), legend = $('#pSourceLegend');
    if (!L.length) { legend.innerHTML = ''; return emptyPlot('pSource', 'No leads in this view', 'Change a filter to see the source mix.'); }
    var counts = {}; L.forEach(function (l) { counts[l.src] = (counts[l.src] || 0) + 1; });
    var labels = Object.keys(counts).sort(function (a, b) { return counts[b] - counts[a]; });
    var sel = S.f.source;
    legend.innerHTML = labels.map(function (s) {
      return '<button class="pill ' + (sel === s ? 'on' : '') + '" data-src="' + esc(s) + '" style="--c:' + srcColor(s) + '"><i></i>' + esc(prettySrc(s)) + ' <span class="mono">' + counts[s] + '</span></button>';
    }).join('');
    draw('pSource', [{
      type: 'pie', labels: labels.map(prettySrc), customdata: labels, values: labels.map(function (s) { return counts[s]; }), hole: 0.62, sort: false,
      marker: { colors: labels.map(function (s) { return sel && s !== sel ? rgba(srcColor(s), 0.28) : srcColor(s); }), line: { color: '#fff', width: 3 } },
      pull: labels.map(function (s) { return s === sel ? 0.08 : 0; }), textinfo: 'percent', textfont: { color: '#fff', family: "'IBM Plex Mono', monospace", size: 11 },
      hovertemplate: '%{label}<br>%{value} lead(s) · %{percent}<extra></extra>', direction: 'clockwise', rotation: 20
    }], baseLayout(250, {
      margin: { l: 8, r: 8, t: 8, b: 8 },
      annotations: [{ text: '<b>' + L.length + '</b><br>leads', showarrow: false, x: 0.5, y: 0.5, xref: 'paper', yref: 'paper', font: { family: SVG_FONT_SERIF, size: 20, color: C.ink } }]
    }), function (ev) { var p = ev.points && ev.points[0]; if (p) setFilter('source', labels[p.pointNumber]); });
  }

  $('#pSourceLegend').addEventListener('click', function (e) { var b = e.target.closest('[data-src]'); if (b) setFilter('source', b.getAttribute('data-src')); });

  // ---------------------------------------------------------------- dot grid
  function slaText(l) {
    if (!l.due) return l.breached ? 'SLA breached' : 'No SLA date';
    return l.breached ? 'Overdue by ' + humanDur(Date.now() - l.due) : 'Due in ' + humanDur(l.due - Date.now());
  }
  function renderDots() {
    var d = S.d, box = $('#pDots');
    if (!d.leads.length) { box.innerHTML = '<div class="empty"><div><b>No leads assigned yet</b>Once a lead arrives, every lead gets its own square here.</div></div>'; box._sig = ''; return; }
    var sig = d.leads.map(function (l) { return l.id + ':' + l.rep + ':' + (l.breached ? 1 : 0); }).join('|');
    if (box._sig !== sig) {
      var groups = {}; d.leads.forEach(function (l) { (groups[l.rep] = groups[l.rep] || []).push(l); });
      var order = Object.keys(groups).sort(function (a, b) { return groups[b].length - groups[a].length; }), n = 0;
      box.innerHTML = order.map(function (rep) {
        return '<div class="dotrow" data-rep="' + esc(rep) + '"><div class="dotrow-head"><button class="rep-name" data-rep-btn="' + esc(rep) + '"><span class="av" style="background:' + repColor(rep) + '">' +
          esc(initials(rep)) + '</span>' + esc(rep) + '</button><span class="dotrow-meta" data-meta></span></div><div class="dotgrid">' +
          groups[rep].map(function (l) { return '<button class="sq ' + (l.breached ? 'breach' : 'ok') + '" data-id="' + l.id + '" style="--i:' + (n++) + '" aria-label="' + esc(l.name) + '"></button>'; }).join('') + '</div></div>';
      }).join('') + '<div class="legend"><span><i style="background:' + C.green + '"></i>on time</span><span><i style="background:' + C.red + '"></i>SLA breached</span></div>';
      box._sig = sig;
    }
    var byId = {}; d.leads.forEach(function (l) { byId[l.id] = l; });
    $$('.sq', box).forEach(function (sq) { sq.classList.toggle('dim', !passLead(byId[sq.getAttribute('data-id')])); });
    $$('.dotrow', box).forEach(function (row) {
      var rep = row.getAttribute('data-rep'), mine = d.leads.filter(function (l) { return l.rep === rep; });
      var vis = mine.filter(function (l) { return passLead(l); }), over = vis.filter(function (l) { return l.breached; }).length;
      $('[data-meta]', row).textContent = vis.length + ' of ' + mine.length + ' · ' + over + ' overdue';
      row.classList.toggle('sel', S.f.rep === rep);
      row.classList.toggle('dim', !!S.f.rep && S.f.rep !== rep);
    });
    applyLinks();
  }
  (function bindDots() {
    var box = $('#pDots'), tip = $('#tip');
    box.addEventListener('click', function (e) {
      var b = e.target.closest('[data-rep-btn]');
      if (b) return setFilter('rep', b.getAttribute('data-rep-btn'));
      var sq = e.target.closest('.sq'); if (sq) toggleFocus(+sq.getAttribute('data-id'));
    });
    box.addEventListener('mouseover', function (e) { var sq = e.target.closest('.sq'); if (sq) { S.hover = +sq.getAttribute('data-id'); applyLinks(); showTip(sq, e); } });
    box.addEventListener('mousemove', function (e) { if (tip.classList.contains('show')) moveTip(e); });
    box.addEventListener('mouseout', function (e) { if (e.target.closest('.sq')) { S.hover = null; applyLinks(); tip.classList.remove('show'); } });
  })();
  function showTip(sq, e) {
    var l = S.d.leads.filter(function (x) { return x.id === +sq.getAttribute('data-id'); })[0]; if (!l) return;
    var tip = $('#tip');
    tip.innerHTML = '<b>' + esc(l.name) + '</b><br>' + esc(l.company || 'no company') + '<br><span class="mono">' + esc(prettySrc(l.src)) + ' · score ' + (l.score == null ? 'n/a' : l.score) + '<br>' + esc(slaText(l)) + '</span>';
    tip.classList.add('show'); moveTip(e);
  }
  function moveTip(e) {
    var tip = $('#tip'), x = e.clientX + 16, y = e.clientY + 16;
    if (x + 280 > window.innerWidth) x = e.clientX - 286;
    if (y + 110 > window.innerHeight) y = e.clientY - 100;
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function toggleFocus(id) {
    S.focus = S.focus === id ? null : id;
    applyLinks();
    renderTimeline();
    if (S.focus != null) { var card = $('.lead-card[data-id="' + S.focus + '"]'); if (card) card.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' }); }
  }
  function applyLinks() {
    $$('.sq, .lead-card').forEach(function (el) {
      var id = +el.getAttribute('data-id');
      el.classList.toggle('linked', S.hover === id);
      el.classList.toggle('focus', S.focus === id);
    });
  }

  // ---------------------------------------------------------------- trend
  function renderTrend() {
    var L = leadsF(['day']), end = new Date(); end.setHours(0, 0, 0, 0);
    var span = 14, all = S.d.leads;
    if (S.f.range === '7d') span = 7; else if (S.f.range === '24h') span = 2; else if (S.f.range === '30d') span = 30;
    else if (all.length) { var first = Math.min.apply(null, all.map(function (l) { return l.t; })); span = Math.min(60, Math.max(14, Math.round((end.getTime() - first) / DAY) + 2)); }
    var keys = [];
    for (var i = span - 1; i >= 0; i--) keys.push(dayKey(new Date(end.getFullYear(), end.getMonth(), end.getDate() - i).getTime()));
    if (!L.length && !S.d.leads.length) return emptyPlot('pTrend', 'No leads yet', 'The trend appears as soon as the first lead arrives.');
    var per = {}; L.forEach(function (l) { per[l.day] = (per[l.day] || 0) + 1; });
    var ys = keys.map(function (k) { return per[k] || 0; }), cum = [], run = 0;
    ys.forEach(function (v) { run += v; cum.push(run); });
    var sel = S.f.day;
    draw('pTrend', [{
      type: 'bar', x: keys, y: ys, name: 'New leads', marker: { color: keys.map(function (k) { return sel ? (k === sel ? C.coral : rgba(C.blue, 0.25)) : rgba(C.blue, 0.85); }), cornerradius: 8 },
      hovertemplate: '%{x|%a %d %b}<br>%{y} new lead(s)<extra></extra>'
    }, {
      type: 'scatter', mode: 'lines+markers', x: keys, y: cum, yaxis: 'y2', name: 'Cumulative', line: { color: C.violet, width: 2.6, shape: 'spline' }, marker: { size: 5, color: C.violet },
      hovertemplate: '%{y} in total<extra></extra>'
    }], baseLayout(300, {
      xaxis: { type: 'date', tickformat: '%d %b', tickangle: 0, dtick: span <= 8 ? DAY : (span <= 16 ? 2 * DAY : 7 * DAY) },
      yaxis: { tickformat: 'd', dtick: Math.max.apply(null, ys) > 6 ? undefined : 1 },
      yaxis2: { overlaying: 'y', side: 'right', showgrid: false, zeroline: false, rangemode: 'tozero', fixedrange: true, tickformat: 'd', nticks: 5, tickfont: { color: C.violet, size: 10.5 } },
      margin: { l: 36, r: 40, t: 8, b: 34 }, bargap: 0.28
    }), function (ev) { var p = ev.points && ev.points[0]; if (p && p.data.type === 'bar') setFilter('day', String(p.x).slice(0, 10)); });
  }

  // ---------------------------------------------------------------- score bands
  function renderScore() {
    var L = leadsF(['band']);
    if (!S.d.leads.length) return emptyPlot('pScore', 'No scored leads yet', 'Scores appear once the AI has analyzed a lead.');
    var counts = [0, 0, 0, 0, 0]; L.forEach(function (l) { if (l.band >= 0) counts[l.band]++; });
    var sel = S.f.band;
    draw('pScore', [{
      type: 'bar', x: BANDS.map(function (b) { return b.label; }), y: counts, text: counts.map(String), textposition: 'outside', cliponaxis: false, textfont: { family: "'IBM Plex Mono', monospace", size: 11 },
      marker: { color: BANDS.map(function (b, i) { return sel !== null && sel !== i ? rgba(b.color, 0.25) : b.color; }), cornerradius: 8 },
      hovertemplate: 'score %{x}<br>%{y} lead(s)<extra></extra>'
    }], baseLayout(260, { margin: { l: 30, r: 10, t: 22, b: 34 }, bargap: 0.25, yaxis: { dtick: Math.max.apply(null, counts) > 6 ? undefined : 1 } }),
    function (ev) { var p = ev.points && ev.points[0]; if (p) setFilter('band', p.pointNumber); });
  }

  // ---------------------------------------------------------------- SLA per rep
  function renderSla() {
    var L = leadsF(['rep', 'sla']);
    if (!S.d.leads.length) return emptyPlot('pSla', 'No leads yet', 'SLA status per rep shows up here.');
    var reps = S.d.reps.filter(function (r) { return S.d.leads.some(function (l) { return l.rep === r; }); });
    var ok = reps.map(function (r) { return L.filter(function (l) { return l.rep === r && !l.breached; }).length; });
    var bad = reps.map(function (r) { return L.filter(function (l) { return l.rep === r && l.breached; }).length; });
    var selRep = S.f.rep, selSla = S.f.sla;
    function col(base, rep, kind) {
      var dim = (selRep && rep !== selRep) || (selSla !== 'all' && ((selSla === 'breached') !== (kind === 'bad')));
      return dim ? rgba(base, 0.25) : base;
    }
    draw('pSla', [
      { type: 'bar', orientation: 'h', y: reps, x: ok, name: 'On time', marker: { color: reps.map(function (r) { return col(C.green, r, 'ok'); }), cornerradius: 6 }, hovertemplate: '%{y}<br>%{x} on time<extra></extra>' },
      { type: 'bar', orientation: 'h', y: reps, x: bad, name: 'Overdue', marker: { color: reps.map(function (r) { return col(C.red, r, 'bad'); }), cornerradius: 6 }, hovertemplate: '%{y}<br>%{x} overdue<extra></extra>' }
    ], baseLayout(Math.max(220, 90 + reps.length * 50), {
      barmode: 'stack', bargap: 0.4, margin: { l: 8, r: 10, t: 8, b: 34 },
      yaxis: { automargin: true, autorange: 'reversed', showgrid: false, rangemode: 'normal' },
      xaxis: { showgrid: true, gridcolor: C.grid, dtick: Math.max.apply(null, ok.map(function (v, i) { return v + bad[i]; }).concat([1])) > 6 ? undefined : 1 }
    }), function (ev) {
      var p = ev.points && ev.points[0]; if (!p) return;
      var kind = p.curveNumber === 0 ? 'ok' : 'breached';
      if (S.f.rep === p.y && S.f.sla === kind) { S.f.rep = null; S.f.sla = 'all'; } else { S.f.rep = p.y; S.f.sla = kind; }
      renderAll(true);
    });
  }

  // ---------------------------------------------------------------- tickets
  function renderTickets() {
    var box = $('#pTickets'), all = S.d.tickets, T = ticketsF();
    var plot = $('#pTicketsPlot', box);
    function toEmpty(html) {
      if (plot && window.Plotly && plot.data) { try { Plotly.purge(plot); } catch (e) { /* ignore */ } }
      box.innerHTML = html;
    }
    if (!all.length) return toEmpty('<div class="empty"><div><span class="ok-mark"><svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg></span><b>Support queue is clear</b>No support ticket in the system right now.</div></div>');
    if (!T.length) return toEmpty('<div class="empty"><div><b>No tickets match</b>Nothing in the support queue fits the current filters.</div></div>');
    if (!plot) { box.innerHTML = '<div class="plot" id="pTicketsPlot"></div>'; }
    var counts = {}; T.forEach(function (k) { counts[k.status] = (counts[k.status] || 0) + 1; });
    var statuses = Object.keys(counts), colors = { open: C.blue, in_progress: C.violet, pending: C.amber, resolved: C.green, closed: '#8a8f98' };
    draw('pTicketsPlot', [{
      type: 'bar', x: statuses.map(prettySrc), y: statuses.map(function (s) { return counts[s]; }), text: statuses.map(function (s) { return counts[s]; }), textposition: 'outside', cliponaxis: false,
      marker: { color: statuses.map(function (s) { return colors[s] || C.blue; }), cornerradius: 8 }, hovertemplate: '%{x}<br>%{y} ticket(s)<extra></extra>'
    }], baseLayout(260, { margin: { l: 30, r: 10, t: 22, b: 34 }, bargap: 0.3, yaxis: { dtick: 1 } }));
  }

  // ---------------------------------------------------------------- timeline (gantt)
  function renderTimeline() {
    var T = timelineF();
    if (!S.d.timeline.length) return emptyPlot('pTimeline', 'No active task', 'Nothing is currently assigned to an employee.');
    if (!T.length) return emptyPlot('pTimeline', 'No task matches the filters', 'Loosen a filter to see work in progress.');
    var reps = S.d.reps.filter(function (r) { return T.some(function (x) { return x.rep === r; }); });
    var pos = {}, ticks = [], labels = [], cursor = 0, rows = [];
    reps.forEach(function (rep) {
      var mine = T.filter(function (x) { return x.rep === rep; }).sort(function (a, b) { return a.s - b.s; }), laneEnds = [];
      mine.forEach(function (x) {
        var lane = 0; while (lane < laneEnds.length && laneEnds[lane] > x.s) lane++;
        laneEnds[lane] = x.e; x._y = cursor + lane; rows.push(x);
      });
      ticks.push(cursor + (laneEnds.length - 1) / 2); labels.push(rep); pos[rep] = cursor; cursor += laneEnds.length + 0.8;
    });
    var focusName = null;
    if (S.focus != null) { var fl = S.d.leads.filter(function (l) { return l.id === S.focus; })[0]; if (fl) focusName = fl.name; }
    function trace(breached) {
      var pts = rows.filter(function (x) { return x.breached === breached; }), base = breached ? C.red : C.green;
      return {
        type: 'bar', orientation: 'h', name: breached ? 'SLA breached' : 'On time', base: pts.map(function (x) { return localStr(x.s); }), x: pts.map(function (x) { return x.e - x.s; }),
        y: pts.map(function (x) { return x._y; }), width: 0.78, customdata: pts.map(function (x) { return x.rep; }), hovertext: pts.map(function (x) { return x.name; }), text: '', textposition: 'none',
        marker: { color: pts.map(function (x) { return focusName ? (x.name === focusName ? base : rgba(base, 0.18)) : rgba(base, 0.72); }), line: { color: base, width: 1 }, cornerradius: 6 },
        hovertemplate: '<b>%{hovertext}</b><br>%{customdata}<br>' + (breached ? 'SLA breached' : 'On time') + '<extra></extra>'
      };
    }
    var now = localStr(Date.now()), mn = Math.min.apply(null, rows.map(function (x) { return x.s; })), mx = Math.max(Date.now(), Math.max.apply(null, rows.map(function (x) { return x.e; })));
    var padMs = Math.max((mx - mn) * 0.04, 3600e3);
    draw('pTimeline', [trace(false), trace(true)], baseLayout(Math.min(560, Math.max(240, 90 + cursor * 24)), {
      barmode: 'overlay', showlegend: true, legend: { orientation: 'h', y: 1.12, x: 0, font: { size: 11.5 } }, margin: { l: 8, r: 16, t: 26, b: 40 },
      xaxis: { type: 'date', range: [localStr(mn - padMs), localStr(mx + padMs)], tickformat: '%d %b\n%H:%M', showgrid: true, gridcolor: C.grid },
      yaxis: { tickvals: ticks, ticktext: labels, automargin: true, autorange: 'reversed', showgrid: false, rangemode: 'normal', zeroline: false },
      shapes: [{ type: 'line', xref: 'x', yref: 'paper', x0: now, x1: now, y0: 0, y1: 1, line: { color: C.ink, width: 1.5, dash: 'dot' } }],
      annotations: [{ x: now, y: 1, xref: 'x', yref: 'paper', text: 'now', showarrow: false, yanchor: 'bottom', font: { family: "'IBM Plex Mono', monospace", size: 10.5, color: C.ink } }]
    }), function (ev) { var p = ev.points && ev.points[0]; if (p && p.customdata) setFilter('rep', p.customdata); });
  }

  // ---------------------------------------------------------------- ribbon of lead cards
  function renderRibbon() {
    var box = $('#pRibbon'), L = leadsF().sort(function (a, b) { return (b.t - a.t) || ((b.score || 0) - (a.score || 0)); }).slice(0, 14);
    if (!S.d.leads.length) { box.innerHTML = '<div class="empty"><div><b>No leads yet</b>New leads show up here as cards, newest first.</div></div>'; return; }
    if (!L.length) { box.innerHTML = '<div class="empty"><div><b>No lead matches</b>Try clearing a filter.</div></div>'; return; }
    box.innerHTML = '<div class="ribbon">' + L.map(function (l, i) {
      var sc = l.score == null ? 0 : l.score, colr = BANDS[Math.max(0, l.band)].color, off = (100 - sc).toFixed(1);
      return '<button class="lead-card" data-id="' + l.id + '" style="--i:' + i + '"><div class="lc-top"><div class="score-ring"><svg viewBox="0 0 36 36"><circle class="bg" cx="18" cy="18" r="15.9155" fill="none"/>' +
        '<circle class="fg" cx="18" cy="18" r="15.9155" fill="none" stroke="' + colr + '" pathLength="100" style="--off:' + off + '"/></svg><b>' + (l.score == null ? '–' : l.score) + '</b></div>' +
        '<div><div class="lc-name">' + esc(l.name) + '</div><div class="lc-co">' + esc(l.company || 'no company') + '</div></div></div>' +
        '<div class="lc-tags"><span class="lc-tag" style="--c:' + srcColor(l.src) + '"><i></i>' + esc(prettySrc(l.src)) + '</span><span class="lc-tag" style="--c:' + repColor(l.rep) + '"><i></i>' + esc(l.rep) + '</span>' +
        '<span class="lc-tag ' + (l.breached ? 'breach' : 'ok') + '">' + esc(slaText(l)) + '</span></div></button>';
    }).join('') + '</div>';
    applyLinks();
  }
  (function bindRibbon() {
    var box = $('#pRibbon');
    box.addEventListener('mouseover', function (e) { var c = e.target.closest('.lead-card'); if (c) { S.hover = +c.getAttribute('data-id'); applyLinks(); } });
    box.addEventListener('mouseout', function (e) { if (e.target.closest('.lead-card')) { S.hover = null; applyLinks(); } });
    box.addEventListener('click', function (e) { var c = e.target.closest('.lead-card'); if (c) toggleFocus(+c.getAttribute('data-id')); });
  })();

  // ---------------------------------------------------------------- render all
  function renderAll(fromFilter) {
    if (!S.d) return;
    var L = leadsF();
    if (fromFilter) $$('.panel').forEach(function (p) { p.classList.add('updating'); setTimeout(function () { p.classList.remove('updating'); }, 450); });
    $('#rptSub').textContent = S.d.leads.length + ' leads · ' + S.d.tickets.length + ' ticket(s) · ' + S.d.timeline.length + ' active task(s) — straight from the erp_support database, refreshed automatically.';
    renderBanner(L);
    renderFilterbar();
    renderKpis(L);
    renderFunnel();
    renderSource();
    renderDots();
    renderTrend();
    renderScore();
    renderSla();
    renderTickets();
    renderTimeline();
    renderRibbon();
    S.rendered = true;
  }

  // ---------------------------------------------------------------- live loop
  function announce(prev, next) {
    var pids = {}; prev.leads.forEach(function (l) { pids[l.id] = 1; });
    var fresh = next.leads.filter(function (l) { return !pids[l.id]; });
    var parts = [];
    if (fresh.length) parts.push('+' + fresh.length + ' new lead' + (fresh.length > 1 ? 's' : '') + (fresh.length === 1 ? ' (' + fresh[0].name + ')' : ''));
    var db = next.leads.filter(function (l) { return l.breached; }).length - prev.leads.filter(function (l) { return l.breached; }).length;
    if (db) parts.push((db > 0 ? '+' : '') + db + ' overdue');
    var dt = next.tickets.length - prev.tickets.length;
    if (dt) parts.push((dt > 0 ? '+' : '') + dt + ' ticket' + (Math.abs(dt) > 1 ? 's' : ''));
    D.toast('Live update' + (parts.length ? ' · ' + parts.join(' · ') : ' · data changed'), 'ok');
  }
  function handle(res, forced) {
    S.refreshedAt = res.refreshed_at ? Date.parse(res.refreshed_at) : Date.now();
    S.serverError = res.error || null;
    var changed = false;
    if (res.data && res.version !== S.version) {
      var prev = S.d;
      S.version = res.version; S.generatedAt = res.generated_at; S.lastChangeAt = Date.now();
      S.d = prep(res.data); changed = true;
      renderAll(false);
      if (prev) announce(prev, S.d); else { D.emit('report-loaded'); }
    }
    S.fails = 0;
    var loaded = !!S.d;
    if (!loaded) { $('#rptSub').textContent = res.error ? 'The database is not reachable yet: ' + res.error : 'Waiting for the first data…'; }
    setLive(S.serverError ? 'stale' : (S.pollSec === 0 && !forced ? 'paused' : 'live'));
    if (!loaded && res.error) D.emit('report-loaded');   // do not hold the splash hostage to a database problem
    tickAgo();
    return changed;
  }
  function poll(force) {
    clearTimeout(S.pollTimer);
    setLive('syncing');
    var req = force ? D.api.post('/api/report/refresh') : D.api.get('/api/report/data?since=' + encodeURIComponent(S.version || ''));
    return req.then(function (res) {
      var changed = handle(res, !!force);
      if (force) D.toast(changed ? 'Report refreshed · new data' : 'Report refreshed · no changes', changed ? 'ok' : '');
    }).catch(function () {
      S.fails++;
      setLive(S.d && S.fails < 3 ? 'stale' : 'offline');
      if (S.fails === 1 && force) D.toast('Could not reach the report service', 'bad');
    }).then(function () { schedule(); });
  }
  function schedule() {
    clearTimeout(S.pollTimer);
    if (S.pollSec > 0) S.pollTimer = setTimeout(function () { poll(false); }, S.pollSec * 1000);
  }
  function refreshNow() {
    var btn = $('#refreshBtn'), rail = $('#btnRefresh');
    btn.classList.add('spinning'); rail.classList.add('spinning'); btn.disabled = true;
    Promise.all([poll(true), loadDigest()]).then(function () {
      setTimeout(function () { btn.classList.remove('spinning'); rail.classList.remove('spinning'); btn.disabled = false; }, 500);
    });
  }
  $('#refreshBtn').addEventListener('click', refreshNow);
  D.on('refresh-request', function () { if (D.view() !== 'dataflow') refreshNow(); });   // the Data Flow page has its own refresh
  var sel = $('#pollSel');
  sel.value = String(S.pollSec);
  sel.addEventListener('change', function () {
    S.pollSec = parseInt(sel.value, 10) || 0; D.store('erpdesk.poll', String(S.pollSec));
    if (S.pollSec === 0) { clearTimeout(S.pollTimer); setLive('paused'); } else { setLive('live'); poll(false); }
    tickAgo();
  });
  setInterval(tickAgo, 1000);

  // ---------------------------------------------------------------- today's briefing
  var GLYPH = { opportunities: '↗', risks: '!', actions: '✓' };
  function digestSeries(key, dg) {
    var pts = (dg.history || []).map(function (h) { return h.metrics && h.metrics[key]; }).filter(function (v) { return v != null; });
    if (dg.metrics && dg.metrics[key] != null && (!dg.history.length || dg.history[dg.history.length - 1].date !== dg.date)) pts.push(dg.metrics[key]);
    return pts;
  }
  function miniSpark(vals) {
    if (vals.length < 2) return '<svg class="bm-spark" viewBox="0 0 56 26"><circle cx="50" cy="13" r="3"/></svg>';
    var min = Math.min.apply(null, vals), max = Math.max.apply(null, vals), rng = max - min || 1;
    var pts = vals.map(function (v, i) { return (i * 56 / (vals.length - 1)).toFixed(1) + ',' + (22 - (v - min) / rng * 18).toFixed(1); });
    var last = pts[pts.length - 1].split(',');
    return '<svg class="bm-spark" viewBox="0 0 56 26"><polyline points="' + pts.join(' ') + '"/><circle cx="' + last[0] + '" cy="' + last[1] + '" r="3"/></svg>';
  }
  function signalOf(m) {
    var total = m.total_leads || 0, breachL = m.sla_breached_leads || 0, breachT = m.sla_breached_tickets || 0;
    var other = (m.clickup_unsynced_leads || 0) + (m.leads_not_analyzed || 0);
    if (breachT > 0 || (total && breachL / total >= 0.5)) return { color: C.red, bg: 'rgba(224,57,62,.07)', title: 'Needs attention' };
    if (breachL > 0 || other > 0) return { color: C.amber, bg: 'rgba(242,183,5,.10)', title: 'Watch closely' };
    return { color: C.green, bg: 'rgba(18,184,134,.08)', title: 'Healthy' };
  }
  function loadDigest() {
    return D.api.get('/api/digest').then(function (dg) { S.digest = dg; renderBrief(); scheduleDigestPoll(); }).catch(function () { /* keep the last briefing */ });
  }
  function scheduleDigestPoll() {
    clearTimeout(S.digestTimer);
    var running = S.digest && S.digest.job && S.digest.job.state === 'running';
    S.digestTimer = setTimeout(loadDigest, running ? 2000 : 60000);
  }
  var lastJobState = null;
  function renderBrief() {
    var dg = S.digest, box = $('#brief');
    if (!dg) { box.innerHTML = ''; return; }
    var job = dg.job || {}, running = job.state === 'running';
    if (lastJobState === 'running' && job.state === 'done') D.toast('Today’s briefing is ready', 'ok');
    if (lastJobState === 'running' && job.state === 'error') D.toast('Digest failed: ' + (job.message || 'see the log'), 'bad', 7000);
    lastJobState = job.state;
    var today = dg.today, head, body, tags = '';
    var btn = '<button class="btn primary" id="genBtn"' + (running ? ' disabled' : '') + '>' + (running ? '<span class="spinner" style="width:15px;height:15px;border-width:2px"></span>Generating…' :
      '<svg viewBox="0 0 24 24"><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9zM19 16l.8 2.2L22 19l-2.2.8L19 22l-.8-2.2L16 19l2.2-.8z"/></svg>' + (dg.exists && dg.is_today ? 'Regenerate' : 'Generate today’s digest')) + '</button>';
    var hint = running ? 'Reading live metrics and asking the AI — usually 10–30 s.' : 'Live metrics vs. yesterday + one AI summary call (counts only, no customer data). Nothing is emailed from the app.' + (dg.auto_generate ? ' Also runs automatically once a day.' : '');
    var actions = '<div class="brief-actions">' + btn + '<span class="hint">' + hint + '</span>' + (running ? '<div class="gen-progress"><span></span></div>' : '') + '</div>';

    if (!dg.exists) {
      box.innerHTML = '<div class="brief-empty"><svg viewBox="0 0 180 120"><rect class="b1" x="20" y="60" width="30" height="50" rx="8" fill="' + C.coral + '" opacity=".85"/><rect class="b2" x="65" y="35" width="30" height="75" rx="8" fill="' + C.violet + '" opacity=".85"/>' +
        '<rect class="b3" x="110" y="15" width="30" height="95" rx="8" fill="' + C.blue + '" opacity=".85"/><circle cx="155" cy="18" r="8" fill="' + C.amber + '"/></svg>' +
        '<div><div class="eyebrow">TODAY’S BRIEFING</div><h3>No briefing yet</h3><p>The daily digest turns the numbers above into a short read: overall status, opportunities, risks and suggested actions — compared with yesterday. Generate the first one now' +
        (dg.auto_generate ? ', or leave the app open and it will do it by itself.' : '.') + '</p>' + (job.state === 'error' ? '<div class="brief-banner err">Last attempt failed: ' + esc(job.message) + '</div>' : '') + actions + '</div></div>';
      bindGen(); return;
    }
    var m = dg.metrics || {}, sig = signalOf(m), s = dg.sections || {};
    tags += dg.is_today ? '<span class="tag fresh">FRESH · TODAY</span>' : '<span class="tag stale">FROM ' + esc(fmtLongDay(dg.date).toUpperCase()) + '</span>';
    tags += '<span class="tag ai">' + (dg.ai_note ? 'AI UNAVAILABLE' : (dg.ai_configured ? 'AI SUMMARY' : 'NO AI KEY')) + '</span>';
    if (dg.generated_at) tags += '<span class="tag">GENERATED ' + esc(fmtTime(Date.parse(dg.generated_at)).slice(0, 5)) + '</span>';
    tags += dg.email_sent ? '<span class="tag">EMAILED</span>' : '<span class="tag">NOT EMAILED</span>';
    var stale = !dg.is_today ? '<div class="brief-banner">This briefing is from ' + esc(fmtLongDay(dg.date)) + ' — today’s hasn’t been generated yet. Use “Generate today’s digest” to refresh it.</div>' : '';
    var errBanner = job.state === 'error' ? '<div class="brief-banner err">The last generation attempt failed: ' + esc(job.message) + '</div>' : '';
    var status = s.status || (s.recognised ? '' : '');
    var statusHtml = dg.ai_note ? esc(dg.narrative) : (status ? md(status) : 'No status sentence in this digest.');
    var metrics = Object.keys(dg.labels).filter(function (k) { return m[k] != null; }).map(function (k) {
      var dl = dg.deltas ? dg.deltas[k] : null, pol = dg.polarity[k] || 1, cls = 'flat', txt = 'no baseline';
      if (dl != null) { if (dl === 0) txt = '▬ no change'; else { txt = (dl > 0 ? '▲ +' : '▼ ') + dl; cls = (dl * pol > 0) ? 'good' : 'bad'; } }
      return '<div class="bm-tile"><div class="bm-label">' + esc(dg.labels[k]) + '</div><div class="bm-row"><div><div class="bm-value">' + esc(m[k]) + '</div><div class="bm-delta ' + cls + '">' + txt + '</div></div>' + miniSpark(digestSeries(k, dg)) + '</div></div>';
    }).join('');
    function col(key, title, cls) {
      var items = (s[key] || []);
      if (!items.length) return '';
      var doneKey = 'erpdesk.done.' + dg.date;
      var done = {}; try { done = JSON.parse(D.store(doneKey) || '{}'); } catch (e) { done = {}; }
      return '<div class="bcol ' + (key === 'actions' ? 'actions' : '') + '" style="--bc:' + cls + '"><h4><i>' + GLYPH[key] + '</i>' + title + '</h4><ul>' + items.map(function (t, i) {
        return '<li style="--i:' + i + '" data-act="' + i + '" class="' + (key === 'actions' && done[i] ? 'done' : '') + '">' + md(t) + '</li>'; }).join('') + '</ul></div>';
    }
    var cols = col('opportunities', 'Opportunities', C.green) + col('risks', 'Risks', C.red) + col('actions', 'Suggested actions', C.blue);
    if (!cols && !dg.ai_note) cols = '<div class="brief-note">' + esc(dg.narrative) + '</div>';
    if (dg.ai_note) cols = '<div class="brief-note warn">The AI narrative is not available for this digest, so only the numbers are shown. Set DEEPSEEK_API_KEY in .env and generate again for the written analysis.</div>';
    box.innerHTML = '<div class="brief-head"><div><div class="eyebrow">TODAY’S BRIEFING</div><div class="brief-date">' + esc(fmtLongDay(today)) + '</div><div class="brief-tags">' + tags + '</div></div>' + actions + '</div>' +
      stale + errBanner + '<div class="brief-main"><div class="signal" style="--sig:' + sig.color + ';--sig-bg:' + sig.bg + '"><span class="orb"></span><div><h4 class="signal-title">' + sig.title + '</h4><p class="signal-text">' + statusHtml + '</p></div></div>' +
      '<div class="bm">' + metrics + '</div></div><div class="brief-cols">' + cols + '</div>';
    bindGen();
    $$('.bcol.actions li', box).forEach(function (li) {
      li.addEventListener('click', function () {
        var key = 'erpdesk.done.' + dg.date, st = {}; try { st = JSON.parse(D.store(key) || '{}'); } catch (e) { st = {}; }
        var i = li.getAttribute('data-act'); st[i] = !st[i]; li.classList.toggle('done', !!st[i]); D.store(key, JSON.stringify(st));
      });
    });
  }
  function bindGen() {
    var b = $('#genBtn'); if (!b) return;
    b.addEventListener('click', function () {
      b.disabled = true;
      D.api.post('/api/digest/generate').then(function (r) {
        if (!r.started) D.toast('A digest is already being generated', 'warn');
        else D.toast('Generating today’s digest…', '');
        return loadDigest();
      }).catch(function () { D.toast('Could not start the digest', 'bad'); b.disabled = false; });
    });
  }

  // ---------------------------------------------------------------- reveal on scroll
  var io = window.IntersectionObserver ? new IntersectionObserver(function (entries) {
    entries.forEach(function (en) {
      if (en.isIntersecting) { en.target.classList.add('in'); io.unobserve(en.target); if (window.Plotly) window.dispatchEvent(new Event('resize')); }
    });
  }, { root: $('#reportScroll'), threshold: 0.06 }) : null;
  $$('.reveal').forEach(function (el) { if (io) io.observe(el); else el.classList.add('in'); });
  setTimeout(function () { $$('.reveal:not(.in)').forEach(function (el) { el.classList.add('in'); }); }, 3500);   // safety net

  D.on('view', function (name) {
    if (name !== 'reporting') return;
    requestAnimationFrame(function () { $$('.plot').forEach(function (el) { if (window.Plotly && el.data && el.offsetWidth) { try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ } } }); });
  });

  // ---------------------------------------------------------------- boot
  buildKpis();
  setLive(S.pollSec === 0 ? 'paused' : 'connecting');
  if (window.__plotlyMissing) D.toast('plotly.js is missing - charts are disabled', 'warn', 8000);
  poll(false);
  loadDigest();
})();
