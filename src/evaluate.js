'use strict';
// 多点求值：朴素 Horner 逐点 O(n*m) vs 乘积树+余数树。
// 精确版基于 BigInt；余数树的渐近优势 O(M(n) log m) 依赖快速乘法，
// 在中小规模常数因子较大，交叉点见 RESULTS.md。

const { multiplyNaive, multiplyExact } = require('./multiply');

// 逐点 Horner，精确
function evalNaive(coeffs, points) {
  return points.map(x => {
    let acc = 0n;
    for (let i = coeffs.length - 1; i >= 0; i--) acc = acc * x + coeffs[i];
    return acc;
  });
}

// 多项式除法取余（精确，首一除式），schoolbook O(deg(f)*deg(g))
function polyRem(f, g) {
  const dg = g.length - 1;
  if (f.length <= dg) return trim(f.slice());
  const r = f.slice();
  const gLead = g[dg];
  for (let k = r.length - 1; k >= dg; k--) {
    const c = r[k];
    if (c === 0n) continue;
    if (gLead !== 1n) {
      if (c % gLead !== 0n) throw new Error('polyRem: 非首一除式且不能整除');
    }
    const q = gLead === 1n ? c : c / gLead;
    for (let j = 0; j <= dg; j++) r[k - dg + j] -= q * g[j];
  }
  r.length = dg;
  return trim(r);
}

function trim(p) {
  let l = p.length;
  while (l > 1 && p[l - 1] === 0n) l--;
  return p.slice(0, l);
}

// 乘积树 + 余数树多点求值（精确）
// mulFn(a,b) -> BigInt[]，默认 multiplyNaive（该规模下最快；见 RESULTS.md 交叉点分析）
function evalFast(coeffs, points, mulFn = multiplyNaive) {
  const m = points.length;
  if (m === 0) return [];
  // 乘积树：叶子是 (x - z_i)
  let level = points.map(z => [-z, 1n]);
  const tree = [level];
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i < level.length; i += 2) {
      next.push(i + 1 < level.length ? trim(mulFn(level[i], level[i + 1])) : level[i]);
    }
    level = next;
    tree.push(level);
  }
  // 余数树
  let rems = [polyRem(coeffs, tree[tree.length - 1][0])];
  for (let h = tree.length - 1; h > 0; h--) {
    const next = [];
    for (let i = 0; i < tree[h].length; i++) {
      const r = rems[i];
      if (2 * i + 1 < tree[h - 1].length) {
        next.push(polyRem(r, tree[h - 1][2 * i]));
        next.push(polyRem(r, tree[h - 1][2 * i + 1]));
      } else {
        next.push(r); // 节点被直接带上层，子节点即自身
      }
    }
    rems = next;
  }
  return rems.map(r => (r.length ? r[0] : 0n));
}

module.exports = { evalNaive, evalFast, polyRem };
