'use strict';
// 多点求值对拍基准：精确(BigInt)版 与 模p(NTT)版。
// 运行：node --expose-gc bench/bench_eval.js
const { evalNaive, evalFast } = require('../src/evaluate');
const { evalModNaive, evalModFast } = require('../src/modntt');
const { mulberry32, measure, fmtMs, fmtMB } = require('../src/util');
const { multiplyExact } = require('../src/multiply');

const rnd = mulberry32(99);
const nBig = (n, B) => Array.from({ length: n }, () => BigInt(Math.floor(rnd() * (2 * B + 1))) - BigInt(B)); // B 为 Number

function report(title, n, m, kind) {
  if (kind === 'exact') {
    const c = nBig(n, 1000);
    const pts = nBig(m, 1000);
    const t1 = measure(() => evalNaive(c, pts));
    const t2 = measure(() => evalFast(c, pts));
    const match = t1.result.every((v, i) => v === t2.result[i]);
    console.log(`${title}\tn=${n},m=${m}\tHorner=${fmtMs(t1.ms)} 堆峰=${fmtMB(t1.heapDeltaMB)}\t乘积树=${fmtMs(t2.ms)} 堆峰=${fmtMB(t2.heapDeltaMB)}\t对拍=${match ? '一致' : '不一致'}`);
  } else {
    const P = 998244353n, PN = 998244352;
    const c = nBig(n, PN).map(x => ((x % P) + P) % P);
    const pts = nBig(m, PN).map(x => ((x % P) + P) % P);
    const t1 = measure(() => evalModNaive(c, pts));
    const t2 = measure(() => evalModFast(c, pts));
    const match = t1.result.every((v, i) => v === t2.result[i]);
    console.log(`${title}\tn=${n},m=${m}\tHorner=${fmtMs(t1.ms)} 堆峰=${fmtMB(t1.heapDeltaMB)}\t乘积树=${fmtMs(t2.ms)} 堆峰=${fmtMB(t2.heapDeltaMB)}\t对拍=${match ? '一致' : '不一致'}`);
  }
}

console.log('=== 精确(BigInt)多点求值：Horner 逐点 vs 乘积树+余数树 ===');
console.log('说明：两条路径均为 O(n*m) 次 BigInt 运算（精确余数树的常数更大），');
console.log('      亚二次收益需要分块FFT乘法，交叉点在万级以上（见 README）。');
for (const [n, m] of [[128, 128], [256, 256], [512, 512], [1024, 1024], [2048, 2048]]) {
  report('[精确]', n, m, 'exact');
}

console.log('\n=== 模 p=998244353 多点求值：Horner O(nm) vs NTT 乘积树 O(n log^2 n) ===');
for (const [n, m] of [[512, 512], [1024, 1024], [2048, 2048], [4096, 4096], [8192, 8192]]) {
  report('[模p]', n, m, 'mod');
}

// 大规模只跑快速路径
console.log('\n=== 模p大规模快速求值 ===');
for (const [n, m] of [[16384, 16384], [32768, 32768], [65536, 65536], [131072, 131072], [262144, 262144]]) {
  const P = 998244353n, PN = 998244352;
  const c = nBig(n, PN).map(x => ((x % P) + P) % P);
  const pts = nBig(m, PN).map(x => ((x % P) + P) % P);
  const t = measure(() => evalModFast(c, pts));
  // 与 Horner 抽样对拍 16 个点
  const idxs = Array.from({ length: 16 }, () => Math.floor(rnd() * m));
  let ok = true;
  for (const k of idxs) {
    const x = pts[k]; let acc = 0n;
    for (let i = c.length - 1; i >= 0; i--) acc = (acc * x + c[i]) % P;
    if (acc !== t.result[k]) { ok = false; break; }
  }
  console.log(`[模p-大] n=${n},m=${m}\t乘积树=${fmtMs(t.ms)} 堆峰=${fmtMB(t.heapDeltaMB)} 峰值RSS=${fmtMB(t.maxRssMB)}\t抽样16点对拍=${ok ? '一致' : '不一致'}`);
}
