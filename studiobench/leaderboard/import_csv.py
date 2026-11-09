# -*- coding: utf-8 -*-
"""Import existing machine and human leaderboard CSVs into a static page.

    python -m studiobench leaderboard-import \
        --machine exports/.../leaderboard160_v406.csv \
        --human exports/.../human_leaderboard.csv \
        --out site/leaderboard

The two boards are merged on the model name. Machine rows carry rank, score, submitted
counts and a rank interval; human rows carry the human score, rated cells and rank.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from typing import Dict, List

# CSV short names -> (canonical id, display name)
MODELS = {
    "gemini": ("gemini-4-argon", "Gemini 4 Argon"),
    "claude": ("claude-opus-5-5", "Claude Opus 5.5"),
    "gpt-6": ("gpt-6-astra", "GPT-6 Astra"),
    "doubao": ("doubao-seed-2-1-pro-260915", "Doubao Seed 2.1 Pro"),
    "deepseek": ("deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash"),
    "qwen": ("qwen3.8-max-0902", "Qwen3.8 Max"),
    "qwen-max": ("qwen3.8-max-0902", "Qwen3.8 Max"),
    "hy4": ("hy4", "Hy4"),
    "mimo": ("mimo-v2.6-pro", "MiMo v2.6 Pro"),
}


def norm(name: str) -> tuple:
    key = name.strip().lower()
    return MODELS.get(key, (key, name.strip()))


def read_csv(path: Path) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build(machine_csv: Path, human_csv: Path) -> List[dict]:
    rows: Dict[str, dict] = {}
    for row in read_csv(machine_csv):
        cid, display = norm(row["model"])
        rows[cid] = {"model": cid, "name": display,
                     "machine": {"rank": int(float(row["rank"])),
                                 "score": number(row.get("score")),
                                 "submitted": int(float(row.get("submitted") or 0)),
                                 "rank_lo": number(row.get("rank_lo")),
                                 "rank_hi": number(row.get("rank_hi")),
                                 "lead_next": number(row.get("lead_next"))},
                     "human": None}
    for row in read_csv(human_csv):
        cid, display = norm(row["model"])
        item = rows.setdefault(cid, {"model": cid, "name": display, "machine": None, "human": None})
        item["human"] = {"rank": int(float(row["rank"])),
                         "score": number(row.get("human_score")),
                         "cells": int(float(row.get("cells") or 0))}
    return sorted(rows.values(), key=lambda r: (r["machine"] or {}).get("rank", 999))


def _fmt(value, digits=2):
    return "—" if value is None else ("%.*f" % (digits, value))


def to_html(rows: List[dict], title: str, machine_label: str, human_label: str) -> str:
    body = []
    for row in rows:
        m, h = row.get("machine") or {}, row.get("human") or {}
        interval = ("%d–%d" % (int(m["rank_lo"]), int(m["rank_hi"]))
                    if m.get("rank_lo") and m.get("rank_hi") else "—")
        body.append(
            "<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
            "<td>%s</td><td>%s</td><td>%s</td></tr>" % (
                m.get("rank", "—"), html.escape(row["name"]), _fmt(m.get("score")),
                interval, m.get("submitted", "—"), _fmt(h.get("score")),
                h.get("cells", "—"), h.get("rank", "—")))
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e9ee;margin:0}
header{padding:22px;border-bottom:1px solid #262c35}main{padding:22px;max-width:1100px;margin:0 auto}
h1{font-size:20px;margin:0 0 6px}a{color:#6ea8fe;text-decoration:none}a:hover{text-decoration:underline}
table{width:100%%;border-collapse:collapse;font-size:13px;margin-top:14px}
th,td{padding:8px 10px;border-bottom:1px solid #20262f;text-align:right}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{background:#12161c;color:#8b93a1;font-weight:600}
tr:hover td{background:#131820}.muted{color:#8b93a1;font-size:12px}
</style></head><body>
<header><h1>%s</h1>
<p class="muted">%s · %s · 160 tasks · ranked by machine score. A dash means the model was not
rated on that board.</p>
<p class="muted"><a href="../">← process canvas</a></p></header>
<main><table>
<thead><tr><th>Rank</th><th>Model</th><th>Machine score</th><th>Rank interval</th>
<th>Submitted</th><th>Human score</th><th>Rated cells</th><th>Human rank</th></tr></thead>
<tbody>%s</tbody></table>
<p class="muted">Machine: %s. Human: %s. Scores are on the published 0–100 scale; rank
intervals come from paired bootstrap over tasks.</p></main></body></html>
""" % (html.escape(title), html.escape(title), html.escape(machine_label), html.escape(human_label),
       "\n".join(body), html.escape(machine_label), html.escape(human_label))


def main() -> int:
    ap = argparse.ArgumentParser(description="Import machine + human leaderboard CSVs")
    ap.add_argument("--machine", required=True)
    ap.add_argument("--human", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="StudioBench leaderboard")
    ap.add_argument("--machine-label", default="machine v406")
    ap.add_argument("--human-label", default="human 1106")
    args = ap.parse_args()
    rows = build(Path(args.machine), Path(args.human))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(to_html(rows, args.title, args.machine_label, args.human_label),
                                    encoding="utf-8")
    print("wrote %d models to %s" % (len(rows), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
