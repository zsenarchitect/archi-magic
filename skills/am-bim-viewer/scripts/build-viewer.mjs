// Builds a self-contained 3D viewer page from a BIM JSON file.
// Usage: node build-viewer.mjs [--offline] [in.json] [out.html]
//   in.json  default: the demo dataset
//   out.html default: next to in.json, same name + .viewer.html
import { readFileSync, writeFileSync } from 'node:fs';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { dirname, resolve } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
// The skill's own copy (scripts/sync-shared-files.ps1 writes it from mods/am-bim-viewer/viewer),
// so the installed skill never reads above its plugin folder.
const viewerDir = resolve(here, '../viewer');
const { buildViewerHtml } = await import(pathToFileURL(resolve(viewerDir, 'build.mjs')).href);

const args = process.argv.slice(2);
const offline = args[0] === '--offline';
if (offline) args.shift();

const input = resolve(args[0] || resolve(viewerDir, 'demo.json'));
const output = resolve(args[1] || input.replace(/\.json$/i, '') + '.viewer.html');
let template = readFileSync(resolve(viewerDir, 'template.html'), 'utf8');

if (offline) {
  const version = '0.160.0';
  const cdn = `https://cdn.jsdelivr.net/npm/three@${version}/build/three.module.js`;
  let three;
  try {
    three = new URL(import.meta.resolve('three'));
  } catch (error) {
    if (error.code !== 'ERR_MODULE_NOT_FOUND') throw error;
    throw new Error('Offline builds need the locked Three.js package. Run npm ci in am-bim-viewer.');
  }
  const installed = JSON.parse(readFileSync(new URL('../package.json', three), 'utf8'));
  if (installed.version !== version || !template.includes(cdn)) {
    throw new Error(`Offline builds need the same Three.js ${version} release as the viewer template.`);
  }
  const license = readFileSync(new URL('../LICENSE', three), 'utf8');
  const encoded = readFileSync(three).toString('base64');
  template = template
    .replace(cdn, 'data:text/javascript;base64,' + encoded)
    .replace(/<link[^>]+href="https:\/\/fonts\.(?:googleapis|gstatic)\.com[^"]*"[^>]*>\r?\n/g, '')
    .replace('<meta charset="utf-8">', `<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline' data:; style-src 'unsafe-inline'; img-src data: blob:">
<!--
Three.js ${version}
${license}
-->`);
}

const html = buildViewerHtml({
  template,
  core: readFileSync(resolve(viewerDir, 'viewer-core.mjs'), 'utf8'),
  data: JSON.parse(readFileSync(input, 'utf8')),
});
writeFileSync(output, html);
console.log(output);
