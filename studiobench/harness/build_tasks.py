#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 studio_ref_v1021.jsonl 转成 StudioBench 题目（harness/tasks/*.json）。

    python3 build_tasks.py --matrix                      # 看可用格子
    python3 build_tasks.py --domain narrative --family open --limit 3
    python3 build_tasks.py --domain music --family audio-led --limit 1 --force
    python3 build_tasks.py --ids narr-open-001           # 重建指定题

约定：
· `wish` 只放 prompt.brief —— 用户消息只给期望/素材/参考/音频，不给 must/forbid
· `must` / `forbid` / `constraints` 先留空，由 rubric 生成器补（full_prompt 存在 brief_full）
· `duration_sec` = 实测时长 ±5s，再夹进 [--duration-min, --duration-max]（默认 60–180，文档：每题 1–3 分钟）
· 冒烟题用 --smoke 放宽时长（题目里写 "smoke": true，评测时只告警不报错）
· 题目和素材是数据，默认写到共享盘 StudioBench/harness/tasks、assets（代码在本地盘）
· Audio-led → 从原片抽音轨到 assets/<id>/given_audio.wav
· Assets / Reference → 从原片抽 15% / 50% / 85% 三个时间点的帧
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import taskspec
from ..tools import media

SHARE = Path(os.environ.get("STUDIO_SHARE", str(Path.cwd())))
FFMPEG = media.FFMPEG
DATA = os.environ.get("STUDIO_REF_JSONL", "")

FAMILY_SLUG = {"Open Brief": "open", "Constraint": "constraint", "Assets": "assets",
               "Reference": "reference", "Audio-led": "audio-led"}
DOMAIN_SLUG = {"narrative": "narr", "commercial": "comm", "product": "prod",
               "education": "educ", "music": "musi", "documentary": "docu",
               "creator": "crea", "dialogue": "dial"}
AUDIO_SLUG = {"Speech": "speech", "Music": "music",
              "SFX/Ambience": "sfx", "Dialogue": "dialogue"}


def _ff(*args: str) -> None:
    proc = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                          capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 失败：%s" % proc.stderr[-400:])


def _aspect(width: int, height: int) -> str:
    ratio = width / height if height else 16 / 9
    for name, want in (("16:9", 16 / 9), ("9:16", 9 / 16), ("4:3", 4 / 3),
                       ("3:4", 3 / 4), ("1:1", 1.0), ("21:9", 21 / 9)):
        if abs(ratio - want) / want < 0.03:
            return name
    return "16:9" if ratio >= 1 else "9:16"


def _video_path(record: Dict[str, Any]) -> Path:
    media = record.get("media") or {}
    path = media.get("path") or ""
    for cand in (path, path + ".mp4"):
        if cand and Path(cand).is_file():
            return Path(cand)
    raise FileNotFoundError("原片不存在：%s" % path)


def _duration_range(target: float, lo_cap: float = 60, hi_cap: float = 180) -> List[int]:
    """实测时长 ±5 秒，夹进 [lo_cap, hi_cap]；夹完区间太窄（<6 秒）时向另一侧补。"""
    low = max(lo_cap, round(target - 5))
    high = min(hi_cap, round(target + 5))
    if high - low < 6:
        if low == lo_cap:
            high = min(hi_cap, low + 6)
        else:
            low = max(lo_cap, high - 6)
    return [int(low), int(high)]


def _extract_audio(src: Path, out: Path) -> None:
    _ff("-i", str(src), "-vn", "-acodec", "pcm_s16le", "-ar", "48000", "-ac", "2", str(out))


def _extract_frame(src: Path, at: float, out: Path) -> None:
    _ff("-ss", "%.3f" % at, "-i", str(src), "-frames:v", "1", str(out))


def build(record: Dict[str, Any], index: int, out_dir: Path, asset_root: Path, force: bool,
          dmin: float = 60, dmax: float = 180, smoke: bool = False) -> Optional[Path]:
    fam_raw = record.get("task_family") or "Open Brief"
    fam = FAMILY_SLUG.get(fam_raw)
    dom = record.get("domain") or "narrative"
    if fam is None or dom not in DOMAIN_SLUG:
        return None

    media = record.get("media") or {}
    prompt = record.get("prompt") or {}
    duration = float(media.get("duration") or prompt.get("duration_target_s") or 60)
    task_id = "%s-%s-%03d" % (DOMAIN_SLUG[dom], fam, index)
    task_path = out_dir / ("%s.json" % task_id)
    if task_path.exists() and not force:
        return task_path

    audio = [AUDIO_SLUG[a] for a in (prompt.get("audio_deps") or [])
             if a in AUDIO_SLUG] or ["sfx"]
    task: Dict[str, Any] = {
        "id": task_id,
        "domain": dom,
        "task": fam,
        "audio_profile": audio,
        "wish": prompt.get("brief") or record.get("description") or record.get("title") or "",
        "duration_sec": _duration_range(duration, dmin, dmax),
        "aspect": _aspect(int(media.get("width") or 1920), int(media.get("height") or 1080)),
        "must": [],
        "forbid": [],
        "constraints": [],
        "plot_beats": [],
        "facts": [],
        "assets": [],
        "references": [],
        "given_audio": None,
        # 以下只做溯源与后续 rubric 生成，不会进用户消息
        "brief_full": prompt.get("full_prompt"),
        "style": prompt.get("style"),
        "ref_shots": prompt.get("shots"),
        "source": {"video_id": record.get("video_id"), "url": record.get("source_url"),
                   "title": record.get("title")},
    }

    try:
        src = _video_path(record)
    except FileNotFoundError as exc:
        print("跳过 %s：%s" % (task_id, exc), file=sys.stderr)
        return None

    adir = asset_root / task_id
    adir.mkdir(parents=True, exist_ok=True)

    if fam == "audio-led":
        wav = adir / "given_audio.wav"
        if force or not wav.exists():
            _extract_audio(src, wav)
        task["given_audio"] = str(wav)

    if fam in ("assets", "reference"):
        marks = [(0.15, "a"), (0.50, "b")]
        if fam == "reference":
            marks.append((0.85, "c"))
        files: List[str] = []
        for pos, tag in marks:
            png = adir / ("ref_%s.png" % tag)
            if force or not png.exists():
                _extract_frame(src, duration * pos, png)
            files.append(str(png))
        if fam == "reference":
            clip = adir / "ref_clip.mp4"
            if force or not clip.exists():
                _ff("-ss", "%.3f" % (duration * 0.1), "-t", "4", "-i", str(src),
                    "-an", "-c:v", "libx264", "-preset", "veryfast",
                    "-pix_fmt", "yuv420p", str(clip))
            task["references"] = files + [str(clip)]
        else:
            task["assets"] = files

    if smoke:
        task["smoke"] = True
    task_path.write_text(json.dumps(task, ensure_ascii=False, indent=2), encoding="utf-8")
    for level, msg in taskspec.validate(task):
        print("  [%s] %s：%s" % (level, task_id, msg), file=sys.stderr)
    return task_path


def load_records() -> List[Dict[str, Any]]:
    path = Path(DATA)
    if not path.is_file():
        raise SystemExit("找不到数据集：%s（用 STUDIO_REF_JSONL 指定标注 jsonl，"
                         "或把数据挂载到本机）" % path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def cmd_matrix(records: List[Dict[str, Any]], dmin: float = 60, dmax: float = 180) -> None:
    all_keep = [r for r in records if (r.get("quality") or {}).get("verdict") == "keep"]
    keep = [r for r in all_keep
            if dmin <= float((r.get("media") or {}).get("duration") or 0) <= dmax]
    fams = list(FAMILY_SLUG.values())
    counts: Dict[Any, int] = {}
    for r in keep:
        key = (r["domain"], FAMILY_SLUG.get(r["task_family"]))
        counts[key] = counts.get(key, 0) + 1
    print("keep %d，时长 %s–%ss 内 %d / total %d\n"
          % (len(all_keep), dmin, dmax, len(keep), len(records)))
    print("%-12s" % "domain" + "".join("%-11s" % f for f in fams))
    for dom in sorted({r["domain"] for r in keep}):
        print("%-12s" % dom + "".join("%-11d" % counts.get((dom, f), 0) for f in fams))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--domain")
    ap.add_argument("--family", help="open|constraint|assets|reference|audio-led")
    ap.add_argument("--limit", type=int, default=1)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--duration-min", type=float, default=60,
                    help="成片时长下限（秒）；目标文档要求 1–3 分钟")
    ap.add_argument("--duration-max", type=float, default=180,
                    help="成片时长上限（秒）")
    ap.add_argument("--force", action="store_true", help="覆盖已有题目与素材")
    ap.add_argument("--matrix", action="store_true", help="只打印可用格子")
    ap.add_argument("--smoke", action="store_true", help="冒烟题：允许时长不在 60–180 秒内")
    ap.add_argument("--out-dir", default=str(SHARE / "harness" / "tasks"))
    ap.add_argument("--asset-root", default=str(SHARE / "harness" / "assets"))
    args = ap.parse_args()

    records = load_records()
    if args.matrix:
        cmd_matrix(records, args.duration_min, args.duration_max)
        return 0

    keep = [r for r in records if (r.get("quality") or {}).get("verdict") == "keep"]
    sel = [r for r in keep
           if args.duration_min <= float((r.get("media") or {}).get("duration") or 0)
           <= args.duration_max]
    if args.domain:
        sel = [r for r in sel if r["domain"] == args.domain]
    if args.family:
        sel = [r for r in sel if FAMILY_SLUG.get(r["task_family"]) == args.family]
    rows = sel[args.offset: args.offset + args.limit]
    if not rows:
        print("筛选后没有记录（时长区间 %s–%ss）" % (args.duration_min, args.duration_max),
              file=sys.stderr)
        return 2

    out_dir = Path(args.out_dir)
    asset_root = Path(args.asset_root)
    out_dir.mkdir(parents=True, exist_ok=True)
    built = []
    for i, r in enumerate(rows, start=args.offset + 1):
        path = build(r, i, out_dir, asset_root, force=args.force,
                     dmin=args.duration_min, dmax=args.duration_max, smoke=args.smoke)
        if path:
            built.append(path)
            print("wrote %s" % path)
    return 0 if built else 1


if __name__ == "__main__":
    sys.exit(main())
