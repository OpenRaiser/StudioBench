# -*- coding: utf-8 -*-
"""Build a static case explorer from a run tree.

    python -m studiobench explorer --runs ./runs --out ./explorer_site \
        --media-base http://127.0.0.1:8130

The site is dependency-free HTML/CSS: an index of runs and one page per run with the tool-call
timeline, per-tool summary, produced files and the final cut. It reads artifacts only.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List

from ..results import RunInfo, iter_runs

MAX_CALLS = 800

CSS = """
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#0f1115;color:#e6e9ee}
header{padding:16px 22px;border-bottom:1px solid #262c35;display:flex;gap:14px;align-items:baseline}
h1{font-size:17px;margin:0} a{color:#6ea8fe;text-decoration:none} a:hover{text-decoration:underline}
main{padding:18px 22px;display:grid;gap:18px}
.muted{color:#8b93a1;font-size:12px}
table{width:100%;border-collapse:collapse;background:#161a20;border:1px solid #262c35;border-radius:10px;overflow:hidden}
th,td{padding:7px 10px;border-bottom:1px solid #20262f;text-align:left;font-size:13px;vertical-align:top}
th{background:#12161c;color:#8b93a1;font-weight:600}
tr:last-child td{border-bottom:none}
.ok{color:#7ee2a8}.bad{color:#ff7b7b}.warn{color:#f0c674}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
.card{background:#161a20;border:1px solid #262c35;border-radius:10px;padding:12px 14px}
.card b{display:block;font-size:20px;margin-top:4px}
pre{white-space:pre-wrap;word-break:break-word;background:#10141a;border:1px solid #262c35;border-radius:8px;padding:10px;font-size:12px}
video{max-width:min(900px,100%);border-radius:10px;border:1px solid #262c35;background:#000}
"""


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _page(title: str, body: str) -> str:
    return ("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            "<title>%s</title><style>%s</style></head><body>%s</body></html>"
            % (esc(title), CSS, body))


def _calls_table(run: RunInfo) -> str:
    calls = run.calls()
    if not calls:
        return "<p class=\"muted\">No tool calls recorded (work/.calls.jsonl missing).</p>"
    start = min((c.get("t") or c.get("t_run") or 0) for c in calls)
    rows = []
    for call in calls[:MAX_CALLS]:
        t = call.get("t") or call.get("t_run") or 0
        args = json.dumps(call.get("args") or {}, ensure_ascii=False)
        if len(args) > 140:
            args = args[:140] + "…"
        outputs = ", ".join(str(o) for o in (call.get("outputs") or [])[:3])
        rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class=\"%s\">%s</td>"
                    "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        esc(call.get("i", "")), esc("%.1f" % (t - start)),
                        esc(call.get("tool")), esc(call.get("backend")),
                        "ok" if call.get("ok", True) else "bad",
                        "ok" if call.get("ok", True) else "error",
                        esc(call.get("sec")), esc(args), esc(outputs)))
    more = ""
    if len(calls) > MAX_CALLS:
        more = "<p class=\"muted\">Showing the first %d of %d calls.</p>" % (MAX_CALLS, len(calls))
    return ("<table><tr><th>#</th><th>t+s</th><th>tool</th><th>backend</th><th>status</th>"
            "<th>sec</th><th>args</th><th>outputs</th></tr>%s</table>%s" % ("".join(rows), more))


def _tool_summary(run: RunInfo) -> str:
    stats = run.tool_stats()
    if not stats:
        return "<p class=\"muted\">No tool calls.</p>"
    rows = "".join("<tr><td>%s</td><td>%s</td><td class=\"%s\">%s</td></tr>"
                   % (esc(name), row["calls"], "bad" if row["errors"] else "", row["errors"])
                   for name, row in sorted(stats.items(), key=lambda kv: -kv[1]["calls"]))
    return "<table><tr><th>tool</th><th>calls</th><th>errors</th></tr>%s</table>" % rows


def _files(run: RunInfo) -> str:
    work = run.run_dir / "work"
    items: List[str] = []
    try:
        for entry in sorted(os.scandir(work), key=lambda e: e.name)[:200]:
            if entry.is_file() and not entry.name.startswith("."):
                size = entry.stat().st_size
                items.append("<tr><td>%s</td><td>%s</td></tr>"
                             % (esc(entry.name), esc("%.1f KB" % (size / 1024) if size < 1 << 20
                                                     else "%.1f MB" % (size / (1 << 20)))))
    except OSError:
        pass
    if not items:
        return "<p class=\"muted\">No files in work/.</p>"
    return "<table><tr><th>file</th><th>size</th></tr>%s</table>" % "".join(items)


def build_run_page(run: RunInfo, media_base: str) -> str:
    final = run.final_path
    video = ""
    if final and media_base:
        src = "%s/%s/%s/final.mp4" % (media_base.rstrip("/"), run.task_id, run.model)
        video = "<video controls preload=\"metadata\" src=\"%s\"></video>" % esc(src)
    elif final:
        video = "<p class=\"muted\">Final cut: <code>%s</code> (pass --media-base to embed)</p>" % esc(final)

    cards = [("Status", run.status), ("Seconds", run.seconds), ("Tool calls", run.tool_calls),
             ("Tool errors", run.tool_errors), ("Cost USD", run.cost_usd)]
    card_html = "".join("<div class=\"card\">%s<b>%s</b></div>" % (esc(k), esc(v if v is not None else "—"))
                        for k, v in cards)
    body = ("<header><a href=\"index.html\">← all runs</a><h1>%s · %s</h1>"
            "<span class=\"muted\">%s</span></header><main>"
            "<section class=\"cards\">%s</section>%s"
            "<section><h2>Final cut</h2>%s</section>"
            "<section><h2>Tool summary</h2>%s</section>"
            "<section><h2>Calls</h2>%s</section>"
            "<section><h2>Files in work/</h2>%s</section>"
            "</main>") % (esc(run.task_id), esc(run.model), esc(run.run_dir), card_html,
                          "", video, _tool_summary(run), _calls_table(run), _files(run))
    return _page("%s · %s" % (run.task_id, run.model), body)


def build_index(runs: List[RunInfo], generated: float) -> str:
    rows = []
    for run in sorted(runs, key=lambda r: (r.task_id, r.model)):
        link = "%s__%s.html" % (run.task_id, run.model)
        cls = {"done": "ok", "failed": "bad", "invalid": "warn", "unsubmitted": "warn"}.get(run.status, "")
        rows.append("<tr><td><a href=\"%s\">%s</a></td><td>%s</td><td class=\"%s\">%s</td>"
                    "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                        esc(link), esc(run.task_id), esc(run.model), cls, esc(run.status),
                        esc(run.seconds if run.seconds is not None else "—"),
                        esc(run.tool_calls), esc(run.cost_usd if run.cost_usd is not None else "—")))
    body = ("<header><h1>StudioBench case explorer</h1><span class=\"muted\">%d runs · generated %s</span>"
            "</header><main><table><tr><th>task</th><th>model</th><th>status</th><th>seconds</th>"
            "<th>calls</th><th>cost</th></tr>%s</table></main>"
            % (len(runs), time.strftime("%Y-%m-%d %H:%M", time.localtime(generated)), "".join(rows)))
    return _page("StudioBench case explorer", body)


def main() -> int:
    ap = argparse.ArgumentParser(description="Build a static case explorer")
    ap.add_argument("--runs", default=os.environ.get("STUDIO_RUNS_DIR", "runs"))
    ap.add_argument("--out", default="explorer_site")
    ap.add_argument("--media-base", default="",
                    help="URL prefix under which the run tree is served, e.g. http://host:8130")
    ap.add_argument("--include-invalid", action="store_true", default=True)
    args = ap.parse_args()

    root = Path(args.runs).resolve()
    if not root.is_dir():
        print("runs directory not found: %s" % root)
        return 1
    runs = list(iter_runs(root, include_invalid=True))
    if not runs:
        print("no runs found under %s" % root)
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    generated = time.time()
    for run in runs:
        (out / ("%s__%s.html" % (run.task_id, run.model))).write_text(
            build_run_page(run, args.media_base), encoding="utf-8")
    (out / "index.html").write_text(build_index(runs, generated), encoding="utf-8")
    (out / "runs.json").write_text(json.dumps([r.summary() for r in runs], ensure_ascii=False, indent=2),
                                   encoding="utf-8")
    print("wrote %d run pages to %s" % (len(runs), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
