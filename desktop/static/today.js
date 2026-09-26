/* ERP Desk - Today page (the landing page). Draws the JSON of GET /api/today (desktop/today_data.py).
   Nothing is computed here except layout and wording of things the server already decided: every number, every
   sentence and every health colour comes from the feed. All server text goes through esc()/textContent. */
(function () {
  'use strict';
  var D = window.Desk;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = D.esc;
  var POLL_MS = 15000;
  var FETCH_TIMEOUT_MS = 8000;               // a feed that has not answered by now is "slow", not "loading": show STALE, never hang
  var OLD_AFTER_MS = POLL_MS * 3;            // a payload the server read longer ago than this is old, whatever the network says
  var S = { data: null, at: 0, timer: null, fails: 0, played: false, sig: {}, busy: false, again: false, inflight: null, live: 'connecting', old: false, err: '' };

  var ICON = {
    ok: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="M12 6.5v7M12 17.2v.3"/></svg>',
    watch: '<svg viewBox="0 0 24 24"><path d="M12 7v6l3.5 2"/></svg>',
    unknown: '<svg viewBox="0 0 24 24"><path d="M9.5 9.2a2.7 2.7 0 1 1 3.6 2.6c-.8.4-1.1.9-1.1 1.7M12 17.3v.2"/></svg>',
    lead: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.7-3.6 3.5-5.5 7-5.5s6.3 1.9 7 5.5"/></svg>',
    ticket: '<svg viewBox="0 0 24 24"><path d="M4 8.5a2 2 0 0 0 0 4v0a2 2 0 0 1 0 4V18h16v-1.5a2 2 0 0 1 0-4v0a2 2 0 0 0 0-4V7H4z" transform="translate(0 -.5)"/><path d="M14 7v10" stroke-dasharray="2 2.6"/></svg>',
    ok2: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>'
  };
  var LEVEL_LABEL = { ok: 'ALL CLEAR', attention: 'NEEDS ATTENTION', watch: 'KEEP AN EYE ON IT', unknown: 'CANNOT READ THE NUMBERS', loading: 'CHECKING' };
  var TAG = { ok: 'OK', warn: 'CHECK', bad: 'DOWN', off: 'NOT SET UP', unknown: 'UNKNOWN' };
  var TONE_VAR = { blue: 'var(--blue)', red: 'var(--red)', green: 'var(--green)', amber: 'var(--amber)', violet: 'var(--violet)', grey: '#b9b5aa' };

  function fmtTime(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    return isNaN(d) ? '' : d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  }
  function ago(ms) {
    var s = Math.max(0, Math.round((Date.now() - ms) / 1000));
    if (s < 5) return 'just now';
    if (s < 90) return s + 's ago';
    return Math.round(s / 60) + 'm ago';
  }
  function pretty(s) { return String(s == null ? '' : s).replace(/ - /g, ' – '); }   // typographic dash for display only
  function dayShort(iso) {
    var d = new Date(iso + 'T00:00:00');
    return isNaN(d) ? iso : d.toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric' });
  }

  // ------------------------------------------------------------------ live pill
  function setLive(state, label) {
    S.live = state;
    var p = $('#tPill');
    p.setAttribute('data-state', state);
    S.old = (state === 'stale' && label !== 'PARTIAL') || state === 'offline';     // "PARTIAL" = fresh but incomplete, not old
    $('#tLive').textContent = label || { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#tAgo');
    if (!S.at) { el.textContent = ''; return; }
    el.textContent = 'updated ' + ago(S.at);
    $('#tPill').title = (S.old && S.err ? S.err + '. Showing the numbers read at ' + fmtTime(S.data && S.data.generated_at) + '. ' : 'Read from the database at ' + fmtTime(S.data && S.data.generated_at) + ' - ') +
      'This page checks again every ' + (POLL_MS / 1000) + ' s while it is open';
  }
  function eyebrow() {                       // "STATUS · ALL CLEAR", and when the numbers are old, since when
    if (!S.data || !S.data.status) return;
    var lv = S.data.status.level;
    $('#tEyebrow').textContent = 'STATUS · ' + (LEVEL_LABEL[lv] || '') + (S.old ? ' · AS OF ' + fmtTime(S.data.generated_at) : '');
  }

  // ------------------------------------------------------------------ hero
  function renderHero(d) {
    var st = d.status || { level: 'unknown', lead: 'Can’t read today’s numbers', rest: '', headline: '', sub: '' };
    var hero = $('#tHero');
    hero.setAttribute('data-level', st.level);
    hero.classList.remove('loading');
    $('#tOrb').innerHTML = ICON[st.level === 'ok' ? 'ok' : st.level === 'attention' ? 'alert' : st.level === 'watch' ? 'watch' : 'unknown'];
    eyebrow();
    var s = $('#tSentence');
    s.setAttribute('aria-label', st.headline || '');
    s.textContent = '';
    var lead = document.createElement('span'); lead.className = 'lead'; lead.textContent = st.lead || '';
    var sep = document.createElement('span'); sep.className = 'sep';
    sep.textContent = st.sep == null || st.sep === ' - ' ? ' – ' : st.sep;     // ", " for "No problems found, but ..."
    s.appendChild(lead);
    if (st.rest) {
      s.appendChild(sep);
      var restMain = String(st.rest), paren = null;
      if (st.sep === ', ') {        // "No problems found, but the numbers may be incomplete (3 new leads today, none overdue)": the figures go on their own smaller line
        var pm = /^([\s\S]*?)\s*(\([^()]*\))$/.exec(restMain);
        if (pm) { restMain = pm[1]; paren = pm[2]; }
      }
      restMain.split(/(\d+(?:\.\d+)?-business-hour)/).forEach(function (part) {   // never break "5-business-hour" over two lines
        if (!part) return;
        if (/-business-hour$/.test(part)) { var nb = document.createElement('span'); nb.className = 'nb'; nb.textContent = part; s.appendChild(nb); }
        else s.appendChild(document.createTextNode(part));
      });
      if (paren) { var pe = document.createElement('span'); pe.className = 'paren'; var fig = paren.slice(1, -1); pe.textContent = fig.charAt(0).toUpperCase() + fig.slice(1) + '.'; s.appendChild(pe); }
    }
    $('#tSub').textContent = pretty(st.sub);
    renderCaveat(st.caveat);
    $('#tDay').textContent = d.day_label || 'Today';
  }
  // The honesty line: shown only when the server says some input is blind (webhook offline, ClickUp pull not scheduled ...).
  // The server's own prefix ("Numbers may be incomplete:") becomes the small amber label, so the caveat reads as a different
  // KIND of text from the figures line above it instead of a second sentence in the same voice.
  function renderCaveat(text) {
    var box = $('#tCaveat'), lab = $('#tCaveatLabel'), out = $('#tCaveatText');
    out.textContent = ''; lab.textContent = '';
    box.hidden = !text;
    if (!text) return;
    var m = /^([^:]{3,60}):\s*([\s\S]*)$/.exec(String(text));
    if (m) { lab.textContent = m[1]; out.textContent = m[2]; }
    else { lab.textContent = 'Heads up'; out.textContent = String(text); }
  }

  // ------------------------------------------------------------------ "since yesterday"
  // One plain line the server wrote; each clause that has rows behind it is a button that opens the drawer on that list.
  // When a source is blind or unreadable the server leaves the clause out and gives a short note instead of a zero, and
  // when there is nothing on file from before today it says so by not offering a line at all.
  function renderSince(d) {
    var s = d.since || { available: false };
    var box = $('#tSince'), parts = $('#tSinceParts'), note = $('#tSinceNote'), label = $('#tSinceLabel');
    var sig = JSON.stringify(s);
    if (S.sig.since === sig) return;
    S.sig.since = sig;
    box.hidden = !(s.available || (s.note && s.unknown && s.unknown.length));
    if (box.hidden) { parts.textContent = ''; note.hidden = true; return; }
    label.textContent = s.label || 'Since yesterday';
    label.title = s.day_note || '';
    box.setAttribute('aria-label', s.text || s.note || 'What changed since yesterday');
    parts.innerHTML = (s.parts || []).map(function (p) {
      var t = esc(pretty(p.text)), tone = esc(p.tone || 'flat');
      if (!p.detail) return '<span class="t-chip" data-tone="' + tone + '">' + t + '</span>';
      return '<button type="button" class="t-chip drill" data-tone="' + tone + '" data-detail="' + esc(p.detail) +
        '" title="Show the leads or tickets behind this">' + t + '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>';
    }).join('');
    note.hidden = !s.note;
    note.textContent = s.note ? pretty(s.note) : '';
  }

  // ------------------------------------------------------------------ KPI tiles
  function sparkHtml(t) {
    var v = t.spark;
    if (!v || v.length < 2) return '<div class="t-spark" aria-hidden="true"></div>';
    var days = t.spark_days || [];
    var label = 'Last ' + v.length + ' days: ' + v.map(function (n, i) { return (days[i] ? dayShort(days[i]) + ' ' : '') + n; }).join(', ');
    var max = Math.max.apply(null, v);
    var html;
    if (t.spark_kind === 'bars') {
      html = '<div class="t-bars">' + v.map(function (n, i) {
        var h = max ? Math.max(9, Math.round(n / max * 100)) : 9;
        return '<i style="height:' + h + '%;--k:' + i + '"></i>';
      }).join('') + '</div>';
    } else {
      var n = v.length, pts = [];
      v.forEach(function (x, i) { pts.push([i / (n - 1) * 100, max ? 30 - x / max * 26 : 30]); });
      var line = pts.map(function (p, i) { return (i ? 'L' : 'M') + p[0].toFixed(2) + ' ' + p[1].toFixed(2); }).join(' ');
      var last = pts[n - 1];
      html = '<svg viewBox="0 0 100 34" preserveAspectRatio="none"><line class="base" x1="0" y1="32" x2="100" y2="32"/>' +
        '<path class="ar" d="' + line + ' L100 32 L0 32 Z"/><path class="ln" pathLength="1" d="' + line + '"/></svg>' +
        '<span class="pt" style="left:' + last[0].toFixed(2) + '%;top:' + (last[1] / 34 * 100).toFixed(2) + '%"></span>';
    }
    return '<div class="t-spark" role="img" aria-label="' + esc(label) + '" title="' + esc(label) + '">' + html + '</div>';
  }
  function deltaHtml(t) {
    var x = t.delta;
    if (!x) return '';
    var cls = x.good === true ? ' good' : x.good === false ? ' bad' : '';
    var arrow = x.dir === 'up' ? '▲ ' : x.dir === 'down' ? '▼ ' : '';
    return '<span class="t-delta' + cls + '">' + arrow + esc(x.text) + '</span>';
  }
  function tileHtml(t) {
    var na = t.state !== 'ok';
    var val;
    if (na) val = '<div class="t-kpi-row"><span class="t-kpi-value dash">–</span></div>';
    else if (t.text != null) val = '<div class="t-kpi-row"><span class="t-kpi-value txt">' + esc(t.text) + '</span></div>';
    else if (t.value2 != null) val = '<div class="t-kpi-pair"><div class="p"><span class="t-kpi-value">' + esc(t.value) + '</span><span class="t-kpi-unit">' + esc(t.unit || '') +
      '</span></div><div class="p"><span class="t-kpi-value">' + esc(t.value2) + '</span><span class="t-kpi-unit">' + esc(t.label2 || '') + '</span></div></div>';
    else val = '<div class="t-kpi-row"><span class="t-kpi-value">' + esc(t.value) + '</span>' + deltaHtml(t) + '</div>';
    // A tile is clickable only when the server put a drawer list behind it AND that list really came back with rows,
    // so the chevron never opens an empty panel. The button covers the tile so mouse and keyboard reach the same target.
    // no title= on the button: the tile's own title (the definition of the number) must keep showing on hover
    var open = t.detail ? '<button type="button" class="t-kpi-open" data-detail="' + esc(t.detail) +
      '" aria-label="' + esc('Show what “' + t.label + '” counts') +
      '"><span class="t-drill" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span></button>' : '';
    return '<div class="t-kpi-label">' + esc(t.label) + '</div>' + val +
      (na ? '<div class="t-spark" aria-hidden="true"></div><span class="t-na">UNAVAILABLE</span>' : sparkHtml(t)) +
      '<div class="t-kpi-sub">' + esc(pretty(t.sub)) + '</div>' + open;
  }
  function renderKpis(d) {
    var box = $('#tKpis');
    (d.kpis || []).forEach(function (t, i) {
      var el = box.querySelector('[data-id="' + t.id + '"]');
      var sig = JSON.stringify(t);
      if (!el) {
        el = document.createElement('article');
        el.className = 't-kpi t-rise';
        el.setAttribute('data-id', t.id);
        el.style.setProperty('--i', String(i + 2));
        box.appendChild(el);
      }
      if (S.sig[t.id] === sig) return;
      var first = S.sig[t.id] === undefined;
      S.sig[t.id] = sig;
      el.setAttribute('data-tone', t.tone || 'blue');
      el.classList.toggle('na', t.state !== 'ok');
      el.classList.toggle('drill', !!t.detail);
      el.title = t.hint || '';
      el.innerHTML = tileHtml(t);
      if (!first) { el.classList.remove('flash'); void el.offsetWidth; el.classList.add('flash'); }
    });
    // a tile that disappeared from the feed (should not happen) is removed
    Array.prototype.slice.call(box.children).forEach(function (el) {
      if (!(d.kpis || []).some(function (t) { return t.id === el.getAttribute('data-id'); })) el.remove();
    });
  }

  // ------------------------------------------------------------------ attention list
  function renderAttention(d) {
    var a = d.attention || { available: false, items: [] };
    var body = $('#tAttnBody'), cnt = $('#tAttnCount');
    // the signature includes which rows have a detail panel, or a row could stay plain text after the drawer data arrives
    var sig = JSON.stringify([a, (a.items || []).map(function (it) { return !!details().items[it.ref]; })]);
    if (S.sig.attn === sig) return;
    S.sig.attn = sig;
    if (!a.available) {
      cnt.textContent = ''; cnt.className = 't-count';
      body.innerHTML = '<div class="t-empty na"><span class="ok-mark">' + ICON.unknown + '</span><div><b>Can’t build this list right now</b><span>' +
        esc((a.note || 'The database could not be read') + '.') + ' The tiles above say which numbers are affected.</span></div></div>';
      return;
    }
    if (!a.items.length) {
      cnt.textContent = 'nothing waiting'; cnt.className = 't-count';
      body.innerHTML = '<div class="t-empty"><span class="ok-mark">' + ICON.ok + '</span><div><b>Nothing needs a human right now</b><span>No lead is past its deadline and no ticket is stuck. ' +
        'This list fills itself in the moment that changes, so there is nothing to check here.</span></div></div>';
      return;
    }
    cnt.textContent = a.total + (a.total === 1 ? ' item' : ' items'); cnt.className = 't-count red';
    var known = details().items;
    // Lesson L-145: the server tells us when every row is in the same state; then that phrase is printed once, here,
    // and each row shows who owns it instead of repeating it.
    var same = a.all_same_what || '';
    var html = (same ? '<p class="t-allsame">' + esc(same) + ' &middot; every row below.</p>' : '') +
      '<ul class="t-list">' + a.items.map(function (it, i) {
      // the row is a button whenever the drawer payload really carries this lead / ticket; otherwise it stays plain text
      var can = !!known[it.ref], tag = can ? 'button' : 'div';
      return '<li class="t-item" data-sev="' + esc(it.severity) + '" style="--k:' + i + '">' +
        '<' + tag + (can ? ' type="button" class="t-item-btn" data-ref="' + esc(it.ref) + '" aria-label="' + esc('Open the details of ' + it.title) + '"' : ' class="t-item-btn plain"') + '>' +
        '<span class="t-ico">' + (it.kind === 'ticket' ? ICON.ticket : ICON.lead) + '</span>' +
        '<span class="t-item-text"><span class="t-item-title" title="' + esc(it.title) + '">' + esc(it.title) + '</span>' +
        '<span class="t-item-what">' + (same ? esc(it.who) : '<b>' + esc(it.what) + '</b> · ' + esc(it.who)) + '</span></span>' +
        '<span class="t-badge">' + esc(it.badge) + '</span>' +
        (can ? '<span class="t-drill" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span>' : '') +
        '</' + tag + '></li>';
    }).join('') + '</ul>';
    var rest = (a.total || 0) - a.items.length;
    html += '<div class="t-more">' + (rest > 0 ? 'and ' + rest + ' more · ' : '') +
      '<button type="button" class="linkbtn big" data-go="reporting">Open Reporting for the full picture →</button>' +
      (a.note ? '<span>' + esc(a.note) + '</span>' : '') + '</div>';
    body.innerHTML = html;
  }

  // ------------------------------------------------------------------ system health strip
  function renderHealth(d) {
    var strip = $('#tHealthStrip');
    var sig = JSON.stringify(d.health || []);
    if (S.sig.health === sig) return;
    S.sig.health = sig;
    strip.innerHTML = (d.health || []).map(function (c) {
      return '<div class="t-cell" data-state="' + esc(c.state) + '"' + (c.detail ? ' title="' + esc(c.detail) + '"' : '') + '>' +
        '<div class="t-cell-top"><span class="t-dot"></span><span class="t-cell-tag">' + esc(TAG[c.state] || '') + '</span></div>' +
        '<div class="t-cell-name">' + esc(c.label) + '</div><div class="t-cell-text">' + esc(pretty(c.text)) + '</div></div>';
    }).join('') || '<div class="t-cell" data-state="unknown"><div class="t-cell-text">The health checks are not available yet.</div></div>';
  }

  function renderRules(d) {
    var ul = $('#tRulesList');
    var sig = JSON.stringify(d.rules || []);
    if (S.sig.rules === sig) return;
    S.sig.rules = sig;
    ul.innerHTML = (d.rules || []).map(function (r) { return '<li>' + esc(r) + '</li>'; }).join('');
  }

  // ------------------------------------------------------------------ detail drawer
  // Everything it shows is already in the /api/today payload (desktop/today_data.py builds it), so opening the drawer
  // fetches nothing and there is still exactly one poller. Two modes: a LIST (what a tile counts) and one ITEM.
  var DR = { open: false, mode: '', list: '', ref: '', opener: null, back: '', sig: '', kept: null, scrolled: '' };

  function details() { return (S.data && S.data.details) || { lists: {}, items: {}, privacy: '' }; }
  function sevOf(s) { return s === 'late' ? 'late' : s === 'old' ? 'old' : s === 'ok' ? 'ok' : 'none'; }

  // `sameWhat`: in a list where every row is in the same state ("Leads past SLA now") repeating that state ten times is
  // noise - the panel's own sub-heading already says it, so the row shows who owns it instead.
  function rowHtml(r, i, sameWhat) {
    var second = sameWhat ? esc(r.who || '') : ('<b>' + esc(r.what) + '</b>' + (r.who ? ' · ' + esc(r.who) : ''));
    return '<li class="t-dr-row" data-sev="' + esc(sevOf(r.severity)) + '" style="--k:' + i + '">' +
      '<button type="button" class="t-dr-rowbtn" data-ref="' + esc(r.ref) + '">' +
      '<span class="t-ico">' + (r.kind === 'ticket' ? ICON.ticket : ICON.lead) + '</span>' +
      '<span class="t-item-text"><span class="t-item-title">' + esc(r.title) + '</span>' +
      '<span class="t-item-what">' + second + '</span></span>' +
      '<span class="t-badge">' + esc(r.badge) + '</span>' +
      '<span class="t-drill" aria-hidden="true"><svg viewBox="0 0 24 24"><path d="M9 6l6 6-6 6"/></svg></span></button></li>';
  }

  function listBody(spec) {
    if (!spec.available) {
      return '<p class="t-dr-none">This list could not be read: ' + esc(spec.error || 'the database did not answer') +
        '. Only this panel is affected - the numbers on the page behind it were read separately.</p>';
    }
    if (!spec.rows.length) return '<p class="t-dr-none">Nothing is in this list right now.</p>';
    var same = spec.rows.length > 1 && spec.rows.every(function (r) { return r.what === spec.rows[0].what; });
    var html = (same ? '<p class="t-dr-allsame">' + esc(spec.rows[0].what) + ' · every row below.</p>' : '') +
      '<ul class="t-dr-list">' + spec.rows.map(function (r, i) { return rowHtml(r, i, same); }).join('') + '</ul>';
    if (spec.more > 0) html += '<p class="t-dr-more">' + esc(spec.more) + ' more ' + (spec.more === 1 ? 'is' : 'are') +
      ' not shown here · this panel lists at most ' + esc(spec.cap) + ' rows. Open the Leads page or Reporting for all of them.</p>';
    if (spec.related) html += '<p class="t-dr-rel"><button type="button" class="linkbtn big" data-detail="' + esc(spec.related.id) + '">' +
      esc(spec.related.text) + ' →</button></p>';
    return html;
  }

  function cardHtml(title, inner) { return '<section class="t-dr-card"><h3>' + esc(title) + '</h3>' + inner + '</section>'; }

  function aiHtml(it) {
    var a = it.ai;
    if (!a) return '<p class="t-dr-none">' + esc(it.ai_none) + '</p>';
    var chips = [];
    if (a.score != null) chips.push('<span class="t-dr-score" data-band="' + (a.score >= 70 ? 'hi' : a.score >= 40 ? 'mid' : 'lo') + '">' + esc(a.score) + '<small>/100 potential</small></span>');
    if (a.org) chips.push('<span class="t-chip" data-tone="flat">' + esc(a.org) + '</span>');
    if (a.scale) chips.push('<span class="t-chip" data-tone="flat">' + esc(a.scale) + '</span>');
    return '<div class="t-dr-airow">' + chips.join('') + '</div>' +
      (a.notes ? '<p class="t-dr-quote">' + esc(a.notes) + (a.notes_cut ? ' <i>(shortened)</i>' : '') + '</p>' : '') +
      '<p class="t-dr-meta">' + esc((a.model ? a.model + ' · ' : '') + 'analysed ' + (a.ago || 'at an unknown time')) + '</p>';
  }

  function clickupHtml(it) {
    var c = it.clickup;
    if (!c) return '<p class="t-dr-none">' + esc(it.clickup_none) + '</p>';
    var h = '<p class="t-dr-line" data-tone="' + esc(c.tone) + '"><span class="t-dot"></span>' + esc(c.text) + '</p>';
    if (c.url) h += '<p class="t-dr-link"><a href="' + esc(c.url) + '" target="_blank" rel="noopener noreferrer">Open the task in ClickUp' +
      '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 5h5v5M19 5l-8 8M18 14v5H5V6h5"/></svg></a></p>';
    else if (c.task_id) h += '<p class="t-dr-meta">Task <code>' + esc(c.task_id) + '</code></p>';
    h += '<p class="t-dr-meta">' + esc('Sent to ClickUp ' + (c.sent_ago || 'never') + ' · comments last pulled ' + (c.pulled_ago || 'never')) + '</p>';
    return h;
  }

  function noteHtml(it) {
    var n = it.note;
    if (!n) return '<p class="t-dr-none">' + esc(it.note_none) + '</p>';
    return '<blockquote class="t-dr-quote note">' + esc(n.text) + (n.cut ? ' <i>(shortened)</i>' : '') + '</blockquote>' +
      '<p class="t-dr-meta">' + esc(n.by + ' · ' + (n.ago || '') + ' · ' + n.source + ' · the time shown is ' + n.stamp) + '</p>';
  }

  function itemBody(it, stale) {
    var o = it.outcome || {};
    var h = (stale ? '<p class="t-dr-stale">This is no longer among the rows the page just read - it was read at ' +
      esc(fmtTime(S.data && S.data.generated_at)) + '. Close and reopen the list for the current picture.</p>' : '');
    h += '<div class="t-dr-state" data-sev="' + esc(sevOf(o.severity)) + '"><span class="t-badge">' + esc(it.badge) + '</span>' +
      '<span class="t-dr-statetext"><b>' + esc(o.label) + '</b><span>' + esc(o.meaning) + '</span></span></div>';
    h += '<dl class="t-dr-facts">' + (it.facts || []).map(function (f) {
      return '<div class="f" data-tone="' + esc(f.tone || '') + '"><dt>' + esc(f.label) + '</dt>' +
        '<dd' + (f.at ? ' title="' + esc(f.at) + '"' : '') + '>' + esc(pretty(f.value)) + '</dd></div>';
    }).join('') + '</dl>';
    if (it.kind === 'lead') {
      h += cardHtml('What the AI made of it', aiHtml(it));
      h += cardHtml('ClickUp', clickupHtml(it));
      h += cardHtml('Last note pulled from ClickUp', noteHtml(it));
    } else {
      h += cardHtml('Notes', '<p class="t-dr-none">' + esc(it.note_none) + ' ' + esc(it.clickup_none) + '</p>');
    }
    return h;
  }

  // Redraw only when the content really changed (a poll every 15 s must not eat keyboard focus - lesson L-100).
  function paintDrawer(force) {
    if (!DR.open) return;
    var eyebrow = '', title = '', order = '', body = '', foot = details().privacy || '', sig;
    var backSpec = DR.back ? details().lists[DR.back] : null;
    if (DR.mode === 'list') {
      var spec = details().lists[DR.list];
      if (!spec) { closeDrawer(); return; }
      eyebrow = spec.kind === 'ticket' ? 'TICKETS' : 'LEADS';
      title = spec.title;
      order = spec.order + (spec.total != null ? '  ·  ' + spec.total + ' in total' : '');
      body = listBody(spec);
      sig = 'L' + JSON.stringify(spec);
    } else {
      var it = details().items[DR.ref] || DR.kept;
      if (!it) { closeDrawer(); return; }
      var stale = !details().items[DR.ref];
      if (!stale) DR.kept = it;
      eyebrow = it.eyebrow;
      title = it.title;
      order = it.outcome ? it.outcome.what : '';
      body = itemBody(it, stale);
      sig = 'I' + DR.ref + stale + JSON.stringify(it);
    }
    sig += '|' + DR.back;
    if (!force && sig === DR.sig) return;
    DR.sig = sig;
    $('#tDrEyebrow').textContent = eyebrow;
    $('#tDrTitle').textContent = title;
    $('#tDrOrder').textContent = pretty(order);
    $('#tDrBody').innerHTML = body;
    $('#tDrFoot').textContent = foot;
    var back = $('#tDrBack');
    back.hidden = !backSpec;
    if (backSpec) $('#tDrBackText').textContent = 'Back to “' + backSpec.title + '”';
    var key = DR.mode + ':' + (DR.mode === 'list' ? DR.list : DR.ref);
    if (key !== DR.scrolled) { DR.scrolled = key; $('#tDrBody').scrollTop = 0; }   // a poll must not scroll the panel back up
  }

  function openDrawer(mode, key, opener, back) {
    var dr = $('#tDrawer');
    if (!DR.open) DR.opener = opener || document.activeElement;
    DR.open = true; DR.mode = mode; DR.back = back || '';
    if (mode === 'list') { DR.list = key; DR.ref = ''; DR.kept = null; } else { DR.ref = key; }
    dr.classList.add('open');
    dr.setAttribute('aria-hidden', 'false');
    $('#tScrim').hidden = false;
    paintDrawer(true);
    if (!DR.open) return;                               // paintDrawer closed it again (nothing to show)
    setTimeout(function () { if (DR.open) $('#tDrClose').focus({ preventScroll: true }); }, 60);
  }
  function closeDrawer() {
    if (!DR.open) return;
    DR.open = false; DR.mode = ''; DR.list = ''; DR.ref = ''; DR.back = ''; DR.sig = ''; DR.kept = null;
    var dr = $('#tDrawer');
    dr.classList.remove('open');
    dr.setAttribute('aria-hidden', 'true');
    $('#tScrim').hidden = true;
    var o = DR.opener; DR.opener = null;
    // preventScroll: focusing an element while the drawer slides off-screen would scroll the whole view sideways (L-099)
    if (o && document.body.contains(o)) { try { o.focus({ preventScroll: true }); } catch (err) { /* detached */ } }
  }

  $('#tDrClose').addEventListener('click', closeDrawer);
  $('#tScrim').addEventListener('click', closeDrawer);
  $('#tDrBack').addEventListener('click', function () { if (DR.back) openDrawer('list', DR.back, DR.opener, ''); });

  // one delegated handler for every way into the drawer: a KPI tile, an attention row, a "since yesterday" chip,
  // a row inside the drawer, and the "other half of this question" link at the foot of a list
  document.addEventListener('click', function (e) {
    if (!e.target.closest) return;
    var d = e.target.closest('#view-today [data-detail]');
    if (d) {
      // following the "other half of the question" link from inside a list keeps a way back to that list
      var from = (DR.mode === 'list' && d.closest('#tDrawer')) ? DR.list : '';
      e.preventDefault();
      openDrawer('list', d.getAttribute('data-detail'), d, from);
      return;
    }
    var r = e.target.closest('#view-today [data-ref]');
    if (r) { e.preventDefault(); openDrawer('item', r.getAttribute('data-ref'), r, DR.mode === 'list' ? DR.list : ''); }
  });

  // Escape closes it, Tab stays inside it (capture, so the page's own shortcuts never see the key first).
  document.addEventListener('keydown', function (e) {
    if (!DR.open) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDrawer(); return; }
    if (e.key !== 'Tab') return;
    var dr = $('#tDrawer');
    var f = Array.prototype.filter.call(dr.querySelectorAll('a[href],button:not([disabled]),[tabindex]:not([tabindex="-1"])'),
                                        function (el) { return !el.hidden && el.offsetParent !== null; });
    if (!f.length) { e.preventDefault(); dr.focus({ preventScroll: true }); return; }
    var first = f[0], last = f[f.length - 1], a = document.activeElement, inside = dr.contains(a);
    if (e.shiftKey && (a === first || !inside)) { e.preventDefault(); last.focus({ preventScroll: true }); }
    else if (!e.shiftKey && (a === last || !inside)) { e.preventDefault(); first.focus({ preventScroll: true }); }
  }, true);

  function renderAll(d) {
    renderHero(d);
    renderSince(d);
    renderKpis(d);
    renderAttention(d);
    renderHealth(d);
    renderRules(d);
    paintDrawer(false);
    var ok = (d.sources && d.sources.leads === 'ok' && d.sources.tickets === 'ok');
    $('#tFoot').textContent = 'erp_support · PostgreSQL · read ' + fmtTime(d.generated_at) + (ok ? '' : ' · some sources are unavailable');
  }

  // ------------------------------------------------------------------ loading
  function active() { return D.view() === 'today'; }
  function play() {
    if (S.played) return;
    S.played = true;
    $('#today').classList.add('play');
  }
  // One request at a time, and never one that can hang: the fetch is aborted after FETCH_TIMEOUT_MS (the race also settles
  // if the browser ignores the abort), so `busy` is always released and the pill says STALE instead of SYNCING forever.
  function fetchToday(fresh) {
    var ctl = typeof AbortController === 'function' ? new AbortController() : null;
    var timer;
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () {
        var e = new Error('timeout'); e.timeout = true;
        reject(e);
        if (ctl) ctl.abort();
      }, FETCH_TIMEOUT_MS);
    });
    var req = D.api.get('/api/today' + (fresh ? '?fresh=1' : ''), ctl ? { signal: ctl.signal } : undefined);
    return Promise.race([req, timeout]).then(function (d) { clearTimeout(timer); return d; },
                                             function (e) { clearTimeout(timer); throw e; });
  }
  function load(fresh) {
    if (S.busy) {                                    // never stack requests: a Refresh that arrives meanwhile re-runs once afterwards
      if (fresh) S.again = true;
      return S.inflight || Promise.resolve();
    }
    S.busy = true; S.again = false;
    clearTimeout(S.timer);
    setLive('syncing');
    S.inflight = fetchToday(fresh).then(function (d) {
      var gen = Date.parse(d.generated_at);
      S.data = d; S.fails = 0; S.err = '';
      S.at = isNaN(gen) ? Date.now() : Math.min(Date.now(), gen);            // "updated" = when the server read the database
      var old = Date.now() - S.at > OLD_AFTER_MS;                            // the server had to serve its last payload (slow build)
      if (old) S.err = 'The database read is taking too long';
      if (d.ok && !old) setLive('live');
      else if (old) setLive('stale');
      else setLive('stale', 'PARTIAL');                                      // amber: the page works but some source could not be read
      renderAll(d);
      tickAgo();
      if (fresh) D.toast(d.ok && !old ? 'Today refreshed' : old ? 'Refreshed - the database is slow, showing the last numbers' : 'Refreshed - some numbers are unavailable', d.ok && !old ? 'ok' : 'warn');
    }).catch(function (e) {
      var slow = !!(e && (e.timeout || e.name === 'AbortError'));
      S.fails++;
      S.err = slow ? 'The data service did not answer within ' + (FETCH_TIMEOUT_MS / 1000) + ' s' : 'Could not reach the data service';
      setLive(slow || (S.data && S.fails < 3) ? 'stale' : 'offline');
      if (S.data) { eyebrow(); tickAgo(); }
      else {
        var h = $('#tHero'); h.setAttribute('data-level', 'unknown'); h.classList.remove('loading');
        $('#tOrb').innerHTML = ICON.unknown;
        $('#tEyebrow').textContent = 'STATUS · CANNOT READ THE NUMBERS';
        $('#tSentence').textContent = slow ? 'Today’s numbers are taking too long' : 'Can’t reach ERP Desk’s data service';
        $('#tSub').textContent = slow ? 'The data service did not answer in ' + (FETCH_TIMEOUT_MS / 1000) + ' seconds. This page keeps trying by itself.'
                                      : 'The app may be shutting down. Try Refresh in a moment.';
        renderCaveat(null);
      }
      if (fresh) D.toast(slow ? 'The Today feed is slow - still showing the last numbers' : 'Could not reach the Today feed', 'bad');
    }).then(function () {
      S.busy = false;
      if (S.again) { S.again = false; return load(true); }
      schedule();
    });
    return S.inflight;
  }
  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { if (active() && !document.hidden) load(false); else schedule(); }, POLL_MS);
  }

  $('#tRefresh').addEventListener('click', function () {
    var b = this;
    b.classList.add('spinning'); b.disabled = true;
    load(true).then(function () { setTimeout(function () { b.classList.remove('spinning'); b.disabled = false; }, 400); });
  });
  document.addEventListener('click', function (e) {          // "Open Reporting" / "See Data Flow" buttons
    var t = e.target.closest ? e.target.closest('#today [data-go]') : null;
    if (t) D.go(t.getAttribute('data-go'));
  });
  D.on('refresh-request', function () { if (active()) $('#tRefresh').click(); });
  D.on('ready', play);
  D.on('view', function (name) {
    if (name !== 'today') { closeDrawer(); return; }
    play();
    if (!S.data || Date.now() - S.at > 5000) load(false);
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && active() && Date.now() - S.at > POLL_MS) load(false); });
  setInterval(tickAgo, 1000);
  setTimeout(play, 6000);   // safety net: never leave the page invisible if the splash event was missed

  setLive('connecting');
  load(false);
})();
