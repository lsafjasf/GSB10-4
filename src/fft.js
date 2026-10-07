'use strict';
// 迭代 radix-2 复数 FFT（Float64Array），纯标准库。
// 约定：多项式用升幂系数数组表示，a[i] 是 x^i 的系数。

function fft(re, im, invert) {
  const n = re.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) {
      let t = re[i]; re[i] = re[j]; re[j] = t;
      t = im[i]; im[i] = im[j]; im[j] = t;
    }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const half = len >> 1;
    const ang = (2 * Math.PI / len) * (invert ? -1 : 1);
    const wlenRe = Math.cos(ang), wlenIm = Math.sin(ang);
    for (let i = 0; i < n; i += len) {
      let wRe = 1, wIm = 0;
      for (let j = 0; j < half; j++) {
        const uRe = re[i + j], uIm = im[i + j];
        const vRe = re[i + j + half] * wRe - im[i + j + half] * wIm;
        const vIm = re[i + j + half] * wIm + im[i + j + half] * wRe;
        re[i + j] = uRe + vRe; im[i + j] = uIm + vIm;
        re[i + j + half] = uRe - vRe; im[i + j + half] = uIm - vIm;
        const nwRe = wRe * wlenRe - wIm * wlenIm;
        wIm = wRe * wlenIm + wIm * wlenRe;
        wRe = nwRe;
      }
    }
  }
  if (invert) {
    for (let i = 0; i < n; i++) { re[i] /= n; im[i] /= n; }
  }
}

function nextPow2(n) { let s = 1; while (s < n) s <<= 1; return s; }

// 原始浮点卷积：输入 Number 数组，返回 { c: Float64Array, size }
// 不做任何取整/校验，供误差测量与底层复用。
function convolveRaw(a, b) {
  const size = nextPow2(a.length + b.length - 1);
  const re = new Float64Array(size), im = new Float64Array(size);
  for (let i = 0; i < a.length; i++) re[i] = a[i];
  for (let i = 0; i < b.length; i++) im[i] = b[i];
  fft(re, im, false);
  for (let i = 0; i < size; i++) {
    const ar = re[i], ai = im[i];
    re[i] = ar * ar - ai * ai;   // (a+ib)^2 的实部 = a^2-b^2
    im[i] = 2 * ar * ai;         // 虚部 = 2ab
  }
  fft(re, im, true);
  const c = new Float64Array(size);
  for (let i = 0; i < size; i++) c[i] = im[i] / 2; // 打包技巧使结果为 2*conv
  return { c, size };
}

// 与精确结果对拍：返回最大绝对误差（exact 为 BigInt 数组）
function maxErrorVsExact(floatArr, exact) {
  let maxErr = 0, argmax = -1;
  for (let i = 0; i < exact.length; i++) {
    const e = Math.abs(floatArr[i] - Number(exact[i]));
    if (e > maxErr) { maxErr = e; argmax = i; }
  }
  return { maxErr, argmax };
}

module.exports = { fft, nextPow2, convolveRaw, maxErrorVsExact };
