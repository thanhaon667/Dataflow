/* ERP Desk - Leads page, "Sources & cohorts" tab. Draws the JSON of GET /api/leads/sources (desktop/sources_data.py):
   one row per lead source, and one curve per arrival cohort followed day by day since arrival.

   It owns no state and no poller. leads.js keeps the filters, does the fetching (one tab polls at a time, so the page
   still has exactly one poller) and calls render(payload, ctx); every change of bucket / metric / cohort / sort goes
   back through ctx so the filter bar, the table, the chart and both CSV links can never describe different leads.
   Nothing is computed here except layout: the server already decided every number, every percentage and every
   sentence, including whether a percentage is allowed at all. All server text goes through esc() / textContent. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = D.esc;
  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  var kit = D.chartKit || null;
  var C = (kit && kit.colors) || { ink: '#1c1d22', dim: '#6b6f76', blue: '#3452eb', coral: '#ff5a36', green: '#12b886', violet: '#7c5cff', amber: '#f2b705', red: '#e0393e', line: '#e7e2d8', grid: '#f2efe8' };
  var STATE_COLOR = { on_time: C.green, late: C.amber, past_sla: C.red, pending: C.blue, no_deadline: '#b9b5aa' };
  // Same five words as static/leads.js's STATE_SHORT, both sourced from desktop/sla_words.py SHORT via
  // `options.statuses[i].short` (syncStateShort()) so the two scripts can never drift apart; these are only the
  // pre-first-payload fallback.
  var STATE_SHORT = { on_time: 'On time', late: 'Answered late', past_sla: 'Past SLA', pending: 'Waiting', no_deadline: 'No deadline' };
  function syncStateShort(o) {
    (o && o.statuses || []).forEach(function (s) { if (s.short) STATE_SHORT[s.value] = s.short; });
  }
  var COLS = [                              // label, sort key (null = not sortable), title attribute
    ['Source', 'name', 'leads.source exactly as the form sent it'],
    ['Leads', 'leads', 'Leads of this source in view, and its share of the view'],
    ['Assigned', 'assigned', 'Reached a sales rep (a current lead_assignments row)'],
    ['AI analysed', 'analyzed', 'Reached the AI analysis - and has a rep, because the stages are strict'],
    ['ClickUp', 'task', 'Reached a ClickUp task - and every earlier stage'],
    ['Replied', 'replied', 'Reached a reply on record - and every earlier stage'],
    ['SLA outcome', null, 'How this source’s leads ended up, in the five shared outcomes'],
    ['On time', 'on_time', 'Answered at or before the deadline'],
    ['Past SLA', 'past_sla', 'No reply on record and the deadline has passed - the same rule as the Today page'],
    ['Median reply', 'median', 'Median clock time from arrival to the first reply on record, with its n'],
    ['Sales reps', null, 'Which reps this source’s leads are currently assigned to']
  ];
  var ORDER = ['on_time', 'late', 'past_sla', 'pending', 'no_deadline'];

  var last = { sig: {}, ctx: null, data: null };

  // ------------------------------------------------------------------ helpers
  function num(v) { return Number(v).toLocaleString('en-US'); }
  function pretty(s) { return String(s == null ? '' : s).replace(/_/g, ' '); }
  function plural(n, one, many) { return n + ' ' + (n === 1 ? one : (many || one + 's')); }
  function rateText(r) {                    // the server decides whether a percentage is allowed (denominator >= LOW_N)
    if (!r) return '-';
    return r.pct == null ? r.n + ' of ' + r.of : Math.round(r.pct) + '%';
  }
  function hex(v) { var n = Math.round(Math.max(0, Math.min(255, v))).toString(16); return n.length < 2 ? '0' + n : n; }
  function parseHex(h) { var s = String(h).replace('#', ''); return [parseInt(s.slice(0, 2), 16), parseInt(s.slice(2, 4), 16), parseInt(s.slice(4, 6), 16)]; }
  function mix(a, b, t) {
    var x = parseHex(a), y = parseHex(b);
    return '#' + hex(x[0] + (y[0] - x[0]) * t) + hex(x[1] + (y[1] - x[1]) * t) + hex(x[2] + (y[2] - x[2]) * t);
  }
  // Oldest cohort -> newest runs cool to warm, so the reading order of the legend is also the order of time. These are
  // series colours (identity), not status colours: the status palette stays reserved for the SLA outcomes.
  var RAMP = [C.blue, C.violet, C.coral, C.amber];
  function rampColor(i, total) {
    if (total <= 1) return C.blue;
    var t = i / (total - 1) * (RAMP.length - 1);
    var k = Math.min(RAMP.length - 2, Math.floor(t));
    return mix(RAMP[k], RAMP[k + 1], t - k);
  }
  function chartsOk() { return !!(window.Plotly && kit); }
  function na(id, p) {
    $(id).innerHTML = '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>This panel could not be read (' + esc((p && p.error) || 'unknown error') + '). The other panels keep working.</span></div>';
  }
  function empty(id, title, sub) { $(id).innerHTML = '<div class="ld-empty"><div><b>' + esc(title) + '</b><span>' + esc(sub) + '</span></div></div>'; }
  function purge(id) { var el = $(id + ' .ld-plot'); if (el && window.Plotly && el.data) { try { Plotly.purge(el); } catch (e) { /* ignore */ } } }
  function plot(id, traces, layout, onClick) {
    var host = $(id);
    if (!chartsOk()) { host.innerHTML = '<div class="ld-empty"><div><b>Charts unavailable</b><span>plotly.js could not be loaded.</span></div></div>'; return; }
    var el = host.querySelector('.ld-plot');
    if (!el) { host.insertAdjacentHTML('afterbegin', '<div class="ld-plot"></div>'); el = host.querySelector('.ld-plot'); }
    if (reduceMotion) layout.transition = { duration: 0 };
    if (!D.fontsDone()) { D.fontsReady(function () { plot(id, traces, layout, onClick); }); return; }  // Plotly measures text once: wait for the font
    Plotly.react(el, traces, layout, { displayModeBar: false, responsive: true });
    if (onClick && !el._bound) { el._bound = true; el.on('plotly_click', onClick); }
    if (ro && !el._ro) { el._ro = true; ro.observe(el); }
  }
  var ro = window.ResizeObserver ? new ResizeObserver(function (entries) {
    entries.forEach(function (en) {
      var el = en.target; clearTimeout(el._rz);
      el._rz = setTimeout(function () { if (window.Plotly && el.data && el.offsetWidth) { try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ } } }, 120);
    });
  }) : null;

  // ------------------------------------------------------------------ banner
  var BANNER_ICON = {
    single: '<path d="M12 8v5.5M12 16.6v.2"/>',
    low_n: '<path d="M12 8v5.5M12 16.6v.2"/>',
    thin: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5M12 7.6v.2"/>',
    empty: '<circle cx="11" cy="11" r="6.5"/><path d="M15.8 15.8 20 20"/>'
  };
  function renderBanner(b) {
    var el = $('#sxBanner');
    if (!b) { el.hidden = true; el.innerHTML = ''; return; }
    el.hidden = false;
    el.setAttribute('data-level', b.level || 'thin');
    el.innerHTML = '<span class="sx-b-ico" aria-hidden="true"><svg viewBox="0 0 24 24">' + (BANNER_ICON[b.level] || BANNER_ICON.thin) + '</svg></span>' +
      '<h4>' + esc(b.title) + '</h4><p class="sx-b-text">' + esc(b.text) + '</p>' +
      ((b.needs && b.needs.length) ? '<ul>' + b.needs.map(function (x) { return '<li>' + esc(x) + '</li>'; }).join('') + '</ul>' : '');
  }

  // ------------------------------------------------------------------ chart controls (bucket / metric)
  function renderControls(d, ctx) {
    var o = d.options, v = d.view;
    var sig = JSON.stringify([o.buckets, o.metrics, v.bucket, v.metric, v.bucket_auto]);
    if (last.sig.controls === sig) return;
    last.sig.controls = sig;
    var html = '<span class="lbl">Metric</span><div class="seg" data-seg="metric">' +
      o.metrics.map(function (m) {
        return '<button type="button" data-v="' + esc(m.value) + '" title="' + esc(m.label + ': ' + m.meaning) + '" class="' + (v.metric === m.value ? 'on' : '') + '">' + esc(m.label) + '</button>';
      }).join('') + '</div>' +
      '<span class="lbl">Group by</span><div class="seg" data-seg="bucket">' +
      '<button type="button" data-v="auto" title="Let the page pick the finest bucket the data span can carry" class="' + (v.bucket_auto ? 'on' : '') + '">Auto</button>' +
      o.buckets.map(function (b) {
        return '<button type="button" data-v="' + esc(b.value) + '" title="' + esc(b.meaning + ' - ' + plural(b.cohorts, 'cohort') + ' in the database') + '" class="' + (!v.bucket_auto && v.bucket === b.value ? 'on' : '') + '">' + esc(b.label) + '</button>';
      }).join('') + '</div>';
    $('#sxControls').innerHTML = html;
  }
  $('#sxControls').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b || !last.ctx) return;
    var seg = b.closest('[data-seg]');
    if (!seg) return;
    if (seg.getAttribute('data-seg') === 'metric') last.ctx.setMetric(b.getAttribute('data-v'));
    else last.ctx.setBucket(b.getAttribute('data-v'));
  });

  // ------------------------------------------------------------------ the cohort curves
  function renderCohorts(p, d, ctx) {
    var host = $('#sxCohort');
    if (p.state === 'unavailable') { purge('#sxCohort'); return na('#sxCohort', p); }
    if (p.state === 'too_big') { purge('#sxCohort'); return empty('#sxCohort', 'Too many leads for this chart', p.error || ''); }
    if (p.state === 'empty') { purge('#sxCohort'); return empty('#sxCohort', 'No lead matches these filters', 'A cohort curve appears as soon as a lead does. Loosen a filter, or press Reset all above.'); }
    if (!chartsOk()) return empty('#sxCohort', 'Charts unavailable', 'plotly.js could not be loaded.');
    var cohorts = p.cohorts, pct = p.y_mode === 'pct', single = cohorts.length === 1;
    var sel = d.view.cohort;
    var traces = cohorts.map(function (c, i) {
      var color = rampColor(i, cohorts.length);
      var dim = sel && c.key !== sel;
      var xs = c.points.map(function (x) { return x.age; });
      var ys = c.points.map(function (x) { return pct ? (x.rate.pct || 0) : x.cum; });
      var hov = c.points.map(function (x) {
        return 'day ' + x.age + ' after arrival<br>' + x.cum + ' of ' + c.n + ' ' + (p.metric_label || '').toLowerCase() +
          (x.rate.pct == null ? ' (too few leads for a percentage)' : ' · ' + Math.round(x.rate.pct * 10) / 10 + '%');
      });
      var mode = c.single_point ? 'markers' : (single ? 'lines+markers' : 'lines+markers');
      var labels = null;
      if (single && c.points.length <= 12) { mode += '+text'; labels = c.points.map(function (x) { return pct ? (Math.round(x.rate.pct * 10) / 10 + '%') : String(x.cum); }); }
      return {
        type: 'scatter', mode: mode, x: xs, y: ys, name: c.label + '  (n=' + c.n + ')',
        text: labels || undefined, textposition: 'top center', textfont: { family: D.font, size: 10.5, color: C.dim },
        cliponaxis: false,
        line: { color: color, width: single ? 1.6 : 2.4, shape: 'hv', dash: single ? 'dot' : 'solid' },
        marker: { color: color, size: single ? 9 : 7, line: { color: '#fff', width: 1.5 } },
        opacity: dim ? 0.28 : 1,
        customdata: hov,
        hovertemplate: '<b>%{fullData.name}</b><br>%{customdata}<extra></extra>'
      };
    });
    var maxAge = p.max_age || 0;
    var lay = kit.baseLayout(single ? 300 : 330, {
      margin: { l: 46, r: 14, t: single ? 14 : 44, b: 46 },
      showlegend: !single,
      legend: { orientation: 'h', y: 1.16, x: 0, font: { family: D.font, size: 11 }, bgcolor: 'rgba(0,0,0,0)' },
      hovermode: 'closest',
      xaxis: { dtick: maxAge <= 14 ? 1 : undefined, tickformat: 'd', range: [-0.4, Math.max(1, maxAge) + 0.4],
               title: { text: 'days since the lead arrived', font: { family: D.font, size: 11, color: C.dim }, standoff: 10 } },
      yaxis: pct ? { range: [0, 104], ticksuffix: '%', dtick: 25 }
                 : { tickformat: 'd', dtick: p.leads > 8 ? undefined : 1, rangemode: 'tozero',
                     title: { text: 'leads (cumulative)', font: { family: D.font, size: 11, color: C.dim }, standoff: 8 } }
    });
    plot('#sxCohort', traces, lay, function (ev) {
      var pt = ev.points && ev.points[0];
      if (!pt || !last.ctx) return;
      var c = cohorts[pt.curveNumber];
      if (c) last.ctx.setCohort(c.key === d.view.cohort ? null : c.key);
    });
    $$('#sxCohort .sx-single, #sxCohort .sx-selnote, #sxCohort .ld-note').forEach(function (el) { el.remove(); });
    if (single) {
      var c0 = cohorts[0];
      host.insertAdjacentHTML('beforeend', '<p class="sx-single"><span>' +
        (c0.single_point
          ? 'This cohort is <b>' + plural(c0.n, 'lead') + '</b> old enough for day 0 only, so there is one point and no line at all.'
          : 'One cohort, <b>' + plural(c0.n, 'lead') + '</b>: the dots are the measured figures at each day since arrival and the dotted line only joins them &mdash; there is no second cohort to compare it with, so nothing here is a trend.') +
        ' ' + esc((d.banner && d.banner.level === 'single') ? 'See the note at the top for what this view needs.' : '') + '</span></p>');
    }
    if (!pct) host.insertAdjacentHTML('beforeend', '<p class="ld-note">Plain counts, not percentages: ' +
      (single ? 'a cohort' : 'at least one cohort') + ' here has fewer than <b>' + ((d.config || {}).low_n || 5) +
      '</b> leads, and a percentage of so few leads would move by tens of points for one lead.</p>');
    if (sel) host.insertAdjacentHTML('beforeend', '<p class="sx-selnote">The table below shows <b>' + esc(d.view.cohort_label || sel) +
      '</b> only. The chart deliberately keeps every cohort, so you can still see what you are comparing it against.' +
      '<button type="button" data-clear-cohort>Show all cohorts in the table</button></p>');
  }
  $('#sxCohort').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-clear-cohort]') : null;
    if (b && last.ctx) last.ctx.setCohort(null);
  });

  // ------------------------------------------------------------------ the cohort list
  function renderList(p, d) {
    if (p.state === 'unavailable') return na('#sxList', p);
    if (p.state === 'too_big') return empty('#sxList', 'Too many leads', 'Narrow the date range to bring the cohorts back.');
    if (p.state === 'empty') return empty('#sxList', 'No cohort yet', 'Cohorts appear as soon as a lead matches the filters.');
    var pct = p.y_mode === 'pct', sel = d.view.cohort;
    var maxFinal = Math.max.apply(null, p.cohorts.map(function (c) { return c.final; }).concat([1]));
    var html = '<div class="sx-cohs">' + p.cohorts.map(function (c, i) {
      var w = pct ? (c.final_rate.pct || 0) : (c.final / maxFinal * 100);
      return '<button type="button" class="sx-coh-row' + (sel === c.key ? ' on' : '') + '" data-cohort="' + esc(c.key) + '" style="--c:' + rampColor(i, p.cohorts.length) + '"' +
        ' aria-pressed="' + (sel === c.key) + '" title="' + esc(c.label + ' - ' + plural(c.n, 'lead') + ', followed to day ' + c.observed_age) + '">' +
        '<span class="sw"></span><span class="lbl">' + esc(c.label) + '<em>n = ' + c.n + ' &middot; to day ' + c.observed_age + '</em></span>' +
        '<span class="fig">' + esc(c.final + ' of ' + c.n) + (c.final_rate.pct == null ? '' : '<small>' + Math.round(c.final_rate.pct) + '%</small>') + '</span>' +
        '<span class="bar"><i style="width:' + Math.max(0, Math.min(100, w)) + '%"></i></span></button>';
    }).join('') + '</div>' +
      '<p class="ld-note"><b>' + esc(p.metric_label || '') + '</b> means ' + esc(p.metric_meaning || '') +
      '. Each figure is where its cohort stands at the last day every one of its leads has reached.' +
      (p.more ? ' <b>' + p.more + '</b> older cohort(s) are not shown.' : '') + '</p>';
    $('#sxList').innerHTML = html;
  }
  $('#sxList').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('[data-cohort]') : null;
    if (!b || !last.ctx || !last.data) return;
    var k = b.getAttribute('data-cohort');
    last.ctx.setCohort(last.data.view.cohort === k ? null : k);
  });

  // ------------------------------------------------------------------ the source table
  function cell(label, r, extra) { return '<td data-label="' + esc(label) + '"' + (extra || '') + '>' + r + '</td>'; }
  function stageCell(label, rt) {
    // the sub-line always carries the denominator, and the percentage only when the server allowed one (n >= LOW_N)
    var sub = rt.pct == null ? 'of ' + rt.of : Math.round(rt.pct) + '% of ' + rt.of;
    return '<td data-label="' + esc(label) + '"' + (rt.n ? '' : ' class="dim"') + '><b>' + num(rt.n) + '</b><small>' + esc(sub) + '</small></td>';
  }
  function renderTable(p, d) {
    if (p.state === 'unavailable') return na('#sxTable', p);
    if (p.state === 'empty') return empty('#sxTable', 'No source matches these filters', 'Loosen a filter or press Reset all above.');
    var selSources = (d.filters && d.filters.source) || [];
    var head = COLS.map(function (c) {
      // The 'on_time'/'past_sla' sort keys double as SLA-outcome keys, so their header text comes from the same
      // synced STATE_SHORT as the mini-bar badges on this same row (desktop/sla_words.py SHORT) rather than the
      // column's own literal - a header and a badge in one row must never be able to say something different.
      var label = STATE_SHORT[c[1]] || c[0];
      if (!c[1]) return '<th scope="col" title="' + esc(c[2]) + '">' + esc(label) + '</th>';
      var on = d.view.sort === c[1];
      return '<th scope="col" title="' + esc(c[2]) + '"' + (on ? ' aria-sort="' + (d.view.dir === 'asc' ? 'ascending' : 'descending') + '"' : '') +
        '><button type="button" data-sort="' + c[1] + '">' + esc(label) + '<span class="ar" aria-hidden="true">' + (on ? (d.view.dir === 'asc' ? '▲' : '▼') : '↕') + '</span></button></th>';
    }).join('');
    var rows = p.rows.map(function (r, i) {
      var on = selSources.indexOf(r.name) >= 0;
      var mini = ORDER.filter(function (k) { return r.counts[k] > 0; }).map(function (k) {
        return '<i style="--c:' + STATE_COLOR[k] + ';flex:' + r.counts[k] + ' 1 0" title="' + esc(STATE_SHORT[k] + ': ' + r.counts[k]) + '"></i>';
      }).join('');
      var reps = r.reps_known === false ? '<span class="sx-rep">not read</span>'
        : (r.reps.map(function (x) { return '<span class="sx-rep' + (r.reps_total === 1 ? ' solo' : '') + '">' + esc(x.name) + '<b>' + x.n + '</b></span>'; }).join('') +
           (r.reps_more ? '<span class="sx-rep">+' + r.reps_more + ' more</span>' : ''));
      var med = r.median_known === false ? '<b>n/a</b>'
        : (r.median_text ? '<b>' + esc(r.median_text) + '</b><small>n = ' + r.replied + '</small>' : '<b>-</b><small>n = 0</small>');
      var ot = r.decided ? '<b>' + esc(rateText(r.on_time_rate)) + '</b><small>of ' + r.decided + ' decided</small>'
                         : '<b>-</b><small>none decided</small>';
      return '<tr' + (on ? ' class="on"' : '') + ' style="--i:' + i + '">' +
        '<td data-label="Source"><button type="button" class="sx-src-name" data-source="' + esc(r.name) + '" aria-pressed="' + on + '" title="' + esc('Filter every panel by ' + r.name) + '"><i></i><span>' + esc(pretty(r.name)) + '</span></button></td>' +
        '<td data-label="Leads"><span class="sx-lead"><b>' + num(r.n) + '</b><span class="sx-share" title="' + esc(rateText(r.share) + ' of the leads in view') + '"><i style="width:' + Math.max(2, (r.share.pct == null ? (r.n / Math.max(1, p.total_leads) * 100) : r.share.pct)) + '%"></i></span></span></td>' +
        stageCell('Assigned', r.stages.assigned) + stageCell('AI analysed', r.stages.analyzed) + stageCell('ClickUp', r.stages.task) + stageCell('Replied', r.stages.update) +
        cell('SLA outcome', '<span class="sx-mini" role="img" aria-label="' + esc(ORDER.map(function (k) { return STATE_SHORT[k] + ' ' + r.counts[k]; }).join(', ')) + '">' + mini + '</span>') +
        cell(STATE_SHORT.on_time, ot) +
        '<td data-label="' + esc(STATE_SHORT.past_sla) + '"' + (r.past_sla ? '' : ' class="dim"') + '><b>' + num(r.past_sla) + '</b><small>of ' + r.n + '</small></td>' +
        cell('Median reply', med) + cell('Sales reps', '<span class="sx-reps">' + reps + '</span>') + '</tr>';
    }).join('');
    var foot = '<tr><td colspan="' + COLS.length + '"><b>' + num(p.total_names) + '</b> source' + (p.total_names === 1 ? '' : 's') +
      ' &middot; <b>' + num(p.total_leads) + '</b> lead' + (p.total_leads === 1 ? '' : 's') + ' in view' +
      (p.more ? ' &middot; the ' + p.rows.length + ' largest are shown' : '') +
      (p.best ? ' &middot; best answered on time: <b>' + esc(pretty(p.best.name)) + '</b> (' + esc(rateText(p.best.rate)) + ')' : '') + '</td></tr>';
    var lowN = (d.config || {}).low_n || 5;
    var anySmall = p.rows.some(function (r) { return r.decided > 0 && r.on_time_rate.pct == null; }) || (p.total_leads < lowN);
    $('#sxTable').innerHTML = '<div class="sx-wrap"><table class="sx-tbl"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody><tfoot>' + foot + '</tfoot></table></div>' +
      (anySmall ? '<p class="ld-note">A figure becomes a percentage only from <b>' + lowN + '</b> leads behind it; below that it stays a plain count such as &ldquo;0 of 2&rdquo;, because one lead either way would move a percentage by tens of points. Every cell carries its own n.</p>' : '');
    $('#sxSortNote').textContent = 'sorted by ' + ((d.options.sorts || []).filter(function (s) { return s.value === d.view.sort; })[0] || { label: d.view.sort }).label.toLowerCase() + ', ' + (d.view.dir === 'asc' ? 'smallest first' : 'largest first');
  }
  $('#sxTable').addEventListener('click', function (e) {
    var t = e.target.closest ? e.target.closest('button') : null;
    if (!t || !last.ctx) return;
    if (t.hasAttribute('data-sort')) return last.ctx.setSort(t.getAttribute('data-sort'));
    if (t.hasAttribute('data-source')) return last.ctx.toggleSource(t.getAttribute('data-source'));
  });

  // ------------------------------------------------------------------ entry point used by leads.js
  function render(d, ctx) {
    last.ctx = ctx;
    last.data = d;
    syncStateShort(d.options);
    renderBanner(d.banner);
    renderControls(d, ctx);
    var P = d.panels;
    renderCohorts(P.cohorts, d, ctx);
    renderList(P.cohorts, d);
    renderTable(P.sources, d);
    var why = $('#sxCohortHint');
    why.innerHTML = 'grouped by <b>' + esc(d.view.bucket_noun) + '</b>' + (d.view.bucket_auto ? ' (chosen for you)' : '') +
      ' &middot; ' + esc(d.view.bucket_why || '');
    $('#sxCsvCohort').disabled = !(P.cohorts && P.cohorts.state === 'ok');
  }
  function clear() {
    purge('#sxCohort');
    last.sig = {};
  }
  function failure(msg) {
    ['#sxCohort', '#sxList', '#sxTable'].forEach(function (id) {
      $(id).innerHTML = '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>' + esc(msg) + ' This tab keeps trying by itself.</span></div>';
    });
    $('#sxBanner').hidden = true;
  }

  D.pages = D.pages || {};
  D.pages.sources = { render: render, clear: clear, failure: failure, colors: C };
})();
