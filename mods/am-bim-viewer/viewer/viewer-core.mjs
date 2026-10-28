// am-bim-viewer core: camera math and Rhino-style input mapping.
//
// ORIENTATION CONTRACT
//   World = Revit / Rhino: right-handed, X = East, Y = North, Z = Up, decimal feet.
//   Model data stays in these coordinates. There is NO root rotation and NO
//   negative scale. three.js is Y-up only by default: we never rely on that.
//   The camera orientation is built explicitly from azimuth / elevation as a
//   right-handed basis (right, up, back), so Top view (looking down -Z) is not
//   a lookAt singularity. camera.up is set to world Z for anything else that
//   reads it.
//
//   azimuth   = angle in the XY plane of the direction target -> camera,
//               measured from +X toward +Y (radians).
//   elevation = angle of that direction above the XY plane (radians).
//
// This file has no imports. The viewer page inlines it; node tests import it.
// Functions that touch three.js take the THREE namespace as an argument.

export const WORLD_UP = [0, 0, 1];
const DEG = Math.PI / 180;

// Named views. Front = camera at -Y looking toward +Y. Right = camera at +X
// looking toward -X. Perspective = south-east iso (camera at +X, -Y, +Z).
export const NAMED_VIEWS = {
  top: { azimuth: -90 * DEG, elevation: 90 * DEG, projection: 'parallel' },
  front: { azimuth: -90 * DEG, elevation: 0, projection: 'parallel' },
  right: { azimuth: 0, elevation: 0, projection: 'parallel' },
  perspective: { azimuth: -45 * DEG, elevation: Math.atan(1 / Math.SQRT2), projection: 'perspective' },
  // View cube faces not above: Back = camera at +Y looking south; Left = camera at -X looking east;
  // Bottom = looking up +Z with north at the top of the screen (east is then on the left).
  back: { azimuth: 90 * DEG, elevation: 0, projection: 'parallel' },
  left: { azimuth: 180 * DEG, elevation: 0, projection: 'parallel' },
  bottom: { azimuth: 90 * DEG, elevation: -90 * DEG, projection: 'parallel' },
};

// Rhino mouse mapping (Windows defaults), from:
//   https://www.rhino3d.com/docs/guides/modeling/rhino-windows-navigation/
//   https://docs.mcneel.com/rhino/8/help/en-us/seealso/sak_navigate.htm
//   https://docs.mcneel.com/rhino/8/help/en-us/commands/zoom.htm
// Perspective view: right drag = rotate, Shift+right drag = pan.
// Parallel view (Top/Front/Right): right drag = pan, Ctrl+Shift+right drag = rotate.
// Both: Ctrl+right drag up/down = zoom. Wheel = zoom at cursor; Ctrl+wheel = zoom at view center.
// Left button = select. Middle drag = pan (not a Rhino default; added for laptops).
// button follows DOM MouseEvent.button: 0 left, 1 middle, 2 right.
export function mapPointer({ button, shift = false, ctrl = false, isParallel = false }) {
  if (button === 0) return 'select';
  if (button === 1) return 'pan';
  if (button !== 2) return 'none';
  if (ctrl && shift) return 'rotate';
  if (ctrl) return 'zoom';
  if (shift) return 'pan';
  return isParallel ? 'pan' : 'rotate';
}

export function mapWheel({ ctrl = false }) {
  return ctrl ? 'zoom-center' : 'zoom-cursor';
}

export function createViewState() {
  return {
    target: [0, 0, 0],
    azimuth: NAMED_VIEWS.perspective.azimuth,
    elevation: NAMED_VIEWS.perspective.elevation,
    distance: 100,
    viewHeight: 60, // parallel projection: world height of the viewport
    projection: 'perspective',
    fov: 40, // perspective vertical field of view, degrees
    aspect: 1.5, // viewport width / height; the viewer updates it
    sceneCenter: [0, 0, 0], // model bounds, for clipping planes
    sceneRadius: 50,
  };
}

// Right-handed camera basis. r = screen right, u = screen up, d = from target
// toward camera (the camera looks along -d). r x u = d.
export function cameraBasis(azimuth, elevation) {
  const ca = Math.cos(azimuth), sa = Math.sin(azimuth);
  const ce = Math.cos(elevation), se = Math.sin(elevation);
  const d = [ce * ca, ce * sa, se];
  const r = [-sa, ca, 0];
  const u = cross(d, r);
  return { r, u, d };
}

export function cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}

function add(a, b, s = 1) {
  return [a[0] + b[0] * s, a[1] + b[1] * s, a[2] + b[2] * s];
}

export function cameraPosition(state) {
  const { d } = cameraBasis(state.azimuth, state.elevation);
  return add(state.target, d, state.distance);
}

// Half height of the visible region at the target plane, in world units.
export function halfHeight(state) {
  if (state.projection === 'parallel') return state.viewHeight / 2;
  return state.distance * Math.tan((state.fov * DEG) / 2);
}

export function setNamedView(state, name, bounds) {
  const v = NAMED_VIEWS[name];
  if (!v) throw new Error('Unknown view: ' + name);
  state.azimuth = v.azimuth;
  state.elevation = v.elevation;
  state.projection = v.projection;
  if (bounds) {
    setScene(state, bounds);
    fitBounds(state, bounds);
  }
  return state;
}

function boundsCenterRadius(bounds) {
  const c = [0, 1, 2].map(i => (bounds.min[i] + bounds.max[i]) / 2);
  const ext = [0, 1, 2].map(i => bounds.max[i] - bounds.min[i]);
  return { c, radius: Math.max(1e-6, Math.hypot(ext[0], ext[1], ext[2]) / 2) };
}

// Remember the whole model's extent. Clipping planes use it, so zooming in
// on a detail never moves the camera into the model.
export function setScene(state, bounds) {
  const { c, radius } = boundsCenterRadius(bounds);
  state.sceneCenter = c;
  state.sceneRadius = radius;
  return state;
}

// bounds = { min: [x,y,z], max: [x,y,z] }
// Keeps the view direction; centers the target on the bounds and fits their
// bounding sphere inside the viewport (both width and height).
export function fitBounds(state, bounds, margin = 1.15) {
  const { c, radius: r0 } = boundsCenterRadius(bounds);
  const radius = r0 * margin;
  const aspect = state.aspect > 0 ? state.aspect : 1;
  const halfV = (state.fov * DEG) / 2;
  const halfH = Math.atan(Math.tan(halfV) * aspect);
  state.target = c;
  state.viewHeight = (radius * 2) / Math.min(1, aspect);
  state.distance = radius / Math.sin(Math.min(halfV, halfH));
  return state;
}

// Zoom Selected (Rhino): same direction, target = element center, fitted.
export const frameBounds = fitBounds;

// Smooth camera moves: blend two states, t in 0..1 (eased by the caller).
export function interpolateView(a, b, t) {
  let dAz = b.azimuth - a.azimuth;
  dAz = Math.atan2(Math.sin(dAz), Math.cos(dAz)); // shortest way round
  const geo = (x, y) => x * Math.pow(y / x, t);
  return {
    ...b,
    target: [0, 1, 2].map(i => a.target[i] + (b.target[i] - a.target[i]) * t),
    azimuth: a.azimuth + dAz * t,
    elevation: a.elevation + (b.elevation - a.elevation) * t,
    distance: geo(a.distance, b.distance),
    viewHeight: geo(a.viewHeight, b.viewHeight),
  };
}

export function easeInOut(t) {
  return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
}

const ORBIT_RAD_PER_PX = 0.008;

// Drag (dx right, dy down, in pixels) rotates as a turntable around world Z.
// Convention: the near face of the model follows the cursor ("grab the model").
// Rhino's docs do not state the direction; this matches Rhino's behaviour as
// we know it and three.js OrbitControls. Horizon stays level: no roll ever.
export function orbit(state, dx, dy) {
  state.azimuth -= dx * ORBIT_RAD_PER_PX;
  state.elevation += dy * ORBIT_RAD_PER_PX;
  const lim = 90 * DEG;
  state.elevation = Math.max(-lim, Math.min(lim, state.elevation));
  return state;
}

// Pan so the content under the cursor follows the cursor.
export function pan(state, dx, dy, viewportHeightPx) {
  const { r, u } = cameraBasis(state.azimuth, state.elevation);
  const perPx = (2 * halfHeight(state)) / viewportHeightPx;
  state.target = add(add(state.target, r, -dx * perPx), u, dy * perPx);
  return state;
}

// Ctrl+right drag: up (dy < 0) zooms in.
export function dragZoomFactor(dy) {
  return Math.exp(dy * 0.01);
}

// Scale the view by factor (< 1 zooms in) about the point under (ndcX, ndcY)
// on the target plane. ndc = normalized device coords, -1..1, +y up.
export function zoomAt(state, factor, ndcX = 0, ndcY = 0, aspect = 1) {
  const { r, u } = cameraBasis(state.azimuth, state.elevation);
  const hh = halfHeight(state);
  const p = add(add(state.target, r, ndcX * hh * aspect), u, ndcY * hh);
  state.target = [0, 1, 2].map(i => p[i] + (state.target[i] - p[i]) * factor);
  state.distance *= factor;
  state.viewHeight *= factor;
  return state;
}

// Apply the state to a three.js camera pair. Returns the active camera.
export function applyView(THREE, state, cams, aspect) {
  const { r, u, d } = cameraBasis(state.azimuth, state.elevation);
  const cam = state.projection === 'parallel' ? cams.ortho : cams.persp;
  const hh = halfHeight(state);
  if (cam.isOrthographicCamera) {
    cam.left = -hh * aspect;
    cam.right = hh * aspect;
    cam.top = hh;
    cam.bottom = -hh;
    // Parallel camera sits outside the whole model, whatever the zoom, so the
    // near plane never cuts it. Depth comes from the scene, not the zoom.
    const off = Math.hypot(...[0, 1, 2].map(i => state.target[i] - state.sceneCenter[i]));
    const back = off + state.sceneRadius * 2 + hh;
    cam.position.fromArray(add(state.target, d, back));
    cam.near = Math.max(0.01, back * 1e-4);
    cam.far = back * 2 + state.sceneRadius * 2;
  } else {
    cam.fov = state.fov;
    cam.aspect = aspect;
    cam.position.fromArray(add(state.target, d, state.distance));
    cam.near = Math.max(0.01, state.distance / 1000);
    const off = Math.hypot(...[0, 1, 2].map(i => state.target[i] - state.sceneCenter[i]));
    cam.far = Math.max(state.distance * 100, state.distance + off + state.sceneRadius * 2);
  }
  cam.up.set(WORLD_UP[0], WORLD_UP[1], WORLD_UP[2]);
  const m = new THREE.Matrix4().makeBasis(
    new THREE.Vector3().fromArray(r),
    new THREE.Vector3().fromArray(u),
    new THREE.Vector3().fromArray(d),
  );
  cam.quaternion.setFromRotationMatrix(m);
  cam.updateProjectionMatrix();
  cam.updateMatrixWorld(true);
  return cam;
}

// Screen direction of each world axis for the gizmo: [x right, y up] per axis.
export function gizmoAxes(state) {
  const { r, u } = cameraBasis(state.azimuth, state.elevation);
  return {
    x: [r[0], u[0]],
    y: [r[1], u[1]],
    z: [r[2], u[2]],
  };
}

// Revit internal length is decimal feet.
export function formatLength(ft) {
  if (typeof ft !== 'number' || !isFinite(ft)) return String(ft);
  return ft.toFixed(2) + ' ft (' + (ft * 0.3048).toFixed(3) + ' m)';
}

export function formatParam(value) {
  if (value && typeof value === 'object' && 'v' in value) {
    const unit = value.u;
    if (unit === 'ft') return formatLength(value.v);
    if (unit === 'sq ft') return value.v.toFixed(2) + ' sq ft (' + (value.v * 0.09290304).toFixed(3) + ' m²)';
    if (unit === 'cu ft') return value.v.toFixed(2) + ' cu ft (' + (value.v * 0.0283168466).toFixed(3) + ' m³)';
    return String(value.v) + (unit ? ' ' + unit : '');
  }
  return value === null || value === undefined ? '' : String(value);
}

export function meshBounds(elements) {
  const min = [Infinity, Infinity, Infinity];
  const max = [-Infinity, -Infinity, -Infinity];
  for (const el of elements) {
    const v = (el.mesh && el.mesh.vertices) || [];
    for (let i = 0; i + 2 < v.length; i += 3) {
      for (let k = 0; k < 3; k++) {
        if (v[i + k] < min[k]) min[k] = v[i + k];
        if (v[i + k] > max[k]) max[k] = v[i + k];
      }
    }
  }
  if (min[0] === Infinity) return { min: [-1, -1, -1], max: [1, 1, 1] };
  return { min, max };
}

export function elementBounds(el) {
  return meshBounds([el]);
}

// One flat-shaded three.js geometry per element (Revit coordinates as is).
export function buildElementGeometry(THREE, el) {
  const g = new THREE.BufferGeometry();
  g.setAttribute('position', new THREE.Float32BufferAttribute(el.mesh.vertices, 3));
  g.setIndex(el.mesh.indices);
  const flat = g.toNonIndexed();
  flat.computeVertexNormals();
  flat.computeBoundingSphere();
  return flat;
}

// Left click: the nearest visible element mesh under ndc ({x, y}, -1..1), or
// null for empty space. Child objects (edge lines, outlines) are never picked.
export function pickElement(raycaster, meshes, ndc, camera) {
  raycaster.setFromCamera(ndc, camera);
  const hits = raycaster.intersectObjects(meshes.filter(m => m.visible), false);
  return hits.length ? hits[0].object : null;
}

// Left click selection. A click on the selected element changes nothing, so
// the two clicks of a double-click never redraw the panel twice.
export function nextSelection(currentId, pickedEl) {
  const id = pickedEl ? pickedEl.id : null;
  return { id, isChanged: String(id) !== String(currentId) };
}

// Double-click: on an element, Zoom Selected (keep direction, target = its
// center, fit its box); on empty space, zoom extents. Returns the end state of
// the camera move; the page animates to it.
export function dblClickView(state, pickedEl, modelBounds) {
  const end = { ...state, target: state.target.slice() };
  return fitBounds(end, pickedEl ? elementBounds(pickedEl) : modelBounds);
}

// ---- World-space ground grid ----
// Minor/major spacing from the model size: about 50-500 minor cells across.
export function gridSteps(extent) {
  const minor = Math.pow(10, Math.floor(Math.log10(Math.max(extent, 1e-6) / 50)));
  return { minor, major: minor * 10 };
}

// Placement of the grid for the given (visible) model bounds: in the XY plane
// just below the lowest point, centered on the XY box, fading out at ~1.75x
// the larger XY extent.
export function gridLayout(bounds) {
  const ex = bounds.max[0] - bounds.min[0], ey = bounds.max[1] - bounds.min[1];
  const extent = Math.max(ex, ey, 1e-3);
  const { minor, major } = gridSteps(extent);
  const fade = extent * 1.75;
  return {
    center: [(bounds.min[0] + bounds.max[0]) / 2, (bounds.min[1] + bounds.max[1]) / 2],
    z: bounds.min[2] - extent * 1e-4, // a hair below the base: no z-fighting
    minor, major, fade,
  };
}

// One plane in the scene graph (not a screen overlay). A shader draws the
// lines from world XY with a radial alpha fade, so it has no hard edge.
export function createGroundGrid(THREE, bounds, color = '#f4f4f1') {
  const mat = new THREE.ShaderMaterial({
    transparent: true, depthWrite: false, side: THREE.DoubleSide,
    uniforms: {
      uMinor: { value: 1 }, uMajor: { value: 10 }, uFade: { value: 100 },
      uCenter: { value: new THREE.Vector2() }, uColor: { value: new THREE.Color(color) }, uOpacity: { value: 1 },
      uDensityFade: { value: 1 }, // 1: lines fade out where cells get denser than a few px (no moire); 0: off (test control)
    },
    vertexShader: 'varying vec3 vWorld; void main() { vec4 w = modelMatrix * vec4(position, 1.0); vWorld = w.xyz; gl_Position = projectionMatrix * viewMatrix * w; }',
    fragmentShader: [
      'uniform float uMinor; uniform float uMajor; uniform float uFade; uniform vec2 uCenter; uniform vec3 uColor; uniform float uOpacity; uniform float uDensityFade;',
      'varying vec3 vWorld;',
      // Anti-aliased line coverage from fwidth(); then fade by screen-space density: fwidth(q) is
      // cells per pixel, so as one cell shrinks from ~25 px to ~6 px (far away, or grazing angles)
      // the lines fade to 0 and dissolve into the clean background instead of aliasing into moire.
      'float gridLine(vec2 p, float s) { vec2 q = p / s; vec2 w = fwidth(q); vec2 g = abs(fract(q - 0.5) - 0.5) / w; float line = 1.0 - min(min(g.x, g.y), 1.0);',
      '  float dens = max(w.x, w.y); return line * mix(1.0, 1.0 - smoothstep(0.04, 0.16, dens), uDensityFade); }',
      'void main() {',
      '  vec2 p = vWorld.xy;',
      '  float d = length(p - uCenter);',
      '  float fade = 1.0 - smoothstep(uFade * 0.3, uFade, d);',
      '  float a = max(gridLine(p, uMinor) * 0.16, gridLine(p, uMajor) * 0.38) * fade * uOpacity;',
      '  if (a < 0.004) discard;',
      '  gl_FragColor = vec4(uColor, a);',
      '}',
    ].join('\n'),
  });
  const grid = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), mat); // PlaneGeometry lies in XY: right for Z up
  grid.name = 'ground-grid';
  grid.renderOrder = -1; // drawn before the model
  grid.userData.isGrid = true;
  placeGroundGrid(grid, bounds);
  return grid;
}

export function placeGroundGrid(grid, bounds) {
  const g = gridLayout(bounds);
  grid.position.set(g.center[0], g.center[1], g.z);
  grid.scale.set(g.fade, g.fade, 1);
  const u = grid.material.uniforms;
  u.uMinor.value = g.minor; u.uMajor.value = g.major; u.uFade.value = g.fade;
  u.uCenter.value.set(g.center[0], g.center[1]);
  grid.updateMatrixWorld(true);
  return g;
}

// ---- Scene colors per theme (design/DESIGN-SYSTEM.md, "selected (3D)") ----
// tokens: CSS token values read from the page: v0..v7 ('#rrggbb'), ink, ink2, line.
// Night: the selected element turns paper with an ink hairline outline.
// Paper: the inverse, an ink element with a paper hairline outline.
// Hover lifts the category value a little toward the selection value.
export function scenePalette(theme, tokens) {
  const isNight = theme === 'night';
  const steps = {};
  for (const k of [2, 3, 4, 5, 6]) steps[k] = tokens['v' + k];
  const selected = isNight ? tokens.v7 : tokens.v0;
  return {
    theme: isNight ? 'night' : 'paper',
    steps,
    selected,
    outline: isNight ? tokens.v0 : tokens.v7,
    hoverToward: selected,
    hoverLift: 0.22,
    edge: tokens.v0,
    edgeOpacity: isNight ? 0.7 : 0.5,
    grid: tokens.line,
    gizmo: tokens.ink2 || tokens.ink, // the axis gizmo is quiet: --ink-2
  };
}

// ---- Static SVG rendering, for surfaces without WebGL (the inline pane) ----
// Same camera math as the 3D page. Painter's algorithm, flat shading.
// Categories are told apart by VALUE, not hue (design/DESIGN-SYSTEM.md):
// each category gets one step of the gray scale --v2..--v6 (design/tokens.css).
// The 3D page reads the steps from CSS at runtime; VALUE_RGB is the same scale
// as literal RGB for surfaces without CSS (the inline pane SVG, node tests).
export const VALUE_RGB = {
  0: [10, 10, 10], 2: [58, 58, 56], 3: [107, 107, 104], 4: [154, 154, 150], 5: [201, 201, 196], 6: [231, 231, 227], 7: [244, 244, 241],
};
export const CATEGORY_STEP = {
  Floors: 2, Roofs: 3, Walls: 4, Columns: 5, 'Structural Columns': 5, Doors: 6, Windows: 6,
};
const EXTRA_STEPS = [3, 5, 2, 6, 4];

// Category -> value step (2..6), in first-seen order for unknown categories.
export function categoryValues(elements) {
  const out = {};
  let extra = 0;
  for (const el of elements) {
    const cat = el.category || 'Generic';
    if (!(cat in out)) out[cat] = CATEGORY_STEP[cat] || EXTRA_STEPS[extra++ % EXTRA_STEPS.length];
  }
  return out;
}

// Category -> [r, g, b] of its value step.
export function categoryColors(elements) {
  const steps = categoryValues(elements);
  const out = {};
  for (const cat of Object.keys(steps)) out[cat] = VALUE_RGB[steps[cat]].slice();
  return out;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
}

export function renderSvg(elements, state, opts = {}) {
  const width = opts.width || 640, height = opts.height || 400;
  const hidden = new Set(opts.hidden || []);
  const selected = opts.selected === undefined || opts.selected === null ? null : String(opts.selected);
  const maxTriangles = opts.maxTriangles || 1800;
  const { r, u, d } = cameraBasis(state.azimuth, state.elevation);
  const hh = halfHeight(state);
  const scale = height / 2 / hh;
  const isPersp = state.projection !== 'parallel';
  const light = [-0.35, -0.55, 0.76];
  const cats = categoryColors(elements);
  const tris = [];
  let total = 0;
  for (const el of elements) {
    const cat = el.category || 'Generic';
    if (hidden.has(cat)) continue;
    const v = el.mesh.vertices, ix = el.mesh.indices;
    const isSel = selected !== null && String(el.id) === selected;
    for (let t = 0; t + 2 < ix.length; t += 3) {
      total++;
      const pts = [], world = [];
      let depth = 0, behind = false;
      for (let k = 0; k < 3; k++) {
        const p = [v[ix[t + k] * 3], v[ix[t + k] * 3 + 1], v[ix[t + k] * 3 + 2]];
        world.push(p);
        const q = [p[0] - state.target[0], p[1] - state.target[1], p[2] - state.target[2]];
        const x = q[0] * r[0] + q[1] * r[1] + q[2] * r[2];
        const y = q[0] * u[0] + q[1] * u[1] + q[2] * u[2];
        const z = q[0] * d[0] + q[1] * d[1] + q[2] * d[2];
        if (isPersp && state.distance - z <= 1e-3) behind = true;
        const f = isPersp ? state.distance / Math.max(1e-6, state.distance - z) : 1;
        pts.push([width / 2 + x * f * scale, height / 2 - y * f * scale]);
        depth += z;
      }
      if (behind) continue;
      const n = cross(
        [world[1][0] - world[0][0], world[1][1] - world[0][1], world[1][2] - world[0][2]],
        [world[2][0] - world[0][0], world[2][1] - world[0][1], world[2][2] - world[0][2]]);
      const len = Math.hypot(n[0], n[1], n[2]) || 1;
      const shade = 0.55 + 0.45 * Math.abs((n[0] * light[0] + n[1] * light[1] + n[2] * light[2]) / len);
      // The selected element turns flat paper (--v7), so it reads at a glance.
      const rgb = isSel ? VALUE_RGB[7] : cats[cat].map(c => Math.round(Math.min(255, c * shade)));
      tris.push({ depth: depth / 3, pts, rgb, el, isSel });
    }
  }
  tris.sort((a, b) => a.depth - b.depth); // far first
  const drawn = tris.length > maxTriangles ? tris.slice(tris.length - maxTriangles) : tris;
  // Consecutive triangles of one element share a <g> with a <title> (hover text).
  let body = '';
  let open = null;
  for (const t of drawn) {
    if (t.el !== open) {
      if (open) body += '</g>';
      open = t.el;
      const title = [open.name || open.category, 'ID ' + open.id, open.category, open.type, open.level].filter(Boolean).join(' | ');
      body += '<g><title>' + esc(title) + '</title>';
    }
    const fill = 'rgb(' + t.rgb.join(',') + ')';
    body += '<path d="M' + t.pts.map(p => p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join('L') + 'Z" fill="' + fill + '" stroke="' + fill + '" stroke-width=".6"/>';
  }
  if (open) body += '</g>';
  // The one focal dot: an ink dot ringed in paper at the selected element's center.
  let dot = '';
  const selTris = drawn.filter(t => t.isSel);
  if (selTris.length) {
    let sx = 0, sy = 0, n = 0;
    for (const t of selTris) for (const p of t.pts) { sx += p[0]; sy += p[1]; n++; }
    dot = '<circle cx="' + (sx / n).toFixed(1) + '" cy="' + (sy / n).toFixed(1) + '" r="4.5" fill="rgb(' + VALUE_RGB[0].join(',') + ')" stroke="rgb(' + VALUE_RGB[7].join(',') + ')" stroke-width="1.5"/>';
  }
  // Axis gizmo, bottom-left, without hue: X solid, Y dashed, Z dotted, each labeled.
  const gx = 34, gy = height - 34;
  const ink = opts.ink || 'rgb(' + VALUE_RGB[7].join(',') + ')';
  let gizmo = '';
  for (const [k, lab, dash, w] of [[0, 'X', '', 2], [1, 'Y', '4 3', 1.6], [2, 'Z', '0.1 3.4', 2.2]]) {
    gizmo += '<line x1="' + gx + '" y1="' + gy + '" x2="' + (gx + r[k] * 22).toFixed(1) + '" y2="' + (gy - u[k] * 22).toFixed(1) + '" stroke="' + ink + '" stroke-width="' + w + '" stroke-linecap="round"' + (dash ? ' stroke-dasharray="' + dash + '"' : '') + '/>' +
      '<text x="' + (gx + r[k] * 30).toFixed(1) + '" y="' + (gy - u[k] * 30 + 4).toFixed(1) + '" fill="' + ink + '" font-size="11" font-family="IBM Plex Mono, ui-monospace, Consolas, monospace" font-weight="500" text-anchor="middle">' + lab + '</text>';
  }
  const svg = '<svg xmlns="http://www.w3.org/2000/svg" width="' + width + '" height="' + height + '" viewBox="0 0 ' + width + ' ' + height + '">' +
    '<rect width="100%" height="100%" fill="' + (opts.background || '#0a0a0a') + '"/>' + body + dot + gizmo + '</svg>';
  return { svg, drawn: drawn.length, total, isCut: drawn.length < tris.length };
}

// ---- First-person walk (Z up) ----
// yaw: plan angle of the look direction from +X toward +Y (radians); pitch: up/down, clamped.
// Moving uses only the XY plane of the look direction, so looking up never flies you up.
export const WALK = { eye: 5.5, speed: 4.5, run: 3, accel: 10, fov: 70, lookRadPerPx: 0.0025, pitchMax: 85 * DEG, stopAt: 1, followRate: 10, stepUp: 2 };

// Start at the view target dropped to the floor under it, looking along the view direction
// flattened to XY. floorZ(x, y, zFrom) returns the floor height under a point, or null.
export function enterWalk(state, floorZ) {
  const { d } = cameraBasis(state.azimuth, state.elevation);
  const flat = Math.hypot(d[0], d[1]);
  const yaw = flat > 1e-6 ? Math.atan2(-d[1], -d[0]) : state.azimuth + Math.PI;
  const [x, y, z0] = state.target;
  const f = floorZ ? floorZ(x, y, z0 + WALK.stepUp) : null; // the floor just below the target, not the roof
  return {
    saved: { ...state, target: state.target.slice() },
    pos: [x, y, f === null || f === undefined ? z0 + WALK.eye : f + WALK.eye],
    yaw, pitch: 0, vel: [0, 0], eyeOffset: 0, isCollision: true,
  };
}

// Mouse look: +dx turns right (clockwise in plan), +dy looks down.
export function walkLook(w, dx, dy) {
  w.yaw -= dx * WALK.lookRadPerPx;
  w.pitch = Math.max(-WALK.pitchMax, Math.min(WALK.pitchMax, w.pitch - dy * WALK.lookRadPerPx));
  return w;
}

// One fixed time step. keys: { forward, back, left, right, up, down, run } (booleans).
// env.floorZ(x, y, zFrom) -> floor height or null; env.hitDist(origin, dir2, max) -> distance to
// the nearest wall along the XY direction, or null.
export function walkStep(w, keys, dt, env = {}) {
  const fwd = [Math.cos(w.yaw), Math.sin(w.yaw)], right = [Math.sin(w.yaw), -Math.cos(w.yaw)];
  const f = (keys.forward ? 1 : 0) - (keys.back ? 1 : 0), s = (keys.right ? 1 : 0) - (keys.left ? 1 : 0);
  let want = [fwd[0] * f + right[0] * s, fwd[1] * f + right[1] * s];
  const n = Math.hypot(want[0], want[1]);
  const speed = WALK.speed * (keys.run ? WALK.run : 1);
  want = n > 0 ? [want[0] / n * speed, want[1] / n * speed] : [0, 0];
  const k = Math.min(1, WALK.accel * dt); // smooth start and stop
  w.vel = [w.vel[0] + (want[0] - w.vel[0]) * k, w.vel[1] + (want[1] - w.vel[1]) * k];
  let step = [w.vel[0] * dt, w.vel[1] * dt];
  const len = Math.hypot(step[0], step[1]);
  if (len > 1e-9 && w.isCollision && env.hitDist) {
    const dir = [step[0] / len, step[1] / len];
    const hit = env.hitDist(w.pos, dir, len + WALK.stopAt);
    if (hit !== null && hit !== undefined) {
      const allowed = Math.max(0, hit - WALK.stopAt);
      step = [dir[0] * Math.min(len, allowed), dir[1] * Math.min(len, allowed)];
      if (allowed < len) w.vel = [0, 0];
    }
  }
  w.pos[0] += step[0]; w.pos[1] += step[1];
  const feet = w.pos[2] - WALK.eye - w.eyeOffset; // where the feet are now (before this step's Q/E)
  w.eyeOffset += ((keys.up ? 1 : 0) - (keys.down ? 1 : 0)) * WALK.speed * dt;
  const floor = env.floorZ ? env.floorZ(w.pos[0], w.pos[1], feet + WALK.stepUp) : null;
  const targetZ = (floor === null || floor === undefined ? feet : floor) + WALK.eye + w.eyeOffset;
  w.pos[2] += (targetZ - w.pos[2]) * Math.min(1, WALK.followRate * dt); // stairs and floors, smoothed
  return w;
}

// The page camera for a walk: perspective, eye at pos, looking along yaw/pitch.
export function walkView(w, state) {
  const look = [Math.cos(w.pitch) * Math.cos(w.yaw), Math.cos(w.pitch) * Math.sin(w.yaw), Math.sin(w.pitch)];
  return {
    ...state, projection: 'perspective', fov: WALK.fov, distance: 1,
    target: [w.pos[0] + look[0], w.pos[1] + look[1], w.pos[2] + look[2]],
    azimuth: Math.atan2(-look[1], -look[0]), elevation: -w.pitch,
  };
}

// Where a left click picks in walk mode. With the pointer locked the pointer is hidden and frozen,
// so the click picks what is under the screen center (the crosshair). Otherwise it picks under the
// cursor. rect is the canvas bounding box (left, top, width, height) in page pixels.
export function walkPickPoint(isLocked, clientX, clientY, rect) {
  return isLocked ? { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 } : { x: clientX, y: clientY };
}

// ---- View cube (Revit conventions, Z up, Y = north) ----
// Each face's normal points from the model toward the camera that looks at that face:
// FRONT = camera south looking north; BACK = camera north looking south; RIGHT = camera east
// looking west; LEFT = camera west looking east; TOP looks down; BOTTOM looks up.
// a and b span the face (cell column and row directions, b is "up" on the face as drawn).
export const CUBE_FACES = {
  front: { n: [0, -1, 0], a: [1, 0, 0], b: [0, 0, 1] },
  back: { n: [0, 1, 0], a: [-1, 0, 0], b: [0, 0, 1] },
  right: { n: [1, 0, 0], a: [0, 1, 0], b: [0, 0, 1] },
  left: { n: [-1, 0, 0], a: [0, -1, 0], b: [0, 0, 1] },
  top: { n: [0, 0, 1], a: [1, 0, 0], b: [0, 1, 0] },
  bottom: { n: [0, 0, -1], a: [1, 0, 0], b: [0, -1, 0] },
};
const faceOfNormal = v => Object.keys(CUBE_FACES).find(k => CUBE_FACES[k].n.every((x, i) => x === v[i]));

// A cube region (1 face, 2 faces = an edge, 3 faces = a corner) -> the camera to go to.
// Faces give the parallel view of that face; edges and corners keep the current projection.
// Looking straight down or up, north stays at the top of the screen (BOTTOM: east is on the left).
export function cubeView(names) {
  const s = [0, 0, 0];
  for (const k of names) CUBE_FACES[k].n.forEach((v, i) => { s[i] += v; });
  const len = Math.hypot(s[0], s[1], s[2]);
  const d = s.map(v => v / len);
  let azimuth, elevation;
  if (Math.abs(d[2]) > 1 - 1e-9) { elevation = Math.sign(d[2]) * 90 * DEG; azimuth = d[2] > 0 ? -90 * DEG : 90 * DEG; }
  else { azimuth = Math.atan2(d[1], d[0]); elevation = Math.asin(d[2]); }
  return { azimuth, elevation, projection: names.length === 1 ? 'parallel' : null };
}

const CELL = [[-1, -0.56], [-0.56, 0.56], [0.56, 1]];
// The visible faces of the unit cube as seen by the camera, in screen units (x right, y up),
// each split into a 3x3 grid: the center cell is the face, edge cells the edges, corner cells the corners.
export function cubeLayout(state) {
  const { r, u, d } = cameraBasis(state.azimuth, state.elevation);
  const scr = p => [p[0] * r[0] + p[1] * r[1] + p[2] * r[2], p[0] * u[0] + p[1] * u[1] + p[2] * u[2]];
  const faces = [];
  for (const [name, f] of Object.entries(CUBE_FACES)) {
    const facing = f.n[0] * d[0] + f.n[1] * d[1] + f.n[2] * d[2];
    if (facing <= 1e-6) continue;
    const at = (x, y) => scr([0, 1, 2].map(i => f.n[i] + f.a[i] * x + f.b[i] * y));
    const cells = [];
    for (let i = 0; i < 3; i++) for (let j = 0; j < 3; j++) {
      const [x0, x1] = CELL[i], [y0, y1] = CELL[j];
      const names = [name];
      if (i !== 1) names.push(faceOfNormal(f.a.map(v => v * (i - 1))));
      if (j !== 1) names.push(faceOfNormal(f.b.map(v => v * (j - 1))));
      cells.push({ names, pts: [at(x0, y0), at(x1, y0), at(x1, y1), at(x0, y1)] });
    }
    faces.push({ name, facing, outline: [at(-1, -1), at(1, -1), at(1, 1), at(-1, 1)], center: at(0, 0), cells });
  }
  // The compass ring lies in the XY plane under the cube; N = +Y.
  const ring = [];
  for (let k = 0; k <= 48; k++) { const t = (k / 48) * 2 * Math.PI; ring.push(scr([Math.cos(t) * 1.5, Math.sin(t) * 1.5, -1])); }
  const compass = { N: scr([0, 1.95, -1]), E: scr([1.95, 0, -1]), S: scr([0, -1.95, -1]), W: scr([-1.95, 0, -1]) };
  return { faces, ring, compass };
}

// ---- Cursors: soft, rounded SVG shapes, value palette only ----
// Paper fill with an ink outline on BOTH themes: paper is the most legible
// mark on the night field, and the ink outline separates it on paper.
// Paper theme: a soft dark drop shadow. Night theme: a dark shadow cannot
// show on near-black, so a soft paper halo (a glow) takes its place.
// Shapes keep >= 3px padding inside the 32px canvas so the blur is not clipped.
export const CURSOR_INK = '#0a0a0a';
export const CURSOR_PAPER = '#f4f4f1';
const PLUMP_ARROW = 'M7.5 4.8 L7.5 21.6 Q7.5 24.2 9.6 22.6 L12.4 20.4 L14.9 25.6 Q15.7 27.2 17.3 26.5 L18.9 25.8 Q20.5 25.1 19.7 23.5 L17.3 18.4 L21.3 18.2 Q23.9 18.1 22.1 16.3 L9.9 4.8 Q7.5 2.9 7.5 4.8 Z';
export const CURSOR_SHAPES = {
  idle: { hot: [8, 5], fill: PLUMP_ARROW },
  hover: { hot: [8, 5], fill: PLUMP_ARROW, dot: [24.6, 24.2] },
  rotate: { hot: [16, 16], strokes: [['M22.6 12.2 A7.6 7.6 0 1 0 22.2 20.6', 6.2, 3], ['M18.6 12.4 L22.8 12.6 L23.2 8.4', 6.2, 3]] },
  pan: { hot: [16, 16], strokes: [['M12.4 9 L16 5.4 L19.6 9', 5.6, 2.6], ['M12.4 23 L16 26.6 L19.6 23', 5.6, 2.6], ['M9 12.4 L5.4 16 L9 19.6', 5.6, 2.6], ['M23 12.4 L26.6 16 L23 19.6', 5.6, 2.6]], center: 3.2 },
  zoom: { hot: [13, 13], strokes: [['M19.4 19.4 L25.4 25.4', 7.6, 4.2], ['M13 6 A7 7 0 1 0 13 20 A7 7 0 1 0 13 6 Z', 6, 3]], plus: [13, 13, 3] },
};

export function cursorSvg(name, theme = 'paper', opts = {}) {
  const s = CURSOR_SHAPES[name];
  if (!s) throw new Error('Unknown cursor ' + name);
  const isNight = theme === 'night';
  const shadow = isNight
    ? '<feDropShadow dx="0" dy="0" stdDeviation="1.1" flood-color="' + CURSOR_PAPER + '" flood-opacity="0.35"/>'
    : '<feDropShadow dx="0.6" dy="1.2" stdDeviation="1.1" flood-color="' + CURSOR_INK + '" flood-opacity="0.45"/>';
  const hasFilter = opts.filter !== false;
  let body = '';
  if (s.fill) body += '<path d="' + s.fill + '" fill="' + CURSOR_PAPER + '" stroke="' + CURSOR_INK + '" stroke-width="2.2" stroke-linejoin="round"/>';
  for (const [d, outer] of s.strokes || []) body += '<path d="' + d + '" fill="none" stroke="' + CURSOR_INK + '" stroke-width="' + outer + '" stroke-linecap="round" stroke-linejoin="round"/>';
  for (const [d, , inner] of s.strokes || []) body += '<path d="' + d + '" fill="none" stroke="' + CURSOR_PAPER + '" stroke-width="' + inner + '" stroke-linecap="round" stroke-linejoin="round"/>';
  if (s.center) body += '<circle cx="16" cy="16" r="' + s.center + '" fill="' + CURSOR_INK + '" stroke="' + CURSOR_PAPER + '" stroke-width="1.6"/>';
  if (s.plus) {
    const [x, y, h] = s.plus;
    body += '<path d="M' + (x - h) + ' ' + y + ' L' + (x + h) + ' ' + y + ' M' + x + ' ' + (y - h) + ' L' + x + ' ' + (y + h) + '" stroke="' + CURSOR_INK + '" stroke-width="1.8" stroke-linecap="round"/>';
  }
  if (s.dot) body += '<circle cx="' + s.dot[0] + '" cy="' + s.dot[1] + '" r="3.6" fill="' + CURSOR_INK + '" stroke="' + CURSOR_PAPER + '" stroke-width="1.6"/>';
  return '<svg xmlns="http://www.w3.org/2000/svg" width="32" height="32" viewBox="0 0 32 32">' +
    (hasFilter ? '<defs><filter id="s" x="-30%" y="-30%" width="160%" height="160%">' + shadow + '</filter></defs><g filter="url(#s)">' : '<g>') +
    body + '</g></svg>';
}

export function cursorCss(name, theme = 'paper') {
  const s = CURSOR_SHAPES[name];
  const fallback = { idle: 'default', hover: 'pointer', rotate: 'grabbing', pan: 'move', zoom: 'zoom-in' }[name];
  return 'url("data:image/svg+xml,' + encodeURIComponent(cursorSvg(name, theme)) + '") ' + s.hot[0] + ' ' + s.hot[1] + ', ' + fallback;
}

// ---- Gumball: manual move / rotate edits (viewer copy only) ----
// Handles: 'x', 'y', 'z' move along world X (east), Y (north), Z (up);
// 'rz' rotates about world Z through the element's center. Rotation is
// counterclockwise-positive in plan (seen from above), as in Revit.
export const GUMBALL = { moveSnap: 0.5, rotateSnap: 5 };
const AXIS_INDEX = { x: 0, y: 1, z: 2 };

export function snapTo(value, step) {
  return Math.round(value / step) * step + 0; // + 0 turns -0 into 0
}

// World units per screen pixel at the target plane.
export function worldPerPixel(state, viewportHeightPx) {
  return (halfHeight(state) * 2) / Math.max(1, viewportHeightPx);
}

// A gumball drag from screen point a to b (page pixels, y down).
// view: { height } of the viewport in px, and for 'rz' the screen point of the
// rotation center as view.pivot. Returns { move: [dx, dy, dz] } in ft, or
// { rotateZ } in degrees. isFree (Alt held) turns snapping off.
export function gumballDrag(handle, state, a, b, view, isFree = false) {
  const basis = cameraBasis(state.azimuth, state.elevation);
  if (handle in AXIS_INDEX) {
    const k = AXIS_INDEX[handle];
    const s = [basis.r[k], basis.u[k]]; // screen direction of the world axis, y up
    const len2 = s[0] * s[0] + s[1] * s[1];
    const move = [0, 0, 0];
    if (len2 > 0.04) { // an axis that points at the viewer cannot be dragged along
      const d = [b.x - a.x, -(b.y - a.y)];
      const amount = ((d[0] * s[0] + d[1] * s[1]) / len2) * worldPerPixel(state, view.height);
      move[k] = isFree ? amount : snapTo(amount, GUMBALL.moveSnap);
    }
    return { move };
  }
  if (handle === 'rz') {
    let deg;
    const se = Math.sin(state.elevation);
    if (Math.abs(se) < 0.2) {
      deg = (b.x - a.x) * 0.5; // ring seen edge-on: a horizontal drag turns it
    } else {
      const p = view.pivot;
      const a0 = Math.atan2(-(a.y - p.y), a.x - p.x), a1 = Math.atan2(-(b.y - p.y), b.x - p.x);
      let t = a1 - a0;
      while (t > Math.PI) t -= 2 * Math.PI;
      while (t < -Math.PI) t += 2 * Math.PI;
      deg = ((t * 180) / Math.PI) * Math.sign(se); // seen from below, screen CCW is plan CW
    }
    return { rotateZ: isFree ? deg : snapTo(deg, GUMBALL.rotateSnap) };
  }
  throw new Error('Unknown gumball handle ' + handle);
}

// Pending edits: one record per element, in the order first edited.
// Final position = rotate the original about pivot (its original center) by
// rotateZ, then translate by move. A later rotation turns about the current
// center (pivot + move), and this form composes that exactly.
export function createEdits() {
  return { order: [], byId: {} };
}

export function editRecord(edits, el) {
  const key = String(el.id);
  if (!edits.byId[key]) {
    const b = elementBounds(el);
    edits.byId[key] = { id: el.id, el, move: [0, 0, 0], rotateZ: 0, pivot: [0, 1, 2].map(i => (b.min[i] + b.max[i]) / 2) };
    edits.order.push(key);
  }
  return edits.byId[key];
}

export function applyDrag(record, result) {
  if (result.move) record.move = record.move.map((v, i) => v + result.move[i]);
  if (result.rotateZ) record.rotateZ += result.rotateZ;
  return record;
}

// One drag on one element. update() previews the drag result on top of the
// record as it was at the start; cancel() (Esc) puts it back; commit() keeps
// it (and drops a record the drag left without any change).
export function beginGumballDrag(edits, el) {
  const isNew = !edits.byId[String(el.id)];
  const record = editRecord(edits, el);
  const base = { move: record.move.slice(), rotateZ: record.rotateZ };
  const reset = () => { record.move = base.move.slice(); record.rotateZ = base.rotateZ; };
  return {
    record,
    update(result) { reset(); return applyDrag(record, result); },
    cancel() { if (isNew) undoEdit(edits, el.id); else reset(); },
    commit() { if (isNew && isIdentity(record)) undoEdit(edits, el.id); return record; },
  };
}

export function isIdentity(record) {
  return record.move.every(v => Math.abs(v) < 1e-9) && Math.abs(record.rotateZ % 360) < 1e-9;
}

export function undoEdit(edits, id) {
  const key = String(id);
  delete edits.byId[key];
  edits.order = edits.order.filter(k => k !== key);
}

export function clearEdits(edits) {
  edits.order = [];
  edits.byId = {};
}

export function pendingEdits(edits) {
  return edits.order.map(k => edits.byId[k]).filter(r => !isIdentity(r));
}

// Column-major 4x4 (three.js Matrix4.fromArray order) for a record.
export function editMatrix(record) {
  const t = (record.rotateZ * Math.PI) / 180, c = Math.cos(t), s = Math.sin(t);
  const [px, py] = record.pivot, [mx, my, mz] = record.move;
  const tx = px + mx - (c * px - s * py), ty = py + my - (s * px + c * py);
  return [c, s, 0, 0, -s, c, 0, 0, 0, 0, 1, 0, tx, ty, mz, 1];
}

export function applyEditToPoint(record, p) {
  const m = editMatrix(record);
  return [m[0] * p[0] + m[4] * p[1] + m[8] * p[2] + m[12], m[1] * p[0] + m[5] * p[1] + m[9] * p[2] + m[13], m[2] * p[0] + m[6] * p[1] + m[10] * p[2] + m[14]];
}

const roundTo = (v, n) => { const f = Math.pow(10, n); return Math.round(v * f) / f + 0; };

// The hand-off contract for Revit. It is applied only through am-safe-change.
export function exportTransforms(edits) {
  return {
    kind: 'element-transforms',
    source: 'am-bim-viewer',
    units: 'ft',
    items: pendingEdits(edits).map(r => ({
      id: r.id,
      name: r.el.name || ((r.el.category || 'Generic') + ' ' + r.id),
      category: r.el.category || 'Generic',
      move: r.move.map(v => roundTo(v, 4)),
      rotateZ: roundTo(r.rotateZ, 3),
      pivot: r.pivot.map(v => roundTo(v, 4)),
    })),
  };
}

// ---- Typed gumball input: architect units ----
export const LENGTH_HINT = 'Type a length, for example 2.5, 2\'6", 2\'-6 1/2", 30", 750mm or 0.75m. To calculate, put spaces around + - * /, for example 10\' - 3" or 12\'6" / 2.';
export const ANGLE_HINT = 'Type an angle in degrees, for example 15, -90 or 22.5. To calculate, put spaces around + - * /, for example 90 - 15.';
const METRIC_PER_FT = { mm: 304.8, cm: 30.48, m: 0.3048 };

// "2.5" (decimal feet), "2'6\"", "2' 6\"", "2'-6 1/2\"", "2'6", "30\"", "6 1/2\"", "750mm", "0.75m",
// with an optional leading sign. Returns { ft } or { error } (plain words).
function parseLengthTerm(text) {
  const s = String(text === undefined || text === null ? '' : text).trim().replace(/[′’]/g, "'").replace(/[″”]/g, '"');
  let m = /^([+-]?)\s*(\d+(?:\.\d*)?|\.\d+)\s*(mm|cm|m)$/i.exec(s);
  if (m) { const v = Number(m[2]) / METRIC_PER_FT[m[3].toLowerCase()]; return { ft: m[1] === '-' ? -v : v }; }
  m = /^([+-]?)\s*(\d+(?:\.\d*)?|\.\d+)$/.exec(s);
  if (m) { const v = Number(m[2]); return { ft: m[1] === '-' ? -v : v }; }
  if (!/['"]/.test(s)) return { error: LENGTH_HINT };
  m = /^([+-]?)\s*(?:(\d+(?:\.\d+)?)\s*')?\s*-?\s*(\d+(?:\.\d+)?)?(?:\s*-?\s*(\d+)\s*\/\s*(\d+))?\s*("?)$/.exec(s);
  if (!m || (m[2] === undefined && m[3] === undefined && m[4] === undefined)) return { error: LENGTH_HINT };
  if (m[2] === undefined && m[6] !== '"') return { error: LENGTH_HINT }; // inches need the " mark
  if (m[5] !== undefined && Number(m[5]) === 0) return { error: 'The fraction cannot divide by zero. ' + LENGTH_HINT };
  const inches = Number(m[3] || 0) + (m[4] !== undefined ? Number(m[4]) / Number(m[5]) : 0);
  const v = Number(m[2] || 0) + inches / 12;
  return { ft: m[1] === '-' ? -v : v };
}

function parseAngleTerm(text) {
  const m =/^([+-]?\s*(?:\d+(?:\.\d*)?|\.\d+))\s*(?:°|deg|degrees?)?$/i.exec(String(text === undefined || text === null ? '' : text).trim());
  return m ? { deg: Number(m[1].replace(/\s+/g, '')) } : { error: ANGLE_HINT };
}

// Arithmetic on typed values. Operators need spaces around them (10' - 3", 12'6" / 2), because
// without spaces 10'-3/2" is ordinary feet-inches notation (10 ft 1 1/2 in). * and / take a plain
// number on one side; + and - add lengths (a bare number is feet, as in single entry).
const OP_SPLIT = /\s+([+\-*/])\s+/;
function evalTyped(text, parseTerm, unitKey, hint) {
  const s = String(text === undefined || text === null ? '' : text).trim();
  const parts = s.split(OP_SPLIT);
  if (parts.length === 1) return parseTerm(s);
  const terms = [], ops = [];
  for (let i = 0; i < parts.length; i++) {
    if (i % 2) { ops.push(parts[i]); continue; }
    const isScalar = /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)$/.test(parts[i].trim());
    const r = parseTerm(parts[i]);
    if (r.error) return { error: 'Cannot read "' + parts[i] + '". ' + hint };
    terms.push({ v: r[unitKey], scalar: isScalar });
  }
  // * and / first, left to right; a product or quotient keeps the unit of its length side.
  const tv = [terms[0]], addOps = [];
  for (let i = 0; i < ops.length; i++) {
    const op = ops[i], b = terms[i + 1];
    if (op === '*' || op === '/') {
      const a = tv[tv.length - 1];
      if (op === '/' && !b.scalar) return { error: 'Divide by a plain number only. ' + hint };
      if (op === '*' && !a.scalar && !b.scalar) return { error: 'Multiply by a plain number only. ' + hint };
      if (op === '/' && b.v === 0) return { error: 'Cannot divide by zero. ' + hint };
      tv[tv.length - 1] = { v: op === '*' ? a.v * b.v : a.v / b.v, scalar: a.scalar && b.scalar };
    } else { addOps.push(op); tv.push(b); }
  }
  let v = tv[0].v;
  for (let i = 0; i < addOps.length; i++) v = addOps[i] === '+' ? v + tv[i + 1].v : v - tv[i + 1].v;
  if (!isFinite(v)) return { error: hint };
  const out = {}; out[unitKey] = v; return out;
}
export function parseLength(text) { return evalTyped(text, parseLengthTerm, 'ft', LENGTH_HINT); }
export function parseAngle(text) { return evalTyped(text, parseAngleTerm, 'deg', ANGLE_HINT); }

// Typing during a gumball drag: the key that starts typed entry, and the text it starts with.
export function dragTypingStart(key) { return /^[0-9.+\-'"]$/.test(String(key)) ? String(key) : null; }

// 2.541666 -> 2'-6 1/2" (inches rounded to 1/16").
export function formatFeetInches(ft) {
  const sign = ft < 0 ? '-' : '';
  let sixteenths = Math.round(Math.abs(ft) * 12 * 16);
  const feet = Math.floor(sixteenths / (12 * 16));
  sixteenths -= feet * 12 * 16;
  const whole = Math.floor(sixteenths / 16);
  let num = sixteenths - whole * 16, den = 16;
  while (num && num % 2 === 0) { num /= 2; den /= 2; }
  return sign + feet + "'-" + whole + (num ? ' ' + num + '/' + den : '') + '"';
}

// Live readout for typed entry: "move x +2'-6\" (2.500 ft)", "rotate 15 deg".
export function typedReadout(handle, value) {
  if (handle === 'rz') return 'rotate ' + value + ' deg';
  return 'move ' + handle + ' ' + (value < 0 ? '' : '+') + formatFeetInches(value) + ' (' + value.toFixed(3) + ' ft)';
}

// Typed values apply exactly (no snap) and add to the pending change, like a drag.
export function applyTyped(edits, el, handle, value) {
  const d = beginGumballDrag(edits, el);
  if (handle === 'rz') d.update({ rotateZ: value });
  else { const move = [0, 0, 0]; move[AXIS_INDEX[handle]] = value; d.update({ move }); }
  return d.commit();
}

// Side-panel fields set the pending change to absolute values (move in ft, rotateZ in deg).
export function setEditValues(edits, el, values) {
  const rec = editRecord(edits, el);
  if (values.move) rec.move = values.move.slice();
  if (values.rotateZ !== undefined) rec.rotateZ = values.rotateZ;
  if (isIdentity(rec)) undoEdit(edits, el.id);
  return rec;
}

function signed(v, digits) { return (v < 0 ? '-' : '+') + Math.abs(v).toFixed(digits); }

// Live readout next to the cursor during a drag: "move x +2.50 ft", "rotate 15 deg".
export function gumballReadout(handle, result) {
  if (handle === 'rz') return 'rotate ' + (Number.isInteger(result.rotateZ) ? String(result.rotateZ) : result.rotateZ.toFixed(1)) + ' deg';
  return 'move ' + handle + ' ' + signed(result.move[AXIS_INDEX[handle]], 2) + ' ft';
}
