'use strict';
// 自测：所有快速路径与精确整数(BigInt)结果对拍。
// 运行：node test/selftest.js
const { multiplyNaive, multiplyExact, chunkedFftConvolveSigned, preflightError } = require('../src/multiply');
const { evalNaive, evalFast } = require('../src/evaluate');
const { evalModNaive, evalModFast, mulMod, P } = require('../src/modntt');
const { convolveRaw } = require('../src/fft');
const { mulberry32 } = require('../src/util');

const PB = BigInt(P);
let passed = 0;
function ok(name, cond, extra = '') {
  if (!cond) { console.error(`FAIL  ${name} ${extra}`); process.exitCode = 1; }
  else { passed++; console.log(`pass  ${name} ${extra}`); }
}
function eqArr(a, b) { return a.length === b.length && a.every((v, i) => v === b[i]); }
const rnd = mulberry32(12345);
const randBig = (n, bound) => Array.from({ length: n }, () => BigInt(Math.floor(rnd() * (2 * bound + 1))) - BigInt(bound));

// ---------- 1. 常数项 ----------
{
  const r = multiplyExact([7n], [-6n]);
  ok('常数项相乘', eqArr(r.result, [-42n]), `method=${r.method}`);
  const r0 = multiplyExact([0n], [5n]);
  ok('零常数', eqArr(r0.result, [0n]));
}

// ---------- 2. 单项式 ----------
{
  const mono = multiplyExact([1n], [0n, 0n, 0n, 1n]); // 1 * x^3
  ok('单项式移位', eqArr(mono.result, [0n, 0n, 0n, 1n]));
  const two = multiplyExact([0n, 2n], [0n, 0n, 3n]); // 2x * 3x^2 = 6x^3
  ok('单项式相乘', eqArr(two.result, [0n, 0n, 0n, 6n]));
}

// ---------- 3. 极端次数差 ----------
{
  const a = randBig(1, 1000), b = randBig(100000, 1000);
  const log = [];
  const r = multiplyExact(a, b, { log });
  ok('极端次数差 1x100000 自动走朴素', r.method.startsWith('naive'), `method=${r.method}`);
  ok('极端次数差结果正确', eqArr(r.result, multiplyNaive(a, b)));
  const a2 = randBig(3, 1000), b2 = randBig(50000, 1000);
  const r2 = multiplyExact(a2, b2);
  ok('极端次数差 3x50000 正确', eqArr(r2.result, multiplyNaive(a2, b2)), `method=${r2.method}`);
}

// ---------- 4. 乘法三路径随机对拍 ----------
{
  let allOk = true, maxResidualSeen = 0;
  for (const [n, m, B] of [[1, 1, 10], [2, 7, 100], [64, 64, 1000], [100, 300, 1e6], [500, 500, 1e9], [1000, 33, 1e4]]) {
    const a = randBig(n, B), b = randBig(m, B);
    const exact = multiplyNaive(a, b);
    const log = [];
    const r = multiplyExact(a, b, { log });
    if (!eqArr(r.result, exact)) { allOk = false; console.error(`  不一致 @ n=${n},m=${m},B=${B}, method=${r.method}`); }
    for (const e of log) if (e.maxResidual !== undefined) maxResidualSeen = Math.max(maxResidualSeen, e.maxResidual);
  }
  ok('乘法随机对拍(6组)', allOk, `maxResidual=${maxResidualSeen.toExponential(2)}`);
}

// ---------- 5. 大系数丢精度：识别 + 回退 ----------
{
  // 5a. 原始 FFT 的误差随系数增大而失控（演示精度边界）
  const n = 512;
  const rows = [];
  for (const bits of [10, 20, 30, 40, 50]) {
    const B = 2 ** bits;
    const a = randBig(n, B).map(Number), b = randBig(n, B).map(Number);
    const exact = multiplyNaive(a.map(BigInt), b.map(BigInt));
    const { c } = convolveRaw(a, b);
    let maxErr = 0;
    for (let i = 0; i < exact.length; i++) maxErr = Math.max(maxErr, Math.abs(c[i] - Number(exact[i])));
    const est = preflightError(B, B, n, n);
    rows.push({ bits, maxErr, est });
    console.log(`  [精度] B=2^${bits}: 实测最大误差=${maxErr.toExponential(2)}  预估界=${est.toExponential(2)}`);
  }
  ok('误差随系数增长', rows[4].maxErr > rows[0].maxErr);
  ok('B=2^50 时单次FFT误差巨大(>2^20)', rows[4].maxErr > 2 ** 20, `实测 ${rows[4].maxErr.toExponential(2)}`);

  // 5b. 预检回退：大系数自动改走分块FFT，结果精确
  const a = randBig(300, 0).map((_, i) => (1n << 50n) + BigInt(i));
  const b = randBig(300, 0).map((_, i) => (1n << 49n) - BigInt(i * 7));
  const exact = multiplyNaive(a, b);
  const log = [];
  const r = multiplyExact(a, b, { log });
  ok('大系数预检回退到分块FFT', r.method.includes('chunked'), `method=${r.method}`);
  ok('回退后结果精确', eqArr(r.result, exact));
  ok('回退有日志记录', log.some(e => e.stage === 'preflight'));
  console.log('  [回退记录]', JSON.stringify(log));

  // 5c. 后验拒绝：强制不安全的块宽，残差超阈值 -> 回退朴素
  const log2 = [];
  const r2 = multiplyExact(a, b, { force: 'chunked', chunkW: 26, log: log2 });
  ok('后验残差拒绝并回退朴素', r2.method.includes('fallback'), `method=${r2.method}`);
  ok('后验回退结果精确', eqArr(r2.result, exact));
  console.log('  [回退记录]', JSON.stringify(log2));

  // 5d. 幅值护栏：卷积峰值超 2^52 时单次FFT不可信 -> 回退
  const big = Array.from({ length: 200 }, (_, i) => (1n << 40n) + BigInt(i));
  const log3 = [];
  const r3 = multiplyExact(big, big, { force: 'fft', relaxed: true, log: log3 }); // relaxed 跳过预检，专门触发幅值护栏
  ok('幅值护栏回退', r3.method.includes('chunked') || r3.method.includes('fallback'), `method=${r3.method}`);
  ok('幅值护栏结果精确', eqArr(r3.result, multiplyNaive(big, big)));
  console.log('  [回退记录]', JSON.stringify(log3));
}

// ---------- 6. 分块FFT独立对拍 ----------
{
  const a = randBig(400, 0).map(() => BigInt(Math.floor(rnd() * 1e6)) * (1n << 55n) - (1n << 60n));
  const b = randBig(256, 0).map(() => BigInt(Math.floor(rnd() * 1e6)) * (1n << 52n) + 3n);
  const exact = multiplyNaive(a, b);
  const { out, maxResidual, w, chunkPairs } = chunkedFftConvolveSigned(a, b);
  ok('分块FFT(60bit系数)精确', eqArr(out, exact), `w=${w} pairs=${chunkPairs} residual=${maxResidual.toExponential(2)}`);
}

// ---------- 7. 多点求值对拍 ----------
{
  for (const [n, m] of [[1, 5], [2, 1], [50, 64], [200, 100], [128, 128]]) {
    const c = randBig(n, 1000);
    const pts = Array.from({ length: m }, () => BigInt(Math.floor(rnd() * 200) - 100));
    const exact = evalNaive(c, pts);
    const fast = evalFast(c, pts);
    ok(`精确多点求值 n=${n},m=${m}`, eqArr(fast, exact));
  }
  // 模数版
  for (const [n, m] of [[50, 64], [300, 200], [1000, 512]]) {
    const c = randBig(n, 1e6).map(x => ((x % PB) + PB) % PB);
    const pts = Array.from({ length: m }, () => BigInt(Math.floor(rnd() * P)));
    const naive = evalModNaive(c, pts);
    const fast = evalModFast(c, pts);
    ok(`模p多点求值 n=${n},m=${m}`, eqArr(fast, naive));
  }
  // 模p版与精确版交叉验证
  {
    const c = randBig(80, 500);
    const pts = Array.from({ length: 50 }, () => BigInt(Math.floor(rnd() * 1000) - 500));
    const exact = evalNaive(c, pts);
    const modFast = evalModFast(c.map(x => ((x % PB) + PB) % PB), pts.map(x => ((x % PB) + PB) % PB));
    ok('模p快速求值 == 精确值 mod p', modFast.every((v, i) => v === ((exact[i] % PB) + PB) % PB));
  }
}

// ---------- 8. 模乘对拍 ----------
{
  const a = randBig(300, 1e8).map(x => ((x % PB) + PB) % PB);
  const b = randBig(300, 1e8).map(x => ((x % PB) + PB) % PB);
  const exact = multiplyNaive(a, b).map(v => v % PB);
  const got = mulMod(a, b);
  ok('NTT模乘对拍', eqArr(got.map(v => ((v % PB) + PB) % PB), exact));
}

console.log(`\n${passed} 项通过${process.exitCode ? '，存在失败' : '，全部通过'}`);
