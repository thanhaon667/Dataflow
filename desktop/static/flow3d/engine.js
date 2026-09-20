/* ERP Desk - Data Flow 3D: a small WebGL2 engine (no third-party code).
   math     column-major 4x4 matrices and a few vector helpers
   Ctx      WebGL2 context wrapper that TRACKS every GPU object it creates, so dispose() frees all of them and stats()
            can prove nothing leaks across page switches
   geo      primitive meshes (box, octahedron, cone, quad, torus)
   Post     HDR scene target (MSAA when available) -> thresholded multi-level bloom -> tone-mapped composite
   OrbitCam damped orbit / pan / zoom camera with eased fly-to tweens */
(function () {
  'use strict';
  var F3D = (window.F3D = window.F3D || {});
  var G = F3D.glsl;

  // ================================================================== math
  var mat = {
    ident: function (o) { o.fill(0); o[0] = o[5] = o[10] = o[15] = 1; return o; },
    mul: function (o, a, b) {
      var i, j, k, s, t = mat._t;
      for (i = 0; i < 4; i++) for (j = 0; j < 4; j++) { s = 0; for (k = 0; k < 4; k++) s += a[k * 4 + j] * b[i * 4 + k]; t[i * 4 + j] = s; }
      for (i = 0; i < 16; i++) o[i] = t[i];
      return o;
    },
    _t: new Float32Array(16),
    persp: function (o, fov, aspect, n, f) {
      var t = 1 / Math.tan(fov / 2); o.fill(0);
      o[0] = t / aspect; o[5] = t; o[10] = (f + n) / (n - f); o[11] = -1; o[14] = 2 * f * n / (n - f);
      return o;
    },
    lookAt: function (o, e, c, up) {
      var zx = e[0] - c[0], zy = e[1] - c[1], zz = e[2] - c[2], l = Math.hypot(zx, zy, zz) || 1; zx /= l; zy /= l; zz /= l;
      var xx = up[1] * zz - up[2] * zy, xy = up[2] * zx - up[0] * zz, xz = up[0] * zy - up[1] * zx; l = Math.hypot(xx, xy, xz) || 1; xx /= l; xy /= l; xz /= l;
      var yx = zy * xz - zz * xy, yy = zz * xx - zx * xz, yz = zx * xy - zy * xx;
      o[0] = xx; o[1] = yx; o[2] = zx; o[3] = 0; o[4] = xy; o[5] = yy; o[6] = zy; o[7] = 0; o[8] = xz; o[9] = yz; o[10] = zz; o[11] = 0;
      o[12] = -(xx * e[0] + xy * e[1] + xz * e[2]); o[13] = -(yx * e[0] + yy * e[1] + yz * e[2]); o[14] = -(zx * e[0] + zy * e[1] + zz * e[2]); o[15] = 1;
      return o;
    },
    inv: function (o, m) {
      var a00 = m[0], a01 = m[1], a02 = m[2], a03 = m[3], a10 = m[4], a11 = m[5], a12 = m[6], a13 = m[7], a20 = m[8], a21 = m[9], a22 = m[10], a23 = m[11], a30 = m[12], a31 = m[13], a32 = m[14], a33 = m[15];
      var b00 = a00 * a11 - a01 * a10, b01 = a00 * a12 - a02 * a10, b02 = a00 * a13 - a03 * a10, b03 = a01 * a12 - a02 * a11, b04 = a01 * a13 - a03 * a11, b05 = a02 * a13 - a03 * a12;
      var b06 = a20 * a31 - a21 * a30, b07 = a20 * a32 - a22 * a30, b08 = a20 * a33 - a23 * a30, b09 = a21 * a32 - a22 * a31, b10 = a21 * a33 - a23 * a31, b11 = a22 * a33 - a23 * a32;
      var d = b00 * b11 - b01 * b10 + b02 * b09 + b03 * b08 - b04 * b07 + b05 * b06; if (!d) return null; d = 1 / d;
      o[0] = (a11 * b11 - a12 * b10 + a13 * b09) * d; o[1] = (a02 * b10 - a01 * b11 - a03 * b09) * d; o[2] = (a31 * b05 - a32 * b04 + a33 * b03) * d; o[3] = (a22 * b04 - a21 * b05 - a23 * b03) * d;
      o[4] = (a12 * b08 - a10 * b11 - a13 * b07) * d; o[5] = (a00 * b11 - a02 * b08 + a03 * b07) * d; o[6] = (a32 * b02 - a30 * b05 - a33 * b01) * d; o[7] = (a20 * b05 - a22 * b02 + a23 * b01) * d;
      o[8] = (a10 * b10 - a11 * b08 + a13 * b06) * d; o[9] = (a01 * b08 - a00 * b10 - a03 * b06) * d; o[10] = (a30 * b04 - a31 * b02 + a33 * b00) * d; o[11] = (a21 * b02 - a20 * b04 - a23 * b00) * d;
      o[12] = (a11 * b07 - a10 * b09 - a12 * b06) * d; o[13] = (a00 * b09 - a01 * b07 + a02 * b06) * d; o[14] = (a31 * b01 - a30 * b03 - a32 * b00) * d; o[15] = (a20 * b03 - a21 * b01 + a22 * b00) * d;
      return o;
    },
    // model = T(pos) * Ry(yaw) * S(sx, sy, sz)
    trs: function (o, px, py, pz, yaw, sx, sy, sz) {
      var c = Math.cos(yaw), s = Math.sin(yaw);
      o[0] = c * sx; o[1] = 0; o[2] = -s * sx; o[3] = 0; o[4] = 0; o[5] = sy; o[6] = 0; o[7] = 0;
      o[8] = s * sz; o[9] = 0; o[10] = c * sz; o[11] = 0; o[12] = px; o[13] = py; o[14] = pz; o[15] = 1;
      return o;
    },
    // columns are the (already scaled) basis vectors
    basis: function (o, p, x, y, z) {
      o[0] = x[0]; o[1] = x[1]; o[2] = x[2]; o[3] = 0; o[4] = y[0]; o[5] = y[1]; o[6] = y[2]; o[7] = 0;
      o[8] = z[0]; o[9] = z[1]; o[10] = z[2]; o[11] = 0; o[12] = p[0]; o[13] = p[1]; o[14] = p[2]; o[15] = 1;
      return o;
    },
    // reflection across the horizontal plane y = h
    mirrorY: function (o, h) { mat.ident(o); o[5] = -1; o[13] = 2 * h; return o; }
  };
  function clamp(v, a, b) { return v < a ? a : (v > b ? b : v); }
  function lerp(a, b, t) { return a + (b - a) * t; }
  var ease = {
    inOutCubic: function (t) { return t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2; },
    outQuint: function (t) { return 1 - Math.pow(1 - t, 5); },
    outCubic: function (t) { return 1 - Math.pow(1 - t, 3); },
    inOutSine: function (t) { return -(Math.cos(Math.PI * t) - 1) / 2; }
  };
  function hex2lin(hex, k) {           // '#rrggbb' (sRGB) -> [r, g, b] linear light, times k
    var n = parseInt(String(hex).replace('#', ''), 16); if (isNaN(n)) n = 0xffffff; k = k == null ? 1 : k;
    return [Math.pow(((n >> 16) & 255) / 255, 2.2) * k, Math.pow(((n >> 8) & 255) / 255, 2.2) * k, Math.pow((n & 255) / 255, 2.2) * k];
  }
  F3D.math = mat; F3D.util = { clamp: clamp, lerp: lerp, ease: ease, hex2lin: hex2lin };

  // ================================================================== GL context wrapper with resource tracking
  function Ctx(gl) {
    this.gl = gl;
    this.res = { buffers: [], vaos: [], textures: [], programs: [], fbos: [], rbos: [] };
    this.floatRT = !!gl.getExtension('EXT_color_buffer_float');
    if (!this.floatRT) this.halfRT = !!gl.getExtension('EXT_color_buffer_half_float');
    this.loseExt = gl.getExtension('WEBGL_lose_context');
    this.maxSamples = gl.getParameter(gl.MAX_SAMPLES) || 0;
    this.hdr = this.floatRT || this.halfRT;
    var dbg = gl.getExtension('WEBGL_debug_renderer_info');
    this.renderer = dbg ? gl.getParameter(dbg.UNMASKED_RENDERER_WEBGL) : gl.getParameter(gl.RENDERER);
    this.emptyVao = this._track('vaos', gl.createVertexArray());
  }
  Ctx.prototype._track = function (kind, obj) { if (obj) this.res[kind].push(obj); return obj; };
  Ctx.prototype._del = function (kind, obj) {
    var i = this.res[kind].indexOf(obj); if (i < 0) return; this.res[kind].splice(i, 1);
    var gl = this.gl;
    ({ buffers: gl.deleteBuffer, vaos: gl.deleteVertexArray, textures: gl.deleteTexture, programs: gl.deleteProgram, fbos: gl.deleteFramebuffer, rbos: gl.deleteRenderbuffer })[kind].call(gl, obj);
  };
  Ctx.prototype.stats = function () {
    var r = this.res; return { buffers: r.buffers.length, vaos: r.vaos.length, textures: r.textures.length, programs: r.programs.length, framebuffers: r.fbos.length, renderbuffers: r.rbos.length };
  };
  Ctx.prototype.dispose = function () {
    var self = this;
    ['fbos', 'rbos', 'textures', 'buffers', 'vaos', 'programs'].forEach(function (k) { self.res[k].slice().forEach(function (o) { self._del(k, o); }); });
  };
  Ctx.prototype.forget = function () { for (var k in this.res) this.res[k] = []; };   // the context was lost: the handles are already gone

  Ctx.prototype.program = function (vsSrc, fsSrc, name) {
    var gl = this.gl;
    function sh(type, src) {
      var s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) { var log = gl.getShaderInfoLog(s); gl.deleteShader(s); throw new Error('shader ' + (name || '') + ': ' + log); }
      return s;
    }
    var vs = sh(gl.VERTEX_SHADER, vsSrc), fs = sh(gl.FRAGMENT_SHADER, fsSrc), p = gl.createProgram();
    gl.attachShader(p, vs); gl.attachShader(p, fs); gl.linkProgram(p);
    gl.deleteShader(vs); gl.deleteShader(fs);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) { var l = gl.getProgramInfoLog(p); gl.deleteProgram(p); throw new Error('link ' + (name || '') + ': ' + l); }
    this._track('programs', p);
    var cache = {};
    var P = {
      p: p, name: name,
      u: function (n) { var l = cache[n]; if (l === undefined) { l = gl.getUniformLocation(p, n); cache[n] = l; } return l; },
      use: function () { gl.useProgram(p); return P; },
      f1: function (n, v) { gl.uniform1f(P.u(n), v); return P; },
      f2: function (n, a, b) { gl.uniform2f(P.u(n), a, b); return P; },
      f3: function (n, v) { gl.uniform3f(P.u(n), v[0], v[1], v[2]); return P; },
      f4: function (n, a, b, c, d) { gl.uniform4f(P.u(n), a, b, c, d); return P; },
      m4: function (n, m) { gl.uniformMatrix4fv(P.u(n), false, m); return P; },
      i1: function (n, v) { gl.uniform1i(P.u(n), v); return P; },
      tex: function (n, unit, tex) { gl.activeTexture(gl.TEXTURE0 + unit); gl.bindTexture(gl.TEXTURE_2D, tex); gl.uniform1i(P.u(n), unit); return P; }
    };
    return P;
  };

  Ctx.prototype.buffer = function (target, data, usage) {
    var gl = this.gl, b = this._track('buffers', gl.createBuffer());
    gl.bindBuffer(target, b); gl.bufferData(target, data, usage || gl.STATIC_DRAW); return b;
  };
  // desc: { attribs: [{loc, size, data, dynamic}], index: Uint16/32Array, mode, count }
  Ctx.prototype.mesh = function (desc) {
    var gl = this.gl, self = this, vao = this._track('vaos', gl.createVertexArray()), bufs = [];
    gl.bindVertexArray(vao);
    desc.attribs.forEach(function (a) {
      var b = self.buffer(gl.ARRAY_BUFFER, a.data, a.dynamic ? gl.DYNAMIC_DRAW : gl.STATIC_DRAW);
      gl.enableVertexAttribArray(a.loc); gl.vertexAttribPointer(a.loc, a.size, gl.FLOAT, false, 0, 0); bufs.push(b);
    });
    var idxType = 0, idxBuf = null;
    if (desc.index) { idxBuf = self.buffer(gl.ELEMENT_ARRAY_BUFFER, desc.index); idxType = desc.index instanceof Uint32Array ? gl.UNSIGNED_INT : gl.UNSIGNED_SHORT; }
    gl.bindVertexArray(null);
    var count = desc.count != null ? desc.count : (desc.index ? desc.index.length : desc.attribs[0].data.length / desc.attribs[0].size);
    var mode = desc.mode != null ? desc.mode : gl.TRIANGLES;
    return {
      vao: vao, bufs: bufs, idxBuf: idxBuf, count: count,
      draw: function (n) {
        gl.bindVertexArray(vao);
        if (idxType) gl.drawElements(mode, n == null ? count : n, idxType, 0); else gl.drawArrays(mode, 0, n == null ? count : n);
      },
      update: function (i, data, offsetFloats) { gl.bindBuffer(gl.ARRAY_BUFFER, bufs[i]); gl.bufferSubData(gl.ARRAY_BUFFER, (offsetFloats || 0) * 4, data); }
    };
  };

  Ctx.prototype.texture = function (w, h, hdr, opts) {
    var gl = this.gl; opts = opts || {};
    var t = this._track('textures', gl.createTexture());
    gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texStorage2D(gl.TEXTURE_2D, 1, hdr && this.hdr ? gl.RGBA16F : gl.RGBA8, w, h);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, opts.repeatS ? gl.REPEAT : gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return t;
  };
  Ctx.prototype.fbo = function (tex, depthRbo) {
    var gl = this.gl, f = this._track('fbos', gl.createFramebuffer());
    gl.bindFramebuffer(gl.FRAMEBUFFER, f);
    gl.framebufferTexture2D(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.TEXTURE_2D, tex, 0);
    if (depthRbo) gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, depthRbo);
    return f;
  };
  Ctx.prototype.rbo = function (w, h, fmt, samples) {
    var gl = this.gl, r = this._track('rbos', gl.createRenderbuffer());
    gl.bindRenderbuffer(gl.RENDERBUFFER, r);
    if (samples > 0) gl.renderbufferStorageMultisample(gl.RENDERBUFFER, samples, fmt, w, h); else gl.renderbufferStorage(gl.RENDERBUFFER, fmt, w, h);
    return r;
  };
  F3D.Ctx = Ctx;

  // ================================================================== geometry
  var geo = F3D.geo = {};
  geo.box = function () {       // unit cube centred on the origin; aux = face uv
    var P = [], N = [], A = [], I = [];
    var faces = [
      [[0, 0, 1], [-.5, -.5, .5], [.5, -.5, .5], [.5, .5, .5], [-.5, .5, .5]],
      [[0, 0, -1], [.5, -.5, -.5], [-.5, -.5, -.5], [-.5, .5, -.5], [.5, .5, -.5]],
      [[1, 0, 0], [.5, -.5, .5], [.5, -.5, -.5], [.5, .5, -.5], [.5, .5, .5]],
      [[-1, 0, 0], [-.5, -.5, -.5], [-.5, -.5, .5], [-.5, .5, .5], [-.5, .5, -.5]],
      [[0, 1, 0], [-.5, .5, .5], [.5, .5, .5], [.5, .5, -.5], [-.5, .5, -.5]],
      [[0, -1, 0], [-.5, -.5, -.5], [.5, -.5, -.5], [.5, -.5, .5], [-.5, -.5, .5]]
    ], uv = [[0, 0], [1, 0], [1, 1], [0, 1]];
    faces.forEach(function (f, fi) {
      for (var k = 0; k < 4; k++) { P.push.apply(P, f[k + 1]); N.push.apply(N, f[0]); A.push(uv[k][0], uv[k][1], 0); }
      var b = fi * 4; I.push(b, b + 1, b + 2, b, b + 2, b + 3);
    });
    return { pos: new Float32Array(P), nrm: new Float32Array(N), aux: new Float32Array(A), index: new Uint16Array(I) };
  };
  geo.octa = function () {      // flat-shaded octahedron, aux = barycentric coordinates
    var v = [[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]];
    var tris = [[0, 2, 4], [4, 2, 1], [1, 2, 5], [5, 2, 0], [4, 3, 0], [1, 3, 4], [5, 3, 1], [0, 3, 5]];
    var P = [], N = [], A = [], bary = [[1, 0, 0], [0, 1, 0], [0, 0, 1]];
    tris.forEach(function (t) {
      var a = v[t[0]], b = v[t[1]], c = v[t[2]];
      var ux = b[0] - a[0], uy = b[1] - a[1], uz = b[2] - a[2], wx = c[0] - a[0], wy = c[1] - a[1], wz = c[2] - a[2];
      var nx = uy * wz - uz * wy, ny = uz * wx - ux * wz, nz = ux * wy - uy * wx, l = Math.hypot(nx, ny, nz);
      for (var k = 0; k < 3; k++) { P.push.apply(P, v[t[k]]); N.push(nx / l, ny / l, nz / l); A.push.apply(A, bary[k]); }
    });
    return { pos: new Float32Array(P), nrm: new Float32Array(N), aux: new Float32Array(A), index: null };
  };
  geo.cone = function (seg) {   // apex at +Y (y = 1), base radius 1 at y = 0
    var P = [], N = [], A = [], I = [], i;
    for (i = 0; i <= seg; i++) {
      var a = i / seg * Math.PI * 2, c = Math.cos(a), s = Math.sin(a);
      P.push(c, 0, s, 0, 1, 0); N.push(c * 0.7, 0.7, s * 0.7, c * 0.7, 0.7, s * 0.7); A.push(i / seg, 0, 0, i / seg, 1, 0);
    }
    for (i = 0; i < seg; i++) I.push(i * 2, i * 2 + 1, (i + 1) * 2);
    return { pos: new Float32Array(P), nrm: new Float32Array(N), aux: new Float32Array(A), index: new Uint16Array(I) };
  };
  geo.quad = function () {      // unit quad in the xy plane, aux = uv
    return {
      pos: new Float32Array([-.5, -.5, 0, .5, -.5, 0, .5, .5, 0, -.5, .5, 0]), nrm: new Float32Array([0, 0, 1, 0, 0, 1, 0, 0, 1, 0, 0, 1]),
      aux: new Float32Array([0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0]), index: new Uint16Array([0, 1, 2, 0, 2, 3])
    };
  };
  geo.torus = function (R, r, su, sv) {   // ring around the Y axis
    var P = [], N = [], A = [], I = [], i, j;
    for (i = 0; i <= su; i++) for (j = 0; j <= sv; j++) {
      var u = i / su * Math.PI * 2, v = j / sv * Math.PI * 2, cu = Math.cos(u), su_ = Math.sin(u), cv = Math.cos(v), sv_ = Math.sin(v);
      P.push((R + r * cv) * cu, r * sv_, (R + r * cv) * su_); N.push(cv * cu, sv_, cv * su_); A.push(i / su, j / sv, 0);
    }
    for (i = 0; i < su; i++) for (j = 0; j < sv; j++) { var a = i * (sv + 1) + j, b = (i + 1) * (sv + 1) + j; I.push(a, b, a + 1, b, b + 1, a + 1); }
    return { pos: new Float32Array(P), nrm: new Float32Array(N), aux: new Float32Array(A), index: new Uint16Array(I) };
  };
  Ctx.prototype.modelMesh = function (g) {
    return this.mesh({ attribs: [{ loc: 0, size: 3, data: g.pos }, { loc: 1, size: 3, data: g.nrm }, { loc: 2, size: 3, data: g.aux }], index: g.index, count: g.index ? g.index.length : g.pos.length / 3 });
  };

  // ================================================================== post-processing
  // scene (HDR, MSAA if possible) -> bright pass -> N blur levels -> composite (bloom + soft edges + ACES + vignette + grain)
  function Post(ctx) {
    this.c = ctx; var gl = ctx.gl;
    this.progs = {
      bright: ctx.program(G.fullVS, G.brightFS, 'bright'), blur: ctx.program(G.fullVS, G.blurFS, 'blur'), comp: ctx.program(G.fullVS, G.compositeFS, 'composite')
    };
    this.w = 0; this.h = 0; this.samples = 0; this.levels = 5; this.msaaOk = true;
    this.black = ctx.texture(1, 1, false); gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, new Uint8Array([0, 0, 0, 255]));
  }
  Post.prototype.free = function () {
    var c = this.c, self = this;
    ['sceneTex', 'brightTex'].forEach(function (k) { if (self[k]) { c._del('textures', self[k]); self[k] = null; } });
    ['resolveFbo', 'msaaFbo', 'brightFbo'].forEach(function (k) { if (self[k]) { c._del('fbos', self[k]); self[k] = null; } });
    ['colorRb', 'depthRb'].forEach(function (k) { if (self[k]) { c._del('rbos', self[k]); self[k] = null; } });
    (this.lv || []).forEach(function (l) { c._del('textures', l.tmp); c._del('textures', l.out); c._del('fbos', l.tmpF); c._del('fbos', l.outF); });
    this.lv = [];
  };
  // w,h: drawing-buffer size; samples: requested MSAA samples (0 = off)
  Post.prototype.resize = function (w, h, samples) {
    var c = this.c, gl = c.gl, hdr = true; w = Math.max(2, w | 0); h = Math.max(2, h | 0);
    this.free();
    this.w = w; this.h = h;
    this.sceneTex = c.texture(w, h, hdr);
    var want = c.hdr && this.msaaOk ? Math.min(samples | 0, c.maxSamples) : 0, ok = false;
    if (want > 0) {
      this.colorRb = c.rbo(w, h, c.hdr ? gl.RGBA16F : gl.RGBA8, want); this.depthRb = c.rbo(w, h, gl.DEPTH_COMPONENT24, want);
      this.msaaFbo = c.fbo(null); gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.COLOR_ATTACHMENT0, gl.RENDERBUFFER, this.colorRb);
      gl.framebufferRenderbuffer(gl.FRAMEBUFFER, gl.DEPTH_ATTACHMENT, gl.RENDERBUFFER, this.depthRb);
      this.resolveFbo = c.fbo(this.sceneTex);
      ok = gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.msaaFbo); ok = ok && gl.checkFramebufferStatus(gl.FRAMEBUFFER) === gl.FRAMEBUFFER_COMPLETE;
      if (!ok) { c._del('fbos', this.msaaFbo); c._del('fbos', this.resolveFbo); c._del('rbos', this.colorRb); c._del('rbos', this.depthRb); this.msaaFbo = this.resolveFbo = this.colorRb = this.depthRb = null; this.msaaOk = false; }
    }
    this.samples = ok ? want : 0;
    if (!ok) {
      this.depthRb = c.rbo(w, h, gl.DEPTH_COMPONENT24, 0);
      this.resolveFbo = c.fbo(this.sceneTex, this.depthRb);
      if (gl.checkFramebufferStatus(gl.FRAMEBUFFER) !== gl.FRAMEBUFFER_COMPLETE && c.hdr) {   // float targets refused: fall back to 8 bit
        c.hdr = false; gl.bindFramebuffer(gl.FRAMEBUFFER, null); this.resize(w, h, 0); return;
      }
    }
    // bloom chain
    this.brightTex = c.texture(Math.max(2, w >> 1), Math.max(2, h >> 1), hdr); this.brightFbo = c.fbo(this.brightTex);
    this.lv = [];
    for (var i = 0; i < this.levels; i++) {
      var lw = Math.max(2, w >> (i + 1)), lh = Math.max(2, h >> (i + 1)), tmp = c.texture(lw, lh, hdr), tmpF = c.fbo(tmp), out = c.texture(lw, lh, hdr), outF = c.fbo(out);
      this.lv.push({ w: lw, h: lh, tmp: tmp, tmpF: tmpF, out: out, outF: outF });
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null);
  };
  Post.prototype.beginScene = function () {
    var gl = this.c.gl; gl.bindFramebuffer(gl.FRAMEBUFFER, this.msaaFbo || this.resolveFbo); gl.viewport(0, 0, this.w, this.h);
    gl.depthMask(true); gl.disable(gl.BLEND); gl.clearColor(0, 0, 0, 1); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);
  };
  Post.prototype.endScene = function () {
    var gl = this.c.gl;
    if (this.msaaFbo) {
      gl.bindFramebuffer(gl.READ_FRAMEBUFFER, this.msaaFbo); gl.bindFramebuffer(gl.DRAW_FRAMEBUFFER, this.resolveFbo);
      gl.blitFramebuffer(0, 0, this.w, this.h, 0, 0, this.w, this.h, gl.COLOR_BUFFER_BIT, gl.NEAREST);
    }
  };
  Post.prototype.full = function () { var gl = this.c.gl; gl.bindVertexArray(this.c.emptyVao); gl.drawArrays(gl.TRIANGLES, 0, 3); };
  // fx: { levels, bloom, thresh, exposure, ca, vig, grain, soft, fade, time }
  Post.prototype.finish = function (fx) {
    var gl = this.c.gl, i, l, n = clamp(fx.levels | 0, 0, this.levels);
    gl.disable(gl.DEPTH_TEST); gl.disable(gl.BLEND); gl.depthMask(false);
    if (n > 0) {
      gl.bindFramebuffer(gl.FRAMEBUFFER, this.brightFbo); gl.viewport(0, 0, this.brightTex ? Math.max(2, this.w >> 1) : 2, Math.max(2, this.h >> 1));
      this.progs.bright.use().tex('uTex', 0, this.sceneTex).f1('uThresh', fx.thresh).f1('uKnee', 0.6); this.full();
      var src = this.brightTex, sw = Math.max(2, this.w >> 1), sh = Math.max(2, this.h >> 1);
      for (i = 0; i < n; i++) {
        l = this.lv[i];
        gl.viewport(0, 0, l.w, l.h);
        gl.bindFramebuffer(gl.FRAMEBUFFER, l.tmpF); this.progs.blur.use().tex('uTex', 0, src).f2('uDir', 1 / sw, 0); this.full();
        gl.bindFramebuffer(gl.FRAMEBUFFER, l.outF); this.progs.blur.use().tex('uTex', 0, l.tmp).f2('uDir', 0, 1 / l.h); this.full();
        src = l.out; sw = l.w; sh = l.h;
      }
    }
    gl.bindFramebuffer(gl.FRAMEBUFFER, null); gl.viewport(0, 0, this.w, this.h);
    var W = [0.55, 0.50, 0.42, 0.34, 0.26], cp = this.progs.comp.use();
    cp.tex('uScene', 0, this.sceneTex);
    for (i = 0; i < 5; i++) cp.tex('uB' + i, i + 1, i < n ? this.lv[i].out : this.black);
    cp.f4('uW', i0(W, 0, n), i0(W, 1, n), i0(W, 2, n), i0(W, 3, n)).f1('uW4', i0(W, 4, n));
    cp.f2('uRes', this.w, this.h).f1('uTime', fx.time).f1('uCA', fx.ca).f1('uVig', fx.vig).f1('uGrain', fx.grain).f1('uExposure', fx.exposure).f1('uFade', fx.fade).f1('uSoft', fx.soft).f1('uBloomI', fx.bloom);
    this.full();
  };
  function i0(W, i, n) { return i < n ? W[i] : 0; }
  F3D.Post = Post;

  // ================================================================== orbit camera
  function OrbitCam() {
    this.fov = 0.5;                                   // vertical, radians
    this.az = 0; this.pol = 1.2; this.rad = 20; this.t = [0, 0, 0]; this.shx = 0; this.shy = 0; this.roll = 0;
    this.g = { az: 0, pol: 1.2, rad: 20, t: [0, 0, 0], shx: 0, shy: 0 };       // goals (user input drives these, the pose follows with damping)
    this.limits = { az: [-0.95, 0.95], pol: [0.72, 1.52], rad: [5, 60], box: [[-20, -20, -20], [20, 20, 20]] };
    this.tween = null; this.vAz = 0; this.vPol = 0; this.idle = 0; this.parX = 0; this.parY = 0; this.parTX = 0; this.parTY = 0; this.driftAmt = 1;
    this.eye = [0, 0, 20]; this.view = new Float32Array(16); this.proj = new Float32Array(16); this.vp = new Float32Array(16); this.ivp = new Float32Array(16);
    this.right = [1, 0, 0]; this.up = [0, 1, 0]; this.fwd = [0, 0, -1]; this.aspect = 1;
  }
  OrbitCam.prototype.pose = function () { return { az: this.az, pol: this.pol, rad: this.rad, t: this.t.slice(), shx: this.shx, shy: this.shy }; };
  OrbitCam.prototype.set = function (p) {
    this.az = this.g.az = p.az; this.pol = this.g.pol = p.pol; this.rad = this.g.rad = p.rad; this.t = p.t.slice(); this.g.t = p.t.slice();
    this.shx = this.g.shx = p.shx || 0; this.shy = this.g.shy = p.shy || 0; this.tween = null;
  };
  // fly to a pose with easing; opts: { dur, ease, roll0, onDone, spiral }
  OrbitCam.prototype.flyTo = function (p, opts) {
    opts = opts || {}; var self = this, from = this.pose();
    this.tween = { from: from, to: { az: p.az, pol: p.pol, rad: p.rad, t: p.t.slice(), shx: p.shx || 0, shy: p.shy || 0 }, t0: null, dur: (opts.dur || 1.2), ease: opts.ease || ease.inOutCubic, done: opts.onDone, roll0: opts.roll0 || 0, elapsed: 0 };
    this.vAz = this.vPol = 0; this.idle = 0; return this.tween;
  };
  OrbitCam.prototype.cancel = function () { if (this.tween) { this.g.az = this.az; this.g.pol = this.pol; this.g.rad = this.rad; this.g.t = this.t.slice(); this.g.shx = this.shx; this.g.shy = this.shy; this.tween = null; } };
  OrbitCam.prototype.clampGoal = function () {
    var L = this.limits, g = this.g;
    g.az = clamp(g.az, L.az[0], L.az[1]); g.pol = clamp(g.pol, L.pol[0], L.pol[1]); g.rad = clamp(g.rad, L.rad[0], L.rad[1]);
    for (var i = 0; i < 3; i++) g.t[i] = clamp(g.t[i], L.box[0][i], L.box[1][i]);
  };
  OrbitCam.prototype.update = function (dt, time) {
    var tw = this.tween, k = 1 - Math.exp(-dt * 9), i;
    this.idle += dt;
    if (tw) {
      tw.elapsed += dt; var p = clamp(tw.elapsed / tw.dur, 0, 1), e = tw.ease(p), f = tw.from, to = tw.to;
      this.az = lerp(f.az, to.az, e); this.pol = lerp(f.pol, to.pol, e); this.rad = lerp(f.rad, to.rad, e);
      for (i = 0; i < 3; i++) this.t[i] = lerp(f.t[i], to.t[i], e);
      this.shx = lerp(f.shx, to.shx, e); this.shy = lerp(f.shy, to.shy, e);
      this.roll = tw.roll0 * (1 - e);
      this.g.az = this.az; this.g.pol = this.pol; this.g.rad = this.rad; this.g.t = this.t.slice(); this.g.shx = this.shx; this.g.shy = this.shy;
      if (p >= 1) { this.tween = null; this.roll = 0; if (tw.done) tw.done(); }
    } else {
      this.g.az += this.vAz * dt; this.g.pol += this.vPol * dt;
      var dmp = Math.exp(-dt * 5.5); this.vAz *= dmp; this.vPol *= dmp; this.clampGoal();
      this.az = lerp(this.az, this.g.az, k); this.pol = lerp(this.pol, this.g.pol, k); this.rad = lerp(this.rad, this.g.rad, k);
      for (i = 0; i < 3; i++) this.t[i] = lerp(this.t[i], this.g.t[i], k);
      this.shx = lerp(this.shx, this.g.shx, 1 - Math.exp(-dt * 5)); this.shy = lerp(this.shy, this.g.shy, 1 - Math.exp(-dt * 5));
      this.roll = lerp(this.roll, 0, k);
    }
    this.parX = lerp(this.parX, this.parTX, 1 - Math.exp(-dt * 3)); this.parY = lerp(this.parY, this.parTY, 1 - Math.exp(-dt * 3));
    // idle drift: after a few quiet seconds the camera breathes very slowly
    var idleW = clamp((this.idle - 4) / 3, 0, 1) * this.driftAmt * (tw ? 0 : 1);
    var az = this.az + Math.sin(time * 0.11) * 0.075 * idleW, pol = this.pol + Math.sin(time * 0.083 + 1.3) * 0.028 * idleW;
    var sp = Math.sin(pol), r = this.rad, ex = this.t[0] + r * sp * Math.sin(az), ey = this.t[1] + r * Math.cos(pol), ez = this.t[2] + r * sp * Math.cos(az);
    // pointer parallax shifts the eye sideways while the target stays put
    var rx = Math.cos(az), rz = -Math.sin(az);
    ex += rx * this.parX * r * 0.028; ez += rz * this.parX * r * 0.028; ey += this.parY * r * 0.02;
    this.eye = [ex, ey, ez];
    var up = [Math.sin(this.roll), Math.cos(this.roll), 0];
    mat.lookAt(this.view, this.eye, this.t, up);
    var fx = this.t[0] - ex, fy = this.t[1] - ey, fz = this.t[2] - ez, fl = Math.hypot(fx, fy, fz) || 1; this.fwd = [fx / fl, fy / fl, fz / fl];
    this.right = [this.view[0], this.view[4], this.view[8]]; this.up = [this.view[1], this.view[5], this.view[9]];
  };
  OrbitCam.prototype.build = function (aspect) {
    this.aspect = aspect; mat.persp(this.proj, this.fov, aspect, 0.2, 400);
    this.proj[8] -= this.shx; this.proj[9] -= this.shy;     // lens shift (NDC units, + = scene moves right / up) without turning the camera
    mat.mul(this.vp, this.proj, this.view); mat.inv(this.ivp, this.vp);
  };
  // world -> [x px-normalised 0..1, y 0..1 (top = 0), depth w]
  OrbitCam.prototype.project = function (x, y, z, out) {
    var m = this.vp, cx = m[0] * x + m[4] * y + m[8] * z + m[12], cy = m[1] * x + m[5] * y + m[9] * z + m[13], cw = m[3] * x + m[7] * y + m[11] * z + m[15];
    out = out || [0, 0, 0]; out[0] = (cx / cw) * 0.5 + 0.5; out[1] = 0.5 - (cy / cw) * 0.5; out[2] = cw; return out;
  };
  OrbitCam.prototype.ray = function (nx, ny) {     // nx, ny in 0..1 (y down) -> {o, d}
    var m = this.ivp, X = nx * 2 - 1, Y = 1 - ny * 2;
    function un(z) { var x = m[0] * X + m[4] * Y + m[8] * z + m[12], y = m[1] * X + m[5] * Y + m[9] * z + m[13], zz = m[2] * X + m[6] * Y + m[10] * z + m[14], w = m[3] * X + m[7] * Y + m[11] * z + m[15]; return [x / w, y / w, zz / w]; }
    var a = un(-1), b = un(1), d = [b[0] - a[0], b[1] - a[1], b[2] - a[2]], l = Math.hypot(d[0], d[1], d[2]) || 1;
    return { o: this.eye.slice(), d: [d[0] / l, d[1] / l, d[2] / l] };
  };
  F3D.OrbitCam = OrbitCam;
})();
