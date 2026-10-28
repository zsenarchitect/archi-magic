// Pet mode: deterministic fixed steps (a mock clock of dt = 1/60 s). No browser, no real scene.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as THREE from 'three';
import { core } from './load-core.mjs';

const DT = 1 / 60;
const flat = { ground: () => 0 };
// Step until the pet has no target; returns the number of steps and whether it jumped on the way.
const walkTo = (p, env, maxSteps = 6000) => {
  let n = 0, jumped = false;
  while ((p.goal || p.target) && n < maxSteps) { core.petStep(p, DT, env); n++; if (p.jump) jumped = true; }
  return { n, jumped };
};
const stepFor = (p, seconds, env = flat) => { for (let i = 0; i < Math.round(seconds / DT); i++) core.petStep(p, DT, env); return p; };

test('petSurface: an up-facing floor is standable, a wall is not, and a back face is flipped toward the camera', () => {
  const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1)); mesh.updateMatrixWorld(true);
  const hit = (nx, ny, nz) => ({ point: new THREE.Vector3(1, 2, 3), face: { normal: new THREE.Vector3(nx, ny, nz) }, object: mesh });
  const down = new THREE.Vector3(0, 0, -1);
  assert.equal(core.petSurface(hit(0, 0, 1), down).isStandable, true, 'floor seen from above');
  assert.equal(core.petSurface(hit(1, 0, 0), down).isStandable, false, 'wall');
  // A downward ray that meets a downward-pointing face hits its back side, so the normal flips up.
  const back = core.petSurface(hit(0, 0, -1), new THREE.Vector3(0, 0, -1));
  assert.equal(back.normal[2], 1, 'a face that points away from the ray is flipped');
  assert.deepEqual(core.petSurface(hit(0, 0, 1), down).point, [1, 2, 3]);
  assert.equal(core.petSurface(null, down), null);
  const slope = Math.sin(59 * Math.PI / 180);
  assert.equal(core.petSurface(hit(0, Math.cos(59 * Math.PI / 180), slope), down).isStandable, false, 'a normal 59 deg above horizontal is too steep');
});

test('petStep: turns in an arc, never spins, and stops exactly at the target', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [10, 0, 0]);
  p.heading = Math.PI; // facing the wrong way
  let prev = p.heading, steps = 0;
  while (p.target && steps < 3000) {
    const x0 = p.pos[0], y0 = p.pos[1];
    core.petStep(p, DT, flat); steps++;
    const turned = Math.abs(Math.atan2(Math.sin(p.heading - prev), Math.cos(p.heading - prev)));
    assert.ok(turned <= core.PET.turnRate * DT + 1e-9, 'never turns faster than turnRate: ' + turned);
    prev = p.heading;
    if (p.target) assert.ok(Math.hypot(p.pos[0] - x0, p.pos[1] - y0) > 0, 'always moving forward while it turns (step ' + steps + ')');
  }
  assert.deepEqual(p.pos, [10, 0, 0], 'arrives at the target');
  assert.equal(p.target, null);
  assert.equal(p.moving, false, 'not moving after arrival');
  assert.ok(steps * DT < 20, 'arrives in a sensible time: ' + steps * DT + ' s');
});

test('petStep: takes the short way round when it turns', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [0, 10, 0]); // north = +90 deg
  p.heading = -170 * Math.PI / 180;
  core.petStep(p, DT, flat);
  assert.ok(p.heading < -170 * Math.PI / 180, 'from -170 to +90 the short way is 100 deg clockwise, so the heading decreases');
});

test('petStep: the feet step while it turns, so it never slides', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [0, 10, 0]);
  const before = p.phase; core.petStep(p, DT, flat);
  assert.ok(p.phase > before, 'the gait phase advances during a turn');
});

test('petGo: walk or run is a mood: the farther the target, the more likely a run (env.rand gives the draw)', () => {
  const run = (dist, u) => core.petGo(core.createPet([0, 0, 0]), [dist, 0, 0], { rand: () => u }).run;
  assert.deepEqual([run(2, 0.5), run(5, 0.5), run(30, 0.5)], [false, false, true], 'a middle draw: a walk for near and middle targets, a run for a far one');
  assert.deepEqual([run(2, 0.2), run(5, 0.2), run(30, 0.2)], [false, true, true], 'a lower draw runs sooner');
  assert.deepEqual([run(2, 0.05), run(30, 0.99)], [true, false], 'any target can be either: a very low draw runs, a very high one walks');
});

test('petStep: a run reaches the run speed', () => {
  const far = core.petGo(core.createPet([0, 0, 0]), [30, 0, 0], { rand: () => 0 });
  assert.equal(far.run, true);
  stepFor(far, 2);
  assert.ok(far.speed > core.PET.speed && far.speed <= core.PET.run + 1e-9, 'speeds up toward the run speed: ' + far.speed);
});

test('petStep: stops in front of a wall that is too high to jump', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [20, 0, 0]);
  const env = { ground: () => 0, blocked: (x, y, z, dx, dy, len) => x + len >= 10 }; // a wall at x = 10, at every height
  walkTo(p, env);
  assert.equal(p.target, null);
  assert.ok(p.pos[0] > 8 && p.pos[0] < 10, 'stopped just short of the wall: ' + p.pos[0]);
});

test('petStep: turns toward a target behind it when a wall is ahead, instead of stopping', () => {
  const p = core.createPet([9, 0, 0]); p.heading = 0;               // facing the wall at x = 10
  core.petGo(p, [0, 0, 0]);                                           // the target is behind it
  const env = { ground: () => 0, blocked: (x, y, z, dx, dy, len) => dx > 0 && x + len >= 10 }; // a wall only in front of it
  walkTo(p, env);
  assert.ok(p.pos[0] < 1, 'turned around and walked to the target: x ' + p.pos[0]);
});

test('petStep: small steps are walked; a ledge, drop or gap within reach is jumped; an edge, big drop or tall ledge stops it', () => {
  const go = ground => { const p = core.petGo(core.createPet([0, 0, 0]), [10, 0, 0]); const r = walkTo(p, { ground }); return { p, r }; };
  let t = go(x => (x < 5 ? 0 : 0.5));
  assert.equal(t.p.pos[2], 0.5, 'steps up a 0.5 ft kerb (below stepUp)'); assert.equal(t.r.jumped, false);
  t = go(x => (x < 5 ? 0 : 2));
  assert.deepEqual(t.p.pos, [10, 0, 2], 'jumps up a 2 ft ledge and walks on'); assert.equal(t.r.jumped, true);
  t = go(x => (x < 5 ? 0 : -3));
  assert.deepEqual(t.p.pos, [10, 0, -3], 'jumps down a 3 ft drop'); assert.equal(t.r.jumped, true);
  t = go(x => (x < 5 || x > 6.5 ? 0 : null)); // a gap from x = 5 to 6.5
  assert.equal(t.r.jumped, true, 'jumps a gap about 1.5 ft wide'); assert.deepEqual(t.p.pos, [10, 0, 0], 'and lands on the far side');
  t = go(x => (x < 5 ? 0 : null));
  assert.ok(t.p.pos[0] < 5.1 && t.p.target === null, 'stops at an edge with no ground ahead: x ' + t.p.pos[0]);
  t = go(x => (x < 5 ? 0 : -20));
  assert.ok(t.p.pos[0] < 5.1 && t.r.jumped === false, 'stops at a 20 ft drop');
  t = go(x => (x < 5 ? 0 : 4));
  assert.ok(t.p.pos[0] < 5.1 && t.r.jumped === false, 'stops at a 4 ft ledge (jumpUp is ' + core.PET.jumpUp + ')');
});

test('petStep: a jump is an arc that peaks above the landing height and lands on the ground height', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [10, 0, 0]);
  let peak = -Infinity, sawJump = false;
  const env = { ground: x => (x < 5 ? 0 : 2) };
  for (let i = 0; i < 1500 && p.target; i++) { core.petStep(p, DT, env); if (p.jump) { sawJump = true; peak = Math.max(peak, p.pos[2]); } }
  assert.ok(sawJump && peak > 2, 'peaks above the landing height: ' + peak);
  assert.equal(p.pos[2], 2);
});

test('petStep: a bigger pet (scale) moves faster', () => {
  const small = core.petGo(core.createPet([0, 0, 0], 1), [30, 0, 0], { rand: () => 0 });
  const big = core.petGo(core.createPet([0, 0, 0], 3), [30, 0, 0], { rand: () => 0 });
  const t1 = walkTo(small, flat).n, t3 = walkTo(big, flat).n;
  assert.ok(t3 < t1 / 2, 'scale 3 arrives in well under half the time: ' + t1 + ' vs ' + t3);
});

test('petScale grows with the model and is clamped', () => {
  const b = ext => ({ min: [0, 0, 0], max: [ext, 10, 10] });
  assert.equal(core.petScale(b(20)), 1);
  assert.ok(Math.abs(core.petScale(b(100)) - 2) < 1e-9);
  assert.equal(core.petScale(b(1e6)), core.PET.maxScale);
});

test('gait: a four-beat lateral sequence, with exactly three feet on the ground at any time', () => {
  const stance = u => ((u % 1) + 1) % 1 < core.GAIT.stance;
  for (let i = 0; i < 400; i++) {
    const ph = 0.013 + i * 0.0157; // avoids the exact boundaries
    const on = ['hl', 'fl', 'hr', 'fr'].filter(k => stance((ph + core.GAIT[k]) / (2 * Math.PI))).length;
    assert.equal(on, 3, 'three feet stand at phase ' + ph);
  }
  // footfall order: a foot lands when phase + offset reaches a whole cycle
  const landAt = k => (((2 * Math.PI - core.GAIT[k]) % (2 * Math.PI)) + 2 * Math.PI) % (2 * Math.PI);
  const order = ['hl', 'fl', 'hr', 'fr'].sort((a, b) => landAt(a) - landAt(b));
  assert.deepEqual(order, ['hl', 'fl', 'hr', 'fr'], 'hind left, fore left, hind right, fore right');
});

test('legCycle: the foot goes back on the ground in stance, and the knee bends to lift it in swing', () => {
  const stanceMid = core.legCycle(2 * Math.PI * core.GAIT.stance * 0.5);
  const swingMid = core.legCycle(2 * Math.PI * (core.GAIT.stance + (1 - core.GAIT.stance) / 2));
  assert.ok(Math.abs(stanceMid.hip) < 1e-9 && stanceMid.knee < 0.2, 'mid-stance: leg vertical, nearly straight');
  assert.ok(swingMid.knee > 0.8, 'mid-swing: knee bent to lift the foot: ' + swingMid.knee);
  assert.ok(core.legCycle(0).hip < 0 && core.legCycle(2 * Math.PI * 0.74).hip > 0, 'stance moves the foot from front to back');
});

test('petPose: still when idle; the legs swing while walking; the run bounds; a jump reaches forward', () => {
  const idle = core.petPose({ moving: false });
  for (const k of ['flHip', 'frHip', 'hlHip', 'hrHip', 'flKnee', 'frKnee', 'hlKnee', 'hrKnee', 'bob', 'roll', 'pitch', 'drop']) assert.equal(idle[k], 0, k);
  assert.equal(idle.eyes, 1, 'eyes open');
  const w = core.petPose({ moving: true, phase: 1.1 });
  assert.ok(Math.abs(w.flHip) + Math.abs(w.hlHip) > 0.1 && w.bob >= 0, 'legs swing and the body bobs while walking');
  const run = core.petPose({ moving: true, run: true, phase: 1.1 });
  assert.equal(run.flHip, run.frHip, 'a bound: the two front legs move together');
  assert.equal(run.hlHip, run.hrHip, 'and the two hind legs together');
  assert.ok(run.tailPitch < -0.5 && run.earBack > 0.5, 'tail streams back, ears go back');
  const air = core.petPose({ moving: true, jump: { t: 0.5 }, phase: 0 });
  assert.ok(air.flHip < -0.5 && air.hlHip > 0.5, 'in the air: front legs forward, hind legs back');
});

test('idle: stand, then sit, then an action, then sit again; a long rest ends in sleep; a click wakes it', () => {
  const p = core.createPet([0, 0, 0]); const seen = [];
  const rand = () => 0.99; // always the last action (yawn)
  const run = seconds => {
    for (let i = 0; i < Math.round(seconds / DT); i++) { core.petStep(p, DT, { ...flat, rand }); if (seen[seen.length - 1] !== p.act) seen.push(p.act); }
  };
  run(1.4); assert.equal(p.act, 'stand', 'still standing before sitAfter');
  run(0.3); assert.equal(p.act, 'sit', 'sits after about ' + core.PET.sitAfter + ' s');
  run(1.1); assert.equal(p.act, 'yawn', 'then an action (rand picks the last: yawn)');
  run(core.PET_ACTS.yawn + 0.1); assert.equal(p.act, 'sit', 'back to sitting when the action ends');
  run(core.PET.sleepAfter + 3); assert.equal(p.act, 'sleep', 'sleeps after a long rest');
  run(20); assert.equal(p.act, 'sleep', 'and stays asleep');
  core.petGo(p, [3, 0, 0]);
  assert.equal(p.act, 'stand', 'a new target wakes it'); assert.equal(p.idleT, 0);
  assert.ok(seen.includes('sit') && seen.includes('yawn') && seen.includes('sleep'));
});

test('idle: the redraw rate is low when the cat is still, and the three actions are lick, groom and yawn', () => {
  assert.equal(core.petFps({ moving: true }), 60);
  assert.equal(core.petFps({ moving: false, jump: { t: 0 } }), 60);
  assert.equal(core.petFps({ moving: false, act: 'lick' }), 30);
  assert.equal(core.petFps({ moving: false, act: 'sleep' }), 8);
  assert.equal(core.petFps({ moving: false, act: 'sit' }), 8);
  assert.deepEqual([...core.PET_IDLE].sort(), ['groom', 'lick', 'yawn'], 'the random idle actions');
  assert.ok(core.PET_ACTS.meow > 0 && !core.PET_IDLE.includes('meow'), 'meow is an action, but never a random one');
});

test('createPetModel + petApply: a group with the named parts, placed and turned', () => {
  const m = core.createPetModel(THREE);
  for (const k of ['legL', 'legR', 'hindL', 'hindR', 'earL', 'earR', 'tail', 'shadow', 'head', 'mouth', 'tongue']) assert.ok(m.parts[k], 'part ' + k);
  assert.equal(m.parts.eyes.length, 2, 'two eyes');
  for (const k of ['legL', 'legR', 'hindL', 'hindR']) assert.ok(m.parts[k].userData.knee, k + ' has a knee');
  const p = core.createPet([3, 4, 5], 2); p.heading = Math.PI / 2;
  core.petApply(m, p);
  assert.deepEqual(m.group.position.toArray(), [3, 4, 5]);
  assert.ok(Math.abs(m.group.rotation.z - Math.PI / 2) < 1e-12);
  assert.equal(m.group.scale.x, 2);
  const box = new THREE.Box3().setFromObject(m.group);
  assert.ok(box.max.z - box.min.z > 1.5 && box.max.z - box.min.z < 3, 'about 1 ft x scale 2 tall: ' + (box.max.z - box.min.z));
});

test('petApply: a sleeping cat lies low with its eyes closed; a yawning cat shows its mouth; the legs are at four points of the cycle', () => {
  const m = core.createPetModel(THREE), p = core.createPet([0, 0, 0]);
  p.act = 'sleep'; p.ib = undefined; core.petApply(m, p); // no smoothed pose yet: use the sleep pose
  assert.ok(m.group.position.z < 0, 'the body drops: ' + m.group.position.z);
  assert.ok(m.parts.eyes.every(e => e.scale.z <= 0.0601), 'eyes closed');
  assert.equal(m.parts.mouth.visible, false);
  p.act = 'yawn'; p.actT = 1.3; p.ib = undefined; core.petApply(m, p);
  assert.equal(m.parts.mouth.visible, true, 'mouth open in the middle of a yawn');
  const w = core.createPet([0, 0, 0]); w.moving = true; w.phase = 1.1; w.w = 1; core.petApply(m, w);
  const hips = ['hindL', 'legL', 'hindR', 'legR'].map(k => m.parts[k].rotation.y.toFixed(3));
  assert.equal(new Set(hips).size, 4, 'the four legs are at four different points of the cycle');
});

// ---- Routing, stuck and playful behavior ----
// Floor everywhere, and walls as rectangles [x0, x1, y0, y1] that block any ray reaching into them.
const wallEnv = rects => {
  const inside = (x, y) => rects.some(([x0, x1, y0, y1]) => x > x0 && x < x1 && y > y0 && y < y1);
  return { ground: () => 0, inside, blocked: (x, y, z, ux, uy, len) => { for (let d = 0; d <= len; d += 0.05) if (inside(x + ux * d, y + uy * d)) return true; return false; } };
};
const calm = { rand: () => 0.99 }; // a walk, no hops, the last idle action

test('routing: a blocked straight line starts a route search, and the pet goes around the wall', () => {
  const env = { ...wallEnv([[9, 10, -5, 5]]), ...calm };           // a wall 1 ft thick and 10 ft long across the way
  const p = core.petGo(core.createPet([0, 0, 0]), [20, 0, 0], env);
  assert.ok(p.planning, 'a search starts, since the straight line is blocked');
  let maxY = 0, inWall = false, n = 0;
  while ((p.goal || p.target || p.planning) && n < 6000) {
    core.petStep(p, DT, env); n++;
    maxY = Math.max(maxY, Math.abs(p.pos[1]));
    if (env.inside(p.pos[0], p.pos[1])) inWall = true;
  }
  assert.ok(Math.hypot(p.pos[0] - 20, p.pos[1]) < 0.3, 'arrives at the goal: ' + p.pos.map(v => v.toFixed(2)));
  assert.ok(maxY > 5, 'went around the end of the wall: max |y| ' + maxY.toFixed(2));
  assert.equal(inWall, false, 'never inside the wall');
  assert.ok(!p.meowCount, 'not stuck, so no meow');
});

test('routing: a clear straight line needs no search', () => {
  const env = wallEnv([[9, 10, 6, 12]]); // a wall off to the side
  const p = core.petGo(core.createPet([0, 0, 0]), [20, 0, 0], env);
  assert.equal(p.planning, null);
  assert.deepEqual(p.target, [20, 0, 0]);
});

test('createPlanner: finds a route round a wall, and the route points are walkable, spaced and end at the goal', () => {
  const env = wallEnv([[9, 10, -5, 5]]);
  const planner = core.createPlanner([0, 0, 0], [20, 0, 0], 1, env);
  let r = { status: 'searching' }, calls = 0;
  while (r.status === 'searching' && calls < 200) { r = planner.step(core.PET.planNodes); calls++; }
  assert.equal(r.status, 'found');
  assert.deepEqual(r.path[r.path.length - 1], [20, 0, 0], 'ends at the goal');
  assert.ok(r.path.length >= 2 && r.path.length <= 8, 'string-pulled to a few points: ' + r.path.length);
  assert.ok(r.path.some(q => Math.abs(q[1]) > 5), 'a point beyond the end of the wall');
  assert.ok(calls > 1, 'the search takes several steps, so the page stays responsive');
});

test('stuck: a goal that cannot be reached makes it walk as near as it can, then meow three times, five seconds apart', () => {
  const env = { ...wallEnv([[9, 10, -100, 100]]), ...calm };         // a wall too long to go around
  const p = core.petGo(core.createPet([0, 0, 0]), [20, 0, 0], env);
  const calls = [];
  let last = 0;
  for (let i = 0; i < 40 * 60; i++) {
    core.petStep(p, DT, env);
    if ((p.meowCount || 0) !== last) { last = p.meowCount; calls.push(+(i * DT).toFixed(1)); }
  }
  assert.ok(p.pos[0] > 6 && p.pos[0] < 9, 'walked up to the wall: x ' + p.pos[0]);
  assert.equal(p.goal, null, 'it gave up');
  assert.equal(p.meowCount, 3, 'three calls in all');
  assert.ok(calls[1] - calls[0] > 5.5 && calls[1] - calls[0] < 8 && calls[2] - calls[1] > 5.5 && calls[2] - calls[1] < 8, 'calls about ' + core.PET.meowEvery + ' s after each call ends: ' + calls);
  assert.notEqual(p.act, 'meow', 'back to normal idle after the last call');
});

test('stuck: arriving where it was sent is not stuck, and a new target stops the calls', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [5, 0, 0], calm);
  walkTo(p, flat);
  assert.ok(!p.meowCount, 'no meow after a normal arrival');
  p.meowsLeft = 2; p.act = 'sit';
  core.petGo(p, [3, 0, 0], calm);
  assert.equal(p.meowsLeft, 0, 'a new target ends the waiting calls');
});

test('playful: now and then it hops forward on flat ground, but only when p.playful is set', () => {
  const trip = playful => {
    const p = core.petGo(core.createPet([0, 0, 0]), [40, 0, 0], calm); p.playful = playful;
    let jumped = false;
    for (let i = 0; i < 3000 && (p.goal || p.target); i++) { core.petStep(p, DT, { ground: () => 0, rand: () => 0 }); if (p.jump) jumped = true; }
    return { jumped, arrived: !p.goal && Math.hypot(p.pos[0] - 40, p.pos[1]) < 0.3 };
  };
  assert.deepEqual(trip(true), { jumped: true, arrived: true }, 'hops on the way and still arrives');
  assert.deepEqual(trip(false), { jumped: false, arrived: true }, 'no hops when not playful');
});

// ---- Petting and thoughts ----
test('petting: a click on the cat stops it, makes it purr, and counts; clicking again restarts the purr', () => {
  const p = core.petGo(core.createPet([0, 0, 0]), [20, 0, 0], calm);
  stepFor(p, 1);
  assert.ok(p.moving && p.target, 'on its way');
  core.petPetted(p);
  assert.equal(p.target, null); assert.equal(p.goal, null); assert.equal(p.act, 'purr'); assert.equal(p.petCount, 1);
  stepFor(p, 1.5, { ...flat, ...calm });
  assert.equal(p.act, 'purr', 'still purring');
  core.petPetted(p); assert.equal(p.petCount, 2); assert.equal(p.actT, 0, 'the purr restarts');
  stepFor(p, core.PET_ACTS.purr + 0.2, { ...flat, ...calm });
  assert.equal(p.act, 'sit', 'sits when the purr ends');
  assert.ok(core.petPose({ act: 'purr', actT: 1, moving: false }).eyes < 0.2, 'eyes shut in the purr pose');
});

test('petting: it wakes a sleeping cat, and stops the waiting calls of a stuck one', () => {
  const p = core.createPet([0, 0, 0]); p.act = 'sleep'; p.meowsLeft = 2;
  core.petPetted(p);
  assert.equal(p.act, 'purr'); assert.equal(p.meowsLeft, 0);
  stepFor(p, 1, { ...flat, ...calm });
  assert.ok(core.petPose(p).eyes < 0.2, 'eyes shut a moment after it is petted (the pose is smoothed in)');
});

test('thoughts: a thought of a kind shows for a few seconds, then goes; a new one has a new id', () => {
  const p = core.createPet([0, 0, 0]);
  core.petThink(p, 'idle', () => 0);
  assert.equal(p.thought.text, core.PET_THOUGHTS.idle[0]); assert.equal(p.thoughtId, 1);
  core.petThink(p, 'idle', () => 0.999); assert.equal(p.thought.text, core.PET_THOUGHTS.idle[core.PET_THOUGHTS.idle.length - 1]); assert.equal(p.thoughtId, 2);
  core.petThink(p, 'no-such-kind'); assert.equal(p.thoughtId, 2, 'an unknown kind changes nothing');
  stepFor(p, p.thought.dur - 0.5, { ...flat, ...calm });
  assert.ok(p.thought, 'still shown');
  stepFor(p, 1, { ...flat, ...calm });
  assert.equal(p.thought, null, 'gone after its time');
});

test('thoughts: every kind has some short lines, and the cat has a thought of its own now and then while idle', () => {
  for (const [kind, lines] of Object.entries(core.PET_THOUGHTS)) {
    assert.ok(lines.length >= 2, kind + ' has lines');
    for (const l of lines) assert.ok(l.length > 0 && l.length <= 40, kind + ': a short line: ' + l);
  }
  const p = core.createPet([0, 0, 0]); let saw = null;
  for (let i = 0; i < 12 * 60 && !saw; i++) { core.petStep(p, DT, { ...flat, ...calm }); if (p.thought) saw = p.thought.kind; }
  assert.ok(saw && core.PET_THOUGHTS[saw], 'a thought of a known kind within about 12 s: ' + saw);
  const sleeper = core.createPet([0, 0, 0]); sleeper.act = 'sleep'; sleeper.thinkIn = 0.01; core.petStep(sleeper, DT, { ...flat, ...calm });
  assert.equal(sleeper.thought.kind, 'sleep', 'a sleeper dreams');
});

test('thoughts: a run, a jump and an arrival can each bring a thought (by the draw), and a stuck cat thinks about the wall', () => {
  const run = core.petGo(core.createPet([0, 0, 0]), [30, 0, 0], { rand: () => 0 });
  assert.equal(run.thought.kind, 'run');
  const walk = core.petGo(core.createPet([0, 0, 0]), [30, 0, 0], { rand: () => 0.99 });
  assert.equal(walk.thought, undefined, 'a walk brings no thought of its own');
  const p = core.petGo(core.createPet([0, 0, 0]), [10, 0, 0], { rand: () => 0 }); p.thought = null;
  walkTo(p, { ground: x => (x < 5 ? 0 : 2), rand: () => 0 });
  assert.ok(p.thoughtId >= 2, 'it thought something on the way (a jump and an arrival), after the thought of the run');
  const stuck = core.createPet([0, 0, 0]); stuck.act = 'sit'; stuck.meowsLeft = 2; stuck.thinkIn = 0.01;
  core.petStep(stuck, DT, { ...flat, ...calm });
  assert.equal(stuck.thought.kind, 'stuck');
});
