"""Uploads and their results, kept in a private temp folder for a limited time.

The UI works in steps (upload, find, replace, preview), so each upload gets an id
and its original and rewritten files are stored until they expire. Storage is in
this process's memory index plus files under a temp directory that is deleted on
shutdown. Run one worker process; a second process would not see these jobs.
"""

from __future__ import annotations

import atexit
import re
import secrets
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_TTL_SECONDS = 60 * 60


@dataclass
class Job:
    id: str
    filename: str
    format: str
    size: int
    created: float
    directory: Path
    details: dict = field(default_factory=dict)
    has_result: bool = False

    @property
    def original_path(self) -> Path:
        return self.directory / "original"

    @property
    def result_path(self) -> Path:
        return self.directory / "result"

    @property
    def stem(self) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", Path(self.filename).stem)[:80] or "document"

    @property
    def suffix(self) -> str:
        suffix = Path(self.filename).suffix.lower()
        return suffix if re.fullmatch(r"\.[a-z0-9]{1,10}", suffix) else ""


class JobStore:
    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.root = Path(tempfile.mkdtemp(prefix="universal-replace-"))
        self.ttl = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        atexit.register(shutil.rmtree, self.root, True)

    def create(self, filename: str, data: bytes, format_name: str, details: dict) -> Job:
        with self._lock:
            self._expire_locked()
            job_id = secrets.token_urlsafe(12)
            directory = self.root / job_id
            directory.mkdir()
            job = Job(
                id=job_id,
                filename=filename,
                format=format_name,
                size=len(data),
                created=time.time(),
                directory=directory,
                details=details,
            )
            job.original_path.write_bytes(data)
            self._jobs[job_id] = job
            return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            self._expire_locked()
            return self._jobs.get(job_id)

    def _expire_locked(self) -> None:
        cutoff = time.time() - self.ttl
        for job_id in [key for key, job in self._jobs.items() if job.created < cutoff]:
            shutil.rmtree(self._jobs.pop(job_id).directory, ignore_errors=True)
