# -*- coding: utf-8 -*-
"""Web monitor for a run tree.

    python -m studiobench monitor --runs ./runs --port 8132

Serves a single self-contained page plus ``/api/status``. It only reads the run tree; it never
writes to it and never touches a scheduler. Works with any layout that contains
``<...>/result.json`` run directories (and ``prompt.txt`` for runs still in progress).
"""
from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ..results import iter_runs

TTL = float(os.environ.get("STUDIO_MONITOR_TTL", "10"))
_cache_lock = threading.Lock()
_cache = {"at": 0.0, "data": None}


def collect(root: Path, limit_recent: int = 30) -> dict:
    runs = list(iter_runs(root, include_invalid=True, include_incomplete=True))
    totals = {"runs": len(runs), "done": 0, "unsubmitted": 0, "failed": 0,
              "invalid": 0, "running": 0, "tool_calls": 0, "tool_errors": 0,
              "cost_usd": 0.0}
    models: dict = defaultdict(lambda: {"runs": 0, "done": 0, "failed": 0, "invalid": 0,
                                        "unsubmitted": 0, "running": 0, "tool_calls": 0,
                                        "tool_errors": 0, "cost_usd": 0.0, "seconds": [],
                                        "tool_usage": defaultdict(lambda: {"calls": 0, "errors": 0})})
    tools: dict = defaultdict(lambda: {"calls": 0, "errors": 0})
    recent = []

    for run in runs:
        status = run.status
        totals[status if status in ("done", "unsubmitted", "failed", "invalid", "running") else "running"] += 1
        m = models[run.model]
        m["runs"] += 1
        m[status if status in ("done", "unsubmitted", "failed", "invalid", "running") else "running"] += 1
        calls = 0 if status == "running" else run.tool_calls
        errors = 0 if status == "running" else run.tool_errors
        cost = run.cost_usd or 0.0
        totals["tool_calls"] += calls
        totals["tool_errors"] += errors
        totals["cost_usd"] += cost
        m["tool_calls"] += calls
        m["tool_errors"] += errors
        m["cost_usd"] += cost
        if run.seconds:
            m["seconds"].append(run.seconds)
        if status != "running":
            for name, row in run.tool_stats().items():
                stats = m["tool_usage"][name]
                stats["calls"] += row["calls"]
                stats["errors"] += row["errors"]
                tools[name]["calls"] += row["calls"]
                tools[name]["errors"] += row["errors"]
        try:
            mtime = run.run_dir.stat().st_mtime
        except OSError:
            mtime = 0
        row = run.summary()
        row["mtime"] = mtime
        recent.append(row)

    recent.sort(key=lambda r: r["mtime"], reverse=True)
    model_rows = []
    for name, m in models.items():
        seconds = m.pop("seconds")
        row = {"model": name, **m,
               "avg_seconds": round(sum(seconds) / len(seconds), 1) if seconds else None,
               "cost_usd": round(m["cost_usd"], 2),
               "tool_usage": sorted(({"tool": k, **v} for k, v in m["tool_usage"].items()),
                                    key=lambda x: -x["calls"])}
        model_rows.append(row)
    model_rows.sort(key=lambda r: (-r["done"], -r["runs"], r["model"]))

    return {"root": str(root), "generated_at": time.time(),
            "totals": {**totals, "cost_usd": round(totals["cost_usd"], 2)},
            "models": model_rows,
            "tools": sorted(({"tool": k, **v} for k, v in tools.items()), key=lambda x: -x["calls"]),
            "recent": recent[:limit_recent]}


def cached(root: Path) -> dict:
    with _cache_lock:
        now = time.time()
        if _cache["data"] is None or now - _cache["at"] > TTL:
            _cache["data"] = collect(root)
            _cache["at"] = now
        return _cache["data"]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>StudioBench monitor</title>
<style>
 body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;margin:0;background:#0f1115;color:#e6e9ee}
 header{padding:18px 22px;border-bottom:1px solid #262c35;display:flex;gap:16px;align-items:baseline}
 h1{font-size:17px;margin:0} .muted{color:#8b93a1;font-size:12px}
 main{padding:18px 22px;display:grid;gap:18px}
 .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px}
 .card{background:#161a20;border:1px solid #262c35;border-radius:10px;padding:12px 14px}
 .card b{display:block;font-size:22px;margin-top:4px}
 table{width:100%;border-collapse:collapse;background:#161a20;border:1px solid #262c35;border-radius:10px;overflow:hidden}
 th,td{padding:7px 10px;border-bottom:1px solid #20262f;text-align:left;font-size:13px}
 th{background:#12161c;color:#8b93a1;font-weight:600}
 tr:last-child td{border-bottom:none}
 .ok{color:#7ee2a8}.bad{color:#ff7b7b}.warn{color:#f0c674}.run{color:#6ea8fe}
 h2{font-size:13px;color:#8b93a1;text-transform:uppercase;letter-spacing:.06em;margin:0}
</style></head>
<body>
<header><h1>StudioBench monitor</h1><span class="muted" id="meta">loading…</span></header>
<main>
 <section class="cards" id="cards"></section>
 <section><h2>Models</h2><table id="models"></table></section>
 <section><h2>Tool usage</h2><table id="tools"></table></section>
 <section><h2>Recent runs</h2><table id="recent"></table></section>
</main>
<script>
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function card(label, value) { return `<div class="card">${esc(label)}<b>${esc(value)}</b></div>`; }
async function tick(){
  const r = await fetch("/api/status"); const d = await r.json(); const t = d.totals;
  document.getElementById("meta").textContent = `${d.root} · refreshed ${new Date(d.generated_at*1000).toLocaleTimeString()}`;
  document.getElementById("cards").innerHTML = [
    card("Runs", t.runs), card("Done", t.done), card("Running", t.running),
    card("Failed", t.failed), card("Unsubmitted", t.unsubmitted),
    card("Tool calls", t.tool_calls), card("Tool errors", t.tool_errors), card("Cost USD", t.cost_usd)
  ].join("");
  document.getElementById("models").innerHTML = "<tr><th>model</th><th>runs</th><th>done</th><th>failed</th><th>unsubmitted</th><th>avg s</th><th>calls</th><th>errors</th><th>cost</th></tr>" +
    d.models.map(m => `<tr><td>${esc(m.model)}</td><td>${m.runs}</td><td class="ok">${m.done}</td><td class="bad">${m.failed}</td><td class="warn">${m.unsubmitted}</td><td>${m.avg_seconds ?? "—"}</td><td>${m.tool_calls}</td><td>${m.tool_errors}</td><td>${m.cost_usd}</td></tr>`).join("");
  document.getElementById("tools").innerHTML = "<tr><th>tool</th><th>calls</th><th>errors</th></tr>" +
    d.tools.slice(0, 20).map(x => `<tr><td>${esc(x.tool)}</td><td>${x.calls}</td><td class="${x.errors?"bad":""}">${x.errors}</td></tr>`).join("");
  document.getElementById("recent").innerHTML = "<tr><th>task</th><th>model</th><th>status</th><th>seconds</th><th>calls</th><th>cost</th></tr>" +
    d.recent.map(x => `<tr><td>${esc(x.task)}</td><td>${esc(x.model)}</td><td class="${x.status==="done"?"ok":x.status==="running"?"run":x.status==="failed"?"bad":"warn"}">${esc(x.status)}</td><td>${x.seconds ?? "—"}</td><td>${x.tool_calls}</td><td>${x.cost_usd ?? "—"}</td></tr>`).join("");
}
tick().catch(e => document.getElementById("meta").textContent = String(e));
setInterval(() => tick().catch(()=>{}), 5000);
</script>
</body></html>
"""


class Handler(BaseHTTPRequestHandler):
    root: Path = Path(".")

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path.startswith("/api/status"):
            data = cached(self.root)
            self._send(200, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")
        elif self.path == "/healthz":
            self._send(200, b'{"ok": true}', "application/json")
        else:
            self._send(404, b"not found", "text/plain; charset=utf-8")

    def log_message(self, format: str, *args) -> None:  # quiet default logging
        return


def main() -> int:
    ap = argparse.ArgumentParser(description="StudioBench web monitor")
    ap.add_argument("--runs", default=os.environ.get("STUDIO_RUNS_DIR", "runs"))
    ap.add_argument("--host", default=os.environ.get("STUDIO_MONITOR_HOST", "127.0.0.1"))
    ap.add_argument("--port", type=int, default=int(os.environ.get("STUDIO_MONITOR_PORT", "8132")))
    ap.add_argument("--once", action="store_true", help="print one JSON snapshot and exit")
    args = ap.parse_args()

    root = Path(args.runs).resolve()
    if not root.is_dir():
        print("runs directory not found: %s" % root)
        return 1
    if args.once:
        print(json.dumps(collect(root), ensure_ascii=False, indent=2))
        return 0

    Handler.root = root
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print("monitor: http://%s:%d/  (runs: %s)" % (args.host, args.port, root), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
