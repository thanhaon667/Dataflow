/* ERP Desk - Social page: social listening for a delivery brand (mentions, sentiment, topics, channels, hours, anomalies, data quality).
   Draws the JSON of GET /api/social (desktop/social_data.py). The server sends cubes (counts by day x brand x channel x sentiment,
   by day x brand x topic x sentiment, by brand x weekday x hour, plus small lists); every filter (date range, channel, brand focus,
   week/day) is applied HERE, so one request serves all of them. Charts are plain SVG. All server text goes through esc().
   Read-only: the page calls no state-changing endpoint. It loads when first shown and on Refresh (R), never on a timer. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var esc = D.esc;
  var root = document.getElementById('soc');
  if (!root) return;

  var loadedAt = Date.now(), DATA = null, S = { days: 90, brand: 0, plat: 'all', gran: 'week' }, built = false, busy = false, rt = null;
  var PLN = { facebook: 'Facebook', tiktok: 'TikTok', youtube: 'YouTube', news: 'News', forum: 'Forums', ecommerce: 'E-commerce' };
  var DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  var SC = { negative: 'var(--red)', neutral: '#cfc9bb', positive: 'var(--green)' }, SN = { negative: 'Negative', neutral: 'Neutral', positive: 'Positive' };
  var $ = function (id) { return document.getElementById('s-' + id); };
  var nf = function (n) { return Math.round(n).toLocaleString('en-US'); };
  var pf = function (n, d) { d = d == null ? 1 : d; return Number(n).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); };
  var BRAND_VARS = ['--blue', '--coral', '--green', '--violet'];
  var BC = function (i) { return 'var(' + BRAND_VARS[i % 4] + ')'; };
  var NEG = 'var(--red)', NEU = '#cfc9bb', POS = 'var(--green)';
  var pname = function (p) { return PLN[p] || p; };

  // ---- shared tooltip (one element, fixed position) ----
  var tip = document.createElement('div'); tip.className = 'soc-tip'; document.body.appendChild(tip);
  function showTip(e, html) {
    tip.innerHTML = html; tip.style.display = 'block';
    var r = tip.getBoundingClientRect(), x = e.clientX + 14, y = e.clientY + 14;
    if (x + r.width > innerWidth - 8) x = e.clientX - r.width - 14;
    if (y + r.height > innerHeight - 8) y = e.clientY - r.height - 14;
    tip.style.left = Math.max(8, x) + 'px'; tip.style.top = Math.max(8, y) + 'px';
  }
  function hideTip() { tip.style.display = 'none'; }

  // ---- dates: the payload gives d0 (ISO day) and day indexes ----
  var D0 = null, NDAY = 0;
  function dayDate(i) { return new Date(D0.getTime() + i * 864e5); }
  function dshort(i) { var d = dayDate(i); return d.getUTCDate() + ' ' + MON[d.getUTCMonth()]; }
  function dfull(i) { return dshort(i) + ' ' + dayDate(i).getUTCFullYear(); }
  function range() { var hi = NDAY - 1, lo = S.days ? Math.max(0, NDAY - S.days) : 0; return [lo, hi]; }
  function platOK(p) { return S.plat === 'all' || DATA.plats[p] === S.plat; }

  // ---- aggregation over the cube: [day, brand, plat, sentiment, n, engagement, views] ----
  function agg(lo, hi, brand) {
    var r = { n: 0, eng: 0, negative: 0, neutral: 0, positive: 0 };
    DATA.cube.forEach(function (a) {
      if (a[0] < lo || a[0] > hi || !platOK(a[2]) || (brand != null && a[1] !== brand)) return;
      r.n += a[4]; r.eng += a[5]; r[DATA.sents[a[3]]] += a[4];
    });
    return r;
  }
  function netOf(r) { return r.n ? 100 * (r.positive - r.negative) / r.n : 0; }
  function series(lo, hi) {
    var W = S.gran === 'week' ? 7 : 1, k = Math.floor((hi - lo) / W) + 1;
    var out = DATA.brands.map(function () { return Array.apply(null, Array(k)).map(function () { return { n: 0, pos: 0, neg: 0 }; }); });
    DATA.cube.forEach(function (a) {
      if (a[0] < lo || a[0] > hi || !platOK(a[2])) return;
      var c = out[a[1]][Math.floor((a[0] - lo) / W)]; c.n += a[4]; if (a[3] === 2) c.pos += a[4]; if (a[3] === 0) c.neg += a[4];
    });
    return { W: W, k: k, out: out, idx: function (d) { return Math.floor((d - lo) / W); }, label: function (b) { return dshort(lo + b * W); } };
  }

  // ---- chart kit (plain SVG) ----
  function nice(a, b) {
    var st = Math.pow(10, Math.floor(Math.log10((b - a) / 4 || 1))), f = [1, 2, 2.5, 5, 10].filter(function (x) { return (b - a) / 4 <= x * st; })[0] * st;
    return [Math.floor(a / f) * f, Math.ceil(b / f) * f, f];
  }
  function lineChart(el, labels, ser, o) {
    o = o || {};
    var W = el.clientWidth || 600, H = o.h || 250, m = { l: 44, r: 14, t: 10, b: o.events && o.events.length ? 34 : 24 }, iw = W - m.l - m.r, ih = H - m.t - m.b;
    var all = [].concat.apply([], ser.map(function (s) { return s.v; }));
    var lo = o.min != null ? Math.min(o.min, Math.min.apply(null, all)) : 0, hi = Math.max.apply(null, all.concat(o.max != null ? [o.max] : []));
    var ny = nice(lo, hi), y0 = ny[0], y1 = ny[1], st = ny[2];
    var X = function (i) { return m.l + (labels.length > 1 ? i * iw / (labels.length - 1) : iw / 2); }, Y = function (v) { return m.t + ih * (1 - (v - y0) / ((y1 - y0) || 1)); };
    var g = '', v, i;
    for (v = y0; v <= y1 + 1e-9; v += st) g += '<line x1="' + m.l + '" x2="' + (W - m.r) + '" y1="' + Y(v) + '" y2="' + Y(v) + '" stroke="' + (v === 0 && y0 < 0 ? '#cfc9bb' : 'var(--grid)') + '"/><text x="' + (m.l - 6) + '" y="' + (Y(v) + 4) + '" text-anchor="end">' + (o.yf ? o.yf(v) : nf(v)) + '</text>';
    var step = Math.ceil(labels.length / Math.max(2, Math.floor(iw / 62)));
    for (i = 0; i < labels.length; i += step) g += '<text x="' + X(i) + '" y="' + (H - (o.events && o.events.length ? 18 : 6)) + '" text-anchor="middle">' + esc(labels[i]) + '</text>';
    ser.forEach(function (s) {
      g += '<polyline fill="none" stroke="' + s.c + '" stroke-width="' + (s.w || 2) + '" stroke-linejoin="round" points="' + s.v.map(function (val, k) { return X(k).toFixed(1) + ',' + Y(val).toFixed(1); }).join(' ') + '"/>';
      var e = s.v.length - 1; g += '<circle cx="' + X(e) + '" cy="' + Y(s.v[e]) + '" r="4" fill="' + s.c + '" stroke="var(--surface)" stroke-width="2"/>';
    });
    (o.events || []).forEach(function (ev) {
      g += '<line x1="' + X(ev.i) + '" x2="' + X(ev.i) + '" y1="' + m.t + '" y2="' + (m.t + ih) + '" stroke="#cfc9bb" stroke-dasharray="2 3"/><circle cx="' + X(ev.i) + '" cy="' + (H - 6) + '" r="8" fill="var(--ink)"/><text x="' + X(ev.i) + '" y="' + (H - 2.5) + '" text-anchor="middle" style="fill:#fff;font-weight:600">' + ev.n + '</text>';
    });
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '" role="img" aria-label="' + esc(o.aria || '') + '">' + g + '<line class="xh" y1="' + m.t + '" y2="' + (m.t + ih) + '" stroke="#cfc9bb" style="display:none"/><g class="dots"></g><rect x="' + m.l + '" y="' + m.t + '" width="' + iw + '" height="' + ih + '" fill="transparent" style="touch-action:pan-y"/></svg>';
    var svg = el.firstChild, xh = svg.querySelector('.xh'), dots = svg.querySelector('.dots'), rect = svg.querySelector('rect');
    rect.addEventListener('pointermove', function (e) {
      var bb = rect.getBoundingClientRect(), f = (e.clientX - bb.left) / bb.width, k = Math.max(0, Math.min(labels.length - 1, Math.round(f * (labels.length - 1))));
      xh.setAttribute('x1', X(k)); xh.setAttribute('x2', X(k)); xh.style.display = '';
      dots.innerHTML = ser.map(function (s) { return '<circle cx="' + X(k) + '" cy="' + Y(s.v[k]) + '" r="5" fill="' + s.c + '" stroke="var(--surface)" stroke-width="2"/>'; }).join('');
      var rows = ser.slice().sort(function (a, b) { return b.v[k] - a.v[k]; }).map(function (s) { return '<div class="r"><span><i style="background:' + s.c + '"></i>' + esc(s.name) + '</span><b>' + (o.tf ? o.tf(s.v[k]) : nf(s.v[k])) + '</b></div>'; }).join('');
      showTip(e, '<b>' + esc(o.tl ? o.tl(k) : labels[k]) + '</b>' + rows);
    });
    rect.addEventListener('pointerleave', function () { xh.style.display = 'none'; dots.innerHTML = ''; hideTip(); });
  }
  function stackBars(el, rows, o) {
    o = o || {};
    var W = el.clientWidth || 400, rh = o.rh || 30, lw = o.lw || 96, H = rows.length * rh + 4, iw = Math.max(40, W - lw - (o.rm || 44)), g = '';
    rows.forEach(function (r, ri) {
      var tot = r.parts.reduce(function (a, p) { return a + p.v; }, 0) || 1, scale = o.pct ? iw / tot : iw / (o.max || tot), x = lw, y = ri * rh + 3;
      g += '<text x="' + (lw - 8) + '" y="' + (y + rh / 2 - 1) + '" text-anchor="end" style="fill:var(--ink-dim)">' + esc(r.label) + '</text>';
      r.parts.forEach(function (p, pi) {
        var w = Math.max(0, p.v * scale); if (w <= 0) return;
        var gap = pi ? 2 : 0;
        g += '<rect data-r="' + ri + '" data-p="' + pi + '" x="' + (x + gap) + '" y="' + y + '" width="' + Math.max(0, w - gap) + '" height="' + (rh - 8) + '" rx="2" fill="' + p.c + '"/>';
        if (o.pct && w > 34) g += '<text x="' + (x + gap + (w - gap) / 2) + '" y="' + (y + rh / 2 - 1) + '" text-anchor="middle" style="fill:#fff;font-weight:600">' + Math.round(100 * p.v / tot) + '%</text>';
        x += w;
      });
      g += '<text class="val" x="' + (x + 6) + '" y="' + (y + rh / 2 - 1) + '">' + esc(o.tot ? o.tot(r, tot) : nf(tot)) + '</text>';
    });
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '" role="img" aria-label="' + esc(o.aria || '') + '">' + g + '</svg>';
    Array.prototype.forEach.call(el.querySelectorAll('rect'), function (rc) {
      var r = rows[+rc.getAttribute('data-r')], p = r.parts[+rc.getAttribute('data-p')], tot = r.parts.reduce(function (a, q) { return a + q.v; }, 0) || 1;
      rc.addEventListener('pointermove', function (e) { showTip(e, '<b>' + esc(r.label) + '</b><div class="r"><span><i style="background:' + p.c + '"></i>' + esc(p.name) + '</span><b>' + nf(p.v) + ' &middot; ' + pf(100 * p.v / tot, 0) + '%</b></div>'); });
      rc.addEventListener('pointerleave', hideTip);
    });
  }
  function heat(el, rl, cl, cells, o) {
    var W = el.clientWidth || 500, lw = o.lw || 70, cw = (W - lw) / cl.length, ch = o.ch || 26, H = rl.length * ch + 22, g = '';
    var vals = [].concat.apply([], cells).filter(function (v) { return v != null; }), mx = Math.max.apply(null, vals.concat([0])), mn = o.zero ? 0 : Math.min.apply(null, vals);
    cl.forEach(function (c, ci) { if (!o.skipx || ci % o.skipx === 0) g += '<text x="' + (lw + ci * cw + cw / 2) + '" y="10" text-anchor="middle">' + esc(c) + '</text>'; });
    rl.forEach(function (r, ri) {
      g += '<text x="' + (lw - 8) + '" y="' + (22 + ri * ch + ch / 2 + 1) + '" text-anchor="end" style="fill:var(--ink-dim)">' + esc(r) + '</text>';
      cl.forEach(function (c, ci) {
        var v = cells[ri][ci];
        if (v == null) { g += '<rect x="' + (lw + ci * cw + 1) + '" y="' + (18 + ri * ch + 1) + '" width="' + (cw - 2) + '" height="' + (ch - 2) + '" rx="2" fill="var(--grid)"/>'; return; }
        var t = (v - mn) / ((mx - mn) || 1), op = .08 + .92 * t;
        g += '<rect data-r="' + ri + '" data-c="' + ci + '" x="' + (lw + ci * cw + 1) + '" y="' + (18 + ri * ch + 1) + '" width="' + (cw - 2) + '" height="' + (ch - 2) + '" rx="2" fill="var(--blue)" fill-opacity="' + op.toFixed(2) + '"/>';
        if (o.text && cw > 34) g += '<text x="' + (lw + ci * cw + cw / 2) + '" y="' + (18 + ri * ch + ch / 2 + 4) + '" text-anchor="middle" style="fill:' + (op > .55 ? '#fff' : 'var(--ink)') + ';font-weight:600">' + o.text(v) + '</text>';
      });
    });
    el.innerHTML = '<svg viewBox="0 0 ' + W + ' ' + H + '" height="' + H + '" role="img" aria-label="' + esc(o.aria || '') + '">' + g + '</svg>';
    Array.prototype.forEach.call(el.querySelectorAll('rect[data-r]'), function (rc) {
      var ri = +rc.getAttribute('data-r'), ci = +rc.getAttribute('data-c');
      rc.addEventListener('pointermove', function (e) { showTip(e, '<b>' + esc(rl[ri]) + ' &middot; ' + esc(cl[ci]) + '</b><div class="r"><span>' + esc(o.name) + '</span><b>' + o.tt(cells[ri][ci], ri, ci) + '</b></div>'); });
      rc.addEventListener('pointerleave', hideTip);
    });
  }
  function table(h, rows, num) {
    num = num || [];
    return '<div class="w"><table class="soc-tbl"><thead><tr>' + h.map(function (x, i) { return '<th class="' + (num.indexOf(i) >= 0 ? 'n' : '') + '">' + esc(x) + '</th>'; }).join('') + '</tr></thead><tbody>' +
      rows.map(function (r) { return '<tr>' + r.map(function (x, i) { return '<td class="' + (num.indexOf(i) >= 0 ? 'n' : '') + '">' + x + '</td>'; }).join('') + '</tr>'; }).join('') + '</tbody></table></div>';
  }
  function legend(items) { return '<div class="ch-legend">' + items.map(function (it) { return '<span class="ch-lg" style="--c:' + it[1] + '"><i></i>' + esc(it[0]) + '</span>'; }).join('') + '</div>'; }
  var SENT_LEGEND = [['Negative', NEG], ['Neutral', NEU], ['Positive', POS]];

  // ---- page skeleton (built once, after the first successful load) ----
  function panel(span, title, hint, body) { return '<article class="ld-panel span-' + span + '"><header><h3>' + title + '</h3>' + (hint ? '<span class="hint">' + hint + '</span>' : '') + '</header>' + body + '</article>'; }
  function skeleton() {
    var b = DATA.brands[0];
    root.innerHTML =
      '<header class="ld-head"><div class="ld-head-text"><div class="eyebrow">SOCIAL &middot; LISTENING</div>' +
      '<h1 class="ld-title">What people say about ' + esc(b) + '.</h1>' +
      '<p class="ld-sub">Mentions, sentiment, topics and channels for <b>' + esc(b) + '</b> against ' + (DATA.brands.length - 1) + ' competitors, read from the cleaned posts in the <code>sl</code> schema. Spam is excluded.</p></div>' +
      '<div class="live-controls"><div class="live-pill" id="s-pill" data-state="live"><span class="live-dot"></span><b>LOADED</b><span class="live-ago" id="s-ago"></span></div>' +
      '<button class="btn" id="s-refresh" type="button" title="Read the data again"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 12a8 8 0 1 1-2.6-5.9M20 4v4.5h-4.5"/></svg>Refresh</button></div></header>' +
      '<div class="ld-caveat" id="s-note"><span class="cv-ico"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 7v6M12 17v.2"/></svg></span><div id="s-notetxt"></div></div>' +
      '<div class="ld-filters" id="s-filters" role="group" aria-label="Filters">' +
      '<div class="fgroup"><span class="fgroup-label">RANGE</span><div class="seg" id="s-fRange"></div></div>' +
      '<div class="fgroup"><span class="fgroup-label">FOCUS BRAND</span><label class="ld-select"><select id="s-fBrand" aria-label="Focus brand"></select></label></div>' +
      '<div class="fgroup"><span class="fgroup-label">CHANNEL</span><label class="ld-select"><select id="s-fPlat" aria-label="Channel"></select></label></div>' +
      '<div class="fgroup"><span class="fgroup-label">GROUP BY</span><div class="seg" id="s-fGran"></div></div>' +
      '<span class="hint" id="s-rangeTxt" style="margin-left:auto"></span></div>' +
      '<div class="ld-kpis" id="s-kpis"></div>' +
      '<div class="ld-grid">' +
      panel(7, 'Mentions by brand', 'non-spam posts; numbered circles are events', '<div id="s-lgBrand"></div><div id="s-cVol"></div><div class="soc-events" id="s-evList"></div><div id="s-tVol"></div>') +
      panel(5, 'Share of voice', 'by month', '<p class="ld-note" style="margin:0 0 8px">Each brand\'s share of all mentions in the group.</p><div id="s-cSov"></div>') +
      panel(7, 'Net sentiment over time', '% positive − % negative', '<div id="s-lgBrand2"></div><div id="s-cNet"></div><p class="ld-note">Above 0 means more praise than complaints.</p>') +
      panel(5, 'Sentiment mix', 'per brand, in range', '<div id="s-cSent"></div>') +
      panel(12, 'Days with unusually high mentions', 'at least 2 standard deviations above the range average', '<div id="s-alerts"></div>') +
      panel(6, 'Topics for the focus brand', 'a post can have several topics', '<div id="s-cTopic"></div><div id="s-tTopic"></div>') +
      panel(6, 'Negative share by topic and brand', 'cells with at least 20 posts', '<div id="s-cHeatT"></div><p class="ld-note">Darker cells mean a higher share of negative posts.</p>') +
      panel(5, 'Channels for the focus brand', 'by sentiment', '<div id="s-cPlat"></div>') +
      panel(7, 'When people post', 'weekday and hour, Vietnam time', '<div id="s-cHour"></div>') +
      panel(6, 'Top posts by engagement', 'whole data set, all brands', '<div class="w" id="s-tTop"></div><p class="ld-note">Engagement = likes + comments + shares.</p>') +
      panel(6, 'Accounts talking most about ' + esc(b), 'whole data set, at least 5 posts', '<div class="w" id="s-tAuth"></div>') +
      panel(7, 'From raw export to analysis set', 'rows removed at each cleaning step', '<div id="s-cFunnel"></div><p class="ld-note">The first place to look when numbers seem low.</p>') +
      panel(5, 'Keywords that pulled posts in', 'a post can be found by several', '<div id="s-cKw"></div>') +
      '</div>';
    seg('fRange', [[30, '30 days'], [90, '90 days'], [0, 'All']], function () { return S.days; }, function (v) { S.days = +v; });
    seg('fGran', [['week', 'Week'], ['day', 'Day']], function () { return S.gran; }, function (v) { S.gran = v; });
    $('fBrand').innerHTML = DATA.brands.map(function (x, i) { return '<option value="' + i + '">' + esc(x) + '</option>'; }).join('');
    $('fBrand').onchange = function (e) { S.brand = +e.target.value; render(); };
    $('fPlat').innerHTML = '<option value="all">All channels</option>' + DATA.plats.map(function (p) { return '<option value="' + esc(p) + '">' + esc(pname(p)) + '</option>'; }).join('');
    $('fPlat').onchange = function (e) { S.plat = e.target.value; render(); };
    $('refresh').onclick = function () { load(true); };
    built = true;
  }
  function seg(id, opts, get, set) {
    var el = $(id);
    el.innerHTML = opts.map(function (o) { return '<button type="button" data-v="' + o[0] + '" class="' + (get() == o[0] ? 'on' : '') + '" aria-pressed="' + String(get() == o[0]) + '">' + o[1] + '</button>'; }).join('');
    el.onclick = function (e) {
      var b = e.target.closest('button'); if (!b) return;
      set(b.getAttribute('data-v'));
      Array.prototype.forEach.call(el.querySelectorAll('button'), function (x) { x.classList.toggle('on', x === b); x.setAttribute('aria-pressed', String(x === b)); });
      render();
    };
  }

  // ---- render ----
  function render() {
    if (!DATA || !built) return;
    hideTip();
    var r = range(), lo = r[0], hi = r[1], len = hi - lo + 1, pl = Math.max(0, lo - len), ph = lo - 1, fb = S.brand, hasPrev = lo - len >= 0, B = DATA.brands;
    $('rangeTxt').textContent = dfull(lo) + ' – ' + dfull(hi) + ' · ' + len + ' days' + (S.plat === 'all' ? '' : ' · ' + pname(S.plat));
    $('notetxt').innerHTML = '<b>Check where this came from.</b> If the data was made by <code>db/sample/sl_generate_sample.py</code>, every brand, number and event on this page is invented.';
    $('ago').textContent = 'read ' + new Date(loadedAt).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    // KPIs
    var cur = agg(lo, hi, fb), prv = hasPrev ? agg(pl, ph, fb) : null, all = agg(lo, hi), allp = hasPrev ? agg(pl, ph) : null;
    var dl = function (c, p, inv) {
      if (p == null || !p) return '<span class="t-delta">no previous period</span>';
      var x = 100 * (c - p) / p, good = inv ? x < 0 : x > 0;
      return '<span class="t-delta ' + (Math.abs(x) < 1 ? '' : good ? 'good' : 'bad') + '">' + (x > 0 ? '▲' : x < 0 ? '▼' : '■') + ' ' + pf(Math.abs(x), 0) + '% vs previous ' + len + ' days</span>';
    };
    var dp = function (c, p) {
      if (p == null) return '<span class="t-delta">no previous period</span>';
      var x = c - p;
      return '<span class="t-delta ' + (Math.abs(x) < .5 ? '' : x > 0 ? 'good' : 'bad') + '">' + (x > 0 ? '▲' : x < 0 ? '▼' : '■') + ' ' + pf(Math.abs(x), 1) + ' pts vs previous period</span>';
    };
    var sov = all.n ? 100 * cur.n / all.n : 0, sovp = allp && allp.n ? 100 * prv.n / allp.n : null;
    $('kpis').innerHTML = [
      ['Mentions · ' + esc(B[fb]), nf(cur.n), dl(cur.n, prv && prv.n)], ['Engagement', nf(cur.eng), dl(cur.eng, prv && prv.eng)],
      ['Net sentiment', (netOf(cur) > 0 ? '+' : '') + pf(netOf(cur)), dp(netOf(cur), prv && netOf(prv))],
      ['Negative posts', nf(cur.negative), dl(cur.negative, prv && prv.negative, true)], ['Share of voice', pf(sov) + '%', dp(sov, sovp)]
    ].map(function (k, i) { return '<div class="kpi" style="--kc:var(' + ['--blue', '--violet', '--coral', '--red', '--green'][i] + ')"><div class="kpi-label">' + k[0] + '</div><div class="kpi-value">' + k[1] + '</div><div class="kpi-cap">' + k[2] + '</div></div>'; }).join('');
    // volume and net sentiment
    var lg = legend(B.map(function (x, i) { return [x, BC(i)]; })); $('lgBrand').innerHTML = lg; $('lgBrand2').innerHTML = lg;
    var se = series(lo, hi), labels = []; for (var b = 0; b < se.k; b++) labels.push(se.label(b));
    var tl = function (i) { return S.gran === 'week' ? 'Week of ' + dfull(lo + i * 7) : dfull(lo + i); };
    var evs = DATA.events.filter(function (e) { return e[0] >= lo && e[0] <= hi; }).map(function (e, i) { return { n: i + 1, d: e[0], t: e[1], i: se.idx(e[0]) }; });
    lineChart($('cVol'), labels, B.map(function (x, i) { return { name: x, c: BC(i), v: se.out[i].map(function (c) { return c.n; }), w: i === fb ? 3 : 1.8 }; }), { events: evs, tl: tl, aria: 'Mentions per ' + S.gran + ' for each brand' });
    $('evList').innerHTML = evs.map(function (e) { return '<span><b>' + e.n + '</b>' + dfull(e.d) + ' ' + esc(e.t) + '</span>'; }).join('');
    $('tVol').innerHTML = '<details><summary>View as table</summary>' + table(['Period'].concat(B), labels.map(function (l, k) { return [esc(tl(k))].concat(B.map(function (_, i) { return nf(se.out[i][k].n); })); }), B.map(function (_, i) { return i + 1; })) + '</details>';
    lineChart($('cNet'), labels, B.map(function (x, i) { return { name: x, c: BC(i), v: se.out[i].map(function (c) { return c.n ? 100 * (c.pos - c.neg) / c.n : 0; }), w: i === fb ? 3 : 1.8 }; }), { tl: tl, min: -60, max: 20, yf: function (v) { return v + ''; }, tf: function (v) { return (v > 0 ? '+' : '') + pf(v); }, aria: 'Net sentiment over time' });
    // share of voice by month
    var mo = {};
    DATA.cube.forEach(function (a) {
      if (a[0] < lo || a[0] > hi || !platOK(a[2])) return;
      var d = dayDate(a[0]), k = d.getUTCFullYear() * 100 + d.getUTCMonth(); (mo[k] = mo[k] || B.map(function () { return 0; }))[a[1]] += a[4];
    });
    stackBars($('cSov'), Object.keys(mo).sort().map(function (k) { return { label: MON[+k % 100] + ' ' + String(Math.floor(k / 100)).slice(2), parts: B.map(function (x, i) { return { v: mo[k][i], c: BC(i), name: x }; }) }; }), { pct: true, lw: 56, aria: 'Share of voice by month' });
    $('cSent').innerHTML = legend(SENT_LEGEND) + '<div id="s-cSentBars"></div>';
    stackBars($('cSentBars'), B.map(function (x, i) { var q = agg(lo, hi, i); return { label: x, parts: [{ v: q.negative, c: NEG, name: 'Negative' }, { v: q.neutral, c: NEU, name: 'Neutral' }, { v: q.positive, c: POS, name: 'Positive' }] }; }), { pct: true, lw: 100, rh: 38, tot: function () { return ''; }, aria: 'Sentiment mix per brand' });
    // anomalies
    var day = Array.apply(null, Array(NDAY)).map(function () { return 0; });
    DATA.cube.forEach(function (a) { if (a[1] === fb && a[0] >= lo && a[0] <= hi && platOK(a[2])) day[a[0]] += a[4]; });
    var dv = day.slice(lo, hi + 1), mean = dv.reduce(function (a, c) { return a + c; }, 0) / dv.length, sd = Math.sqrt(dv.reduce(function (a, c) { return a + (c - mean) * (c - mean); }, 0) / dv.length), hot = [];
    for (var d = lo; d <= hi; d++) { var z = (day[d] - mean) / (sd || 1); if (z >= 2) hot.push({ d: d, n: day[d], z: z }); }
    var tn = {}; DATA.by_topic.forEach(function (x) { if (x[1] === fb && x[3] === 0 && x[0] >= lo && x[0] <= hi) tn[x[0] * 100 + x[2]] = (tn[x[0] * 100 + x[2]] || 0) + x[4]; });
    var topTopic = function (dd) { var best = null; DATA.topics.forEach(function (t, ti) { var v = tn[dd * 100 + ti] || 0; if (!best || v > best[1]) best = [t[1], v]; }); return best; };
    hot.sort(function (a, c) { return c.z - a.z; });
    $('alerts').innerHTML = hot.length ? hot.slice(0, 8).map(function (h) {
      var t = topTopic(h.d);
      return '<div class="alert"><b>' + dfull(h.d) + '</b><span>' + nf(h.n) + ' mentions, ' + pf(h.n / mean) + '× the average of ' + nf(mean) + ' a day <span class="soc-dim">(z = ' + pf(h.z) + ')</span></span><span class="soc-tag" style="color:var(--red)">' + (t && t[1] ? esc(t[0]) + ' · ' + nf(t[1]) + ' negative' : 'no clear topic') + '</span></div>';
    }).join('') : '<p class="ld-note">No day is above the threshold in this range.</p>';
    // topics
    var tc = DATA.topics.map(function () { return [0, 0, 0]; });
    DATA.by_topic.forEach(function (x) { if (x[1] === fb && x[0] >= lo && x[0] <= hi) tc[x[2]][x[3]] += x[4]; });
    var order = DATA.topics.map(function (_, i) { return i; }).sort(function (a, c) { return tc[c][0] + tc[c][1] + tc[c][2] - tc[a][0] - tc[a][1] - tc[a][2]; });
    var tmax = Math.max.apply(null, tc.map(function (q) { return q[0] + q[1] + q[2]; }).concat([1]));
    $('cTopic').innerHTML = legend([['Negative', NEG], ['Neutral and positive', NEU]]) + '<div id="s-cTopicBars"></div>';
    stackBars($('cTopicBars'), order.map(function (i) { return { label: DATA.topics[i][1], parts: [{ v: tc[i][0], c: NEG, name: 'Negative' }, { v: tc[i][1] + tc[i][2], c: NEU, name: 'Neutral and positive' }] }; }), { lw: 140, max: tmax, aria: 'Topics for ' + B[fb] });
    $('tTopic').innerHTML = '<details><summary>View as table</summary>' + table(['Topic', 'Negative', 'Neutral', 'Positive', '% negative'], order.map(function (i) { var t = tc[i][0] + tc[i][1] + tc[i][2]; return [esc(DATA.topics[i][1]), nf(tc[i][0]), nf(tc[i][1]), nf(tc[i][2]), t ? pf(100 * tc[i][0] / t, 0) + '%' : '—']; }), [1, 2, 3, 4]) + '</details>';
    var hm = DATA.topics.map(function () { return B.map(function () { return [0, 0]; }); });
    DATA.by_topic.forEach(function (x) { if (x[0] < lo || x[0] > hi) return; var c = hm[x[2]][x[1]]; c[1] += x[4]; if (x[3] === 0) c[0] += x[4]; });
    heat($('cHeatT'), DATA.topics.map(function (t) { return t[1]; }), B, hm.map(function (row) { return row.map(function (c) { return c[1] >= 20 ? 100 * c[0] / c[1] : null; }); }),
      { lw: 140, ch: 30, zero: true, text: function (v) { return Math.round(v) + '%'; }, name: '% negative', aria: 'Negative share by topic and brand', tt: function (v, ri, ci) { return Math.round(v) + '% (' + nf(hm[ri][ci][0]) + ' of ' + nf(hm[ri][ci][1]) + ' posts)'; } });
    // channels
    var pc = DATA.plats.map(function () { return [0, 0, 0]; });
    DATA.cube.forEach(function (a) { if (a[1] === fb && a[0] >= lo && a[0] <= hi) pc[a[2]][a[3]] += a[4]; });
    var po = DATA.plats.map(function (_, i) { return i; }).sort(function (a, c) { return pc[c][0] + pc[c][1] + pc[c][2] - pc[a][0] - pc[a][1] - pc[a][2]; });
    $('cPlat').innerHTML = legend(SENT_LEGEND) + '<div id="s-cPlatBars"></div>';
    stackBars($('cPlatBars'), po.map(function (i) { return { label: pname(DATA.plats[i]), parts: [{ v: pc[i][0], c: NEG, name: 'Negative' }, { v: pc[i][1], c: NEU, name: 'Neutral' }, { v: pc[i][2], c: POS, name: 'Positive' }] }; }), { lw: 90, rh: 34, max: Math.max.apply(null, pc.map(function (q) { return q[0] + q[1] + q[2]; }).concat([1])), aria: 'Channels for ' + B[fb] });
    var hc = DAYS.map(function () { return Array.apply(null, Array(24)).map(function () { return 0; }); });
    DATA.hours.forEach(function (h) { if (h[0] === fb) hc[h[1]][h[2]] += h[3]; });
    heat($('cHour'), DAYS, Array.apply(null, Array(24)).map(function (_, h) { return h; }), hc, { lw: 40, ch: 28, zero: true, name: 'Posts', skipx: 2, aria: 'Posting hours by weekday', tt: function (v) { return nf(v) + ' posts'; } });
    $('tTop').innerHTML = table(['Date', 'Channel', 'Brand', 'Sentiment', 'Engagement', 'Post'], DATA.top.slice(0, 10).map(function (t) {
      var p = t[0].split('-');
      return [(+p[2]) + ' ' + MON[+p[1] - 1], esc(pname(t[1])), esc(t[2]), '<span class="soc-tag" style="color:' + (SC[t[3]] || '#666') + '">' + (SN[t[3]] || esc(t[3])) + '</span>', nf(t[4]), esc(t[6])];
    }), [4]);
    $('tAuth').innerHTML = table(['Account', 'Posts', 'Engagement', '% negative'], DATA.authors.map(function (a) { return [esc(a[0]), nf(a[1]), nf(a[2]), a[3] + '%']; }), [1, 2, 3]);
    // data quality
    var q = DATA.quality, used = DATA.cube.reduce(function (s, a) { return s + a[4]; }, 0);
    var steps = [['Raw export', q.rows_in, 'var(--blue)'], ['Minus bad rows (date, too short)', q.rows_in - q.rows_bad, 'var(--blue)'], ['Minus duplicates', q.rows_kept, 'var(--blue)'], ['Minus spam', q.rows_kept - q.rows_spam, 'var(--blue)'], ['Used for analysis', used, 'var(--green)']];
    stackBars($('cFunnel'), steps.map(function (s) { return { label: s[0], parts: [{ v: s[1], c: s[2], name: s[0] }] }; }), { lw: 200, rm: 120, rh: 34, max: Math.max(q.rows_in, 1), tot: function (rr, t) { return nf(t) + ' (' + pf(100 * t / Math.max(q.rows_in, 1), 0) + '%)'; }, aria: 'Cleaning funnel' });
    stackBars($('cKw'), DATA.keywords.map(function (k) { return { label: k[0], parts: [{ v: k[1], c: 'var(--blue)', name: 'Posts' }] }; }), { lw: 140, rh: 28, max: DATA.keywords.length ? DATA.keywords[0][1] : 1, aria: 'Keywords' });
  }

  // ---- load / states ----
  function setup(title, body, cmds) {
    var steps = (cmds || []).map(function (c, i) {
      return '<li><div class="ch-step-h"><span class="ch-step-n">' + (i + 1) + '</span><b>' + esc(c[0]) + '</b></div><div class="ch-cmd"><code>' + esc(c[1]) + '</code><button type="button" class="ch-copy" data-copy="' + esc(c[1]) + '">Copy</button></div></li>';
    }).join('');
    root.innerHTML = '<header class="ld-head"><div class="ld-head-text"><div class="eyebrow">SOCIAL &middot; LISTENING</div><h1 class="ld-title">' + esc(title) + '</h1><p class="ld-sub">' + body + '</p></div></header>' +
      (steps ? '<article class="ld-panel ch-setup"><header><h3>Steps</h3><span class="hint">read-only page: it runs none of this for you</span></header><ol class="ch-steps">' + steps + '</ol></article>' : '');
    Array.prototype.forEach.call(root.querySelectorAll('.ch-copy'), function (btn) {
      btn.onclick = function () { var t = btn.getAttribute('data-copy'); try { navigator.clipboard.writeText(t); btn.textContent = 'Copied'; } catch (e) { btn.textContent = 'Select and copy'; } };
    });
    built = false;
  }
  function load(fresh) {
    if (busy) return; busy = true;
    var ctl = typeof AbortController === 'function' ? new AbortController() : null, to = setTimeout(function () { if (ctl) ctl.abort(); }, 20000);
    D.api.get('/api/social' + (fresh ? '?fresh=1' : ''), { signal: ctl && ctl.signal }).then(function (p) {
      clearTimeout(to); busy = false;
      if (p.state === 'not_installed') return setup('The social listening tables are not installed', 'Run this once to create the <code>sl</code> schema, then load an export (or the generated sample).', [['Create the schema', p.install], ['Load the generated sample (optional)', p.sample]]);
      if (p.state === 'empty') return setup('No mentions have been loaded yet', 'The schema exists but holds no clean mention. Load a keyword export into <code>sl.raw_mention</code> and run <code>sl.run_cleaning(batch_id)</code>, or generate the sample.', [['Load the generated sample', p.sample]]);
      DATA = p; loadedAt = Date.now(); D0 = new Date(p.d0 + 'T00:00:00Z'); NDAY = Math.round((new Date(p.d1 + 'T00:00:00Z') - D0) / 864e5) + 1;
      if (!built) skeleton();
      render();
    }).catch(function () {
      clearTimeout(to); busy = false;
      if (!DATA) setup('Can’t read the social listening data', 'The data service did not answer. Check that PostgreSQL is running, then press R to retry.');
    });
  }
  var sc = document.getElementById('socScroll');
  if (sc) sc.addEventListener('scroll', function () { var f = document.getElementById('s-filters'); if (f) f.classList.toggle('stuck', sc.scrollTop > f.offsetTop + 6); }, { passive: true });
  D.on('view', function (v) { if (v === 'social' && !DATA) load(false); });
  D.on('refresh-request', function () { if (D.view() === 'social') load(true); });
  window.addEventListener('resize', function () { clearTimeout(rt); rt = setTimeout(function () { if (D.view() === 'social') render(); }, 150); });
  if (D.view && D.view() === 'social') load(false);
})();
