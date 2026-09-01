/**
 * Artifact storage and job persistence.
 *
 * One directory per job holds the uploaded PDF and everything the Python
 * engine wrote.  Jobs are written to disk as JSON so a restart does not lose
 * a completed analysis.
 */

import { createHash } from "node:crypto";
import { mkdir, readFile, readdir, writeFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import path from "node:path";

import type { Job, JobProgress, JobState } from "../domain/types.js";

export const ARTIFACT_NAMES = [
  "analysis.json",
  "forensics.json",
  "marked.pdf",
  "debug.pdf",
  "quantities.csv",
] as const;

export class JobStore {
  readonly root: string;

  constructor(root: string) {
    this.root = path.resolve(root);
  }

  jobDir(id: string): string {
    return path.join(this.root, id);
  }

  /** Content-addressed job id: the same PDF analysed twice reuses its results. */
  static idFor(fileName: string, bytes: Uint8Array): string {
    const hash = createHash("sha256").update(bytes).digest("hex").slice(0, 16);
    const stem = path.basename(fileName, path.extname(fileName)).replace(/[^A-Za-z0-9_-]/g, "_");
    return `${stem || "drawing"}-${hash}`;
  }

  async create(id: string, fileName: string, bytes: Uint8Array): Promise<Job> {
    const dir = this.jobDir(id);
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, "input.pdf"), bytes);
    const job: Job = {
      id,
      fileName,
      state: "queued",
      createdAt: new Date().toISOString(),
      finishedAt: null,
      progress: [],
      error: null,
      artifacts: {},
    };
    await this.save(job);
    return job;
  }

  async save(job: Job): Promise<void> {
    const dir = this.jobDir(job.id);
    await mkdir(dir, { recursive: true });
    await writeFile(path.join(dir, "job.json"), JSON.stringify(job, null, 2), "utf8");
  }

  async load(id: string): Promise<Job | null> {
    const file = path.join(this.jobDir(id), "job.json");
    if (!existsSync(file)) return null;
    return JSON.parse(await readFile(file, "utf8")) as Job;
  }

  async list(): Promise<readonly Job[]> {
    if (!existsSync(this.root)) return [];
    const entries = await readdir(this.root, { withFileTypes: true });
    const jobs: Job[] = [];
    for (const entry of entries) {
      if (!entry.isDirectory()) continue;
      const job = await this.load(entry.name);
      if (job) jobs.push(job);
    }
    jobs.sort((a, b) => (a.createdAt < b.createdAt ? 1 : a.createdAt > b.createdAt ? -1 : 0));
    return jobs;
  }

  async discoverArtifacts(id: string): Promise<Record<string, string>> {
    const dir = this.jobDir(id);
    const out: Record<string, string> = {};
    for (const name of ARTIFACT_NAMES) {
      if (existsSync(path.join(dir, name))) out[name] = `/api/jobs/${id}/artifacts/${name}`;
    }
    return out;
  }

  withProgress(job: Job, stage: string): Job {
    const progress: JobProgress[] = [...job.progress, { stage, at: new Date().toISOString() }];
    return { ...job, progress };
  }

  withState(job: Job, state: JobState, error: string | null = null): Job {
    return {
      ...job,
      state,
      error,
      finishedAt: state === "succeeded" || state === "failed" ? new Date().toISOString() : null,
    };
  }
}
