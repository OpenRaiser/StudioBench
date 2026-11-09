# -*- coding: utf-8 -*-
"""Read run directories into a stable structure shared by the monitor, explorer and leaderboard.

A run directory is any directory containing ``result.json``; its task and model are taken from
the result when present, otherwise from the directory names. Heavy subdirectories (``work``,
``eval``, ``session``) are never traversed while discovering runs.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SKIP_DIRS = {"_canvas", "work", "eval", "session", "previews", "thumbs", "data", "fonts",
             "screens", "node_modules", "__pycache__", ".git"}
RESULT = "result.json"
EVAL = Path("eval") / "eval.json"
CALLS = Path("work") / ".calls.jsonl"


def _read_json(path: Path) -> Optional[dict]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def iter_run_dirs(root: Path, max_depth: int = 6, include_incomplete: bool = False) -> Iterator[Path]:
    """Yield run directories under ``root``.

    With ``include_incomplete`` a directory containing ``prompt.txt`` but no ``result.json`` is
    also yielded (a run that started but has not finished).
    """
    stack: List[tuple[Path, int]] = [(Path(root), 0)]
    while stack:
        directory, depth = stack.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        files = {e.name for e in entries if e.is_file()}
        if RESULT in files or (include_incomplete and "prompt.txt" in files):
            yield directory
            continue
        if depth >= max_depth:
            continue
        for entry in entries:
            if not entry.is_dir() or entry.name in SKIP_DIRS or entry.name.startswith("."):
                continue
            stack.append((Path(entry.path), depth + 1))


@dataclass
class RunInfo:
    run_dir: Path
    task_id: str
    model: str
    result: Dict[str, Any] = field(default_factory=dict)
    eval: Optional[Dict[str, Any]] = None
    _calls: Optional[List[dict]] = None

    @property
    def submitted(self) -> bool:
        return bool(self.result.get("submitted"))

    @property
    def valid(self) -> bool:
        return bool(self.result.get("valid_for_bench"))

    @property
    def returncode(self) -> Optional[int]:
        return self.result.get("returncode")

    @property
    def seconds(self) -> Optional[float]:
        value = self.result.get("seconds")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def cost_usd(self) -> Optional[float]:
        llm = self.result.get("llm") or {}
        value = llm.get("total_cost_usd")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def tool_calls(self) -> int:
        value = self.result.get("tool_calls")
        if isinstance(value, int):
            return value
        return len(self.calls())

    @property
    def tool_errors(self) -> int:
        value = self.result.get("tool_errors")
        if isinstance(value, int):
            return value
        return sum(1 for c in self.calls() if not c.get("ok", True))

    @property
    def status(self) -> str:
        if not self.result:
            return "running"
        if self.returncode not in (None, 0):
            return "failed"
        if not self.submitted:
            return "unsubmitted"
        return "done" if self.valid else "invalid"

    @property
    def final_path(self) -> Optional[Path]:
        candidate = self.run_dir / "final.mp4"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate
        recorded = self.result.get("final")
        if recorded and Path(recorded).is_file():
            return Path(recorded)
        return None

    def calls(self) -> List[dict]:
        if self._calls is None:
            path = self.run_dir / CALLS
            rows: List[dict] = []
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except ValueError:
                            continue
            except OSError:
                pass
            self._calls = rows
        return self._calls

    def tool_stats(self) -> Dict[str, Dict[str, int]]:
        stats: Dict[str, Dict[str, int]] = {}
        for call in self.calls():
            name = str(call.get("tool"))
            row = stats.setdefault(name, {"calls": 0, "errors": 0})
            row["calls"] += 1
            if not call.get("ok", True):
                row["errors"] += 1
        return stats

    def summary(self) -> Dict[str, Any]:
        return {"task": self.task_id, "model": self.model, "status": self.status,
                "submitted": self.submitted, "valid": self.valid,
                "seconds": self.seconds, "cost_usd": self.cost_usd,
                "tool_calls": self.tool_calls, "tool_errors": self.tool_errors,
                "run_dir": str(self.run_dir)}


def load_run(run_dir: Path) -> RunInfo:
    run_dir = Path(run_dir)
    result = _read_json(run_dir / RESULT) or {}
    task_id = str(result.get("task") or run_dir.parent.name)
    model = str(result.get("model") or run_dir.name)
    return RunInfo(run_dir=run_dir, task_id=task_id, model=model, result=result,
                   eval=_read_json(run_dir / EVAL))


def iter_runs(root: Path, include_invalid: bool = True,
              include_incomplete: bool = False) -> Iterator[RunInfo]:
    for directory in iter_run_dirs(Path(root), include_incomplete=include_incomplete):
        run = load_run(directory)
        if include_invalid or run.valid:
            yield run


def snapshot_rows(root: Path, include_invalid: bool = False) -> List[Dict[str, Any]]:
    """Rows for the leaderboard snapshot: scores and provenance, no media paths."""
    rows = []
    for run in iter_runs(root, include_invalid=include_invalid):
        ev = run.eval or {}
        rows.append({
            "task": run.task_id,
            "model": run.model,
            "valid_for_bench": run.valid,
            "submitted": run.submitted,
            "scores": ev.get("scores") or {},
            "judge": ev.get("judge"),
            "harness_version": ev.get("harness_version"),
            "budget": ev.get("budget") or run.result.get("budget"),
            "seconds": run.seconds,
            "cost_usd": run.cost_usd,
            "tool_calls": run.tool_calls,
            "tool_errors": run.tool_errors,
        })
    return rows


def write_snapshot(root: Path, out: Path, include_invalid: bool = False) -> Path:
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    rows = snapshot_rows(root, include_invalid=include_invalid)
    target = out / "snapshot.json"
    target.write_text(json.dumps({"source": str(root), "runs": rows}, ensure_ascii=False, indent=2),
                      encoding="utf-8")
    return target
