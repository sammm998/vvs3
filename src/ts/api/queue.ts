/**
 * Single-worker job queue.
 *
 * Analyses are CPU-bound and deterministic, so they are run one at a time in
 * submission order.  That keeps the machine responsive and, more importantly,
 * means two concurrent runs can never interleave writes into the same
 * artifact directory.
 */

export type Task = () => Promise<void>;

export class JobQueue {
  #pending: Task[] = [];
  #running = false;
  #idleWaiters: (() => void)[] = [];

  get size(): number {
    return this.#pending.length + (this.#running ? 1 : 0);
  }

  push(task: Task): void {
    this.#pending.push(task);
    void this.#drain();
  }

  async onIdle(): Promise<void> {
    if (!this.#running && this.#pending.length === 0) return;
    await new Promise<void>((resolve) => this.#idleWaiters.push(resolve));
  }

  async #drain(): Promise<void> {
    if (this.#running) return;
    this.#running = true;
    try {
      while (this.#pending.length > 0) {
        const task = this.#pending.shift();
        if (!task) break;
        try {
          await task();
        } catch {
          // A task is responsible for recording its own failure on the job;
          // the queue must keep draining regardless.
        }
      }
    } finally {
      this.#running = false;
      const waiters = this.#idleWaiters;
      this.#idleWaiters = [];
      for (const resolve of waiters) resolve();
    }
  }
}
