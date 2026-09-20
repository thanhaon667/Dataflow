/* ERP Desk - Data Flow page, 3D mode.
   Draws the SAME live payload the 2D map uses (/api/flow -> nodes, edges, lanes, health, stats, edge mode, trace) as a
   cinematic WebGL2 scene. Nothing is invented here: geometry comes from the map's layout (col/row -> x/y, lane -> depth),
   colours and line styles come from the same trigger / health / automation vocabulary as the 2D legend, particle density
   and speed come from flow.js's own flowParams(), and the details drawer, tooltips, legend, coverage cards and the
   refresh cadence are flow.js's. flow.js owns the mode switch and calls Flow3D.mount / unmount / setData / tour.

   Art direction: "holographic glass". The lanes are glass panes stepped back into depth over a reflective grid floor;
   nodes are dark glass slabs with a glowing frame, a floating health beacon and a crisp HTML label; edges are emissive
   tubes with light particles running along them in the direction the data moves. Bloom, fog, soft-focus edges,
   chromatic aberration, vignette and grain finish the picture.

   Every string shown comes from the feed through textContent (or from flow.js's already-escaped tooltip builders). */
(function () {
  'use strict';
  var D = window.Desk, F = window.F3D;
  if (!D || !F || !F.Ctx || !F.glsl) return;
  var M = F.math, U = F.util, clamp = U.clamp, lerp = U.lerp, ease = U.ease, hex2lin = U.hex2lin;
  var K = 1 / 56;                       // world units per design pixel of the 2D layout
  var KY = 1.18;                        // vertical rows are spread a little wider than in 2D so the scene fills a wide stage
  var KH = 1.0;                         // slabs keep their 2D box height
  var $ = function (s, r) { return (r || document).querySelector(s); };
  var Flow3D = (window.Flow3D = {});

  var HEALTH = {
    healthy: ['#12b886', 0], stale: ['#f2b705', 1], error: ['#e0393e', 2], 'never-run': ['#8a8f98', 3], mocked: ['#0ea5c6', 4],
    offline: ['#f2b705', 3], idle: ['#8a8f98', 3], unavailable: ['#e0393e', 3], info: ['#8a8f98', 3]
  };
  var AUTO = { active: 0, partial: 1, available: 2, manual: 3, passive: 4, external: 5 };
  var EDGE_MODE = { live: 0, dormant: 1, unknown: 1, manual: 2, passive: 3 };
  var QUALITY = [
    { name: 'high', dpr: 2, samples: 4, bloom: 5, pmul: 1, trail: 3, mirror: true, soft: 1, dust: 240 },
    { name: 'medium', dpr: 1.5, samples: 2, bloom: 4, pmul: 0.65, trail: 2, mirror: true, soft: 1, dust: 140 },
    { name: 'low', dpr: 1, samples: 0, bloom: 3, pmul: 0.4, trail: 1, mirror: false, soft: 0.6, dust: 60 },
    { name: 'minimal', dpr: 0.75, samples: 0, bloom: 2, pmul: 0.25, trail: 0, mirror: false, soft: 0, dust: 0 }
  ];
  var FOG_COL = hex2lin('#0b0c12');
  var qLevel = 0;                        // survives page switches within a session: once the machine struggled, stay lower
  var qLocked = false;
  var qFailed = {};                      // levels that already proved too heavy on this machine: never climb back into them
  var savedPose = null;                  // camera pose survives page switches
  var availCache = null, lastError = '', slowDevice = false, lastTeardown = null, teardowns = 0;
  var R = null;                          // runtime of the current mount (null = not mounted)

  // ------------------------------------------------------------------ availability
  Flow3D.available = function () {
    if (slowDevice) return false;
    if (availCache !== null) return availCache;
    var ok = false;
    try {
      var c = document.createElement('canvas'), gl = c.getContext('webgl2');
      ok = !!gl;
      if (gl) { var ext = gl.getExtension('WEBGL_lose_context'); if (ext) ext.loseContext(); }
    } catch (e) { ok = false; }
    availCache = ok; if (!ok) lastError = 'WebGL2 is not available on this device';
    return ok;
  };
  Flow3D.lastError = function () { return lastError; };
  Flow3D.mounted = function () { return !!R; };
  Flow3D.touring = function () { return !!(R && R.tour); };

  // ------------------------------------------------------------------ mount / unmount
  Flow3D.mount = function (host, api) {
    if (R) return true;
    var view = $('.f3d-view', host);
    if (!view) { lastError = 'The 3D stage is missing'; return false; }
    var canvas = document.createElement('canvas');
    canvas.className = 'f3d-canvas'; canvas.tabIndex = -1;
    canvas.setAttribute('aria-hidden', 'true');
    view.insertBefore(canvas, view.firstChild);
    var gl = null;
    try { gl = canvas.getContext('webgl2', { alpha: false, antialias: false, depth: false, stencil: false, powerPreference: 'high-performance', preserveDrawingBuffer: false }); } catch (e) { gl = null; }
    if (!gl) { canvas.remove(); lastError = 'Could not create a WebGL2 context'; return false; }
    R = {
      host: host, api: api, view: view, canvas: canvas, gl: gl, labels: $('#f3dLabels'), cam: new F.OrbitCam(), time: 0, lastTs: 0, raf: 0, frames: 0,
      nodes: [], nodeById: {}, edges: [], edgeById: {}, lanes: [], adj: {}, sig: null, data: null, cssW: 0, cssH: 0, dpr: 1,
      hover: null, hoverKind: null, sel: null, focusFrom: null, preFocus: null, engaged: false, immersive: false,
      drag: null, pointers: {}, pinch: null, ptr: { x: -1, y: -1, dirty: false, inside: false }, tour: null,
      sparks: [], pulses: [], ripples: [], rings: [], prevTotals: {}, fade: 0, fadeGoal: 1, appear: 0, lost: false,
      fps: 0, fpsAcc: 0, fpsN: 0, fpsLow: 0, sinceQ: 0, t0: performance.now(), qLog: [], lastChip: 0, listeners: [], q: QUALITY[qLevel], intro: !savedPose,
      mirror: new Float32Array(16), ident: M.ident(new Float32Array(16)), tmp16: new Float32Array(16), tmp3: [0, 0, 0], floorY: -8, center: [0, 0, 0],
      bounds: null, home: null, stars: null, dustBase: null, edgeFlash: {}, lastResize: 0, seen: true, hold: false, holdT: 0, holdMax: 0
    };
    try { setupGL(); }
    catch (err) { console.warn('Flow3D: setup failed -', err && err.message); lastError = 'GPU setup failed: ' + (err && err.message ? String(err.message).slice(0, 140) : 'unknown'); teardown(); return false; }
    bindEvents();
    resizeCanvas(true);
    var data = api.data && api.data();
    if (data) setData(data);
    host.classList.add('on');
    var sel = api.selected && api.selected();
    if (sel && R.nodeById[sel]) Flow3D.onSelect(sel);          // the drawer stayed open while the scene was away: fly back to that node
    start();
    return true;
  };

  Flow3D.unmount = function () { if (R) teardown(); };

  function teardown() {
    var r = R; if (!r) return;
    if (r.tour) stopTour(true);
    if (r.raf) cancelAnimationFrame(r.raf);
    if (r.ro) r.ro.disconnect();
    if (r.io) r.io.disconnect();
    r.listeners.forEach(function (l) { l[0].removeEventListener(l[1], l[2], l[3]); });
    savedPose = r.preFocus || r.cam.pose();
    clearTimeout(r.qTimer);
    var lose = r.ctx && r.ctx.loseExt, td = { before: r.ctx ? r.ctx.stats() : null, after: null, contextLost: null };
    try { if (r.ctx && !r.lost) r.ctx.dispose(); } catch (e) { /* ignore */ }
    try { if (lose && !r.lost) lose.loseContext(); } catch (e2) { /* ignore */ }
    try { td.after = r.ctx ? r.ctx.stats() : null; td.contextLost = r.gl.isContextLost(); } catch (e3) { /* ignore */ }
    lastTeardown = td; teardowns++;
    if (r.canvas.parentNode) r.canvas.parentNode.removeChild(r.canvas);
    while (r.labels && r.labels.firstChild) r.labels.removeChild(r.labels.firstChild);
    r.host.classList.remove('on', 'touring', 'immersive');
    var fb = $('#f3dFull'); if (fb) { fb.setAttribute('aria-pressed', 'false'); var fs = fb.querySelector('span'); if (fs) fs.textContent = 'Immersive'; }
    var tip = $('#tip'); if (tip) tip.classList.remove('show');
    R = null;
  }

  // ------------------------------------------------------------------ GL setup (also re-run after a context restore)
  function setupGL() {
    var gl = R.gl, G = F.glsl, c = R.ctx = new F.Ctx(gl), P;
    P = R.P = {
      slab: c.program(G.modelVS, G.slabFS, 'slab'), beacon: c.program(G.modelVS, G.beaconFS, 'beacon'), solid: c.program(G.modelVS, G.solidFS, 'solid'),
      tube: c.program(G.tubeVS, G.tubeFS, 'tube'), part: c.program(G.particleVS, G.particleFS, 'particles'), stars: c.program(G.starsVS, G.starsFS, 'stars'),
      backdrop: c.program(G.fullVS, G.backdropFS, 'backdrop'), floor: c.program(G.modelVS, G.floorFS, 'floor'), pane: c.program(G.modelVS, G.paneFS, 'pane'),
      ring: c.program(G.modelVS, G.ringFS, 'ring')
    };
    R.g = { box: c.modelMesh(F.geo.box()), octa: c.modelMesh(F.geo.octa()), cone: c.modelMesh(F.geo.cone(14)), quad: c.modelMesh(F.geo.quad()), torus: c.modelMesh(F.geo.torus(1, 0.012, 96, 6)) };
    R.post = new F.Post(c);
    R.post.msaaOk = R.q.samples > 0;
    // dynamic point-sprite buffer (data particles, halos, sparks, dust)
    var cap = 9000;
    R.pb = { cap: cap, pos: new Float32Array(cap * 3), col: new Float32Array(cap * 4), size: new Float32Array(cap), n: 0 };
    R.pmesh = c.mesh({ attribs: [{ loc: 0, size: 3, data: R.pb.pos, dynamic: true }, { loc: 1, size: 4, data: R.pb.col, dynamic: true }, { loc: 2, size: 1, data: R.pb.size, dynamic: true }], mode: gl.POINTS, count: cap });
    // stars (a fixed sky, drawn around the camera)
    var ns = 1400, sd = new Float32Array(ns * 3), sc = new Float32Array(ns * 3), sx = new Float32Array(ns * 2), i, rnd = seeded(7);
    var pal = [hex2lin('#ffffff', 0.9), hex2lin('#a88fff', 1.0), hex2lin('#6f86ff', 1.0), hex2lin('#ff9a7a', 0.9)];
    for (i = 0; i < ns; i++) {
      var u = rnd() * 2 - 1, a = rnd() * Math.PI * 2, s = Math.sqrt(1 - u * u), col = pal[Math.floor(rnd() * rnd() * 4)], b = 0.35 + rnd() * rnd() * 1.1;
      sd[i * 3] = s * Math.cos(a); sd[i * 3 + 1] = u; sd[i * 3 + 2] = s * Math.sin(a);
      sc[i * 3] = col[0] * b; sc[i * 3 + 1] = col[1] * b; sc[i * 3 + 2] = col[2] * b; sx[i * 2] = 1.3 + rnd() * rnd() * 2.6; sx[i * 2 + 1] = rnd();
    }
    R.starMesh = c.mesh({ attribs: [{ loc: 0, size: 3, data: sd }, { loc: 1, size: 3, data: sc }, { loc: 2, size: 2, data: sx }], mode: gl.POINTS, count: ns });
    // nebula, baked once into an equirectangular texture (the backdrop only samples it)
    var nw = 512, nh = 256, nt = c.texture(nw, nh, false, { repeatS: true }), nf = c.fbo(nt), np = c.program(G.fullVS, G.nebulaFS, 'nebula');
    gl.viewport(0, 0, nw, nh); gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND); np.use(); gl.bindVertexArray(c.emptyVao); gl.drawArrays(gl.TRIANGLES, 0, 3);
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); c._del('fbos', nf); c._del('programs', np.p);
    R.nebTex = nt;
    R.dustBase = null;
  }
  function seeded(seed) { var s = seed >>> 0; return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
  function freeMesh(m) {
    if (!m) return; var c = R.ctx;
    m.bufs.forEach(function (b) { c._del('buffers', b); }); if (m.idxBuf) c._del('buffers', m.idxBuf); c._del('vaos', m.vao);
  }

  // ------------------------------------------------------------------ events
  function on(target, type, fn, opts) { target.addEventListener(type, fn, opts); R.listeners.push([target, type, fn, opts]); }
  function bindEvents() {
    var cv = R.canvas;
    on(cv, 'webglcontextlost', function (e) { e.preventDefault(); onLost(); });
    on(cv, 'webglcontextrestored', onRestored);
    on(cv, 'pointerdown', onDown);
    on(cv, 'pointermove', onMove);
    on(cv, 'pointerup', onUp);
    on(cv, 'pointercancel', onUp);
    on(cv, 'pointerleave', onLeave);
    on(cv, 'wheel', onWheel, { passive: false });
    on(cv, 'dblclick', function () { resetView(); });
    on(cv, 'contextmenu', function (e) { e.preventDefault(); });
    on(document, 'visibilitychange', function () { if (document.hidden) stop(); else { if (R) { R.lastTs = performance.now(); start(); } } });
    on(document, 'keydown', onKey);
    if (window.ResizeObserver) { R.ro = new ResizeObserver(function () { if (R) { clearTimeout(R.rzT); R.rzT = setTimeout(function () { if (R) resizeCanvas(false); }, 60); } }); R.ro.observe(R.view); }
    on(window, 'resize', function () { if (R) resizeCanvas(false); });
    // the stage can be scrolled out of view (the summary cards sit above it): no point rendering what nobody sees
    if (window.IntersectionObserver) {
      R.visible = true;
      var steps = []; for (var q = 0; q <= 20; q++) steps.push(q / 20);
      R.io = new IntersectionObserver(function (es) {
        if (!R) return; var en = es[es.length - 1], vh = window.innerHeight || 800;
        R.visible = en.isIntersecting && en.intersectionRatio > 0.015;
        // "seen": at least about half of what could be shown (the stage, or the window height if the stage is taller) is on screen
        R.seen = R.visible && en.intersectionRect.height >= 0.5 * Math.min(en.boundingClientRect.height, vh);
        if (R.visible) { R.lastTs = performance.now(); start(); } else stop();
      }, { threshold: steps });
      R.io.observe(R.host);
    } else R.seen = true;
  }

  // ------------------------------------------------------------------ resize / quality
  function resizeCanvas(force) {
    if (!R || R.lost) return;
    var rc = R.view.getBoundingClientRect(), w = Math.round(rc.width), h = Math.round(rc.height);
    if (w < 8 || h < 8) return;
    var dpr = Math.min(window.devicePixelRatio || 1, R.q.dpr), bw = Math.max(2, Math.round(w * dpr)), bh = Math.max(2, Math.round(h * dpr));
    if (!force && bw === R.canvas.width && bh === R.canvas.height && w === R.cssW) return;
    var wasHome = !R.home || nearHome();
    R.cssW = w; R.cssH = h; R.dpr = dpr; R.canvas.width = bw; R.canvas.height = bh;
    R.canvas.style.width = w + 'px'; R.canvas.style.height = h + 'px';
    R.post.msaaOk = R.q.samples > 0;
    R.post.resize(bw, bh, R.q.samples);
    if (R.bounds) {
      computeHome();
      if (R.intro && R.cam.tween) { var hm = R.home; R.cam.tween.to = { az: hm.az, pol: hm.pol, rad: hm.rad, t: hm.t.slice(), shx: 0, shy: 0 }; }     // the stage changed size while the fly-in runs: land on the framing for the new size
      else if (wasHome && !R.tour && R.sel == null && !R.cam.tween) { R.cam.set(R.home); }
    }
    R.cam.build(w / h);
  }
  function nearHome() { var h = R.home, c = R.cam; return !h || (Math.abs(c.g.rad - h.rad) < 0.4 && Math.abs(c.g.az - h.az) < 0.06); }
  function setQuality(level, why) {
    if (!R) return; level = clamp(level | 0, 0, QUALITY.length - 1); if (level === qLevel && R.q === QUALITY[level]) return;
    if (level > qLevel && R.climbTo === qLevel && R.time - R.climbAt < 90) qFailed[qLevel] = true;      // it went down again right after a climb: that level really is too heavy
    if (level < qLevel) { R.climbTo = level; R.climbAt = R.time; }
    qLevel = level; R.q = QUALITY[level]; R.calm = 0; R.qLog.push({ level: level, name: R.q.name, why: why || 'manual', fps: Math.round(R.fps) });
    R.sinceQ = 0; R.fpsLow = 0; R.crawl = 0;
    resizeCanvas(true);
  }

  // ------------------------------------------------------------------ building the scene from the payload
  function structSig(d) {
    return JSON.stringify([d.layout, d.lanes.map(function (l) { return [l.id, l.cols, l.color]; }),
      d.nodes.map(function (n) { return [n.id, n.lane, n.col, n.row, n.h, n.kind]; }), d.edges.map(function (e) { return [e.id, e.from, e.to, e.via]; })]);
  }
  function setData(data) {
    if (!R) return;
    if (R.lost) { R.pending = data; return; }
    var sig = structSig(data);
    if (sig !== R.sig) { R.sig = sig; buildScene(data); }
    applyData(data);
  }
  Flow3D.setData = setData;

  function buildScene(data) {
    var L = data.layout, nodes = data.nodes, edges = data.edges, lanes = data.lanes, c = R.ctx, i, api = R.api;
    R.edges.forEach(function (e) { freeMesh(e.mesh); });
    while (R.labels.firstChild) R.labels.removeChild(R.labels.firstChild);
    R.nodes = []; R.nodeById = {}; R.edges = []; R.edgeById = {}; R.lanes = []; R.adj = {}; R.rings = R.rings || [];
    var colPitch = (L.width - L.pad_x - L.pad_r - L.node_w) / (L.cols - 1), cx = L.width / 2, minTop = 1e9, maxBot = 0, laneIdx = {};
    lanes.forEach(function (ln, k) { laneIdx[ln.id] = k; });
    nodes.forEach(function (n) { var h = n.h || L.node_h, top = L.top + n.row * L.row_pitch; minTop = Math.min(minTop, top); maxBot = Math.max(maxBot, top + h); });
    var midY = (minTop + maxBot) / 2, nl = Math.max(1, lanes.length), step = 2.3;
    var b = { x0: 1e9, x1: -1e9, y0: 1e9, y1: -1e9, z0: 1e9, z1: -1e9 };
    nodes.forEach(function (n) {
      var h = n.h || L.node_h, top = L.top + n.row * L.row_pitch, li = laneIdx[n.lane] || 0;
      var kd = n.kind, depth = kd === 'store' ? 0.5 : (kd === 'source' || kd === 'external' ? 0.2 : 0.3);
      var o = {
        id: n.id, d: n, kind: kd, lane: li, x: (L.pad_x + n.col * colPitch + L.node_w / 2 - cx) * K, y: -(top + h / 2 - midY) * K * KY,
        z: ((nl - 1) / 2 - li) * step + Math.sin(n.row * 1.9 + li) * 0.28, w: L.node_w * K, h: h * K * KH, depth: depth, yaw: 0, dimT: 0, hoverT: 0, selT: 0, flashT: 0,
        seed: (Math.abs(Math.sin(n.row * 12.9 + n.col * 78.2 + li * 3.1)) * 43758.5453) % 1, appear: 0, appearAt: 0.15 + li * 0.32 + n.row * 0.05, sx: 0, sy: 0, rippleAt: 0.4 + Math.random() * 2.2,
        tint: [1, 1, 1], hc: [1, 1, 1], style: 3, auto: 3, glow: 0.8, err: 0, pulse: false, lastSig: null
      };
      R.nodes.push(o); R.nodeById[n.id] = o; R.adj[n.id] = { n: {}, e: {} }; R.adj[n.id].n[n.id] = 1;
      b.x0 = Math.min(b.x0, o.x - o.w / 2); b.x1 = Math.max(b.x1, o.x + o.w / 2); b.y0 = Math.min(b.y0, o.y - o.h / 2); b.y1 = Math.max(b.y1, o.y + o.h / 2 + 0.7);
      b.z0 = Math.min(b.z0, o.z - depth / 2); b.z1 = Math.max(b.z1, o.z + depth / 2);
    });
    R.bounds = b; R.floorY = b.y0 - 1.05; R.center = [(b.x0 + b.x1) / 2, (b.y0 + b.y1) / 2, (b.z0 + b.z1) / 2];
    // glass panes, one per lane
    lanes.forEach(function (ln, k) {
      var mine = R.nodes.filter(function (o) { return o.lane === k; });
      if (!mine.length) return;
      var x0 = Math.min.apply(null, mine.map(function (o) { return o.x - o.w / 2; })) - 0.75, x1 = Math.max.apply(null, mine.map(function (o) { return o.x + o.w / 2; })) + 0.75;
      var y1 = b.y1 + 0.95, y0 = Math.min.apply(null, mine.map(function (o) { return o.y - o.h / 2; })) - 1.0, z = Math.min.apply(null, mine.map(function (o) { return o.z - o.depth / 2; })) - 0.5;
      R.lanes.push({ d: ln, x0: x0, x1: x1, y0: y0, y1: y1, z: z, color: hex2lin(ln.color, 1.0), seed: k * 0.27, el: null, idx: k });
    });
    b.y1 += 0.4;       // room for the lane headers above the tallest node
    // edges
    var ports = {};
    edges.forEach(function (e) {
      var a = R.nodeById[e.from], t = R.nodeById[e.to]; if (!a || !t) return;
      var ca = a.d.col, cb = t.d.col, mode = 'h', sa, sb;
      if (ca === cb) {
        var lo = Math.min(a.y, t.y), hi = Math.max(a.y, t.y), blocked = false;
        R.nodes.forEach(function (o) { if (o !== a && o !== t && o.d.col === ca && o.lane === a.lane && o.y > lo && o.y < hi) blocked = true; });
        mode = blocked ? 'loop' : 'v'; sa = blocked ? 'r' : (a.y > t.y ? 'b' : 't'); sb = blocked ? 'r' : (a.y > t.y ? 't' : 'b');
      } else { sa = ca < cb ? 'r' : 'l'; sb = ca < cb ? 'l' : 'r'; }
      e._geo = { a: a, b: t, mode: mode, sa: sa, sb: sb };
      (ports[a.id + '|' + sa] = ports[a.id + '|' + sa] || []).push({ e: e, end: 'a', ref: t.y });
      (ports[t.id + '|' + sb] = ports[t.id + '|' + sb] || []).push({ e: e, end: 'b', ref: a.y });
    });
    Object.keys(ports).forEach(function (k) {
      var list = ports[k].sort(function (p, q) { return q.ref - p.ref; }), nd = R.nodeById[k.split('|')[0]], side = k.split('|')[1], n = list.length;
      list.forEach(function (p, j) {
        var f = (j + 1) / (n + 1);
        if (side === 'l' || side === 'r') p.e._geo['off' + p.end] = (nd.h / 2 - 0.16) - (nd.h - 0.32) * f;
        else p.e._geo['off' + p.end] = (f - 0.5) * (nd.w - 0.6);
      });
    });
    edges.forEach(function (e) {
      if (!e._geo) return;
      var g = e._geo, a = g.a, t = g.b, pts = [], tanA, tanB, pa, pb;
      if (g.mode === 'h') {
        var sgn = g.sa === 'r' ? 1 : -1;
        pa = [a.x + sgn * (a.w / 2 - 0.01), a.y + (g.offa || 0), a.z]; pb = [t.x - sgn * (t.w / 2 - 0.01), t.y + (g.offb || 0), t.z];
        pts.push(pa);
        (e.via || []).forEach(function (v) {
          var x = (L.pad_x + L.node_w / 2 + v[0] * colPitch - cx) * K, y = -(L.top + v[1] * L.row_pitch - midY) * K * KY, f = pb[0] === pa[0] ? 0.5 : clamp((x - pa[0]) / (pb[0] - pa[0]), 0, 1);
          pts.push([x, y, lerp(pa[2], pb[2], f) + 0.15]);
        });
        pts.push(pb); tanA = [sgn * Math.abs(pb[0] - pa[0]) * 0.55 / Math.max(1, pts.length - 1), 0, 0]; tanB = tanA.slice();
      } else if (g.mode === 'v') {
        var up = g.sa === 'b';
        pa = [a.x + (g.offa || 0), a.y + (up ? -1 : 1) * (a.h / 2 - 0.01), a.z]; pb = [t.x + (g.offb || 0), t.y + (up ? 1 : -1) * (t.h / 2 - 0.01), t.z];
        pts.push(pa, [(pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2, (pa[2] + pb[2]) / 2 + 0.4], pb); tanA = tanB = null;
      } else {
        pa = [a.x + a.w / 2 - 0.01, a.y + (g.offa || 0), a.z]; pb = [t.x + t.w / 2 - 0.01, t.y + (g.offb || 0), t.z];
        pts.push(pa, [Math.max(pa[0], pb[0]) + 1.5, (pa[1] + pb[1]) / 2, Math.max(a.z, t.z) + 1.1], pb); tanA = [1.6, 0, 0]; tanB = [-1.6, 0, 0];
      }
      var curve = hermite(pts, tanA, tanB, g.mode === 'loop'), rs = resample(curve);
      var eo = { id: e.id, d: e, a: a, b: t, P: rs.P, M: rs.M, len: rs.len, mesh: null, arrow: null, dimT: 0, hotT: 0, traceT: 0, appear: 0, ps: [], fp: null, col: [1, 1, 1], mode: 0, radius: 0.04, pulse: null, appearAt: 0.6 + R.edges.length * 0.05 };
      eo.mesh = c.mesh({ attribs: [{ loc: 0, size: 3, data: rs.pos }, { loc: 1, size: 3, data: rs.dir }, { loc: 2, size: 2, data: rs.tt }], index: rs.idx, count: rs.idx.length });
      eo.arrow = arrowMatrix(rs.P, rs.M);
      R.edges.push(eo); R.edgeById[e.id] = eo;
      R.adj[e.from].e[e.id] = 1; R.adj[e.from].n[e.to] = 1; R.adj[e.to].e[e.id] = 1; R.adj[e.to].n[e.from] = 1;
    });
    buildLabels(data);
    computeHome(); computeLimits();
    // intro / restore camera
    if (savedPose && !R.introDone) { R.cam.set(savedPose); R.fade = 0; R.appear = 3; R.intro = false; }
    else if (!R.introDone) {
      R.intro = true; holdUntilSeen(3.5); var h = R.home, s = { az: -1.05, pol: 0.62, rad: h.rad * 1.9, t: [h.t[0] + 3, h.t[1] + 1, h.t[2] - 4], shx: 0, shy: 0 };
      R.cam.set(s); R.cam.flyTo(h, { dur: 3.6, ease: ease.outQuint, roll0: 0.16, onDone: function () { if (R) R.intro = false; } });
    } else R.cam.set(R.home);
    R.introDone = true; R.cam.build((R.cssW || 16) / (R.cssH || 9));
    R.dustBase = null;
    if (R.sel && R.nodeById[R.sel]) { /* keep the selection */ } else R.sel = null;
  }

  // wait (at most `max` seconds of rendered time) for the stage to be about half on screen before the next cinematic moment starts
  function holdUntilSeen(max) { if (!R || R.seen) return; R.hold = true; R.holdT = 0; R.holdMax = max; }
  // bring the stage into the window (the summary cards sit above it, so on a laptop-sized window it starts below the fold)
  function ensureInView() {
    if (!R) return; var rc = R.host.getBoundingClientRect(), vh = window.innerHeight || 800;
    var shown = Math.min(rc.bottom, vh) - Math.max(rc.top, 0);
    if (shown >= 0.9 * Math.min(rc.height, vh - 16)) return;
    var calm = window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    try { R.host.scrollIntoView({ block: rc.height <= vh - 24 ? 'center' : 'start', behavior: calm ? 'auto' : 'smooth' }); } catch (e) { try { R.host.scrollIntoView(true); } catch (e2) { /* ignore */ } }
  }

  // cubic Hermite through control points; returns dense polyline [[x,y,z]...]
  function hermite(pts, tA, tB, loop) {
    var out = [], n = pts.length, tans = [], i, j;
    for (i = 0; i < n; i++) {
      if (i === 0) tans.push(tA ? tA : [(pts[1][0] - pts[0][0]) * 0.5, (pts[1][1] - pts[0][1]) * 0.5, (pts[1][2] - pts[0][2]) * 0.5]);
      else if (i === n - 1) tans.push(tB ? tB : [(pts[i][0] - pts[i - 1][0]) * 0.5, (pts[i][1] - pts[i - 1][1]) * 0.5, (pts[i][2] - pts[i - 1][2]) * 0.5]);
      else tans.push([(pts[i + 1][0] - pts[i - 1][0]) * 0.5, (pts[i + 1][1] - pts[i - 1][1]) * 0.5, (pts[i + 1][2] - pts[i - 1][2]) * 0.5]);
    }
    if (n === 3 && !tA) { tans[0] = [(pts[1][0] - pts[0][0]) * 0.9, (pts[1][1] - pts[0][1]) * 0.9, (pts[1][2] - pts[0][2]) * 0.9]; tans[2] = [(pts[2][0] - pts[1][0]) * 0.9, (pts[2][1] - pts[1][1]) * 0.9, (pts[2][2] - pts[1][2]) * 0.9]; }
    if (n === 3 && loop) { var s = tB[0]; tans[0] = [Math.abs(s) * 1.0, 0, 0]; tans[2] = [-Math.abs(s) * 1.0, 0, 0]; }
    for (i = 0; i < n - 1; i++) {
      var p0 = pts[i], p1 = pts[i + 1], m0 = tans[i], m1 = tans[i + 1];
      for (j = 0; j < 40; j++) {
        var t = j / 40, t2 = t * t, t3 = t2 * t, h00 = 2 * t3 - 3 * t2 + 1, h10 = t3 - 2 * t2 + t, h01 = -2 * t3 + 3 * t2, h11 = t3 - t2;
        out.push([h00 * p0[0] + h10 * m0[0] + h01 * p1[0] + h11 * m1[0], h00 * p0[1] + h10 * m0[1] + h01 * p1[1] + h11 * m1[1], h00 * p0[2] + h10 * m0[2] + h01 * p1[2] + h11 * m1[2]]);
      }
    }
    out.push(pts[n - 1].slice());
    return out;
  }
  // uniform arc-length resampling + tube vertex data (parallel-transport frames)
  function resample(curve) {
    var cum = [0], i, len;
    for (i = 1; i < curve.length; i++) cum.push(cum[i - 1] + Math.hypot(curve[i][0] - curve[i - 1][0], curve[i][1] - curve[i - 1][1], curve[i][2] - curve[i - 1][2]));
    len = cum[cum.length - 1] || 0.001;
    var Mn = clamp(Math.ceil(len / 0.2), 8, 200), P = new Float32Array(Mn * 3), j = 0;
    for (i = 0; i < Mn; i++) {
      var s = len * i / (Mn - 1); while (j < cum.length - 2 && cum[j + 1] < s) j++;
      var f = (s - cum[j]) / Math.max(1e-6, cum[j + 1] - cum[j]);
      P[i * 3] = lerp(curve[j][0], curve[j + 1][0], f); P[i * 3 + 1] = lerp(curve[j][1], curve[j + 1][1], f); P[i * 3 + 2] = lerp(curve[j][2], curve[j + 1][2], f);
    }
    var RS = 8, pos = new Float32Array(Mn * RS * 3), dir = new Float32Array(Mn * RS * 3), tt = new Float32Array(Mn * RS * 2), idx = new Uint16Array((Mn - 1) * RS * 6);
    var nrm = null;
    for (i = 0; i < Mn; i++) {
      var a = Math.max(0, i - 1), b = Math.min(Mn - 1, i + 1), tx = P[b * 3] - P[a * 3], ty = P[b * 3 + 1] - P[a * 3 + 1], tz = P[b * 3 + 2] - P[a * 3 + 2], tl = Math.hypot(tx, ty, tz) || 1; tx /= tl; ty /= tl; tz /= tl;
      if (!nrm) { nrm = [ty * 0 - tz * 1, tz * 0 - tx * 0, tx * 1 - ty * 0]; if (Math.hypot(nrm[0], nrm[1], nrm[2]) < 1e-3) nrm = [0, 0, 1]; }
      var d = nrm[0] * tx + nrm[1] * ty + nrm[2] * tz; nrm = [nrm[0] - tx * d, nrm[1] - ty * d, nrm[2] - tz * d]; var nl = Math.hypot(nrm[0], nrm[1], nrm[2]) || 1; nrm = [nrm[0] / nl, nrm[1] / nl, nrm[2] / nl];
      var bx = ty * nrm[2] - tz * nrm[1], by = tz * nrm[0] - tx * nrm[2], bz = tx * nrm[1] - ty * nrm[0];
      for (var k = 0; k < RS; k++) {
        var ang = k / RS * Math.PI * 2, ca = Math.cos(ang), sa = Math.sin(ang), o = (i * RS + k);
        pos[o * 3] = P[i * 3]; pos[o * 3 + 1] = P[i * 3 + 1]; pos[o * 3 + 2] = P[i * 3 + 2];
        dir[o * 3] = nrm[0] * ca + bx * sa; dir[o * 3 + 1] = nrm[1] * ca + by * sa; dir[o * 3 + 2] = nrm[2] * ca + bz * sa;
        tt[o * 2] = len * i / (Mn - 1); tt[o * 2 + 1] = i / (Mn - 1);
      }
    }
    var q = 0;
    for (i = 0; i < Mn - 1; i++) for (var m = 0; m < RS; m++) {
      var v00 = i * RS + m, v01 = i * RS + (m + 1) % RS, v10 = (i + 1) * RS + m, v11 = (i + 1) * RS + (m + 1) % RS;
      idx[q++] = v00; idx[q++] = v10; idx[q++] = v01; idx[q++] = v01; idx[q++] = v10; idx[q++] = v11;
    }
    return { P: P, M: Mn, len: len, pos: pos, dir: dir, tt: tt, idx: idx };
  }
  function tangentAt(P, Mn, i) { var a = Math.max(0, i - 1), b = Math.min(Mn - 1, i + 1), x = P[b * 3] - P[a * 3], y = P[b * 3 + 1] - P[a * 3 + 1], z = P[b * 3 + 2] - P[a * 3 + 2], l = Math.hypot(x, y, z) || 1; return [x / l, y / l, z / l]; }
  function arrowMatrix(P, Mn) {
    var T = tangentAt(P, Mn, Mn - 1), e = [P[(Mn - 1) * 3], P[(Mn - 1) * 3 + 1], P[(Mn - 1) * 3 + 2]], len = 0.36, r = 0.11;
    var ref = Math.abs(T[1]) > 0.9 ? [1, 0, 0] : [0, 1, 0], x = [T[1] * ref[2] - T[2] * ref[1], T[2] * ref[0] - T[0] * ref[2], T[0] * ref[1] - T[1] * ref[0]], xl = Math.hypot(x[0], x[1], x[2]) || 1; x = [x[0] / xl, x[1] / xl, x[2] / xl];
    var z = [T[1] * x[2] - T[2] * x[1], T[2] * x[0] - T[0] * x[2], T[0] * x[1] - T[1] * x[0]];
    var base = [e[0] - T[0] * (len + 0.02), e[1] - T[1] * (len + 0.02), e[2] - T[2] * (len + 0.02)];
    return M.basis(new Float32Array(16), base, [x[0] * r, x[1] * r, x[2] * r], [T[0] * len, T[1] * len, T[2] * len], [z[0] * r, z[1] * r, z[2] * r]);
  }
  function posAt(eo, t, out) {
    var f = clamp(t, 0, 1) * (eo.M - 1), i = Math.min(eo.M - 2, Math.floor(f)), k = f - i, P = eo.P;
    out[0] = lerp(P[i * 3], P[i * 3 + 3], k); out[1] = lerp(P[i * 3 + 1], P[i * 3 + 4], k); out[2] = lerp(P[i * 3 + 2], P[i * 3 + 5], k); return out;
  }

  // camera framing: find the radius (and re-centre the target) so that every pane corner and the whole bounding box
  // project inside the frame at the home angle - exact for the actual perspective, at any window aspect
  function framePts() {           // the corners of every lane pane plus the front of its nodes: what has to be inside the frame
    var pts = [];
    R.lanes.forEach(function (l) {
      var zf = l.z; R.nodes.forEach(function (o) { if (o.lane === l.idx) zf = Math.max(zf, o.z + o.depth / 2); });
      [l.x0, l.x1].forEach(function (x) { [l.y1, l.y0].forEach(function (y) { pts.push([x, y, l.z]); pts.push([x, y, zf]); }); });
    });
    return pts;
  }
  function fitPose(pose, pts, aspect, fov, mx, my) {
    var cam = new F.OrbitCam(), tmp = [0, 0, 0], i, it, t = pose.t.slice(), rad = pose.rad;
    cam.fov = fov;
    function fits(r) {
      cam.set({ az: pose.az, pol: pose.pol, rad: r, t: t, shx: 0, shy: 0 }); cam.update(0, 0); cam.build(aspect);
      for (var k = 0; k < pts.length; k++) { cam.project(pts[k][0], pts[k][1], pts[k][2], tmp); if (tmp[2] <= 0.1 || Math.abs(tmp[0] * 2 - 1) > mx || Math.abs(tmp[1] * 2 - 1) > my) return false; }
      return true;
    }
    for (it = 0; it < 3; it++) {
      var lo = 4, hi = 300;
      for (i = 0; i < 26; i++) { var mid = (lo + hi) / 2; if (fits(mid)) hi = mid; else lo = mid; }
      rad = hi; fits(rad);
      var x0 = 9, x1 = -9, y0 = 9, y1 = -9;
      pts.forEach(function (p) { cam.project(p[0], p[1], p[2], tmp); var X = tmp[0] * 2 - 1, Y = 1 - tmp[1] * 2; x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, Y); y1 = Math.max(y1, Y); });
      var cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, th = Math.tan(fov / 2), kx = rad * th * aspect, ky = rad * th;
      for (i = 0; i < 3; i++) t[i] += cam.right[i] * cx * kx * 0.85 + cam.up[i] * cy * ky * 0.85;
    }
    return { az: pose.az, pol: pose.pol, rad: rad, t: t, shx: 0, shy: 0 };
  }
  function computeHome() {
    var aspect = (R.cssW || 16) / (R.cssH || 9), base = { az: -0.26, pol: 1.24, rad: 30, t: [R.center[0], R.center[1], R.center[2]] };
    R.home = fitPose(base, framePts(), aspect, R.cam.fov, 0.95, 0.92);
  }
  function computeLimits() {
    var L = R.cam.limits, b = R.bounds, h = R.home;
    L.rad = [4.2, h.rad * 1.6]; L.box = [[b.x0 - 3, b.y0 - 2, b.z0 - 3], [b.x1 + 3, b.y1 + 2, b.z1 + 3]];
  }
  function resetView() {
    if (!R || !R.home) return; if (R.tour) stopTour(); R.sel != null && R.api.closeDrawer();
    R.preFocus = null; R.cam.flyTo(R.home, { dur: 1.3 });
  }

  // ------------------------------------------------------------------ per-refresh state (colours, styles, particles)
  function applyData(data) {
    var api = R.api, trig = api.trig, now = Date.now();
    R.data = data;
    data.nodes.forEach(function (n) {
      var o = R.nodeById[n.id]; if (!o) return;
      o.d = n; var h = n.health || {}, hs = HEALTH[h.state] || HEALTH.info, tk = n.trigger_kind;
      o.tint = hex2lin((trig[tk] || trig.manual).color, 1.0); o.hc = hex2lin(hs[0], 1.0); o.style = hs[1]; o.auto = AUTO[n.automation] != null ? AUTO[n.automation] : 3;
      o.err = (h.state === 'error' || h.state === 'unavailable') ? 1 : 0;
      o.pulse = h.state === 'healthy' && ((h.last_ok_at && now - Date.parse(h.last_ok_at) < 20 * 60000) || n.id === 'live_report');
      o.glow = (o.auto === 0 ? 1.0 : (o.auto === 1 ? 0.75 : 0.5)) + (o.pulse ? 0.35 : 0);
      updateLabel(o);
    });
    data.edges.forEach(function (e) {
      var eo = R.edgeById[e.id]; if (!eo) return;
      eo.d = e; eo.mode = EDGE_MODE[e.mode] != null ? EDGE_MODE[e.mode] : 1;
      var fp = api.flowParams(e); eo.fp = fp; eo.col = hex2lin(fp.color, 1.0);
      eo.radius = eo.mode === 0 ? 0.05 : (eo.mode === 1 ? 0.036 : (eo.mode === 2 ? 0.034 : 0.028));
      eo.act = api.edgeActivity(e);
      var want = fp.spacing ? clamp(Math.round(eo.len / (fp.spacing * K * 1.15)), 1, 14) : 0;
      if (eo.ps.length !== want) { eo.ps = []; for (var i = 0; i < want; i++) eo.ps.push({ t: (i + Math.random() * 0.5) / want }); }
      // a counter that went up since the last refresh = something really moved down this edge: shockwave
      var prev = R.prevTotals[e.id];
      if (prev != null && e.total != null && e.total > prev) emitPulse(eo, 'counter');
      R.prevTotals[e.id] = e.total;
    });
    applyFocusTargets();
  }
  function emitPulse(eo, why) { R.pulses.push({ eo: eo, t0: R.time, dur: clamp(eo.len * 0.16, 1.1, 2.0), why: why }); if (R.pulses.length > 24) R.pulses.shift(); }

  // ------------------------------------------------------------------ labels (HTML overlay: crisp text, textContent only)
  function buildLabels(data) {
    var api = R.api;
    R.nodes.forEach(function (o) {
      var el = document.createElement('button'); el.type = 'button'; el.className = 'l3n'; el.setAttribute('data-id', o.id);
      el.style.width = Math.round(o.w / K) + 'px'; el.style.height = Math.round(o.h / K) + 'px';
      var ic = document.createElement('span'); ic.className = 'l3-ic'; ic.innerHTML = api.icon(o.d.icon);           // static svg from flow.js's fixed icon table
      var tx = document.createElement('span'); tx.className = 'l3-txt'; var b = document.createElement('b'), em = document.createElement('em'); tx.appendChild(b); tx.appendChild(em);
      var bd = document.createElement('span'); bd.className = 'fn-badge'; var st = document.createElement('span'); st.className = 'l3-st';
      el.appendChild(ic); el.appendChild(tx); el.appendChild(bd); el.appendChild(st);
      el.addEventListener('focus', function () { if (R) { R.hover = o.id; R.hoverKind = 'node'; applyFocusTargets(); } });
      el.addEventListener('blur', function () { if (R && R.hover === o.id) { R.hover = null; R.hoverKind = null; applyFocusTargets(); } });
      el.addEventListener('click', function () { if (R) R.api.select(o.id); });
      R.labels.appendChild(el); o.el = el; o.elB = b; o.elEm = em; o.elBd = bd; o.elSt = st; o.elIc = ic; o.tf = '';
    });
    R.lanes.forEach(function (l) {
      var el = document.createElement('div'); el.className = 'l3-lane'; el.style.setProperty('--lc', l.d.color);
      var n = document.createElement('b'); n.textContent = l.d.n; var t = document.createElement('span'); t.className = 't'; t.textContent = l.d.title;
      var s = document.createElement('span'); s.className = 's'; s.textContent = l.d.tag;
      var tx = document.createElement('span'); tx.className = 'tx'; tx.appendChild(t); tx.appendChild(s);
      el.style.width = Math.max(120, Math.round((l.x1 - l.x0) / K - 14)) + 'px'; el.classList.toggle('wide', (l.x1 - l.x0) / K > 300);
      el.appendChild(n); el.appendChild(tx); R.labels.appendChild(el); l.el = el; l.tf = '';
    });
  }
  function updateLabel(o) {
    if (!o.el) return; var api = R.api, n = o.d, h = n.health || {}, tk = n.trigger_kind, armed = n.automation === 'active' || n.automation === 'partial' || n.automation === 'manual' || n.automation === 'passive' || n.automation === 'external';
    var hl = api.headline(n), sig = [n.label, hl, h.state, tk, n.automation].join('|');
    if (o.lastSig === sig) return; o.lastSig = sig;
    o.elB.textContent = n.label; o.elEm.textContent = hl; o.elSt.textContent = h.state === 'healthy' ? '' : (api.state[h.state] || '');
    o.elSt.setAttribute('data-state', h.state || 'info');
    o.elBd.className = 'fn-badge badge-' + tk + (armed ? '' : ' off'); o.elBd.innerHTML = api.trigGlyph(tk);
    o.el.style.setProperty('--c', (api.trig[tk] || api.trig.manual).color);
    o.el.setAttribute('aria-label', n.label + (hl ? ', ' + hl : '') + ', ' + (api.state[h.state] || h.state || ''));
    o.el.setAttribute('data-state', h.state || 'info'); o.el.setAttribute('data-auto', n.automation);
  }

  // ------------------------------------------------------------------ focus (hover / selection) targets
  function focusInfo() {
    var f = R.hover || R.sel; if (!f) return null;
    if (R.nodeById[f] && (R.hoverKind !== 'edge' || R.hover !== f)) return { n: R.adj[f].n, e: R.adj[f].e, node: f };
    var eo = R.edgeById[f]; if (eo) { var n = {}, e = {}; n[eo.a.id] = 1; n[eo.b.id] = 1; e[f] = 1; return { n: n, e: e, edge: f }; }
    return null;
  }
  function applyFocusTargets() { if (R) R.fi = focusInfo(); }

  // ------------------------------------------------------------------ frame loop
  function start() { if (!R || R.raf || R.lost || document.hidden || R.visible === false) return; R.lastTs = performance.now(); R.raf = requestAnimationFrame(frame); }
  function stop() { if (R && R.raf) { cancelAnimationFrame(R.raf); R.raf = 0; } }
  Flow3D.pause = stop; Flow3D.resume = function () { if (R) { resizeCanvas(false); start(); } };

  function frame(ts) {
    if (!R) return;
    R.raf = requestAnimationFrame(frame);
    var raw = (ts - R.lastTs) / 1000; R.lastTs = ts; var dt = clamp(raw, 0, 0.05);
    // cinematic moments (the intro fly-in, a tour that was just started) wait until the stage is actually on screen, so they are not played to an empty window
    if (R.hold) { R.holdT += clamp(raw, 0, 0.1); if (R.seen || R.holdT > R.holdMax) R.hold = false; else dt = 0; }
    R.time += dt; R.frames++;
    meter(raw, dt);
    if (!R) return;                                   // meter() may have handed the page back to 2D (which unmounts us)
    try { update(dt); render(); }
    catch (err) {
      if (!R) return;
      if (R.lost || (R.gl && R.gl.isContextLost && R.gl.isContextLost())) return;
      R.errCount = (R.errCount || 0) + 1; if (R.errCount < 4) console.error('Flow3D frame error:', err);
      if (R.errCount > 8) { stop(); R.api.fail && R.api.fail('The 3D view hit a rendering error'); }
    }
  }
  function meter(raw, dt) {
    R.sinceQ += raw;
    // a single very long frame is a stall (tab switch, GC, window drag) and says nothing about the scene; a run of them is a slow machine
    if (raw > 0.25) { R.slowRun = (R.slowRun || 0) + 1; if (R.slowRun < 3) return; } else R.slowRun = 0;
    R.fpsAcc += raw; R.fpsN++;
    if (R.fpsAcc >= 0.5) {
      var fps = R.fpsN / R.fpsAcc; R.fps = R.fps ? lerp(R.fps, Math.min(fps, 240), 0.5) : Math.min(fps, 240); R.fpsAcc = 0; R.fpsN = 0;
      var bad = fps < 36, awful = fps < 15, up = (performance.now() - R.t0) / 1000;
      // below ~40 fps for two seconds (one second when it is really bad): drop pixel ratio, bloom levels, MSAA, particles, reflections
      if (!qLocked && up > 3.5 && R.frames > 8 && bad && qLevel < QUALITY.length - 1 && R.sinceQ > (awful ? 2 : 4.5)) { R.fpsLow++; if (R.fpsLow >= (awful ? 2 : 4)) setQuality(qLevel + 1, 'frame rate ' + Math.round(fps) + ' fps'); }
      else if (!bad) R.fpsLow = 0;
      // headroom: a stall (window drag, a busy moment at start-up) may have pushed the quality down for nothing, so after a long calm stretch at
      // full frame rate it climbs back one level - but never into a level that already failed
      if (!qLocked && qLevel > 0 && !qFailed[qLevel - 1] && fps >= 57 && up > 20) { R.calm = (R.calm || 0) + 0.5; if (R.calm >= 30) setQuality(qLevel - 1, 'headroom'); } else if (fps < 50) R.calm = 0;
      // even the lowest quality is a slideshow (software rendering, a dead GPU driver): hand the page back to the 2D map
      if (!qLocked && qLevel === QUALITY.length - 1 && fps < 10 && up > 14 && R.sinceQ > 7) { R.crawl = (R.crawl || 0) + 1; if (R.crawl >= 6) { slowDevice = true; availCache = false; lastError = 'This device renders 3D too slowly (' + Math.round(fps) + ' fps even at the lowest quality)'; R.api.fail && R.api.fail(lastError + '.'); return; } }
      else if (fps >= 10) R.crawl = 0;
      if (R.time - R.lastChip > 0.45 || R.frames < 3) { R.lastChip = R.time; var chip = $('#f3dFps'); if (chip) { chip.textContent = Math.round(R.fps) + ' fps · ' + R.q.name; chip.setAttribute('data-q', String(qLevel)); } }
    }
  }

  // ------------------------------------------------------------------ simulation
  function update(dt) {
    var t = R.time, cam = R.cam, i, k = 1 - Math.exp(-dt * 8), fi = R.fi;
    R.fade = lerp(R.fade, R.fadeGoal, 1 - Math.exp(-dt * (R.intro ? 1.6 : 4)));
    R.appear += dt;
    if (R.tour) updateTour(dt);
    cam.update(dt, t); cam.build(R.cssW / R.cssH);
    if (R.ptr.dirty && !R.drag && !R.tour) { R.ptr.dirty = false; hoverPick(); }
    R.nodes.forEach(function (o) {
      var tgtDim = fi && !fi.n[o.id] ? 1 : 0, tgtHov = R.hover === o.id && R.hoverKind === 'node' ? 1 : 0, tgtSel = R.sel === o.id ? 1 : 0;
      o.dimT = lerp(o.dimT, tgtDim, k); o.hoverT = lerp(o.hoverT, tgtHov, k); o.selT = lerp(o.selT, tgtSel, k); o.flashT = Math.max(0, o.flashT - dt * 1.3);
      var ap = clamp((R.appear - o.appearAt) / 0.9, 0, 1); o.appear = ease.outCubic(ap);
      var yaw = Math.atan2(cam.eye[0] - o.x, cam.eye[2] - o.z); o.yaw = clamp(yaw, -1.0, 1.0) * 0.82;
      if (o.pulse && t > o.rippleAt && ap >= 1) { o.rippleAt = t + 2.6 + o.seed * 1.4; R.ripples.push({ o: o, t0: t, dur: 2.4 }); if (R.ripples.length > 40) R.ripples.shift(); }
    });
    R.edges.forEach(function (eo) {
      var tgtDim = fi && !fi.e[eo.id] ? 1 : 0, tgtHot = fi && fi.e[eo.id] ? 1 : 0;
      eo.dimT = lerp(eo.dimT, tgtDim, k); eo.hotT = lerp(eo.hotT, tgtHot, k);
      eo.appear = ease.outCubic(clamp((R.appear - eo.appearAt) / 1.1, 0, 1));
      if (eo.fp && eo.ps.length) { var sp = eo.fp.speed * K * 1.25 * (R.fade > 0.05 ? 1 : 0); for (i = 0; i < eo.ps.length; i++) { var p = eo.ps[i]; p.t += sp * dt / eo.len; if (p.t > 1) p.t -= 1; } }
    });
    // pulses, ripples, sparks
    R.pulses = R.pulses.filter(function (p) {
      var age = (t - p.t0) / p.dur;
      if (age >= 1) { arrive(p.eo, p.why === 'tour'); return false; }
      return true;
    });
    R.ripples = R.ripples.filter(function (r) { return t - r.t0 < r.dur; });
    R.rings = R.rings.filter(function (r) { return t - r.t0 < r.dur; });
    for (i = R.sparks.length - 1; i >= 0; i--) {
      var s = R.sparks[i]; s.life -= dt; if (s.life <= 0) { R.sparks.splice(i, 1); continue; }
      var drag = Math.exp(-dt * 1.6); s.v[0] *= drag; s.v[1] *= drag; s.v[2] *= drag; s.p[0] += s.v[0] * dt; s.p[1] += s.v[1] * dt; s.p[2] += s.v[2] * dt;
    }
    // cool-down of the trace highlight after a tour
    if (!R.tour) R.edges.forEach(function (eo) { eo.traceT = Math.max(0, eo.traceT - dt * 0.8); });
    updateLabels();
  }
  // a pulse reached the far end of its edge
  function arrive(eo, fromTour) {
    var n = eo.b; n.flashT = 1;
    R.rings.push({ p: [n.x, n.y, n.z + n.depth / 2 + 0.05], t0: R.time, dur: 1.3, col: eo.col, size: 4.6, bill: true });
    var cnt = fromTour ? 34 : 22, i;
    for (i = 0; i < cnt; i++) {
      var a = Math.random() * Math.PI * 2, u = Math.random() * 2 - 1, sp = 1.4 + Math.random() * 2.6, r = Math.sqrt(1 - u * u);
      R.sparks.push({ p: [n.x, n.y, n.z + n.depth / 2], v: [Math.cos(a) * r * sp, u * sp, Math.sin(a) * r * sp * 0.8 + 0.6], life: 0.7 + Math.random() * 0.7, max: 1.4, col: eo.col });
    }
  }

  // ------------------------------------------------------------------ labels follow their slabs
  function updateLabels() {
    var cam = R.cam, W = R.cssW, H = R.cssH, tanH = Math.tan(cam.fov / 2), pxu = (H / 2) / tanH, tmp = R.tmp3, api = R.api, i;
    for (i = 0; i < R.nodes.length; i++) {
      var o = R.nodes[i]; if (!o.el) continue;
      cam.project(o.x, o.y, o.z + o.depth / 2 + 0.02, tmp);
      var s = pxu * K / Math.max(0.5, tmp[2]), x = tmp[0] * W, y = tmp[1] * H, vis = tmp[2] > 0.5 && o.appear > 0.5 && x > -160 && x < W + 160 && y > -100 && y < H + 100;
      var a = vis ? clamp(1.22 - tmp[2] / 85, 0.35, 1) * (1 - 0.74 * o.dimT) * clamp((o.appear - 0.5) * 2, 0, 1) * R.fade : 0;
      var tf = vis ? 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0) translate(-50%,-50%) scale(' + s.toFixed(3) + ')' : 'translate3d(-999px,-999px,0)';
      if (tf !== o.tf) { o.el.style.transform = tf; o.tf = tf; }
      var op = a.toFixed(2); if (op !== o.op) { o.el.style.opacity = op; o.op = op; }
      var hot = o.hoverT > 0.5 || o.selT > 0.5; if (hot !== o.hot) { o.el.classList.toggle('hot', hot); o.hot = hot; }
    }
    R.lanes.forEach(function (l) {
      if (!l.el) return; cam.project(l.x0 + 0.25, l.y1 - 0.15, l.z, tmp);
      var s = pxu * K / Math.max(0.5, tmp[2]), x = tmp[0] * W, y = tmp[1] * H, vis = tmp[2] > 0.5;
      var tf = vis ? 'translate3d(' + x.toFixed(1) + 'px,' + y.toFixed(1) + 'px,0) scale(' + s.toFixed(3) + ')' : 'translate3d(-999px,-999px,0)';
      if (tf !== l.tf) { l.el.style.transform = tf; l.tf = tf; }
      var op = (clamp(R.appear * 0.8 - l.idx * 0.25, 0, 1) * R.fade * (R.fi ? 0.55 : 1)).toFixed(2); if (op !== l.op) { l.el.style.opacity = op; l.op = op; }
    });
  }

  // ------------------------------------------------------------------ render
  function common(P, mirrorOn) {
    var cam = R.cam; P.use();
    P.m4('uVP', cam.vp).f3('uCam', cam.eye).f3('uFogCol', FOG_COL).f1('uFog', 0.0105).f1('uTime', R.time).m4('uMirror', mirrorOn ? R.mirror : R.ident).f1('uMirrorOn', mirrorOn ? 1 : 0).f1('uFloorY', R.floorY);
    return P;
  }
  function render() {
    var gl = R.gl, P = R.P, cam = R.cam, q = R.q, post = R.post, t = R.time, i;
    if (!R.bounds) { gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, R.canvas.width, R.canvas.height); gl.clearColor(0.03, 0.03, 0.05, 1); gl.clear(gl.COLOR_BUFFER_BIT); return; }
    M.mirrorY(R.mirror, R.floorY);
    fillParticles();
    post.beginScene();
    // backdrop + stars
    gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.depthMask(false);
    var tanY = Math.tan(cam.fov / 2), asp = R.cssW / R.cssH;
    P.backdrop.use().tex('uNeb', 0, R.nebTex).f3('uRight', cam.right).f3('uUp', cam.up).f3('uFwd', cam.fwd).f2('uTan', tanY * asp, tanY).f2('uShift', cam.shx, cam.shy).f1('uTime', t).f1('uAspect', asp);
    post.full();
    gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE);
    P.stars.use().m4('uVP', cam.vp).f3('uCam', cam.eye).f1('uTime', t).f1('uDpr', R.dpr);
    R.starMesh.draw();
    gl.enable(gl.DEPTH_TEST); gl.depthFunc(gl.LEQUAL);
    if (q.mirror) { drawWorld(true); gl.clear(gl.DEPTH_BUFFER_BIT); }
    drawFloor();
    drawWorld(false);
    post.endScene();
    post.finish({ levels: q.bloom, bloom: 0.5, thresh: 1.15, exposure: 1.0, ca: 0.02, vig: 0.7, grain: 0.016, soft: q.soft * 0.4, fade: clamp(R.fade, 0, 1), time: t });
  }
  function drawFloor() {
    var gl = R.gl, b = R.bounds, P = common(R.P.floor, false), m = R.tmp16, pools = new Float32Array(16), pc = new Float32Array(12), i;
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(false);
    M.basis(m, [R.center[0], R.floorY, R.center[2]], [300, 0, 0], [0, 0, -300], [0, 1, 0]);
    R.lanes.forEach(function (l, k) { if (k > 3) return; pools[k * 4] = (l.x0 + l.x1) / 2; pools[k * 4 + 1] = l.z; pools[k * 4 + 2] = Math.max(3.4, (l.x1 - l.x0) * 0.75); pc[k * 3] = l.color[0] * 0.5; pc[k * 3 + 1] = l.color[1] * 0.5; pc[k * 3 + 2] = l.color[2] * 0.5; });
    P.m4('uModel', m).f2('uCenter', R.center[0], R.center[2]).f1('uFlowDir', 1);
    R.gl.uniform4fv(P.u('uPool'), pools); R.gl.uniform3fv(P.u('uPoolCol'), pc);
    R.g.quad.draw();
  }
  function drawWorld(mirror) {
    var gl = R.gl, P = R.P, m = R.tmp16, q = R.q, t = R.time, api = R.api, i;
    // ---- slabs (opaque glass)
    var S = common(P.slab, mirror);
    gl.enable(gl.BLEND); gl.blendFunc(gl.SRC_ALPHA, gl.ONE_MINUS_SRC_ALPHA); gl.depthMask(!mirror);
    R.nodes.forEach(function (o) {
      var ap = o.appear; if (ap <= 0.001) return;
      var bob = Math.sin(t * 0.7 + o.seed * 30) * 0.035, sy = Math.max(0.02, ap), hov = o.hoverT;
      M.trs(m, o.x, o.y + bob, o.z + (1 - ap) * -0.6, o.yaw, o.w * (0.9 + 0.1 * ap), o.h * sy, o.depth * (1 + o.selT * 0.25));
      S.m4('uModel', m).f2('uSize', o.w, o.h * sy).f3('uTint', o.tint).f3('uHc', o.hc).f1('uAuto', o.auto).f1('uGlow', o.glow * ap + hov * 0.5).f1('uDim', o.dimT).f1('uSel', o.selT).f1('uHover', hov).f1('uFlash', o.flashT).f1('uSeed', o.seed).f1('uErr', o.err);
      R.g.box.draw();
    });
    // ---- additive layer
    gl.blendFunc(gl.ONE, gl.ONE); gl.depthMask(false);
    var PN = common(P.pane, mirror);
    R.lanes.forEach(function (l, k) {
      var w = l.x1 - l.x0, h = l.y1 - l.y0, ap = clamp(R.appear * 0.7 - k * 0.2, 0, 1);
      M.trs(m, (l.x0 + l.x1) / 2, (l.y0 + l.y1) / 2, l.z, 0, w, h, 1);
      PN.m4('uModel', m).f3('uColor', [l.color[0] * 0.26, l.color[1] * 0.26, l.color[2] * 0.26]).f2('uSize', w, h).f1('uSeed', l.seed).f1('uIntensity', (R.fi ? 0.6 : 1) * ap);
      R.g.quad.draw();
    });
    var T = common(P.tube, mirror);
    R.edges.forEach(function (eo) {
      var pulse = null, pi;
      for (pi = R.pulses.length - 1; pi >= 0; pi--) if (R.pulses[pi].eo === eo) { pulse = R.pulses[pi]; break; }
      var pp = pulse ? (t - pulse.t0) / pulse.dur : 0;
      T.f1('uRadius', (eo.radius + eo.hotT * 0.018 + eo.traceT * 0.02) * eo.appear).f3('uColor', eo.col).f1('uMode', eo.mode).f1('uDim', eo.dimT).f1('uHot', eo.hotT).f1('uTrace', eo.traceT).f1('uAct', eo.act || 0)
        .f4('uPulse', pulse ? pp : -5, 0.05 + 0.02 * pp, pulse ? 1 - pp * 0.35 : 0, 0);
      eo.mesh.draw();
    });
    var SO = common(P.solid, mirror);
    R.edges.forEach(function (eo) {
      SO.m4('uModel', eo.arrow).f3('uColor', eo.col).f1('uIntensity', 1.5 + eo.hotT + eo.traceT * 2).f1('uDim', eo.dimT); R.g.cone.draw();
    });
    R.nodes.forEach(function (o) {
      if (o.kind !== 'store' || o.appear < 0.5) return;
      var rings = o.h > 2.5 ? 3 : 1, j;
      for (j = 0; j < rings; j++) {
        var yy = o.y + (rings === 1 ? 0 : (j - 1) * o.h * 0.34) + Math.sin(t * 0.6 + j) * 0.05;
        M.trs(m, o.x, yy, o.z, t * (0.35 + j * 0.12) * (j % 2 ? -1 : 1), o.w * 0.66 + j * 0.08, 1, o.w * 0.66 + j * 0.08);
        SO.m4('uModel', m).f3('uColor', o.tint).f1('uIntensity', 1.4 + o.selT).f1('uDim', o.dimT); R.g.torus.draw();
      }
    });
    var B = common(P.beacon, mirror);
    R.nodes.forEach(function (o) {
      if (o.appear < 0.6) return;
      var top = o.y + o.h / 2, s = 0.15 * (0.85 + 0.15 * Math.sin(t * 2 + o.seed * 9)) * o.appear;
      M.trs(m, o.x - o.w / 2 + 0.3, top + 0.3 + Math.sin(t * 1.3 + o.seed * 20) * 0.07, o.z, t * 0.9 + o.seed * 6, s, s * 1.35, s);
      B.m4('uModel', m).f3('uCol', o.hc).f1('uStyle', o.style).f1('uDim', o.dimT).f1('uBoost', o.hoverT * 0.6 + o.selT * 0.6 + o.flashT); R.g.octa.draw();
    });
    // expanding rings: shockwaves at nodes, ripples on the floor
    var RG = common(P.ring, mirror), cam = R.cam;
    R.rings.forEach(function (r) {
      var p = (t - r.t0) / r.dur, s = r.size * (0.35 + p * 0.65);
      M.basis(m, r.p, [cam.right[0] * s, cam.right[1] * s, cam.right[2] * s], [cam.up[0] * s, cam.up[1] * s, cam.up[2] * s], [-cam.fwd[0], -cam.fwd[1], -cam.fwd[2]]);
      RG.m4('uModel', m).f3('uColor', r.col).f1('uR', p).f1('uW', 0.06 + p * 0.1).f1('uI', 2.6 * Math.pow(1 - p, 1.5)); R.g.quad.draw();
    });
    if (!mirror) R.ripples.forEach(function (r) {
      var p = (t - r.t0) / r.dur, s = 7.5;
      M.basis(m, [r.o.x, R.floorY + 0.01, r.o.z], [s, 0, 0], [0, 0, -s], [0, 1, 0]);
      RG.m4('uModel', m).f3('uColor', r.o.hc).f1('uR', p * 0.9).f1('uW', 0.05 + p * 0.07).f1('uI', 1.1 * Math.pow(1 - p, 1.6)); R.g.quad.draw();
    });
    // point sprites
    var PT = P.part.use(), cam2 = R.cam;
    PT.m4('uVP', cam2.vp).m4('uMirror', mirror ? R.mirror : R.ident).f1('uMirrorOn', mirror ? 1 : 0).f1('uFloorY', R.floorY).f1('uPx', R.cssH * R.dpr * cam2.proj[5] * 0.5);
    if (R.pb.n) R.pmesh.draw(R.pb.n);
    gl.depthMask(true);
  }

  // ------------------------------------------------------------------ point sprites: halos, data particles, comets, sparks, dust
  function fillParticles() {
    var b = R.pb, n = 0, t = R.time, q = R.q, i, j, tmp = R.tmp3, cap = b.cap - 4;
    function add(x, y, z, c, a, size, k) {
      if (n >= cap) return; k = k || 1;
      b.pos[n * 3] = x; b.pos[n * 3 + 1] = y; b.pos[n * 3 + 2] = z; b.col[n * 4] = c[0] * k; b.col[n * 4 + 1] = c[1] * k; b.col[n * 4 + 2] = c[2] * k; b.col[n * 4 + 3] = a; b.size[n] = size; n++;
    }
    var fade = clamp(R.fade, 0, 1);
    // node halos: a soft glow behind every slab
    R.nodes.forEach(function (o) {
      if (o.appear < 0.3) return;
      var a = (0.05 + (o.auto === 0 ? 0.06 : 0) + o.hoverT * 0.16 + o.selT * 0.2 + o.flashT * 0.5 + (o.pulse ? 0.03 * (1 + Math.sin(t * 2.2 + o.seed * 9)) : 0)) * (1 - 0.8 * o.dimT) * o.appear;
      add(o.x, o.y, o.z - o.depth / 2 - 0.3, o.tint, a * 1.8, Math.max(o.w, o.h) * 1.9, 1.4);
    });
    // data particles on the edges (density / speed / colour from flow.js's own flowParams)
    R.edges.forEach(function (eo) {
      var fp = eo.fp; if (!fp || !eo.ps.length || eo.appear < 0.98) return;
      var dim = 1 - 0.93 * eo.dimT, hot = 1 + 0.5 * eo.hotT, per = q.trail + 1, cnt = Math.max(1, Math.round(eo.ps.length * q.pmul));
      var size = 0.34 * (fp.r / 2.5) * (1 + eo.hotT * 0.3), col = eo.col, step = Math.max(1, Math.floor(eo.ps.length / cnt));
      for (i = 0; i < eo.ps.length; i += step) {
        var p = eo.ps[i], env = Math.min(1, p.t * 9, (1 - p.t) * 9);
        for (j = 0; j < per; j++) {
          var tj = p.t - j * 0.16 / eo.len; if (tj < 0) continue; posAt(eo, tj, tmp);
          add(tmp[0], tmp[1], tmp[2], col, fp.alpha * env * dim * hot * fade * (1 - j * 0.28), size * (1 - j * 0.2), 2.3 - j * 0.3);
        }
      }
    });
    // pulses (counter increased / tour comets): bright head with a trail
    R.pulses.forEach(function (p) {
      var eo = p.eo, age = clamp((t - p.t0) / p.dur, 0, 1), e = ease.inOutSine(age);
      for (j = 0; j < 16; j++) {
        var tj = Math.max(0, e - j * 0.014); posAt(eo, tj, tmp);
        add(tmp[0], tmp[1], tmp[2], p.why === 'tour' ? [3.2, 3.2, 3.2] : [eo.col[0] * 2 + 1, eo.col[1] * 2 + 1, eo.col[2] * 2 + 1], (1 - j / 16) * 0.95, (0.75 - j * 0.03) * 1.1, 1.6);
      }
    });
    for (i = 0; i < R.sparks.length; i++) { var s = R.sparks[i], a = clamp(s.life / s.max, 0, 1); add(s.p[0], s.p[1], s.p[2], s.col, a, 0.14 * (0.4 + a), 3.2); }
    // dust motes drifting in the air (skipped on low quality)
    var dn = q.dust;
    if (dn) {
      if (!R.dustBase || R.dustBase.length !== dn * 4) {
        var bo = R.bounds, rnd = seeded(11); R.dustBase = new Float32Array(dn * 4);
        for (i = 0; i < dn; i++) { R.dustBase[i * 4] = lerp(bo.x0 - 4, bo.x1 + 4, rnd()); R.dustBase[i * 4 + 1] = lerp(R.floorY, bo.y1 + 2, rnd()); R.dustBase[i * 4 + 2] = lerp(bo.z0 - 5, bo.z1 + 5, rnd()); R.dustBase[i * 4 + 3] = rnd() * 40; }
      }
      var dc = [0.55, 0.62, 1.0], D0 = R.dustBase;
      for (i = 0; i < dn; i++) {
        var ph = D0[i * 4 + 3];
        add(D0[i * 4] + Math.sin(t * 0.21 + ph) * 0.7, D0[i * 4 + 1] + Math.cos(t * 0.17 + ph * 1.3) * 0.5, D0[i * 4 + 2] + Math.sin(t * 0.13 + ph * 0.7) * 0.7, dc, (0.10 + 0.10 * Math.sin(t * 0.9 + ph * 3)) * fade, 0.07 + (ph % 1) * 0.08, 1.5);
      }
    }
    b.n = n;
    R.pmesh.update(0, b.pos.subarray(0, n * 3)); R.pmesh.update(1, b.col.subarray(0, n * 4)); R.pmesh.update(2, b.size.subarray(0, n));
  }

  // ------------------------------------------------------------------ picking / pointer
  function localPoint(e) { var rc = R.canvas.getBoundingClientRect(); return { x: e.clientX - rc.left, y: e.clientY - rc.top, w: rc.width, h: rc.height }; }
  function pick(px, py) {
    var cam = R.cam, W = R.cssW, H = R.cssH, ray = cam.ray(px / W, py / H), best = null, bt = 1e9;
    R.nodes.forEach(function (o) {
      if (o.appear < 0.6) return;
      var dx = ray.o[0] - o.x, dy = ray.o[1] - o.y, dz = ray.o[2] - o.z, c = Math.cos(o.yaw), s = Math.sin(o.yaw);
      var lo = [c * dx - s * dz, dy, s * dx + c * dz], ld = [c * ray.d[0] - s * ray.d[2], ray.d[1], s * ray.d[0] + c * ray.d[2]];
      var hx = o.w / 2 + 0.06, hy = o.h / 2 + 0.06, hz = o.depth / 2 + 0.06, h = [hx, hy, hz], t0 = 0, t1 = 1e9, ok = true;
      for (var a = 0; a < 3; a++) {
        if (Math.abs(ld[a]) < 1e-6) { if (Math.abs(lo[a]) > h[a]) { ok = false; break; } continue; }
        var ta = (-h[a] - lo[a]) / ld[a], tb = (h[a] - lo[a]) / ld[a]; if (ta > tb) { var sw = ta; ta = tb; tb = sw; }
        t0 = Math.max(t0, ta); t1 = Math.min(t1, tb); if (t0 > t1) { ok = false; break; }
      }
      if (ok && t0 < bt) { bt = t0; best = { kind: 'node', id: o.id }; }
    });
    if (best) return best;
    var bd = 11, tmp = R.tmp3;
    R.edges.forEach(function (eo) {
      if (eo.appear < 0.9) return;
      var prev = null;
      for (var i = 0; i < eo.M; i += 2) {
        cam.project(eo.P[i * 3], eo.P[i * 3 + 1], eo.P[i * 3 + 2], tmp); if (tmp[2] <= 0.2) { prev = null; continue; }
        var x = tmp[0] * W, y = tmp[1] * H;
        if (prev) { var d = segDist(px, py, prev[0], prev[1], x, y); if (d < bd) { bd = d; best = { kind: 'edge', id: eo.id }; } }
        prev = [x, y];
      }
    });
    return best;
  }
  function segDist(px, py, ax, ay, bx, by) { var dx = bx - ax, dy = by - ay, l = dx * dx + dy * dy, t = l ? clamp(((px - ax) * dx + (py - ay) * dy) / l, 0, 1) : 0; return Math.hypot(px - (ax + t * dx), py - (ay + t * dy)); }
  function hoverPick() {
    if (!R.ptr.inside) return;
    var hit = pick(R.ptr.x, R.ptr.y), id = hit ? hit.id : null, kind = hit ? hit.kind : null, tip = $('#tip');
    if (id !== R.hover || kind !== R.hoverKind) {
      R.hover = id; R.hoverKind = kind; applyFocusTargets(); R.canvas.style.cursor = hit ? (kind === 'node' ? 'pointer' : 'help') : (R.drag ? 'grabbing' : 'grab');
      if (hit) { tip.innerHTML = kind === 'node' ? R.api.tipNode(id) : R.api.tipEdge(id); tip.classList.add('show'); } else tip.classList.remove('show');
    }
    if (hit) moveTip(R.ptr.cx, R.ptr.cy);
  }
  function moveTip(cx, cy) {
    var tip = $('#tip'), x = cx + 16, y = cy + 16;
    if (x + 290 > window.innerWidth) x = cx - 296; if (y + 170 > window.innerHeight) y = Math.max(8, cy - 150);
    tip.style.left = x + 'px'; tip.style.top = y + 'px';
  }
  function onDown(e) {
    if (!R || R.lost) return;
    if (R.tour) { stopTour(); }
    R.canvas.focus({ preventScroll: true }); R.engaged = true; R.cam.cancel(); R.cam.idle = 0;
    try { R.canvas.setPointerCapture(e.pointerId); } catch (er) { /* ignore */ }
    var p = localPoint(e); R.pointers[e.pointerId] = { x: p.x, y: p.y };
    var ids = Object.keys(R.pointers);
    if (ids.length === 2) { var a = R.pointers[ids[0]], b = R.pointers[ids[1]]; R.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), mx: (a.x + b.x) / 2, my: (a.y + b.y) / 2 }; R.drag = null; return; }
    R.drag = { mode: (e.button === 2 || e.button === 1 || e.shiftKey) ? 'pan' : 'orbit', x: p.x, y: p.y, sx: p.x, sy: p.y, t0: performance.now(), moved: false, vx: 0, vy: 0, lt: performance.now() };
    R.canvas.style.cursor = 'grabbing'; $('#tip').classList.remove('show');
  }
  function onMove(e) {
    if (!R || R.lost) return;
    var p = localPoint(e); R.ptr.x = p.x; R.ptr.y = p.y; R.ptr.cx = e.clientX; R.ptr.cy = e.clientY; R.ptr.inside = true; R.ptr.dirty = true;
    R.cam.parTX = clamp((p.x / p.w) * 2 - 1, -1, 1); R.cam.parTY = clamp((p.y / p.h) * 2 - 1, -1, 1);
    if (R.pointers[e.pointerId]) R.pointers[e.pointerId] = { x: p.x, y: p.y };
    var ids = Object.keys(R.pointers), cam = R.cam;
    if (R.pinch && ids.length >= 2) {
      var a = R.pointers[ids[0]], b = R.pointers[ids[1]], d = Math.hypot(a.x - b.x, a.y - b.y), mx = (a.x + b.x) / 2, my = (a.y + b.y) / 2;
      cam.g.rad *= R.pinch.d / Math.max(20, d); panBy(mx - R.pinch.mx, my - R.pinch.my); R.pinch = { d: d, mx: mx, my: my }; cam.clampGoal(); cam.idle = 0; return;
    }
    var dr = R.drag; if (!dr) return;
    var dx = p.x - dr.x, dy = p.y - dr.y, now = performance.now(), dtm = Math.max(4, now - dr.lt); dr.x = p.x; dr.y = p.y; dr.lt = now;
    if (!dr.moved && Math.hypot(p.x - dr.sx, p.y - dr.sy) > 5) dr.moved = true;
    if (!dr.moved) return;
    cam.idle = 0;
    if (dr.mode === 'orbit') { cam.g.az -= dx * 0.0038; cam.g.pol -= dy * 0.0032; dr.vx = lerp(dr.vx, -dx * 0.0038 / (dtm / 1000), 0.5); dr.vy = lerp(dr.vy, -dy * 0.0032 / (dtm / 1000), 0.5); }
    else panBy(dx, dy);
    cam.clampGoal();
  }
  function panBy(dx, dy) {
    var cam = R.cam, k = cam.rad * Math.tan(cam.fov / 2) * 2 / Math.max(1, R.cssH);
    for (var i = 0; i < 3; i++) cam.g.t[i] += -cam.right[i] * dx * k + cam.up[i] * dy * k;
  }
  function onUp(e) {
    if (!R) return;
    var dr = R.drag; delete R.pointers[e.pointerId]; if (Object.keys(R.pointers).length < 2) R.pinch = null;
    try { R.canvas.releasePointerCapture(e.pointerId); } catch (er) { /* ignore */ }
    R.drag = null;
    if (!dr) return;
    R.canvas.style.cursor = 'grab';
    if (!dr.moved && performance.now() - dr.t0 < 600 && e.type === 'pointerup') {
      var p = localPoint(e), hit = pick(p.x, p.y);
      if (hit && hit.kind === 'node') R.api.select(hit.id); else if (!hit) R.api.closeDrawer();
      R.ptr.dirty = true;
    } else if (dr.mode === 'orbit' && performance.now() - dr.lt < 90) { R.cam.vAz = clamp(dr.vx, -3, 3); R.cam.vPol = clamp(dr.vy, -2, 2); }
  }
  function onLeave() { if (!R) return; R.ptr.inside = false; R.engaged = R.drag ? true : false; R.cam.parTX = 0; R.cam.parTY = 0; R.hover = null; R.hoverKind = null; applyFocusTargets(); $('#tip').classList.remove('show'); }
  function onWheel(e) {
    if (!R || R.lost) return;
    if (!R.engaged && !e.ctrlKey && !R.immersive) return;    // otherwise let the page scroll: the wheel only zooms once the scene is "engaged" (clicked) or immersive
    e.preventDefault(); if (R.tour) stopTour();
    var cam = R.cam; cam.cancel(); cam.idle = 0; cam.g.rad *= Math.exp(clamp(e.deltaY, -160, 160) * 0.0013); cam.clampGoal();
  }
  function onKey(e) {
    if (!R || D.view() !== 'dataflow') return;
    var tag = (e.target && e.target.tagName) || ''; if (/INPUT|TEXTAREA|SELECT/.test(tag) || e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key === 'Home' || e.key === '0') { e.preventDefault(); resetView(); }
    else if (e.key === 'f' || e.key === 'F') { e.preventDefault(); toggleImmersive(); }
  }
  function toggleImmersive() {
    if (!R) return; R.immersive = !R.immersive; R.host.classList.toggle('immersive', R.immersive);
    var b = $('#f3dFull'); if (b) { b.setAttribute('aria-pressed', R.immersive ? 'true' : 'false'); b.querySelector('span').textContent = R.immersive ? 'Exit immersive' : 'Immersive'; }
    var st = $('#flowStage'); if (R.immersive && st) { try { st.scrollIntoView({ behavior: 'auto', block: 'start' }); } catch (e) { /* ignore */ } }
    setTimeout(function () { if (R) resizeCanvas(false); }, 30);
  }

  // ------------------------------------------------------------------ selection (drawer) -> camera dolly
  Flow3D.drawerLeft = function (id) {
    if (!R || !R.nodeById[id]) return false; var o = R.nodeById[id], p = R.cam.project(o.x, o.y, o.z, R.tmp3); return p[0] > 0.5;
  };
  Flow3D.onSelect = function (id) {
    if (!R) return;
    R.sel = id || null; applyFocusTargets();
    if (id && R.nodeById[id]) {
      if (R.tour) stopTour(true);
      if (!R.preFocus) R.preFocus = R.cam.pose();
      var o = R.nodeById[id], dr = $('#flowDrawer'), left = dr && dr.classList.contains('left'), narrow = window.innerWidth <= 820;
      var vr = $('#view-dataflow').getBoundingClientRect(), cr = R.canvas.getBoundingClientRect(), dw = Math.min(410, window.innerWidth - 28), shx = 0, shy = 0;
      if (narrow) shy = 0.34;
      else { var dx0 = left ? vr.left + 14 : vr.right - 14 - dw, ov = Math.max(0, Math.min(cr.right, dx0 + dw) - Math.max(cr.left, dx0)); shx = clamp(ov / Math.max(1, cr.width), 0, 0.5) * (left ? 1 : -1); }
      var rad = clamp(Math.max(16.5, o.w * 4.8, o.h * 5.4), R.cam.limits.rad[0], R.cam.limits.rad[1]);
      R.cam.flyTo({ az: clamp(R.cam.az * 0.5 + (o.x > R.center[0] ? -0.12 : 0.12), -0.7, 0.7), pol: 1.32, rad: rad, t: [o.x, o.y, o.z], shx: shx, shy: shy }, { dur: 1.3 });
    } else if (R.preFocus && !R.tour) { var pf = R.preFocus; R.preFocus = null; R.cam.flyTo({ az: pf.az, pol: pf.pol, rad: pf.rad, t: pf.t, shx: 0, shy: 0 }, { dur: 1.1 }); }
  };

  // ------------------------------------------------------------------ cinematic tour (the real journey of the latest lead)
  function cap(top, text, at, pct, show) {
    var el = $('#f3dCap'); if (!el) return;
    $('.tc-top', el).textContent = top || ''; $('.tc-text', el).textContent = text || ''; $('.tc-at', el).textContent = at || ''; $('.tc-bar span', el).style.width = (pct || 0) + '%';
    el.classList.toggle('show', !!show);
  }
  function absTime(iso) { var d = new Date(Date.parse(iso)); if (isNaN(d)) return ''; var p = function (n) { return (n < 10 ? '0' : '') + n; }; return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes()); }
  Flow3D.tour = function () { return startTour(); };
  Flow3D.stopTour = function () { stopTour(); };
  function startTour() {
    if (!R || R.tour || !R.data || !R.data.trace || !R.data.trace.steps || !R.data.trace.steps.length) return false;
    var tr = R.data.trace; R.api.closeDrawer(); R.preFocus = null;
    ensureInView(); holdUntilSeen(2.2);
    R.tour = { tr: tr, i: -1, phase: 'intro', t: 0, dur: 1.7, back: R.cam.pose(), steps: tr.steps, ended: false, prevHover: null };
    R.hover = null; R.hoverKind = null; applyFocusTargets();
    R.host.classList.add('touring'); $('#tip').classList.remove('show');
    cap('TRACING LEAD #' + tr.lead_id + ' · ' + String(tr.source || '').replace(/_/g, ' '), 'Follow the real journey of the newest lead…', absTime(tr.created_at), 0, true);
    var h = R.home; R.cam.flyTo({ az: -0.5, pol: 1.18, rad: h.rad * 0.8, t: h.t.slice(), shx: 0, shy: 0 }, { dur: 1.7 });
    R.edges.forEach(function (eo) { eo.traceT = 0; });
    D.emit('flow3d-tour', true);
    return true;
  }
  function stopTour(quiet) {
    var T = R && R.tour; if (!T) return;
    R.tour = null; R.host.classList.remove('touring'); cap(null, null, null, 0, false);
    R.pulses = R.pulses.filter(function (p) { return p.why !== 'tour'; });
    if (!quiet && !R.sel) { R.cam.flyTo(T.back && T.back.rad ? T.back : R.home, { dur: 1.4 }); }
    D.emit('flow3d-tour', false);
  }
  function stepEdge(st) { return st && R.edgeById[st.edge]; }
  function nextStep() {
    var T = R.tour; T.i++;
    var st = T.steps[T.i], tr = T.tr;
    if (!st) { T.phase = 'outro'; T.t = 0; T.dur = 4.2; cap('LEAD #' + tr.lead_id + ' · DONE', 'Journey complete: every stage above really happened for this lead.', absTime(tr.created_at), 100, true); R.cam.flyTo(R.home, { dur: 2.2 }); return; }
    var pct = Math.round((T.i + 1) / T.steps.length * 100), head = 'STEP ' + (T.i + 1) + ' / ' + T.steps.length, eo = stepEdge(st);
    if (!st.reached || !eo) { T.phase = 'outro'; T.t = 0; T.dur = 4.4; cap('LEAD #' + tr.lead_id + ' · ' + head, 'The journey stops here: this lead never reached this step.', st.text || '', pct, true); if (eo) focusCam(eo, 0.5); T.flyAt = 2.0; return; }
    T.phase = 'fly'; T.t = 0; T.dur = 0.85; T.eo = eo; T.pct = pct; T.head = head;
    cap('LEAD #' + tr.lead_id + ' · ' + head, st.text, st.at ? absTime(st.at) + ' · from the database' : '', pct, true);
    focusCam(eo, 0.5);
  }
  function focusCam(eo, at) {
    var m = posAt(eo, at, [0, 0, 0]), T = R.tour, s = T ? T.i % 2 : 0, len = eo.len;
    R.cam.flyTo({ az: (s ? 0.26 : -0.3) + (m[0] > R.center[0] ? -0.06 : 0.06), pol: 1.22, rad: clamp(len * 1.45 + 14, 22, 30), t: m, shx: 0, shy: 0 }, { dur: 0.85 });
  }
  function updateTour(dt) {
    var T = R.tour; T.t += dt;
    if (T.phase === 'intro') { if (T.t >= T.dur) nextStep(); }
    else if (T.phase === 'fly') {
      if (T.t >= T.dur) { T.phase = 'run'; T.t = 0; T.dur = clamp(T.eo.len * 0.17, 1.0, 1.6); T.eo.traceT = 1; R.pulses.push({ eo: T.eo, t0: R.time, dur: T.dur, why: 'tour' }); }
    } else if (T.phase === 'run') {
      var p = ease.inOutSine(clamp(T.t / T.dur, 0, 1)); posAt(T.eo, p, R.tmp3);
      var g = R.cam.g, m = posAt(T.eo, 0.5, [0, 0, 0]); for (var i = 0; i < 3; i++) g.t[i] = R.cam.t[i] = lerp(R.cam.t[i], m[i] * 0.55 + R.tmp3[i] * 0.45, 1 - Math.exp(-dt * 3.2));
      R.cam.g.az = R.cam.az = R.cam.az + (T.i % 2 ? -1 : 1) * dt * 0.035; R.cam.tween = null;
      if (T.t >= T.dur) { T.phase = 'hold'; T.t = 0; T.dur = 0.22; }
    } else if (T.phase === 'hold') { if (T.t >= T.dur) nextStep(); }
    else if (T.phase === 'outro') {
      if (T.flyAt != null && T.t >= T.flyAt) { T.flyAt = null; R.cam.flyTo(R.home, { dur: 2.2 }); }
      if (T.t >= T.dur) stopTour(true);
    }
  }

  // ------------------------------------------------------------------ context loss / restore
  function onLost() {
    if (!R) return; R.lost = true; stop(); R.ctx.forget(); R.edges.forEach(function (e) { e.mesh = null; });
    R.host.classList.add('lost'); var n = $('#f3dNote'); if (n) { n.textContent = 'Graphics context lost — restoring…'; n.classList.add('show'); }
  }
  function onRestored() {
    if (!R) return;
    try {
      var keepData = R.data, sel = R.sel;
      R.ctx = null; R.sig = null; R.edges = []; R.edgeById = {}; R.post = null;
      R.lost = false; setupGL(); resizeCanvas(true);
      R.introDone = true; R.appear = 3; R.fade = 0;
      if (keepData) setData(keepData);
      R.sel = sel; R.host.classList.remove('lost'); var n = $('#f3dNote'); if (n) n.classList.remove('show');
      R.restores = (R.restores || 0) + 1; start();
    } catch (err) { console.warn('Flow3D: restore failed -', err && err.message); R.api.fail && R.api.fail('The 3D view could not recover after a graphics reset'); }
  }

  // ------------------------------------------------------------------ HUD buttons (bound once; the elements live in shell.html)
  function bindHud() {
    var b;
    if ((b = $('#f3dReset'))) b.addEventListener('click', function () { resetView(); });
    if ((b = $('#f3dTour'))) b.addEventListener('click', function () { if (!R) return; if (R.tour) stopTour(); else if (!startTour()) D.toast('No lead has travelled the pipeline yet, so there is no journey to fly.', ''); });
    if ((b = $('#f3dFull'))) b.addEventListener('click', toggleImmersive);
  }
  bindHud();
  D.on('flow-select', function (id) { Flow3D.onSelect(id); });
  D.on('flow3d-tour', function (on) { var s = $('#f3dTour span'); if (s) s.textContent = on ? 'Stop tour' : 'Cinematic tour'; });

  // ------------------------------------------------------------------ introspection (used by the automated verification, harmless otherwise)
  Flow3D.stats = function () {
    if (!R) return { mounted: false, quality: QUALITY[qLevel].name, qualityLevel: qLevel, lastTeardown: lastTeardown, teardowns: teardowns };
    var s = R.ctx ? R.ctx.stats() : {}, gl = R.gl, info = { mounted: true, lost: R.lost, fps: Math.round(R.fps * 10) / 10, frames: R.frames, quality: R.q.name, qualityLevel: qLevel, qualityLog: R.qLog.slice(), gpu: s, canvas: [R.canvas.width, R.canvas.height], dpr: R.dpr, msaa: R.post ? R.post.samples : 0, hdr: R.ctx ? R.ctx.hdr : null, renderer: R.ctx ? R.ctx.renderer : '', nodes: R.nodes.length, edges: R.edges.length, tour: R.tour ? R.tour.phase : null, pose: R.cam.pose(), home: R.home, tween: !!R.cam.tween, particles: R.pb ? R.pb.n : 0, restores: R.restores || 0, selected: R.sel, pulses: R.pulses.length, teardowns: teardowns, lastTeardown: lastTeardown };
    return info;
  };
  Flow3D.nodeScreen = function (id) {
    if (!R || !R.nodeById[id]) return null; var o = R.nodeById[id], p = R.cam.project(o.x, o.y, o.z + o.depth / 2, R.tmp3), rc = R.canvas.getBoundingClientRect();
    return { x: rc.left + p[0] * rc.width, y: rc.top + p[1] * rc.height };
  };
  Flow3D.debug = {
    loseContext: function () { if (R && R.ctx && R.ctx.loseExt) R.ctx.loseExt.loseContext(); return !!(R && R.ctx && R.ctx.loseExt); },
    restoreContext: function () { if (R && R.ctx && R.ctx.loseExt) R.ctx.loseExt.restoreContext(); },
    setQuality: function (l, lock) { qLocked = !!lock; if (R) setQuality(l, 'debug'); else qLevel = clamp(l | 0, 0, QUALITY.length - 1); },
    emitPulse: function (edgeId) { if (R && R.edgeById[edgeId]) { emitPulse(R.edgeById[edgeId], 'counter'); return true; } return false; },
    time: function () { return R ? R.time : 0; },
    edgeScreen: function (id, at) {   // client-space position of a point along an edge (for the automated checks)
      if (!R || !R.edgeById[id]) return null; var p = posAt(R.edgeById[id], at == null ? 0.5 : at, [0, 0, 0]), q = R.cam.project(p[0], p[1], p[2], [0, 0, 0]), rc = R.canvas.getBoundingClientRect();
      return { x: rc.left + q[0] * rc.width, y: rc.top + q[1] * rc.height };
    },
    extents: function () {           // projected extent (NDC, -1..1) of the framing points at the current camera
      if (!R || !R.bounds) return null; var x0 = 9, x1 = -9, y0 = 9, y1 = -9, tmp = [0, 0, 0];
      framePts().forEach(function (p) { R.cam.project(p[0], p[1], p[2], tmp); var X = tmp[0] * 2 - 1, Y = 1 - tmp[1] * 2; x0 = Math.min(x0, X); x1 = Math.max(x1, X); y0 = Math.min(y0, Y); y1 = Math.max(y1, Y); });
      return { x: [x0, x1], y: [y0, y1], aspect: R.cssW / R.cssH, home: R.home, pose: R.cam.pose() };
    }  };
})();
