'use strict';

// ============================================================================
// ABA-safe lock-free Treiber stack (Node.js, stdlib only)
//
// Shared memory layout (one SharedArrayBuffer):
//   offset 0  u64 HEAD    tagged stack head   = (tag << 32) | (nodeIndex + 1), 0 = null
//   offset 8  u64 FREE    tagged free-list head, same encoding
//   offset 16 u32 EPOCH   global epoch in {0,1,2} for epoch-based reclamation
//   offset 20 u32 PUSHED  shared push counter (benchmarks)
//   offset 24 u32 POPPED  shared pop counter (benchmarks)
//   offset 28 u32 START   start gate for workers
//   offset 32 u32 slot[64] per-worker EBR announce words: 0=inactive,
//                         (epoch << 1) | 1 = active in that epoch
//   node area: per node two int32 words: [value, next(index+1, 0=null)]
//
// Every word is touched through Atomics (sequentially consistent), so the
// tagged 64-bit head is a true hardware CAS (Atomics.compareExchange on
// BigUint64Array).
// ============================================================================

const MAX_WORKERS = 64;

const OFF_HEAD = 0;            // u64
const OFF_FREE = 8;            // u64
const OFF_EPOCH = 16;          // u32
const OFF_PUSHED = 20;         // u32
const OFF_POPPED = 24;         // u32
const OFF_START = 28;          // u32
const OFF_SLOTS = 32;          // u32[MAX_WORKERS]
const CTRL_BYTES = OFF_SLOTS + 4 * MAX_WORKERS; // 288
const NODE_I32 = 2;            // int32 words per node
const SCAN_LIMIT = 128;        // retire this many nodes before trying an EBR scan

const EMPTY = null;            // pop() result for an empty stack

class LockFreeStack {
  // options:
  //   tagBits: 1..32, width of the version tag inside the 64-bit head (default 32)
  //   ebr:      true = epoch-based delayed reclamation (default), false = immediate reuse
  //   spin:     0..N, test hook: busy-spin between reading head.next and the CAS,
  //             widens the ABA race window
  constructor(sab, capacity, opts = {}) {
    this.sab = sab;
    this.capacity = capacity;
    this.tagBits = Math.max(1, Math.min(32, opts.tagBits ?? 32));
    this.tagMask = this.tagBits >= 32 ? 0xffffffff : (1 << this.tagBits) - 1;
    this.ebr = opts.ebr !== false;
    this.spin = opts.spin | 0;
    this.opts = opts;

    this.u64 = new BigUint64Array(sab, 0, 2);
    this.u32 = new Uint32Array(sab);
    this.nodes = new Int32Array(sab, CTRL_BYTES, capacity * NODE_I32);

    this.iEpoch = OFF_EPOCH >>> 2;
    this.iPushed = OFF_PUSHED >>> 2;
    this.iPopped = OFF_POPPED >>> 2;
    this.iStart = OFF_START >>> 2;
    this.iSlot0 = OFF_SLOTS >>> 2;
  }

  static byteSize(capacity) {
    return CTRL_BYTES + capacity * NODE_I32 * 4;
  }

  static create(capacity, opts = {}) {
    const sab = new SharedArrayBuffer(LockFreeStack.byteSize(capacity));
    const s = new LockFreeStack(sab, capacity, opts);
    s.reset();
    return s;
  }

  reset() {
    // Thread every node onto the free list first.
    for (let i = 0; i < this.capacity; i++) {
      this.setNext(i, i + 1 < this.capacity ? i + 2 : 0); // store index+1
    }
    Atomics.store(this.u64, 0, 0n);                     // stack empty
    Atomics.store(this.u64, 1, this.pack(0, 1));        // free list: node 0
    Atomics.store(this.u32, this.iEpoch, 0);
    Atomics.store(this.u32, this.iPushed, 0);
    Atomics.store(this.u32, this.iPopped, 0);
    Atomics.store(this.u32, this.iStart, 0);
    for (let s = 0; s < MAX_WORKERS; s++) {
      Atomics.store(this.u32, this.iSlot0 + s, 0);
    }
  }

  pack(tag, indexPlusOne) {
    return (BigInt(tag & this.tagMask) << 32n) | BigInt(indexPlusOne >>> 0);
  }
  static tagOf(word) { return Number(word >> 32n); }
  static idxOf(word) { return Number(word & 0xffffffffn); }

  getValue(i) { return Atomics.load(this.nodes, i * 2); }
  setValue(i, v) { Atomics.store(this.nodes, i * 2, v | 0); }
  getNext(i) { return Atomics.load(this.nodes, i * 2 + 1); }
  setNext(i, v) { Atomics.store(this.nodes, i * 2 + 1, v >>> 0); }

  // ---- EBR per-thread context ---------------------------------------------
  newContext(slot) {
    return { slot, epoch: 0, bags: [[], [], []], retired: 0 };
  }

  // Enter a critical section: announce the current epoch as one atomic word.
  enter(ctx) {
    const e = Atomics.load(this.u32, this.iEpoch);
    ctx.epoch = e;
    Atomics.store(this.u32, this.iSlot0 + ctx.slot, (e << 1) | 1);
  }

  exit(ctx) {
    Atomics.store(this.u32, this.iSlot0 + ctx.slot, 0);
  }

  // Reclaim every local retired node once all threads have passed a quiescent
  // point. Classic 3-epoch EBR:
  //   * global epoch may advance only when every active thread is announced in it;
  //   * a node retired in epoch r is freed only after the global epoch has moved
  //     to (r+2) mod 3 and no active thread is still announced in the "safe"
  //     epoch (r == (g+1) mod 3 at that moment).
  scan(ctx) {
    const g = Atomics.load(this.u32, this.iEpoch);
    let allInG = true;
    for (let s = 0; s < MAX_WORKERS; s++) {
      const w = Atomics.load(this.u32, this.iSlot0 + s);
      if ((w & 1) && (w >>> 1) !== g) { allInG = false; break; }
    }
    if (allInG) {
      Atomics.compareExchange(this.u32, this.iEpoch, g, (g + 1) % 3);
    }
    const g2 = Atomics.load(this.u32, this.iEpoch);
    const safe = (g2 + 1) % 3; // bag retired two epochs ago
    for (let s = 0; s < MAX_WORKERS; s++) {
      const w = Atomics.load(this.u32, this.iSlot0 + s);
      if ((w & 1) && (w >>> 1) === safe) return; // a late or stale announcer: wait
    }
    const bag = ctx.bags[safe];
    if (bag.length) {
      for (const node of bag) this.freeNode(node); // <-- the ONLY real release
      ctx.retired -= bag.length;
      bag.length = 0;
    }
  }

  // Worker shutdown: keep scanning (outside any critical section) until every
  // locally retired node has been handed back. With no stragglers the epoch
  // advances every scan, so at most 3 scans suffice.
  flush(ctx) {
    let guard = 0;
    while (ctx.retired > 0 && guard++ < 64) this.scan(ctx);
  }

  // ---- allocator: tagged free list ----------------------------------------
  alloc() {
    for (;;) {
      const f = Atomics.load(this.u64, 1);
      const fi = LockFreeStack.idxOf(f);
      if (fi === 0) throw new Error('node slab exhausted');
      const node = fi - 1;
      const nxt = this.getNext(node);
      const desired = this.pack(LockFreeStack.tagOf(f) + 1, nxt);
      if (Atomics.compareExchange(this.u64, 1, f, desired) === f) return node;
    }
  }

  freeNode(node) {
    for (;;) {
      const f = Atomics.load(this.u64, 1);
      this.setNext(node, LockFreeStack.idxOf(f));
      const desired = this.pack(LockFreeStack.tagOf(f) + 1, node + 1);
      if (Atomics.compareExchange(this.u64, 1, f, desired) === f) return;
    }
  }

  // ---- Treiber stack -------------------------------------------------------
  push(ctx, value) {
    this.enter(ctx);
    const node = this.alloc();
    this.setValue(node, value);
    let head = Atomics.load(this.u64, 0);
    for (;;) {
      this.setNext(node, LockFreeStack.idxOf(head));
      const desired = this.pack(LockFreeStack.tagOf(head) + 1, node + 1);
      if (Atomics.compareExchange(this.u64, 0, head, desired) === head) break;
      head = Atomics.load(this.u64, 0);
    }
    this.exit(ctx);
  }

  pop(ctx) {
    this.enter(ctx);
    for (;;) {
      const head = Atomics.load(this.u64, 0);
      const headIdx = LockFreeStack.idxOf(head);
      if (headIdx === 0) { this.exit(ctx); return EMPTY; }
      const node = headIdx - 1;
      const nextIdx = this.getNext(node);
      if (this.spin) { // test hook: delay right inside the ABA window
        const stop = this.spin;
        for (let k = 0; k < stop; k++) { /* busy */ }
      }
      const desired = this.pack(LockFreeStack.tagOf(head) + 1, nextIdx);
      if (Atomics.compareExchange(this.u64, 0, head, desired) === head) {
        const value = this.getValue(node);
        this.retire(ctx, node);
        this.exit(ctx);
        return value;
      }
    }
  }

  // Pop succeeded: node is logically dead but may still be referenced by other
  // threads that read it as head. Hand it to EBR (or, in the deliberately
  // unsafe mode, push it straight back to the free list = immediate reuse).
  retire(ctx, node) {
    if (!this.ebr) { this.freeNode(node); return; }
    ctx.bags[ctx.epoch].push(node);
    ctx.retired++;
    if (ctx.retired >= SCAN_LIMIT) this.scan(ctx);
  }

  drainRemaining(ctx, out) {
    for (;;) {
      const v = this.pop(ctx);
      if (v === EMPTY) return out;
      out.push(v);
    }
  }
}

// ---------------------------------------------------------------------------
// Lock-based reference implementation used for differential testing.
// A real spinlock (Atomics on an Int32Array) guards a plain array stack.
// ---------------------------------------------------------------------------
class LockedStack {
  constructor() {
    this.lockWord = new Int32Array(new SharedArrayBuffer(4));
    this.items = [];
  }
  acquire() {
    while (Atomics.compareExchange(this.lockWord, 0, 0, 1) !== 0) {
      // spin
    }
  }
  release() { Atomics.store(this.lockWord, 0, 0); }
  push(v) {
    this.acquire();
    this.items.push(v);
    this.release();
  }
  pop() {
    this.acquire();
    const v = this.items.length ? this.items.pop() : EMPTY;
    this.release();
    return v;
  }
  size() {
    this.acquire();
    const n = this.items.length;
    this.release();
    return n;
  }
}

// Deterministic PRNG so random differential sequences are reproducible.
function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0; a = (a + 0x6D2B79F5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

module.exports = { LockFreeStack, LockedStack, EMPTY, MAX_WORKERS, mulberry32 };
