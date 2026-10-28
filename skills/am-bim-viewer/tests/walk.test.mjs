// First-person walk: deterministic fixed steps (a mock clock of dt = 1/60 s).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { core } from './load-core.mjs';

const DT = 1 / 60;
const run = (w, keys, seconds, env) => { for (let i = 0; i < Math.round(seconds / DT); i++) core.walkStep(w, keys, DT, env); return w; };
const northWalker = (extra = {}) => ({ saved: null, pos: [0, 0, 5.5], yaw: Math.PI / 2, pitch: 0, vel: [0, 0], eyeOffset: 0, isCollision: true, ...extra });

test('W moves along the flattened view direction (+Y looking north) and never changes Z when pitch != 0', () => {
  for (const pitch of [0, 60 * Math.PI / 180, -70 * Math.PI / 180]) {
    const w = run(northWalker({ pitch }), { forward: true }, 2);
    assert.ok(Math.abs(w.pos[0]) < 1e-9, 'no sideways drift');
    assert.ok(w.pos[1] > 6 && w.pos[1] < 9, 'about 4.5 ft/s with smooth start: y ' + w.pos[1]);
    assert.equal(w.pos[2], 5.5, 'Z unchanged at pitch ' + pitch);
  }
  const r = run(northWalker(), { forward: true, run: true }, 2);
  assert.ok(r.pos[1] > 20, 'Shift runs at 3x: y ' + r.pos[1]);
  const d = run(northWalker(), { right: true }, 1);
  assert.ok(d.pos[0] > 3 && Math.abs(d.pos[1]) < 1e-9, 'D strafes east when facing north');
});

test('Smooth stop: releasing W decelerates instead of stopping dead', () => {
  const w = run(northWalker(), { forward: true }, 1);
  const y1 = w.pos[1];
  core.walkStep(w, {}, DT);
  assert.ok(w.pos[1] > y1, 'still gliding one frame after release');
  run(w, {}, 2);
  assert.ok(Math.hypot(w.vel[0], w.vel[1]) < 1e-3, 'comes to rest');
});

test('Mouse +x turns right (clockwise in plan); pitch is clamped to +/-85 deg', () => {
  const w = northWalker();
  core.walkLook(w, 200, 0);
  assert.ok(w.yaw < Math.PI / 2 && Math.cos(w.yaw) > 0, 'from north toward east: yaw ' + w.yaw);
  core.walkLook(w, 0, -1e6);
  assert.ok(Math.abs(w.pitch - 85 * Math.PI / 180) < 1e-12);
  core.walkLook(w, 0, 1e6);
  assert.ok(Math.abs(w.pitch + 85 * Math.PI / 180) < 1e-12);
});

test('Eye height = floor + 5.5 ft, following floors and stairs smoothly; Q/E adjust it', () => {
  const env = { floorZ: (x, y) => (y < 10 ? 0 : 2) }; // a 2 ft step up at y = 10
  const w = run(northWalker(), { forward: true }, 4, env);
  assert.ok(w.pos[1] > 10 && Math.abs(w.pos[2] - 7.5) < 0.01, 'on the upper floor: z ' + w.pos[2]);
  run(w, { up: true }, 1, env); run(w, {}, 1, env);
  assert.ok(Math.abs(w.pos[2] - (7.5 + 4.5)) < 0.05, 'E raised the eye by 4.5 ft: z ' + w.pos[2]);
  const s = core.enterWalk(core.setNamedView(core.createViewState(), 'perspective', { min: [0, 0, 0], max: [20, 20, 10] }), () => 3);
  assert.equal(s.pos[2], 3 + 5.5, 'enter: eye = floor under the target + 5.5 ft');
});

test('Collision stops 1 ft before a wall; C (collision off) walks through', () => {
  const wall = (o, dir, max) => (dir[1] > 0.5 && o[1] < 10 ? (10 - o[1]) / dir[1] : null); // a wall at y = 10
  const w = run(northWalker(), { forward: true }, 6, { hitDist: (o, d, m) => { const h = wall(o, d, m); return h !== null && h <= m ? h : null; } });
  assert.ok(w.pos[1] <= 9 + 1e-9 && w.pos[1] > 8.8, 'stopped at y ' + w.pos[1]);
  const off = run(northWalker({ isCollision: false }), { forward: true }, 6, { hitDist: wall });
  assert.ok(off.pos[1] > 15, 'collision off: y ' + off.pos[1]);
});

test('Walk camera: perspective at the eye, Z up, looking along yaw; Esc restores the previous camera', () => {
  const before = core.setNamedView(core.createViewState(), 'top', { min: [0, 0, 0], max: [40, 30, 12] });
  const keep = JSON.parse(JSON.stringify(before));
  const w = core.enterWalk(before, () => 0);
  run(w, { forward: true }, 1);
  core.walkLook(w, 120, 40);
  const v = core.walkView(w, before);
  const cams = { persp: new THREE.PerspectiveCamera(), ortho: new THREE.OrthographicCamera() };
  const cam = core.applyView(THREE, v, cams, 1.5);
  assert.ok(cam.isPerspectiveCamera && cam.fov === 70);
  assert.deepEqual(cam.position.toArray().map(x => Math.round(x * 1e6) / 1e6), w.pos.map(x => Math.round(x * 1e6) / 1e6), 'camera at the eye');
  const dir = new THREE.Vector3(); cam.getWorldDirection(dir);
  const look = [Math.cos(w.pitch) * Math.cos(w.yaw), Math.cos(w.pitch) * Math.sin(w.yaw), Math.sin(w.pitch)];
  assert.ok(dir.toArray().every((x, i) => Math.abs(x - look[i]) < 1e-6));
  assert.ok(new THREE.Vector3(0, 1, 0).applyQuaternion(cam.quaternion).z > 0, 'screen up has +Z');
  assert.deepEqual(w.saved, keep, 'Esc returns to this camera');
  assert.deepEqual(before, keep, 'entering walk does not change the saved view');
});

test('Walk click: picks under the cursor, or under the screen center when the pointer is locked', () => {
  const rect = { left: 10, top: 20, width: 800, height: 600 };
  assert.deepEqual(core.walkPickPoint(false, 123, 456, rect), { x: 123, y: 456 }, 'unlocked: the cursor');
  assert.deepEqual(core.walkPickPoint(true, 123, 456, rect), { x: 410, y: 320 }, 'locked: the center of the canvas');
});
