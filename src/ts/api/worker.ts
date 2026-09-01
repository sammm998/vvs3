/**
 * Python worker adapter.
 *
 * The analysis itself lives in Python; this module only starts it, streams its
 * stage output into the job's progress log and reports the exit status.  It
 * passes RUN_BLIND_TEST=1 and RUN_FORENSICS=1 through so the engine records
 * that no facit was available and exports every intermediate.
 *
 * The ground-truth path is deliberately *not* plumbed through here: the
 * post-hoc comparison is a separate command a human runs afterwards.
 */

import { spawn } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";

export interface WorkerOptions {
  readonly pythonBin: string;
  readonly repoRoot: string;
  readonly inputPdf: string;
  readonly outDir: string;
  readonly forensics: boolean;
  readonly onStage?: (stage: string) => void;
  readonly timeoutMs?: number;
}

export interface WorkerResult {
  readonly ok: boolean;
  readonly code: number | null;
  readonly stdout: string;
  readonly stderr: string;
}

export function defaultPythonBin(repoRoot: string): string {
  const venv = path.join(repoRoot, ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}

export async function runAnalysis(options: WorkerOptions): Promise<WorkerResult> {
  const args = [
    "-m",
    "vvs_pipe.cli",
    "analyse",
    options.inputPdf,
    "--out",
    options.outDir,
    ...(options.forensics ? ["--forensics"] : []),
  ];
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PYTHONPATH: path.join(options.repoRoot, "src", "python"),
    PYTHONHASHSEED: "0",
    RUN_BLIND_TEST: "1",
    ...(options.forensics ? { RUN_FORENSICS: "1" } : {}),
  };

  return await new Promise<WorkerResult>((resolve) => {
    const child = spawn(options.pythonBin, args, { cwd: options.repoRoot, env });
    let stdout = "";
    let stderr = "";
    const timer = options.timeoutMs
      ? setTimeout(() => child.kill("SIGKILL"), options.timeoutMs)
      : null;

    child.stdout.setEncoding("utf8");
    child.stdout.on("data", (chunk: string) => {
      stdout += chunk;
      for (const line of chunk.split("\n")) {
        const trimmed = line.trim();
        if (trimmed) options.onStage?.(trimmed);
      }
    });
    child.stderr.setEncoding("utf8");
    child.stderr.on("data", (chunk: string) => {
      stderr += chunk;
    });
    child.on("error", (err) => {
      if (timer) clearTimeout(timer);
      resolve({ ok: false, code: null, stdout, stderr: `${stderr}${String(err)}` });
    });
    child.on("close", (code) => {
      if (timer) clearTimeout(timer);
      resolve({ ok: code === 0, code, stdout, stderr });
    });
  });
}
