/* ERP Desk - Health page. Draws the JSON of GET /api/health (desktop/health_data.py).
   Nothing is decided here: every state, colour, sentence and fix hint is computed on the server. This file lays
   them out, polls, and copies a fix hint to the clipboard. It never calls a state-changing endpoint - the page
   that tells you how to fix things must not be able to touch them. All server text goes through esc(). */
(function () {
  'use strict';
  var D = window.Desk;
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var esc = D.esc;
  var POLL_MS = 15000;
  var FETCH_TIMEOUT_MS = 20000;          // the feed composes two cached snapshots; 20 s is far past any real build
  var OLD_AFTER_MS = POLL_MS * 3;
  var S = { data: null, at: 0, timer: null, played: false, sig: {}, busy: false, again: false, inflight: null,
            live: 'connecting', old: false, err: '' };

  var ICON = {
    ok: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    alert: '<svg viewBox="0 0 24 24"><path d="M12 6.5v7M12 17.2v.3"/></svg>',
    watch: '<svg viewBox="0 0 24 24"><path d="M12 7v6l3.5 2"/></svg>',
    unknown: '<svg viewBox="0 0 24 24"><path d="M9.5 9.2a2.7 2.7 0 1 1 3.6 2.6c-.8.4-1.1.9-1.1 1.7M12 17.3v.2"/></svg>',
    hand: '<svg viewBox="0 0 24 24"><path d="M12 3v9M6.4 6.6a8 8 0 1 0 11.2 0"/></svg>',
    clock: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 1.8"/></svg>',
    tick: '<svg viewBox="0 0 24 24"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
    copy: '<svg viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2.5"/><path d="M15 6.5A2.5 2.5 0 0 0 12.5 4h-6A2.5 2.5 0 0 0 4 6.5v6A2.5 2.5 0 0 0 6.5 15"/></svg>',
    arrow: '<svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg>'
  };
  // what kind of fix it is - a shape you can recognise before you read it
  var KIND_ICON = {
    command: '<svg viewBox="0 0 24 24"><path d="M6 8.5l3.5 3.5L6 15.5M12.5 16h5.5"/><rect x="2.5" y="4" width="19" height="16" rx="3"/></svg>',
    config: '<svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M12 3v2.2M12 18.8V21M3 12h2.2M18.8 12H21M5.6 5.6l1.6 1.6M16.8 16.8l1.6 1.6M5.6 18.4l1.6-1.6M16.8 7.2l1.6-1.6"/></svg>',
    click: '<svg viewBox="0 0 24 24"><path d="M6.5 4.5l12 6.2-5 1.6-1.7 5z"/><path d="M14 14l5 5"/></svg>',
    owner: '<svg viewBox="0 0 24 24"><circle cx="12" cy="8.5" r="3.5"/><path d="M5 20c.7-3.6 3.5-5.5 7-5.5s6.3 1.9 7 5.5"/></svg>'
  };
  var KIND_LABEL = { command: 'Run this yourself', config: 'Set this in .env', click: 'Do this in the app', owner: 'Needs the owner' };
  var LEVEL_LABEL = { ok: 'ALL CLEAR', attention: 'SOMETHING IS BROKEN', watch: 'SOMETHING NEEDS A LOOK',
                      unknown: 'CANNOT CHECK', loading: 'CHECKING' };
  var STATE_TAG = { ok: 'HEALTHY', warn: 'CHECK', bad: 'DOWN', off: 'NOT SET UP', unknown: 'UNKNOWN' };

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
  function pretty(s) { return String(s == null ? '' : s).replace(/ - /g, ' – '); }

  // ------------------------------------------------------------------ live pill
  function setLive(state, label) {
    S.live = state;
    var p = $('#hxPill');
    p.setAttribute('data-state', state);
    S.old = state === 'stale' || state === 'offline';
    $('#hxLive').textContent = label || { live: 'LIVE', syncing: 'SYNCING', stale: 'STALE', offline: 'OFFLINE', connecting: 'CONNECTING' }[state] || state.toUpperCase();
  }
  function tickAgo() {
    var el = $('#hxAgo');
    if (!S.at) { el.textContent = ''; return; }
    el.textContent = 'updated ' + ago(S.at);
    $('#hxPill').title = (S.old && S.err ? S.err + '. Showing the checks read at ' + fmtTime(S.data && S.data.generated_at) + '. ' :
      'Composed at ' + fmtTime(S.data && S.data.generated_at) + ' from checks made at ' + (fmtTime(S.data && S.data.checked_at) || 'an unknown time') + ' - ') +
      'This page looks again every ' + (POLL_MS / 1000) + ' s while it is open';
  }

  // ------------------------------------------------------------------ hero
  function renderHero(d) {
    var h = d.headline || { level: 'unknown', text: 'Still checking', rest: '', sub: '' };
    var hero = $('#hxHero');
    hero.setAttribute('data-level', h.level || 'unknown');
    hero.classList.remove('loading');
    $('#hxOrb').innerHTML = ICON[h.level === 'ok' ? 'ok' : h.level === 'attention' ? 'alert' : h.level === 'watch' ? 'watch' : 'unknown'];
    $('#hxEyebrow').textContent = 'SYSTEM · ' + (LEVEL_LABEL[h.level] || '') + (S.old ? ' · AS OF ' + fmtTime(d.generated_at) : '');
    $('#hxSentence').innerHTML = '<span class="lead">' + esc(pretty(h.text)) + '</span>' +
      (h.rest ? '<span class="rest">' + esc(pretty(h.rest)) + '</span>' : '');
    $('#hxSub').textContent = pretty(h.sub || '');
    $('#hxNever').textContent = d.never_runs || '';
    var t = d.today || {};
    var box = $('#hxToday');
    box.setAttribute('data-level', t.level || 'unknown');
    $('#hxTodayText').textContent = t.headline ? pretty(t.headline) : 'not read yet';
  }

  // ------------------------------------------------------------------ signal row
  function renderSignals(d) {
    var sig = JSON.stringify((d.cards || []).map(function (c) { return [c.id, c.label, c.state, c.attention]; }));
    if (S.sig.signals === sig) return;
    S.sig.signals = sig;
    $('#hxSignals').innerHTML = (d.cards || []).map(function (c) {
      return '<button type="button" class="hx-sig" data-state="' + esc(c.state) + '" data-jump="' + esc(c.id) + '"' +
        ' aria-label="' + esc(c.label + ' - ' + (STATE_TAG[c.state] || c.state) + '. Jump to its card.') + '">' +
        '<i aria-hidden="true"></i>' + esc(c.label) + '</button>';
    }).join('');
  }

  // ------------------------------------------------------------------ what this means for the numbers
  function renderNumbers(d) {
    var n = d.numbers || { complete: true, items: [], text: '', note: '' };
    var sig = JSON.stringify(n);
    if (S.sig.numbers === sig) return;
    S.sig.numbers = sig;
    var p = $('#hxNumbers');
    p.setAttribute('data-complete', n.complete ? 'true' : 'false');
    var html = '<p class="hx-num-text">' + esc(pretty(n.text)) + '</p>';
    if (n.items && n.items.length) {
      html += '<ul class="hx-num-list">' + n.items.map(function (it) {
        return '<li class="hx-num-item"><b>' + esc(pretty(it.clause).replace(/^./, function (m) { return m.toUpperCase(); })) + '</b>' +
          (it.affects && it.affects.length ? '<ul>' + it.affects.map(function (a) { return '<li>' + esc(pretty(a)) + '</li>'; }).join('') + '</ul>' : '') +
          '</li>';
      }).join('') + '</ul>';
    }
    html += '<p class="hx-note">' + esc(n.note || '') + '</p>';
    $('#hxNumBody').innerHTML = html;
  }

  // ------------------------------------------------------------------ cards
  function fixHtml(f) {
    var kind = KIND_ICON[f.kind] ? f.kind : 'click';
    return '<li class="hx-fix" data-kind="' + esc(kind) + '">' +
      '<span class="hx-kind" title="' + esc(KIND_LABEL[kind] || '') + '" aria-hidden="true">' + KIND_ICON[kind] + '</span>' +
      '<span class="hx-fix-text">' + esc(f.text) + '</span>' +
      (f.copy ? '<button type="button" class="hx-copy" data-copy="' + esc(f.text) + '" aria-label="' + esc('Copy: ' + f.text) + '">' + ICON.copy + 'Copy</button>' : '<span></span>') +
      (f.note ? '<span class="hx-fix-note">' + esc(pretty(f.note)) + '</span>' : '') +
      '</li>';
  }

  function cardHtml(c) {
    var h = '<article class="hx-card" id="hxc-' + esc(c.id) + '" data-state="' + esc(c.state) + '" data-attention="' + (c.attention ? '1' : '0') + '">' +
      '<div class="hx-card-top"><span class="hx-dot" aria-hidden="true"></span>' +
      '<span class="hx-word">' + esc(c.word) + '</span>' +
      // the server decides whether the severity badge says anything the word does not (health_data._tag)
      (c.tag ? '<span class="hx-tag">' + esc(c.tag) + '</span>' : '') +
      (c.attention ? '<span class="hx-need">NEEDS ATTENTION</span>' : '') + '</div>' +
      '<h3 class="hx-name">' + esc(c.label) + '</h3>' +
      (c.where || c.sub ? '<p class="hx-sub2">' + (c.where ? '<code>' + esc(c.where) + '</code>' : '') +
        (c.where && c.sub ? ' \u00b7 ' : '') + esc(c.sub || '') + '</p>' : '') +
      '<p class="hx-why">' + esc(pretty(c.why)) + '</p>' +
      (c.detail ? '<p class="hx-detail">' + esc(pretty(c.detail)) + '</p>' : '') +
      (c.extra ? '<p class="hx-detail">' + esc(pretty(c.extra)) + '</p>' : '') +
      // the relative-time text (checked_text/ok_text) ticks over about once a minute on its own; it is kept OUT of the
      // grid's redraw signature below (lesson L-100) and instead refreshed in place by updateCardMeta() on every poll,
      // so a tick-over never rebuilds the grid and never discards focus or a 'Copied' confirmation elsewhere on the page
      '<div class="hx-meta"><span class="hx-meta-checked">' + ICON.clock + '<b>' + esc(c.checked_text || '') + '</b></span>' +
      '<span class="hx-meta-ok">' + ICON.tick + '<b>' + esc(c.ok_text || '') + '</b></span></div>';
    if (c.impact) h += '<div class="hx-impact"><b>' + esc(c.impact_title || 'If this is down') + '</b>' + esc(pretty(c.impact)) + '</div>';
    if (c.fix && c.fix.length) {
      h += '<p class="hx-fix-h">' + esc(c.fix_title || 'What to do') + '</p><ul class="hx-fixes">' + c.fix.map(fixHtml).join('') + '</ul>';
    }
    return h + '</article>';
  }

  // The relative-time text on every card (checked_text/ok_text) changes about once a minute purely because "now"
  // moved on, even when nothing was actually re-checked. Comparing the whole card object (lesson L-100) therefore
  // rebuilds the entire grid roughly once a minute and throws away keyboard focus and any 'Copied' confirmation
  // mid-flash. `cardSigPart` strips those two fields before the redraw signature is computed; `updateCardMeta`
  // still keeps the displayed text current by patching just those two spans in place, on every poll.
  function cardSigPart(c) {
    var copy = {};
    for (var k in c) { if (k !== 'checked_text' && k !== 'ok_text') copy[k] = c[k]; }
    return copy;
  }
  function updateCardMeta(cards) {
    (cards || []).forEach(function (c) {
      var card = document.getElementById('hxc-' + c.id);
      if (!card) return;
      var chk = card.querySelector('.hx-meta-checked > b');
      if (chk && chk.textContent !== (c.checked_text || '')) chk.textContent = c.checked_text || '';
      var ok = card.querySelector('.hx-meta-ok > b');
      if (ok && ok.textContent !== (c.ok_text || '')) ok.textContent = c.ok_text || '';
    });
  }

  function renderGroups(d) {
    var sig = JSON.stringify([d.groups, (d.cards || []).map(cardSigPart)]);
    if (S.sig.groups !== sig) {
      S.sig.groups = sig;
      var byId = {};
      (d.cards || []).forEach(function (c) { byId[c.id] = c; });
      var box = $('#hxGroups');
      if (!(d.groups || []).length) {
        box.innerHTML = '<section class="hx-panel"><p class="hx-empty">No check has produced a verdict yet. ' +
          'The system checks run a few seconds after ERP Desk starts - press Refresh above.</p></section>';
        return;
      }
      box.innerHTML = (d.groups || []).map(function (g, gi) {
        var cards = (g.cards || []).map(function (id) { return byId[id]; }).filter(Boolean);
        return '<section class="hx-group hx-rise" style="--i:' + (5 + gi) + '" data-n="' + cards.length + '" aria-label="' + esc(g.title) + '">' +
          '<div class="hx-group-head"><h2>' + esc(g.title) + '</h2><span class="tag">' + esc(g.tag) + '</span>' +
          (g.attention ? '<span class="n" data-state="' + esc(g.attention_state || 'warn') + '">' + esc(g.attention) +
            ' need' + (g.attention === 1 ? 's' : '') + ' attention</span>' : '') +
          '</div><div class="hx-cards">' + cards.map(cardHtml).join('') + '</div></section>';
      }).join('');
    }
    // Runs whether or not the grid was just rebuilt: on a fresh build the spans already carry the right text, so
    // this is a harmless no-op write; on a skipped build it is the only thing that keeps the "ago" wording current.
    updateCardMeta(d.cards);
  }

  // ------------------------------------------------------------------ configuration
  function renderConfig(d) {
    var cfg = d.config || { available: false, rows: [], integrations: [] };
    var sig = JSON.stringify(cfg);
    if (S.sig.config === sig) return;
    S.sig.config = sig;
    if (!cfg.available) {
      $('#hxConfigBody').innerHTML = '<p class="hx-empty">The settings module could not be read' +
        (cfg.error ? ' (' + esc(cfg.error) + ')' : '') + ', so this section is empty. Every other card was read separately.</p>';
      return;
    }
    var html = '<div class="hx-int">' + (cfg.integrations || []).map(function (i) {
      return '<div class="hx-int-card" data-state="' + esc(i.state) + '">' +
        '<div class="hx-int-top"><b>' + esc(i.label) + '</b><span class="hx-mode">' + esc(i.mode) + '</span></div>' +
        '<p class="hx-int-why">' + esc(pretty(i.why)) + '</p>' +
        (i.missing && i.missing.length ? '<p class="hx-int-why" style="color:var(--ink-dim)">Missing: <code>' + i.missing.map(esc).join('</code>, <code>') + '</code></p>' : '') +
        '</div>';
    }).join('') + '</div>';
    html += '<table class="hx-keys"><thead><tr><th>Setting</th><th>What it is</th><th class="why">Without it</th><th>In .env</th></tr></thead><tbody>' +
      (cfg.rows || []).map(function (r) {
        var on = r.state === 'set';
        return '<tr><td><code>' + esc(r.key) + '</code></td><td>' + esc(r.label) + '</td>' +
          '<td class="why">' + esc(pretty(r.why)) + '</td>' +
          '<td><span class="hx-set" data-set="' + (on ? '1' : '0') + '"><i aria-hidden="true"></i>' + esc(r.state) + '</span></td></tr>';
      }).join('') + '</tbody></table>';
    html += '<p class="hx-note">' + esc(cfg.note || '') + ' Source: ' + esc(cfg.source || '') + '</p>';
    $('#hxConfigBody').innerHTML = html;
  }

  function renderRules(d) {
    var sig = JSON.stringify(d.rules || []);
    if (S.sig.rules === sig) return;
    S.sig.rules = sig;
    $('#hxRulesList').innerHTML = (d.rules || []).map(function (r) { return '<li>' + esc(pretty(r)) + '</li>'; }).join('');
  }

  // ------------------------------------------------------------------ copy + jump
  function copyText(text, btn) {
    var done = function () {
      btn.classList.add('done');
      btn.innerHTML = ICON.tick + 'Copied';
      setTimeout(function () { btn.classList.remove('done'); btn.innerHTML = ICON.copy + 'Copy'; }, 2000);
      D.toast('Copied: ' + text, 'ok', 2600);
    };
    var fallback = function () {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('readonly', '');
        ta.style.cssText = 'position:fixed;left:-2000px;top:0';
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand('copy');
        ta.remove();
        if (ok) { done(); return; }
      } catch (e) { /* ignore */ }
      D.toast('Could not copy automatically - select the text and press Ctrl+C', 'warn', 5000);
    };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, fallback);
    else fallback();
  }

  document.addEventListener('click', function (e) {
    var copy = e.target.closest && e.target.closest('.hx-copy');
    if (copy && $('#view-health').contains(copy)) { copyText(copy.getAttribute('data-copy') || '', copy); return; }
    var go = e.target.closest && e.target.closest('#hx [data-go]');
    if (go) { D.go(go.getAttribute('data-go')); return; }
    var jump = e.target.closest && e.target.closest('[data-jump]');
    if (jump && $('#view-health').contains(jump)) {
      var card = document.getElementById('hxc-' + jump.getAttribute('data-jump'));
      if (card) {
        card.scrollIntoView({ block: 'center', behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
        card.classList.remove('flash');
        void card.offsetWidth;
        card.classList.add('flash');
      }
    }
  });

  // ------------------------------------------------------------------ render + poll
  function renderAll(d) {
    S.data = d;
    renderHero(d);
    renderSignals(d);
    renderNumbers(d);
    renderGroups(d);
    renderConfig(d);
    renderRules(d);
    play();
  }
  function active() { return D.view() === 'health'; }
  function play() {                                  // never animate behind the splash (a #health hash at start-up)
    if (S.played) return;
    if (!document.body.classList.contains('ready')) { D.on('ready', play); return; }
    S.played = true;
    // The entrance rules hide .hx-rise until #hx has .play, so this class is what makes the page visible at all:
    // a requestAnimationFrame alone is not enough (it does not fire while the window paints nothing), so the same
    // class is also set from a timer. Setting it twice is harmless; never setting it would leave a blank page.
    var show = function () { $('#hx').classList.add('play'); };
    if (window.requestAnimationFrame) { requestAnimationFrame(show); setTimeout(show, 800); } else show();
  }

  function fetchHealth(fresh) {
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = null;
    var req = D.api.get('/api/health' + (fresh ? '?fresh=1' : ''), { signal: ctrl && ctrl.signal });
    var timeout = new Promise(function (resolve, reject) {
      timer = setTimeout(function () {
        if (ctrl) ctrl.abort();
        reject(new Error('the health feed did not answer within ' + (FETCH_TIMEOUT_MS / 1000) + ' s'));
      }, FETCH_TIMEOUT_MS);
    });
    return Promise.race([req, timeout]).then(function (d) { clearTimeout(timer); return d; },
                                             function (e) { clearTimeout(timer); throw e; });
  }

  function load(fresh) {
    if (S.busy) { S.again = S.again || !!fresh; return S.inflight || Promise.resolve(); }
    S.busy = true;
    if (S.data) setLive('syncing'); else setLive('connecting');
    S.inflight = fetchHealth(fresh).then(function (d) {
      S.err = '';
      var at = Date.parse(d && d.generated_at);
      S.at = isNaN(at) ? Date.now() : at;
      renderAll(d);
      setLive(Date.now() - S.at > OLD_AFTER_MS ? 'stale' : 'live');
      tickAgo();
    }).catch(function (e) {
      S.err = String((e && e.message) || e);
      setLive(S.data ? 'stale' : 'offline');
      if (!S.data) {
        $('#hxHero').setAttribute('data-level', 'unknown');
        $('#hxHero').classList.remove('loading');
        $('#hxOrb').innerHTML = ICON.unknown;
        $('#hxEyebrow').textContent = 'SYSTEM · CANNOT CHECK';
        $('#hxSentence').innerHTML = '<span class="lead">Can’t read the system checks</span>';
        $('#hxSub').textContent = 'ERP Desk itself is not answering (' + S.err + '). Press Refresh; if it keeps failing, see erp_desktop.log.';
        play();
      }
      tickAgo();
    }).then(function () {
      S.busy = false;
      S.inflight = null;
      if (S.again) { S.again = false; load(true); } else schedule();
    });
    return S.inflight;
  }

  function schedule() {
    clearTimeout(S.timer);
    S.timer = setTimeout(function () { if (active() && !document.hidden) load(false); else schedule(); }, POLL_MS);
  }

  $('#hxRefresh').addEventListener('click', function () {
    var b = this;
    b.classList.add('spinning');
    b.disabled = true;
    load(true).then(function () { setTimeout(function () { b.classList.remove('spinning'); b.disabled = false; }, 400); });
  });
  D.on('refresh-request', function () { if (active()) $('#hxRefresh').click(); });
  D.on('view', function (name) {
    if (name !== 'health') return;
    if (!S.data || Date.now() - S.at > POLL_MS) load(false); else schedule();
  });
  document.addEventListener('visibilitychange', function () { if (!document.hidden && active() && Date.now() - S.at > POLL_MS) load(false); });
  setInterval(tickAgo, 5000);
  setTimeout(play, 6000);   // safety net (as on Today): never leave the page invisible if 'ready' or the feed never arrives

  // L-080: this script loads after shell.js, so the first `view` event has already fired. Do the first load here.
  if (active()) load(false); else schedule();
})();
