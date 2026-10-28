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
// An open hand for petting the cat: four fingers, a thumb and a palm, one outline. hand2 and handb are the same hand
// turned a little each way: the page swaps them while you stroke the cat, so the hand seems to move.
const HAND = 'M9 19 Q5.6 15.5 7.6 12.6 Q9.6 11.2 11.4 14.2 L11.4 8.2 Q11.4 5.6 13.4 5.6 Q15.4 5.6 15.4 8.2 L15.4 11.6 L15.4 6.2 Q15.4 3.6 17.4 3.6 Q19.4 3.6 19.4 6.2 L19.4 11.6 L19.4 7.4 Q19.4 4.9 21.3 4.9 Q23.2 4.9 23.2 7.4 L23.2 12.2 L23.2 10.4 Q23.2 8.4 24.9 8.4 Q26.6 8.4 26.6 10.4 L26.6 19.4 Q26.6 27.4 18.6 27.6 L15.6 27.6 Q10.4 27.4 9 19 Z';
export const CURSOR_SHAPES = {
  idle: { hot: [8, 5], fill: PLUMP_ARROW },
  hover: { hot: [8, 5], fill: PLUMP_ARROW, dot: [24.6, 24.2] },
  rotate: { hot: [16, 16], strokes: [['M22.6 12.2 A7.6 7.6 0 1 0 22.2 20.6', 6.2, 3], ['M18.6 12.4 L22.8 12.6 L23.2 8.4', 6.2, 3]] },
  pan: { hot: [16, 16], strokes: [['M12.4 9 L16 5.4 L19.6 9', 5.6, 2.6], ['M12.4 23 L16 26.6 L19.6 23', 5.6, 2.6], ['M9 12.4 L5.4 16 L9 19.6', 5.6, 2.6], ['M23 12.4 L26.6 16 L23 19.6', 5.6, 2.6]], center: 3.2 },
  zoom: { hot: [13, 13], strokes: [['M19.4 19.4 L25.4 25.4', 7.6, 4.2], ['M13 6 A7 7 0 1 0 13 20 A7 7 0 1 0 13 6 Z', 6, 3]], plus: [13, 13, 3] },
  hand: { hot: [17, 12], fill: HAND },
  hand2: { hot: [17, 12], fill: HAND, turn: 14 },
  handb: { hot: [17, 12], fill: HAND, turn: -14 },
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
  if (s.fill) body += '<path d="' + s.fill + '"' + (s.turn ? ' transform="rotate(' + s.turn + ' 16 20)"' : '') + ' fill="' + CURSOR_PAPER + '" stroke="' + CURSOR_INK + '" stroke-width="2.2" stroke-linejoin="round"/>';
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
  const fallback = { idle: 'default', hover: 'pointer', rotate: 'grabbing', pan: 'move', zoom: 'zoom-in', hand: 'grab', hand2: 'grabbing', handb: 'grabbing' }[name];
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

// ---- Pet mode: a small cartoon cat that walks, runs and jumps to a clicked surface (Z up) ----
// The pet is a visual toy and a rough sense of scale. It is not part of the model: it is never
// picked, listed or exported. All the behavior is here and takes callbacks for the scene, so node
// tests run it without three.js. Lengths are feet; the figure is about 1 ft tall at scale 1.
//   walk   a four-beat lateral-sequence gait (the cat walk), turning in arcs, never spinning
//   run    for a far target: a bounding gallop at a higher speed
//   jump   up a ledge, down a drop, over a small gap or a low obstacle (when the landing is in reach)
//   idle   stand, then sit, then lick a paw, groom, yawn; after a long rest it curls up and sleeps
export const PET = {
  height: 1, speed: 2, run: 5, runDist: 6, accel: 8, turnRate: 2.5, turnGain: 8, arrive: 0.04,
  stride: 0.27, runStride: 0.6, stepUp: 0.8, stepDown: 1.5, jumpUp: 2.5, jumpDown: 8, wallProbe: 0.5,
  sitAfter: 1.5, sleepAfter: 14, hopChance: 0.35, // idle timers; the chance of a playful hop at each hop check
  stuckDist: 2, meows: 3, meowEvery: 5,           // stuck: ending more than stuckDist (x scale) from the goal; calls in all; seconds between calls
  cell: 0.6, planNodes: 40, planMax: 1500, waypoint: 1, side: 0.5, replans: 3, // routing: cell size (x scale), cells searched per step, search limit, arrival radius at a route point (x cell), room kept from walls (x scale), new searches when blocked
  minUpZ: Math.sin(60 * DEG), // a surface counts as standable when its normal is 60+ degrees above horizontal
  maxScale: 6, scaleDiv: 50,
};
export const PET_ACTS = { lick: 3.6, groom: 4.2, yawn: 2.6, meow: 1.5, purr: 4 }; // idle actions and how long each lasts (s)
export const PET_IDLE = ['lick', 'groom', 'yawn']; // the actions it picks at random when it has nothing to do (meow is not one: it is a call for help)

// What the cat thinks, shown in a thought cloud. The kind follows what it is doing. Short, and a little silly.
export const PET_THOUGHTS = {
  idle: ['nap, then a second nap', 'is that a sunbeam?', 'this needs a cat door', 'a mouse in the ceiling?', 'fish. just fish.', 'load-bearing? asking for a friend',
    'the 2nd floor is mine', 'who moved my slab?', 'zoning allows naps', 'a tiny ladder would be nice'],
  run: ['zoomies!', 'gotta go fast', '3 a.m. energy'],
  jump: ['parkour!', 'nine lives, one jump', 'stairs are for humans'],
  arrive: ['nice spot', 'mine now', 'good view from here', 'fire rating: nine lives'],
  sleep: ['zzz...', 'dreaming of tuna', 'chasing laser dots'],
  lick: ['clean paws, clean plans', 'ready for the review'],
  groom: ['gotta look good for the review', 'fur is a finish schedule'],
  yawn: ['long day of site visits', 'this meeting could be a nap'],
  purr: ['more please', 'best client ever', 'keep going'],
  stuck: ['who put a wall here?', 'I need a cat door', 'meow? (rude)'],
};
// Show a thought of this kind (p.thought = { text, t, dur }; p.thoughtId counts them, so the page can tell a new one).
export function petThink(p, kind, rand = Math.random) {
  const list = PET_THOUGHTS[kind];
  if (!list) return p;
  p.thoughtId = (p.thoughtId || 0) + 1;
  p.thought = { text: list[Math.min(list.length - 1, Math.floor(rand() * list.length))], kind, t: 0, dur: 3.6 };
  return p;
}

// A click hit (a three.js intersection) -> world point, outward normal and whether the pet can stand.
// rayDir is the pick ray direction; a face seen from behind is flipped toward the camera.
export function petSurface(hit, rayDir) {
  if (!hit || !hit.face) return null;
  const n = hit.face.normal.clone().transformDirection(hit.object.matrixWorld);
  if (rayDir && n.x * rayDir.x + n.y * rayDir.y + n.z * rayDir.z > 0) n.negate();
  return { point: [hit.point.x, hit.point.y, hit.point.z], normal: [n.x, n.y, n.z], isStandable: n.z >= PET.minUpZ };
}

// Grow the pet with the model so it stays visible in a large building (1 for a small model, up to maxScale).
export function petScale(bounds) {
  const ext = Math.max(bounds.max[0] - bounds.min[0], bounds.max[1] - bounds.min[1], bounds.max[2] - bounds.min[2], 0);
  return Math.min(PET.maxScale, Math.max(1, ext / PET.scaleDiv));
}

// ---- Poses ----
// A pose is a flat object of numbers (radians, feet before scale). Leg angles: hip > 0 swings the foot
// back, knee > 0 bends the lower leg back. pitch turns the body about the hind paws (< 0 = nose up).
const POSE_KEYS = ['flHip', 'flKnee', 'frHip', 'frKnee', 'hlHip', 'hlKnee', 'hrHip', 'hrKnee', 'bob', 'roll', 'pitch', 'drop',
  'tail', 'tailPitch', 'ear', 'earBack', 'headPitch', 'headYaw', 'headTilt', 'eyes', 'mouth', 'tongue', 'breath'];
const restPose = () => { const o = {}; for (const k of POSE_KEYS) o[k] = 0; o.eyes = 1; return o; };
const withPose = over => Object.assign(restPose(), over);
const lerpPose = (a, b, k) => { const o = {}; for (const key of POSE_KEYS) o[key] = a[key] + (b[key] - a[key]) * k; return o; };

// Gait: a four-beat "lateral sequence" walk, the walk of a cat: hind left, fore left, hind right, fore
// right, a quarter cycle apart, so one foot swings while three stand. In the stance part of the cycle
// (75%) the foot goes back along the ground; in the swing part (25%) the knee bends to lift the foot.
// Each offset is the place of a leg in the cycle at phase 0. A foot lands when phase + offset reaches a whole
// cycle, so over one cycle the feet land in the order hind left, fore left, hind right, fore right.
export const GAIT = { hl: 0, fl: Math.PI * 1.5, hr: Math.PI, fr: Math.PI / 2, stance: 0.75, hip: 0.8 };
export function legCycle(ph) {
  const u = (((ph / (2 * Math.PI)) % 1) + 1) % 1, A = GAIT.hip;
  if (u < GAIT.stance) { const k = u / GAIT.stance; return { hip: -A + 2 * A * k, knee: 0.12 * Math.sin(Math.PI * k) }; }
  const k = (u - GAIT.stance) / (1 - GAIT.stance), e = k * k * (3 - 2 * k); // smoothstep
  return { hip: A - 2 * A * e, knee: 0.95 * Math.sin(Math.PI * k) };
}
// Walk: the body rises and dips twice per cycle and shifts weight side to side; the head nods and
// turns a little, the ears flick, and the tail follows the body a little later (overlapping action).
function walkPose(ph) {
  const l = {}; for (const k of ['hl', 'fl', 'hr', 'fr']) l[k] = legCycle(ph + GAIT[k]);
  return withPose({
    flHip: l.fl.hip, flKnee: l.fl.knee, frHip: l.fr.hip, frKnee: l.fr.knee, hlHip: l.hl.hip, hlKnee: l.hl.knee, hrHip: l.hr.hip, hrKnee: l.hr.knee,
    bob: 0.012 * (1 + Math.sin(2 * ph)), roll: 0.035 * Math.sin(ph), tail: 0.28 * Math.sin(ph - 1), ear: 0.06 * Math.sin(2 * ph),
    headPitch: 0.05 * Math.sin(2 * ph + 1.2), headYaw: 0.07 * Math.sin(ph + 0.6),
  });
}
// Run: a bound. The front legs reach together, the hind legs follow, the spine flexes, the tail streams back.
function runPose(ph) {
  const h = ph - 2.4, hip = a => 0.9 * Math.sin(a), knee = a => 0.8 * Math.max(0, Math.cos(a));
  return withPose({
    flHip: hip(ph), frHip: hip(ph), flKnee: knee(ph), frKnee: knee(ph), hlHip: hip(h), hrHip: hip(h), hlKnee: knee(h), hrKnee: knee(h),
    bob: 0.04 * (1 + Math.sin(ph + 0.3)), pitch: 0.12 * Math.sin(ph + 0.5), tail: 0.1 * Math.sin(ph), tailPitch: -1, earBack: 0.7, headPitch: -0.05,
  });
}
// Jump, t from 0 to 1: crouch, push off, reach forward in the air (nose up, then down), land.
function jumpPose(t) {
  const crouch = Math.max(0, 1 - t / 0.2), air = Math.min(1, Math.max(0, (t - 0.1) / 0.3)) * (1 - Math.max(0, (t - 0.8) / 0.2)), land = Math.max(0, (t - 0.8) / 0.2);
  return withPose({
    drop: -0.1 * crouch - 0.05 * land, pitch: -0.4 + 0.8 * t,
    flHip: -0.9 * air, frHip: -0.9 * air, flKnee: 0.5 * air, frKnee: 0.5 * air, hlHip: 1.1 * air, hrHip: 1.1 * air, hlKnee: 0.4 * air, hrKnee: 0.4 * air,
    tailPitch: -0.6, earBack: 0.5, headPitch: 0.3,
  });
}
// Idle poses. Sitting turns the body nose-up about the hind paws; the head and front legs cancel the tilt.
const SIT = { pitch: -0.75, flHip: 0.75, frHip: 0.75, hlHip: -1, hrHip: -1, hlKnee: 1.7, hrKnee: 1.7, headPitch: 0.45, tailPitch: -0.5 };
const IDLE_POSES = {
  stand: withPose({}),
  sit: withPose(SIT),
  lick: withPose({ ...SIT, flHip: -1.2, flKnee: 1.7, headPitch: 0.75, headYaw: 0.3, eyes: 0.5 }),   // a front paw up to the mouth
  groom: withPose({ ...SIT, headPitch: 0.8, headYaw: 0.95, roll: 0.12, eyes: 0.4 }),                  // head turned to lick a flank
  yawn: withPose({ ...SIT, headPitch: -0.05, eyes: 0.1, earBack: 0.6 }),
  meow: withPose({ ...SIT, headPitch: 0.15, tailPitch: -0.1 }),                                        // sits up, ears forward, calls for help
  purr: withPose({ ...SIT, headPitch: 0.15, headTilt: 0.18, eyes: 0.05, earBack: 0.25, roll: 0.06, tailPitch: -0.2 }), // eyes shut, head pushed up into the hand
  sleep: withPose({ drop: -0.17, flHip: -1.2, frHip: -1.2, hlHip: -1.2, hrHip: -1.2, flKnee: 2.2, frKnee: 2.2, hlKnee: 2.2, hrKnee: 2.2,
    headPitch: 0.9, headYaw: 0.6, eyes: 0, earBack: 0.3, tail: 0.9, tailPitch: -1.3 }),                                // curled up, legs tucked, nose on the paws
};
// Small repeating motions added on top of the idle pose (they must not be smoothed, or they blur away).
function idleOsc(p) {
  const t = p.actT || 0, w = f => Math.sin(2 * Math.PI * f * t);
  if (p.act === 'lick') return { headPitch: 0.08 * w(2.4), flKnee: 0.18 * w(2.4), tongue: Math.max(0, w(2.4)) };
  if (p.act === 'groom') return { headPitch: 0.1 * w(3), headYaw: 0.05 * w(1.5), tongue: Math.max(0, w(3)) };
  if (p.act === 'yawn') { const m = Math.min(1, 1.6 * Math.sin(Math.PI * Math.min(1, t / PET_ACTS.yawn))); return { mouth: m, headPitch: -0.3 * m }; }
  if (p.act === 'sleep') return { breath: w(1 / 2.6) };
  if (p.act === 'purr') return { breath: 0.7 * w(11), tail: 0.25 * w(0.6), headTilt: 0.04 * w(1.2) }; // a purr: the body trembles a little
  if (p.act === 'meow') { const m = Math.sin(Math.PI * Math.min(1, (t % 0.75) / 0.7)); return { mouth: Math.max(0, m), headPitch: -0.25 * Math.max(0, m), tail: 0.3 * w(1.2) }; } // two calls, the head tips up with each
  return { tail: 0.18 * Math.sin(1.6 * t), ear: 0.04 * Math.sin(0.9 * t + 1) }; // stand or sit: a slow tail sway
}

// The pose now: the idle pose (smoothed in p.ib) with its small motions, blended toward the walk, run
// or jump pose by p.w (0..1). Bare test objects without p.ib / p.w work too.
export function petPose(p) {
  const base = p.ib || IDLE_POSES[p.act || 'stand'], osc = idleOsc(p), idle = Object.assign({}, base);
  for (const k of Object.keys(osc)) idle[k] += osc[k];
  const w = p.w === undefined ? (p.moving ? 1 : 0) : p.w;
  if (w <= 0) return idle;
  const cyc = p.jump ? jumpPose(p.jump.t) : p.run ? runPose(p.phase || 0) : walkPose(p.phase || 0);
  return w >= 1 ? cyc : lerpPose(idle, cyc, w);
}

// How often the page should redraw for this pet: full speed while it moves, a little slower for a lick,
// groom or yawn, and rarely while it sits or sleeps (it only needs to advance its timers).
export function petFps(p) { return p.moving || p.jump || p.planning ? 60 : PET_ACTS[p.act] !== undefined ? 30 : 8; }

// ---- State ----
export function createPet(point, scale = 1) {
  return { pos: point.slice(), heading: 0, target: null, goal: null, path: [], planning: null, route: null, routeId: 0,
    phase: 0, moving: false, scale, speed: 0, run: false, playful: false, hopIn: 4, replans: 0, jump: null, act: 'stand', actT: 0, idleT: 0, w: 0, ib: IDLE_POSES.stand };
}

// ---- Routing ----
// Can the pet walk from (x, y, z) to (nx, ny)? Returns the ground height there, or null. The same checks the
// walk uses: ground there, a step it can walk, and no wall ahead (with the wall probe as clearance).
function walkEdge(env, s, x, y, z, nx, ny) {
  const dx = nx - x, dy = ny - y, d = Math.hypot(dx, dy);
  if (d < 1e-9) return z;
  const gz = env.ground(nx, ny, z + PET.stepUp * s);
  if (gz === null || gz === undefined || gz - z > PET.stepUp * s || z - gz > PET.stepDown * s) return null;
  if (env.blocked && env.blocked(x, y, z, dx / d, dy / d, d + PET.wallProbe * s)) return null;
  return gz;
}
// Room to stand: no wall within PET.side * scale ft in the four directions. A route keeps this distance from walls,
// so a turning pet does not clip a corner.
function roomAround(env, s, x, y, z) {
  if (!env.blocked) return true;
  const r = PET.side * s;
  return !(env.blocked(x, y, z, 1, 0, r) || env.blocked(x, y, z, -1, 0, r) || env.blocked(x, y, z, 0, 1, r) || env.blocked(x, y, z, 0, -1, r));
}
// Is the straight line from a to b walkable all the way, with room around? (Checked in steps of one routing cell;
// the last point, where the pet stops, needs no room.)
function lineClear(env, s, a, b) {
  const c = PET.cell * s, d = Math.hypot(b[0] - a[0], b[1] - a[1]), n = Math.max(1, Math.ceil(d / c));
  let x = a[0], y = a[1], z = a[2];
  for (let i = 1; i <= n; i++) {
    const nx = a[0] + (b[0] - a[0]) * i / n, ny = a[1] + (b[1] - a[1]) * i / n, gz = walkEdge(env, s, x, y, z, nx, ny);
    if (gz === null || (i < n && !roomAround(env, s, nx, ny, gz))) return false;
    x = nx; y = ny; z = gz;
  }
  return true;
}

// A route search (A*) on a grid of cells (PET.cell * scale ft, 8 neighbors) around the start. A step to a neighbor
// must be walkable (see walkEdge); if it is not, a jump to a landing 1 to 3 cells ahead is allowed at 3 times the
// cost, so the pet goes around when it can and jumps when it must. step(n) expands up to n cells and returns
// { status: 'searching' } or { status: 'found' | 'partial' | 'failed', path }. 'partial' is a route to the reachable
// cell nearest the goal. The path is a list of [x, y, z] points, string-pulled (points that a straight walk can skip
// are dropped), and for 'found' it ends at the goal.
export function createPlanner(start, goal, scale, env) {
  const s = scale || 1, c = PET.cell * s, gx = goal[0], gy = goal[1];
  const h = (x, y) => Math.hypot(gx - x, gy - y);
  const nodes = new Map(), open = [], key = (ix, iy) => ix + ',' + iy, roomCache = new Map();
  const roomAt = (ix, iy, x, y, z) => { const k = key(ix, iy); let r = roomCache.get(k); if (r === undefined) { r = roomAround(env, s, x, y, z); roomCache.set(k, r); } return r; };
  const first = { ix: 0, iy: 0, x: start[0], y: start[1], z: start[2], g: 0, f: h(start[0], start[1]), parent: null, closed: false };
  nodes.set(key(0, 0), first); open.push(first);
  const R = Math.ceil(1.6 * h(start[0], start[1]) / c) + 10, DIRS = [[1, 0], [-1, 0], [0, 1], [0, -1], [1, 1], [1, -1], [-1, 1], [-1, -1]];
  let expanded = 0, best = first, done = null;
  const smooth = pts => {
    const out = []; let a = start.slice(), i = 0;
    while (i < pts.length) {
      let j = pts.length - 1;
      while (j > i && !lineClear(env, s, a, pts[j])) j--;
      out.push(pts[j]); a = pts[j]; i = j + 1;
    }
    return out;
  };
  const pathTo = (n, tail) => {
    const pts = []; for (let q = n; q && q.parent; q = q.parent) pts.push([q.x, q.y, q.z]);
    pts.reverse(); if (tail) pts.push(tail);
    return smooth(pts);
  };
  const expand = () => {
    let bi = 0; for (let k = 1; k < open.length; k++) if (open[k].f < open[bi].f) bi = k;
    const n = open.splice(bi, 1)[0];
    if (n.closed) return;
    n.closed = true; expanded++;
    if (h(n.x, n.y) < h(best.x, best.y)) best = n;
    if (h(n.x, n.y) <= 0.8 * c || (h(n.x, n.y) <= 1.6 * c && lineClear(env, s, [n.x, n.y, n.z], goal))) { done = { status: 'found', path: pathTo(n, goal.slice()) }; return; }
    for (const [dx, dy] of DIRS) {
      const ix = n.ix + dx, iy = n.iy + dy;
      if (Math.abs(ix) > R || Math.abs(iy) > R) continue;
      const len = Math.hypot(dx, dy) * c, ux = dx / Math.hypot(dx, dy), uy = dy / Math.hypot(dx, dy);
      let tx = n.x + dx * c, ty = n.y + dy * c, ex = ix, ey = iy, cost = len, gz = walkEdge(env, s, n.x, n.y, n.z, tx, ty);
      if (gz !== null && !roomAt(ix, iy, tx, ty, gz)) continue; // walkable, but too close to a wall
      for (let k = 1; gz === null && k <= 3; k++) { // not walkable: look for a landing a jump can reach
        if (env.blocked && env.blocked(n.x, n.y, n.z, ux, uy, len * k, PET.jumpUp * s)) break; // a wall too high to jump
        const px = n.x + dx * c * k, py = n.y + dy * c * k, z2 = env.ground(px, py, n.z + PET.jumpUp * s);
        if (z2 === null || z2 === undefined || z2 - n.z > PET.jumpUp * s || n.z - z2 > PET.jumpDown * s) continue;
        gz = z2; tx = px; ty = py; ex = n.ix + dx * k; ey = n.iy + dy * k; cost = len * k * 3;
      }
      if (gz === null) continue;
      const kk = key(ex, ey), old = nodes.get(kk), g2 = n.g + cost;
      if (old && old.g <= g2) continue;
      const m = { ix: ex, iy: ey, x: tx, y: ty, z: gz, g: g2, f: g2 + h(tx, ty), parent: n, closed: false };
      nodes.set(kk, m); open.push(m);
    }
  };
  const bestResult = () => (best !== first && h(best.x, best.y) < h(start[0], start[1]) - c ? { status: 'partial', path: pathTo(best, null) } : { status: 'failed', path: null });
  return {
    step(maxN) {
      for (let i = 0; i < maxN && !done; i++) {
        if (!open.length || expanded >= PET.planMax) { done = bestResult(); break; }
        expand();
      }
      return done || { status: 'searching' };
    },
  };
}

// Send the pet to a point. A far target means a run. Any idle action ends. With env, a straight line that is not
// walkable starts a route search (the pet turns toward the goal while it searches); without env it goes straight.
export function petGo(p, point, env) {
  const s = p.scale || 1;
  p.goal = point.slice(); p.dest = point.slice(); p.target = point.slice(); p.path = []; p.planning = null; p.route = null; p.routeId = (p.routeId || 0) + 1; p.replans = 0; p.meowsLeft = 0;
  // Walk or run is a mood, not a rule: the farther the target, the more likely a run (env.rand() gives 0..1).
  const d = Math.hypot(point[0] - p.pos[0], point[1] - p.pos[1]) / s, rand = (env && env.rand) || Math.random;
  p.run = rand() < (d < PET.runDist / 2 ? 0.1 : d < PET.runDist ? 0.35 : 0.85);
  p.act = 'stand'; p.actT = 0; p.idleT = 0;
  if (p.run && rand() < 0.6) petThink(p, 'run', rand);
  if (env && env.ground && env.blocked && !lineClear(env, s, p.pos, point)) { p.planning = createPlanner(p.pos, point, s, env); p.target = null; }
  return p;
}

const wrapAngle = a => Math.atan2(Math.sin(a), Math.cos(a));

// The user clicked the cat: it stops what it is doing and enjoys being petted (eyes shut, a purr). Each click
// restarts the purr and counts in p.petCount, so the page can show hearts and play a purr.
export function petPetted(p) {
  p.target = null; p.goal = null; p.dest = null; p.path = []; p.planning = null; p.route = null; p.routeId = (p.routeId || 0) + 1;
  p.speed = 0; p.meowsLeft = 0; p.meowIn = undefined;
  p.act = 'purr'; p.actT = 0; p.idleT = 0;
  p.petCount = (p.petCount || 0) + 1;
  petThink(p, 'purr');
  return p;
}

// The pet cannot get to where it was sent: it sits up and meows for attention (p.meowCount counts the calls, so the
// page can show a bubble and play a sound), and calls again after a pause, PET.meows times in all.
function startMeow(p, more) { p.act = 'meow'; p.actT = 0; p.idleT = 0; p.meowCount = (p.meowCount || 0) + 1; p.meowsLeft = more; }

// Look for a landing ahead (along the heading) in reach of a jump: not more than jumpUp higher or jumpDown
// lower, with no wall in the way at the jump height. Returns true when a jump has started.
function tryJump(p, ux, uy, env, s) {
  if (!env.ground) return false;
  for (const d of [0.7, 1.3, 2, 3]) {
    if (env.blocked && env.blocked(p.pos[0], p.pos[1], p.pos[2], ux, uy, d * s, PET.jumpUp * s)) continue;
    const x = p.pos[0] + ux * d * s, y = p.pos[1] + uy * d * s, gz = env.ground(x, y, p.pos[2] + PET.jumpUp * s);
    if (gz === null || gz === undefined) continue;
    const dz = gz - p.pos[2];
    if (dz > PET.jumpUp * s || -dz > PET.jumpDown * s) continue;
    p.jump = { from: p.pos.slice(), to: [x, y, gz], t: 0, dur: 0.4 + 0.08 * d, h: (0.35 + 0.25 * Math.max(0, dz / s)) * s };
    if ((env.rand || Math.random)() < 0.5) petThink(p, 'jump', env.rand);
    return true;
  }
  return false;
}

// A small playful hop forward (a short, low jump) when the ground ahead is walkable and clear.
function hop(p, ux, uy, env, s) {
  const d = 0.9 * s, x = p.pos[0] + ux * d, y = p.pos[1] + uy * d;
  const gz = env.ground ? env.ground(x, y, p.pos[2] + PET.stepUp * s) : p.pos[2];
  if (gz === null || gz === undefined || Math.abs(gz - p.pos[2]) > PET.stepUp * s) return false;
  if (env.blocked && env.blocked(p.pos[0], p.pos[1], p.pos[2], ux, uy, d + PET.wallProbe * s)) return false;
  p.jump = { from: p.pos.slice(), to: [x, y, gz], t: 0, dur: 0.38, h: 0.3 * s };
  return true;
}

// One time step. env.ground(x, y, zFrom) returns the ground height there or null; env.blocked(x, y, z,
// dirX, dirY, length, height) is true when a wall is in the way (height defaults to half the pet);
// env.rand() gives 0..1 for the idle choices (default Math.random).
//  - While a route search runs (p.planning) the pet turns toward the goal; each step expands PET.planNodes cells.
//  - With a target (the goal, or the next point of the route), the pet walks along its heading and turns toward the
//    target as it goes, so a turn is an arc, not a spin: the heading eases toward the target direction (at most
//    turnRate rad/s), and the further it is from facing the target, the slower it walks. Passing a route point is
//    smooth (a wide arrival radius, no stop); it slows down only at the goal.
//  - At a wall, an edge or a step it cannot walk, it jumps if a landing is in reach; else it stops.
//  - With no target it idles: stand, sit, then an action, and in the end it sleeps.
// p.moving stays true while it turns, walks, runs or jumps.
export function petStep(p, dt, env = {}) {
  const s = p.scale || 1, rand = env.rand || Math.random;
  p.moving = false;
  if (p.planning && !p.jump) {
    const g = p.goal, dg = wrapAngle(Math.atan2(g[1] - p.pos[1], g[0] - p.pos[0]) - p.heading), maxT = PET.turnRate * dt;
    const turn = Math.max(-maxT, Math.min(maxT, dg * Math.min(1, PET.turnGain * dt)));
    p.heading = wrapAngle(p.heading + turn); p.phase += Math.abs(turn) * 1.2; p.moving = Math.abs(turn) > 1e-4;
    const r = p.planning.step(PET.planNodes);
    if (r.status !== 'searching') {
      p.planning = null; p.routeId++;
      if (r.path && r.path.length) { p.path = r.path.map(q => q.slice()); p.target = p.path.shift(); p.route = [p.pos.slice(), ...r.path]; }
      else p.target = p.goal.slice(); // no route found: try the straight line (it will jump or stop)
    }
  }
  if (p.jump) {
    const j = p.jump; j.t = Math.min(1, j.t + dt / j.dur);
    const u = j.t;
    p.pos = [j.from[0] + (j.to[0] - j.from[0]) * u, j.from[1] + (j.to[1] - j.from[1]) * u, j.from[2] + (j.to[2] - j.from[2]) * u + 4 * j.h * u * (1 - u)];
    p.moving = true;
    if (u >= 1) { p.pos = j.to.slice(); p.jump = null; }
  } else if (p.target) {
    const dx = p.target[0] - p.pos[0], dy = p.target[1] - p.pos[1], dist = Math.hypot(dx, dy);
    const last = !p.path || p.path.length === 0, reach = last ? PET.arrive * s : PET.waypoint * PET.cell * s;
    const finish = () => { // arrived, or gave up: a pet that ends far from where it was sent is stuck, and calls for help
      const stuck = p.dest && Math.hypot(p.pos[0] - p.dest[0], p.pos[1] - p.dest[1]) > PET.stuckDist * s;
      p.target = null; p.goal = null; p.dest = null; p.path = []; p.speed = 0; p.route = null; p.routeId++;
      if (stuck) startMeow(p, PET.meows - 1);
      else if (rand() < 0.4) petThink(p, 'arrive', rand);
    };
    if (dist < reach) {
      if (last) { p.pos = [p.target[0], p.target[1], p.pos[2]]; finish(); }
      else p.target = p.path.shift();
    } else {
      const diff = wrapAngle(Math.atan2(dy, dx) - p.heading), maxTurn = PET.turnRate * dt;
      const turn = Math.max(-maxTurn, Math.min(maxTurn, diff * Math.min(1, PET.turnGain * dt))); // eases out: no overshoot
      p.heading = wrapAngle(p.heading + turn);
      p.phase += Math.abs(turn) * 1.2; // the feet step while it turns
      p.moving = true;
      const want = p.run ? PET.run : PET.speed;
      p.speed += Math.max(-PET.accel * dt, Math.min(PET.accel * dt, want - p.speed));
      const facing = 0.25 + 0.75 * Math.max(0, Math.cos(diff)); // 1 when facing the target, 0.25 when facing away
      const near = last ? Math.min(1, 0.4 + dist / (2.5 * s)) : 1; // slow down on arrival at the goal only
      const step = Math.min(p.speed * s * facing * near * dt, dist);
      const ux = Math.cos(p.heading), uy = Math.sin(p.heading);
      const stop = () => { finish(); p.moving = false; };
      const nx = p.pos[0] + ux * step, ny = p.pos[1] + uy * step;
      const wall = env.blocked && env.blocked(p.pos[0], p.pos[1], p.pos[2], ux, uy, step + PET.wallProbe * s);
      const z = wall ? null : (env.ground ? env.ground(nx, ny, p.pos[2] + PET.stepUp * s) : p.pos[2]);
      if (wall || z === null || z === undefined || z - p.pos[2] > PET.stepUp * s || p.pos[2] - z > PET.stepDown * s) {
        if (Math.abs(diff) > 0.5) { /* still turning toward the target: wait here, do not stop */ }
        else if (tryJump(p, ux, uy, env, s)) { /* jumping */ }
        else if (p.goal && env.ground && env.blocked && (p.replans || 0) < PET.replans) { // blocked: search for a new route from here
          p.replans = (p.replans || 0) + 1; p.planning = createPlanner(p.pos, p.goal, s, env); p.path = []; p.target = null; p.moving = false;
        } else stop();
      } else {
        p.pos = [nx, ny, z];
        p.phase += step / ((p.run ? PET.runStride : PET.stride) * s) * Math.PI;
        if (p.playful) { // now and then, a playful hop forward on flat ground
          p.hopIn = (p.hopIn === undefined ? 4 : p.hopIn) - dt;
          if (p.hopIn <= 0) {
            p.hopIn = 3 + rand() * 6;
            if (rand() < PET.hopChance && dist > 3 * s && Math.abs(diff) < 0.2) hop(p, ux, uy, env, s);
          }
        }
      }
    }
  }
  if (!p.moving && !p.jump && !p.planning) { // idle
    p.speed = 0; p.idleT += dt; p.actT += dt;
    p.thinkIn = (p.thinkIn === undefined ? 5 : p.thinkIn) - dt; // every 7 to 15 s, a thought that fits what it is doing
    if (p.thinkIn <= 0) {
      p.thinkIn = 7 + rand() * 8;
      if (!p.thought && p.act !== 'meow') petThink(p, p.meowsLeft > 0 ? 'stuck' : ({ sit: 'idle', stand: 'idle' }[p.act] || p.act), rand);
    }
    const waiting = p.meowsLeft > 0; // stuck: it waits and calls again instead of picking an idle action
    if (p.act === 'stand' && p.idleT > PET.sitAfter) { p.act = 'sit'; p.actT = 0; }
    else if (p.act === 'sit' && waiting) { p.meowIn = (p.meowIn === undefined ? PET.meowEvery : p.meowIn) - dt; if (p.meowIn <= 0) { p.meowIn = undefined; startMeow(p, p.meowsLeft - 1); } }
    else if (p.act === 'sit' && p.actT > 1) {
      if (p.idleT > PET.sleepAfter) p.act = 'sleep';
      else p.act = PET_IDLE[Math.min(PET_IDLE.length - 1, Math.floor(rand() * PET_IDLE.length))];
      p.actT = 0;
    } else if (PET_ACTS[p.act] !== undefined && p.actT > PET_ACTS[p.act]) { p.act = 'sit'; p.actT = 0; }
  }
  if (p.thought) { p.thought.t += dt; if (p.thought.t > p.thought.dur) p.thought = null; }
  // Smooth the idle pose and the blend weight (the walk, run and jump poses repeat fast, so they are never smoothed).
  const busy = p.moving || !!p.jump;
  p.w = (p.w || 0) + ((busy ? 1 : 0) - (p.w || 0)) * (1 - Math.exp(-(busy ? 12 : 8) * dt));
  if (p.jump) p.w = 1;
  const goal = IDLE_POSES[busy ? 'stand' : p.act];
  p.ib = lerpPose(p.ib || goal, goal, 1 - Math.exp(-6 * dt));
  return p;
}

// ---- The figure ----
// An original cartoon cat (an orange tabby with a cream muzzle, chest and paws, round cheeks, big green
// eyes, pointed ears, whiskers, back stripes and a long curled tail). It is not based on any existing
// character. Local axes: +X forward, +Y left, +Z up, feet at z = 0; about 1 ft tall at scale 1. Built
// only from spheres, cones, boxes and one tube (no image or model file), so the page stays one file.
// Returns { group, parts }; petApply() moves it. legL and legR are the front legs, hindL and hindR the
// hind legs; each is a hip pivot with a knee group inside (userData.knee). The head is its own group
// (parts.head) so it can nod and turn; parts.eyes close; parts.mouth and parts.tongue show for a yawn or lick.
export function createPetModel(THREE) {
  const mat = (c, glow = 0.3) => new THREE.MeshLambertMaterial({ color: c, emissive: c, emissiveIntensity: glow }); // a little glow keeps the colors bright in the dark theme
  const ORANGE = mat(0xf3a152), STRIPE = mat(0xd9772c), CREAM = mat(0xfff1de), PINK = mat(0xf6a5ae), EYE = mat(0x7fd467), DARK = mat(0x2a2320, 0), WHITE = mat(0xffffff, 0.6);
  const group = new THREE.Group();
  group.rotation.order = 'ZYX'; // heading (Z) first, then pitch (Y), then a small roll about the forward axis (X)
  const add = (parent, geo, m, x, y, z, sx = 1, sy = 1, sz = 1) => {
    const o = new THREE.Mesh(geo, m); o.position.set(x, y, z); o.scale.set(sx, sy, sz); parent.add(o); return o;
  };
  const ball = new THREE.SphereGeometry(1, 24, 16);
  const cone = (r, h) => { const g = new THREE.ConeGeometry(r, h, 3); g.rotateX(Math.PI / 2); return g; }; // 3-sided: a pointed triangle ear, tip up (+Z)
  const post = (r, h) => { const g = new THREE.CylinderGeometry(r, r, h, 12); g.rotateX(Math.PI / 2); return g; };
  const smile = () => { const g = new THREE.TorusGeometry(0.022, 0.004, 6, 10, Math.PI); g.rotateZ(Math.PI); g.rotateY(Math.PI / 2); return g; };
  add(group, ball, ORANGE, 0, 0, 0.36, 0.33, 0.2, 0.19);                      // chubby body
  add(group, ball, CREAM, 0.02, 0, 0.27, 0.27, 0.16, 0.1);                    // cream belly
  add(group, ball, CREAM, 0.26, 0, 0.31, 0.13, 0.12, 0.13);                   // cream chest
  for (const x of [-0.1, 0.02, 0.14]) add(group, ball, STRIPE, x, 0, 0.545, 0.022, 0.1, 0.014); // stripes on the back
  // Head: its own group, pivoting at the neck. addH takes body coordinates.
  const NECK = [0.3, 0, 0.5];
  const head = new THREE.Group(); head.position.set(NECK[0], NECK[1], NECK[2]); group.add(head);
  const addH = (geo, m, x, y, z, sx = 1, sy = 1, sz = 1) => add(head, geo, m, x - NECK[0], y - NECK[1], z - NECK[2], sx, sy, sz);
  addH(ball, ORANGE, 0.42, 0, 0.58, 0.24, 0.25, 0.21);                        // big round head
  const eyes = [];
  for (const s of [1, -1]) {
    addH(ball, ORANGE, 0.42, s * 0.2, 0.5, 0.09, 0.07, 0.08);                 // round cheek fluff
    addH(ball, PINK, 0.605, s * 0.175, 0.515, 0.012, 0.045, 0.03);            // blush
    const eye = new THREE.Group(); eye.position.set(0.61 - NECK[0], s * 0.105, 0.6 - NECK[2]); head.add(eye); eyes.push(eye); // squashed in z to close
    add(eye, ball, EYE, 0, 0, 0, 0.036, 0.058, 0.066);                        // big green eye
    add(eye, ball, DARK, 0.026, 0, 0, 0.012, 0.03, 0.046);                    // pupil
    add(eye, ball, WHITE, 0.038, s * 0.01, 0.028, 0.008, 0.014, 0.016);       // big highlight
    add(eye, ball, WHITE, 0.035, -s * 0.01, -0.026, 0.006, 0.008, 0.008);     // small highlight
    addH(smile(), DARK, 0.69, s * 0.022, 0.5);                                // one half of a "w" mouth
    for (const a of [0.25, 0, -0.25]) {                                       // three whiskers
      const w = addH(new THREE.BoxGeometry(0.003, 0.2, 0.003), DARK, 0.66, s * 0.13, 0.515 + a * 0.1);
      w.rotation.z = s * 0.4; w.rotation.x = a * 0.5;
    }
  }
  addH(ball, CREAM, 0.632, 0, 0.525, 0.07, 0.09, 0.055);                      // cream muzzle
  addH(ball, PINK, 0.693, 0, 0.545, 0.014, 0.024, 0.016);                     // pink nose
  addH(ball, DARK, 0.688, 0, 0.51, 0.005, 0.004, 0.012);                      // line from nose to mouth
  for (const x of [-0.025, 0, 0.025]) addH(ball, STRIPE, 0.5 + x * 2, x * 4, 0.785, 0.02, 0.012, 0.01); // forehead marks
  const mouth = addH(ball, DARK, 0.69, 0, 0.495, 0.014, 0.012, 0.004); mouth.visible = false;   // open mouth (yawn)
  const tongue = addH(ball, PINK, 0.694, 0, 0.482, 0.01, 0.02, 0.02); tongue.visible = false;   // tongue (yawn, lick, groom)
  const mkEar = side => {
    const pivot = new THREE.Group(); pivot.position.set(0.4 - NECK[0], side * 0.16, 0.74 - NECK[2]); pivot.rotation.x = -side * 0.3; pivot.userData.tilt = 0.3;
    add(pivot, cone(0.11, 0.21), ORANGE, 0, 0, 0.105, 1, 0.5, 1);             // pointed ear
    add(pivot, cone(0.065, 0.14), PINK, 0.014, 0, 0.085, 1, 0.3, 1);          // pink inside
    head.add(pivot); return pivot;
  };
  const earL = mkEar(1), earR = mkEar(-1);
  // Legs: hip pivot -> upper leg -> knee group -> lower leg and paw (feet at z = 0 when standing).
  const mkLeg = (x, side) => {
    const hip = new THREE.Group(); hip.position.set(x, side * 0.1, 0.28);
    add(hip, post(0.05, 0.12), ORANGE, 0, 0, -0.06);                          // upper leg
    const knee = new THREE.Group(); knee.position.set(0, 0, -0.12); hip.add(knee); hip.userData.knee = knee;
    add(knee, post(0.042, 0.12), ORANGE, 0, 0, -0.06);                        // lower leg
    add(knee, ball, CREAM, 0.02, 0, -0.125, 0.075, 0.055, 0.04);              // round cream paw
    group.add(hip); return hip;
  };
  const legL = mkLeg(0.22, 1), legR = mkLeg(0.22, -1), hindL = mkLeg(-0.22, 1), hindR = mkLeg(-0.22, -1);
  // Tail: a long, thick curve that rises from the rump and curls forward at the tip.
  const pts = [[0, 0, 0], [-0.12, 0, 0.05], [-0.22, 0, 0.18], [-0.25, 0, 0.34], [-0.2, 0, 0.48], [-0.12, 0, 0.55]];
  const tail = new THREE.Group(); tail.position.set(-0.31, 0, 0.4);
  add(tail, new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts.map(([x, y, z]) => new THREE.Vector3(x, y, z))), 20, 0.042, 10, false), ORANGE, 0, 0, 0);
  add(tail, ball, STRIPE, -0.12, 0, 0.555, 0.05, 0.05, 0.05);                 // darker tail tip
  add(tail, ball, STRIPE, -0.245, 0, 0.3, 0.046, 0.046, 0.014);               // tail rings
  add(tail, ball, STRIPE, -0.225, 0, 0.42, 0.046, 0.046, 0.014);
  group.add(tail);
  const shadow = new THREE.Mesh(new THREE.CircleGeometry(0.34, 24), new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.22, depthWrite: false }));
  shadow.position.z = 0.02; shadow.scale.set(1.5, 0.75, 1); group.add(shadow);
  return { group, parts: { legL, legR, hindL, hindR, earL, earR, tail, shadow, head, eyes, mouth, tongue } };
}

// Put the figure at the pet's position, heading and pose. Pitch turns the body about the hind paws, so
// they stay on the ground. The shadow stays flat on the ground.
export function petApply(model, p) {
  const s = p.scale || 1, o = petPose(p), g = model.group, th = o.pitch, px = -0.22;
  const offX = px * (1 - Math.cos(th)), offZ = px * Math.sin(th), ch = Math.cos(p.heading), sh = Math.sin(p.heading);
  g.position.set(p.pos[0] + offX * ch * s, p.pos[1] + offX * sh * s, p.pos[2] + (offZ + o.drop + o.bob) * s);
  g.rotation.set(o.roll, th, p.heading);
  g.scale.set(s, s, s * (1 + 0.02 * o.breath));
  const { legL, legR, hindL, hindR, earL, earR, tail, shadow, head, eyes, mouth, tongue } = model.parts;
  const joint = (leg, hip, knee) => { leg.rotation.y = hip; leg.userData.knee.rotation.y = knee; };
  joint(legL, o.flHip, o.flKnee); joint(legR, o.frHip, o.frKnee); joint(hindL, o.hlHip, o.hlKnee); joint(hindR, o.hrHip, o.hrKnee);
  head.rotation.set(o.headTilt, o.headPitch, o.headYaw);
  earL.rotation.set(-(earL.userData.tilt || 0.4) - o.ear, -o.earBack * 0.9, 0); earR.rotation.set((earR.userData.tilt || 0.4) + o.ear, -o.earBack * 0.9, 0);
  tail.rotation.set(0, o.tailPitch, o.tail);
  for (const e of eyes) e.scale.z = Math.max(0.06, o.eyes);
  mouth.visible = o.mouth > 0.03; mouth.scale.set(0.014, 0.012 + 0.045 * o.mouth, 0.004 + 0.055 * o.mouth);
  tongue.visible = o.tongue > 0.05 || o.mouth > 0.5; tongue.scale.set(0.01, 0.02, 0.012 + 0.03 * Math.max(o.tongue, o.mouth * 0.6));
  shadow.position.z = 0.02 - o.drop - o.bob; shadow.rotation.set(-o.roll, -th, 0);
  return model;
}
