#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 test_prompt 下的 demo 表格（title / source_url / domain / task_family / brief / full_prompt）转成题目 JSON。

    python3 -m studiobench.harness.build_csv_tasks --csv "<表格.csv>" --out tasks_demo1024 --prefix demo1024

规则（2026-10-24 定）：
· 用户消息只用 brief。full_prompt 是按秒转写的原片结构，等于把参考视频告诉模型，一个字都不写进题目
· 不从原片抽帧、抽音轨：assets / references / given_audio 一律为空，模型完全看不到参考视频
· 时长取自 brief 里的「约 X 分 Y 秒 / 约 N 秒」，±5 秒；brief 没写画幅就不给画幅（不从 full_prompt 补）
· 题目 id 不带片名（id 会出现在运行目录里，模型能看到工作目录路径）
· 另写一份 search 屏蔽表（原片链接、片名、视频站），挡住检索结果里的原片页面；它不在任何工作目录里，模型读不到。
  片名里的通用词（如「流浪地球3」「沉浸式开箱」）不自动屏蔽，否则正常查资料也会被挡；
  这类要组合着挡的，写进 --block-extra（{"phrases": [...], "all_of": [[...], ...], "domains": [...]}）合并进来
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any, Dict, List, Optional

VIDEO_SITES = ["bilibili.com", "b23.tv", "liblib.tv", "liblib.art", "douyin.com", "iesdouyin.com", "kuaishou.com",
               "ixigua.com", "xiaohongshu.com", "xhslink.com", "youtube.com", "youtu.be", "vimeo.com", "acfun.cn",
               "v.qq.com", "youku.com", "iqiyi.com", "weishi.qq.com", "haokan.baidu.com"]
FAMILY = {"open brief": "open", "open": "open", "constraint": "constraint", "assets": "assets",
          "reference": "reference", "audio-led": "audio-led"}
SPEECH = ("旁白", "独白", "讲解", "解说", "介绍", "口播", "配音", "朗读")
DIALOGUE = ("对白", "对话", "台词", "腔")


def duration(brief: str) -> Optional[int]:
    m = re.search(r"约\s*(\d+)\s*分\s*(\d+)\s*秒", brief)
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    m = re.search(r"约\s*(\d+)\s*分钟", brief)
    if m:
        return int(m.group(1)) * 60
    m = re.search(r"约\s*(\d+)\s*秒", brief)
    return int(m.group(1)) if m else None


def audio_profile(brief: str) -> List[str]:
    prof = []
    if any(k in brief for k in SPEECH):
        prof.append("speech")
    if any(k in brief for k in DIALOGUE):
        prof.append("dialogue")
    return prof + ["music", "sfx"]


def url_key(url: str) -> str:
    """链接里能唯一标识原片的部分：B 站 BV 号、liblib 详情页 id；都不是就用去掉协议的整条链接。"""
    m = re.search(r"(BV[0-9A-Za-z]{10})", url) or re.search(r"/detail/([0-9a-f]{16,})", url)
    return m.group(1) if m else re.sub(r"^https?://", "", url).rstrip("/")


def title_phrases(title: str, brief: str) -> List[str]:
    """片名里够独特的片段：整条标题，加上书名号/方括号里、brief 没提到的内容（如获奖名、赛道名）。
    brief 里本来就有的词（片名、产品名）模型查资料时一定会用，自动屏蔽会把正常资料一起挡掉，交给 --block-extra。"""
    def norm(t: str) -> str:
        return re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", t).lower())
    out = [title.strip()] if len(norm(title)) >= 6 else []
    for p in re.findall(r"[《【]([^》】]+)[》】]", title):
        if len(norm(p)) >= 4 and norm(p) not in norm(brief) and p not in out:
            out.append(p.strip())
    return out


def build(rows: List[Dict[str, str]], csv_path: Path, prefix: str) -> tuple:
    tasks, block = [], {"url_keys": [], "domains": list(VIDEO_SITES), "phrases": [], "all_of": []}
    for i, row in enumerate(rows, 1):
        brief = (row.get("brief") or "").strip()
        family = FAMILY.get((row.get("task_family") or "").strip().lower())
        if not brief or not family:
            print("第 %d 行跳过：brief 或 task_family 不对（%r）" % (i, row.get("task_family")), file=sys.stderr)
            continue
        secs = duration(brief)
        task: Dict[str, Any] = {
            "id": "%s-%02d" % (prefix, i), "domain": (row.get("domain") or "").strip(), "task": family,
            "audio_profile": audio_profile(brief), "wish": brief,
            "duration_sec": [max(1, secs - 5), secs + 5] if secs else [],
            "aspect": None,
            "must": [], "forbid": [], "constraints": [], "plot_beats": [], "facts": [],
            "assets": [], "references": [], "given_audio": None,
            "source": {"csv": str(csv_path), "row": i, "rule": "brief only; no full_prompt; no reference media"},
        }
        tasks.append(task)
        if row.get("source_url"):
            block["url_keys"].append(url_key(row["source_url"].strip()))
        block["phrases"] += [p for p in title_phrases(row.get("title") or "", brief) if p not in block["phrases"]]
    return tasks, block


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--csv", required=True)
    ap.add_argument("--out", required=True, help="题目目录（会新建）")
    ap.add_argument("--prefix", default="demo")
    ap.add_argument("--force", action="store_true", help="覆盖已有题目文件")
    ap.add_argument("--block-extra", help="手工补充的屏蔽项（JSON），合并进 search_block.json")
    args = ap.parse_args(argv)
    csv_path = Path(args.csv).resolve()
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8-sig", newline="")))
    tasks, block = build(rows, csv_path, args.prefix)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for t in tasks:
        f = out / ("%s.json" % t["id"])
        if f.exists() and not args.force:
            print("已存在，跳过（加 --force 覆盖）：%s" % f, file=sys.stderr)
            continue
        f.write_text(json.dumps(t, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("%s  %-10s %-10s %s 秒  %s" % (t["id"], t["domain"], t["task"], t["duration_sec"], t["wish"][:40]))
    if args.block_extra:
        extra = json.loads(Path(args.block_extra).read_text(encoding="utf-8"))
        for key in ("url_keys", "domains", "phrases", "all_of"):
            block[key] += [v for v in extra.get(key) or [] if v not in block[key]]
    (out / "search_block.json").write_text(json.dumps(block, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("search 屏蔽表：%s（%d 个链接、%d 个域名、%d 个短语、%d 组组合词）" % (
        out / "search_block.json", len(block["url_keys"]), len(block["domains"]), len(block["phrases"]),
        len(block["all_of"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
