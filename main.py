"""Deployable web service for the pipe analysis engine.

Railpack (and most zero-config PaaS builders) detect this repository as a
Python project and look for ``main.py`` in the root, so this is the deployment
entry point.  It is deliberately dependency-free beyond the analysis engine
itself - the standard library's HTTP server plus a single background worker -
so the container needs no Node build to run.

    python main.py            # PORT defaults to 8080

Endpoints:
    GET  /                                  the UI
    POST /api/jobs?name=<file>              upload a PDF, returns the job
    GET  /api/jobs                          list jobs
    GET  /api/jobs/<id>                     job state and progress
    GET  /api/jobs/<id>/report              the analysis report
    GET  /api/jobs/<id>/artifacts/<name>    marked.pdf, quantities.csv, ...
    GET  /healthz                           liveness

The TypeScript layer in ``src/ts`` remains the typed domain/API/UI layer and
speaks the same JSON contract; this module is the no-build deployment target.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import queue
import subprocess
import sys
import threading
import traceback
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "python"))

STORAGE = Path(os.environ.get("VVS_STORAGE", ROOT / "artifacts" / "jobs"))
WEB_ROOT = ROOT / "web"
DIST_UI = ROOT / "dist" / "ui"
MAX_UPLOAD_BYTES = int(os.environ.get("VVS_MAX_UPLOAD_BYTES", 128 * 1024 * 1024))
ARTIFACTS = ("analysis.json", "forensics.json", "marked.pdf", "debug.pdf", "quantities.csv")
WORKER = ROOT / "worker.py"
# A restart must not re-run a job for ever: an analysis that reliably kills
# the container would otherwise take the service down every time it came up.
MAX_ATTEMPTS = int(os.environ.get("VVS_MAX_ATTEMPTS", 2))

ENGINE_ERROR: str | None = None

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_work: "queue.Queue[str]" = queue.Queue()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _job_dir(job_id: str) -> Path:
    return STORAGE / job_id


def _load_job(job_id: str) -> dict | None:
    with _jobs_lock:
        if job_id in _jobs:
            return dict(_jobs[job_id])
    path = _job_dir(job_id) / "job.json"
    if path.exists():
        try:
            job = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return None
        with _jobs_lock:
            _jobs[job_id] = job
        return dict(job)
    return None


def _save_job(job: dict) -> None:
    with _jobs_lock:
        _jobs[job["id"]] = dict(job)
    d = _job_dir(job["id"])
    d.mkdir(parents=True, exist_ok=True)
    # Written through a temporary file: a container killed mid-write would
    # otherwise leave truncated JSON, and every later read of that job would
    # fail in a way that looks nothing like the original problem.
    tmp = d / "job.json.tmp"
    tmp.write_text(json.dumps(job, indent=2), encoding="utf-8")
    tmp.replace(d / "job.json")


def _progress(job_id: str, stage: str) -> None:
    job = _load_job(job_id)
    if not job:
        return
    job["progress"] = [*job.get("progress", []), {"stage": stage, "at": _now()}]
    _save_job(job)


def _worker() -> None:
    """Single background worker: analyses are CPU-bound and deterministic."""
    while True:
        job_id = _work.get()
        try:
            _run_job(job_id)
        except Exception:  # pragma: no cover - a failure must not kill the worker
            job = _load_job(job_id) or {"id": job_id}
            job["state"] = "failed"
            job["error"] = traceback.format_exc()[-4000:]
            job["finishedAt"] = _now()
            _save_job(job)
        finally:
            _work.task_done()


def _run_job(job_id: str) -> None:
    """Run one analysis in a child process and mirror its progress into the job.

    Everything the child says arrives as it happens, so a job that is slow and a
    job that is wedged look different from the outside, and a child that is
    killed outright still leaves a job marked failed with its last stage and its
    exit signal recorded.
    """
    job = _load_job(job_id)
    if not job:
        return
    directory = _job_dir(job_id)
    job["state"] = "running"
    job["startedAt"] = _now()
    job["attempts"] = int(job.get("attempts", 0)) + 1
    job["error"] = None
    _save_job(job)

    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, str(WORKER), str(directory)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stderr_tail: list[str] = []
    stderr_thread = threading.Thread(
        target=_drain, args=(proc.stderr, stderr_tail), daemon=True
    )
    stderr_thread.start()

    results: dict[str, str] = {}
    assert proc.stdout is not None
    for line in proc.stdout:
        line = line.rstrip("\n")
        sys.stdout.write(f"[{job_id}] {line}\n")
        sys.stdout.flush()
        if not line.startswith("@@"):
            continue
        kind, _, text = line[2:].partition(" ")
        if kind == "STAGE":
            _progress(job_id, text)
        elif kind == "ALIVE":
            _heartbeat(job_id, text)
        elif kind == "RESULT":
            name, _, value = text.partition(" ")
            results[name] = value
    code = proc.wait()
    stderr_thread.join(timeout=5)

    job = _load_job(job_id) or job
    if code == 0:
        job["state"] = "succeeded"
        job["analysisStatus"] = results.get("status")
        job["canonicalDigest"] = results.get("digest")
        job["reconciled"] = results.get("reconciled") == "yes"
    else:
        job["state"] = "failed"
        # A negative code is a signal: the usual one here is the platform's
        # out-of-memory killer, which leaves no traceback at all, so say plainly
        # that the child was killed rather than reporting an empty error.
        detail = "".join(stderr_tail)[-4000:]
        if code < 0:
            detail = f"the analysis process was killed by signal {-code}\n{detail}"
        job["error"] = detail or f"the analysis process exited with code {code}"
    job["exitCode"] = code
    job["finishedAt"] = _now()
    job["artifacts"] = {
        name: f"/api/jobs/{job_id}/artifacts/{name}"
        for name in ARTIFACTS
        if (directory / name).exists()
    }
    _save_job(job)


def _drain(stream, sink: list[str]) -> None:
    try:
        for line in stream:
            sink.append(line)
            del sink[:-200]
            sys.stderr.write(line)
    except Exception:  # pragma: no cover - the pipe closing is not an error
        pass


def _heartbeat(job_id: str, seconds: str) -> None:
    job = _load_job(job_id)
    if not job:
        return
    job["aliveAt"] = _now()
    job["elapsedSeconds"] = seconds
    _save_job(job)


def _recover_interrupted() -> list[str]:
    """Re-queue jobs a previous container died in the middle of.

    Without this a restart - a deploy, a health-check timeout, an out-of-memory
    kill - leaves the job file saying ``queued`` or ``running`` for ever while
    nothing is working on it.  That is indistinguishable, from the browser, from
    an upload that was never accepted.
    """
    recovered: list[str] = []
    for job in _list_jobs():
        if job.get("state") not in ("queued", "running"):
            continue
        if int(job.get("attempts", 0)) >= MAX_ATTEMPTS:
            job["state"] = "failed"
            job["error"] = (
                f"the analysis was interrupted {job.get('attempts')} times "
                "without finishing; it is not being retried again"
            )
            job["finishedAt"] = _now()
            _save_job(job)
            continue
        job["state"] = "queued"
        _save_job(job)
        _work.put(job["id"])
        recovered.append(job["id"])
    return recovered


def _submit(file_name: str, payload: bytes) -> dict:
    if len(payload) < 5 or payload[:4] != b"%PDF":
        raise ValueError("not a PDF")
    digest = hashlib.sha256(payload).hexdigest()[:16]
    stem = "".join(c if c.isalnum() or c in "-_" else "_" for c in Path(file_name).stem) or "drawing"
    job_id = f"{stem}-{digest}"

    existing = _load_job(job_id)
    if existing and existing.get("state") == "succeeded":
        return existing

    directory = _job_dir(job_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "input.pdf").write_bytes(payload)
    job = {
        "id": job_id,
        "fileName": file_name,
        "state": "queued",
        "createdAt": _now(),
        "startedAt": None,
        "finishedAt": None,
        "attempts": 0,
        "progress": [],
        "error": None,
        "artifacts": {},
        "bytes": len(payload),
    }
    _save_job(job)
    _work.put(job_id)
    return job


def _list_jobs() -> list[dict]:
    out: list[dict] = []
    if STORAGE.exists():
        for d in sorted(STORAGE.iterdir()):
            if d.is_dir():
                job = _load_job(d.name)
                if job:
                    out.append(job)
    out.sort(key=lambda j: j.get("createdAt", ""), reverse=True)
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "vvs-pipe"

    def log_message(self, fmt: str, *args) -> None:  # quieter, structured logs
        sys.stdout.write(f"{self.address_string()} {fmt % args}\n")

    def _json(self, status: int, payload) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, path: Path) -> None:
        if not path.exists() or not path.is_file():
            self._json(404, {"error": "not found"})
            return
        data = path.read_bytes()
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        parts = [p for p in url.path.split("/") if p]

        if url.path in ("/healthz", "/health"):
            self._json(
                200,
                {
                    "ok": ENGINE_ERROR is None,
                    "queued": _work.qsize(),
                    "engineError": ENGINE_ERROR,
                    "storage": str(STORAGE),
                    "worker": WORKER.exists(),
                },
            )
            return
        if url.path == "/api/jobs":
            self._json(200, {"jobs": _list_jobs(), "queueSize": _work.qsize()})
            return
        if len(parts) >= 3 and parts[0] == "api" and parts[1] == "jobs":
            job_id = parts[2]
            if len(parts) == 3:
                job = _load_job(job_id)
                self._json(200, job) if job else self._json(404, {"error": "no such job"})
                return
            if len(parts) == 4 and parts[3] == "report":
                path = _job_dir(job_id) / "analysis.json"
                if not path.exists():
                    self._json(404, {"error": "no report yet"})
                    return
                self._file(path)
                return
            if len(parts) == 5 and parts[3] == "artifacts" and parts[4] in ARTIFACTS:
                self._file(_job_dir(job_id) / parts[4])
                return

        rel = "index.html" if url.path == "/" else url.path.lstrip("/")
        for base in (WEB_ROOT, DIST_UI):
            candidate = (base / rel).resolve()
            if str(candidate).startswith(str(base.resolve())) and candidate.exists():
                self._file(candidate)
                return
        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        url = urlparse(self.path)
        if url.path != "/api/jobs":
            self._json(404, {"error": "not found"})
            return
        length = int(self.headers.get("content-length") or 0)
        if length <= 0 or length > MAX_UPLOAD_BYTES:
            self._json(400, {"error": "empty or oversized upload"})
            return
        payload = self.rfile.read(length)
        name = (parse_qs(url.query).get("name") or ["drawing.pdf"])[0]
        try:
            self._json(202, _submit(name, payload))
        except ValueError as err:
            self._json(400, {"error": str(err)})


def main() -> int:
    STORAGE.mkdir(parents=True, exist_ok=True)

    # Report a broken engine through the health endpoint rather than exiting.
    # A container that exits restarts, and a restart loop looks identical from
    # the outside to a routing problem; staying up and saying what is wrong
    # keeps the two distinguishable.
    global ENGINE_ERROR
    try:
        import vvs_pipe  # noqa: F401
    except Exception as err:  # pragma: no cover - deployment diagnostics
        ENGINE_ERROR = repr(err)
        sys.stderr.write(f"ERROR: analysis engine failed to import: {ENGINE_ERROR}\n")
        sys.stderr.write("Uploads will fail until requirements.txt is installed.\n")

    threading.Thread(target=_worker, name="vvs-worker", daemon=True).start()

    recovered = _recover_interrupted()
    if recovered:
        sys.stdout.write(f"re-queued {len(recovered)} interrupted job(s): {', '.join(recovered)}\n")

    # PaaS platforms inject the port to listen on.  When they do not, 8080 is
    # the conventional default and is what the platform's generated domain
    # targets unless it is told otherwise.
    port = int(os.environ.get("PORT") or 8080)
    host = os.environ.get("HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    sys.stdout.write(
        f"vvs-pipe listening on http://{host}:{port} "
        f"(PORT env {'set' if os.environ.get('PORT') else 'not set, defaulted'}); "
        f"health check at /healthz; storage {STORAGE}\n"
    )
    sys.stdout.flush()
    try:
        server.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
