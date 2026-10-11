"""The run log: settings, inputs, counts, QC results and the SHA-256 of every output file."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path

from readystamp import __version__

RUN_LOG_NAME = "run_log.json"


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


def hash_files(paths: list[Path]) -> list[dict]:
    return [{"path": str(p), "sha256": sha256_file(p), "bytes": p.stat().st_size} for p in paths]


def output_hashes(volume_dir: Path) -> list[dict]:
    """Every file under the volume folder except the run log itself, with paths relative to it."""
    files = sorted(p for p in volume_dir.rglob("*") if p.is_file() and p.name != RUN_LOG_NAME
                   and not p.name.endswith(".partial"))
    return [{"path": str(p.relative_to(volume_dir)).replace(os.sep, "\\"), "sha256": sha256_file(p),
             "bytes": p.stat().st_size} for p in files]


def write_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(text, encoding=encoding)
    os.replace(tmp, path)


def write_run_log(volume_dir: Path, log: dict) -> Path:
    path = volume_dir / "DATA" / RUN_LOG_NAME
    write_atomic(path, json.dumps(log, indent=2, ensure_ascii=False, default=str) + "\n")
    return path


def now() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def tool_version() -> str:
    return f"ReadyStamp Lite {__version__}"
