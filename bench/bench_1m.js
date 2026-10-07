'use strict';
// 100 万系数基准：朴素 vs 快速（FFT）耗时与内存峰值。
// 运行：node --expose-gc bench/bench_1m.js
const { multiplyNaive, multiplyExact } = require('../src/multiply');
const { randCoeffs, measure, fmtMs, fmtMB } = require('../src/util');

const N = 1_000_000;
console.log(`=== 100 万系数乘法基准 (n=m=${N}) ===`);
console.log(`进程最大RSS基线: ${fmtMB(process.memoryUsage.rss() / 1048576)} MB`);

// ---- 快速路径：FFT（系数界 256，单次FFT安全区内）----
const a = randCoeffs(N, 256, 1001);
const b = randCoeffs(N, 256, 2002);
const aBig = a.map(BigInt), bBig = b.map(BigInt);

const fftLog = [];
const rFft = measure(() => multiplyExact(aBig, bBig, { log: fftLog }));
console.log(`\n[快速] method=${rFft.result.method}`);
console.log(`[快速] 耗时=${fmtMs(rFft.ms)}  堆增量峰值=${fmtMB(rFft.heapDeltaMB)} MB  进程峰值RSS=${fmtMB(rFft.maxRssMB)} MB`);
const cFft = rFft.result.result;

// 抽样验证：与朴素逐点计算 8 个位置的系数对拍
console.log('[快速] 抽样对拍(8个位置)...');
const { mulberry32 } = require('../src/util');
const rnd = mulberry32(77);
let spotOk = true;
for (let t = 0; t < 8; t++) {
  const k = Math.floor(rnd() * (2 * N - 1));
  let sum = 0n;
  const i0 = Math.max(0, k - (N - 1)), i1 = Math.min(N - 1, k);
  for (let i = i0; i <= i1; i++) sum += aBig[i] * bBig[k - i];
  if (sum !== cFft[k]) { spotOk = false; console.log(`  位置 ${k}: 不一致!`); }
}
console.log(`[快速] 抽样对拍: ${spotOk ? '全部一致' : '存在不一致'}`);

// ---- 朴素路径（稠密 1M x 1M 不可行，用缩放实测外推）----
console.log('\n[朴素] 稠密 1M x 1M 为 10^12 次 BigInt 乘加，直接运行不现实；用缩放实测外推：');
const scaling = [];
for (const n of [2000, 4000, 8000]) {
  const x = aBig.slice(0, n), y = bBig.slice(0, n);
  const r = measure(() => multiplyNaive(x, y));
  scaling.push({ n, ms: r.ms });
  console.log(`  n=${n}: ${fmtMs(r.ms)}  堆增量峰值=${fmtMB(r.heapDeltaMB)} MB`);
}
// 按 O(n^2) 外推到 1M
const perOp = scaling[2].ms / (scaling[2].n ** 2);
const extrapMs = perOp * N * N;
console.log(`  拟合单次乘加 ≈ ${(perOp * 1e6).toFixed(0)} ns -> 1M x 1M 朴素预计 ≈ ${(extrapMs / 3600000).toFixed(2)} 小时`);

// ---- 朴素路径（极端次数差：1M x 1，常数乘法，O(n)）----
const one = [bBig[0]];
const rNaive1 = measure(() => multiplyNaive(aBig, one));
console.log(`\n[朴素-极端次数差] 1M x 1: 耗时=${fmtMs(rNaive1.ms)}  堆增量峰值=${fmtMB(rNaive1.heapDeltaMB)} MB  进程峰值RSS=${fmtMB(rNaive1.maxRssMB)} MB`);
// 与 FFT 结果对拍（常数乘法）
let ok1 = true;
for (let i = 0; i < N; i++) if (rNaive1.result[i] !== aBig[i] * one[0]) { ok1 = false; break; }
console.log(`[朴素-极端次数差] 对拍: ${ok1 ? '一致' : '不一致'}`);

// ---- 朴素路径（1M x 64，仍在线性量级）----
const small64 = bBig.slice(0, 64);
const rNaive64 = measure(() => multiplyNaive(aBig, small64));
console.log(`[朴素-极端次数差] 1M x 64: 耗时=${fmtMs(rNaive64.ms)}  堆增量峰值=${fmtMB(rNaive64.heapDeltaMB)} MB`);

// ---- 自动分发在 1M 下的选择 ----
console.log(`\n[自动分发] 1M x 1M (B=256) 选择: ${fftLog.length ? fftLog[fftLog.length-1].stage || 'single-fft' : 'single-fft'}，记录: ${JSON.stringify(fftLog)}`);
console.log(`\n进程最终峰值RSS: ${fmtMB(process.memoryUsage.rss() / 1048576)} MB`);
