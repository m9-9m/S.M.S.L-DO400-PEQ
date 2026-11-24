/* build_html.mjs - embed peq_core.js into the single-file app.
 *
 *   node build_html.mjs           regenerate do400_peq_webhid.html
 *   node build_html.mjs --check    verify the embedded copy is identical (no write)
 *
 * The generated HTML is what ships: it works from file://, from GitHub Pages or
 * from a static localhost server, with no build step at run time.
 */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const TPL = path.join(HERE, 'do400_peq_webhid.template.html');
const CORE = path.join(HERE, 'peq_core.js');
const OUT = path.join(HERE, 'do400_peq_webhid.html');

const RE = /(\/\* ==== PEQ_CORE_BEGIN[^\n]*\n)([\s\S]*?)(\/\* ==== PEQ_CORE_END ==== \*\/)/;

const tpl = fs.readFileSync(TPL, 'utf8');
const core = fs.readFileSync(CORE, 'utf8');
const m = tpl.match(RE);
if (!m) {
  console.error('template: PEQ_CORE markers not found');
  process.exit(2);
}
const expect = tpl.replace(RE, (_, a, __, c) => a + core + (core.endsWith('\n') ? '' : '\n') + c);

if (process.argv.includes('--check')) {
  if (!fs.existsSync(OUT)) {
    console.error('check: ' + path.basename(OUT) + ' does not exist (run node build_html.mjs)');
    process.exit(1);
  }
  const have = fs.readFileSync(OUT, 'utf8');
  if (have === expect) {
    console.log('sync OK: ' + path.basename(OUT) + ' == template + peq_core.js (' + have.length + ' bytes)');
    process.exit(0);
  }
  const a = have.split('\n'), b = expect.split('\n');
  let i = 0;
  while (i < a.length && i < b.length && a[i] === b[i]) i++;
  console.error('sync DIFF at line ' + (i + 1) + ':\n  html: ' + JSON.stringify((a[i] || '').slice(0, 100)) +
    '\n  want: ' + JSON.stringify((b[i] || '').slice(0, 100)));
  process.exit(1);
}

fs.writeFileSync(OUT, expect);
console.log('wrote ' + path.basename(OUT) + ' (' + expect.length + ' bytes, core ' + core.length + ' bytes)');
