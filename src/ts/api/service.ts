/**
 * Analysis service: the orchestration the HTTP layer is a thin shell over.
 *
 * Kept separate from the server so it can be driven directly from tests
 * without binding a socket.
 */

import { readFile } from "node:fs/promises";
import path from "node:path";

import type { AnalysisReport, Job } from "../domain/types.js";
import { JobQueue } from "./queue.js";
import { JobStore } from "./store.js";
import { defaultPythonBin, runAnalysis } from "./worker.js";

export interface ServiceOptions {
  readonly repoRoot: string;
  readonly storageRoot: string;
  readonly pythonBin?: string;
  readonly forensics?: boolean;
  readonly timeoutMs?: number;
}

export class AnalysisService {
  readonly store: JobStore;
  readonly #queue = new JobQueue();
  readonly #options: ServiceOptions;

  constructor(options: ServiceOptions) {
    this.#options = options;
    this.store = new JobStore(options.storageRoot);
  }

  get queueSize(): number {
    return this.#queue.size;
  }

  async submit(fileName: string, bytes: Uint8Array): Promise<Job> {
    if (bytes.byteLength === 0) throw new Error("empty upload");
    if (!looksLikePdf(bytes)) throw new Error("not a PDF");

    const id = JobStore.idFor(fileName, bytes);
    const existing = await this.store.load(id);
    if (existing && existing.state === "succeeded") return existing;

    const job = await this.store.create(id, fileName, bytes);
    this.#queue.push(async () => {
      await this.#run(id);
    });
    return job;
  }

  async waitForIdle(): Promise<void> {
    await this.#queue.onIdle();
  }

  async get(id: string): Promise<Job | null> {
    return await this.store.load(id);
  }

  async list(): Promise<readonly Job[]> {
    return await this.store.list();
  }

  async report(id: string): Promise<AnalysisReport | null> {
    const file = path.join(this.store.jobDir(id), "analysis.json");
    try {
      return JSON.parse(await readFile(file, "utf8")) as AnalysisReport;
    } catch {
      return null;
    }
  }

  async #run(id: string): Promise<void> {
    let job = await this.store.load(id);
    if (!job) return;
    job = this.store.withState(this.store.withProgress(job, "starting"), "running");
    await this.store.save(job);

    const dir = this.store.jobDir(id);
    const python = this.#options.pythonBin ?? defaultPythonBin(this.#options.repoRoot);
    const result = await runAnalysis({
      pythonBin: python,
      repoRoot: this.#options.repoRoot,
      inputPdf: path.join(dir, "input.pdf"),
      outDir: dir,
      forensics: this.#options.forensics ?? true,
      ...(this.#options.timeoutMs === undefined ? {} : { timeoutMs: this.#options.timeoutMs }),
      onStage: (stage) => {
        job = job ? this.store.withProgress(job, stage) : job;
      },
    });

    const artifacts = await this.store.discoverArtifacts(id);
    const current = job ?? (await this.store.load(id));
    if (!current) return;
    const withArtifacts: Job = { ...current, artifacts };
    await this.store.save(
      this.store.withState(
        withArtifacts,
        result.ok ? "succeeded" : "failed",
        result.ok ? null : result.stderr.trim().slice(-4000) || `exit ${String(result.code)}`,
      ),
    );
  }
}

export function looksLikePdf(bytes: Uint8Array): boolean {
  return (
    bytes.byteLength > 4 &&
    bytes[0] === 0x25 &&
    bytes[1] === 0x50 &&
    bytes[2] === 0x44 &&
    bytes[3] === 0x46
  );
}
