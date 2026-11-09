"""Run discovery, monitor aggregation and explorer output over a synthetic run tree."""
import json
from pathlib import Path

from studiobench.explorer.build import build_index, build_run_page
from studiobench.monitor.server import collect
from studiobench.results import iter_runs, snapshot_rows


def make_run(root: Path, task: str, model: str, submitted=True, valid=True,
             returncode=0, calls=None):
    calls = calls or [{"i": 1, "tool": "t2i", "ok": True, "sec": 0.1},
                      {"i": 2, "tool": "t2v", "ok": False, "sec": 0.2, "error": "boom"}]
    run = root / task / model
    (run / "work").mkdir(parents=True)
    (run / "eval").mkdir()
    result = {"task": task, "model": model, "submitted": submitted,
              "valid_for_bench": valid, "returncode": returncode, "seconds": 12.5,
              "tool_calls": len(calls or []), "tool_errors": 0,
              "llm": {"total_cost_usd": 1.25}, "budget": {"calls_max": 200, "wall_hours": 4}}
    (run / "result.json").write_text(json.dumps(result))
    (run / "eval" / "eval.json").write_text(json.dumps(
        {"model": model, "task": task, "valid_for_bench": valid,
         "scores": {"film": 0.8, "visual": 0.7, "audio": 0.6, "av": 0.5}}))
    (run / "work" / ".calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    (run / "prompt.txt").write_text("brief")
    (run / "final.mp4").write_bytes(b"stub")
    return run


def test_discovery_and_summary(tmp_path):
    make_run(tmp_path, "T-1", "model-a")
    make_run(tmp_path, "T-1", "model-b", submitted=False, valid=False, returncode=1)
    runs = list(iter_runs(tmp_path, include_invalid=True))
    assert len(runs) == 2
    by_model = {r.model: r for r in runs}
    assert by_model["model-a"].status == "done"
    assert by_model["model-b"].status == "failed"
    assert by_model["model-a"].tool_stats()["t2v"]["errors"] == 1
    rows = snapshot_rows(tmp_path)
    assert len(rows) == 1 and rows[0]["model"] == "model-a"


def test_monitor_totals(tmp_path):
    make_run(tmp_path, "T-1", "model-a")
    make_run(tmp_path, "T-1", "model-b", valid=False, submitted=True)
    data = collect(tmp_path)
    assert data["totals"]["runs"] == 2
    assert data["totals"]["done"] == 1
    assert data["totals"]["invalid"] == 1
    assert data["totals"]["tool_calls"] == 4      # 2 runs x 2 calls
    assert {m["model"] for m in data["models"]} == {"model-a", "model-b"}
    assert any(t["tool"] == "t2v" for t in data["tools"])


def test_explorer_pages(tmp_path):
    make_run(tmp_path, "T-1", "model-a")
    run = list(iter_runs(tmp_path))[0]
    page = build_run_page(run, "http://example.test/runs")
    assert "T-1" in page and "model-a" in page and "t2v" in page
    assert "http://example.test/runs/T-1/model-a/final.mp4" in page
    index = build_index([run], 0)
    assert "T-1__model-a.html" in index and "done" in index
