'use strict';
// 多项式乘法与多点求值库（纯标准库，Node.js >= 14）
//
// 乘法（src/multiply.js）：
//   multiplyNaive(a, b)            朴素 O(n*m)，BigInt 精确
//   multiplyExact(a, b, opts)      自动分发：朴素 / 单次FFT / 分块FFT，带精度护栏与回退
//   chunkedFftConvolveSigned(a,b)  分块FFT，任意大系数精确
// 求值（src/evaluate.js）：
//   evalNaive(coeffs, points)      逐点 Horner，O(n*m)，精确
//   evalFast(coeffs, points)       乘积树+余数树，精确
// 模数版（src/modntt.js，p = 998244353）：
//   evalModNaive / evalModFast     NTT + 牛顿迭代快速除法，O(M(n) log n)
//
// 约定：多项式为升幂系数数组，a[i] 是 x^i 的系数；精确接口用 BigInt。

const multiply = require('./src/multiply');
const evaluate = require('./src/evaluate');
const modntt = require('./src/modntt');
const fft = require('./src/fft');

module.exports = { ...multiply, ...evaluate, modntt, fft };
