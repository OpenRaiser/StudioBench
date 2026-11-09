#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把多次运行的 eval/eval.json 汇总成排行表。

    python3 aggregate.py                                  # 默认扫 StudioBench/runs
    python3 aggregate.py --runs <dir> --include-invalid --out leaderboard.md

按模型出：成片分 / 画面分 / 声音分 / 声画分（各自对算出了数的题取平均，并给出覆盖题数）；
声音分、声画分再按 audio_profile 标签（speech / music / sfx / dialogue）分列，
MV 和讲解片不混在一个音频均分里（文档 §1）。默认只统计 valid_for_bench 的运行。
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

RUNS_DIR = os.environ.get("STUDIO_RUNS_DIR", str(Path.cwd() / "runs"))
LABELS = ("speech", "music", "sfx", "dialogue")
MAIN = ("film", "visual", "audio", "av")


def _mean(vals: List[Optional[float]]) -> Optional[float]:
    xs = [v for v in vals if v is not None]
    return round(sum(xs) / len(xs), 4) if xs else None


def combine_overall(per_label: Dict[str, Optional[float]], counts: Dict[str, int]) -> Optional[float]:
    """按标签分列之后的「总平均」（文档 §1：口径待定）。

    per_label: 标签 → 该模型在带这个标签的题上的平均分（没有题或没算出来为 None）
    counts:    标签 → 参与平均的题数
    返回总平均；还没定口径时抛 NotImplementedError，表里显示「待定」。
    """
    # TODO(xenoxu): 选一种总平均口径，5–10 行即可：
    #   · 各标签等权平均：MV（music）和讲解（speech）权重相同，不受各类题数量影响
    #   · 按题数加权：等价于把所有题直接平均，题多的标签影响更大
    #   · 只有所有标签都算出了分才给总平均，否则返回 None，避免缺项的模型占便宜
    raise NotImplementedError


def collect(root: Path, include_invalid: bool) -> List[Dict[str, Any]]:
    rows = []
    for ev in sorted(root.glob("**/eval/eval.json")):
        data = json.loads(ev.read_text(encoding="utf-8"))
        if not include_invalid and not data.get("valid_for_bench"):
            continue
        try:
            task = json.loads(Path(json.loads((ev.parent.parent / "result.json").read_text())["task_file"]).read_text())
        except Exception:  # noqa: BLE001
            task = {}
        data["_profile"] = task.get("audio_profile") or []
        data["_domain"] = task.get("domain")
        rows.append(data)
    return rows


def table(rows: List[Dict[str, Any]]) -> str:
    models = sorted({str(r.get("model")) for r in rows})
    lines = ["| 模型 | 题数 | 成片分 | 画面分 | 声音分 | 声画分 | " + " | ".join("声音·%s" % l for l in LABELS) + " | 总平均 |",
             "|" + " --- |" * (7 + len(LABELS))]
    for m in models:
        mine = [r for r in rows if str(r.get("model")) == m]
        cells = []
        for key in MAIN:
            vals = [r["scores"].get(key) for r in mine]
            mean = _mean(vals)
            cells.append("—" if mean is None else "%.3f (%d)" % (mean, sum(v is not None for v in vals)))
        per_label, counts = {}, {}
        for label in LABELS:
            vals = [r["scores"].get("audio") for r in mine if label in r["_profile"]]
            per_label[label], counts[label] = _mean(vals), sum(v is not None for v in vals)
        try:
            overall = combine_overall(per_label, counts)
            overall_cell = "—" if overall is None else "%.3f" % overall
        except NotImplementedError:
            overall_cell = "待定"
        lines.append("| %s | %d | %s | %s | %s |" % (
            m, len(mine), " | ".join(cells),
            " | ".join("—" if per_label[l] is None else "%.3f (%d)" % (per_label[l], counts[l]) for l in LABELS),
            overall_cell))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=RUNS_DIR)
    ap.add_argument("--include-invalid", action="store_true", help="把 mock / 隔离失败 / 后端不全的运行也算进来")
    ap.add_argument("--out")
    args = ap.parse_args()
    rows = collect(Path(args.runs), args.include_invalid)
    if not rows:
        print("没有可汇总的 eval.json（默认只统计 valid_for_bench 的运行，加 --include-invalid 看全部）")
        return 1
    text = table(rows) + "\n\n括号里是算出了该项分数的题数；— 表示没有可用的分数。\n"
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
