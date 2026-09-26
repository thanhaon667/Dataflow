/* ERP Desk - Channels page, INSIGHTS panel: severity-sorted finding cards with the evidence and a suggested action, an
   "automation ideas" strip (plain-text suggestions - this page never schedules or runs anything) and the "How these are decided"
   drawer. Draws the JSON of GET /api/channels/insights (desktop/insights_data.py); the SERVER decides every finding, number,
   threshold and sentence, this file only lays it out. The URL comes from the export links of the /api/channels payload on screen
   (lesson L-101), so the panel always describes the same rows as the tiles. It never touches the raw events: the rules read the
   rollup. All server text goes through esc(). channels.js calls Desk.channelsInsights.update(payload) after every render. */
(function () {
  'use strict';
  var D = window.Desk;
  if (!D) return;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = D.esc;
  var FETCH_TIMEOUT_MS = 20000;
  var REFETCH_MS = 25000;
  var SHOWN = 6;
  var S = { data: null, url: null, at: 0, busy: false, again: false, err: '', sig: null, all: false, open: false, dirty: false, sigDr: null };
  var SEV = { crit: 'Critical', warn: 'Warning', info: 'Info' };
  var MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  function fmtDay(k) { var p = String(k || '').split('-'); return p.length === 3 && MON[+p[1] - 1] ? (+p[2]) + ' ' + MON[+p[1] - 1] + ' ' + p[0] : (k || ''); }
  function plural(n, one, many) { return Number(n).toLocaleString('en-US') + ' ' + (n === 1 ? one : (many || one + 's')); }

  function windowLine(d) {
    var w = d.window;
    if (!w) return '';
    var h = 'Analysed <b>' + esc(fmtDay(w.from)) + '</b> to <b>' + esc(fmtDay(w.to)) + '</b> (' + esc(plural(w.days, 'day')) + ')';
    h += ' against the previous ' + esc(plural(w.days, 'day')) + ' (' + esc(fmtDay(w.prev_from)) + ' to ' + esc(fmtDay(w.prev_to)) + ').';
    if (w.data_first_day) h += ' The rollup holds data from ' + esc(fmtDay(w.data_first_day)) + ' to ' + esc(fmtDay(w.data_last_day)) + '.';
    if (w.defaulted) h += ' No range was chosen, so this is the default window: the last days up to the newest day of data. Use the RANGE control above to change it.';
    return h;
  }
  function evidenceHtml(f) {
    return '<ul class="ins-ev">' + (f.evidence || []).map(function (e) {
      return '<li class="' + (e.compare ? (e.met ? 'met' : 'unmet') : 'ctx') + '"><span aria-hidden="true">' + (e.compare ? (e.met ? '✓' : '○') : '·') + '</span><span>' + esc(e.text) + '</span></li>';
    }).join('') + '</ul>';
  }
  function cardHtml(f) {
    var chips = [];
    if (f.channel) chips.push('<span>' + esc(f.channel.name) + '</span>');
    if (f.campaign && f.campaign.id) chips.push('<span>' + esc(f.campaign.name) + '</span>');
    chips.push('<span>' + (f.currency ? esc(f.currency) : 'counts only') + '</span>');
    if (f.estimate) chips.unshift('<span class="ins-est" title="Arithmetic on past numbers with the assumption written below - not a forecast">ESTIMATE</span>');
    return '<article class="ins-card" data-sev="' + esc(f.severity) + '" data-rule="' + esc(f.id) + '">' +
      '<div class="ins-top"><span class="ins-sev">' + esc(SEV[f.severity] || f.severity) + '</span>' +
      '<span class="ins-conf' + (f.confidence === 'thin data' ? ' thin' : '') + '" title="' + esc(f.confidence === 'thin data' ? 'Too little volume for a firm verdict: shown as info only' : 'Enough volume behind the numbers for the rule to be trusted') + '">' + esc(f.confidence) + '</span></div>' +
      '<h4>' + esc(f.what) + '</h4><div class="ins-meta">' + chips.join('') + '</div>' + evidenceHtml(f) +
      '<p class="ins-act"><b>Suggested action</b>' + esc(f.action) + '</p></article>';
  }
  function render() {
    var host = $('#insBody');
    var d = S.data;
    var sig = JSON.stringify([d, S.all, S.err]);
    if (S.sig === sig) return;
    S.sig = sig;
    if (!d) {
      host.innerHTML = '<div class="ins-wait">' + (S.err ? esc(S.err) : 'Reading the insights…') + '</div>';
      return;
    }
    var h = '';
    var head = d.headline || {};
    h += '<div class="ins-head" data-tone="' + esc(head.tone || 'grey') + '"><b>' + esc(head.text || '') + '</b>' + (head.sub ? '<span>' + esc(head.sub) + '</span>' : '') + '</div>';
    if (d.window) h += '<p class="ins-win">' + windowLine(d) + '</p>';
    var canAll = d.window && d.window.data_first_day && (d.window.from > d.window.data_first_day || d.window.to < d.window.data_last_day);
    if (d.state === 'no_data_in_window' || canAll) h += '<p class="ins-tools"><button type="button" class="linkbtn" data-ins-all>Analyse all data</button><span>or pick another range with the RANGE control above</span></p>';
    if (S.err && d.state) h += '<p class="ins-err" role="status">The latest refresh failed (' + esc(S.err) + '); showing what was read before.</p>';
    if (d.state === 'unavailable') h += '<div class="ld-na"><span class="tag">UNAVAILABLE</span><span>' + esc(d.error || 'could not be read') + '</span></div>';
    var fs = d.findings || [];
    if (fs.length) {
      var shown = S.all ? fs : fs.slice(0, SHOWN);
      h += '<div class="ins-grid">' + shown.map(cardHtml).join('') + '</div>';
      if (fs.length > SHOWN) h += '<p class="ins-tools"><button type="button" class="linkbtn" data-ins-more aria-expanded="' + S.all + '">' + (S.all ? 'Show fewer' : 'Show all ' + fs.length + ' findings') + '</button></p>';
    }
    if ((d.notes || []).length) h += '<ul class="ins-notes">' + d.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>';
    if ((d.automation || []).length) {
      h += '<div class="ins-auto"><h5>Automation ideas <em>suggestions only - this page never schedules or runs anything</em></h5><ul>' + d.automation.map(function (a) {
        return '<li><b>' + esc(a.title) + '</b><span>' + esc(a.text) + '</span><small>Based on ' + esc(a.based_on) + '</small></li>'; }).join('') + '</ul></div>';
    }
    if ((d.rules || []).length) {
      h += '<details class="ins-rules"><summary>Rules checked (' + d.rules.filter(function (r) { return r.status === 'ran'; }).length + ' of ' + d.rules.length + ' had enough data to run)</summary><ul>' +
        d.rules.map(function (r) { return '<li><b>' + esc(r.title) + '</b> - ' + (r.status === 'ran' ? 'ran, ' + esc(plural(r.fired, 'finding')) : 'skipped: ' + esc(r.why || '')) + '</li>'; }).join('') + '</ul></details>';
    }
    host.innerHTML = h;
  }
  function renderDrawer() {
    var d = S.data;
    if (!d || !d.how) return;
    var sig = JSON.stringify(d.how);
    if (S.sigDr === sig) return;
    S.sigDr = sig;
    var byRule = {};
    d.how.thresholds.forEach(function (t) { (byRule[t.rule] = byRule[t.rule] || []).push(t); });
    var h = '<dl>' + d.how.rules.map(function (r) {
      return '<dt>' + esc(r.title) + '</dt><dd>' + esc(r.text) + '<table class="ins-thr"><tbody>' + (byRule[r.id] || []).map(function (t) {
        return '<tr><th scope="row">' + esc(t.name) + '</th><td>' + esc(String(t.value)) + ' ' + esc(t.unit) + '</td><td>' + esc(t.meaning) + '</td></tr>'; }).join('') + '</tbody></table></dd>';
    }).join('') + '</dl>';
    var auto = byRule.automation || [];
    h += '<dl><dt>Automation ideas</dt><dd>' + auto.map(function (t) { return esc(t.name) + ' = ' + esc(String(t.value)) + ' ' + esc(t.unit) + ': ' + esc(t.meaning); }).join(' ') + '</dd></dl>';
    h += '<ul class="ins-notes">' + d.how.notes.map(function (n) { return '<li>' + esc(n) + '</li>'; }).join('') + '</ul>' +
      '<p class="ld-dr-foot">Generated by the server from the same constants the rules use, so this text cannot drift from the calculation.</p>';
    $('#chInsDrBody').innerHTML = h;
  }
  function openDrawer(open) {
    S.open = open;
    var dr = $('#chInsDrawer');
    dr.classList.toggle('open', open);
    dr.setAttribute('aria-hidden', open ? 'false' : 'true');
    $('#chInsScrim').hidden = !open;
    $('#insHow').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) setTimeout(function () { $('#chInsClose').focus({ preventScroll: true }); }, 60); else $('#insHow').focus({ preventScroll: true });
  }

  function load() {
    if (S.busy) { S.again = true; return; }
    if (!S.url) return;
    S.busy = true; S.again = false;
    var url = S.url, ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer = setTimeout(function () { if (ctl) ctl.abort(); }, FETCH_TIMEOUT_MS);
    fetch(url, { cache: 'no-store', signal: ctl ? ctl.signal : undefined }).then(function (r) {
      return r.json().then(function (j) { return { status: r.status, body: j }; }, function () { return { status: r.status, body: null }; });
    }).then(function (x) {
      if (x.status === 200 && x.body && x.body.state) { S.data = x.body; S.err = ''; S.at = Date.now(); }
      else S.err = (x.body && x.body.error) || ('The insights feed answered ' + x.status);
    }).catch(function (e) {
      S.err = e && e.name === 'AbortError' ? 'the data service did not answer in ' + (FETCH_TIMEOUT_MS / 1000) + ' s' : 'could not reach the data service';
    }).then(function () {
      clearTimeout(timer); S.busy = false; render(); renderDrawer();
      if (S.again || S.url !== url) { S.again = false; load(); }
    });
  }
  function update(d) {
    var url = d && d.export && d.export.insights;
    if (!url) return;
    var changed = url !== S.url;
    S.url = url;
    if (changed || S.dirty || Date.now() - S.at > REFETCH_MS) { S.dirty = false; load(); }
    render();
  }

  document.addEventListener('click', function (e) {
    var b = e.target.closest ? e.target.closest('button') : null;
    if (!b) return;
    if (b.hasAttribute('data-ins-more')) { S.all = !S.all; render(); }
    else if (b.hasAttribute('data-ins-all')) { if (D.channels) D.channels.useAllData(); }
  });
  $('#insHow').addEventListener('click', function () { openDrawer(!S.open); });
  $('#chInsClose').addEventListener('click', function () { openDrawer(false); });
  $('#chInsScrim').addEventListener('click', function () { openDrawer(false); });
  document.addEventListener('keydown', function (e) { if (e.key === 'Escape' && S.open) { e.stopPropagation(); openDrawer(false); } });

  D.channelsInsights = { update: update, invalidate: function () { S.dirty = true; } };
  if (D.channels && D.channels.last()) update(D.channels.last());
})();
