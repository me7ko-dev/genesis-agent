"""Storage monitor — warn when the project tree exceeds configured threshold.

Operates under Project Genesis Agent DNA: growth must not bypass GENE-ETHICS / GENE-AUTHORITY /
GENE-SECURITY policies enforced elsewhere (Brain, Executor, Skills).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from genesis_agent.config import LOGS_DIR, PROJECT_ROOT, STORAGE_THRESHOLD_BYTES


@dataclass
class StorageReport:
    total_bytes: int
    threshold_bytes: int
    compression_required: bool
    log_path: Path | None
    candidates_path: Path | None


def _all_files(root: Path):
    for p in root.rglob("*"):
        if p.is_file():
            yield p


def _candidate_filter(p: Path, root: Path) -> bool:
    rel_parts = p.relative_to(root).parts
    if "__pycache__" in rel_parts:
        return False
    if ".sandbox_run" in rel_parts:
        return False
    if ".git" in rel_parts:
        return False
    return True


def _file_sizes(root: Path, *, candidates_only: bool) -> list[tuple[Path, int]]:
    rows: list[tuple[Path, int]] = []
    for p in _all_files(root):
        if candidates_only and not _candidate_filter(p, root):
            continue
        try:
            rows.append((p, p.stat().st_size))
        except OSError:
            continue
    return rows


def directory_total_bytes(root: Path) -> int:
    return sum(sz for _p, sz in _file_sizes(root, candidates_only=False))


def check_storage(
    *,
    root: Path | None = None,
    threshold: int | None = None,
    top_n_candidates: int = 200,
) -> StorageReport:
    """
    If total size > threshold, log a Compression Required event and emit a JSON list
    of the largest files (candidates for pruning / off-site archival / model quantization prep).
    """
    root = root or PROJECT_ROOT
    threshold = threshold or STORAGE_THRESHOLD_BYTES
    total = directory_total_bytes(root)
    required = total > threshold

    log_path: Path | None = None
    candidates_path: Path | None = None

    if required:
        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        log_path = LOGS_DIR / "storage_watch.log"
        ts = datetime.now(timezone.utc).isoformat()
        line = f"{ts}\tCOMPRESSION_REQUIRED\ttotal_bytes={total}\tthreshold={threshold}\n"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.open("a", encoding="utf-8").write(line)

        rows = sorted(
            _file_sizes(root, candidates_only=True),
            key=lambda x: -x[1],
        )[:top_n_candidates]
        payload: dict[str, Any] = {
            "event": "COMPRESSION_REQUIRED",
            "generated_at": ts,
            "root": str(root),
            "total_bytes": total,
            "threshold_bytes": threshold,
            "candidates_largest_first": [
                {"path": str(p.relative_to(root)).replace("\\", "/"), "bytes": sz} for p, sz in rows
            ],
        }
        candidates_path = LOGS_DIR / "compression_candidates.json"
        candidates_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    return StorageReport(
        total_bytes=total,
        threshold_bytes=threshold,
        compression_required=required,
        log_path=log_path,
        candidates_path=candidates_path,
    )


def human_gb(n: int) -> float:
    return round(n / (1024**3), 3)
