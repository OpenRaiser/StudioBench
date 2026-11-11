# -*- coding: utf-8 -*-
"""Import a published board CSV (v410-style) into a static leaderboard page.

    python -m studiobench leaderboard-import \\
        --board outputs/.../leaderboard160_grade_r01.csv \\
        --human exports/.../human_leaderboard.csv \\
        --exclude qwen3.8-27b \\
        --out site/leaderboard

Board CSV columns: rank, model, submitted, score, rank_lo, rank_hi, lead_next, AUD, SEM,
VIS, AV. The human CSV is optional and only shown as a reference column.
"""
from __future__ import annotations

import argparse
import csv
import html
import json
from pathlib import Path
from typing import Dict, List

MODELS = {
    "gemini": ("gemini-4-argon", "Gemini 4 Argon"),
    "claude": ("claude-opus-5-5", "Claude Opus 5.5"),
    "gpt-6": ("gpt-6-astra", "GPT-6 Astra"),
    "doubao": ("doubao-seed-2-1-pro-260915", "Doubao Seed 2.1 Pro"),
    "deepseek": ("deepseek-v4.1-flash-1017", "DeepSeek V4.1 Flash"),
    "qwen": ("qwen3.8-max-0902", "Qwen3.8 Max"),
    "qwen-max": ("qwen3.8-max-0902", "Qwen3.8 Max"),
    "qwen-27b": ("qwen3.8-27b", "Qwen3.8 27B"),
    "mimo": ("mimo-v2.6-pro", "MiMo v2.6 Pro"),
}
BOARDS = ("AUD", "SEM", "VIS", "AV")


def norm(name: str) -> tuple:
    return MODELS.get(name.strip().lower(), (name.strip().lower(), name.strip()))


def read_csv(path: Path) -> List[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build(board_csv: Path, human_csv: Path | None, exclude: set) -> List[dict]:
    rows: Dict[str, dict] = {}
    for row in read_csv(board_csv):
        cid, display = norm(row["model"])
        if cid in exclude:
            continue
        rows[cid] = {
            "model": cid, "name": display,
            "rank": int(float(row["rank"])), "score": number(row.get("score")),
            "submitted": int(float(row.get("submitted") or 0)),
            "rank_lo": number(row.get("rank_lo")), "rank_hi": number(row.get("rank_hi")),
            "lead_next": number(row.get("lead_next")),
            "boards": {b: number(row.get(b)) for b in BOARDS},
            "human": None,
        }
    if human_csv:
        for row in read_csv(human_csv):
            cid, _ = norm(row["model"])
            if cid in rows:
                rows[cid]["human"] = {"rank": int(float(row["rank"])),
                                      "score": number(row.get("human_score")),
                                      "cells": int(float(row.get("cells") or 0))}
    ordered = sorted(rows.values(), key=lambda r: (-(r["score"] or 0), r["rank"]))
    for display_rank, row in enumerate(ordered, 1):
        row["display_rank"] = display_rank
    rated = sorted((r for r in ordered if r["human"]), key=lambda r: r["human"]["rank"])
    for human_rank, row in enumerate(rated, 1):
        row["human"]["rank"] = human_rank
    return ordered


def _f(value, digits=2):
    return "—" if value is None else (("%.*f" % (digits, value)) if isinstance(value, float) else str(value))


def to_html(rows: List[dict], title: str, label: str, human_label: str | None) -> str:
    body = []
    for row in rows:
        interval = ("%d–%d" % (int(row["rank_lo"]), int(row["rank_hi"]))
                    if row.get("rank_lo") is not None and row.get("rank_hi") is not None else "—")
        human = row.get("human") or {}
        cells = [str(row["display_rank"]), html.escape(row["name"]), _f(row["score"]),
                 interval, str(row["submitted"])]
        cells += [_f(row["boards"][b], 1) for b in BOARDS]
        if human_label:
            cells += [_f(human.get("score")), human.get("cells", "—"), human.get("rank", "—")]
        body.append("<tr>" + "".join("<td>%s</td>" % c for c in cells) + "</tr>")
    heads = ["Rank", "Model", "Score", "95% rank interval", "Submitted"]
    heads += BOARDS
    if human_label:
        heads += ["Human score", "Rated cells", "Human rank"]
    note = ("Human column is shown as a reference only; ranking and scores come from the "
            "published machine board." if human_label else "")
    return """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>%s</title>
<style>
body{font-family:system-ui,-apple-system,"Segoe UI",sans-serif;background:#0f1115;color:#e6e9ee;margin:0}
header{padding:22px;border-bottom:1px solid #262c35}main{padding:22px;max-width:1180px;margin:0 auto}
h1{font-size:20px;margin:0 0 6px}a{color:#6ea8fe;text-decoration:none}a:hover{text-decoration:underline}
table{width:100%%;border-collapse:collapse;font-size:13px;margin-top:14px}
th,td{padding:8px 10px;border-bottom:1px solid #20262f;text-align:right;white-space:nowrap}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){text-align:left}
th{background:#12161c;color:#8b93a1;font-weight:600}
tr:hover td{background:#131820}.muted{color:#8b93a1;font-size:12px}
</style></head><body>
<header><h1>%s</h1>
<p class="muted">%s · grades are 1–3 converted with development-set cut points; a missing
cut counts as 0. %s</p>
<p class="muted"><a href="../">← process canvas</a></p></header>
<main><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table>
<p class="muted">Ranks are renumbered after omitting models excluded from this page; 95%%
intervals come from the original published board (paired bootstrap over tasks). AUD/SEM/VIS/AV
are board means on the same 0–100 scale.</p></main></body></html>
""" % (html.escape(title), html.escape(title), html.escape(label), html.escape(note),
       "".join("<th>%s</th>" % html.escape(h) for h in heads), "\n".join(body))


def main() -> int:
    ap = argparse.ArgumentParser(description="Import a published board CSV")
    ap.add_argument("--board", required=True)
    ap.add_argument("--human", default="", help="optional human board CSV (reference only)")
    ap.add_argument("--out", required=True)
    ap.add_argument("--title", default="StudioBench leaderboard")
    ap.add_argument("--label", default="machine v4.1.0 · grade mode")
    ap.add_argument("--human-label", default="human 1106",
                    help="label for the reference human column; empty to hide it")
    ap.add_argument("--exclude", default="qwen3.8-27b",
                    help="comma-separated canonical model ids to omit")
    args = ap.parse_args()
    exclude = {x.strip() for x in args.exclude.split(",") if x.strip()}
    rows = build(Path(args.board), Path(args.human) if args.human else None, exclude)
    if not rows:
        print("no rows after filtering")
        return 1
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "data.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "index.html").write_text(
        to_html(rows, args.title, args.label, args.human_label or None), encoding="utf-8")
    print("wrote %d models to %s" % (len(rows), out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
