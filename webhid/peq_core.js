/* peq_core.js - DO400 WebHID PEQ pure-logic core (no DOM, no network).
 *
 * 100% offline: this file is only ever executed by the
 * browser UI and by the Node.js test harness.  Nothing here talks to hardware.
 *
 * Contains:
 *   1. RBJ biquad design + Q29 quantisation, bit-compatible with
 *      src/peq_ref.py (including Python's round-half-even).
 *   2. Cascade / per-band magnitude response from the *quantised* taps, using the
 *      exact firmware ratio  H(z) = (b0 + b1 z + b2 z^2) / (2^29 - na1 z - na2 z^2).
 *   3. PEQ1 protocol v1 request encoder / response decoder (64-byte reports,
 *      tolerant of Chrome's optional leading report-id byte).
 *   4. MockDevice: a complete offline firmware simulator for the same protocol.
 *   5. Equalizer APO / PEQ text import + export.
 *   6. Built-in presets (Flat / Reference 10-band / Harman Bass Boost / Vocal Clarity).
 *
 * UMD-ish: `module.exports` under Node, `globalThis.PEQCore` in the browser.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.PEQCore = api;
})(typeof globalThis !== 'undefined' ? globalThis : this, function () {
  'use strict';

  var Q29 = 536870912;                 /* 1 << 29 */
  var Q28 = 268435456;                 /* 1 << 28 */
  var PREAMP_MAX = 0x20000000;         /* +6.02 dB in Q28 */
  var PREAMP_UNITY = Q28;

  /* ------------------------------------------------------------------ utils */

  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  /* Python 3 `round()` semantics: nearest, ties to even.  Python's quantise()
   * does `int(round(v * q))`, so JS must round exactly the same way or a tie
   * (x.5) would differ by 1 LSB from the reference. */
  function pyRound(x) {
    if (!isFinite(x)) throw new RangeError('pyRound: non-finite value');
    var f = Math.floor(x);
    var d = x - f;
    if (d > 0.5) return f + 1;
    if (d < 0.5) return f;
    return (f % 2 === 0) ? f : f + 1;   /* exactly .5 -> to even */
  }

  function bytesToHex(u8) {
    var s = '';
    for (var i = 0; i < u8.length; i++) {
      s += (u8[i] < 16 ? '0' : '') + u8[i].toString(16).toUpperCase();
      if ((i & 3) === 3 && i !== u8.length - 1) s += ' ';
    }
    return s;
  }

  function u32le(u8, off) {
    return (u8[off] | (u8[off + 1] << 8) | (u8[off + 2] << 16) | (u8[off + 3] << 24)) >>> 0;
  }

  function i32le(u8, off) {
    var v = u32le(u8, off);
    return v > 0x7FFFFFFF ? v - 0x100000000 : v;
  }

  /* ------------------------------------------------------- biquad design (RBJ)
   * Mirrors peq_ref.py line for line, same operation order, so the incoming
   * doubles are identical and the quantised taps come out identical. */

  function rbjPeaking(fs, f0, gainDb, q) {
    var A = Math.pow(10, gainDb / 40.0);
    var w0 = 2 * Math.PI * f0 / fs;
    var alpha = Math.sin(w0) / (2 * q);
    var cw = Math.cos(w0);
    var b0 = 1 + alpha * A;
    var b1 = -2 * cw;
    var b2 = 1 - alpha * A;
    var a0 = 1 + alpha / A;
    var a1 = -2 * cw;
    var a2 = 1 - alpha / A;
    return [b0, b1, b2, a0, a1, a2];
  }

  function rbjLowShelf(fs, f0, gainDb, s) {
    var A = Math.pow(10, gainDb / 40.0);
    var w0 = 2 * Math.PI * f0 / fs;
    var alpha = Math.sin(w0) / 2 * Math.sqrt((A + 1 / A) * (1 / s - 1) + 2);
    var cw = Math.cos(w0);
    var sq = 2 * Math.sqrt(A) * alpha;
    var b0 = A * ((A + 1) - (A - 1) * cw + sq);
    var b1 = 2 * A * ((A - 1) - (A + 1) * cw);
    var b2 = A * ((A + 1) - (A - 1) * cw - sq);
    var a0 = (A + 1) + (A - 1) * cw + sq;
    var a1 = -2 * ((A - 1) + (A + 1) * cw);
    var a2 = (A + 1) + (A - 1) * cw - sq;
    return [b0, b1, b2, a0, a1, a2];
  }

  function rbjHighShelf(fs, f0, gainDb, s) {
    var A = Math.pow(10, gainDb / 40.0);
    var w0 = 2 * Math.PI * f0 / fs;
    var alpha = Math.sin(w0) / 2 * Math.sqrt((A + 1 / A) * (1 / s - 1) + 2);
    var cw = Math.cos(w0);
    var sq = 2 * Math.sqrt(A) * alpha;
    var b0 = A * ((A + 1) + (A - 1) * cw + sq);
    var b1 = -2 * A * ((A - 1) + (A + 1) * cw);
    var b2 = A * ((A + 1) + (A - 1) * cw - sq);
    var a0 = (A + 1) - (A - 1) * cw + sq;
    var a1 = 2 * ((A - 1) - (A + 1) * cw);
    var a2 = (A + 1) - (A - 1) * cw - sq;
    return [b0, b1, b2, a0, a1, a2];
  }

  function rbjLowPass(fs, f0, q) {
    var w0 = 2 * Math.PI * f0 / fs;
    var alpha = Math.sin(w0) / (2 * q);
    var cw = Math.cos(w0);
    var b1 = 1 - cw, b0 = b1 / 2, b2 = b0;
    var a0 = 1 + alpha, a1 = -2 * cw, a2 = 1 - alpha;
    return [b0, b1, b2, a0, a1, a2];
  }

  function rbjHighPass(fs, f0, q) {
    var w0 = 2 * Math.PI * f0 / fs;
    var alpha = Math.sin(w0) / (2 * q);
    var cw = Math.cos(w0);
    var b0 = (1 + cw) / 2, b1 = -(1 + cw), b2 = (1 + cw) / 2;
    var a0 = 1 + alpha, a1 = -2 * cw, a2 = 1 - alpha;
    return [b0, b1, b2, a0, a1, a2];
  }

  /* (b0,b1,b2,a0,a1,a2) -> firmware taps [b0,b1,b2,na1,na2] in Q`bits`.
   * na1 = -a1/a0, na2 = -a2/a0  (peq_ref.quantise) */
  function quantise(coefs, bits) {
    bits = bits || 29;
    var q = Math.pow(2, bits);
    return [
      pyRound(coefs[0] / coefs[3] * q),
      pyRound(coefs[1] / coefs[3] * q),
      pyRound(coefs[2] / coefs[3] * q),
      pyRound(-coefs[4] / coefs[3] * q),
      pyRound(-coefs[5] / coefs[3] * q)
    ];
  }

  function identity() { return [Q29, 0, 0, 0, 0]; }

  var TYPES = ['peaking', 'lowshelf', 'highshelf', 'lowpass', 'highpass', 'flat'];

  /* Shelves take the RBJ slope S; the UI exposes a Q-like value where
   * Q = 0.7071067811865476 (1/sqrt2) means S = 1.0 exactly - the value used by
   * peq_ref.pcm10_taps(), so the reference 10-band curve is reproduced bit for bit. */
  function shelfSlopeFromQ(q) {
    var s = q / Math.SQRT1_2;
    return clamp(s, 0.1, 4.0);
  }

  /* band = {type, f0, gainDb, q, s?} -> [b0,b1,b2,na1,na2] Q29 */
  function designTaps(band, fs, explicitS) {
    if (!band || band.type === 'flat') return identity();
    if (!(fs > 0) || !(band.f0 > 0) || band.f0 >= fs / 2) return identity();
    var s = (explicitS !== undefined) ? explicitS : shelfSlopeFromQ(band.q);
    var c;
    switch (band.type) {
      case 'peaking':  c = rbjPeaking(fs, band.f0, band.gainDb, band.q); break;
      case 'lowshelf': c = rbjLowShelf(fs, band.f0, band.gainDb, s); break;
      case 'highshelf': c = rbjHighShelf(fs, band.f0, band.gainDb, s); break;
      case 'lowpass':  c = rbjLowPass(fs, band.f0, band.q); break;
      case 'highpass': c = rbjHighPass(fs, band.f0, band.q); break;
      default: return identity();
    }
    return quantise(c, 29);
  }

  /* ------------------------------------------------------------ response ---- */

  function logFreqGrid(n, fmin, fmax) {
    fmin = fmin || 20;
    fmax = fmax || 20000;
    var out = new Array(n);
    var lo = Math.log(fmin), hi = Math.log(fmax);
    for (var i = 0; i < n; i++) out[i] = Math.exp(lo + (hi - lo) * i / (n - 1));
    return out;
  }

  /* |H(e^{jw})| in dB for one section, exactly the firmware transfer function
   * with H = (b0 + b1 z + b2 z^2) / (2^29 - na1 z - na2 z^2). */
  function sectionResponseDb(taps, freqs, fs) {
    var b0 = taps[0], b1 = taps[1], b2 = taps[2], na1 = taps[3], na2 = taps[4];
    var out = new Array(freqs.length);
    for (var k = 0; k < freqs.length; k++) {
      var th = -2 * Math.PI * freqs[k] / fs;
      var zr = Math.cos(th), zi = Math.sin(th);
      var z2r = zr * zr - zi * zi, z2i = 2 * zr * zi;
      var nr = b0 + b1 * zr + b2 * z2r;
      var ni = b1 * zi + b2 * z2i;
      var dr = Q29 - na1 * zr - na2 * z2r;
      var di = -(na1 * zi + na2 * z2i);
      var den = dr * dr + di * di;
      var hr = (nr * dr + ni * di) / den;
      var hi = (ni * dr - nr * di) / den;
      out[k] = 10 * Math.log10(hr * hr + hi * hi);
    }
    return out;
  }

  /* Sum of dB of many sections (cascade). */
  function cascadeResponseDb(tapsList, freqs, fs) {
    var out = new Array(freqs.length).fill(0);
    for (var i = 0; i < tapsList.length; i++) {
      var r = sectionResponseDb(tapsList[i], freqs, fs);
      for (var k = 0; k < out.length; k++) out[k] += r[k];
    }
    return out;
  }

  function activeTaps(bands, fs) {
    var t = [];
    for (var i = 0; i < bands.length; i++) {
      if (!bands[i].on || bands[i].type === 'flat') continue;
      t.push(designTaps(bands[i], fs));
    }
    return t;
  }

  function totalResponseDb(bands, fs, freqs, preampDb) {
    var r = cascadeResponseDb(activeTaps(bands, fs), freqs, fs);
    var g = preampDb || 0;
    if (g) for (var k = 0; k < r.length; k++) r[k] += g;
    return r;
  }

  function peakDb(bands, fs, preampDb) {
    var f = logFreqGrid(1400, 20, 20000);
    var r = totalResponseDb(bands, fs, f, preampDb || 0);
    var m = -Infinity;
    for (var i = 0; i < r.length; i++) if (r[i] > m) m = r[i];
    return m;
  }

  /* Headroom suggestion: enough preamp so the largest peak lands just under 0 dB. */
  function suggestPreampDb(bands, fs, headroomDb) {
    headroomDb = (headroomDb === undefined) ? 0.2 : headroomDb;
    var p = peakDb(bands, fs, 0);
    if (!(p > 0)) return { peakDb: p, suggestedDb: 0, needed: false };
    var s = -Math.round((p + headroomDb) * 2) / 2;   /* 0.5 dB steps */
    return { peakDb: p, suggestedDb: clamp(s, -12, 0), needed: true };
  }

  /* ------------------------------------------------------------ presets ---- */

  function mkBand(type, f0, gainDb, q) {
    return {
      on: true, type: type, f0: f0, gainDb: gainDb,
      q: (q === undefined) ? (type === 'lowshelf' || type === 'highshelf' ? Math.SQRT1_2 : 1.0) : q
    };
  }

  function flatBands(n) {
    var out = [];
    for (var i = 0; i < n; i++) out.push({ on: false, type: 'peaking', f0: 1000, gainDb: 0, q: 1.0 });
    return out;
  }

  /* Reference 10-band test curve matching src/peq_ref.py pcm10_taps(). */
  var REF10_BANDS = [
    function (fs) { return mkBand('lowshelf', 80, 6.0, Math.SQRT1_2); },
    function () { return mkBand('peaking', 250, 4.0, 1.0); },
    function () { return mkBand('peaking', 700, -6.0, 1.2); },
    function () { return mkBand('peaking', 1500, 3.0, 1.4); },
    function () { return mkBand('peaking', 3000, -4.0, 1.8); },
    function () { return mkBand('peaking', 5000, 2.0, 2.0); },
    function () { return mkBand('highshelf', 8000, 4.0, Math.SQRT1_2); },
    function () { return mkBand('peaking', 11000, -3.0, 2.0); },
    function () { return mkBand('peaking', 14000, 2.5, 2.0); },
    function () { return mkBand('peaking', 18000, 3.0, 1.5); }
  ];

  function bandsFromSpec(spec) {
    var out = [];
    for (var i = 0; i < spec.length; i++) {
      var b = spec[i];
      out.push({ on: true, type: b[0], f0: b[1], gainDb: b[2], q: b[3] });
    }
    return out;
  }

  var PRESETS = [
    {
      id: 'flat',
      name: 'Flat (素通し)',
      note: '全バンド無効。PEQ を入れる前の基準。',
      build: function (n) { return flatBands(n); }
    },
    {
      id: 'ref10',
      name: 'テスト用 10 バンドカーブ',
      note: 'peq_ref.pcm10_taps() と同一パラメータ(80Hz LS+6 / 700Hz -6 / 8kHz HS+4 …)。',
      build: function (n) {
        var b = bandsFromSpec([
          ['lowshelf', 80, 6.0, Math.SQRT1_2],
          ['peaking', 250, 4.0, 1.0],
          ['peaking', 700, -6.0, 1.2],
          ['peaking', 1500, 3.0, 1.4],
          ['peaking', 3000, -4.0, 1.8],
          ['peaking', 5000, 2.0, 2.0],
          ['highshelf', 8000, 4.0, Math.SQRT1_2],
          ['peaking', 11000, -3.0, 2.0],
          ['peaking', 14000, 2.5, 2.0],
          ['peaking', 18000, 3.0, 1.5]
        ]);
        while (b.length < n) b.push({ on: false, type: 'peaking', f0: 1000, gainDb: 0, q: 1.0 });
        return b.slice(0, n);
      }
    },
    {
      id: 'harman_bass',
      name: 'Harman Bass Boost',
      note: '低域シェルフ + サブベース。プリアンプ -5 dB 推奨。',
      preampDb: -5,
      build: function (n) {
        var b = bandsFromSpec([
          ['lowshelf', 105, 5.5, Math.SQRT1_2],
          ['peaking', 45, 2.0, 1.0],
          ['peaking', 180, -1.5, 1.0],
          ['peaking', 900, -0.5, 1.2],
          ['peaking', 3000, -1.0, 2.0],
          ['highshelf', 10000, 1.5, Math.SQRT1_2]
        ]);
        while (b.length < n) b.push({ on: false, type: 'peaking', f0: 1000, gainDb: 0, q: 1.0 });
        return b.slice(0, n);
      }
    },
    {
      id: 'vocal',
      name: 'Vocal Clarity',
      note: '80Hz 以下を落とし 2-4kHz を持ち上げる。',
      preampDb: -3,
      build: function (n) {
        var b = bandsFromSpec([
          ['highpass', 70, 0, 0.7071067811865476],
          ['peaking', 250, -2.0, 1.0],
          ['peaking', 1200, 1.5, 1.2],
          ['peaking', 3000, 3.0, 1.4],
          ['peaking', 6500, 1.5, 2.0],
          ['highshelf', 12000, 1.0, Math.SQRT1_2]
        ]);
        while (b.length < n) b.push({ on: false, type: 'peaking', f0: 1000, gainDb: 0, q: 1.0 });
        return b.slice(0, n);
      }
    }
  ];

  function presetById(id) {
    for (var i = 0; i < PRESETS.length; i++) if (PRESETS[i].id === id) return PRESETS[i];
    return null;
  }

  /* ---------------------------------------------------------- protocol v1 --- */

  var MAGIC = 0x31514550;              /* "PEQ1" */
  var PROTO_VER = 1;

  var CMD = {
    GET_STATUS: 0x01,
    SET_ENABLE: 0x02,
    SET_BAND: 0x03,
    GET_BAND: 0x04,
    CLEAR_HISTORY: 0x05,
    SET_PREAMP: 0x06
  };

  var CMD_NAME = {
    0x01: 'GET_STATUS', 0x02: 'SET_ENABLE', 0x03: 'SET_BAND',
    0x04: 'GET_BAND', 0x05: 'CLEAR_HISTORY', 0x06: 'SET_PREAMP'
  };

  var STATUS = { OK: 0, ERR_PARAM: 1, ERR_CHECKSUM: 2, ERR_CMD: 3 };
  var STATUS_NAME = { 0: 'OK', 1: 'ERR_PARAM', 2: 'ERR_CHECKSUM', 3: 'ERR_CMD' };

  var FW_FLAG = { HAS_DOP_GUARD: 0x01, HAS_DFU_512K: 0x02, DOP_DETECTED_NOW: 0x04 };

  var BAND_COUNT = [10, 12];           /* target 0 = PCM, 1 = MQA 2L */

  var ENABLE_FLAG = {
    SET_PCM_ON: 0x01, SET_MQA_ON: 0x02,
    SET_PCM_MASK: 0x04, SET_MQA_MASK: 0x08
  };

  /* checksum over u32_le words[0..14] (bytes 0..59) XOR 0x5A5A5A5A */
  function checksum(u8) {
    var s = 0;
    for (var i = 0; i < 15; i++) s = (s + u32le(u8, i * 4)) >>> 0;
    return (s ^ 0x5A5A5A5A) >>> 0;
  }

  function dbToPreampQ28(db) {
    var v = Math.round(Math.pow(10, db / 20) * Q28);
    return clamp(v, 0, PREAMP_MAX);
  }

  function preampQ28ToDb(q) {
    if (q <= 0) return -Infinity;
    return 20 * Math.log10(q / Q28);
  }

  /* spec: {cmd, target, channel, band, seq, ...} -> Uint8Array(64) */
  function encodeRequest(spec) {
    var b = new Uint8Array(64);
    var dv = new DataView(b.buffer);
    var cmd = spec.cmd | 0;
    dv.setUint32(0, MAGIC, true);
    b[4] = cmd & 0xFF;
    b[5] = (spec.target | 0) & 0xFF;
    b[6] = (spec.channel | 0) & 0xFF;
    b[7] = (spec.band | 0) & 0xFF;
    dv.setUint32(8, (spec.seq >>> 0), true);

    switch (cmd) {
      case CMD.SET_ENABLE:
        b[12] = (spec.flags | 0) & 0xFF;
        dv.setUint32(16, spec.pcmOn ? 1 : 0, true);
        dv.setUint32(20, spec.mqaOn ? 1 : 0, true);
        dv.setUint32(24, (spec.pcmMask | 0) & 0x3FF, true);
        dv.setUint32(28, (spec.mqaMask | 0) & 0xFFF, true);
        break;
      case CMD.SET_BAND:
        var t = spec.taps || identity();
        for (var i = 0; i < 5; i++) dv.setInt32(12 + 4 * i, t[i] | 0, true);
        b[32] = (spec.opts | 0) & 0xFF;
        break;
      case CMD.SET_PREAMP:
        dv.setUint32(12, clamp(spec.preampQ28 >>> 0, 0, PREAMP_MAX), true);
        break;
      default:
        /* GET_STATUS / GET_BAND / CLEAR_HISTORY: payload all zero */
        break;
    }
    dv.setUint32(60, checksum(b), true);
    return b;
  }

  /* Chrome/Edge may hand back 64 bytes or 65 bytes with a leading report id 0x00. */
  function normaliseReport(input) {
    var u8;
    if (input instanceof ArrayBuffer) u8 = new Uint8Array(input);
    else if (ArrayBuffer.isView(input)) u8 = new Uint8Array(input.buffer, input.byteOffset, input.byteLength);
    else throw new TypeError('normaliseReport: expected ArrayBuffer or view');
    if (u8.length === 65) {
      if (u8[0] === 0x00) return u8.subarray(1);
      throw new RangeError('report has 65 bytes but leading id is 0x' + u8[0].toString(16) + ', expected 0x00');
    }
    if (u8.length !== 64) throw new RangeError('report length ' + u8.length + ', expected 64 or 65');
    return u8;
  }

  function decodeResponse(input) {
    var u8 = normaliseReport(input);
    var dv = new DataView(u8.buffer, u8.byteOffset, u8.byteLength);
    var magic = dv.getUint32(0, true);
    var stored = dv.getUint32(60, true);
    var calc = checksum(u8);
    var r = {
      len: u8.length,
      raw: u8,
      magic: magic,
      magicOk: magic === MAGIC,
      cmd: u8[4],
      cmdName: CMD_NAME[u8[4]] || ('CMD_0x' + u8[4].toString(16)),
      status: u8[5],
      statusName: STATUS_NAME[u8[5]] || ('STATUS_' + u8[5]),
      protoVer: u8[6],
      fwFlags: u8[7],
      seq: dv.getUint32(8, true),
      checksum: stored,
      checksumCalc: calc,
      checksumOk: stored === calc
    };
    if (r.cmd === CMD.SET_BAND || r.cmd === CMD.GET_BAND) {
      var taps = [0, 1, 2, 3, 4].map(function (i) { return dv.getInt32(12 + 4 * i, true); });
      var tag32 = dv.getUint32(32, true);
      r.band = {
        taps: taps,
        tagByte: u8[32],
        tag32: tag32,
        target: (tag32 >>> 16) & 0xFF,
        channel: (tag32 >>> 8) & 0xFF,
        bandIndex: tag32 & 0xFF
      };
    } else {
      r.statusPayload = {
        pcmOn: dv.getUint32(12, true),
        mqaOn: dv.getUint32(16, true),
        pcmMask: dv.getUint32(20, true),
        mqaMask: dv.getUint32(24, true),
        sampleRate: dv.getUint32(28, true),
        pcmPreampQ28: dv.getUint32(32, true),
        mqaPreampQ28: dv.getUint32(36, true),
        pcmSeq: dv.getUint32(40, true),
        mqaSeq: dv.getUint32(44, true),
        dopHits: dv.getUint32(48, true),
        occCur: dv.getUint32(52, true),
        fwBuildId: dv.getUint32(56, true)
      };
    }
    return r;
  }

  /* ---------------------------------------------------------- mock device -- */

  /* Faithful offline model of the PEQ1 v1 firmware behaviour.  Used by the
   * simulator UI and by the automated tests (no hardware, no browser). */
  function MockDevice(opts) {
    opts = opts || {};
    this.reportIdPrefix = (opts.reportIdPrefix === undefined) ? true : !!opts.reportIdPrefix;
    this.sampleRate = opts.sampleRate || 44100;
    this.fwBuildId = (opts.fwBuildId === undefined) ? 0x38000101 : opts.fwBuildId;
    this.dopHits = 0;
    this.dopDetectedNow = !!opts.dopDetectedNow;
    this.occCur = 0;
    this.fwFlags = FW_FLAG.HAS_DOP_GUARD | FW_FLAG.HAS_DFU_512K;
    this.txCount = 0;
    this.rxCount = 0;
    this.lastError = null;
    this.state = {};
    for (var t = 0; t < 2; t++) {
      var n = BAND_COUNT[t];
      var bands = [[], []];
      for (var ch = 0; ch < 2; ch++)
        for (var i = 0; i < n; i++) bands[ch].push(identity());
      this.state[t] = {
        on: 0,                           /* safe boot default: PEQ OFF (bypass) */
        mask: (t === 0) ? 0x3FF : 0xFFF,
        preampQ28: PREAMP_UNITY,
        seq: 0,
        bands: bands
      };
    }
    this._pending = null;
  }

  MockDevice.prototype._mkResponse = function (req, status, fill) {
    var b = new Uint8Array(64);
    var dv = new DataView(b.buffer);
    dv.setUint32(0, MAGIC, true);
    b[4] = req ? req[4] : 0;
    b[5] = status;
    b[6] = PROTO_VER;
    b[7] = this.fwFlags | (this.dopDetectedNow ? FW_FLAG.DOP_DETECTED_NOW : 0);
    dv.setUint32(8, req ? u32le(req, 8) : 0, true);
    if (fill) fill(b, dv);
    dv.setUint32(60, checksum(b), true);
    return b;
  };

  MockDevice.prototype._statusPayload = function (b, dv) {
    dv.setUint32(12, this.state[0].on ? 1 : 0, true);
    dv.setUint32(16, this.state[1].on ? 1 : 0, true);
    dv.setUint32(20, this.state[0].mask >>> 0, true);
    dv.setUint32(24, this.state[1].mask >>> 0, true);
    dv.setUint32(28, this.sampleRate >>> 0, true);
    dv.setUint32(32, this.state[0].preampQ28 >>> 0, true);
    dv.setUint32(36, this.state[1].preampQ28 >>> 0, true);
    dv.setUint32(40, this.state[0].seq >>> 0, true);
    dv.setUint32(44, this.state[1].seq >>> 0, true);
    dv.setUint32(48, this.dopHits >>> 0, true);
    dv.setUint32(52, this.occCur >>> 0, true);
    dv.setUint32(56, this.fwBuildId >>> 0, true);
  };

  /* Process one 64-byte request, return the 64-byte response. */
  MockDevice.prototype.handle = function (request) {
    var req = normaliseReport(request);
    if (req.length !== 64) throw new RangeError('mock: request must be 64 bytes');
    this.txCount++;
    this.lastError = null;

    if (u32le(req, 0) !== MAGIC) {
      this.lastError = 'magic';
      return this._mkResponse(req, STATUS.ERR_PARAM);
    }
    if (u32le(req, 60) !== checksum(req)) {
      this.lastError = 'checksum';
      return this._mkResponse(req, STATUS.ERR_CHECKSUM);
    }

    var cmd = req[4];
    var target = req[5];
    var channel = req[6];
    var band = req[7];

    if (target > 1) { this.lastError = 'target'; return this._mkResponse(req, STATUS.ERR_PARAM); }
    var nBands = BAND_COUNT[target];
    var self = this;

    switch (cmd) {
      case CMD.GET_STATUS:
        return this._mkResponse(req, STATUS.OK, function (b, dv) { self._statusPayload(b, dv); });

      case CMD.SET_ENABLE:
        var flags = req[12];
        var pcmOn = u32le(req, 16), mqaOn = u32le(req, 20);
        var pcmMask = u32le(req, 24), mqaMask = u32le(req, 28);
        if (pcmOn > 1 || mqaOn > 1) { this.lastError = 'on'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        if (pcmMask > 0x3FF || mqaMask > 0xFFF) { this.lastError = 'mask'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        if (flags & ENABLE_FLAG.SET_PCM_ON) this.state[0].on = pcmOn;
        if (flags & ENABLE_FLAG.SET_MQA_ON) this.state[1].on = mqaOn;
        if (flags & ENABLE_FLAG.SET_PCM_MASK) this.state[0].mask = pcmMask;
        if (flags & ENABLE_FLAG.SET_MQA_MASK) this.state[1].mask = mqaMask;
        this.state[0].seq++; this.state[1].seq++;
        return this._mkResponse(req, STATUS.OK, function (b, dv) { self._statusPayload(b, dv); });

      case CMD.SET_BAND:
      case CMD.GET_BAND:
        if (band >= nBands) { this.lastError = 'band'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        if (cmd === CMD.GET_BAND && channel > 1) { this.lastError = 'channel'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        if (channel > 2) { this.lastError = 'channel'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        var taps = [0, 1, 2, 3, 4].map(function (i) { return i32le(req, 12 + 4 * i); });
        if (cmd === CMD.SET_BAND) {
          if (channel === 2) { this.state[target].bands[0][band] = taps.slice(); this.state[target].bands[1][band] = taps.slice(); }
          else this.state[target].bands[channel][band] = taps.slice();
          this.state[target].seq++;
        }
        var tag = (target << 16) | (channel << 8) | band;
        return this._mkResponse(req, STATUS.OK, function (b, dv) {
          var src = (channel === 2) ? self.state[target].bands[0][band] : self.state[target].bands[channel][band];
          for (var i = 0; i < 5; i++) dv.setInt32(12 + 4 * i, src[i] | 0, true);
          dv.setUint32(32, tag >>> 0, true);
        });

      case CMD.CLEAR_HISTORY:
        /* payload zeros; clears the 5 history words of every band, both channels */
        this.state[target].seq++;
        return this._mkResponse(req, STATUS.OK, function (b, dv) { self._statusPayload(b, dv); });

      case CMD.SET_PREAMP:
        var q28 = u32le(req, 12);
        if (q28 > PREAMP_MAX) { this.lastError = 'preamp'; return this._mkResponse(req, STATUS.ERR_PARAM); }
        this.state[target].preampQ28 = q28;
        this.state[target].seq++;
        return this._mkResponse(req, STATUS.OK, function (b, dv) { self._statusPayload(b, dv); });

      default:
        this.lastError = 'cmd';
        return this._mkResponse(req, STATUS.ERR_CMD);
    }
  };

  /* WebHID-shaped async surface: sendFeatureReport(0, req) / receiveFeatureReport(0) */
  MockDevice.prototype.sendFeatureReport = function (reportId, data) {
    this._pending = this.handle(data);
    return Promise.resolve();
  };

  MockDevice.prototype.receiveFeatureReport = function (reportId) {
    if (!this._pending) return Promise.reject(new Error('mock: no pending response'));
    var resp = this._pending;
    this._pending = null;
    this.rxCount++;
    this.occCur += 1024;
    if (this.reportIdPrefix) {
      var out = new Uint8Array(resp.length + 1);
      out[0] = 0x00;
      out.set(resp, 1);
      return Promise.resolve(new DataView(out.buffer));
    }
    return Promise.resolve(new DataView(resp.buffer.slice(0)));
  };

  MockDevice.prototype.bandCount = function (target) { return BAND_COUNT[target]; };
  MockDevice.prototype.getTaps = function (target, channel, band) {
    return this.state[target].bands[channel][band].slice();
  };

  /* ------------------------------------------------------ Equalizer APO / PEQ text ----- */

  var APO_TO_TYPE = {
    PK: 'peaking', PEQ: 'peaking', PEAKING: 'peaking', BELL: 'peaking',
    LS: 'lowshelf', LSC: 'lowshelf', LSQ: 'lowshelf', LOWSHELF: 'lowshelf', LOW_SHELF: 'lowshelf',
    HS: 'highshelf', HSC: 'highshelf', HSQ: 'highshelf', HIGHSHELF: 'highshelf', HIGH_SHELF: 'highshelf',
    LP: 'lowpass', LPQ: 'lowpass', LPF: 'lowpass', LOWPASS: 'lowpass', LOW_PASS: 'lowpass',
    HP: 'highpass', HPQ: 'highpass', HPF: 'highpass', HIGHPASS: 'highpass', HIGH_PASS: 'highpass',
    NO: 'flat', NONE: 'flat', OFF: 'flat', FLAT: 'flat'
  };
  var TYPE_TO_APO = {
    peaking: 'PK', lowshelf: 'LSC', highshelf: 'HSC',
    lowpass: 'LPQ', highpass: 'HPQ'
  };

  function normTypeToken(tok) {
    if (!tok) return null;
    var k = String(tok).trim().toUpperCase().replace(/[\s-]+/g, '_');
    return APO_TO_TYPE[k] || APO_TO_TYPE[k.replace(/_/g, '')] || null;
  }

  function parseHz(tok, unit) {
    var v = parseFloat(String(tok).replace(',', '.'));
    if (!isFinite(v)) return null;
    if (unit && /^k/i.test(unit)) v *= 1000;
    return v;
  }

  function parseNum(tok) {
    var v = parseFloat(String(tok).replace(',', '.'));
    return isFinite(v) ? v : NaN;
  }

  /* Fit a GraphicEQ curve (`GraphicEQ: f1 g1; f2 g2; ...`)
   * into 10 octave-spaced Peaking bands via iterative residual fitting. */
  function fitGraphicEqTo10Bands(pairs, fs) {
    fs = fs || 48000;
    pairs.sort(function (a, b) { return a[0] - b[0]; });
    var centers = [31.5, 63, 125, 250, 500, 1000, 2000, 4000, 8000, 16000];
    function interpTarget(f) {
      if (f <= pairs[0][0]) return pairs[0][1];
      var last = pairs[pairs.length - 1];
      if (f >= last[0]) return last[1];
      var lf = Math.log(f);
      for (var i = 0; i < pairs.length - 1; i++) {
        if (f >= pairs[i][0] && f <= pairs[i + 1][0]) {
          var l0 = Math.log(pairs[i][0]), l1 = Math.log(pairs[i + 1][0]);
          var t = (l1 > l0) ? (lf - l0) / (l1 - l0) : 0;
          return pairs[i][1] + t * (pairs[i + 1][1] - pairs[i][1]);
        }
      }
      return 0;
    }
    var targetAtCenter = centers.map(interpTarget);
    var bands = centers.map(function (fc, idx) {
      return { on: true, type: 'peaking', f0: fc, gainDb: clamp(targetAtCenter[idx], -15, 15), q: 1.41 };
    });
    /* 6 damped coordinate-refinement passes to cancel adjacent-band overlap */
    for (var iter = 0; iter < 6; iter++) {
      var cur = totalResponseDb(bands, fs, centers, 0);
      for (var b = 0; b < bands.length; b++) {
        var err = targetAtCenter[b] - cur[b];
        bands[b].gainDb = Math.round(clamp(bands[b].gainDb + 0.65 * err, -15, 15) * 10) / 10;
      }
    }
    var sug = suggestPreampDb(bands, fs);
    return {
      preampDb: sug.needed ? sug.suggestedDb : 0,
      filters: bands
    };
  }

  /* Tolerant reader for Equalizer APO / ParametricEQ.txt / FixedBandEQ.txt /
   * GraphicEQ / CSV / TSV / Markdown tables.
   * Returns {preampDb|null, filters[], warnings[], lines, skipped, format} */
  function parseApo(text) {
    var res = { preampDb: null, filters: [], warnings: [], lines: 0, skipped: 0, format: 'apo' };
    if (typeof text !== 'string') return res;

    /* 1. GraphicEQ format: `GraphicEQ: 20 -1.2; 21 -1.1; ...` */
    var mGeq = text.replace(/^\uFEFF/, '').match(/^\s*GraphicEQ\s*:\s*(.+)$/im);
    if (mGeq) {
      var segs = mGeq[1].split(';');
      var pairs = [];
      for (var si = 0; si < segs.length; si++) {
        var tok = segs[si].trim().split(/\s+/);
        if (tok.length >= 2) {
          var gf = parseNum(tok[0]), gg = parseNum(tok[1]);
          if (gf > 0 && isFinite(gg)) pairs.push([gf, gg]);
        }
      }
      if (pairs.length >= 2) {
        var fit = fitGraphicEqTo10Bands(pairs, 48000);
        res.preampDb = fit.preampDb;
        res.filters = fit.filters;
        res.lines = 1;
        res.format = 'graphiceq';
        return res;
      }
    }

    var lines = text.split(/\r?\n/);
    for (var li = 0; li < lines.length; li++) {
      var raw = lines[li].replace(/^\uFEFF/, '');
      var line = raw.replace(/[#;].*$/, '').trim();
      if (!line) continue;
      res.lines++;

      var mPre = line.match(/^preamp\s*[:=]?\s*([-+]?\d*[.,]?\d+)\s*(db)?/i);
      if (mPre) { res.preampDb = parseNum(mPre[1]); continue; }

      /* Skip decorative markdown table header / separator lines */
      if (/^\|?[\s:-]+\|[\s|:-]+$/.test(line) || /\b(type|filter\s*type)\b.*\b(freq|fc)\b/i.test(line)) {
        continue;
      }

      /* 2. Standard Equalizer APO / ParametricEQ line */
      var mF = line.match(/^filter\s*(\d*)\s*:\s*(on|off)\s+([a-z_]+)\s*(.*)$/i);
      if (mF) {
        var on = /^on$/i.test(mF[2]);
        var code = mF[3].toUpperCase();
        var rest = mF[4] || '';

        var mFc = rest.match(/fc\s*([-+]?\d*[.,]?\d+)\s*(k?hz)?/i);
        var mGain = rest.match(/gain\s*([-+]?\d*[.,]?\d+)\s*(db)?/i);
        var mQ = rest.match(/\bq\s*([\d.,]+)/i);
        var mS = rest.match(/\bs\s*([\d.,]+)/i);
        var mBW = rest.match(/\bbw\s*([\d.,]+)\s*(oct)?/i);

        var type = normTypeToken(code);
        if (!type) {
          res.warnings.push('L' + (li + 1) + ': unsupported filter "' + code + '"');
          res.skipped++;
          continue;
        }
        if (type === 'flat') continue;
        var f0 = mFc ? parseHz(mFc[1], mFc[2]) : null;
        if (!(f0 > 0)) {
          res.warnings.push('L' + (li + 1) + ': missing/invalid Fc: ' + line.slice(0, 60));
          res.skipped++;
          continue;
        }
        var gain = mGain ? parseNum(mGain[1]) : 0;
        var q = 1.0;
        if (mQ) q = parseNum(mQ[1]);
        else if (mS) q = parseNum(mS[1]) * Math.SQRT1_2;   /* S -> equivalent Q */
        else if (mBW) {
          var bw = parseNum(mBW[1]);
          q = Math.sqrt(Math.pow(2, bw)) / (Math.pow(2, bw) - 1);
        } else if (type === 'lowpass' || type === 'highpass' || type === 'lowshelf' || type === 'highshelf') {
          q = Math.SQRT1_2;
        }

        res.filters.push({
          on: on, type: type, f0: clamp(f0, 20, 20000), gainDb: clamp(gain, -15, 15),
          q: clamp(q, 0.3, 10), line: li + 1, filterNo: mF[1] ? parseInt(mF[1], 10) : null
        });
        continue;
      }

      /* 3. Markdown table or CSV/TSV row:
       *    e.g. `| Low-shelf | 105 Hz | -1.4 dB | 0.70 |` or `PK, 147.3, -3.8, 0.83` */
      var cells = null;
      if (line.indexOf('|') >= 0) {
        cells = line.replace(/^\||\|$/g, '').split('|').map(function (c) { return c.trim(); });
      } else if (line.indexOf(',') >= 0 || line.indexOf('\t') >= 0) {
        cells = line.split(line.indexOf('\t') >= 0 ? '\t' : ',').map(function (c) { return c.trim(); });
      }
      if (cells && cells.length >= 3) {
        var tIdx = -1, fType = null;
        for (var ci = 0; ci < Math.min(3, cells.length); ci++) {
          var cand = normTypeToken(cells[ci]);
          if (cand && cand !== 'flat') { tIdx = ci; fType = cand; break; }
        }
        if (fType && cells.length >= tIdx + 3) {
          var mHz = cells[tIdx + 1].match(/([-+]?\d*[.,]?\d+)\s*(k?hz)?/i);
          var mGd = cells[tIdx + 2].match(/([-+]?\d*[.,]?\d+)/);
          var mQv = (cells[tIdx + 3] || '').match(/([\d.,]+)/);
          var tf0 = mHz ? parseHz(mHz[1], mHz[2]) : null;
          var tGain = mGd ? parseNum(mGd[1]) : 0;
          var tQ = mQv ? parseNum(mQv[1]) : ((fType === 'peaking') ? 1.0 : Math.SQRT1_2);
          if (tf0 > 0 && isFinite(tGain)) {
            res.filters.push({
              on: true, type: fType, f0: clamp(tf0, 20, 20000), gainDb: clamp(tGain, -15, 15),
              q: clamp(isFinite(tQ) ? tQ : 1.0, 0.3, 10), line: li + 1, filterNo: null
            });
            res.format = 'table';
            continue;
          }
        }
      }

      res.warnings.push('L' + (li + 1) + ': skipped: ' + line.slice(0, 60));
      res.skipped++;
    }
    return res;
  }

  function apoFromBands(bands, preampDb, maxBands) {
    var out = [];
    if (preampDb !== undefined && preampDb !== null)
      out.push('Preamp: ' + (preampDb >= 0 ? '+' : '') + preampDb.toFixed(2) + ' dB');
    var n = 0;
    for (var i = 0; i < bands.length; i++) {
      var b = bands[i];
      if (!b.on || b.type === 'flat') continue;
      var code = TYPE_TO_APO[b.type];
      if (!code) continue;
      n++;
      if (maxBands && n > maxBands) break;
      out.push('Filter ' + n + ': ON ' + code +
        ' Fc ' + (b.f0 >= 1000 ? (b.f0 / 1000).toFixed(b.f0 % 1000 === 0 ? 0 : 3) + ' kHz' : b.f0.toFixed(1) + ' Hz') +
        (b.type === 'lowpass' || b.type === 'highpass' ? '' : ' Gain ' + b.gainDb.toFixed(1) + ' dB') +
        ' Q ' + b.q.toFixed(3));
    }
    return out.join('\n') + '\n';
  }

  /* ------------------------------------------------------------- exports --- */

  return {
    Q29: Q29, Q28: Q28, PREAMP_MAX: PREAMP_MAX, PREAMP_UNITY: PREAMP_UNITY,
    MAGIC: MAGIC, PROTO_VER: PROTO_VER,
    CMD: CMD, CMD_NAME: CMD_NAME, STATUS: STATUS, STATUS_NAME: STATUS_NAME,
    FW_FLAG: FW_FLAG, ENABLE_FLAG: ENABLE_FLAG, BAND_COUNT: BAND_COUNT,
    TYPES: TYPES, PRESETS: PRESETS, REF10_BANDS: REF10_BANDS,

    pyRound: pyRound, clamp: clamp, bytesToHex: bytesToHex, u32le: u32le, i32le: i32le,
    rbjPeaking: rbjPeaking, rbjLowShelf: rbjLowShelf, rbjHighShelf: rbjHighShelf,
    rbjLowPass: rbjLowPass, rbjHighPass: rbjHighPass,
    quantise: quantise, identity: identity, designTaps: designTaps,
    shelfSlopeFromQ: shelfSlopeFromQ,

    logFreqGrid: logFreqGrid, sectionResponseDb: sectionResponseDb,
    cascadeResponseDb: cascadeResponseDb, totalResponseDb: totalResponseDb,
    peakDb: peakDb, suggestPreampDb: suggestPreampDb,

    mkBand: mkBand, flatBands: flatBands, presetById: presetById,

    checksum: checksum, dbToPreampQ28: dbToPreampQ28, preampQ28ToDb: preampQ28ToDb,
    encodeRequest: encodeRequest, decodeResponse: decodeResponse,
    normaliseReport: normaliseReport, MockDevice: MockDevice,

    parseApo: parseApo, apoFromBands: apoFromBands
  };
});
