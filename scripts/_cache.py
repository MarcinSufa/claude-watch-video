"""Per-step output cache for watch_video.py.

Each step (fetch, probe, frames, transcribe, dedup, ocr, report) gets a
fingerprint derived from its input file hashes + the flags that affect its
output. Fingerprints are stored in meta.json's `cache.steps` block. On re-run
the orchestrator compares fingerprints; if match AND expected outputs still
exist on disk, the step is skipped.

Design choices:
- File fingerprint = "size:mtime" (fast, no IO over the file body). For our
  use case (mutating a workdir between runs), this catches all real changes.
  Upgrade to partial-sha if mtime spoofing becomes an issue.
- Step fingerprint = sha1(stable-json({step_name, inputs_dict})), truncated.
- Cache decisions are output-aware: a matched fingerprint AND missing output
  files = miss (force re-run). Prevents stale-cache-pointing-at-deleted-files.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any


CACHE_SCHEMA_VERSION = 1


def file_fingerprint(path: Path) -> str | None:
    """Return 'size:mtime_ns' for a file, or None if it doesn't exist."""
    if not path.exists():
        return None
    st = path.stat()
    return f"{st.st_size}:{st.st_mtime_ns}"


def dir_fingerprint(path: Path, glob: str = "t_*.jpg") -> str | None:
    """Return a fingerprint of a directory's <glob> contents (sorted names + each fp)."""
    if not path.exists():
        return None
    entries = sorted(path.glob(glob))
    if not entries:
        return "empty"
    h = hashlib.sha1()
    for p in entries:
        fp = file_fingerprint(p)
        h.update(f"{p.name}|{fp}\n".encode())
    return h.hexdigest()[:16]


def step_fingerprint(step_name: str, inputs: dict[str, Any]) -> str:
    """Hash (step_name, sorted-json-of-inputs) to a short, stable fingerprint."""
    payload = json.dumps(
        {"step": step_name, "inputs": inputs},
        sort_keys=True, separators=(",", ":"), default=str,
    )
    return hashlib.sha1(payload.encode()).hexdigest()[:16]


def get_cache(meta: dict) -> dict:
    """Return meta['cache'], initializing if absent."""
    cache = meta.get("cache")
    if not cache or cache.get("schema") != CACHE_SCHEMA_VERSION:
        cache = {"schema": CACHE_SCHEMA_VERSION, "steps": {}}
        meta["cache"] = cache
    return cache


def is_cached(meta: dict, step_name: str, fingerprint: str,
              expected_outputs: list[Path]) -> bool:
    """True if the step's fingerprint matches AND all expected outputs exist.

    `expected_outputs` is a list of file/dir paths that must exist for the
    cache to be considered valid.
    """
    cache = get_cache(meta)
    cached_step = cache["steps"].get(step_name)
    if not cached_step or cached_step.get("fingerprint") != fingerprint:
        return False
    for p in expected_outputs:
        if not p.exists():
            return False
    return True


def record_step(meta: dict, step_name: str, fingerprint: str,
                outputs: list[Path] | None = None) -> None:
    """Save a step's fingerprint + completion info into meta['cache']."""
    cache = get_cache(meta)
    import time
    cache["steps"][step_name] = {
        "fingerprint": fingerprint,
        "completed_at": int(time.time()),
        "outputs": [str(p) for p in outputs] if outputs else [],
    }


def invalidate_step(meta: dict, step_name: str) -> None:
    """Drop a step's cache entry (used by --force-step)."""
    cache = get_cache(meta)
    cache["steps"].pop(step_name, None)


DEPS = {
    # `from_step` -> [downstream steps that must re-run if from_step's output changes]
    "fetch":      ["probe", "frames", "transcribe", "dedup", "ocr", "report"],
    "probe":      [],  # probe info is read-only; nothing downstream caches against it
    "frames":     ["dedup", "ocr", "report"],
    "transcribe": ["dedup", "report"],  # ocr operates on frames, not transcript
    "dedup":      ["ocr", "report"],
    "ocr":        ["report"],
    "report":     [],
}


def invalidate_downstream(meta: dict, from_step: str) -> None:
    """Drop cache for all steps that consume `from_step`'s output, per DEPS."""
    cache = get_cache(meta)
    for step in DEPS.get(from_step, []):
        cache["steps"].pop(step, None)
