// In-browser check for the built demo page (demo.viewer.html). Run it in the
// page (Claude Browser pane javascript tool, or the dev tools console). It
// drives the page's real pointer handlers and returns { pass, results }.
(async () => {
  const c = document.querySelector('#viewport canvas');
  const wait = ms => new Promise(r => setTimeout(r, ms));
  const r = c.getBoundingClientRect();
  function click(x, y, isDouble) {
    const ev = (t, extra = {}) => new PointerEvent(t, { bubbles: true, pointerId: 1, pointerType: 'mouse', button: 0, buttons: 1, clientX: x, clientY: y, ...extra });
    const once = () => { c.dispatchEvent(ev('pointerdown')); c.dispatchEvent(ev('pointerup', { buttons: 0 })); };
    once();
    if (isDouble) { once(); c.dispatchEvent(new MouseEvent('dblclick', { bubbles: true, button: 0, clientX: x, clientY: y })); }
  }
  function rdrag(mods, dx, dy) {
    const x = r.left + r.width / 2, y = r.top + r.height / 2;
    const ev = (t, X, Y) => new PointerEvent(t, { bubbles: true, pointerId: 1, pointerType: 'mouse', button: 2, buttons: 2, clientX: X, clientY: Y, ...mods });
    c.dispatchEvent(ev('pointerdown', x, y)); c.dispatchEvent(ev('pointermove', x + dx, y + dy)); c.dispatchEvent(ev('pointerup', x + dx, y + dy));
  }
  const near = (a, b) => a.every((v, i) => Math.abs(v - b[i]) < 1e-6);
  const results = {};
  __bim.setView('top'); await wait(50);
  let p = __bim.project(60, 50, 24); click(p.x, p.y); await wait(30);
  results.clickSelectsColumn = __bim.selectedId() === 310009 && document.querySelector('.sel-name').textContent === 'NE corner column';
  p = __bim.project(20, 10, 0); click(p.x, p.y); await wait(30);
  results.clickSelectsSlab = __bim.selectedId() === 310000;
  click(r.left + 4, r.bottom - 4); await wait(30);
  results.clickEmptyClears = __bim.selectedId() === null;
  let s0 = JSON.stringify(__bim.state.target); rdrag({}, 40, 0);
  results.parallelRightDragPans = JSON.stringify(__bim.state.target) !== s0;
  __bim.setView('perspective'); await wait(50);
  const az = __bim.state.azimuth; rdrag({}, 40, 0);
  results.perspRightDragRotates = __bim.state.azimuth < az;
  __bim.setView('perspective'); await wait(50);
  p = __bim.project(60, 50, 12); const before = __bim.panelRenders();
  click(p.x, p.y, true); await wait(450);
  results.dblClickSelectsOnce = __bim.selectedId() === 310009 && __bim.panelRenders() - before === 1;
  results.dblClickTargetsCenter = near(__bim.state.target, [60, 50, 12]);
  const corners = [];
  for (const x of [59, 61]) for (const y of [49, 51]) for (const z of [0, 24]) corners.push(__bim.project(x, y, z));
  results.dblClickFitsBox = corners.every(q => q.x >= r.left && q.x <= r.right && q.y >= r.top && q.y <= r.bottom);
  click(r.left + 4, r.top + 4, true); await wait(450);
  const after = { t: __bim.state.target.slice(), d: __bim.state.distance };
  __bim.setView('perspective');
  results.dblClickEmptyZoomsExtents = near(after.t, __bim.state.target) && Math.abs(after.d - __bim.state.distance) < 1e-6;
  const chip = document.querySelector('.chip[data-cat="Walls"]');
  const wallsVisible = () => __bim.visibleCount('Walls');
  const on0 = chip.getAttribute('aria-pressed') === 'true' && wallsVisible() === 6;
  chip.click(); await wait(30);
  const off = chip.getAttribute('aria-pressed') === 'false' && wallsVisible() === 0;
  chip.click(); await wait(30);
  results.wallsChipMatchesVisibility = on0 && off && chip.getAttribute('aria-pressed') === 'true' && wallsVisible() === 6;
  const cm = new MouseEvent('contextmenu', { bubbles: true, cancelable: true }); c.dispatchEvent(cm);
  results.contextMenuBlocked = cm.defaultPrevented;
  // Theme: dispatching am-theme-change with each theme recolors the scene (selection, outline, grid,
  // gizmo, categories) and the CSS backdrop. Restores the page's own theme afterwards.
  const html = document.documentElement, was = html.className;
  async function themed(theme) {
    html.classList.toggle('am-night', theme === 'night'); html.classList.toggle('am-paper', theme === 'paper');
    document.dispatchEvent(new CustomEvent('am-theme-change', { detail: { theme } })); await wait(60);
    return __bim.sceneColors();
  }
  const night = await themed('night'), paper = await themed('paper');
  html.className = was; document.dispatchEvent(new CustomEvent('am-theme-change', { detail: { theme: (window.amTheme && amTheme.get()) || 'paper' } })); await wait(60);
  results.themeNight = night.theme === 'night' && night.selected === '#f4f4f1' && night.outline === '#0a0a0a' && night.gizmo === '#9a9a96' && night.backdrop === 'rgb(10, 10, 10)';
  results.themePaper = paper.theme === 'paper' && paper.selected === '#0a0a0a' && paper.outline === '#f4f4f1' && paper.gizmo === '#6b6b68' && paper.backdrop === 'rgb(244, 244, 241)';
  results.themeRecolorsGrid = night.grid !== paper.grid;
  results.categoriesAreGray = Object.values(night.categories).every(h => { const c = [1, 3, 5].map(i => parseInt(h.slice(i, i + 2), 16)); return Math.max(...c) - Math.min(...c) <= 6; });
  // Clean viewport: flat --bg, no image behind the model, and the page grain (z 50) stays below it.
  const vs = getComputedStyle(document.getElementById('viewport'));
  const grainZ = Number(getComputedStyle(document.body, '::after').zIndex);
  results.viewportFlat = vs.backgroundImage === 'none' && getComputedStyle(c).backgroundImage === 'none' && Number(vs.zIndex) > grainZ;
  // View cube (replaces the axis gizmo): about 84 px, top right, clear of the help "?".
  const cubeR = document.getElementById('viewcube').getBoundingClientRect(), helpR = document.getElementById('helpbtn').getBoundingClientRect();
  const vpBox = document.getElementById('viewport').getBoundingClientRect();
  results.cubeSizeAndPlace = cubeR.width > 0 && cubeR.width <= 84 && cubeR.height <= 84 && cubeR.right <= vpBox.right && cubeR.top - vpBox.top < 30 && cubeR.bottom < helpR.top && !document.getElementById('gizmo');
  // Click cells with real hit-testing (center of the cell polygon -> elementFromPoint).
  async function clickCell(names) {
    const cell = [...document.querySelectorAll('#viewcube .cell')].find(p => p.dataset.names === names);
    if (!cell) return false;
    const b = cell.getBoundingClientRect(), x = b.left + b.width / 2, y = b.top + b.height / 2;
    const hit = document.elementFromPoint(x, y);
    if (hit !== cell) return false;
    const ev = t => new PointerEvent(t, { bubbles: true, pointerId: 5, pointerType: 'mouse', button: 0, buttons: t === 'pointerup' ? 0 : 1, clientX: x, clientY: y });
    hit.dispatchEvent(ev('pointerdown')); hit.dispatchEvent(ev('pointerup')); await wait(450); __bim.renderNow();
    return true;
  }
  __bim.setView('perspective'); __bim.renderNow();
  const topClicked = await clickCell('top');
  const ctr = __bim.bounds, cc = [(ctr.min[0] + ctr.max[0]) / 2, (ctr.min[1] + ctr.max[1]) / 2, ctr.min[2]];
  const o = __bim.project(...cc), pn = __bim.project(cc[0], cc[1] + 10, cc[2]), pe = __bim.project(cc[0] + 10, cc[1], cc[2]);
  results.cubeTopNorthUp = topClicked && __bim.state.projection === 'parallel' && pn.y < o.y - 5 && Math.abs(pn.x - o.x) < 0.5 && pe.x > o.x + 5 && Math.abs(pe.y - o.y) < 0.5;
  __bim.setView('perspective'); __bim.state.azimuth += Math.PI; __bim.renderNow(); // orbit to the north-west: BACK is visible
  const shownAfterOrbit = [...new Set([...document.querySelectorAll('#viewcube .cell')].map(c => c.dataset.names.split(' ')[0]))].sort().join();
  const backClicked = await clickCell('back');
  results.cubeFollowsAndBack = shownAfterOrbit === 'back,left,top' && backClicked && Math.abs(__bim.state.azimuth - Math.PI / 2) < 1e-6 && __bim.state.elevation === 0;
  const ring = document.querySelector('#viewcube [data-ring]'), rb = ring.getBoundingClientRect(), az0 = __bim.state.azimuth;
  const rev = (t, X) => new PointerEvent(t, { bubbles: true, pointerId: 6, pointerType: 'mouse', button: 0, buttons: t === 'pointerup' ? 0 : 1, clientX: X, clientY: rb.bottom - 2 });
  ring.dispatchEvent(rev('pointerdown', rb.left + rb.width / 2)); document.getElementById('viewcube').dispatchEvent(rev('pointermove', rb.left + rb.width / 2 + 40)); document.getElementById('viewcube').dispatchEvent(rev('pointerup', rb.left + rb.width / 2 + 40));
  results.cubeRingSpins = Math.abs(__bim.state.azimuth - az0) > 0.3 && __bim.state.elevation === 0;
  __bim.setView('perspective');
  // Grid at a grazing angle: the far band dissolves into the clean field (low pixel variance);
  // negative control with the density fade off must alias into texture (high variance).
  async function farBandVariance(fade) {
    __bim.gridOnly(true); __bim.gridDensityFade(fade);
    const s = __bim.state, b = __bim.bounds;
    s.projection = 'perspective'; s.elevation = 3 * Math.PI / 180;
    s.target = [(b.min[0] + b.max[0]) / 2, (b.min[1] + b.max[1]) / 2, b.min[2]];
    s.distance = Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1]) * 0.6;
    __bim.renderNow(); await wait(30);
    const w = c.width, h = c.height, cv = document.createElement('canvas'); cv.width = w; cv.height = h;
    const g = cv.getContext('2d'); g.fillStyle = getComputedStyle(document.getElementById('viewport')).backgroundColor; g.fillRect(0, 0, w, h); g.drawImage(c, 0, 0);
    const horizon = h / 2 - (Math.tan(s.elevation) / Math.tan((s.fov * Math.PI) / 360)) * (h / 2);
    const y0 = Math.round(horizon + h * 0.005), y1 = Math.round(horizon + h * 0.08);
    const px = g.getImageData(Math.round(w * 0.1), y0, Math.round(w * 0.8), y1 - y0).data;
    // High-frequency energy: mean absolute luminance step between neighbouring pixels (texture, not lines).
    const bw = Math.round(w * 0.8), bh = y1 - y0, L = i => px[i] * .2126 + px[i + 1] * .7152 + px[i + 2] * .0722;
    let n = 0, e = 0;
    for (let y = 0; y < bh - 1; y++) for (let x = 0; x < bw - 1; x++) { const i = (y * bw + x) * 4; e += Math.abs(L(i) - L(i + 4)) + Math.abs(L(i) - L(i + bw * 4)); n++; }
    return e / n;
  }
  const varOn = await farBandVariance(true), varOff = await farBandVariance(false);
  __bim.gridDensityFade(true); __bim.gridOnly(false); __bim.setView('perspective'); await wait(30);
  results.gridNoMoire = varOn < 4.5 && varOff > 4.5 && varOff / varOn > 2; // measured: on 3.0, off 7.3 (night demo)
  results.gridVariance = { on: Math.round(varOn * 10) / 10, off: Math.round(varOff * 10) / 10, isInfo: true };
  // Cursors: the soft shadow (paper) or halo (night) really renders outside the outline.
  // Render each cursor SVG into an <img> at 1x and 3x; count pixels that are empty without the
  // filter but painted with it. Negative control: the same count on the unfiltered render is 0.
  async function haloPixels(name, theme, scale, isFiltered) {
    const svgOf = f => __bim.cursor.svg(name, theme, { filter: f });
    const load = src => new Promise((res, rej) => { const im = new Image(); im.onload = () => res(im); im.onerror = rej; im.src = 'data:image/svg+xml,' + encodeURIComponent(src); });
    const draw = async src => { const cv = document.createElement('canvas'); cv.width = cv.height = 32 * scale; const g = cv.getContext('2d'); g.drawImage(await load(src), 0, 0, cv.width, cv.height); return g.getImageData(0, 0, cv.width, cv.height).data; };
    const bare = await draw(svgOf(false)), test = await draw(svgOf(isFiltered));
    let shape = 0, halo = 0;
    for (let i = 3; i < bare.length; i += 4) { if (bare[i] > 0) shape++; else if (test[i] > 6) halo++; }
    return { shape, halo };
  }
  const cursorRows = [];
  for (const name of __bim.cursor.names) for (const theme of ['paper', 'night']) for (const scale of [1, 3]) {
    const on = await haloPixels(name, theme, scale, true), off = await haloPixels(name, theme, scale, false);
    cursorRows.push(on.shape > 0 && on.halo >= 8 * scale && off.halo === 0);
  }
  results.cursorShadowRenders = cursorRows.length === __bim.cursor.names.length * 4 && cursorRows.every(Boolean);
  // Help: the pointer is simulated with real hit-testing (document.elementFromPoint) so the test
  // measures hit areas, not which element we chose to dispatch on. pointerenter/leave are sent
  // along the ancestor chains exactly as a browser does when the element under the pointer changes.
  const help = document.getElementById('help'), hb = document.getElementById('helpbtn'), hp = document.getElementById('helppanel');
  const isOpen = () => getComputedStyle(hp).visibility === 'visible' && hb.getAttribute('aria-expanded') === 'true';
  // Closed = collapsed state and no hit area (immediate); visibility fades out over --slow.
  const isClosed = () => getComputedStyle(hp).pointerEvents === 'none' && hb.getAttribute('aria-expanded') === 'false' && !help.classList.contains('open');
  let under = null;
  const chain = el => { const out = []; for (let n = el; n; n = n.parentElement) out.push(n); return out; };
  function pointerTo(x, y, buttons = 0) {
    const el = document.elementFromPoint(x, y);
    if (el === under) return el;
    const oldC = chain(under), newC = chain(el);
    const opts = { pointerId: 9, pointerType: 'mouse', clientX: x, clientY: y, buttons };
    for (const n of oldC) if (!newC.includes(n)) n.dispatchEvent(new PointerEvent('pointerleave', opts));
    for (const n of newC.slice().reverse()) if (!oldC.includes(n)) n.dispatchEvent(new PointerEvent('pointerenter', opts));
    under = el; return el;
  }
  const hbR = hb.getBoundingClientRect(), hbC = { x: hbR.left + hbR.width / 2, y: hbR.top + hbR.height / 2 };
  const vpR = document.getElementById('viewport').getBoundingClientRect();
  const away = { x: vpR.left + 60, y: vpR.top + 80 }; // far from the ? and from the open panel
  pointerTo(away.x, away.y); await wait(300);
  const rest = isClosed();
  pointerTo(hbR.left - 8, hbC.y); await wait(250);                      // 8 px outside the "?"
  const outside8 = isClosed();
  pointerTo(hbC.x, hbC.y); await wait(80); pointerTo(away.x, away.y); await wait(250); // < 150 ms then leave
  const brief = isClosed();
  pointerTo(hbC.x, hbC.y); await wait(220);                              // > 150 ms
  const opened = isOpen() && hp.querySelectorAll('kbd').length >= 9;
  const pR = hp.getBoundingClientRect();
  pointerTo(hbC.x, hbR.top - 4); await wait(60);                         // the gap between ? and panel
  pointerTo(pR.left + pR.width / 2, pR.top + pR.height / 2); await wait(350);
  const keptOverPanel = isOpen();
  pointerTo(away.x, away.y); await wait(400);                            // leave both
  const closedAfterLeave = isClosed();
  pointerTo(hbC.x, hbC.y, 2); await wait(250);                           // right-button drag passing over
  const dragIgnored = isClosed();
  pointerTo(away.x, away.y); await wait(300);
  // Without OS focus (an automated pane), focus() moves activeElement but fires no focus events: send focusin.
  hb.focus(); if (!document.hasFocus()) hb.dispatchEvent(new FocusEvent('focusin', { bubbles: true })); await wait(20);
  const focused = isOpen();
  hb.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await wait(450);
  const escaped = isClosed() && hb.getAttribute('aria-controls') === 'helppanel';
  results.helpHover = { rest, outside8, brief, opened, keptOverPanel, closedAfterLeave, dragIgnored, focused, escaped, isInfo: true };
  results.helpCollapses = rest && outside8 && brief && opened && keptOverPanel && closedAfterLeave && dragIgnored && focused && escaped;
  // Gumball (left drag on a handle): Top view, X arrow +100 px moves east, Y arrow up moves north,
  // ring CCW is positive; snapping; Esc cancels; undo; Copy for Revit gives the contract JSON.
  __bim.setView('top'); __bim.select(310009); __bim.renderNow(); await wait(30);
  const gum = document.getElementById('gumball');
  function gdragTo(handle, dx, dy, opts = {}) {
    const L = __bim.gumball.layout();
    const from = handle === 'rz' ? L.ringEast : L.tips[handle];
    const target = gum.querySelector('.h[data-h="' + handle + '"] .hit');
    const ev = (t, X, Y) => new PointerEvent(t, { bubbles: true, cancelable: true, pointerId: 7, pointerType: 'mouse', button: 0, buttons: t === 'pointerup' ? 0 : 1, clientX: X, clientY: Y, altKey: !!opts.alt });
    target.dispatchEvent(ev('pointerdown', from.x, from.y));
    const to = typeof dx === 'function' ? dx(from, L.center) : { x: from.x + dx, y: from.y + dy };
    gum.dispatchEvent(ev('pointermove', to.x, to.y));
    if (opts.cancel) window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    else gum.dispatchEvent(ev('pointerup', to.x, to.y));
  }
  const items = () => __bim.gumball.transforms().items;
  gdragTo('x', 100, 0); __bim.renderNow();
  const afterX = items()[0];
  results.gumballXEast = !!afterX && afterX.move[0] > 0 && afterX.move[1] === 0 && afterX.move[2] === 0 && afterX.move[0] % 0.5 === 0;
  gdragTo('y', 0, -100);
  const afterY = items()[0];
  results.gumballYNorth = afterY.move[1] > 0 && afterY.move[0] === afterX.move[0];
  gdragTo('rz', (from, c) => ({ x: c.x, y: c.y - (from.x - c.x) }));
  results.gumballRingCCW = items()[0].rotateZ === 90;
  results.gumballGhostAndPending = __bim.gumball.ghostCount() === 1 && __bim.gumball.pendingRows() === 1;
  const keep = JSON.stringify(items());
  gdragTo('x', 137, 0, { cancel: true });
  results.gumballEscCancels = JSON.stringify(items()) === keep && !__bim.gumball.isDragging();
  gdragTo('x', 37, 0, { alt: true }); // free: not a multiple of 1/2 ft
  results.gumballAltFree = items()[0].move[0] % 0.5 !== 0;
  document.getElementById('copyrevit').click(); await wait(30);
  const copied = JSON.parse(__bim.gumball.lastCopied());
  results.gumballCopyContract = copied.kind === 'element-transforms' && copied.source === 'am-bim-viewer' && copied.units === 'ft' &&
    copied.items.length === 1 && Object.keys(copied.items[0]).join() === 'id,name,category,move,rotateZ,pivot' && copied.items[0].id === 310009;
  // Typed entry: a click (no drag) on a handle opens the field; Enter applies exactly; invalid text
  // keeps it open with an error; Esc cancels; Tab applies and moves on; side-panel fields round-trip.
  document.querySelector('#pending [data-undo]').click(); await wait(30);
  const tbox = document.getElementById('typein'), tval = document.getElementById('typeval'), tlbl = document.getElementById('typelbl');
  const key = (k, extra = {}) => tval.dispatchEvent(new KeyboardEvent('keydown', { key: k, bubbles: true, cancelable: true, ...extra }));
  const typeIn = s => { tval.value = s; tval.dispatchEvent(new Event('input', { bubbles: true })); };
  gdragTo('x', 0, 0);
  const typeOpened = tbox.classList.contains('on') && tlbl.textContent === 'move x' && items().length === 0;
  typeIn('2\'-6 1/2"');
  const preview = document.getElementById('readout').textContent === 'move x +2\'-6 1/2" (2.542 ft)';
  key('Enter');
  results.typedEnterExact = typeOpened && preview && !tbox.classList.contains('on') && items()[0].move[0] === 2.5417;
  gdragTo('y', 0, 0); typeIn('abc'); key('Enter');
  const invalidKeeps = tbox.classList.contains('on') && document.getElementById('typeerr').classList.contains('on') && /Type a length/.test(document.getElementById('typeerr').textContent);
  key('Escape');
  results.typedInvalidAndEsc = invalidKeeps && !tbox.classList.contains('on') && items()[0].move[1] === 0;
  gdragTo('x', 0, 0); typeIn('-1'); key('Tab');
  const tabbed = tbox.classList.contains('on') && tlbl.textContent === 'move y' && items()[0].move[0] === 1.5417;
  typeIn('750mm'); key('Tab');
  results.typedTab = tabbed && tlbl.textContent === 'move z' && items()[0].move[1] === 2.4606;
  key('Escape');
  const vz = document.getElementById('vf-z'), vr = document.getElementById('vf-rz');
  vz.value = '6"'; vz.dispatchEvent(new Event('change')); vr.value = '-22.5'; vr.dispatchEvent(new Event('change'));
  const it = items()[0];
  results.sidePanelRoundTrip = it.move[2] === 0.5 && it.rotateZ === -22.5 && it.move[0] === 1.5417 && document.getElementById('vf-x').value === '1.5417';
  document.querySelector('#pending [data-undo]').click(); await wait(30);
  results.gumballUndo = items().length === 0 && __bim.gumball.ghostCount() === 0 && __bim.gumball.meshMatrix(310009).every((v, i) => v === [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1][i]);
  __bim.select(null); __bim.setView('perspective');
  // Walk: G enters (perspective, fov 70, eye 5.5 ft above the floor, gumball and cube off);
  // W moves along the flattened view direction without changing Z while looking up;
  // right drag looks (pointer lock is denied in an automated pane); Esc restores the old camera.
  __bim.setView('perspective'); __bim.select(310009); __bim.state.target = [20, 10, 5]; __bim.renderNow(); await wait(30); // target over the floor slab
  const camSnap = () => JSON.stringify([__bim.state.azimuth, __bim.state.elevation, __bim.state.target, __bim.state.distance, __bim.state.projection].flat().map(v => typeof v === 'number' ? Math.round(v * 1e6) / 1e6 : v));
  const before0 = camSnap();
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'g', bubbles: true })); await wait(50);
  __bim.walk.pause(true);
  const w0 = __bim.walk.get();
  results.walkEnter = __bim.walk.active() && __bim.state.fov === 70 && __bim.state.projection === 'perspective' && __bim.selectedId() === null &&
    document.getElementById('gumball').innerHTML === '' && getComputedStyle(document.getElementById('cubewrap')).pointerEvents === 'none' &&
    w0.eyeAboveFloor !== null && Math.abs(w0.eyeAboveFloor - 5.5) < 0.05 && document.getElementById('walkinfo').classList.contains('on');
  results.walkEye = { eyeAboveFloor: w0.eyeAboveFloor, isInfo: true };
  __bim.walk.look(0, -300); // look up
  const w1 = __bim.walk.get();
  for (let i = 0; i < 60; i++) __bim.walk.step(1 / 60, { w: true });
  const w2 = __bim.walk.get(), mv = [w2.pos[0] - w1.pos[0], w2.pos[1] - w1.pos[1]], ml = Math.hypot(mv[0], mv[1]);
  results.walkForwardFlat = w1.pitch > 0.5 && ml > 2 && Math.abs(mv[0] / ml - Math.cos(w1.yaw)) < 1e-3 && Math.abs(mv[1] / ml - Math.sin(w1.yaw)) < 1e-3 && Math.abs(w2.pos[2] - w1.pos[2]) < 0.05;
  const cr = c.getBoundingClientRect(), rx = cr.left + cr.width / 2, ry = cr.top + cr.height / 2;
  const rv = (t, X) => new PointerEvent(t, { bubbles: true, pointerId: 8, pointerType: 'mouse', button: 2, buttons: t === 'pointerup' ? 0 : 2, clientX: X, clientY: ry });
  c.dispatchEvent(rv('pointerdown', rx)); c.dispatchEvent(rv('pointermove', rx + 50)); c.dispatchEvent(rv('pointerup', rx + 50));
  results.walkRightDragLooksRight = __bim.walk.get().yaw < w2.yaw - 0.05;
  window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })); await wait(500);
  results.walkEscRestores = !__bim.walk.active() && camSnap() === before0 && !document.getElementById('walkinfo').classList.contains('on');  results.noFooter = !document.querySelector('footer');
  results.oneViewLabel = document.querySelectorAll('#viewname *').length === 0 && document.getElementById('viewname').textContent !== '';
  return { pass: Object.entries(results).every(([k, v]) => v && (v === true || v.isInfo)), results };
})();
