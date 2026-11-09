# -*- coding: utf-8 -*-
"""Build a static leaderboard from evaluation snapshots.

    python -m studiobench leaderboard --runs ./runs --out ./leaderboard_site

Input is a directory of completed runs (``<runs>/<task>/<model>/eval/eval.json``). The
generator does not re-score anything; it only aggregates published ``eval.json`` files and
emits ``index.html``, ``data.json`` and ``leaderboard.md``.

The production leaderboard should be built from a frozen snapshot directory, not from a live
run tree, so the published numbers stay reproducible.
"""
from __future__ import annotations

import argparse
import html
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..eval.aggregate import collect

MAIN = ("film", "visual", "audio", "av")
LABELS = ("speech", "music", "sfx", "dialogue")


def _mean(values: List[Optional[float]]) -> Optional[float]:
    xs = [v for v in values if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def summarize(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    models = sorted({str(r.get("model")) for r in rows})
    out = []
    for model in models:
        mine = [r for r in rows if str(r.get("model")) == model]
        scores = {k: _mean([r.get("scores", {}).get(k) for r in mine]) for k in MAIN}
        coverage = {k: sum(r.get("scores", {}).get(k) is not None for r in mine) for k in MAIN}
        by_label = {}
        for label in LABELS:
            vals = [r.get("scores", {}).get("audio") for r in mine if label in (r.get("_profile") or [])]
            by_label[label] = {"score": _mean(vals), "n": sum(v is not None for v in vals)}
        overall = _mean([scores[k] for k in MAIN if scores[k] is not None])
        out.append({"model": model, "tasks": len(mine), "scores": scores,
                    "coverage": coverage, "audio_by_profile": by_label, "overall": overall})
    return out


def _fmt(value: Optional[float]) -> str:
    return "—" if value is None else "%.3f" % value


def to_markdown(summary: List[Dict[str, Any]]) -> str:
    head = ["模型", "题数", "总分", "成片分", "画面分", "声音分", "声画分", "声音·speech", "声音·music",
            "声音·sfx", "声音·dialogue"]
    lines = ["| " + " | ".join(head) + " |", "|" + " --- |" * len(head)]
    for row in summary:
        cells = [row["model"], str(row["tasks"]), _fmt(row["overall"])]
        cells += [_fmt(row["scores"][k]) for k in MAIN]
        cells += [_fmt(row["audio_by_profile"][l]["score"]) for l in LABELS]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def to_html(summary: List[Dict[str, Any]], title: str) -> str:
    head = ["Model", "Tasks", "Overall", "Film", "Visual", "Audio", "A/V", "Audio·speech",
            "Audio·music", "Audio·sfx", "Audio·dialogue"]
    rows = []
    for row in summary:
        cells = [html.escape(row["model"]), str(row["tasks"]), _fmt(row["overall"])]
        cells += [_fmt(row["scores"][k]) for k in MAIN]
        cells += [_fmt(row["audio_by_profile"][l]["score"]) for l in LABELS]
        rows.append("<tr>" + "".join("<td>%s</td>" % c for c in cells) + "</tr>")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>%s</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:2rem auto;max-width:1100px;padding:0 1rem;color:#111}
 h1{font-size:1.4rem} p.note{color:#555;font-size:.9rem}
 table{border-collapse:collapse;width:100%%;font-size:.92rem}
 th,td{padding:.5rem .6rem;border-bottom:1px solid #e5e5e5;text-align:right}
 th:first-child,td:first-child{text-align:left}
 thead th{background:#fafafa;position:sticky;top:0}
 tr:hover td{background:#fcfcfc}
</style></head><body>
<h1>%s</h1>
<p class="note">Generated from published eval snapshots. Overall is the equal-weight mean of the available main scores. A dash means no task produced a score for that column.</p>
<table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>
</body></html>
""" % (html.escape(title), html.escape(title),
       "".join("<th>%s</th>" % html.escape(h) for h in head), "\n".join(rows))


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a static StudioBench leaderboard")
    ap.add_argument("--runs", default=os.environ.get("STUDIO_RUNS_DIR", "runs"),
                    help="directory with <task>/<model>/eval/eval.json")
    ap.add_argument("--out", default="leaderboard_site", help="output directory")
    ap.add_argument("--title", default="StudioBench Leaderboard")
    ap.add_argument("--include-invalid", action="store_true",
                    help="include runs flagged valid_for_bench=false")
    args = ap.parse_args()

    root = Path(args.runs)
    rows = collect(root, args.include_invalid)
    if not rows:
        print("no eval.json found under %s (pass --include-invalid to include all runs)" % root)
        return 1
    summary = summarize(rows)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "leaderboard.md").write_text(to_markdown(summary), encoding="utf-8")
    (out / "index.html").write_text(to_html(summary, args.title), encoding="utf-8")
    print(to_markdown(summary))
    print("wrote %s" % out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
