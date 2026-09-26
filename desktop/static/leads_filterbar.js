/* ERP Desk - Leads page filter bar: the pure decisions behind the SALES REP / SOURCE pill strip's breakpoint
   (full row above ~1100px of content width vs a compact one-row scroller below it), its edge fade (shown only on a
   side that is genuinely clipped) and preserving each strip's scrollLeft across a filter-triggered rebuild, keyed by
   its stable `data-group` so two different groups' positions can never land on each other. See static/leads.js
   (updateFade, renderFilters) and static/leads.css (.fg-multi / .fg-scroll / @container ld).

   Audit finding, 2026-09-24: this logic had zero scenario-table coverage (lesson L-089) and lived entirely inline in
   leads.js, where a database-free Python test could not reach it. It is extracted here - no DOM, no `window`, no
   dependency on anything else in the page - purely so tests/leads_scenarios.py can run it for real under Node
   (`venv\\Scripts\\python.exe -B -m tests.leads_scenarios`, which shells out to `node`) without a browser. leads.js
   loads this file as a plain <script> BEFORE itself (desktop/static/shell.html) and calls the very same functions at
   runtime via `window.Desk.filterBar`, so the browser and the test suite can never exercise two different copies of
   the decision - the same reasoning as desktop/sla_words.py for wording, applied here to layout logic instead. */
(function (root, factory) {
  'use strict';
  if (typeof module !== 'undefined' && module.exports) {
    module.exports = factory();                                // Node: tests/leads_scenarios.py
  } else if (root) {
    root.Desk = root.Desk || {};
    root.Desk.filterBar = factory();                            // browser: static/leads.js
  }
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';

  // Mirrors static/leads.css `@container ld (min-width: 1100px) { .fgroup.fg-multi { flex-basis: 100%; } }`: at or
  // above this content width the SALES REP / SOURCE strip claims a full row of its own (so most or all of a dozen
  // pills are visible with no scrolling); below it, it shares a row with its neighbours and stays a compact
  // horizontally-scrolling strip. A CSS container query cannot be read from a Python test, so this number is a
  // second, independent copy of the CSS one for the test suite to pin - if you change one, change the other in the
  // same edit (grep "1100" in both files).
  var FULL_ROW_MIN_WIDTH = 1100;
  function fgMultiMode(containerWidthPx) {
    return containerWidthPx >= FULL_ROW_MIN_WIDTH ? 'full-row' : 'compact';
  }

  // Mirrors updateFade() in leads.js: a soft edge fade shows on a side only when the strip is genuinely clipped
  // there - i.e. there really is more content that scrolling further that way would reveal. A strip that fits
  // entirely (scrollWidth <= clientWidth, nothing to scroll) always measures both edges as "already at rest", so
  // neither fade ever shows for it - the same formula proves both "shown when clipped" and "never shown when not".
  var EDGE_SLACK_PX = 1;                     // sub-pixel/rounding slack, same tolerance updateFade() already used
  function computeFade(scrollLeft, clientWidth, scrollWidth) {
    var atStart = scrollLeft <= EDGE_SLACK_PX;
    var atEnd = scrollLeft + clientWidth >= scrollWidth - EDGE_SLACK_PX;
    return { fadeStart: !atStart, fadeEnd: !atEnd };
  }

  // Mirrors the save/restore pair around renderFilters()'s `box.innerHTML = html` rebuild: every live `.fg-scroll`
  // strip's scrollLeft is read BEFORE the rebuild, keyed by its stable `data-group` ('rep' / 'source' today, but any
  // future group works the same way) rather than DOM order or array index, then looked back up AFTER for the
  // matching group. Keying by group id - never position - is exactly what stops one group's saved offset from ever
  // being applied to the other group if a rebuild ever reorders, adds or removes a strip.
  function buildScrollMap(entries) {           // entries: [{group, scrollLeft}, ...], one per live .fg-scroll
    var map = {};
    (entries || []).forEach(function (e) { if (e && e.group != null) map[e.group] = e.scrollLeft; });
    return map;
  }
  function scrollLeftFor(map, group) {
    // Falsy (0, or the group missing entirely - e.g. it did not exist before this rebuild) means "nothing to
    // restore, leave the freshly-rendered strip at its default 0": the same `if (was) el.scrollLeft = was;`
    // shortcut renderFilters() uses, kept identical here so this function proves the real behaviour, not a tidier
    // one that the page does not actually run.
    var v = map ? map[group] : null;
    return v ? v : null;
  }

  return {
    FULL_ROW_MIN_WIDTH: FULL_ROW_MIN_WIDTH, fgMultiMode: fgMultiMode,
    EDGE_SLACK_PX: EDGE_SLACK_PX, computeFade: computeFade,
    buildScrollMap: buildScrollMap, scrollLeftFor: scrollLeftFor
  };
});
