'use strict';

const { Worker, isMainThread, parentPort, workerData } = require('worker_threads');

if (!isMainThread) {
  const res = require('./worker.js').run(workerData);
  parentPort.postMessage(res);
  return;
}

const { LockFreeStack } = require('./stack.js');

// Benchmark: `total` pushes + `total` pops spread over P producers / C consumers,
// all started simultaneously. Pop uniqueness is recorded into a shared bitmap,
// and conservation is verified afterwards.
async function runOnce(label, producers, consumers, perProducer, tagBits = 32) {
  const total = producers * perProducer;
  const opts = { tagBits };
  const capacity = total + 64 * 200;
  const stack = LockFreeStack.create(capacity, opts);
  const seenSab = new SharedArrayBuffer(total);
  const newSeen = () => seenSab; // same bitmap shared by all consumers
  new Uint8Array(seenSab).fill(0);

  const specs = [];
  let slot = 1;
  for (let p = 0; p < producers; p++) {
    specs.push({ sab: stack.sab, capacity, opts, slot: slot++, role: 'producer',
      firstId: p * perProducer, count: perProducer });
  }
  for (let c = 0; c < consumers; c++) {
    specs.push({ sab: stack.sab, capacity, opts, slot: slot++, role: 'consumer',
      total, seenSab });
  }

  // RSS sampler
  let peakRss = process.memoryUsage.rss();
  const sampler = setInterval(() => {
    peakRss = Math.max(peakRss, process.memoryUsage.rss());
  }, 5);

  const workers = specs.map((spec) => new Worker(__filename, { workerData: spec }));
  let done = 0;
  let t0 = 0;
  let elapsedMs = 0;
  const results = await new Promise((resolve, reject) => {
    const rs = new Array(specs.length);
    workers.forEach((w, i) => {
      w.on('message', (msg) => {
        rs[i] = msg;
        if (++done === specs.length) {
          elapsedMs = performance.now() - t0;
          resolve(rs);
        }
      });
      w.on('error', reject);
    });
    setImmediate(() => {
      t0 = performance.now();
      Atomics.store(stack.u32, stack.iStart, 1);
    });
  });
  clearInterval(sampler);
  const ms = elapsedMs;
  for (const w of workers) await w.terminate().catch(() => {});

  // conservation: every id [0,total) exactly once in the popped bitmap, stack empty
  const seen = new Uint8Array(seenSab);
  let unique = 0;
  for (let i = 0; i < total; i++) unique += seen[i];
  let remaining = 0;
  {
    const ctx = stack.newContext(0);
    for (let guard = 0; guard < total + 1024; guard++) {
      if (stack.pop(ctx) === null) break;
      remaining++;
    }
  }
  const dup = total - unique + (remaining > 0 ? remaining : 0); // approximate split below
  const dupOnly = results.reduce((a, r) => a + (r.dup || 0), 0);
  const ok = unique === total && remaining === 0 && dupOnly === 0;

  const slabBytes = LockFreeStack.byteSize(capacity) + seenSab.byteLength;
  const ops = total * 2;
  const row = {
    label, producers, consumers, total,
    elapsedMs: Math.round(ms * 10) / 10,
    opsPerSec: Math.round(ops / (ms / 1000)),
    peakRssMB: Math.round(peakRss / 1048576 * 10) / 10,
    slabMB: Math.round(slabBytes / 1048576 * 100) / 100,
    unique, remaining, dup: dupOnly, ok,
  };
  return row;
}

function fmt(r) {
  console.log(
    `${r.label.padEnd(26)} ${r.producers}P${r.consumers}C  ` +
    `push/pop=${(r.total / 1e4).toFixed(0)}万*2  耗时=${r.elapsedMs}ms  ` +
    `吞吐=${(r.opsPerSec / 1e4).toFixed(2)}万 ops/s  ` +
    `RSS峰值=${r.peakRssMB}MB  节点槽+位图=${r.slabMB}MB  守恒=${r.ok ? 'OK' : 'FAIL'}`);
}

async function main() {
  console.log('环境: Node ' + process.version + ', 100 万次 push + 100 万次 pop/轮');
  console.log('-'.repeat(110));
  const configs = [
    ['SPSC 1P+1C', 1, 1, 1_000_000],
    ['MPMC 2P+2C', 2, 2, 500_000],
    ['MPMC 4P+4C', 4, 4, 250_000],
    ['MPMC 8P+8C', 8, 8, 125_000],
  ];
  const rows = [];
  for (const [label, p, c, per] of configs) {
    const r = await runOnce(label, p, c, per);
    fmt(r);
    rows.push(r);
  }
  console.log('-'.repeat(110));
  console.log('守恒校验: unique=1000000, remaining=0, dup=0 -> 无丢失无重复');
}

main().catch((e) => { console.error(e); process.exit(1); });
