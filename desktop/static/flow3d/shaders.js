/* ERP Desk - Data Flow 3D: GLSL sources (WebGL2 / GLSL ES 3.00).
   Everything is authored in LINEAR light; the composite pass tone-maps (ACES) and gamma-encodes once at the end.
   No third-party code: this file, engine.js and flow3d.js are the whole 3D stack. */
(function () {
  'use strict';
  var F3D = (window.F3D = window.F3D || {});
  var G = (F3D.glsl = {});
  var HEAD = '#version 300 es\nprecision highp float;\nprecision highp int;\n';

  var FOG = [
    'uniform vec3 uCam; uniform vec3 uFogCol; uniform float uFog;',
    'float fogAmt(vec3 w) { float d = length(uCam - w); float x = d * uFog; return 1.0 - exp(-x * x); }',
    'float mirrorFade(vec3 w, float on, float floorY) { return on > 0.5 ? 0.62 * exp(-max(0.0, floorY - w.y) * 0.13) : 1.0; }',
    'float dashf(float s, float period, float duty) { float f = fract(s / period); return smoothstep(0.0, 0.05, f) * (1.0 - smoothstep(duty - 0.05, duty, f)); }'
  ].join('\n');

  // ------------------------------------------------------------------ shared vertices
  G.fullVS = HEAD + [
    'out vec2 vUV;',
    'void main() { vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)); vUV = p; gl_Position = vec4(p * 2.0 - 1.0, 0.0, 1.0); }'
  ].join('\n');

  G.modelVS = HEAD + [
    'layout(location=0) in vec3 aPos; layout(location=1) in vec3 aNrm; layout(location=2) in vec3 aAux;',
    'uniform mat4 uVP; uniform mat4 uModel; uniform mat4 uMirror;',
    'out vec3 vW; out vec3 vN; out vec3 vLN; out vec3 vAux; out vec3 vLP;',
    'void main() {',
    '  vec4 w = uMirror * (uModel * vec4(aPos, 1.0));',
    '  vW = w.xyz;',
    '  vN = normalize(mat3(uMirror) * (mat3(uModel) * aNrm));',
    '  vLN = aNrm; vAux = aAux; vLP = aPos;',
    '  gl_Position = uVP * w;',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ node slab: holographic glass panel
  G.slabFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    FOG,
    'uniform float uTime; uniform vec2 uSize; uniform vec3 uTint; uniform vec3 uHc;',
    'uniform float uAuto; uniform float uGlow; uniform float uDim; uniform float uSel; uniform float uHover;',
    'uniform float uFlash; uniform float uSeed; uniform float uErr; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 o;',
    'void main() {',
    '  vec3 N = normalize(vN); vec3 V = normalize(uCam - vW);',
    '  float fr = pow(max(1.0 - abs(dot(N, V)), 0.0), 2.4);',
    '  vec3 tint = mix(uTint, uHc, uErr * 0.55);',
    '  vec3 col;',
    '  if (abs(vLN.z) > 0.5) {',
    '    vec2 p = vAux.xy * uSize; vec2 q = min(p, uSize - p);',
    '    float d = min(q.x, q.y); float s = (q.x < q.y) ? p.y : p.x; float g = vAux.y;',
    '    col = mix(vec3(0.006, 0.008, 0.016), vec3(0.020, 0.024, 0.048), g);',
    '    col += tint * 0.030 * (1.0 - g);',
    '    vec2 gp = abs(fract(p * 3.0) - 0.5);',
    '    col += tint * 0.020 * smoothstep(0.46, 0.5, max(gp.x, gp.y));',
    '    col += tint * 0.010 * (0.5 + 0.5 * sin(p.y * 90.0 - uTime * 1.8));',
    '    float sw = fract(uTime * 0.10 + uSeed);',
    '    float bd = (vAux.x + vAux.y * 0.4) - (sw * 2.2 - 0.4);',
    '    col += tint * 0.22 * exp(-bd * bd * 30.0) * (0.35 + uGlow * 0.5);',
    // frame: the automation class decides the line style (solid glow / dashed / dotted), like the 2D box border
    '    float width = uAuto < 0.5 ? 0.06 : (uAuto < 1.5 ? 0.052 : (uAuto < 2.5 ? 0.044 : 0.03));',
    '    float m = 1.0 - smoothstep(width, width + 0.025, d);',
    '    float dash = 1.0;',
    '    if (uAuto > 0.5 && uAuto < 1.5) dash = dashf(s + uTime * 0.12, 0.42, 0.68);',
    '    else if (uAuto > 1.5 && uAuto < 2.5) dash = dashf(s, 0.32, 0.5);',
    '    else if (uAuto > 4.5) dash = dashf(s, 0.14, 0.32);',
    '    float corner = step(q.x, 0.34) * step(q.y, 0.34);',
    '    m *= max(dash, corner);',
    '    float inten = uAuto < 0.5 ? 2.0 : (uAuto < 1.5 ? 1.7 : (uAuto < 2.5 ? 1.0 : (uAuto < 3.5 ? 0.55 : (uAuto < 4.5 ? 0.4 : 0.7))));',
    '    col += tint * m * inten * (0.6 + 0.5 * uGlow);',
    '    col += tint * 0.12 * exp(-d * 8.0) * (0.4 + uGlow);',
    '    col += tint * fr * 0.9;',
    '    col += vec3(1.0) * (uSel * (0.35 * m + 0.05) + uFlash * (0.15 + 1.3 * m));',
    '    col += tint * uHover * (0.5 * m + 0.08);',
    '  } else {',
    '    col = vec3(0.006, 0.008, 0.014) + tint * (0.10 + 0.9 * fr) * (0.5 + 0.3 * uGlow) + vec3(1.0) * uSel * 0.12;',
    '  }',
    '  col *= mix(1.0, 0.22, uDim);',
    '  col = mix(col, uFogCol, fogAmt(vW));',
    '  o = vec4(col, mirrorFade(vW, uMirrorOn, uFloorY));',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ health beacon (octahedron)
  // style: 0 solid (healthy) | 1 half (stale) | 2 flicker (error) | 3 hollow wireframe (no run / offline / idle) | 4 hatched (mocked)
  G.beaconFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    FOG,
    'uniform vec3 uCol; uniform float uStyle; uniform float uTime; uniform float uDim; uniform float uMirrorOn; uniform float uFloorY; uniform float uBoost;',
    'out vec4 o;',
    'void main() {',
    '  vec3 N = normalize(vN); vec3 V = normalize(uCam - vW);',
    '  float fr = pow(max(1.0 - abs(dot(N, V)), 0.0), 2.0);',
    '  vec3 c;',
    '  if (uStyle < 0.5) c = uCol * (1.5 + 1.4 * fr + 0.5 * sin(uTime * 2.4));',
    '  else if (uStyle < 1.5) c = mix(vec3(0.02), uCol * 1.7, step(0.0, vLP.y)) + uCol * fr;',
    '  else if (uStyle < 2.5) { float fl = step(0.9, fract(sin(floor(uTime * 11.0) * 12.9898) * 43758.5453)); c = uCol * (1.3 + 1.7 * fl + fr); }',
    '  else if (uStyle < 3.5) { float e = 1.0 - smoothstep(0.0, 0.07, min(vAux.x, min(vAux.y, vAux.z))); c = uCol * (0.10 + e * 1.7); }',
    '  else { float st = step(0.5, fract((vLP.x + vLP.y) * 5.0)); c = uCol * mix(0.10, 1.5, st) + uCol * fr * 0.4; }',
    '  c *= (1.0 + uBoost) * mix(1.0, 0.25, uDim);',
    '  c *= (1.0 - fogAmt(vW)) * mirrorFade(vW, uMirrorOn, uFloorY);',
    '  o = vec4(c, 1.0);',
    '}'
  ].join('\n');

  // solid emissive (arrow heads, orbit rings)
  G.solidFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    FOG,
    'uniform vec3 uColor; uniform float uIntensity; uniform float uDim; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 o;',
    'void main() {',
    '  vec3 N = normalize(vN); vec3 V = normalize(uCam - vW);',
    '  float fr = pow(max(1.0 - abs(dot(N, V)), 0.0), 1.6);',
    '  vec3 c = uColor * uIntensity * (0.55 + 1.2 * fr) * mix(1.0, 0.12, uDim);',
    '  c *= (1.0 - fogAmt(vW)) * mirrorFade(vW, uMirrorOn, uFloorY);',
    '  o = vec4(c, 1.0);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ edge tube
  G.tubeVS = HEAD + [
    'layout(location=0) in vec3 aPos; layout(location=1) in vec3 aDir; layout(location=2) in vec2 aT;',
    'uniform mat4 uVP; uniform mat4 uMirror; uniform float uRadius;',
    'out vec3 vW; out vec3 vDir; out vec2 vT;',
    'void main() {',
    '  vec4 w = uMirror * vec4(aPos + aDir * uRadius, 1.0);',
    '  vW = w.xyz; vDir = mat3(uMirror) * aDir; vT = aT; gl_Position = uVP * w;',
    '}'
  ].join('\n');
  // mode: 0 live | 1 dormant / unknown (dashed) | 2 manual (dotted) | 3 passive (thin solid)
  G.tubeFS = HEAD + [
    'in vec3 vW; in vec3 vDir; in vec2 vT;',
    FOG,
    'uniform vec3 uColor; uniform float uMode; uniform float uDim; uniform float uHot; uniform float uTrace; uniform float uTime; uniform float uAct;',
    'uniform vec4 uPulse; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 o;',
    'void main() {',
    '  vec3 V = normalize(uCam - vW); vec3 N = normalize(vDir);',
    '  float core = mix(0.30, 1.0, pow(clamp(abs(dot(N, V)), 0.0, 1.0), 1.5));',
    '  float s = vT.x; float inten;',
    '  if (uMode < 0.5) { float st = fract(s * 0.5 - uTime * (0.5 + uAct)); inten = 0.75 + 0.9 * st * st * st * st; }',
    '  else if (uMode < 1.5) { inten = 0.15 + 0.6 * dashf(s - uTime * 0.35, 0.5, 0.55); }',
    '  else if (uMode < 2.5) { inten = 0.08 + 0.8 * dashf(s - uTime * 0.2, 0.32, 0.16); }',
    '  else { inten = 0.42; }',
    '  inten *= smoothstep(0.0, 0.03, vT.y);',
    '  float pz = (vT.y - uPulse.x) / max(uPulse.y, 0.001); float pw = exp(-pz * pz) * uPulse.z;',
    '  vec3 col = mix(uColor, vec3(1.0), uTrace * 0.85);',
    '  col = col * inten * core * (1.0 + uHot * 1.1 + uTrace * 2.0) + (vec3(1.0, 0.95, 0.9) * 3.0 + uColor * 2.0) * pw * core;',
    '  col *= mix(1.0, 0.06, uDim);',
    '  col *= (1.0 - fogAmt(vW)) * mirrorFade(vW, uMirrorOn, uFloorY);',
    '  o = vec4(col, 1.0);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ point sprites (data particles, halos, sparks, dust)
  G.particleVS = HEAD + [
    'layout(location=0) in vec3 aPos; layout(location=1) in vec4 aCol; layout(location=2) in float aSize;',
    'uniform mat4 uVP; uniform mat4 uMirror; uniform float uPx; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 vCol; out float vSize;',
    'void main() {',
    '  vec4 w = uMirror * vec4(aPos, 1.0);',
    '  vec4 c = uVP * w; gl_Position = c;',
    '  gl_PointSize = clamp(aSize * uPx / max(c.w, 0.05), 1.0, 300.0);',
    '  float k = uMirrorOn > 0.5 ? 0.62 * exp(-max(0.0, uFloorY - w.y) * 0.13) : 1.0;',
    '  vCol = vec4(aCol.rgb, aCol.a * k); vSize = aSize;',
    '}'
  ].join('\n');
  G.particleFS = HEAD + [
    'in vec4 vCol; in float vSize; out vec4 o;',
    'void main() {',
    '  vec2 p = gl_PointCoord * 2.0 - 1.0; float r2 = dot(p, p);',
    '  float big = smoothstep(1.0, 3.0, vSize);',
    '  float core = exp(-r2 * mix(11.0, 3.4, big)); float halo = exp(-r2 * 2.6) * mix(0.32, 0.0, big);',
    '  float a = (core + halo) * (1.0 - smoothstep(0.80, 1.0, sqrt(r2)));',
    '  o = vec4(vCol.rgb * a * vCol.a, 1.0);',
    '}'
  ].join('\n');

  G.starsVS = HEAD + [
    'layout(location=0) in vec3 aDir; layout(location=1) in vec3 aCol; layout(location=2) in vec2 aData;',
    'uniform mat4 uVP; uniform vec3 uCam; uniform float uTime; uniform float uDpr;',
    'out vec3 vCol;',
    'void main() {',
    '  gl_Position = uVP * vec4(uCam + aDir * 120.0, 1.0);',
    '  float tw = 0.65 + 0.35 * sin(uTime * (0.6 + aData.y * 1.4) + aData.y * 40.0);',
    '  gl_PointSize = max(1.0, aData.x * uDpr * (0.8 + 0.2 * tw)); vCol = aCol * tw;',
    '}'
  ].join('\n');
  G.starsFS = HEAD + [
    'in vec3 vCol; out vec4 o;',
    'void main() { vec2 p = gl_PointCoord * 2.0 - 1.0; float a = exp(-dot(p, p) * 3.5); o = vec4(vCol * a, 1.0); }'
  ].join('\n');

  // ------------------------------------------------------------------ backdrop + baked nebula
  G.nebulaFS = HEAD + [
    'in vec2 vUV; out vec4 o;',
    'float h3(vec3 p) { p = fract(p * 0.3183099 + 0.1); p *= 17.0; return fract(p.x * p.y * p.z * (p.x + p.y + p.z)); }',
    'float vn(vec3 x) {',
    '  vec3 i = floor(x); vec3 f = fract(x); f = f * f * (3.0 - 2.0 * f);',
    '  return mix(mix(mix(h3(i), h3(i + vec3(1,0,0)), f.x), mix(h3(i + vec3(0,1,0)), h3(i + vec3(1,1,0)), f.x), f.y),',
    '             mix(mix(h3(i + vec3(0,0,1)), h3(i + vec3(1,0,1)), f.x), mix(h3(i + vec3(0,1,1)), h3(i + vec3(1,1,1)), f.x), f.y), f.z);',
    '}',
    'float fbm(vec3 p) { float a = 0.5, s = 0.0; for (int i = 0; i < 5; i++) { s += a * vn(p); p = p * 2.03 + vec3(3.1, 1.7, 5.3); a *= 0.5; } return s; }',
    'void main() {',
    '  float lon = (vUV.x - 0.5) * 6.2831853; float lat = (vUV.y - 0.5) * 3.1415927;',
    '  vec3 d = vec3(cos(lat) * sin(lon), sin(lat), cos(lat) * cos(lon));',
    '  vec3 w = d * 2.2; vec3 q = vec3(fbm(w + 1.3), fbm(w + 7.1), fbm(w + 3.7));',
    '  float r = fbm(w * 1.1 + q * 1.4); float g = fbm(w * 1.3 + q * 1.1 + 11.0); float b = fbm(w * 0.9 + q * 1.7 + 23.0);',
    '  o = vec4(smoothstep(0.42, 0.86, r), smoothstep(0.42, 0.86, g), smoothstep(0.44, 0.88, b), 1.0);',
    '}'
  ].join('\n');

  G.backdropFS = HEAD + [
    'in vec2 vUV; out vec4 o;',
    'uniform sampler2D uNeb; uniform vec3 uRight; uniform vec3 uUp; uniform vec3 uFwd; uniform vec2 uTan; uniform vec2 uShift; uniform float uTime; uniform float uAspect;',
    'vec3 lin(vec3 c) { return pow(c, vec3(2.2)); }',
    'void main() {',
    '  vec2 ndc = vUV * 2.0 - 1.0 - uShift;',
    '  vec3 d = normalize(uFwd + uRight * ndc.x * uTan.x + uUp * ndc.y * uTan.y);',
    '  float lon = atan(d.x, d.z) * 0.15915494 + 0.5; float lat = asin(clamp(d.y, -1.0, 1.0)) * 0.31830989 + 0.5;',
    '  vec3 n1 = texture(uNeb, vec2(lon + uTime * 0.0016, lat)).rgb;',
    '  vec3 n2 = texture(uNeb, vec2(lon * 1.0 - uTime * 0.0011 + 0.37, lat * 0.9 + 0.05)).rgb;',
    '  vec3 neb = n1 + n2 * 0.55;',
    '  vec3 coral = lin(vec3(1.0, 0.353, 0.212)); vec3 violet = lin(vec3(0.486, 0.361, 1.0)); vec3 blue = lin(vec3(0.204, 0.322, 0.922));',
    '  vec3 col = mix(lin(vec3(0.085, 0.09, 0.115)), lin(vec3(0.045, 0.048, 0.07)), vUV.y);',
    '  vec2 c = vUV * vec2(uAspect, 1.0);',
    '  col += coral * exp(-dot(c - vec2(0.0, 0.0), c - vec2(0.0, 0.0)) * 1.6) * 0.06;',
    '  col += violet * exp(-dot(c - vec2(uAspect, 1.0), c - vec2(uAspect, 1.0)) * 1.8) * 0.10;',
    '  col += blue * exp(-dot(c - vec2(uAspect * 0.55, 0.0), c - vec2(uAspect * 0.55, 0.0)) * 2.4) * 0.07;',
    '  col += coral * neb.r * 0.035 + blue * neb.g * 0.07 + violet * neb.b * 0.07;',
    '  col += vec3(0.020, 0.018, 0.040) * exp(-abs(d.y + 0.06) * 8.0);',
    '  o = vec4(col, 1.0);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ reflective floor
  G.floorFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    'uniform vec3 uCam; uniform float uTime; uniform vec2 uCenter; uniform vec4 uPool[4]; uniform vec3 uPoolCol[4]; uniform float uFlowDir;',
    'out vec4 o;',
    'void main() {',
    '  vec2 xz = vW.xz;',
    '  vec2 g1 = abs(fract(xz - 0.5) - 0.5) / max(fwidth(xz), vec2(0.0001)); float l1 = 1.0 - min(min(g1.x, g1.y), 1.0);',
    '  vec2 x5 = xz / 5.0; vec2 g5 = abs(fract(x5 - 0.5) - 0.5) / max(fwidth(x5), vec2(0.0001)); float l5 = 1.0 - min(min(g5.x, g5.y), 1.0);',
    '  float dist = length(xz - uCenter); float fade = exp(-dist * 0.045);',
    '  vec3 col = vec3(0.008, 0.010, 0.022);',
    '  col += vec3(0.10, 0.13, 0.42) * l1 * 0.28 * fade;',
    '  col += vec3(0.24, 0.30, 1.00) * l5 * 0.50 * fade;',
    '  float band = pow(max(0.5 + 0.5 * sin(xz.x * 0.30 * uFlowDir - uTime * 1.1), 0.0), 14.0);',
    '  col += vec3(0.62, 0.36, 1.0) * (l1 * 0.7 + l5) * band * 1.4 * fade;',
    '  for (int i = 0; i < 4; i++) { vec2 d = xz - uPool[i].xy; float k = exp(-dot(d, d) / (uPool[i].z * uPool[i].z)); col += uPoolCol[i] * k * 0.30; }',
    '  float a = 0.66 * (1.0 - smoothstep(50.0, 105.0, dist));',
    '  o = vec4(col, a);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ lane glass pane (additive)
  G.paneFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    FOG,
    'uniform vec3 uColor; uniform vec2 uSize; uniform float uTime; uniform float uSeed; uniform float uIntensity; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 o;',
    'void main() {',
    '  vec2 uv = vAux.xy; vec2 p = uv * uSize; vec2 q = min(p, uSize - p); float d = min(q.x, q.y);',
    '  float body = 0.010 + 0.040 * pow(clamp(1.0 - uv.y, 0.0, 1.0), 2.2);',
    '  float scan = mix(1.0, 0.5 + 0.5 * sin(p.y * 5.0 - uTime * 0.7), 0.16);',
    '  float fr = exp(-d * 11.0) * 0.30;',
    '  float ver = exp(-q.x * 4.5) * (0.10 + 0.25 * uv.y);',
    '  float sx = (uv.x - fract(uTime * 0.06 + uSeed)) * 7.0; float sweep = exp(-sx * sx) * 0.07;',
    '  vec3 c = uColor * (body * scan + fr + ver + sweep) * uIntensity;',
    '  c *= (1.0 - fogAmt(vW)) * mirrorFade(vW, uMirrorOn, uFloorY);',
    '  o = vec4(c, 1.0);',
    '}'
  ].join('\n');

  // expanding ring (shockwaves, floor ripples). aux.xy = 0..1 quad uv
  G.ringFS = HEAD + [
    'in vec3 vW; in vec3 vN; in vec3 vLN; in vec3 vAux; in vec3 vLP;',
    FOG,
    'uniform vec3 uColor; uniform float uR; uniform float uW; uniform float uI; uniform float uMirrorOn; uniform float uFloorY;',
    'out vec4 o;',
    'void main() {',
    '  vec2 p = vAux.xy * 2.0 - 1.0; float r = length(p);',
    '  float rr = (r - uR) / max(uW, 0.001); float k = exp(-rr * rr) * (1.0 - smoothstep(0.72, 1.0, r));',
    '  vec3 c = uColor * k * uI * (1.0 - fogAmt(vW)) * mirrorFade(vW, uMirrorOn, uFloorY);',
    '  o = vec4(c, 1.0);',
    '}'
  ].join('\n');

  // ------------------------------------------------------------------ post-processing
  G.brightFS = HEAD + [
    'in vec2 vUV; out vec4 o; uniform sampler2D uTex; uniform float uThresh; uniform float uKnee;',
    'void main() {',
    '  vec3 c = texture(uTex, vUV).rgb; if (any(isnan(c)) || any(isinf(c))) c = vec3(0.0); c = max(c, vec3(0.0)); float br = max(c.r, max(c.g, c.b));',
    '  float soft = clamp(br - uThresh + uKnee, 0.0, 2.0 * uKnee); soft = soft * soft / (4.0 * uKnee + 1e-4);',
    '  float w = max(soft, br - uThresh) / max(br, 1e-4);',
    '  o = vec4(min(c * w, vec3(24.0)), 1.0);',
    '}'
  ].join('\n');

  G.blurFS = HEAD + [
    'in vec2 vUV; out vec4 o; uniform sampler2D uTex; uniform vec2 uDir;',
    'void main() {',
    '  vec3 c = texture(uTex, vUV).rgb * 0.2270270270;',
    '  c += (texture(uTex, vUV + uDir * 1.3846153846).rgb + texture(uTex, vUV - uDir * 1.3846153846).rgb) * 0.3162162162;',
    '  c += (texture(uTex, vUV + uDir * 3.2307692308).rgb + texture(uTex, vUV - uDir * 3.2307692308).rgb) * 0.0702702703;',
    '  o = vec4(c, 1.0);',
    '}'
  ].join('\n');

  G.compositeFS = HEAD + [
    'in vec2 vUV; out vec4 o;',
    'uniform sampler2D uScene; uniform sampler2D uB0; uniform sampler2D uB1; uniform sampler2D uB2; uniform sampler2D uB3; uniform sampler2D uB4;',
    'uniform vec4 uW; uniform float uW4; uniform vec2 uRes; uniform float uTime; uniform float uCA; uniform float uVig; uniform float uGrain;',
    'uniform float uExposure; uniform float uFade; uniform float uSoft; uniform float uBloomI;',
    'vec3 aces(vec3 x) { return clamp((x * (2.51 * x + 0.03)) / (x * (2.43 * x + 0.59) + 0.14), 0.0, 1.0); }',
    'void main() {',
    '  vec2 uv = vUV; vec2 cc = (uv - 0.5) * vec2(uRes.x / uRes.y, 1.0); float r2 = dot(cc, cc);',
    '  vec2 off = (uv - 0.5) * uCA * r2;',
    '  vec3 c = vec3(texture(uScene, uv + off).r, texture(uScene, uv).g, texture(uScene, uv - off).b);',
    '  float edge = smoothstep(0.16, 0.62, r2) * uSoft;',
    '  if (edge > 0.02) {',   // soft focus toward the frame edges: a cheap tilt-shift style depth-of-field
    '    vec2 px = 1.0 / uRes; vec3 acc = vec3(0.0);',
    '    for (int i = 0; i < 8; i++) { float a = float(i) * 0.785398; acc += texture(uScene, uv + vec2(cos(a), sin(a)) * px * (2.0 + 5.0 * edge)).rgb; }',
    '    c = mix(c, acc / 8.0, edge * 0.8);',
    '  }',
    '  vec3 b = texture(uB0, uv).rgb * uW.x + texture(uB1, uv).rgb * uW.y + texture(uB2, uv).rgb * uW.z + texture(uB3, uv).rgb * uW.w + texture(uB4, uv).rgb * uW4;',
    '  c += b * uBloomI;',
    '  if (any(isnan(c))) c = vec3(0.0);',
    '  c = aces(max(c, vec3(0.0)) * uExposure);',
    '  c = pow(c, vec3(1.0 / 2.2));',
    '  c *= 1.0 - uVig * smoothstep(0.10, 0.75, r2);',
    '  float n = fract(sin(dot(uv * uRes + fract(uTime) * 61.0, vec2(12.9898, 78.233))) * 43758.5453);',
    '  c += (n - 0.5) * uGrain;',
    '  o = vec4(c * uFade, 1.0);',
    '}'
  ].join('\n');
})();
