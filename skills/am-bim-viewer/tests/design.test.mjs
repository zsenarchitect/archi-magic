// "Visual Research" design system in the viewer: verbatim token and theme
// blocks, value-only category colors, and the per-theme scene palette.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync, existsSync, mkdtempSync, rmSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { tmpdir } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { core } from './load-core.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../../..');
const lf = s => s.replace(/\r\n/g, '\n').replace(/\n+$/, '');
const TEMPLATE = lf(readFileSync(resolve(root, 'mods/am-bim-viewer/viewer/template.html'), 'utf8'));
const block = (text, start, end) => {
  const a = text.indexOf(start), b = text.indexOf(end);
  assert.ok(a >= 0 && b > a, 'markers ' + start + ' / ' + end + ' present');
  return text.slice(a, b + end.length);
};

test('template carries design/tokens.css verbatim between the am-tokens markers', () => {
  const tokens = lf(readFileSync(resolve(root, 'design/tokens.css'), 'utf8'));
  assert.equal(block(TEMPLATE, '/* am-tokens:start', '/* am-tokens:end */'), tokens);
});

test('template carries design/theme.js verbatim between the am-theme markers', t => {
  const themePath = resolve(root, 'design/theme.js');
  const inPage = block(TEMPLATE, '// am-theme:start', '// am-theme:end');
  assert.ok(inPage.includes('am-theme-change') && inPage.includes('window.amTheme'));
  if (!existsSync(themePath)) { t.diagnostic('design/theme.js not on this branch: drift check skipped'); return; }
  assert.equal(inPage, lf(readFileSync(themePath, 'utf8')));
});

test('page CSS has no hue, no extra fonts, no border-left', () => {
  const css = TEMPLATE.slice(TEMPLATE.indexOf('/* am-tokens:end */'), TEMPLATE.indexOf('</style>'));
  assert.ok(!/#[0-9a-f]{3,8}\b|rgba?\(/i.test(css), 'page CSS uses tokens only');
  assert.ok(!/border-left/i.test(TEMPLATE));
  assert.ok(!/Archivo|JetBrains/i.test(TEMPLATE));
  assert.ok(TEMPLATE.includes('family=IBM+Plex+Mono:wght@300;400;500'));
});

test('3D viewport is a flat clean field: no beam, vignette or gradient, and above the grain layer', () => {
  const css = TEMPLATE.slice(TEMPLATE.indexOf('/* am-tokens:end */'), TEMPLATE.indexOf('</style>'));
  const rules = [...css.matchAll(/(#viewport[^{]*)\{([^}]*)\}/g)].map(m => [m[1].trim(), m[2]]);
  const vp = rules.find(r => r[0] === '#viewport');
  assert.ok(vp, '#viewport rule present');
  for (const [sel, body] of rules) {
    assert.ok(!/gradient|--beam|--vignette|--grain|background-image|url\(/.test(body), sel + ' has no backdrop image: ' + body);
  }
  assert.match(vp[1], /background:\s*var\(--bg\);/);
  const grainZ = Number(/\.am-surface::after[^}]*z-index:\s*(\d+)/.exec(TEMPLATE)[1]);
  const vpZ = Number((/z-index:\s*(\d+)/.exec(vp[1]) || [])[1]);
  assert.ok(vpZ > grainZ, 'viewport z-index ' + vpZ + ' above grain ' + grainZ);
});

const TOKENS = {
  v0: '#0a0a0a', v1: '#1c1c1b', v2: '#3a3a38', v3: '#6b6b68', v4: '#9a9a96', v5: '#c9c9c4', v6: '#e7e7e3', v7: '#f4f4f1',
  ink: '#f4f4f1', ink2: '#9a9a96', line: 'rgba(244, 244, 241, .22)',
};

test('category colors are value steps v2..v6 of the token scale, never hue', () => {
  const hexOf = rgb => '#' + rgb.map(c => c.toString(16).padStart(2, '0')).join('');
  const scale = [2, 3, 4, 5, 6].map(k => TOKENS['v' + k]);
  const cats = ['Walls', 'Floors', 'Doors', 'Columns', 'Roofs', 'Windows', 'Furniture', 'Generic Models', 'Stairs'];
  const colors = core.categoryColors(cats.map(category => ({ category })));
  for (const [cat, rgb] of Object.entries(colors)) {
    assert.ok(scale.includes(hexOf(rgb)), cat + ' is ' + hexOf(rgb) + ', not a value step');
    assert.ok(Math.max(...rgb) - Math.min(...rgb) <= 6, cat + ' has no hue (the scale is a near-neutral warm gray)');
  }
  const steps = core.categoryValues(cats.map(category => ({ category })));
  assert.notEqual(steps.Walls, steps.Floors);
  assert.notEqual(steps.Walls, steps.Doors);
  for (const k of [2, 3, 4, 5, 6]) assert.equal(hexOf(core.VALUE_RGB[k]), TOKENS['v' + k], 'VALUE_RGB matches tokens.css v' + k);
});

test('scene palette: night selects paper with an ink outline, paper is the inverse', () => {
  const night = core.scenePalette('night', TOKENS);
  const paper = core.scenePalette('paper', { ...TOKENS, ink: '#0a0a0a', ink2: '#6b6b68', line: 'rgba(10, 10, 10, .14)' });
  assert.deepEqual([night.theme, night.selected, night.outline], ['night', TOKENS.v7, TOKENS.v0]);
  assert.deepEqual([paper.theme, paper.selected, paper.outline], ['paper', TOKENS.v0, TOKENS.v7]);
  assert.equal(night.grid, 'rgba(244, 244, 241, .22)');
  assert.equal(paper.grid, 'rgba(10, 10, 10, .14)');
  assert.deepEqual([night.gizmo, paper.gizmo], ['#9a9a96', '#6b6b68'], 'gizmo in --ink-2');
  assert.deepEqual(night.steps, { 2: TOKENS.v2, 3: TOKENS.v3, 4: TOKENS.v4, 5: TOKENS.v5, 6: TOKENS.v6 });
});

test('architectural visualization: explode, ghost context, and pins are present in template', () => {
  assert.ok(TEMPLATE.includes('id="explodeslider"'));
  assert.ok(TEMPLATE.includes('id="hud-explode"'));
  assert.ok(TEMPLATE.includes('id="hud-focus"'));
  assert.ok(TEMPLATE.includes('id="hud-peel"'));
  assert.ok(TEMPLATE.includes('id="hud-pin"'));
  assert.ok(TEMPLATE.includes('id="pin-svg"'));
  assert.ok(TEMPLATE.includes('id="pin-dom"'));
});


test('offline viewer build embeds the pinned Three.js module and removes network dependencies', () => {
  const work = mkdtempSync(resolve(tmpdir(), 'archi-offline-viewer-'));
  try {
    const build = resolve(here, '../scripts/build-viewer.mjs');
    const model = resolve(here, '../viewer/demo.json');
    const output = resolve(work, 'demo.viewer.html');
    execFileSync(process.execPath, [build, '--offline', model, output]);
    const page = readFileSync(output, 'utf8');
    const [, mapJson] = page.match(/<script type="importmap">([\s\S]*?)<\/script>/);
    const moduleUrl = JSON.parse(mapJson).imports.three;
    assert.match(moduleUrl, /^data:text\/javascript;base64,/);
    const module = Buffer.from(moduleUrl.split(',')[1], 'base64');
    assert.deepEqual(module, readFileSync(new URL(import.meta.resolve('three'))));
    assert.doesNotMatch(page, /<link[^>]*href="https?:/i);
    assert.match(page, /default-src 'none'/);
    assert.match(page, /MIT License/);

    execFileSync(process.execPath, [build, model, output]);
    const online = readFileSync(output, 'utf8');
    assert.match(online, /https:\/\/cdn\.jsdelivr\.net\/npm\/three@0\.160\.0/);
  } finally {
    rmSync(work, { recursive: true, force: true });
  }
});
