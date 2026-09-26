/* ERP Desk - Channels page: the marketing channel-performance report for managers (one sentence, five tiles) and
   analysts (chart, per-channel table, campaign drill-down, definitions, CSV).
   Draws the JSON of GET /api/channels (desktop/channels_data.py). The SERVER decides everything - the headline sentence, every
   number, every "no spend field" / "no clicks" reason, which percentage is allowed (denominator >= low_n), the sort order -
   and this file only lays it out. Filters live here as plain state and go to the server on every change, so the tiles, the
   chart, the table, the drill-down and all three CSV files always describe the same rows.
   In the owner's database today the normal state is "not installed" or "no data loaded yet": those are drawn as a setup card
   with the exact commands, never as zeros. All server text goes through esc() / textContent. One poller. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var FB = D.filterBar || null;                     // the fade math of the Leads filter bar (static/leads_filterbar.js), when loaded
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var $$ = function (s, r) { return Array.prototype.slice.call((r || document).querySelectorAll(s)); };
  var esc = D.esc;

  var POLL_MS = 30000;
  var FETCH_TIMEOUT_MS = 20000;          // above the server's worst case (connect timeout + query budget), lesson L-094 / L-182
  var OLD_AFTER_MS = POLL_MS * 3;
  var reduceMotion = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);

  var kit = D.chartKit || null;
  var C = (kit && kit.colors) || { ink: '#1c1d22', dim: '#6b6f76', blue: '#3452eb', coral: '#ff5a36', green: '#12b886', violet: '#7c5cff', amber: '#f2b705', red: '#e0393e', line: '#e7e2d8', grid: '#f2efe8' };
  var SLOTS = [C.blue, C.coral, C.green, C.violet, C.amber, C.red, C.ink];   // a channel keeps its colour: the slot comes from the server
  var OTHER = '#9a968b';
  var TONE = { blue: C.blue, violet: C.violet, green: C.green, amber: C.amber, coral: C.coral, red: C.red, ink: C.ink, grey: '#b9b5aa' };
  function slotColor(slot) { return slot < 0 ? OTHER : SLOTS[slot % SLOTS.length]; }

  var S = {
    data: null, at: 0, timer: null, fails: 0, busy: false, again: false, inflight: null, played: false, live: 'connecting', old: false, err: '',
    f: { from: null, to: null, channel: [], campaign: null, currency: null, focus: null }, sort: null, dir: null,
    metric: null, hidden: {}, sig: {}, debounce: null, defsOpen: false, csvBusy: false, badLoops: 0
  };

  // ------------------------------------------------------------------ small helpers
  function pad(n) { return (n < 10 ? '0' : '') + n; }
  function num(v) { return Number(v).toLocaleString('en-US'); }
  function plural(n, one, many) { return num(n) + ' ' + (n === 1 ? one : (many || one + 's')); }
  function pretty(s) { return String(s == null ? '' : s).replace(/_/g, ' '); }
  // Money always carries its currency code and is never converted or added across currencies (the server decides which figures may be
  // shown as one amount: state ok = one currency, multi = one line per currency). Fraction digits follow the currency (VND has none).
  var FRACTION = {};
  function fractionDigits(cur) {
    if (FRACTION[cur] == null) {
      try { FRACTION[cur] = new Intl.NumberFormat('en-US', { style: 'currency', currency: cur }).resolvedOptions().maximumFractionDigits; } catch (e) { FRACTION[cur] = 2; }
    }
    return FRACTION[cur];
  }
  function fmtMoney(v, cur) { var d = fractionDigits(cur); return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function derivedTag(kind) { return kind === 'all' ? 'derived' : kind === 'partly' ? 'partly derived' : ''; }
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];    // spelled here, like the server's sentence: never the browser's own "Sept"
  function fmtDay(k) {
    if (!k) return '';
    var p = k.split('-');
    return p.length === 3 && MON[+p[1] - 1] ? (+p[2]) + ' ' + MON[+p[1] - 1] + ' ' + p[0] : k;
  }
  function fmtShortDay(k) { var p = k.split('-'); return (+p[2]) + ' ' + MON[+p[1] - 1]; }
  function fmtTime(iso) { var d = new Date(iso); return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function fmtStamp(iso) {
    var d = new Date(iso);
    return isNaN(d) ? '' : d.getDate() + ' ' + MON[d.getMonth()] + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }
  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return 'just now';
    if (s < 90) return s + 's ago';
    return Math.round(s / 60) + 'm ago';
  }
  function addDays(key, delta) {           // 'YYYY-MM-DD' arithmetic without time-zone surprises
    var p = key.split('-'); var d = new Date(Date.UTC(+p[0], +p[1] - 1, +p[2] + delta));
    return d.getUTCFullYear() + '-' + pad(d.getUTCMonth() + 1) + '-' + pad(d.getUTCDate());
  }
  function pctText(pct) {                  // the server only sends a pct when its denominator is at least low_n
    if (pct == null) return '';
    if (pct > 0 && pct < 0.1) return '<0.1%';
    return (Math.abs(pct) >= 100 ? Math.round(pct) : (Math.round(pct * 10) / 10)) + '%';
  }
  function hasFilter() { var f = S.f; return !!(f.from || f.to || f.channel.length || f.campaign != null || f.currency); }
  function toggle(list, v) { var i = list.indexOf(v); if (i >= 0) list.splice(i, 1); else list.push(v); }
  function html(el, s) { el.innerHTML = s; }

  // ------------------------------------------------------------------ request
  function params(extra) {
    var q = new URLSearchParams(), f = S.f;
    if (f.from) q.append('from', f.from);
    if (f.to) q.append('to', f.to);
    f.channel.forEach(function (v) { q.append('channel', v); });
    if (f.campaign != null) q.append('campaign', String(f.campaign));
    if (f.currency) q.append('currency', f.currency);
    if (f.focus) q.append('focus', f.focus);
    if (S.sort) q.append('sort', S.sort);
    if (S.dir) q.append('dir', S.dir);
    return q.toString() + (extra || '');
  }
  function fetchChannels(fresh) {
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer;
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () { var e = new Error('timeout'); e.timeout = true; reject(e); if (ctl) ctl.abort(); }, FETCH_TIMEOUT_MS);
    });
    var req = fetch('/api/channels?' + params(fresh ? '&fresh=1' : ''), { cache: 'no-store', signal: ctl ? ctl.signal : undefined })
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }, function () { return { status: r.status, body: null }; }); });
    return Promise.race([req, timeout]).then(function (x) { clearTimeout(timer); return x; }, function (e) { clearTimeout(timer); throw e; });
  }

  // ------------------------------------------------------------------ live pill
  function setLive(state, label) {
    S.live = state;
    var p = $('#chPill');
    p.setAttribute('data-state', state);
    S.old = (state === 'stale' && label !== 'PARTIAL') || state === 'offline';
    $('#chLive').textContent = label || { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#chAgo');
    if (!S.at) { el.textContent = ''; return; }
    el.textContent = 'updated ' + ago(S.at);
    var stale = !!(S.old && S.data), badge = $('#chStale');       // L-093: old numbers must LOOK old, not only carry a pill
    $('#ch').classList.toggle('stale', stale);
    badge.hidden = !stale;
    if (stale) badge.textContent = 'These numbers were read ' + ago(S.at) + (S.err ? ' (latest attempt: ' + S.err.replace(/[.\s]+$/, '') + ')' : '') + ' and are shown until the next successful read.';
    $('#chPill').title = (S.old && S.err ? S.err + '. Showing the numbers read at ' + fmtTime(S.data && S.data.generated_at) + '. ' : 'Read from the database at ' + fmtTime(S.data && S.data.generated_at) + '. ') +
      'This page re-reads every ' + (POLL_MS / 1000) + ' s while it is open, and at once when a filter changes';
  }

  // ------------------------------------------------------------------ header
  function chip(cls, text, title) { return '<span class="ch-chip ' + cls + '"' + (title ? ' title="' + esc(title) + '"' : '') + '>' + text + '</span>'; }
  function renderHead(d) {
    var h = d.headline || {};
    $('#chTitle').textContent = h.text || 'Channels';
    $('#chTitle').setAttribute('data-tone', h.tone || 'grey');
    $('#chSub').textContent = h.sub || '';
    var chips = [];
    if (d.range) {
      chips.push(chip('range', '<b>' + esc(fmtDay(d.range.from)) + '</b> to <b>' + esc(fmtDay(d.range.to)) + '</b>', 'The range in view (both ends included). ' + (d.range.defaulted ? 'No range was chosen, so it is the last ' + d.range.days + ' days up to the newest day the rollup holds.' : 'Chosen with the filters below.')));
      var wk = d.range.week_periods || 0, dy = d.range.day_periods == null ? d.range.days_with_data : d.range.day_periods;
      chips.push(chip('', esc(plural(d.range.days, 'day')) + ' · ' + (wk ? (dy ? esc(String(dy)) + ' with daily data · ' : '') + esc(plural(wk, 'week')) + ' of weekly data' : esc(String(d.range.days_with_data)) + ' with data'),
        wk ? 'A weekly source is stored on each week\'s START day and covers 7 days; it is counted in weeks here, never as one day of activity.' : 'A day with no rollup row is a gap, not a zero: the refresh writes no zero rows.'));
      if (d.money && d.money.mode === 'multi' && !(d.filters && d.filters.currency)) {
        chips.push(chip('warn', 'money in <b>' + esc(String(d.money.currencies.length)) + ' currencies</b> · not added', 'Spend and revenue are shown per currency and are never added together or converted. Pick a currency in the filter bar to narrow the view to one.'));
      }
    }
    if (d.time_zone) chips.push(chip('', 'time zone <b>' + esc(d.time_zone) + '</b>', 'The database session time zone. Every date here is an event_date exactly as the source reported it.'));
    if (d.source_table) chips.push(chip('code', esc(d.source_table), 'The rollup table the tiles, chart and table read (never the raw interaction table).'));
    var f = d.freshness || {};
    if (f.data_as_of) chips.push(chip(f.stale ? 'warn' : '', 'rollup refreshed <b>' + esc(fmtStamp(f.data_as_of)) + '</b>', f.text || ''));
    else if (d.state !== 'not_installed') chips.push(chip('', 'rollup never refreshed', f.text || ''));
    html($('#chChips'), chips.join(''));
    var w = $('#chWarn');
    w.hidden = !(d.warnings && d.warnings.length);
    html(w, (d.warnings || []).map(function (x) { return '<div>' + esc(x) + '</div>'; }).join(''));
  }

  // ------------------------------------------------------------------ filters
  function presetOf(d) {
    var r = d.range, o = d.options || {};
    if (!r || !o.last_day || r.to !== o.last_day) return 'custom';
    var byDays = { 7: '7d', 30: '30d', 90: '90d' };
    if (byDays[r.days]) return byDays[r.days];
    return r.from === o.first_day ? 'all' : 'custom';
  }
  function updateFade(el) {
    if (!FB) return;
    var group = el.closest('.fg-multi');
    if (!group) return;
    var r = FB.computeFade(el.scrollLeft, el.clientWidth, el.scrollWidth);
    group.classList.toggle('fade-start', r.fadeStart);
    group.classList.toggle('fade-end', r.fadeEnd);
  }
  function renderFilters(d) {
    var o = d.options || {}, f = S.f, box = $('#chFilters');
    var canFilter = d.state !== 'not_installed' && d.state !== 'empty';
    box.hidden = !canFilter;
    if (!canFilter) return;
    var sig = JSON.stringify([o, f, d.range, d.filters, d.state]);
    if (S.sig.filters === sig) return;
    S.sig.filters = sig;
    var active = document.activeElement, focusKey = active && box.contains(active) ? active.getAttribute('data-k') : null;
    var scrollPos = FB ? FB.buildScrollMap($$('.fg-scroll', box).map(function (el) { return { group: el.getAttribute('data-group'), scrollLeft: el.scrollLeft }; })) : {};
    var pre = presetOf(d), h = '';
    h += '<div class="fgroup"><span class="fgroup-label">RANGE</span><div class="seg" data-seg="range">' +
      [['7d', '7 days'], ['30d', '30 days'], ['90d', '90 days'], ['all', 'All data']].map(function (p) {
        return '<button type="button" data-k="range-' + p[0] + '" data-v="' + p[0] + '" class="' + (pre === p[0] ? 'on' : '') + '">' + p[1] + '</button>'; }).join('') + '</div>' +
      '<label class="ld-date"><span>from</span><input type="date" data-k="from" data-date="from" value="' + esc((d.range && d.range.from) || '') + '" min="2000-01-01" max="2100-12-31"></label>' +
      '<label class="ld-date"><span>to</span><input type="date" data-k="to" data-date="to" value="' + esc((d.range && d.range.to) || '') + '" min="2000-01-01" max="2100-12-31"></label></div>';
    h += '<div class="fgroup fg-multi"><span class="fgroup-label">CHANNEL</span><div class="fg-scroll" data-group="channel">' + (o.channels || []).map(function (c, i) {
      var on = f.channel.indexOf(c.key) >= 0;
      return '<button type="button" class="pill ' + (on ? 'on' : '') + '" data-k="channel-' + i + '" data-multi="channel" data-v="' + esc(c.key) + '" aria-pressed="' + on + '" title="' + esc(c.name + (c.medium ? ' · ' + pretty(c.medium) : '')) + '"><i style="background:' + slotColor(c.slot) + '"></i>' + esc(c.name) + '</button>'; }).join('') + '</div></div>';
    var curs = o.currencies || [];
    if (curs.length > 1 || f.currency) {
      h += '<div class="fgroup"><span class="fgroup-label">CURRENCY</span><div class="seg" data-cseg="currency">' + curs.map(function (c, i) {
        var on = f.currency === c.code;
        return '<button type="button" data-k="currency-' + i + '" data-cur="' + esc(c.code) + '" class="' + (on ? 'on' : '') + '" aria-pressed="' + on + '" title="' +
          esc(c.code + ' - ' + (c.has_money ? 'carries spend or revenue' : 'no spend or revenue') + ' - ' + plural(c.days, 'day') + ' with data. Click to narrow the whole page to this currency.') + '">' + esc(c.code) + '</button>'; }).join('') + '</div></div>';
    }
    var inView = d.panels && d.panels.table && d.panels.table.state === 'ok' ? d.panels.table.channels : null;
    h += '<div class="fcount"><span>' + (inView == null ? '' : '<b>' + num(inView) + '</b> of ' + num((o.channels || []).length) + ' channels in view') + '</span>' +
      (hasFilter() ? '<button type="button" class="linkbtn" data-reset>Reset all</button>' : '') + '</div>';
    var chips = [], fl = d.filters || {};
    if (f.from || f.to) chips.push(['range', 'DATE', (f.from ? fmtShortDay(f.from) : 'start') + ' to ' + (f.to ? fmtShortDay(f.to) : 'newest day')]);
    f.channel.forEach(function (v) { var c = (o.channels || []).filter(function (x) { return x.key === v; })[0]; chips.push(['channel:' + v, 'CHANNEL', c ? c.name : v]); });
    if (f.campaign != null) chips.push(['campaign', 'CAMPAIGN', fl.campaign_name || ('#' + f.campaign)]);
    if (f.currency) chips.push(['currency', 'CURRENCY', f.currency]);
    h += '<div class="chips">' + chips.map(function (c) { return '<span class="chip"><em>' + c[1] + '</em>' + esc(c[2]) + '<button type="button" data-clear="' + esc(c[0]) + '" aria-label="Remove the ' + esc(c[1].toLowerCase()) + ' filter">×</button></span>'; }).join('') + '</div>';
    html(box, h);
    $$('.fg-scroll', box).forEach(function (el) {
      var was = FB ? FB.scrollLeftFor(scrollPos, el.getAttribute('data-group')) : 0;
      if (was) el.scrollLeft = was;
      updateFade(el);
    });
    if (focusKey) { var again = box.querySelector('[data-k="' + focusKey + '"]'); if (again) again.focus({ preventScroll: true }); }
  }
  function changed(resetFocus) {
    S.sig.filters = null;
    if (resetFocus) S.f.focus = null;
    if (S.data) renderFilters(S.data);
    clearTimeout(S.debounce);
    S.debounce = setTimeout(function () { S.debounce = null; load(true); }, 160);   // cleared when it fires (L-166): the CSV button checks it
  }
  $('#chFilters').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-reset')) { S.f = { from: null, to: null, channel: [], campaign: null, currency: null, focus: null }; return changed(); }
    if (b.hasAttribute('data-clear')) {
      var k = b.getAttribute('data-clear');
      if (k === 'range') { S.f.from = null; S.f.to = null; }
      else if (k === 'campaign') S.f.campaign = null;
      else if (k === 'currency') S.f.currency = null;
      else if (k.indexOf('channel:') === 0) toggle(S.f.channel, k.slice(8));
      return changed();
    }
    if (b.hasAttribute('data-multi')) { toggle(S.f.channel, b.getAttribute('data-v')); return changed(); }
    if (b.hasAttribute('data-cur')) { var code = b.getAttribute('data-cur'); S.f.currency = S.f.currency === code ? null : code; return changed(); }
    var seg = b.closest('[data-seg]');
    if (!seg || !S.data || !S.data.options.last_day) return;
    var v = b.getAttribute('data-v'), o = S.data.options;
    if (v === 'all') { S.f.from = o.first_day; S.f.to = o.last_day; }
    else { var n = parseInt(v, 10); S.f.from = addDays(o.last_day, -(n - 1)); S.f.to = o.last_day; }
    changed();
  });
  $('#chFilters').addEventListener('change', function (e) {
    var t = e.target;
    if (t.hasAttribute('data-date')) { S.f[t.getAttribute('data-date')] = t.value || null; return changed(); }
  });
  $('#chScroll').addEventListener('scroll', function () { $('#chFilters').classList.toggle('stuck', this.scrollTop > $('#chFilters').offsetTop + 6); }, { passive: true });
  $('#chFilters').addEventListener('scroll', function (e) { if (e.target && e.target.classList && e.target.classList.contains('fg-scroll')) updateFade(e.target); }, { passive: true, capture: true });
  if (window.ResizeObserver) new ResizeObserver(function () { $$('#chFilters .fg-scroll').forEach(updateFade); }).observe($('#chFilters'));

  // ------------------------------------------------------------------ KPI tiles
  function sparkHtml(t) {
    var sp = t.spark;
    if (!sp) return '<div class="t-spark" aria-hidden="true"></div>';
    var v = sp.values, n = v.length, real = v.filter(function (x) { return x != null; });
    var lo = Math.min.apply(null, real), hi = Math.max.apply(null, real), span = hi - lo;
    var xy = function (i, x) { return [n > 1 ? i / (n - 1) * 100 : 0, span ? 30 - (x - lo) / span * 26 : 16]; };
    var lines = '', areas = '', run = [], last = null;
    function flush() {
      if (run.length >= 2) {
        var d = run.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(2) + ' ' + p[1].toFixed(2); }).join(' ');
        lines += '<path class="ln" d="' + d + '"/>';
        areas += '<path class="ar" d="' + d + ' L' + run[run.length - 1][0].toFixed(2) + ' 32 L' + run[0][0].toFixed(2) + ' 32 Z"/>';
      }
      run = [];
    }
    v.forEach(function (x, i) { if (x == null) { flush(); return; } var p = xy(i, x); run.push(p); last = p; });
    flush();                                                       // a missing day BREAKS the line: a gap is not a zero
    var gaps = n - real.length;
    var wk = sp.grain === 'week';
    var label = (wk ? 'Per week (each point is one week, on its start day), ' : 'Per day, ') + fmtShortDay(sp.days[0]) + ' to ' + fmtShortDay(sp.days[n - 1]) + ': ' + real.length + (wk ? ' weeks' : ' days') + ' with data' + (gaps ? ', ' + gaps + ' without (gaps in the line)' : '');
    return '<div class="t-spark" role="img" aria-label="' + esc(label) + '" title="' + esc(label) + '"><svg viewBox="0 0 100 34" preserveAspectRatio="none"><line class="base" x1="0" y1="32" x2="100" y2="32"/>' +
      areas + lines + '</svg>' + (last ? '<span class="pt" style="left:' + last[0].toFixed(2) + '%;top:' + (last[1] / 34 * 100).toFixed(2) + '%"></span>' : '') + '</div>';
  }
  function deltaHtml(t) {
    var x = t.delta;
    if (!x) return '';
    var cls = x.good === true ? ' good' : x.good === false ? ' bad' : '';
    var arrow = x.dir === 'up' ? '▲ ' : x.dir === 'down' ? '▼ ' : '';
    return '<span class="t-delta' + cls + '" title="' + esc(x.state === 'ok' ? 'Compared with the same number of days immediately before the range' : x.text) + '">' + arrow + esc(x.text) + '</span>';
  }
  function renderKpis(d) {
    var box = $('#chKpis');
    var h = (d.kpis || []).map(function (t) {
      var na = t.state === 'unavailable', none = t.state === 'none', multi = t.state === 'multi';
      var isMoney = t.id === 'spend' || t.id === 'revenue';
      var val = na ? '<div class="ch-kpi-row"><span class="ch-kpi-value dash">–</span></div>' :
        none ? '<div class="ch-kpi-row"><span class="ch-kpi-value txt dash">no data</span></div>' :
        multi ? '<div class="ch-kpi-parts" title="More than one currency: each amount is on its own line and none are added together">' + (t.parts || []).map(function (p) {
          return '<span><em>' + esc(p.currency) + '</em>' + esc(fmtMoney(p.value, p.currency)) + (p.derived ? '<small>' + esc(derivedTag(p.derived)) + '</small>' : '') + '</span>'; }).join('') + '</div>' :
        t.text != null ? '<div class="ch-kpi-row"><span class="ch-kpi-value txt">' + esc(t.text) + '</span></div>' :
        '<div class="ch-kpi-row"><span class="ch-kpi-value' + (isMoney ? ' mny' : '') + '">' + (isMoney ? '<em class="cur">' + esc(t.unit || '') + '</em>' + esc(fmtMoney(t.value, t.unit)) : t.unit === '%' ? esc(pctText(t.value).replace('%', '')) + '<small>%</small>' : esc(num(t.value))) + '</span></div>';
      return '<div class="ch-kpi' + (na || none ? ' na' : '') + (multi ? ' multi' : '') + '" data-k="' + esc(t.id) + '" style="--kc:' + (TONE[t.tone] || C.blue) + '" title="' + esc(t.hint) + '">' +
        '<div class="ch-kpi-label">' + esc(t.label) + '</div>' + val +
        (na ? '<div class="t-spark" aria-hidden="true"></div><span class="t-na">UNAVAILABLE</span>' : (none ? '<div class="t-spark" aria-hidden="true"></div>' : sparkHtml(t))) +
        '<div class="ch-kpi-delta">' + (na || none ? '' : deltaHtml(t)) + '</div>' +
        '<div class="ch-kpi-sub">' + esc(t.sub || '') + '</div></div>';
    }).join('');
    if (S.sig.kpis !== h) {
      var prev = {}; $$('.ch-kpi', box).forEach(function (el) { prev[el.getAttribute('data-k')] = el.querySelector('.ch-kpi-value') && el.querySelector('.ch-kpi-value').textContent; });
      html(box, h);
      if (S.played) $$('.ch-kpi', box).forEach(function (el) {
        var was = prev[el.getAttribute('data-k')], now = el.querySelector('.ch-kpi-value') && el.querySelector('.ch-kpi-value').textContent;
        if (was != null && now != null && was !== now) { el.classList.add('flash'); setTimeout(function () { el.classList.remove('flash'); }, 1300); }
      });
      S.sig.kpis = h;
    }
  }

  // ------------------------------------------------------------------ panel plumbing
  function chartsOk() { return !!(window.Plotly && kit); }
  function na(id, p) {
    html($(id), '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>This panel could not be read (' + esc(p.error || 'unknown error') + '). The other panels keep working.</span></div>');
  }
  function empty(id, title, sub) { html($(id), '<div class="ld-empty"><div><b>' + esc(title) + '</b><span>' + sub + '</span></div></div>'); }
  function purge() { $$('#chChart .ld-plot').forEach(function (el) { if (window.Plotly && el.data) { try { Plotly.purge(el); } catch (e) { /* ignore */ } } }); }
  var ro = window.ResizeObserver ? new ResizeObserver(function (entries) {
    entries.forEach(function (en) {
      var el = en.target; clearTimeout(el._rz);
      el._rz = setTimeout(function () { if (window.Plotly && el.data && el.offsetWidth) { try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ } } }, 120);
    });
  }) : null;

  // Belt and braces, as on the Leads filter bar: Plotly's own responsive listener and the observer above both go through
  // Plots.resize(), which drops a resize that lands while an earlier relayout is still running (seen when the window is
  // resized in quick steps: the svg kept the old, wider width and the page scrolled sideways). Once a second, only while this
  // page is on screen, compare what Plotly last laid out with the container and re-fit when they differ.
  setInterval(function () {
    if (!active()) return;
    $$('#chChart .ld-plot').forEach(function (el) {
      if (window.Plotly && el.data && el._fullLayout && el.offsetWidth && Math.abs(el._fullLayout.width - el.clientWidth) > 2) {
        try { Plotly.Plots.resize(el); } catch (e) { /* ignore */ }
      }
    });
  }, 1000);

  // ------------------------------------------------------------------ per-day chart (+ a per-week chart for a weekly source)
  // A weekly source's row carries a WHOLE WEEK on the week's start day. Drawn on the per-day axis it would be a false one-day spike, so the
  // server keeps weekly rows out of the per-day series and this page draws them as bars spanning their 7 days, in their own chart.
  function renderChart(p, d) {
    var host = $('#chChart');
    var sig = JSON.stringify([p, S.metric, S.hidden]);              // L-100: a poll that changed nothing must not redraw (focus, hover, scroll)
    if (S.sig.chart === sig) return;
    S.sig.chart = sig;
    if (p.state === 'unavailable') { purge(); return na('#chChart', p); }
    if (p.state === 'empty' || !p.series.length) {
      purge();
      $('#chChartTitle').textContent = 'Per day, by channel';
      return empty('#chChart', 'Nothing to draw for these filters', p.clipped ? 'The newest ' + esc(plural(p.days.length, 'day')) + ' of this range have no daily rows (and no weekly rows in its newest weeks): shorten the range to reach older data.' : 'No channel has a rollup row in this range.');
    }
    if (!chartsOk()) return empty('#chChart', 'Charts unavailable', 'plotly.js could not be loaded.');
    var metrics = p.metrics, cur = S.metric && metrics.some(function (m) { return m.key === S.metric && m.total > 0; }) ? S.metric : p.default_metric;
    S.metric = cur;
    var seg = metrics.map(function (m) {
      return '<button type="button" data-metric="' + m.key + '" class="' + (m.key === cur ? 'on' : '') + '"' + (m.total > 0 ? '' : ' disabled title="' + esc('No ' + m.label.toLowerCase() + ' recorded in this range') + '"') + '>' + esc(m.label) + '</button>';
    }).join('');
    html($('#chMetric'), seg);
    if (!host.querySelector('.ld-plot')) html(host, '<div class="ch-legend" id="chLegend"></div><div class="ch-sub" id="chDayHead"></div><div class="ld-plot ch-plot-day"></div>' +
      '<div class="ch-sub" id="chWeekHead"></div><div class="ld-plot ch-plot-week"></div><div class="ch-note" id="chChartNote"></div>');
    var dayEl = host.querySelector('.ch-plot-day'), weekEl = host.querySelector('.ch-plot-week');
    var showDay = !!p.has_daily, showWeek = !!(p.has_weekly && p.weeks && p.weeks.length);
    $('#chChartTitle').textContent = showWeek ? (showDay ? 'Per day and per week, by channel' : 'Per week, by channel') : 'Per day, by channel';
    $('#chDayHead').textContent = showDay && showWeek ? 'Per day - sources that report daily' : '';
    $('#chDayHead').hidden = !(showDay && showWeek);
    $('#chWeekHead').textContent = showWeek ? 'Per week - sources that report weekly: each bar covers the 7 days from its start date, it is not one day of activity' : '';
    $('#chWeekHead').hidden = !showWeek;
    dayEl.hidden = !showDay; weekEl.hidden = !showWeek;
    var traces = p.series.map(function (s) {
      var col = slotColor(s.slot);
      return { type: 'scatter', mode: 'lines+markers', name: s.name, x: p.days, y: s.values[cur], connectgaps: false, visible: S.hidden[s.key] ? 'legendonly' : true,
        line: { color: col, width: s.slot < 0 ? 1.8 : 2.4, shape: 'linear', dash: s.slot < 0 ? 'dot' : 'solid' }, marker: { color: col, size: 4.5 },
        hovertemplate: '%{fullData.name}: <b>%{y:,}</b><extra></extra>' };
    });
    var legend = p.series.map(function (s) {
      return '<button type="button" class="ch-lg' + (S.hidden[s.key] ? ' off' : '') + '" data-lg="' + esc(s.key) + '" style="--c:' + slotColor(s.slot) + '" aria-pressed="' + (!S.hidden[s.key]) + '" title="Click to show or hide this line"><i></i>' + esc(s.name) + '</button>';
    }).join('');
    html($('#chLegend'), legend);
    var ymax = 0; p.series.forEach(function (s) { s.values[cur].forEach(function (v) { if (v != null && v > ymax) ymax = v; }); });
    var lay = kit.baseLayout(300, { margin: { l: 52, r: 12, t: 8, b: 36 }, hovermode: 'x unified', showlegend: false,
      xaxis: { type: 'date', tickformat: '%d %b', tickangle: 0, nticks: p.days.length > 14 ? 9 : p.days.length, range: [p.days[0], p.days[p.days.length - 1]] },
      yaxis: { tickformat: ',d', rangemode: 'tozero', range: [0, ymax ? ymax * 1.08 : 1] } });
    if (reduceMotion) lay.transition = { duration: 0 };
    var wtraces = [], wlay = null;
    if (showWeek) {
      // a bar is centred on the MIDDLE of its week (start + 3.5 days), so it visibly spans the 7 days it covers
      var mids = p.weeks.map(function (w) { return addDays(w, 3) + ' 12:00'; });
      wtraces = p.series.filter(function (s) { return s.week_values && s.week_values[cur].some(function (x) { return x != null; }); }).map(function (s) {
        var col = slotColor(s.slot);
        return { type: 'bar', name: s.name, x: mids, y: s.week_values[cur], text: p.weeks.map(function (w) { return 'week of ' + fmtDay(w); }), visible: S.hidden[s.key] ? 'legendonly' : true,
          textposition: 'none', marker: { color: col }, hovertemplate: '%{fullData.name}: <b>%{y:,}</b><extra>%{text}</extra>' };
      });
      wlay = kit.baseLayout(240, { margin: { l: 52, r: 12, t: 8, b: 48 }, hovermode: 'closest', showlegend: false, barmode: 'group',
        xaxis: { type: 'date', tickvals: mids, ticktext: p.weeks.map(fmtShortDay), tickangle: 0, title: { text: 'week starting', standoff: 6, font: { size: 11, color: C.dim } },
                 range: [addDays(p.weeks[0], -1) + ' 00:00', addDays(p.weeks[p.weeks.length - 1], 8) + ' 00:00'] },
        yaxis: { tickformat: ',d', rangemode: 'tozero' } });
      if (reduceMotion) wlay.transition = { duration: 0 };
    }
    var draw = function () {
      if (showDay) { Plotly.react(dayEl, traces, lay, { displayModeBar: false, responsive: true }); if (ro && !dayEl._ro) { dayEl._ro = true; ro.observe(dayEl); } }
      else if (dayEl.data) { try { Plotly.purge(dayEl); } catch (e) { /* ignore */ } }
      if (showWeek) { Plotly.react(weekEl, wtraces, wlay, { displayModeBar: false, responsive: true }); if (ro && !weekEl._ro) { weekEl._ro = true; ro.observe(weekEl); } }
      else if (weekEl.data) { try { Plotly.purge(weekEl); } catch (e) { /* ignore */ } }
    };
    if (!D.fontsDone()) D.fontsReady(draw); else draw();                       // Plotly measures text once: wait for the font
    var gaps = p.days.length - p.days_with_data;
    var note = [];
    if (showDay && gaps > 0) note.push('<b>' + plural(gaps, 'day') + '</b> in this window ' + (gaps === 1 ? 'has' : 'have') + ' no rollup row - the lines break there instead of dropping to zero.');
    if (showWeek) {
      var wgaps = p.weeks.length - p.weeks_with_data;
      note.push('Weekly rows are counted on their <b>start day</b>; a bar is one whole week.' + (wgaps > 0 ? ' <b>' + plural(wgaps, 'week') + '</b> ' + (wgaps === 1 ? 'has' : 'have') + ' no row (an empty slot, not a zero).' : ''));
    }
    if (p.clipped && showDay) note.push('The per-day chart shows the newest ' + p.days.length + ' days of the range.');
    if (p.week_clipped && showWeek) note.push('The per-week chart shows the newest ' + ((d.config && d.config.chart_max_weeks) || 104) + ' weeks of the range.');
    if (p.lines_hidden) note.push(plural(p.lines_hidden, 'smaller channel') + ' added into the dotted line.');
    html($('#chChartNote'), note.length ? '<p class="ld-note">' + note.join(' ') + '</p>' : '');
    $('#chDailyCsv').disabled = !(d.export && d.export.daily);
  }
  $('#chMetric').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-metric]') : null;
    if (!b || b.disabled || !S.data) return;
    S.metric = b.getAttribute('data-metric');
    S.sig.chart = null; renderChart(S.data.panels.chart, S.data);
  });
  $('#chChart').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-lg]') : null;
    if (!b || !S.data) return;
    var k = b.getAttribute('data-lg'); S.hidden[k] = !S.hidden[k];
    S.sig.chart = null; renderChart(S.data.panels.chart, S.data);
  });

  // ------------------------------------------------------------------ per-channel table
  var COLS = [                          // heading, sort key (a fixed vocabulary shared with the server), cell class
    ['Channel', 'channel', 'l'], ['Sessions', 'sessions', 'n'], ['Clicks', 'clicks', 'n'], ['Impressions', 'impressions', 'n'], ['CTR', 'ctr', 'n'],
    ['Conversions', 'conversions', 'n'], ['Conv. rate', 'cvr', 'n'], ['Spend', 'spend', 'n'], ['Revenue', 'revenue', 'n'], ['Cost / conv.', 'cpa', 'n']
  ];
  function rateCell(r, what) {          // pct when allowed; "2 of 3" under the low-n rule; the reason when there is no denominator
    if (r.pct != null) return esc(pctText(r.pct));
    if (r.low_n) return '<span class="ch-cnt" title="Fewer than ' + ((S.data && S.data.config.low_n) || 5) + ' ' + what + ' behind this, so the counts are shown instead of a percentage">' + esc(num(r.n) + ' of ' + num(r.of)) + '</span>';
    return '<span class="ch-no" title="' + esc(r.why || '') + '">' + esc(r.why || '–') + '</span>';
  }
  // Money: one amount WITH its currency code, or one line per currency, or the reason there is none. The server never sends a total across currencies.
  function moneyOne(cur, v, derived) {
    return '<span class="ch-mny"><em>' + esc(cur) + '</em> ' + esc(fmtMoney(v, cur)) + '</span>' +
      (derived ? '<small class="ch-drv" title="Computed by the connector (purchases x value per purchase), not reported by the source">' + esc(derivedTag(derived)) + '</small>' : '');
  }
  function moneyCell(m, what) {
    if (m.state === 'ok') return moneyOne(m.currency, m.value, m.derived);
    if (m.state === 'multi') return '<span class="ch-mny-stack" title="More than one currency: each amount is on its own line and none are added together">' + m.parts.map(function (p) { return moneyOne(p.currency, p.value, p.derived); }).join('') + '</span>';
    if (m.state === 'unavailable') return '<span class="ch-no" title="The money figures could not be read">unavailable</span>';
    return '<span class="ch-no" title="The source carries no ' + esc(what) + ' for this row, so nothing is shown rather than 0.00">' + esc(m.why || 'no ' + what) + '</span>';
  }
  function cpaCell(m) {
    if (m.state === 'ok') return '<span class="ch-mny"><em>' + esc(m.currency) + '</em> ' + esc(fmtMoney(m.value, m.currency)) + '</span>' + (m.low_n ? '<small title="Based on very few conversions">n = ' + m.n + '</small>' : '');
    if (m.state === 'multi') return '<span class="ch-mny-stack">' + m.parts.map(function (p) {
      return p.value != null ? '<span class="ch-mny"><em>' + esc(p.currency) + '</em> ' + esc(fmtMoney(p.value, p.currency)) + '</span>' : '<span class="ch-no">' + esc(p.currency + ' ' + (p.why || '–')) + '</span>'; }).join('') + '</span>';
    if (m.state === 'unavailable') return '<span class="ch-no">unavailable</span>';
    return '<span class="ch-no">' + esc(m.why || '–') + '</span>';
  }
  function shareCell(s) {
    var txt = s.pct != null ? pctText(s.pct) : (s.of ? num(s.n) + ' of ' + num(s.of) : '–');
    return '<span class="ch-share"><i style="--w:' + Math.max(0, Math.min(100, (s.frac || 0) * 100)).toFixed(2) + '%"></i></span><span class="ch-share-t">' + esc(txt) + '</span>';
  }
  function renderTable(p) {
    var host = $('#chTable');
    var sig = JSON.stringify([p, S.f.focus]);
    if (S.sig.table === sig) return;
    S.sig.table = sig;
    if (p.state === 'unavailable') return na('#chTable', p);
    if (p.state === 'empty') return empty('#chTable', 'No channel in view', 'Loosen a filter or press Reset all.');
    var focus = S.f.focus;
    var head = COLS.map(function (c) {
      var on = p.sort === c[1];
      return '<th scope="col" class="' + c[2] + '"' + (on ? ' aria-sort="' + (p.dir === 'asc' ? 'ascending' : 'descending') + '"' : '') + '><button type="button" data-sort="' + c[1] + '">' + esc(c[0]) + '<span class="ar" aria-hidden="true">' + (on ? (p.dir === 'asc' ? '▲' : '▼') : '↕') + '</span></button></th>';
    }).join('');
    var rows = p.rows.map(function (r) {
      var isFocus = focus === r.key;
      return '<tr class="' + (r.low_n ? 'low ' : '') + (isFocus ? 'focus' : '') + '" style="--sc:' + slotColor(r.slot) + '">' +
        '<td class="l" data-label="Channel"><button type="button" class="ch-name" data-focus="' + esc(r.key) + '" aria-pressed="' + isFocus + '" title="' + esc('Show the campaigns of ' + r.name) + '"><i style="background:' + slotColor(r.slot) + '"></i><span><b>' + esc(r.name) + '</b>' +
          '<em>' + esc(pretty(r.medium || '') + (r.is_paid === true ? ' · paid' : r.is_paid === false ? ' · unpaid' : '')) + (r.low_n ? ' · <u title="Fewer than ' + ((S.data && S.data.config.low_n) || 5) + ' sessions and fewer than that many clicks: read the rates as counts">low volume</u>' : '') + '</em></span></button></td>' +
        '<td class="n share" data-label="Sessions"><b>' + esc(num(r.sessions)) + '</b>' + shareCell(r.share) + '</td>' +
        '<td class="n" data-label="Clicks">' + esc(num(r.clicks)) + '</td>' +
        '<td class="n" data-label="Impressions">' + esc(num(r.impressions)) + '</td>' +
        '<td class="n" data-label="CTR">' + rateCell(r.ctr, 'impressions') + '</td>' +
        '<td class="n" data-label="Conversions">' + esc(num(r.conversions)) + '</td>' +
        '<td class="n" data-label="Conv. rate">' + rateCell(r.cvr, 'clicks') + '</td>' +
        '<td class="n" data-label="Spend">' + moneyCell(r.spend, 'spend') + '</td>' +
        '<td class="n" data-label="Revenue">' + moneyCell(r.revenue, 'revenue') + '</td>' +
        '<td class="n" data-label="Cost / conv.">' + cpaCell(r.cpa) + '</td></tr>';
    }).join('');
    var t = p.total;
    var foot = '<tr class="total"><td class="l" data-label="Channel"><b>' + esc(t.name) + '</b><em>' + plural(t.channels, 'channel') + '</em></td><td class="n" data-label="Sessions"><b>' + esc(num(t.sessions)) + '</b></td>' +
      '<td class="n" data-label="Clicks">' + esc(num(t.clicks)) + '</td><td class="n" data-label="Impressions">' + esc(num(t.impressions)) + '</td><td class="n" data-label="CTR">' + rateCell(t.ctr, 'impressions') + '</td>' +
      '<td class="n" data-label="Conversions">' + esc(num(t.conversions)) + '</td><td class="n" data-label="Conv. rate">' + rateCell(t.cvr, 'clicks') + '</td><td class="n" data-label="Spend">' + moneyCell(t.spend, 'spend') + '</td><td class="n" data-label="Revenue">' + moneyCell(t.revenue, 'revenue') + '</td><td class="n" data-label="Cost / conv.">' + cpaCell(t.cpa) + '</td></tr>';
    var sorts = (S.data.sorts || []).map(function (s) { return '<option value="' + s.value + '"' + (p.sort === s.value ? ' selected' : '') + '>' + esc(s.label) + '</option>'; }).join('');
    html(host, '<div class="ch-tbl-wrap"><table class="ch-tbl"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody><tfoot>' + foot + '</tfoot></table></div>' +
      '<div class="ch-tfoot"><span>' + (p.more ? '<b>' + num(p.more) + ' more</b> channel' + (p.more === 1 ? '' : 's') + ' not listed - narrow the filters. ' : '') + 'Rows with fewer than ' + ((S.data && S.data.config.low_n) || 5) + ' sessions and fewer than that many clicks are marked <em>low volume</em>; unknown values sort last.' + (p.money_mode === 'multi' ? ' <b>Several currencies in view:</b> money is shown per currency, never added, and a money sort orders within each currency.' : '') + '</span>' +
      '<label class="ch-sortsel">sort <select data-sortsel aria-label="Sort the channels by">' + sorts + '</select><button type="button" data-dir aria-label="Reverse the sort order">' + (p.dir === 'asc' ? '▲ asc' : '▼ desc') + '</button></label></div>');
  }
  $('#chTable').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-sort')) {
      var k = b.getAttribute('data-sort'), p = S.data && S.data.panels.table, first = ((S.data.sorts || []).filter(function (s) { return s.value === k; })[0] || {}).dir || 'desc';
      if (p && p.sort === k) { S.sort = k; S.dir = p.dir === 'asc' ? 'desc' : 'asc'; } else { S.sort = k; S.dir = first; }
      return load(true);
    }
    if (b.hasAttribute('data-dir')) { var q = S.data.panels.table; S.sort = q.sort; S.dir = q.dir === 'asc' ? 'desc' : 'asc'; return load(true); }
    if (b.hasAttribute('data-focus')) { var key = b.getAttribute('data-focus'); S.f.focus = S.f.focus === key ? null : key; S.sig.camp = null; return load(true); }
  });
  $('#chTable').addEventListener('change', function (e) {
    if (e.target.hasAttribute('data-sortsel')) { var k = e.target.value; S.sort = k; S.dir = ((S.data.sorts || []).filter(function (s) { return s.value === k; })[0] || {}).dir || 'desc'; load(true); }
  });

  // ------------------------------------------------------------------ campaign drill-down
  function renderCampaigns(p, d) {
    var host = $('#chCamp'), csv = $('#chCampCsv'), only = $('#chCampOnly');
    var sig = JSON.stringify([p, S.f.campaign]);
    if (S.sig.camp === sig) return;
    S.sig.camp = sig;
    csv.disabled = !(d.export && d.export.campaigns);
    only.hidden = !(p.focus);
    if (p.focus) only.textContent = 'Filter everything to ' + p.focus.name;
    $('#chCampTitle').textContent = p.focus ? 'Campaigns in ' + p.focus.name : 'Campaigns';
    if (p.state === 'need_focus') return empty('#chCamp', 'Pick a channel', 'Click a channel name in the table above to see its campaigns here. This panel reads the campaign rollup only for the channel you pick.');
    if (p.state === 'unavailable') return na('#chCamp', p);
    if (p.state === 'outside_filter') return empty('#chCamp', (p.focus ? p.focus.name : 'That channel') + ' is not in the channel filter', 'Add it to the channel filter, or click a channel that is in view, to see its campaigns.');
    if (p.state === 'empty') return empty('#chCamp', 'No campaign rows for ' + esc(p.focus.name), 'The campaign rollup has nothing for this channel in the range - the channel may report without campaigns.');
    var rows = p.rows.map(function (r) {
      var on = S.f.campaign === r.campaign_id;
      return '<tr class="' + (r.low_n ? 'low' : '') + '"><td class="l" data-label="Campaign"><button type="button" class="ch-name camp' + (on ? ' on' : '') + '" data-campaign="' + r.campaign_id + '" aria-pressed="' + on + '" title="Filter the whole page to this campaign"><span><b>' + esc(r.name) + '</b><em>' + (r.campaign_id ? '#' + r.campaign_id : 'rows without a campaign') + (r.low_n ? ' · <u>low volume</u>' : '') + '</em></span></button></td>' +
        '<td class="n share" data-label="Sessions"><b>' + esc(num(r.sessions)) + '</b>' + shareCell(r.share) + '</td>' +
        '<td class="n" data-label="Clicks">' + esc(num(r.clicks)) + '</td><td class="n" data-label="Impressions">' + esc(num(r.impressions)) + '</td><td class="n" data-label="CTR">' + rateCell(r.ctr, 'impressions') + '</td>' +
        '<td class="n" data-label="Conversions">' + esc(num(r.conversions)) + '</td><td class="n" data-label="Conv. rate">' + rateCell(r.cvr, 'clicks') + '</td>' +
        '<td class="n" data-label="Spend">' + moneyCell(r.spend, 'spend') + '</td><td class="n" data-label="Revenue">' + moneyCell(r.revenue, 'revenue') + '</td><td class="n" data-label="Cost / conv.">' + cpaCell(r.cpa) + '</td></tr>';
    }).join('');
    var head = ['Campaign', 'Sessions · share of the channel', 'Clicks', 'Impressions', 'CTR', 'Conversions', 'Conv. rate', 'Spend', 'Revenue', 'Cost / conv.'].map(function (c, i) {
      return '<th scope="col" class="' + (i ? 'n' : 'l') + '">' + esc(c) + '</th>'; }).join('');
    var note = '';
    if (p.more) note = '<p class="ch-more"><b>' + num(p.more) + ' more campaign' + (p.more === 1 ? '' : 's') + '</b> not listed - the ' + p.shown + ' largest by sessions are shown' +
      (p.rest_sessions != null ? ' (the rest add up to ' + num(p.rest_sessions) + ' sessions)' : '') + '. Narrow the filters - a shorter range or one campaign - to see them.</p>';
    else note = '<p class="ch-more">All ' + plural(p.shown, 'campaign') + ' of ' + esc(p.focus.name) + ' in this range, largest by sessions first.</p>';
    html(host, '<div class="ch-tbl-wrap"><table class="ch-tbl camp"><thead><tr>' + head + '</tr></thead><tbody>' + rows + '</tbody></table></div>' + note);
  }
  $('#chCamp').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button[data-campaign]') : null;
    if (!b) return;
    var id = parseInt(b.getAttribute('data-campaign'), 10);
    S.f.campaign = S.f.campaign === id ? null : id;
    changed();
  });
  $('#chCampOnly').addEventListener('click', function () {
    var f = S.data && S.data.filters && S.data.filters.focus;
    if (f) { S.f.channel = [f]; S.f.focus = null; changed(); }
  });

  // ------------------------------------------------------------------ setup / no-match states (never a zero, always the fix)
  function copyText(text, btn) {
    var done = function () { var t = btn.textContent; btn.textContent = 'Copied'; setTimeout(function () { btn.textContent = t; }, 1600); };
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) { navigator.clipboard.writeText(text).then(done, function () { fallbackCopy(text); done(); }); return; }
    } catch (e) { /* fall through */ }
    fallbackCopy(text); done();
  }
  function fallbackCopy(text) {
    var ta = document.createElement('textarea'); ta.value = text; ta.setAttribute('readonly', ''); ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select(); try { document.execCommand('copy'); } catch (e) { /* ignore */ } ta.remove();
  }
  function ghostTiles() {
    return ['Sessions', 'Clicks', 'Conversions', 'Conversion rate', 'Spend', 'Revenue'].map(function (l) {
      return '<div class="ch-ghost"><span>' + esc(l) + '</span><b>–</b><em>waiting for data</em></div>'; }).join('');
  }
  function renderSetup(d) {
    var host = $('#chSetup');
    if (d.state === 'not_installed' || d.state === 'empty') {
      var ins = d.install || { steps: [] };
      var title = d.state === 'not_installed' ? 'Set the channel report up' : 'Load some data';
      var lead = d.state === 'not_installed'
        ? (ins.missing && ins.missing.length ? 'Missing from this database: <code>' + ins.missing.map(esc).join('</code>, <code>') + '</code>. ' : '') + 'Nothing is shown - and nothing is invented - until the steps below are done.'
        : 'Two steps fill this page. Nothing is shown - and nothing is invented - until they are done.';
      var steps = ins.steps.map(function (s, i) {
        return '<li><div class="ch-step-h"><span class="ch-step-n">' + (i + 1) + '</span><b>' + esc(s.title) + '</b></div>' +
          '<div class="ch-cmd"><code>' + esc(s.command) + '</code><button type="button" class="ch-copy" data-copy="' + esc(s.command) + '" aria-label="Copy the command">Copy</button></div>' +
          (s.note ? '<p>' + esc(s.note) + '</p>' : '') + '</li>'; }).join('');
      html(host, '<article class="ld-panel ch-setup"><header><h3>' + esc(title) + '</h3><span class="hint">read-only page: it runs none of this for you</span></header>' +
        '<p class="ch-setup-lead">' + lead + '</p><ol class="ch-steps">' + steps + '</ol>' +
        (ins.note ? '<p class="ld-note">' + esc(ins.note) + '</p>' : '') + '</article>' +
        '<div class="ch-ghosts" aria-label="What this page will show once data exists (placeholders, no numbers)">' + ghostTiles() + '</div>' +
        '<div class="ch-ghost-chart"><span>A per-day line chart per channel (and a per-week chart for a weekly source), a per-channel table with share of sessions, rates and money per currency, and a campaign drill-down appear here.</span></div>');
      host.hidden = false;
      return;
    }
    if (d.state === 'no_match') {
      html(host, '<article class="ld-panel"><div class="ld-empty"><div><b>Nothing matches these filters</b><span>The rollup holds data (' + esc(fmtDay(d.options.first_day)) + ' to ' + esc(fmtDay(d.options.last_day)) + '), but not for this range, channel or campaign. ' +
        '<button type="button" class="linkbtn" data-reset-nm>Reset all filters</button></span></div></div></article>');
      host.hidden = false;
      return;
    }
    host.hidden = true;
    html(host, '');
  }
  $('#chSetup').addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-copy')) return copyText(b.getAttribute('data-copy'), b);
    if (b.hasAttribute('data-reset-nm')) { S.f = { from: null, to: null, channel: [], campaign: null, currency: null, focus: null }; changed(); }
  });

  // ------------------------------------------------------------------ definitions drawer
  function renderDefs(d) {
    var sig = JSON.stringify(d.definitions || []);
    if (S.sig.defs === sig) return;
    S.sig.defs = sig;
    html($('#chDefsBody'), '<dl>' + (d.definitions || []).map(function (x) { return '<dt>' + esc(x.term) + '</dt><dd>' + esc(x.text) + '</dd>'; }).join('') + '</dl>' +
      '<p class="ld-dr-foot">Generated by the server from the same constants the numbers use (low-n = ' + esc(String((d.config || {}).low_n)) + ', default range = ' + esc(String((d.config || {}).default_days)) + ' days), so this text cannot drift from the calculation.</p>');
  }
  function openDefs(open) {
    S.defsOpen = open;
    var dr = $('#chDrawer');
    dr.classList.toggle('open', open);
    dr.setAttribute('aria-hidden', open ? 'false' : 'true');
    $('#chScrim').hidden = !open;
    $('#chDefsBtn').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) setTimeout(function () { $('#chDefsClose').focus({ preventScroll: true }); }, 60); else $('#chDefsBtn').focus({ preventScroll: true });
  }
  $('#chDefsBtn').addEventListener('click', function () { openDefs(!S.defsOpen); });
  $('#chDefsClose').addEventListener('click', function () { openDefs(false); });
  $('#chScrim').addEventListener('click', function () { openDefs(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.defsOpen) { e.stopPropagation(); openDefs(false); } });

  // ------------------------------------------------------------------ "how this page stays fast"
  function renderFast(d) {
    var box = $('#chFast'), r = d.read || {}, f = d.freshness || {};
    if (d.state === 'not_installed') { box.hidden = true; return; }
    box.hidden = false;
    var tables = (r.tables && r.tables.length ? r.tables : [d.source_table || 'interaction_daily_channel_rollup']).map(function (t) { return '<code>' + esc(t) + '</code>'; }).join(' and ');
    html($('#chFastBody'), '<p>This page reads ' + tables + (r.rollup_rows != null ? ' (' + num(r.rollup_rows) + ' day × channel rows here)' : '') + ' - small daily summaries - and <b>never the raw interaction rows</b>, so a request costs about the same whether the sources sent a thousand events or a billion. Rates are computed from sums when you look, never stored.</p>' +
      '<p>The summaries are ' + esc((d.config && d.config.refresh) || '') + '.' + (f.data_as_of ? ' Last recomputed <b>' + esc(fmtStamp(f.data_as_of)) + '</b>' + (f.last_status && f.last_status !== 'ok' ? ' (the latest run ended <b>' + esc(f.last_status) + '</b>)' : '') + '.' : ' It has not been recomputed yet.') + '</p>');
  }

  // ------------------------------------------------------------------ CSV download
  // L-101: the link comes from the export urls the SERVER put in the payload now on screen, never from live filter state.
  function downloadFrom(url, btn, fallback, what) {
    if (S.csvBusy || S.busy || S.debounce || !url) return;
    S.csvBusy = true;
    btn.disabled = true;
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, 60000);
    fetch(url, { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then(function (r) {
      if (!r.ok) return r.json().then(function (j) { throw new Error((j && j.error) || ('HTTP ' + r.status)); }, function () { throw new Error('HTTP ' + r.status); });
      var name = /filename="?([^";]+)"?/.exec(r.headers.get('Content-Disposition') || '');
      var rows = r.headers.get('X-Row-Count'), cut = r.headers.get('X-Export-Truncated') === 'true';
      return r.blob().then(function (b) {
        var a = document.createElement('a');
        a.href = URL.createObjectURL(b); a.download = name ? name[1] : fallback;
        document.body.appendChild(a); a.click(); a.remove();
        setTimeout(function () { URL.revokeObjectURL(a.href); }, 4000);
        D.toast('Downloaded ' + (rows || '') + ' row' + (rows === '1' ? '' : 's') + ' as ' + (name ? name[1] : fallback) + (cut ? ' - the file was cut at the export limit' : ''), cut ? 'warn' : 'ok', 5200);
      });
    }).catch(function (e) {
      D.toast('The ' + what + ' could not be downloaded: ' + (e && e.name === 'AbortError' ? 'it took too long' : (e && e.message) || 'unknown error'), 'bad', 6000);
    }).then(function () { clearTimeout(timer); S.csvBusy = false; syncButtons(); });
  }
  function downloadCsv(part, btn) {
    if (!S.data || !S.data.export || !S.data.export[part]) return;
    downloadFrom(S.data.export[part], btn, 'channels.csv', 'CSV');
  }
  function syncButtons() {
    var d = S.data, ex = d && d.export;
    $('#chCsv').disabled = !(ex && ex.channels) || S.csvBusy;
    $('#chXlsx').disabled = !(ex && ex.xlsx) || S.csvBusy;
    $('#chDailyCsv').disabled = !(ex && ex.daily) || S.csvBusy || !(d.panels && d.panels.chart && d.panels.chart.state === 'ok');
    $('#chCampCsv').disabled = !(ex && ex.campaigns) || S.csvBusy || !(d.panels && d.panels.campaigns && d.panels.campaigns.state === 'ok');
    if (D.channelsPlacements) D.channelsPlacements.sync(S.csvBusy);
  }
  $('#chCsv').addEventListener('click', function () { downloadCsv('channels', this); });
  $('#chXlsx').addEventListener('click', function () { if (S.data && S.data.export && S.data.export.xlsx) downloadFrom(S.data.export.xlsx, this, 'channels_report.xlsx', 'Excel report'); });
  $('#chDailyCsv').addEventListener('click', function () { downloadCsv('daily', this); });
  $('#chCampCsv').addEventListener('click', function () { downloadCsv('campaigns', this); });

  // ------------------------------------------------------------------ render + load
  function renderAll(d) {
    var ready = d.state === 'ready';
    renderHead(d);
    renderFilters(d);
    renderSetup(d);
    $('#chMain').hidden = !ready;
    $('#chKpis').hidden = !ready;
    if (ready) {
      renderKpis(d);
      renderChart(d.panels.chart, d);
      renderTable(d.panels.table);
      renderCampaigns(d.panels.campaigns, d);
    } else { S.sig.kpis = null; S.sig.chart = null; S.sig.table = null; S.sig.camp = null; purge(); }
    renderDefs(d);
    renderFast(d);
    syncButtons();
    if (D.channelsInsights) D.channelsInsights.update(d);
    if (D.channelsPlacements) D.channelsPlacements.update(d);
    var P = d.panels || {};
    var bad = Object.keys(P).filter(function (k) { return P[k].state === 'unavailable'; });
    $('#chFoot').textContent = 'erp_support · PostgreSQL · read ' + fmtTime(d.generated_at) + ' · read-only' + (d.time_zone ? ' · dates as reported, database time zone ' + d.time_zone : '') +
      (d.source_table ? ' · read ' + d.source_table : '') + (bad.length ? ' · ' + bad.length + ' panel(s) unavailable' : '');
    $$('.ld-panel').forEach(function (el) { el.classList.remove('busy'); });
  }
  function showFailure(msg, offline) {
    setLive(offline ? 'offline' : 'stale');
    if (S.data) { tickAgo(); return; }
    $('#chTitle').textContent = 'The channel report could not be read';
    $('#chTitle').setAttribute('data-tone', 'red');
    $('#chSub').textContent = msg;
    $('#chMain').hidden = true; $('#chKpis').hidden = true;
    html($('#chSetup'), '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>' + esc(msg) + ' This page keeps trying by itself.</span></div>');
    $('#chSetup').hidden = false;
  }
  function load(fresh) {
    if (S.busy) { S.again = true; return S.inflight || Promise.resolve(); }
    S.busy = true; S.again = false;
    clearTimeout(S.timer);
    setLive('syncing');
    $$('.ld-panel').forEach(function (el) { el.classList.add('busy'); });
    S.inflight = fetchChannels(fresh).then(function (x) {
      var d = x.body;
      if (x.status === 400 && d && d.problems) {                       // a stale filter value: drop exactly that filter and say so
        d.problems.forEach(function (p) {
          if (p.param === 'channel') S.f.channel = S.f.channel.filter(function (v) { return v !== p.value && v.slice(0, 57) + '...' !== p.value; });
          else if (p.param === 'focus') S.f.focus = null;
          else if (p.param === 'campaign') S.f.campaign = null;
          else if (p.param === 'from' || p.param === 'to') { S.f.from = null; S.f.to = null; }
          else if (p.param === 'sort' || p.param === 'dir') { S.sort = null; S.dir = null; }
        });
        D.toast('A filter was not valid and was removed: ' + d.error, 'warn', 5200);
        S.sig.filters = null;
        if (++S.badLoops <= 3) S.again = true; else showFailure(d.error, false);
        return;
      }
      S.badLoops = 0;
      if (x.status !== 200 || !d || !d.state) {
        var msg = (d && d.error) || ('The Channels feed answered ' + x.status);
        S.err = msg; S.fails++;
        showFailure(msg, false);
        if (fresh) D.toast(msg, 'bad', 5200);
        return;
      }
      var gen = Date.parse(d.generated_at);
      S.data = d; S.fails = 0; S.err = '';
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
  function active() { return D.view() === 'channels'; }
  function play() { if (S.played) return; S.played = true; $('#ch').classList.add('play'); }

  $('#chRefresh').addEventListener('click', function () {
    var b = this; b.classList.add('spinning'); b.disabled = true;
    if (D.channelsInsights) D.channelsInsights.invalidate();
    if (D.channelsPlacements) D.channelsPlacements.invalidate();
    load(true).then(function () { setTimeout(function () { b.classList.remove('spinning'); b.disabled = false; }, 400); });
  });
  D.on('refresh-request', function () { if (active()) $('#chRefresh').click(); });
  D.on('ready', play);
  D.on('view', function (name) {
    if (name !== 'channels') return;
    play();
    if (!S.data || Date.now() - S.at > 5000) load(false);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && active() && Date.now() - S.at > POLL_MS) load(false); });
  setInterval(tickAgo, 1000);
  setTimeout(play, 6000);                                            // safety net: never leave the page invisible if the splash event was missed
  D.channels = {
    last: function () { return S.data; },
    download: function (url, btn, fallback, what) { downloadFrom(url, btn, fallback, what); },      // the Placements panel's CSV goes through the same guarded download
    useAllData: function () { var o = S.data && S.data.options; if (!o || !o.last_day) return; S.f.from = o.first_day; S.f.to = o.last_day; changed(); }
  };
  setLive('connecting');
  if (active()) load(false);                                         // L-080: opened straight on #channels - this script missed the 'view' event
})();
