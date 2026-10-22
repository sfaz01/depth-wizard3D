"""
jobs.py - Single-worker background job queue with on-disk status.

The depth model owns the GPU, so jobs run one at a time on a worker thread while the API
stays responsive. Status lives in status.json inside the job folder, so it survives a
server restart and the browser can poll it for live progress.
"""

from __future__ import annotations

import queue
import re
import threading
import time
import traceback
import uuid
from pathlib import Path
from typing import Callable, Dict, Optional

from src.scene import read_json, write_json

JOB_ID_RE = re.compile(r"^job_[A-Za-z0-9_]{1,64}$")

Runner = Callable[[Path, Callable[[str, float], None]], None]


def new_job_id() -> str:
    return f"job_{int(time.time())}_{uuid.uuid4().hex[:6]}"


class JobManager:
    def __init__(self, jobs_dir: Path):
        self.jobs_dir = Path(jobs_dir)
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._queue: "queue.Queue[tuple[str, Runner]]" = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, name="dw-worker", daemon=True)
        self._worker.start()

    # -- status -------------------------------------------------------------------------
    def job_dir(self, job_id: str) -> Path:
        if not JOB_ID_RE.match(job_id):
            raise ValueError("Invalid job id")
        return self.jobs_dir / job_id

    def status(self, job_id: str) -> Optional[Dict]:
        d = self.job_dir(job_id)
        st = read_json(d / "status.json")
        if st is None and (d / "manifest.json").exists():
            return {"job_id": job_id, "state": "done", "stage": "Done", "progress": 1.0}
        return st

    def _set(self, job_id: str, **fields) -> None:
        d = self.job_dir(job_id)
        with self._lock:
            st = read_json(d / "status.json", {"job_id": job_id})
            st.update(fields, updated=time.time())
            write_json(d / "status.json", st)

    # -- submission ---------------------------------------------------------------------
    def submit(self, runner: Runner, job_id: Optional[str] = None, title: str = "") -> str:
        job_id = job_id or new_job_id()
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        self._set(job_id, state="queued", stage="Waiting in queue", progress=0.0,
                  title=title, created=time.time(), queue_position=self._queue.qsize() + 1)
        self._queue.put((job_id, runner))
        return job_id

    def run_sync(self, runner: Runner, job_id: Optional[str] = None, title: str = "") -> str:
        """Run in the calling thread (CLI / tests)."""
        job_id = job_id or new_job_id()
        self.job_dir(job_id).mkdir(parents=True, exist_ok=True)
        self._set(job_id, state="queued", stage="Starting", progress=0.0, title=title, created=time.time())
        self._execute(job_id, runner)
        return job_id

    def _execute(self, job_id: str, runner: Runner) -> None:
        def progress(stage: str, frac: float) -> None:
            self._set(job_id, state="running", stage=stage, progress=round(float(frac), 3))

        started = time.time()
        try:
            progress("Starting", 0.01)
            runner(self.job_dir(job_id), progress)
            self._set(job_id, state="done", stage="Done", progress=1.0, seconds=round(time.time() - started, 1))
        except Exception as e:
            traceback.print_exc()
            self._set(job_id, state="error", stage="Failed", error=str(e))

    def _loop(self) -> None:
        while True:
            job_id, runner = self._queue.get()
            try:
                self._execute(job_id, runner)
            finally:
                self._queue.task_done()
