'use strict';

// Worker entry shared by tests.js and bench.js. Each worker reconstructs the
// stack object over the same SharedArrayBuffer and runs one role.

const { LockFreeStack } = require('./stack.js');

function run(spec) {
  const stack = new LockFreeStack(spec.sab, spec.capacity, spec.opts);
  const ctx = stack.newContext(spec.slot);
  const u32 = stack.u32;

  // Start gate: all workers wait until the main thread flips START.
  while (Atomics.load(u32, stack.iStart) === 0) { /* spin */ }

  if (spec.role === 'producer') {
    const { firstId, count } = spec;
    let i = 0;
    try {
      for (; i < count; i++) {
        stack.push(ctx, firstId + i);
        Atomics.add(u32, stack.iPushed, 1);
      }
    } catch (e) {
      // e.g. free list corrupted/exhausted in unsafe mode: report partial progress
      return { pushed: i, firstId, error: String(e) };
    }
    stack.flush(ctx);
    return { pushed: count, firstId };
  }

  if (spec.role === 'consumer') {
    const { total } = spec;
    const seen = spec.seenSab ? new Uint8Array(spec.seenSab) : null;
    const popped = [];
    let localPopped = 0, localDup = 0;
    while (Atomics.load(u32, stack.iPopped) < total &&
           Atomics.load(u32, stack.iStart) === 1) {
      const v = stack.pop(ctx);
      if (v !== null) {
        if (seen) {
          if (Atomics.exchange(seen, v, 1) === 1) localDup++;
        } else {
          popped.push(v);
        }
        localPopped++;
        Atomics.add(u32, stack.iPopped, 1);
      }
    }
    stack.flush(ctx);
    return seen ? { poppedCount: localPopped, dup: localDup } : { popped };
  }

  throw new Error('unknown role: ' + spec.role);
}

module.exports = { run };
