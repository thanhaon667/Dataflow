/* ERP Desk - Channels page, PLACEMENTS panel: where the display ads ran (a website, a marketplace app or a banner slot) and what each place cost and
   produced. Draws the JSON of GET /api/channels/placements (desktop/placements_data.py); the SERVER decides every number, state, threshold and sentence,
   this file only lays it out and never computes a rate. The URL comes from the export links of the /api/channels payload on screen (lesson L-101), so
   the panel describes the same rows as the tiles above it; the CSV link comes from the placements payload itself and is disabled while a newer
   request is pending. Unknown values (ad size, position, viewability, cost per conversion) are drawn as a muted dash with the reason, never as 0.
   All server text goes through esc(). channels.js calls Desk.channelsPlacements.update(payload) after every render. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = D.esc;
  var FETCH_TIMEOUT_MS = 20000;
  var REFETCH_MS = 25000;
  var S = { data: null, base: null, url: null, loadedUrl: null, at: 0, busy: false, again: false, err: '', sig: null, sort: null, dir: null, dirty: false, blocked: false };
  var TYPE = { website: 'Website', app: 'App', video: 'Video', other: 'Other' };
  var POS = { above_fold: 'Above the fold', below_fold: 'Below the fold' };
  var COLS = [['Placement', 'placement', 'l'], ['Size', null, 'l'], ['Position', null, 'l'], ['Impressions', 'impressions', 'n'], ['Clicks', 'clicks', 'n'], ['CTR', 'ctr', 'n'],
              ['Conversions', 'conversions', 'n'], ['Spend', 'spend', 'n'], ['Cost / conv.', 'cpa', 'n'], ['Viewability', 'viewability', 'n']];

  function num(v) { return Number(v).toLocaleString('en-US'); }
  function plural(n, one, many) { return num(n) + ' ' + (n === 1 ? one : (many || one + 's')); }
  var FRACTION = {};
  function fractionDigits(cur) {
    if (FRACTION[cur] == null) {
      try { FRACTION[cur] = new Intl.NumberFormat('en-US', { style: 'currency', currency: cur }).resolvedOptions().maximumFractionDigits; } catch (e) { FRACTION[cur] = 2; }
    }
    return FRACTION[cur];
  }
  function fmtMoney(v, cur) { var d = fractionDigits(cur); return Number(v).toLocaleString('en-US', { minimumFractionDigits: d, maximumFractionDigits: d }); }
  function pctText(pct) {                  // the server only sends a pct when its denominator is at least low_n
    if (pct == null) return '';
    if (pct > 0 && pct < 0.1) return '<0.1%';
    return (Math.abs(pct) >= 100 ? Math.round(pct) : (Math.round(pct * 10) / 10)) + '%';
  }
  function lowN() { return (S.data && S.data.config && S.data.config.low_n) || 5; }

  function unknown(why, title) { return '<span class="ch-no" title="' + esc(title || why || 'not in the export') + '">' + esc(why || '–') + '</span>'; }
  function rateCell(r, what) {
    if (!r) return unknown('–');
    if (r.pct != null) return esc(pctText(r.pct));
    if (r.low_n) return '<span class="ch-cnt" title="Fewer than ' + lowN() + ' ' + esc(what) + ' behind this, so the counts are shown instead of a percentage">' + esc(num(r.n) + ' of ' + num(r.of)) + '</span>';
    return unknown(r.why || '–');
  }
  function moneyCell(m, cur) {
    if (m && m.state === 'ok') return '<span class="ch-mny"><em>' + esc(m.currency || cur) + '</em> ' + esc(fmtMoney(m.value, m.currency || cur)) + '</span>';
    return unknown('no spend', 'The export carries no spend for this row, so nothing is shown rather than 0');
  }
  function cpaCell(m) {
    if (m && m.state === 'ok') return '<span class="ch-mny"><em>' + esc(m.currency) + '</em> ' + esc(fmtMoney(m.value, m.currency)) + '</span>' + (m.low_n ? '<small title="Based on very few conversions">n = ' + esc(m.n) + '</small>' : '');
    return unknown((m && m.why) || '–');
  }
  function viewCell(v) {
    if (!v || v.state === 'none') return unknown('not measured', 'The export left viewability blank for this placement: unknown, never counted as 0%');
    var cov = v.coverage_pct != null && v.coverage_pct < 99.95 ? '<small title="Share of the placement\'s impressions that were measured for viewability">' + esc(pctText(v.coverage_pct)) + ' measured</small>' : '';
    return rateCell(v, 'measured impressions') + cov;
  }
  function nameCell(r) {
    var chips = '<em>' + esc(TYPE[r.type] || r.type) + (r.campaigns ? ' · ' + esc(plural(r.campaigns, 'campaign')) : '') + '</em>';
    if (r.is_other) return '<span class="pl-name"><b title="A bucket, not a place: each campaign keeps its first placements as their own rows and folds the rest into this row. Sums stay exact.">' + esc(r.placement) + '</b><em>bucket of the smaller ones</em></span>';
    return '<span class="pl-name"><b>' + esc(r.placement) + '</b>' + chips + '</span>';
  }
  function rowHtml(r) {
    return '<tr' + (r.is_other ? ' class="pl-other"' : '') + '>' +
      '<td class="l" data-label="Placement">' + nameCell(r) + '</td>' +
      '<td class="l" data-label="Size">' + (r.ad_size ? esc(r.ad_size) : unknown('–', 'The export left the ad size blank: unknown')) + '</td>' +
      '<td class="l" data-label="Position">' + (r.position ? esc(POS[r.position] || r.position) : unknown('–', 'The export left the position blank: unknown')) + '</td>' +
      '<td class="n" data-label="Impressions">' + esc(num(r.impressions)) + '</td>' +
      '<td class="n" data-label="Clicks">' + esc(num(r.clicks)) + '</td>' +
      '<td class="n" data-label="CTR">' + rateCell(r.ctr, 'impressions') + '</td>' +
      '<td class="n" data-label="Conversions">' + esc(num(r.conversions)) + '</td>' +
      '<td class="n" data-label="Spend">' + moneyCell(r.spend, r.currency) + '</td>' +
      '<td class="n" data-label="Cost / conv.">' + cpaCell(r.cpa) + '</td>' +
      '<td class="n" data-label="Viewability">' + viewCell(r.viewability) + '</td></tr>';
  }
  function totalRow(t) {
    return '<tr class="total"><td class="l" data-label="Total"><b>' + (t.currency ? esc(t.currency) + ' total' : 'Total (no money)') + '</b><em>' + esc(plural(t.placements, 'named placement')) + '</em></td>' +
      '<td class="l"></td><td class="l"></td>' +
      '<td class="n" data-label="Impressions">' + esc(num(t.impressions)) + '</td><td class="n" data-label="Clicks">' + esc(num(t.clicks)) + '</td>' +
      '<td class="n" data-label="CTR">' + rateCell(t.ctr, 'impressions') + '</td><td class="n" data-label="Conversions">' + esc(num(t.conversions)) + '</td>' +
      '<td class="n" data-label="Spend">' + moneyCell(t.spend, t.code) + '</td><td class="n" data-label="Cost / conv.">' + cpaCell(t.cpa) + '</td>' +
      '<td class="n" data-label="Viewability">' + viewCell(t.viewability) + '</td></tr>';
  }
  function commandBlock(step) {
    return '<li><span>' + esc(step.label) + '</span><code>' + esc(step.command) + '</code></li>';
  }
  function setupHtml(d) {
    var ins = d.install;
    if (!ins) return '';
    return '<div class="pl-setup"><h4>' + esc(ins.title) + '</h4><ol>' + (ins.steps || []).map(commandBlock).join('') + '</ol></div>';
  }
  function definitionsHtml(d) {
    if (!(d.definitions || []).length) return '';
    return '<details class="pl-defs"><summary>What these columns mean</summary><dl>' + d.definitions.map(function (x) { return '<dt>' + esc(x.term) + '</dt><dd>' + esc(x.text) + '</dd>'; }).join('') + '</dl></details>';
  }
  function tableHtml(d) {
    var t = d.table, sort = d.sort || {};
    var head = COLS.map(function (c) {
      if (!c[1]) return '<th scope="col" class="' + c[2] + '">' + esc(c[0]) + '</th>';
      var on = sort.key === c[1];
      return '<th scope="col" class="' + c[2] + '"' + (on ? ' aria-sort="' + (sort.dir === 'asc' ? 'ascending' : 'descending') + '"' : '') + '><button type="button" data-plsort="' + c[1] + '">' + esc(c[0]) +
        '<span class="ar" aria-hidden="true">' + (on ? (sort.dir === 'asc' ? '▲' : '▼') : '↕') + '</span></button></th>';
    }).join('');
    var sorts = (d.sorts || []).map(function (s) { return '<option value="' + esc(s.value) + '"' + (sort.key === s.value ? ' selected' : '') + '>' + esc(s.label) + '</option>'; }).join('');
    return '<div class="ch-tbl-wrap"><table class="ch-tbl pl-tbl"><thead><tr>' + head + '</tr></thead><tbody>' + t.rows.map(rowHtml).join('') + '</tbody><tfoot>' + (d.totals || []).map(totalRow).join('') + '</tfoot></table></div>' +
      '<div class="ch-tfoot"><span>' + (t.more ? '<b>' + esc(num(t.more)) + ' more</b> row' + (t.more === 1 ? '' : 's') + ' not listed here: the CSV and the Excel report carry all of them. ' : '') +
      'One row per placement, ad size and position, summed over the campaigns in view. Blank means unknown, not 0; percentages need at least ' + esc(lowN()) + ' in the denominator.</span>' +
      '<label class="ch-sortsel">sort <select data-plsel aria-label="Sort the placements by">' + sorts + '</select><button type="button" data-pldir aria-label="Reverse the sort order">' + (sort.dir === 'asc' ? '▲ asc' : '▼ desc') + '</button></label></div>';
  }
  function render() {
    var host = $('#plBody');
    var d = S.data;
    var sig = JSON.stringify([d, S.err]);
    if (S.sig === sig) return;
    S.sig = sig;
    syncButtons();
    if (!d) { host.innerHTML = '<div class="ins-wait">' + esc(S.err || 'Reading the placements…') + '</div>'; return; }
    var head = d.headline || {};
    var h = '<div class="ins-head" data-tone="' + esc(head.tone || 'grey') + '"><b>' + esc(head.text || '') + '</b>' + (head.sub ? '<span>' + esc(head.sub) + '</span>' : '') + '</div>';
    if (S.err) h += '<p class="ins-err" role="status">The latest refresh failed (' + esc(S.err) + '); showing what was read before.</p>';
    if (d.state === 'unavailable') h += '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>' + esc(d.error || 'could not be read') + '</span></div>';
    if (d.state === 'ready') {
      h += tableHtml(d);
      if ((d.notes || []).length) h += '<ul class="ins-notes">' + d.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
    } else h += setupHtml(d);
    h += definitionsHtml(d);
    host.innerHTML = h;
  }
  function syncButtons() {
    var b = $('#plCsv'), d = S.data;
    var ok = !!(d && d.state === 'ready' && d.export && d.export.csv && !S.busy && !S.blocked && S.loadedUrl === S.url);
    if (b) b.disabled = !ok;
  }

  function sortedUrl() {
    if (!S.base) return null;
    return S.base + (S.sort ? '&psort=' + encodeURIComponent(S.sort) + '&pdir=' + encodeURIComponent(S.dir || 'desc') : '');
  }
  function load() {
    if (S.busy) { S.again = true; return; }
    if (!S.url) return;
    S.busy = true; S.again = false;
    syncButtons();
    var url = S.url, ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, FETCH_TIMEOUT_MS);
    fetch(url, { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; }, function () { return { status: r.status, body: null }; });
    }).then(function (x) {
      if (x.status === 200 && x.body && x.body.state) { S.data = x.body; S.err = ''; S.at = Date.now(); S.loadedUrl = url; }
      else S.err = (x.body && x.body.error) || ('The placements feed answered ' + x.status);
    }).catch(function (e) {
      S.err = e && e.name === 'AbortError' ? 'the data service did not answer in ' + (FETCH_TIMEOUT_MS / 1000) + ' s' : 'could not reach the data service';
    }).then(function () {
      clearTimeout(timer); S.busy = false; render(); syncButtons();
      if (S.again || S.url !== url) { S.again = false; load(); }
    });
  }
  function setSort(key, dir) {
    S.sort = key; S.dir = dir;
    S.url = sortedUrl();
    S.sig = null;
    load();
  }
  function update(d) {
    var base = d && d.export && d.export.placements;
    if (!base) return;
    var baseChanged = base !== S.base;
    S.base = base;
    var url = sortedUrl();
    var changed = url !== S.url;
    S.url = url;
    syncButtons();
    if (changed || baseChanged || S.dirty || Date.now() - S.at > REFETCH_MS) { S.dirty = false; load(); }
    render();
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b || !$('#plBody') || !$('#plBody').contains(b)) return;
    var key = b.getAttribute('data-plsort');
    var d = S.data;
    if (key && d) {
      var cur = d.sort || {};
      var first = ((d.sorts || []).filter(function (s) { return s.value === key; })[0] || {}).dir || 'desc';
      setSort(key, cur.key === key ? (cur.dir === 'asc' ? 'desc' : 'asc') : first);
    } else if (b.hasAttribute('data-pldir') && d) {
      var c = d.sort || {};
      setSort(c.key, c.dir === 'asc' ? 'desc' : 'asc');
    }
  });
  document.addEventListener('change', function (e) {
    var sel = e.target;
    if (sel && sel.hasAttribute && sel.hasAttribute('data-plsel') && S.data) {
      var first = ((S.data.sorts || []).filter(function (s) { return s.value === sel.value; })[0] || {}).dir || 'desc';
      setSort(sel.value, first);
    }
  });
  var csv = $('#plCsv');
  if (csv) csv.addEventListener('click', function () {
    var d = S.data;
    if (csv.disabled || !d || !d.export || !d.export.csv) return;          // the link is the server-validated one of the payload on screen (L-101)
    if (D.channels && D.channels.download) D.channels.download(d.export.csv, csv, 'channels_placements.csv', 'placements CSV');
  });

  D.channelsPlacements = { update: update, invalidate: function () { S.dirty = true; }, sync: function (blocked) { S.blocked = !!blocked; syncButtons(); } };
  if (D.channels && D.channels.last()) update(D.channels.last());
})();
