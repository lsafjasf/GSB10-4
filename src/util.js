'use strict';
// 通用工具：确定性随机数、计时、内存峰值采样。

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

// 生成 n 个随机整数系数（Number），|c| <= bound
function randCoeffs(n, bound, seed) {
  const rnd = mulberry32(seed);
  const out = new Array(n);
  for (let i = 0; i < n; i++) out[i] = Math.floor(rnd() * (2 * bound + 1)) - bound;
  return out;
}

function nowMs() { return performance.now(); }

// 运行 fn，返回 { result, ms, heapDeltaMB, maxRssMB }
// heapDeltaMB: fn 执行期间 process.memoryUsage().heapUsed 的峰值 - 起始值（尽量先 GC）
// maxRssMB:    进程自启动以来的最大 RSS（process.memoryUsage.rss 在 Linux 上即峰值 RSS）
function measure(fn, { gc = true } = {}) {
  if (gc && global.gc) global.gc();
  const h0 = process.memoryUsage().heapUsed;
  let peak = h0;
  const timer = setInterval(() => {
    const h = process.memoryUsage().heapUsed;
    if (h > peak) peak = h;
  }, 5);
  const t0 = nowMs();
  const result = fn();
  const ms = nowMs() - t0;
  clearInterval(timer);
  const hEnd = process.memoryUsage().heapUsed;
  if (hEnd > peak) peak = hEnd;
  const maxRssMB = process.memoryUsage.rss() / 1048576;
  return { result, ms, heapDeltaMB: (peak - h0) / 1048576, maxRssMB };
}

function fmtMB(x) { return x.toFixed(1); }
function fmtMs(x) { return x >= 1000 ? (x / 1000).toFixed(2) + ' s' : x.toFixed(1) + ' ms'; }

module.exports = { mulberry32, randCoeffs, nowMs, measure, fmtMB, fmtMs };
