/**
 * HTTP surface.
 *
 * Routes:
 *   POST /api/jobs                        upload a PDF, returns the job
 *   GET  /api/jobs                        list jobs
 *   GET  /api/jobs/:id                    job state and progress
 *   GET  /api/jobs/:id/report             the analysis report
 *   GET  /api/jobs/:id/artifacts/:name    marked.pdf, debug.pdf, quantities.csv, ...
 *   GET  /                                the UI
 */

import { createReadStream, existsSync } from "node:fs";
import http from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { AnalysisService } from "./service.js";
import { ARTIFACT_NAMES } from "./store.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(HERE, "..", "..");
const UI_ROOT = path.resolve(HERE, "..", "ui");

const CONTENT_TYPES: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".pdf": "application/pdf",
  ".csv": "text/csv; charset=utf-8",
};

const MAX_UPLOAD_BYTES = 128 * 1024 * 1024;

export function createServer(service: AnalysisService): http.Server {
  return http.createServer((req, res) => {
    void handle(service, req, res).catch((err: unknown) => {
      send(res, 500, { error: String(err) });
    });
  });
}

async function handle(
  service: AnalysisService,
  req: http.IncomingMessage,
  res: http.ServerResponse,
): Promise<void> {
  const url = new URL(req.url ?? "/", "http://localhost");
  const parts = url.pathname.split("/").filter(Boolean);

  if (req.method === "POST" && url.pathname === "/api/jobs") {
    const bytes = await readBody(req);
    const fileName = String(url.searchParams.get("name") ?? "drawing.pdf");
    try {
      const job = await service.submit(fileName, bytes);
      send(res, 202, job);
    } catch (err) {
      send(res, 400, { error: String(err instanceof Error ? err.message : err) });
    }
    return;
  }

  if (req.method === "GET" && url.pathname === "/api/jobs") {
    send(res, 200, { jobs: await service.list(), queueSize: service.queueSize });
    return;
  }

  const jobId = parts[2];
  if (req.method === "GET" && parts[0] === "api" && parts[1] === "jobs" && jobId) {
    const id: string = jobId;
    if (parts.length === 3) {
      const job = await service.get(id);
      job ? send(res, 200, job) : send(res, 404, { error: "no such job" });
      return;
    }
    if (parts.length === 4 && parts[3] === "report") {
      const report = await service.report(id);
      report ? send(res, 200, report) : send(res, 404, { error: "no report yet" });
      return;
    }
    if (parts.length === 5 && parts[3] === "artifacts") {
      const name = parts[4] ?? "";
      if (!(ARTIFACT_NAMES as readonly string[]).includes(name)) {
        send(res, 404, { error: "unknown artifact" });
        return;
      }
      sendFile(res, path.join(service.store.jobDir(id), name));
      return;
    }
  }

  if (req.method === "GET") {
    const rel = url.pathname === "/" ? "index.html" : url.pathname.replace(/^\/+/, "");
    const file = path.resolve(UI_ROOT, rel);
    if (file.startsWith(UI_ROOT) && existsSync(file)) {
      sendFile(res, file);
      return;
    }
  }

  send(res, 404, { error: "not found" });
}

async function readBody(req: http.IncomingMessage): Promise<Uint8Array> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of req) {
    const buf = chunk as Buffer;
    total += buf.byteLength;
    if (total > MAX_UPLOAD_BYTES) throw new Error("upload too large");
    chunks.push(buf);
  }
  return new Uint8Array(Buffer.concat(chunks));
}

function send(res: http.ServerResponse, status: number, payload: unknown): void {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(body),
  });
  res.end(body);
}

function sendFile(res: http.ServerResponse, file: string): void {
  if (!existsSync(file)) {
    send(res, 404, { error: "not found" });
    return;
  }
  const type = CONTENT_TYPES[path.extname(file)] ?? "application/octet-stream";
  res.writeHead(200, { "content-type": type });
  createReadStream(file).pipe(res);
}

if (process.argv[1] && path.resolve(process.argv[1]) === path.resolve(fileURLToPath(import.meta.url))) {
  const port = Number(process.env.PORT ?? 8080);
  const service = new AnalysisService({
    repoRoot: REPO_ROOT,
    storageRoot: path.join(REPO_ROOT, "artifacts", "jobs"),
  });
  createServer(service).listen(port, () => {
    process.stdout.write(`vvs-pipe listening on http://localhost:${String(port)}\n`);
  });
}
