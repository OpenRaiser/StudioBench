# -*- coding: utf-8 -*-
"""Build a GitHub Pages site from a run tree.

    python -m studiobench site --runs ./runs --out ./site --cases COM-AST-002,NAR-REF-007

Layout::

    site/
      index.html            landing page
      leaderboard/          leaderboard generated from eval snapshots
      explorer/             one page per selected run
      media/<task>/<model>/final.mp4

Only a curated subset is published: Pages has a 1 GB site limit, so the builder copies or
transcodes the selected runs' final cuts and stops before the budget is reached.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from ..eval import aggregate
from ..explorer.build import build_index, build_run_page
from ..leaderboard.build import summarize, to_html
from ..results import RunInfo, iter_runs
from ..tools import media

PROFILES = {"720": ("-2:720", "28", "96k"), "540": ("-2:540", "31", "64k")}


def _encode(src: Path, dst: Path, profile: str) -> None:
    scale, crf, audio = PROFILES[profile]
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run([media.FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                           "-i", str(src), "-vf", "scale=%s" % scale,
                           "-c:v", "libx264", "-preset", "veryfast", "-crf", crf,
                           "-c:a", "aac", "-b:a", audio, "-movflags", "+faststart",
                           str(dst)], capture_output=True, text=True, timeout=1800)
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:])


def _media_source(run: RunInfo, media_from: List[Path]) -> Optional[Path]:
    for root in media_from:
        for name in ("final_720p.mp4", "final_540p.mp4", "final.mp4"):
            candidate = root / run.task_id / run.model / name
            if candidate.is_file():
                return candidate
    return run.final_path


def _landing(title: str, runs: List[RunInfo], skipped: List[str], total_mb: float) -> str:
    rows = "".join("<tr><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (html.escape(r.task_id), html.escape(r.model), html.escape(r.status))
                   for r in runs)
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e9ee;margin:0}
header{padding:22px;border-bottom:1px solid #262c35}h1{margin:0 0 6px;font-size:22px}
main{padding:22px;display:grid;gap:20px;max-width:1100px;margin:0 auto}
a{color:#6ea8fe;text-decoration:none}a:hover{text-decoration:underline}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.card{background:#161a20;border:1px solid #262c35;border-radius:12px;padding:16px}
.card b{display:block;font-size:20px;margin-top:6px}
table{width:100%%;border-collapse:collapse;font-size:13px}
th,td{padding:7px 10px;border-bottom:1px solid #20262f;text-align:left}
th{color:#8b93a1}.muted{color:#8b93a1;font-size:12px}
</style></head><body>
<header><h1>%s</h1><p class="muted">Curated static build · %d runs · %.0f MB</p></header>
<main>
<section class="cards">
 <div class="card"><span>Leaderboard</span><b><a href="leaderboard/">open →</a></b></div>
 <div class="card"><span>Case explorer</span><b><a href="explorer/">open →</a></b></div>
 <div class="card"><span>Published runs</span><b>%d</b></div>
</section>
<section><h2>Runs in this build</h2><table><tr><th>task</th><th>model</th><th>status</th></tr>%s</table>
%s</section>
</main></body></html>
""" % (html.escape(title), html.escape(title), len(runs), total_mb, len(runs), rows,
       ("<p class=\"muted\">Skipped for budget: %s</p>" % html.escape(", ".join(skipped))
        if skipped else ""))


def build_site(runs_root: Path, out: Path, cases: Optional[List[str]] = None,
               models: Optional[List[str]] = None, max_mb: float = 900.0,
               profile: str = "540", media_from: Optional[List[Path]] = None,
               title: str = "StudioBench") -> dict:
    out = Path(out)
    if out.exists():
        shutil.rmtree(out)
    (out / "media").mkdir(parents=True)
    (out / "explorer").mkdir()
    (out / "leaderboard").mkdir()

    all_runs = list(iter_runs(runs_root, include_invalid=True))
    if cases:
        wanted = {c.strip() for c in cases if c.strip()}
        all_runs = [r for r in all_runs if r.task_id in wanted]
    if models:
        wanted_models = {m.strip() for m in models if m.strip()}
        all_runs = [r for r in all_runs if r.model in wanted_models]
    all_runs.sort(key=lambda r: (r.task_id, r.model))

    selected: List[RunInfo] = []
    skipped: List[str] = []
    total_bytes = 0
    budget = int(max_mb * 1024 * 1024)
    for run in all_runs:
        if run.status == "running":
            continue
        src = _media_source(run, media_from or [])
        if src is None:
            skipped.append("%s/%s (no final cut)" % (run.task_id, run.model))
            continue
        dst = out / "media" / run.task_id / run.model / "final.mp4"
        try:
            _encode(src, dst, profile)
        except Exception as exc:  # noqa: BLE001
            skipped.append("%s/%s (encode failed: %s)" % (run.task_id, run.model, str(exc)[:80]))
            continue
        size = dst.stat().st_size
        if total_bytes + size > budget:
            dst.unlink(missing_ok=True)
            skipped.append("%s/%s (over %d MB budget)" % (run.task_id, run.model, max_mb))
            continue
        total_bytes += size
        selected.append(run)

    if not selected:
        raise SystemExit("nothing to publish: no run had a usable final cut")

    for run in selected:
        (out / "explorer" / ("%s__%s.html" % (run.task_id, run.model))).write_text(
            build_run_page(run, "../media"), encoding="utf-8")
    (out / "explorer" / "index.html").write_text(build_index(selected, time.time()),
                                                 encoding="utf-8")

    rows = aggregate.collect(Path(runs_root), False)
    if cases:
        wanted = {c.strip() for c in cases if c.strip()}
        rows = [r for r in rows if str(r.get("task")) in wanted]
    if models:
        wanted_models = {m.strip() for m in models if m.strip()}
        rows = [r for r in rows if str(r.get("model")) in wanted_models]
    if rows:
        (out / "leaderboard" / "index.html").write_text(
            to_html(summarize(rows), title + " leaderboard"), encoding="utf-8")
    else:
        (out / "leaderboard" / "index.html").write_text(
            "<!doctype html><meta charset=\"utf-8\"><body style=\"background:#0f1115;color:#e6e9ee;"
            "font-family:system-ui;padding:22px\"><h1>Leaderboard</h1>"
            "<p>No eval snapshots were found for the selected runs. Run <code>sb eval</code> on the "
            "selected runs and rebuild this site.</p><p><a style=\"color:#6ea8fe\" href=\"../\">← home</a></p>",
            encoding="utf-8")

    (out / "index.html").write_text(
        _landing(title, selected, skipped, total_bytes / (1024 * 1024)), encoding="utf-8")
    rows = [{"task": r.task_id, "model": r.model, "status": r.status,
             "seconds": r.seconds} for r in selected]
    manifest = {"generated_at": time.time(), "profile": profile, "runs": rows,
                "skipped": skipped, "media_bytes": total_bytes}
    (out / "site.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a static GitHub Pages site")
    ap.add_argument("--runs", default=os.environ.get("STUDIO_RUNS_DIR", "runs"))
    ap.add_argument("--out", default="site")
    ap.add_argument("--cases", default="", help="comma-separated task ids (default: all)")
    ap.add_argument("--models", default="", help="comma-separated model ids (default: all)")
    ap.add_argument("--max-size-mb", type=float, default=900.0)
    ap.add_argument("--profile", choices=sorted(PROFILES), default="540")
    ap.add_argument("--media-from", default="", help="comma-separated pre-encoded trees: <dir>/<task>/<model>/final_720p.mp4")
    ap.add_argument("--title", default="StudioBench")
    args = ap.parse_args()

    root = Path(args.runs).resolve()
    if not root.is_dir():
        print("runs directory not found: %s" % root)
        return 1
    manifest = build_site(root, Path(args.out),
                          cases=[c for c in args.cases.split(",") if c],
                          models=[m for m in args.models.split(",") if m],
                          max_mb=args.max_size_mb, profile=args.profile,
                          media_from=[Path(p) for p in args.media_from.split(",") if p],
                          title=args.title)
    print("site: %s runs, %.1f MB media, %d skipped"
          % (len(manifest["runs"]), manifest["media_bytes"] / (1024 * 1024), len(manifest["skipped"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
