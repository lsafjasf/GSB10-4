'use strict';
// 多项式乘法：朴素 / 单次FFT(带精度护栏) / 分块FFT(大系数精确) + 自动回退。
// 所有对外接口返回 BigInt 升幂系数数组（精确整数）。

const { convolveRaw, nextPow2 } = require('./fft');

// ---------------- 朴素乘法 O(n*m)，精确 ----------------
function multiplyNaive(a, b) {
  const n = a.length, m = b.length;
  const out = new Array(n + m - 1).fill(0n);
  for (let i = 0; i < n; i++) {
    const ai = a[i];
    if (ai === 0n) continue;
    for (let j = 0; j < m; j++) {
      const bj = b[j];
      if (bj !== 0n) out[i + j] += ai * bj;
    }
  }
  return out;
}

// ---------------- 精度策略 ----------------
// 单次 FFT 卷积的误差经验模型：err ≈ K * log2(N) * B1 * B2 * min(n,m) * log2(N)
// 即 K_ERR * min(n,m) * log2(N)^2 * B1*B2。常数随 N 标定：
// 实测有效常数在 N=2^10 时约 1.3e-16，N=2^21 时约 2.2e-15（见 RESULTS.md）。
const K_ERR = 2e-16;
const PREFLIGHT_LIMIT = 0.1;   // 预估误差超过该值 -> 不走单次FFT
const POSTCHECK_LIMIT = 0.25;  // 取整残差超过该值 -> 判定结果不可信，回退

function maxAbs(arr) {
  let m = 0n;
  for (const v of arr) { const x = v < 0n ? -v : v; if (x > m) m = x; }
  return m;
}

function preflightError(B1, B2, n, m) {
  const N = nextPow2(n + m - 1);
  const logN = Math.log2(N);
  return K_ERR * B1 * B2 * Math.min(n, m) * logN * logN;
}

// 单次 FFT 卷积（不做安全判断），返回 { out: BigInt[], maxResidual }
function singleFftConvolve(aNum, bNum) {
  const { c } = convolveRaw(aNum, bNum);
  const len = aNum.length + bNum.length - 1;
  const out = new Array(len);
  let maxResidual = 0, maxMag = 0;
  for (let i = 0; i < len; i++) {
    const r = Math.round(c[i]);
    const res = Math.abs(c[i] - r);
    if (res > maxResidual) maxResidual = res;
    const mag = Math.abs(c[i]);
    if (mag > maxMag) maxMag = mag;
    out[i] = BigInt(r);
  }
  return { out, maxResidual, maxMag };
}

// ---------------- 分块 FFT：任意大系数的精确快速乘法 ----------------
// 带符号平衡分块：x = sum d_i * 2^(w*i)，d_i ∈ (-2^(w-1), 2^(w-1)]，
// 则 c = sum_{i,j} conv(D_i, E_j) << (w*(i+j))，块卷积用单次 FFT。
// w=26 时块卷积安全界 ~2^53，对 N <= 2^21 有巨大余量。
const CHUNK_W = 26;

// 带符号平衡分块：把（可负）整数 x 写成 sum d_i * 2^(wi)，d_i ∈ (-2^(w-1), 2^(w-1)]
// 这样块卷积可正可负，FFT 直接处理，重构即得精确带符号结果。
function splitSigned(x, w) {
  const half = 1n << BigInt(w - 1);
  const wBig = BigInt(w), mask = (1n << wBig) - 1n;
  const digits = [];
  let v = x;
  while (v !== 0n) {
    let d = v & mask;
    if (d >= half) d -= (1n << wBig);
    digits.push(d);
    v = (v - d) >> wBig;
  }
  return digits.length ? digits : [0n];
}

// 根据 n,m 选择最大可用 w，保证每个块卷积的预估误差 <= CHUNK_SAFE
const CHUNK_SAFE = 0.02;
function chooseChunkW(n, m) {
  let w = CHUNK_W;
  while (w > 8) {
    const B = 2 ** (w - 1);
    if (preflightError(B, B, n, m) <= CHUNK_SAFE) break;
    w--;
  }
  return w;
}

function chunkedFftConvolveSigned(aBig, bBig, w) {
  const n = aBig.length, m = bBig.length;
  if (w === undefined) w = chooseChunkW(n, m);
  const da = aBig.map(v => splitSigned(v, w));
  const db = bBig.map(v => splitSigned(v, w));
  const ka = Math.max(...da.map(d => d.length));
  const kb = Math.max(...db.map(d => d.length));
  const ca = [], cb = [];
  for (let i = 0; i < ka; i++) { const arr = new Float64Array(n); for (let t = 0; t < n; t++) arr[t] = Number(da[t][i] || 0n); ca.push(arr); }
  for (let j = 0; j < kb; j++) { const arr = new Float64Array(m); for (let t = 0; t < m; t++) arr[t] = Number(db[t][j] || 0n); cb.push(arr); }
  const len = n + m - 1;
  const acc = new Array(len).fill(0n);
  let maxResidual = 0, chunkPairs = 0;
  for (let i = 0; i < ka; i++) {
    for (let j = 0; j < kb; j++) {
      chunkPairs++;
      const { c } = convolveRaw(ca[i], cb[j]);
      const shift = BigInt(w * (i + j));
      for (let t = 0; t < len; t++) {
        const r = Math.round(c[t]);
        const res = Math.abs(c[t] - r);
        if (res > maxResidual) maxResidual = res;
        if (r !== 0) acc[t] += BigInt(r) << shift;
      }
    }
  }
  return { out: acc, maxResidual, chunkPairs, w };
}

// ---------------- 统一入口：带护栏与回退 ----------------
// opts: { force: 'naive'|'fft'|'chunked'|undefined, relaxed: bool, log: [] }
// 返回 { result, method, fallbacks }
function multiplyExact(a, b, opts = {}) {
  const log = opts.log || [];
  const n = a.length, m = b.length;
  const B1 = Number(maxAbs(a)), B2 = Number(maxAbs(b));

  if (opts.force === 'naive') return { result: multiplyNaive(a, b), method: 'naive', fallbacks: log };

  // 极端次数差：朴素是 O(n*m) 中 m 很小，FFT 反而浪费
  const minLen = Math.min(n, m);
  const naiveOps = n * m;
  const N = nextPow2(n + m - 1);
  const fftOps = 3 * N * Math.log2(N);

  if (opts.force !== 'fft' && opts.force !== 'chunked') {
    if (minLen <= 64 || naiveOps <= fftOps) {
      return { result: multiplyNaive(a, b), method: 'naive(auto: 小乘数或极端次数差)', fallbacks: log };
    }
  }

  if (opts.force === 'chunked') {
    const r = chunkedFftConvolveSigned(a, b, opts.chunkW);
    log.push({ stage: 'chunked-fft', reason: '强制分块路径', w: r.w, maxResidual: r.maxResidual, ok: r.maxResidual <= POSTCHECK_LIMIT });
    if (r.maxResidual > POSTCHECK_LIMIT) {
      log.push({ stage: 'fallback', from: 'chunked-fft', to: 'naive', reason: `取整残差 ${r.maxResidual.toExponential(2)} > ${POSTCHECK_LIMIT}` });
      return { result: multiplyNaive(a, b), method: 'naive(fallback from chunked)', fallbacks: log };
    }
    return { result: r.out, method: `chunked-fft(w=${CHUNK_W}, pairs=${r.chunkPairs})`, fallbacks: log };
  }

  // 预检：估计单次 FFT 误差
  const est = preflightError(B1, B2, n, m);
  if (est > PREFLIGHT_LIMIT && !opts.relaxed) {
    log.push({ stage: 'preflight', from: 'single-fft', to: 'chunked-fft',
      reason: `预估误差 ${est.toExponential(2)} > ${PREFLIGHT_LIMIT}（B1=${B1}, B2=${B2}, n=${n}, m=${m}）` });
    const r = chunkedFftConvolveSigned(a, b);
    log.push({ stage: 'chunked-fft', w: r.w, reason: '预检回退后的精确快速路径', maxResidual: r.maxResidual, ok: r.maxResidual <= POSTCHECK_LIMIT });
    if (r.maxResidual > POSTCHECK_LIMIT) {
      log.push({ stage: 'fallback', from: 'chunked-fft', to: 'naive', reason: `取整残差 ${r.maxResidual.toExponential(2)} > ${POSTCHECK_LIMIT}` });
      return { result: multiplyNaive(a, b), method: 'naive(fallback)', fallbacks: log };
    }
    return { result: r.out, method: `chunked-fft(w=${r.w}, pairs=${r.chunkPairs}, 预检回退)`, fallbacks: log };
  }

  // 单次 FFT + 后验校验
  const aNum = a.map(Number), bNum = b.map(Number);
  const { out, maxResidual, maxMag } = singleFftConvolve(aNum, bNum);
  const MAG_LIMIT = 2 ** 52;
  log.push({ stage: 'single-fft', maxResidual, maxMag,
    ok: maxResidual <= POSTCHECK_LIMIT && maxMag <= MAG_LIMIT });
  if (maxMag > MAG_LIMIT) {
    log.push({ stage: 'fallback', from: 'single-fft', to: 'chunked-fft', reason: `卷积峰值 ${maxMag.toExponential(2)} 超 2^52，浮点无法表示整数` });
    const r2 = chunkedFftConvolveSigned(a, b);
    log.push({ stage: 'chunked-fft', w: r2.w, reason: '幅值护栏回退后的精确快速路径', maxResidual: r2.maxResidual });
    return { result: r2.out, method: `chunked-fft(w=${r2.w}, pairs=${r2.chunkPairs}, 幅值回退)`, fallbacks: log };
  }
  if (maxResidual > POSTCHECK_LIMIT) {
    log.push({ stage: 'fallback', from: 'single-fft', to: 'naive', reason: `后验残差 ${maxResidual.toExponential(2)} > ${POSTCHECK_LIMIT}` });
    return { result: multiplyNaive(a, b), method: 'naive(fallback from single-fft)', fallbacks: log };
  }
  return { result: out, method: 'single-fft', fallbacks: log };
}

module.exports = {
  multiplyNaive, multiplyExact, singleFftConvolve, chunkedFftConvolveSigned,
  preflightError, maxAbs, K_ERR, PREFLIGHT_LIMIT, POSTCHECK_LIMIT, CHUNK_W,
};
