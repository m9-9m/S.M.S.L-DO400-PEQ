import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const HERE = path.dirname(fileURLToPath(import.meta.url));
const require = createRequire(import.meta.url);
const C = require('./peq_core.js');

const html = fs.readFileSync(path.join(HERE, 'do400_peq_webhid.html'), 'utf8');
const markup = html.split('<script>')[0];
const realIds = new Set([...markup.matchAll(/\bid="([^"]+)"/g)].map((m) => m[1]));

if (realIds.has('mqaOn') || realIds.has('mqaPreamp') || markup.includes('MQA 2L')) {
  console.error('FAIL: MQA 2L UI still present in HTML markup');
  process.exit(1);
}

const blocks = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
function mkEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(), checked: false, value: '', textContent: '', innerHTML: '',
    placeholder: '', disabled: false, scrollTop: 0, style: {}, dataset: {}, children: [], handlers: {},
    classList: {
      _s: new Set(),
      add(c) { this._s.add(c); },
      remove(c) { this._s.delete(c); },
      toggle(c, f) { (f === undefined ? !this._s.has(c) : f) ? this._s.add(c) : this._s.delete(c); },
      contains(c) { return this._s.has(c); }
    },
    addEventListener(t, fn) { (el.handlers[t] = el.handlers[t] || []).push(fn); },
    removeEventListener() { },
    appendChild(c) { el.children.push(c); return c; },
    remove() { },
    click() { (el.handlers.click || []).forEach((f) => f({ target: el })); },
    querySelector() { return mkEl('div'); },
    querySelectorAll() { return []; },
    closest() { return el; },
    setPointerCapture() { },
    releasePointerCapture() { },
    getBoundingClientRect() { return { left: 0, top: 0, width: 1000, height: 342 }; },
    get firstElementChild() { return el._f || (el._f = mkEl('i')); }
  };
  Object.defineProperty(el, 'className', {
    get() { return [...el.classList._s].join(' '); },
    set(v) { el.classList._s = new Set(String(v).split(/\s+/).filter(Boolean)); }
  });
  return el;
}

const byId = new Map();
const doc = {
  readyState: 'complete',
  getElementById(id) {
    if (!realIds.has(id)) throw new Error('DANGLING getElementById: ' + id);
    if (!byId.has(id)) { const e = mkEl('div'); e.id = id; byId.set(id, e); }
    return byId.get(id);
  },
  createElement: mkEl,
  addEventListener() { },
  body: mkEl('body'),
  activeElement: null
};
const store = new Map();
const ls = {
  getItem: (k) => store.has(k) ? store.get(k) : null,
  setItem: (k, v) => store.set(k, String(v)),
  removeItem: (k) => store.delete(k)
};
const intervals = [];
const win = { PEQCore: C, isSecureContext: true, addEventListener() { }, confirm: () => true };
const loc = { protocol: 'http:', hostname: 'localhost', href: 'http://localhost:8136/do400_peq_webhid.html' };

new Function('window', 'document', 'navigator', 'localStorage', 'location',
  'requestAnimationFrame', 'setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', blocks[1])(
  win, doc, { language: 'ja-JP' }, ls, loc,
  (fn) => { fn(); return 1; }, setTimeout, clearTimeout,
  (fn) => { intervals.push(fn); return intervals.length; }, () => { }
);

const el = (id) => doc.getElementById(id);
const fire = async (id, t, p, evExtra) => {
  const x = el(id);
  if (p) Object.assign(x, p);
  const ev = Object.assign({ target: x, preventDefault() { } }, evExtra || {});
  for (const h of (x.handlers[t] || [])) await h.call(x, ev);
};

// 1. Advanced toggle defaults OFF, toggles body.adv
if (doc.body.classList.contains('adv')) throw new Error('adv should default off');
await fire('chkAdvanced', 'change', { checked: true });
if (!doc.body.classList.contains('adv')) throw new Error('adv should toggle on');
await fire('chkAdvanced', 'change', { checked: false });

// 2. Connect demo -> apply -> read -> dirty badge lifecycle
if (!el('applyResult').textContent.includes('未接続')) throw new Error('initial applyResult should show idle state');
await fire('btnDemo', 'click');
await new Promise((r) => setTimeout(r, 40));
if (el('applyResult').textContent.includes('未接続')) throw new Error('applyResult stuck on 未接続 after connect: ' + el('applyResult').textContent);
await fire('selPreset', 'change', { value: 'harman_bass' });
await fire('btnPreset', 'click');
if (el('dirtyBadge').classList.contains('hidden')) throw new Error('dirtyBadge should show after preset edit');
await fire('btnApply', 'click');
await new Promise((r) => setTimeout(r, 80));
if (!el('dirtyBadge').classList.contains('hidden')) throw new Error('dirtyBadge should clear after apply');
await fire('btnRead', 'click');
await new Promise((r) => setTimeout(r, 80));

// 3. Auto-apply live mode + preamp + PCM ON/OFF
await fire('chkAutoApply', 'change', { checked: true });
await fire('pcmPreamp', 'input', { value: '-3.0' });
await fire('pcmPreamp', 'change', { value: '-3.0' });
await new Promise((r) => setTimeout(r, 300));
if (!el('dirtyBadge').classList.contains('hidden')) throw new Error('autoApply should clear dirtyBadge');
await fire('pcmOn', 'change', { checked: false });
await fire('pcmOn', 'change', { checked: true });
await new Promise((r) => setTimeout(r, 80));

// 4. User profiles: save -> flat -> load -> delete
await fire('profileName', 'input', { value: 'HD800S' });
await fire('btnProfileSave', 'click');
if (!el('selProfile').innerHTML.includes('HD800S')) throw new Error('saved profile not in dropdown');
await fire('btnFlat', 'click');
await fire('selProfile', 'change', { value: 'HD800S' });
await fire('btnProfileLoad', 'click');
await fire('btnProfileDel', 'click');

// 5. Graph mouse-wheel Q adjustment on active point
await fire('selPreset', 'change', { value: 'harman_bass' });
await fire('btnPreset', 'click');
const fakePt = mkEl('circle');
fakePt.dataset = { i: '0' };
fakePt.closest = () => fakePt;
await fire('graph', 'wheel', null, { target: fakePt, deltaY: -100, clientX: 200, clientY: 100 });
if (!el('readout').textContent.includes('Q ')) throw new Error('wheel Q readout not updated: ' + el('readout').textContent);

// 6. Full JP/EN language toggle (static + dynamic suggest + preset + connText)
await fire('btnLang', 'click');
const enTitle = el('hGraph').textContent;
const enConn = el('connText').textContent;
const enSuggest = el('pcmSuggest').innerHTML;
const enApplyRes = el('applyResult').innerHTML || el('applyResult').textContent;
await fire('btnLang', 'click');
const jaTitle = el('hGraph').textContent;
const jaApplyRes = el('applyResult').innerHTML || el('applyResult').textContent;
if (enTitle !== 'Frequency Response' || jaTitle !== '周波数特性') {
  throw new Error('JP/EN heading toggle mismatch: ' + JSON.stringify({ enTitle, jaTitle }));
}
if (!enConn.includes('Demo')) throw new Error('EN connText not translated: ' + enConn);
if (!enSuggest.includes('Peak') && !enSuggest.includes('Headroom')) {
  throw new Error('EN suggest not translated: ' + enSuggest);
}
if (/[ぁ-んァ-ヶ一-龯]/.test(enApplyRes)) throw new Error('EN applyResult still contains Japanese: ' + enApplyRes);
if (!/[ぁ-んァ-ヶ一-龯]/.test(jaApplyRes)) throw new Error('JA applyResult missing Japanese: ' + jaApplyRes);

// 7. Sample-rate polling callback exists
if (intervals.length < 1) throw new Error('fs polling interval not registered');
await intervals[0]();

// 8. Extended APO formats: ParametricEQ (LSC/PK/HSC, 2-decimal preamp), GraphicEQ, CSV/Markdown
const paramEqSample = [
  'Preamp: -4.32 dB',
  'Filter 1: ON LSC Fc 105.0 Hz Gain -1.4 dB Q 0.70',
  'Filter 2: ON PK Fc 147.3 Hz Gain -3.8 dB Q 0.83',
  'Filter 3: ON PK Fc 690.3 Hz Gain 0.9 dB Q 1.57',
  'Filter 4: ON PK Fc 3363.5 Hz Gain 4.2 dB Q 1.20',
  'Filter 5: ON HSC Fc 10000.0 Hz Gain -2.0 dB Q 0.70'
].join('\n');
const parsedParam = C.parseApo(paramEqSample);
if (parsedParam.filters.length !== 5 || Math.abs(parsedParam.preampDb - (-4.32)) > 1e-6 || parsedParam.filters[1].f0 !== 147.3) {
  throw new Error('ParametricEQ parse failed: ' + JSON.stringify(parsedParam));
}
await fire('apoText', 'input', { value: paramEqSample });
await fire('btnApoParse', 'click');
if (!el('apoInfo').textContent.includes('5 filters') || !el('apoInfo').textContent.includes('-4.32 dB')) {
  throw new Error('ParametricEQ apply failed: ' + el('apoInfo').textContent);
}
if (el('pcmPreampVal').value !== '-4.32') {
  throw new Error('Preamp 2-decimal precision lost after import: ' + el('pcmPreampVal').value);
}

// Manual 2-decimal preamp input -> apply -> readback preserves 0.01 dB
await fire('pcmPreampVal', 'input', { value: '-2.75' });
await fire('pcmPreampVal', 'change', { value: '-2.75' });
await fire('btnApply', 'click');
await new Promise((r) => setTimeout(r, 80));
await fire('btnRead', 'click');
await new Promise((r) => setTimeout(r, 80));
if (el('pcmPreampVal').value !== '-2.75') {
  throw new Error('Preamp 2-decimal manual input/readback mismatch: ' + el('pcmPreampVal').value);
}

const graphicEqSample = 'GraphicEQ: 20 -2.0; 50 1.5; 100 3.0; 250 0.5; 1000 -1.5; 3000 2.5; 8000 -1.0; 16000 1.0';
const parsedGeq = C.parseApo(graphicEqSample);
if (parsedGeq.format !== 'graphiceq' || parsedGeq.filters.length !== 10) {
  throw new Error('GraphicEQ fit failed: ' + JSON.stringify(parsedGeq));
}

const csvSample = 'Preamp: -3.5 dB\n| Low-shelf | 105 Hz | +3.0 dB | 0.71 |\nPK, 3000, -2.5, 1.4';
const parsedCsv = C.parseApo(csvSample);
if (parsedCsv.filters.length !== 2 || parsedCsv.filters[0].type !== 'lowshelf' || parsedCsv.filters[1].f0 !== 3000) {
  throw new Error('CSV/Markdown parse failed: ' + JSON.stringify(parsedCsv));
}

console.log(`PASS: strict DOM smoke test (${realIds.size} real IDs, 0 dangling IDs, 0 MQA 2L UI, dirty+autoApply+profiles+wheelQ+fsPoll+adv+JP/EN+APO OK)`);
