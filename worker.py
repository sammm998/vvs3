"""One analysis, run in its own process.

The web service does not analyse in-process.  A sheet takes minutes of pure
Python, which holds the interpreter lock for long stretches; run inside the
server's process it starves the HTTP threads, the platform's health check times
out, the container is restarted mid-analysis, and the job is left saying
``queued`` for ever with nothing in the logs to say why.  Isolating the work
also means an out-of-memory kill takes the child and not the service, so the
failure becomes a reported job failure instead of a silent restart.

The parent tracks progress by reading this script's stdout.  Each stage prints
one ``@@`` line; anything else is ordinary logging and is captured but ignored.

    python worker.py <job-directory>
"""

from __future__ import annotations

import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src" / "python"))

MARKER = "@@"
HEARTBEAT_SECONDS = 10.0


def emit(kind: str, text: str = "") -> None:
    sys.stdout.write(f"{MARKER}{kind} {text}\n".rstrip() + "\n")
    sys.stdout.flush()


def _heartbeat(stop: threading.Event) -> None:
    """Prove the child is alive during the long stages.

    Without this a slow analysis and a hung one look identical from the parent,
    and the only safe response to "no output for ten minutes" would be to kill
    work that was about to finish.
    """
    started = time.monotonic()
    while not stop.wait(HEARTBEAT_SECONDS):
        emit("ALIVE", f"{time.monotonic() - started:.0f}")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        sys.stderr.write("usage: worker.py <job-directory>\n")
        return 2
    directory = Path(argv[1])
    pdf = directory / "input.pdf"
    if not pdf.exists():
        emit("FAIL", "input.pdf missing")
        return 1

    stop = threading.Event()
    threading.Thread(target=_heartbeat, args=(stop,), daemon=True).start()
    try:
        emit("STAGE", "loading engine")
        from vvs_pipe.cli import _write_quantities_csv
        from vvs_pipe.pdf_forensics import forensic_report
        from vvs_pipe.pipeline import PipelineConfig, analyse_extracted
        from vvs_pipe.rendering import render_marked
        from vvs_pipe.vector_extraction import ExtractionConfig, extract_document

        cfg = PipelineConfig()
        emit("STAGE", "reading the PDF")
        forensics = forensic_report(pdf, cfg.extraction)
        emit("STAGE", "extracting vector geometry")
        doc = extract_document(pdf, cfg.extraction)
        emit("COUNT", f"vectorObjects {len(doc.objects)}")
        emit("STAGE", "analysing the drawing")
        result = analyse_extracted(doc, forensics, str(pdf), cfg, blind=True)

        emit("STAGE", "writing the forensic report")
        forensics.write(directory / "forensics.json")
        emit("STAGE", "writing the analysis")
        result.write_json(directory / "analysis.json")
        emit("STAGE", "rendering the marked drawing")
        render_marked(result, directory / "marked.pdf")
        if os.environ.get("RUN_FORENSICS") == "1":
            from vvs_pipe.rendering import render_debug

            emit("STAGE", "rendering the debug drawing")
            render_debug(result, directory / "debug.pdf")
        emit("STAGE", "writing the quantity list")
        _write_quantities_csv(result, directory / "quantities.csv")

        emit("RESULT", f"status {result.status.value}")
        emit("RESULT", f"digest {result.canonical_digest()}")
        emit("RESULT", f"reconciled {'yes' if result.reconciliation.ok else 'no'}")
        emit("DONE", "")
        return 0
    except Exception:
        emit("FAIL", "analysis raised")
        sys.stderr.write(traceback.format_exc())
        sys.stderr.flush()
        return 1
    finally:
        stop.set()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
