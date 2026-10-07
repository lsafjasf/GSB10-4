'use strict';
// 交叉点扫描：朴素 vs FFT 随规模/次数差的变化。
// 运行：node --expose-gc bench/bench_scaling.js
const { multiplyNaive, multiplyExact } = require('../src/multiply');
const { randCoeffs, measure, fmtMs } = require('../src/util');

console.log('=== 稠密等长：朴素 vs 单次FFT 交叉点 ===');
console.log('n\tnaive(ms)\tfft(ms)\t比值');
for (const n of [64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32768, 65536]) {
  const a = randCoeffs(n, 1000, n).map(BigInt);
  const b = randCoeffs(n, 1000, n * 2).map(BigInt);
  const tN = measure(() => multiplyNaive(a, b)).ms;
  const tF = measure(() => multiplyExact(a, b, { force: 'fft' })).ms;
  console.log(`${n}\t${tN.toFixed(1)}\t\t${tF.toFixed(1)}\t\t${(tN / tF).toFixed(2)}x`);
}

console.log('\n=== 极端次数差：n=100000 固定，m 变化 ===');
console.log('m\tnaive(ms)\tfft(ms)\t自动选择');
const big = randCoeffs(100000, 1000, 7).map(BigInt);
for (const m of [1, 4, 16, 64, 256, 1024, 4096]) {
  const b = randCoeffs(m, 1000, 8).map(BigInt);
  const tN = measure(() => multiplyNaive(big, b)).ms;
  const tF = measure(() => multiplyExact(big, b, { force: 'fft' })).ms;
  const auto = multiplyExact(big, b).method;
  console.log(`${m}\t${tN.toFixed(1)}\t\t${tF.toFixed(1)}\t\t${auto}`);
}
