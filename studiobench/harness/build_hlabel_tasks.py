#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把定版数据集 data/items.jsonl（人标入选 1276 条）转成题目 JSON。

    python3 -m studiobench.harness.build_hlabel_tasks \
        --jsonl ../../data/items.jsonl --out tasks_hlabel

规则（2026-10-28 定；沿用 build_csv_tasks.py 的口径）：
· 用户消息只用 human.brief（→ wish）。full_prompt 是按秒转写的原片结构，等于把参考视频告诉模型，
  一个字都不写进题目；这轮评分项也留空，所以 full_prompt 全程不用
· 时长取 media.duration（实测原片时长），压进文档要求的 60–180 秒：
      >180 秒 → 120–180 秒（brief 里的时长表述改成「约150秒」）
      <60 秒  → 60–65 秒  （改成「约60秒」）
      其余    → ±5 秒，和 demo1024 一致
  brief 里的时长表述一并改写，否则用户消息里「期望：…约300秒」和「时长：120–180 秒」自相矛盾
· 画幅取 cand.media.aspect（16:9 / 9:16 …）；音频画像取 ai.audio_tags
· must / forbid / constraints / plot_beats / facts 全部留空：本轮只负责产出，评测由别人做
· 1276 条全建，含 114 条 Assets / Reference —— 源数据没有随题文件，validate 会报 error 级
  task_issue，但 run.py 不阻断，跑起来就是纯 prompt 题
· search 屏蔽表照旧：原片链接 + 片名，挡住检索结果里的原片页面
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .build_csv_tasks import FAMILY, VIDEO_SITES, title_phrases, url_key

# brief 里的时长表述：约 N 分 M 秒 / 约 N 分钟 / 约 N 秒 / N 至 M 分钟 / N 至 M 秒 / 时长 N 分钟
_DUR = [
    re.compile(r"约\s*\d+\s*分\s*\d+\s*秒"),
    re.compile(r"约\s*\d+\s*分钟?"),
    re.compile(r"约\s*\d+\s*秒"),
    re.compile(r"\d+\s*(?:至|到|~|-|—)\s*\d+\s*分钟?"),
    re.compile(r"\d+\s*(?:至|到|~|-|—)\s*\d+\s*秒"),
    re.compile(r"时长\s*\d+\s*(?:分钟?|秒)"),
]
_TAGS = {"Music": "music", "SFX/Ambience": "sfx", "Speech": "speech", "Dialogue": "dialogue"}
_ORDER = ("speech", "dialogue", "music", "sfx")
DOC_MIN, DOC_MAX = 60, 180


def retime(brief: str, secs: int) -> str:
    """把 brief 里第一处时长表述改成「约{secs}秒」；没有表述就不动。"""
    for pat in _DUR:
        m = pat.search(brief)
        if m:
            return brief[:m.start()] + "约%d秒" % secs + brief[m.end():]
    return brief


def target(duration: Optional[float]) -> Tuple[int, List[int]]:
    """实测时长 → (写进 brief 的秒数, duration_sec 区间)，并保证区间落在 60–180 秒里。"""
    d = int(round(float(duration or 0)))
    if d > DOC_MAX:                      # 长片压到 120–180 秒
        return 150, [120, DOC_MAX]
    if d < DOC_MIN:                      # 短片抬到 60–65 秒
        return DOC_MIN, [DOC_MIN, DOC_MIN + 5]
    return d, [max(DOC_MIN, d - 5), min(DOC_MAX, d + 5)]


def audio_profile(tags: Optional[List[str]]) -> List[str]:
    prof = [_TAGS[t] for t in (tags or []) if t in _TAGS]
    if not prof:
        prof = ["music", "sfx"]
    return [k for k in _ORDER if k in prof]


def build(recs: List[Dict[str, Any]], src: Path) -> tuple:
    tasks, block = [], {"url_keys": [], "domains": list(VIDEO_SITES), "phrases": [], "all_of": []}
    for r in recs:
        human = r.get("human") or {}
        brief = (human.get("brief") or "").strip()
        family = FAMILY.get((human.get("task_family") or "").strip().lower())
        if not brief:
            print("跳过（brief 为空）：%s" % r.get("task_id"), file=sys.stderr)
            continue
        if not family:
            print("跳过（task_family 认不出 %r）：%s" % (human.get("task_family"), r.get("task_id")),
                  file=sys.stderr)
            continue
        secs, span = target((r.get("media") or {}).get("duration"))
        task = {
            "id": r["task_id"], "domain": (human.get("domain") or "").strip(), "task": family,
            "audio_profile": audio_profile((r.get("ai") or {}).get("audio_tags")),
            "wish": retime(brief, secs) if secs != int(round(float((r.get("media") or {}).get("duration") or 0))) else brief,
            "duration_sec": span,
            "aspect": ((r.get("cand") or {}).get("media") or {}).get("aspect") or None,
            "must": [], "forbid": [], "constraints": [], "plot_beats": [], "facts": [],
            "assets": [], "references": [], "given_audio": None,
            "source": {"jsonl": str(src), "task_id": r["task_id"], "batch": r.get("batch"),
                       "media_duration": (r.get("media") or {}).get("duration"),
                       "rule": "brief only; no full_prompt; duration from media.duration (clamped 60-180); "
                               "rubric empty (产出轮，评测由别人做)"},
        }
        tasks.append(task)
        if human.get("source_url"):
            block["url_keys"].append(url_key(str(human["source_url"]).strip()))
        for p in title_phrases(human.get("title") or "", brief):
            if p not in block["phrases"]:
                block["phrases"].append(p)
    return tasks, block


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--jsonl", required=True, help="data/items.jsonl")
    ap.add_argument("--out", required=True, help="题目目录（会新建）")
    ap.add_argument("--force", action="store_true", help="覆盖已有题目文件")
    ap.add_argument("--block-extra", help="手工补充的屏蔽项（JSON），合并进 search_block.json")
    args = ap.parse_args(argv)
    src = Path(args.jsonl).resolve()
    recs = [json.loads(l) for l in src.read_text(encoding="utf-8").splitlines() if l.strip()]
    tasks, block = build(recs, src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    written = 0
    for t in tasks:
        f = out / ("%s.json" % t["id"])
        if f.exists() and not args.force:
            print("已存在，跳过（加 --force 覆盖）：%s" % f, file=sys.stderr)
            continue
        f.write_text(json.dumps(t, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        written += 1
    if args.block_extra:
        extra = json.loads(Path(args.block_extra).read_text(encoding="utf-8"))
        for key in ("url_keys", "domains", "phrases", "all_of"):
            block[key] += [v for v in extra.get(key) or [] if v not in block[key]]
    (out / "search_block.json").write_text(json.dumps(block, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    print("题目 %d 条 → %s（新写 %d）" % (len(tasks), out, written))
    print("search 屏蔽表：%d 个链接、%d 个域名、%d 个短语、%d 组组合词"
          % (len(block["url_keys"]), len(block["domains"]), len(block["phrases"]), len(block["all_of"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
