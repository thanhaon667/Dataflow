# Data Flow 3D engine

Everything the Data Flow page's 3D mode needs, with **no third-party code** and no build step (plain browser scripts,
loaded by `desktop/static/shell.html`, served by the existing `/static` route):

| File | What it is |
|---|---|
| `shaders.js` | GLSL ES 3.00 sources: glass slab, health beacon, edge tube, point sprites, floor, lane panes, rings, backdrop + baked nebula, bloom / composite passes |
| `engine.js` | 4x4 matrices, a WebGL2 wrapper that tracks every GPU object it creates (`stats()` / `dispose()`), primitive meshes, the HDR + MSAA + bloom + composite pipeline, the damped orbit camera with eased fly-to tweens |
| `flow3d.js` | `window.Flow3D`: builds the scene from the `/api/flow` payload, labels, picking, interaction, cinematic tour, shockwaves, adaptive quality, context-loss recovery, mount / unmount lifecycle |

`static/flow.js` owns the 2D | 3D switch and calls `Flow3D.mount / unmount / setData / tour`; it hands the module its
data, the details drawer, the tooltip builders and `flowParams()` through a small `api3d` object, so the 3D view never
duplicates the data pipeline. Styles are in `static/flow3d.css`.

Why a home-made engine instead of Three.js? So the app stays fully offline with nothing vendored, no npm / Node and no
build step: the whole stack is three readable files. If a library is ever preferred, only this folder (and the `<script>`
tags in `shell.html`) would be replaced; record the version, licence and SHA-256 of every vendored file next to it.

Introspection for automated checks: `Flow3D.stats()` (frame rate, quality level, per-type GPU object counts, tear-down
report), `Flow3D.debug.*` (force quality, lose / restore the context, emit a shockwave, camera framing extents).
