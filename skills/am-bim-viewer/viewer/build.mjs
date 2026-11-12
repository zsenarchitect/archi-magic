// Builds one self-contained viewer page from the template, the core module
// and a BIM JSON document. Pure string work: used by the mod's /bim-view
// command ($.fs) and by the skill's node script (fs).

export const SCHEMA = 'am-bim-viewer/1';

export function validateData(data) {
  const problems = [];
  if (!data || typeof data !== 'object') return ['The file is not a JSON object.'];
  if (!Array.isArray(data.elements)) problems.push('"elements" must be an array.');
  else {
    data.elements.forEach((el, i) => {
      const where = 'elements[' + i + ']';
      if (el.id === undefined) problems.push(where + ': no "id".');
      if (!el.mesh || !Array.isArray(el.mesh.vertices) || !Array.isArray(el.mesh.indices)) {
        problems.push(where + ': "mesh" needs "vertices" and "indices" arrays.');
      } else {
        if (el.mesh.vertices.length % 3 !== 0) problems.push(where + ': vertex count is not a multiple of 3.');
        if (el.mesh.indices.length % 3 !== 0) problems.push(where + ': index count is not a multiple of 3.');
        const n = el.mesh.vertices.length / 3;
        if (el.mesh.indices.some(k => k < 0 || k >= n)) problems.push(where + ': an index is out of range.');
      }
    });
  }
  return problems.slice(0, 20);
}

// The core is an ES module; inside the page it becomes plain declarations.
export function inlineCore(coreSource) {
  return coreSource.replace(/^export\s+/gm, '');
}

function jsonForScript(value) {
  // U+2028/2029 are legal in JS strings since ES2019; only '<' needs escaping.
  return JSON.stringify(value).replace(/</g, '\\u003c');
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' })[c]);
}

/** @param {{ template: string, core: string, data: any, title?: string }} options */
export function buildViewerHtml({ template, core, data, title }) {
  const problems = validateData(data);
  if (problems.length) throw new Error('Invalid BIM JSON: ' + problems.join(' '));
  const parts = template.split('/*__CORE__*/');
  if (parts.length !== 2) throw new Error('Template must hold exactly one /*__CORE__*/ marker.');
  const heading = escapeHtml(title || (data.source && data.source.title) || 'BIM viewer');
  const dataParts = (parts[0] + inlineCore(core) + parts[1]).split('/*__DATA__*/null');
  if (dataParts.length !== 2) throw new Error('Template must hold exactly one /*__DATA__*/null marker.');
  return dataParts.map(part => part.replaceAll('__TITLE__', () => heading)).join(jsonForScript(data));
}
