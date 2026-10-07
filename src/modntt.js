'use strict';
// 模 p 快速多点求值：NTT 乘法 + 乘积树/余数树，展示真正的亚二次加速。
// p = 998244353 = 119*2^23+1，原根 g=3，Number 精确乘法（p^2 < 2^60 用 BigInt 取模）。

const P = 998244353n, G = 3n, P_NUM = 998244353;

function powmod(a, e) {
  let r = 1n, b = a % P;
  while (e > 0n) { if (e & 1n) r = r * b % P; b = b * b % P; e >>= 1n; }
  return r;
}
const P_INV = powmod(P, 1n); // 占位不用

function ntt(a, invert) {
  const n = a.length;
  for (let i = 1, j = 0; i < n; i++) {
    let bit = n >> 1;
    for (; j & bit; bit >>= 1) j ^= bit;
    j ^= bit;
    if (i < j) { const t = a[i]; a[i] = a[j]; a[j] = t; }
  }
  for (let len = 2; len <= n; len <<= 1) {
    const wlen = powmod(G, (P - 1n) / BigInt(len) * (invert ? P - 2n : 1n) % (P - 1n));
    for (let i = 0; i < n; i += len) {
      let w = 1n;
      for (let j = 0; j < len / 2; j++) {
        const u = a[i + j], v = a[i + j + len / 2] * w % P;
        a[i + j] = (u + v) % P;
        a[i + j + len / 2] = (u - v + P) % P;
        w = w * wlen % P;
      }
    }
  }
  if (invert) {
    const ninv = powmod(BigInt(n), P - 2n);
    for (let i = 0; i < n; i++) a[i] = a[i] * ninv % P;
  }
}

function nttMultiply(a, b) {
  const need = a.length + b.length - 1;
  let n = 1; while (n < need) n <<= 1;
  const fa = a.concat(new Array(n - a.length).fill(0n));
  const fb = b.concat(new Array(n - b.length).fill(0n));
  ntt(fa, false); ntt(fb, false);
  for (let i = 0; i < n; i++) fa[i] = fa[i] * fb[i] % P;
  ntt(fa, true);
  fa.length = need;
  return fa;
}

function trimMod(p) {
  let l = p.length;
  while (l > 1 && p[l - 1] === 0n) l--;
  return p.slice(0, l);
}

// 模 p 多项式乘法：小规模朴素，大规模 NTT
function mulMod(a, b) {
  if (a.length * b.length <= 4096) {
    const out = new Array(a.length + b.length - 1).fill(0n);
    for (let i = 0; i < a.length; i++) {
      if (a[i] === 0n) continue;
      for (let j = 0; j < b.length; j++) out[i + j] = (out[i + j] + a[i] * b[j]) % P;
    }
    return out;
  }
  return trimMod(nttMultiply(a, b));
}

// 模 x^k 乘法逆（牛顿迭代，要求 g[0] 可逆），O(M(k))
function polyInvMod(g, k) {
  let inv = [powmod(g[0], P - 2n)];
  let len = 1;
  while (len < k) {
    const len2 = Math.min(2 * len, k);
    const gCut = g.slice(0, len2);
    const t = trimMod(mulMod(gCut, inv)); // g * inv
    const corr = new Array(len2).fill(0n);
    for (let i = 0; i < Math.min(t.length, len2); i++) corr[i] = (P - t[i]) % P;
    corr[0] = (corr[0] + 2n) % P; // 2 - g*inv
    inv = trimMod(mulMod(corr, inv)).slice(0, len2);
    while (inv.length < len2) inv.push(0n);
    len = len2;
  }
  return inv.slice(0, k);
}

// 快速除法：f = q*g + r，基于反转多项式 + 牛顿迭代，O(M(n))
function divModFast(f, g) {
  const df = f.length - 1, dg = g.length - 1;
  if (df < dg) return { q: [0n], r: trimMod(f.slice()) };
  const k = df - dg + 1;
  const revF = f.slice().reverse();
  const revG = g.slice().reverse();
  const invG = polyInvMod(revG, k);
  const qRev = trimMod(mulMod(revF.slice(0, k), invG)).slice(0, k);
  const q = trimMod(qRev.reverse());
  const qg = mulMod(q, g);
  const rl = Math.max(f.length, qg.length);
  const r = new Array(rl).fill(0n);
  for (let i = 0; i < f.length; i++) r[i] = (r[i] + f[i]) % P;
  for (let i = 0; i < qg.length; i++) r[i] = (r[i] - qg[i] + P) % P;
  return { q, r: trimMod(r.slice(0, Math.max(dg, 1))) };
}

// 模 p 多项式除法取余：小规模 schoolbook，大规模牛顿迭代快速除法
function remMod(f, g) {
  const dg = g.length - 1;
  if (f.length > dg && (f.length - dg) * dg > 8192) return divModFast(f, g).r;
  return remModSchool(f, g);
}

function remModSchool(f, g) {
  const dg = g.length - 1;
  if (f.length <= dg) return trimMod(f.slice());
  const r = f.slice();
  const gLeadInv = powmod(g[dg], P - 2n);
  for (let k = r.length - 1; k >= dg; k--) {
    if (r[k] === 0n) continue;
    const q = r[k] * gLeadInv % P;
    for (let j = 0; j <= dg; j++) r[k - dg + j] = (r[k - dg + j] - q * g[j] % P + P) % P;
  }
  r.length = dg;
  return trimMod(r);
}

// 模 p 逐点 Horner
function evalModNaive(coeffs, points) {
  return points.map(x => {
    let acc = 0n;
    for (let i = coeffs.length - 1; i >= 0; i--) acc = (acc * x + coeffs[i]) % P;
    return acc;
  });
}

// 模 p 乘积树 + 余数树快速多点求值
function evalModFast(coeffs, points) {
  const m = points.length;
  if (m === 0) return [];
  let level = points.map(z => [(P - z % P + P) % P, 1n]);
  const tree = [level];
  while (level.length > 1) {
    const next = [];
    for (let i = 0; i < level.length; i += 2) {
      next.push(i + 1 < level.length ? mulMod(level[i], level[i + 1]) : level[i]);
    }
    level = next;
    tree.push(level);
  }
  let rems = [remMod(coeffs.map(c => (c % P + P) % P), tree[tree.length - 1][0])];
  for (let h = tree.length - 1; h > 0; h--) {
    const next = [];
    for (let i = 0; i < tree[h].length; i++) {
      const r = rems[i];
      if (2 * i + 1 < tree[h - 1].length) {
        next.push(remMod(r, tree[h - 1][2 * i]));
        next.push(remMod(r, tree[h - 1][2 * i + 1]));
      } else {
        next.push(r);
      }
    }
    rems = next;
  }
  return rems.map(r => (r.length ? r[0] : 0n));
}

module.exports = { P: P_NUM, ntt, nttMultiply, mulMod, remMod, remModSchool, divModFast, polyInvMod, evalModNaive, evalModFast, powmod };
