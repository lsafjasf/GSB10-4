'use strict';

const { Worker, isMainThread, parentPort, workerData } = require('worker_threads');

if (!isMainThread) {
  const res = require('./worker.js').run(workerData);
  parentPort.postMessage(res);
  return;
}

const { LockFreeStack, LockedStack, EMPTY, mulberry32 } = require('./stack.js');

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------
function runWorkers(specs, stack, timeoutMs = 60000) {
  return new Promise((resolve, reject) => {
    const workers = specs.map((spec) => new Worker(__filename, { workerData: spec }));
    let done = 0;
    const results = new Array(specs.length);
    const timer = setTimeout(() => {
      // Abort gate: consumers also stop when START flips to 2.
      Atomics.store(stack.u32, stack.iStart, 2);
      setTimeout(() => reject(new Error('worker timeout (possible starvation from corruption)')), 3000);
    }, timeoutMs);
    workers.forEach((w, i) => {
      w.on('message', (msg) => {
        results[i] = msg;
        if (++done === specs.length) { clearTimeout(timer); resolve(results); }
      });
      w.on('error', reject);
    });
    // release the start gate
    Atomics.store(stack.u32, stack.iStart, 1);
  });
}

// Verify the multiset "popped arrays + whatever is still in the stack" equals
// exactly the expected id set. `expected` defaults to all ids in [0, total);
// pass an explicit Uint8Array when producers may have stopped early.
function checkConservation(stack, total, poppedArrays, label, expected) {
  if (!expected) {
    expected = new Uint8Array(total);
    expected.fill(1);
  }
  const seen = new Uint8Array(total);
  let popped = 0, dup = 0, outOfRange = 0, unexpected = 0;
  for (const arr of poppedArrays) {
    for (const v of arr) {
      if (v < 0 || v >= total) { outOfRange++; continue; }
      if (!expected[v]) unexpected++;
      if (seen[v]) dup++; else seen[v] = 1;
      popped++;
    }
  }
  // drain whatever is left in the stack (guarded against ABA-induced cycles)
  const ctx = stack.newContext(0);
  const rest = [];
  for (let guard = 0; guard < total + 1024; guard++) {
    const v = stack.pop(ctx);
    if (v === EMPTY) break;
    rest.push(v);
  }
  let remaining = 0;
  for (const v of rest) {
    if (v >= 0 && v < total) {
      if (!expected[v]) unexpected++;
      if (seen[v]) dup++; else seen[v] = 1;
      remaining++;
    } else outOfRange++;
  }
  let expectedCount = 0;
  for (let i = 0; i < total; i++) expectedCount += expected[i];
  const found = popped + remaining - dup;
  const lost = expectedCount - found;
  const ok = dup === 0 && lost === 0 && outOfRange === 0 && unexpected === 0;
  console.log(`  [${label}] pushed=${expectedCount} popped=${popped} remaining=${remaining} ` +
    `dup=${dup} lost=${lost} unexpected=${unexpected} outOfRange=${outOfRange} -> ` +
    `${ok ? 'OK (元素守恒)' : 'CONSERVATION VIOLATED'}`);
  return { ok, pushed: expectedCount, popped, remaining, dup, lost, unexpected, outOfRange };
}

function makeSpec(stack, slot, role, extra) {
  return Object.assign({
    sab: stack.sab, capacity: stack.capacity, opts: stack.opts, slot, role,
  }, extra);
}

// ---------------------------------------------------------------------------
// T1: single-threaded interleaved ops vs plain array model
// ---------------------------------------------------------------------------
function testSingleThread() {
  console.log('T1 单线程: 200 万次混合 push/pop 与数组模型逐步对拍');
  const stack = LockFreeStack.create(1_100_000);
  const ctx = stack.newContext(0);
  const model = [];
  const rnd = mulberry32(12345);
  let nextId = 0, checks = 0;
  for (let i = 0; i < 2_000_000; i++) {
    if (rnd() < 0.5 || model.length === 0) {
      const v = nextId++;
      stack.push(ctx, v);
      model.push(v);
    } else {
      const got = stack.pop(ctx);
      const want = model.pop();
      if (got !== want) {
        console.log(`  mismatch at op ${i}: got ${got}, want ${want}`);
        return false;
      }
      checks++;
    }
    if (i % 200_000 === 0) stack.flush(ctx);
  }
  stack.flush(ctx);
  const rest = stack.drainRemaining(ctx, []);
  const ok = rest.length === model.length && rest.every((v, i) => v === model[model.length - 1 - i]);
  console.log(`  pop 比对 ${checks} 次, 栈底排空 ${rest.length} 个, 顺序${ok ? '一致' : '不一致'}`);
  return ok && checks > 0;
}

// ---------------------------------------------------------------------------
// T2: differential vs LockedStack on a seeded random op sequence
// ---------------------------------------------------------------------------
function testDifferential() {
  console.log('T2 对拍: 与加锁栈跑同一随机操作序列 (seed=20261005, 100 万操作)');
  const stack = LockFreeStack.create(600_000);
  const ctx = stack.newContext(0);
  const locked = new LockedStack();
  const rnd = mulberry32(20261005);
  let pushes = 0, pops = 0;
  for (let i = 0; i < 1_000_000; i++) {
    if (rnd() < 0.5 || locked.size() === 0) {
      const v = (rnd() * 0x7fffffff) | 0;
      stack.push(ctx, v);
      locked.push(v);
      pushes++;
    } else {
      const a = stack.pop(ctx);
      const b = locked.pop();
      pops++;
      if (a !== b) {
        console.log(`  mismatch at op ${i}: lock-free=${a}, locked=${b}`);
        return false;
      }
    }
    if (i % 100_000 === 0) stack.flush(ctx);
  }
  stack.flush(ctx);
  // final contents and order must be identical
  let orderOk = true, n = 0;
  for (;;) {
    const a = stack.pop(ctx);
    const b = locked.pop();
    if (a === EMPTY && b === EMPTY) break;
    if (a !== b) { orderOk = false; break; }
    n++;
  }
  console.log(`  push=${pushes} pop=${pops} 逐值比对全部一致; 最终栈内容+顺序一致 (${n} 项) -> ${orderOk ? 'OK' : 'FAILED'}`);
  return orderOk;
}

// ---------------------------------------------------------------------------
// concurrent scenario runner
// ---------------------------------------------------------------------------
async function concurrentScenario(label, { producers, consumers, perProducer, opts }) {
  const total = producers * perProducer;
  const capacity = total + 64 * 200; // live nodes + retired slack
  const stack = LockFreeStack.create(capacity, opts);
  stack.opts = opts;
  const specs = [];
  let slot = 1;
  for (let p = 0; p < producers; p++) {
    specs.push(makeSpec(stack, slot++, 'producer', { firstId: p * perProducer, count: perProducer }));
  }
  for (let c = 0; c < consumers; c++) {
    specs.push(makeSpec(stack, slot++, 'consumer', { total }));
  }
  const t0 = performance.now();
  const results = await runWorkers(specs, stack);
  const ms = performance.now() - t0;
  const poppedArrays = results.filter((r) => r && r.popped).map((r) => r.popped);
  const stats = checkConservation(stack, total, poppedArrays, label);
  stats.ms = ms;
  return stats;
}

// ---------------------------------------------------------------------------
async function main() {
  let allOk = true;

  allOk &= testSingleThread();
  allOk &= testDifferential();

  // T3 SPSC
  const spsc = await concurrentScenario('T3 SPSC 1P1C x 50万', {
    producers: 1, consumers: 1, perProducer: 500_000, opts: {},
  });
  allOk &= spsc.ok;

  // T4 MPMC
  const mpmc = await concurrentScenario('T4 MPMC 4P4C x 100万', {
    producers: 4, consumers: 4, perProducer: 250_000, opts: {},
  });
  allOk &= mpmc.ok;

  // T5 tag wraparound: 2-bit tag wraps every 4 head updates; EBR must still hold
  const wrap = await concurrentScenario('T5 版本回绕 tagBits=2, 4P4C x 100万 (EBR 开)', {
    producers: 4, consumers: 4, perProducer: 250_000, opts: { tagBits: 2, spin: 20 },
  });
  allOk &= wrap.ok;

  // T6 unsafe mode: immediate reuse + tiny tag -> expect conservation to break
  console.log('T6 对照实验: 立即回收(无EBR) + tagBits=2, 预期出现丢失/重复以证明回收时机必要性');
  {
    const producers = 4, consumers = 4, perProducer = 100_000;
    const total = producers * perProducer;
    const opts = { tagBits: 2, ebr: false, spin: 200 };
    const stack = LockFreeStack.create(total + 64 * 200, opts);
    const specs = [];
    let slot = 1;
    for (let p = 0; p < producers; p++) {
      specs.push(makeSpec(stack, slot++, 'producer', { firstId: p * perProducer, count: perProducer }));
    }
    for (let c = 0; c < consumers; c++) {
      specs.push(makeSpec(stack, slot++, 'consumer', { total }));
    }
    let results;
    try {
      results = await runWorkers(specs, stack, 30000);
    } catch (e) {
      console.log(`  工作线程异常终止 (损坏表现之一): ${e.message}`);
      results = null;
    }
    if (results) {
      // rebuild the actually-pushed id set (producers may have died mid-way)
      const expected = new Uint8Array(total);
      let pushedActual = 0, prodErrors = 0;
      for (const r of results) {
        if (r && r.pushed !== undefined) {
          if (r.error) prodErrors++;
          for (let i = 0; i < r.pushed; i++) expected[r.firstId + i] = 1;
          pushedActual += r.pushed;
        }
      }
      const poppedArrays = results.filter((r) => r && r.popped).map((r) => r.popped);
      const stats = checkConservation(stack, total, poppedArrays,
        'T6 无EBR tagBits=2, 4P4C', expected);
      if (prodErrors) console.log(`  生产者异常(空闲链表被ABA破坏): ${prodErrors} 个`);
      if (stats.ok) console.log('  注意: 本次未复现损坏 (竞态是概率性的), 请加大 spin 或重跑');
    }
  }

  console.log(allOk ? '\n全部正确性测试通过' : '\n存在失败用例');
  process.exit(allOk ? 0 : 1);
}

main().catch((e) => { console.error(e); process.exit(1); });
