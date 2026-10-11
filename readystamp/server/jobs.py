"""Run a production end to end (output, load files, QC, run log), in the foreground or as a job.

Order of work: stamped files document by document, then load files, then QC, then
the run log. Every file is written under a `.partial` name and renamed when complete.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from readystamp import qc, runlog
from readystamp.model import Production, Settings
from readystamp.output.export import ExportResult, RunBlocked, export


@dataclass
class RunOutcome:
    status: str                     # completed | completed_with_failures | cancelled
    result: ExportResult
    qc: qc.QCReport
    qc_path: Path
    run_log_path: Path
    run_log: dict


def _input_files(production: Production, extra: list[Path]) -> list[Path]:
    seen: dict[Path, None] = {}
    for p in [*extra, *(pg.source_path for pg in production.pages())]:
        seen.setdefault(Path(p), None)
    return list(seen)


def execute(production: Production, settings: Settings, run_date: dt.date | None = None,
            progress: Callable[[int, int, int], None] | None = None,
            should_cancel: Callable[[], bool] | None = None,
            extra_inputs: list[Path] | None = None) -> RunOutcome:
    """Run a production. Raises RunBlocked (with user-readable problems) if it cannot start."""
    started = runlog.now()
    run_date = run_date or dt.date.today()
    inputs = _input_files(production, extra_inputs or [])
    input_hashes = runlog.hash_files([p for p in inputs if p.is_file()])
    input_documents = len(production.documents)

    result = export(production, settings, run_date, progress=progress, should_cancel=should_cancel)

    expected = qc.QCExpectations(
        format=settings.output.format, source_kind=production.source_kind,
        numbering_mode=settings.numbering.mode, volume_name=settings.output.volume_name,
        input_documents=input_documents, failed_documents=len(result.failures),
        expected_bates=[op.page.new_bates for od in result.documents for op in od.pages],
        cancelled=result.cancelled)
    report = qc.run_qc(result.volume_dir, expected)
    status = "cancelled" if result.cancelled else ("completed_with_failures" if result.failures else "completed")
    qc_path = result.volume_dir / "DATA" / f"{settings.output.volume_name}_qc.html"
    runlog.write_atomic(qc_path, qc.render_html(report, f"QC report: {settings.output.volume_name}", {
        "Status": status.replace("_", " "),
        "Documents written": str(len(result.documents)),
        "Pages written": str(result.pages_written),
        "Failed documents": str(len(result.failures)),
        "Run date": run_date.isoformat(),
    }))

    after = runlog.hash_files([p for p in inputs if p.is_file()])
    log = {
        "tool": runlog.tool_version(),
        "status": status,
        "started": started,
        "ended": runlog.now(),
        "run_date": run_date.isoformat(),
        "settings": json.loads(settings.model_dump_json()),
        "source_kind": production.source_kind,
        "inputs": input_hashes,
        "inputs_unchanged": input_hashes == after,
        "counts": {
            "input_documents": input_documents,
            "documents_written": len(result.documents),
            "pages_written": result.pages_written,
            "failed_documents": len(result.failures),
        },
        "failures": [{"document": seq, "message": msg} for seq, msg in result.failures],
        "qc": {"passed": report.passed, "checks": report.as_dict()},
        "outputs": runlog.output_hashes(result.volume_dir),
    }
    log_path = runlog.write_run_log(result.volume_dir, log)
    return RunOutcome(status, result, report, qc_path, log_path, log)


# ---------------------------------------------------------------------------
# Background jobs
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    status: str = "queued"           # queued | running | completed | completed_with_failures | cancelled | blocked | failed
    pages_done: int = 0
    pages_total: int = 0
    current_document: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    problems: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)
    qc_passed: bool | None = None
    qc: list[dict] = field(default_factory=list)
    output_dir: str | None = None
    qc_report: str | None = None
    run_log: str | None = None
    cancel_requested: bool = False

    def snapshot(self) -> dict:
        end = self.finished_at or time.monotonic()
        return {
            "id": self.id, "status": self.status, "pages_done": self.pages_done, "pages_total": self.pages_total,
            "current_document": self.current_document,
            "elapsed_seconds": round(end - self.started_at, 1) if self.started_at else 0.0,
            "problems": self.problems, "failures": self.failures, "qc_passed": self.qc_passed, "qc": self.qc,
            "output_dir": self.output_dir, "qc_report": self.qc_report, "run_log": self.run_log,
        }

    @property
    def finished(self) -> bool:
        return self.status not in ("queued", "running")


class JobBusy(RuntimeError):
    pass


class JobManager:
    """Runs one production at a time in a background thread."""

    def __init__(self):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._threads: dict[str, threading.Thread] = {}

    def start(self, production: Production, settings: Settings, run_date: dt.date | None = None,
              extra_inputs: list[Path] | None = None) -> str:
        with self._lock:
            if any(not j.finished for j in self._jobs.values()):
                raise JobBusy("A run is already in progress. Wait for it to finish or cancel it.")
            job = Job(id=uuid.uuid4().hex[:12], pages_total=production.page_count)
            self._jobs[job.id] = job

        def progress(done, total, seq):
            job.pages_done, job.pages_total, job.current_document = done, total, seq

        def work():
            job.status, job.started_at = "running", time.monotonic()
            try:
                outcome = execute(production, settings, run_date, progress=progress,
                                  should_cancel=lambda: job.cancel_requested, extra_inputs=extra_inputs)
            except RunBlocked as exc:
                job.status, job.problems = "blocked", exc.problems
            except Exception as exc:  # report, never crash the server
                job.status, job.problems = "failed", [f"The run stopped unexpectedly: {exc}"]
            else:
                job.status = outcome.status
                job.failures = outcome.run_log["failures"]
                job.qc_passed = outcome.qc.passed
                job.qc = outcome.qc.as_dict()
                job.output_dir = str(outcome.result.volume_dir)
                job.qc_report = str(outcome.qc_path)
                job.run_log = str(outcome.run_log_path)
            finally:
                job.finished_at = time.monotonic()

        thread = threading.Thread(target=work, name=f"readystamp-job-{job.id}", daemon=True)
        self._threads[job.id] = thread
        thread.start()
        return job.id

    def get(self, job_id: str) -> dict | None:
        job = self._jobs.get(job_id)
        return job.snapshot() if job else None

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if job is None or job.finished:
            return False
        job.cancel_requested = True
        return True

    def wait(self, job_id: str, timeout: float | None = None) -> dict | None:
        thread = self._threads.get(job_id)
        if thread:
            thread.join(timeout)
        return self.get(job_id)
