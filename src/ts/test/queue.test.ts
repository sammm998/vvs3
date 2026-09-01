import assert from "node:assert/strict";
import test from "node:test";

import { JobQueue } from "../api/queue.js";
import { looksLikePdf } from "../api/service.js";
import { JobStore } from "../api/store.js";

test("the queue runs tasks one at a time, in submission order", async () => {
  const queue = new JobQueue();
  const order: number[] = [];
  let concurrent = 0;
  let maxConcurrent = 0;
  for (let i = 0; i < 5; i += 1) {
    queue.push(async () => {
      concurrent += 1;
      maxConcurrent = Math.max(maxConcurrent, concurrent);
      await new Promise((resolve) => setTimeout(resolve, 5));
      order.push(i);
      concurrent -= 1;
    });
  }
  await queue.onIdle();
  assert.deepEqual(order, [0, 1, 2, 3, 4]);
  assert.equal(maxConcurrent, 1);
});

test("a failing task does not stall the queue", async () => {
  const queue = new JobQueue();
  const done: string[] = [];
  queue.push(async () => {
    await Promise.resolve();
    throw new Error("boom");
  });
  queue.push(async () => {
    await Promise.resolve();
    done.push("second");
  });
  await queue.onIdle();
  assert.deepEqual(done, ["second"]);
});

test("job ids are content addresses", () => {
  const bytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 1, 2, 3]);
  const a = JobStore.idFor("Plan 1.pdf", bytes);
  const b = JobStore.idFor("Plan 1.pdf", bytes);
  const c = JobStore.idFor("Plan 1.pdf", new Uint8Array([0x25, 0x50, 0x44, 0x46, 9]));
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.match(a, /^Plan_1-[0-9a-f]{16}$/);
});

test("only real PDFs are accepted", () => {
  assert.equal(looksLikePdf(new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d])), true);
  assert.equal(looksLikePdf(new Uint8Array([1, 2, 3, 4, 5])), false);
  assert.equal(looksLikePdf(new Uint8Array()), false);
});
