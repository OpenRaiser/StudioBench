# -*- coding: utf-8 -*-
"""§4.1 不依赖生成模型的指标：时长 / 画幅 / 音轨、闪烁（只算静止段）、人声没被盖住。"""
from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools import media
from ..tools.backends.common import env, env_int, run_argv_template

ASPECTS = {"16:9": 16 / 9, "9:16": 9 / 16, "4:3": 4 / 3, "3:4": 3 / 4, "1:1": 1.0, "21:9": 21 / 9}


def spec(final: Path, task: Dict[str, Any]) -> Dict[str, Any]:
    info = media.probe(final)
    out: Dict[str, Any] = {k: info[k] for k in ("duration_s", "width", "height", "fps", "has_audio")}
    rng = list(task.get("duration_sec") or [])
    lo, hi = (rng + [None, None])[:2]
    if lo is not None and hi is not None and info["duration_s"] is not None:
        out["duration_in_range"] = float(lo) <= info["duration_s"] <= float(hi)
    want = ASPECTS.get(task.get("aspect") or "")
    if want and info["width"] and info["height"]:
        out["aspect_ok"] = abs(info["width"] / info["height"] - want) / want < 0.02
    if info["width"] and info["height"]:
        out["short_side"] = min(info["width"], info["height"])
    return out


def flicker_cal_score(video: Path) -> Optional[float]:
    """VBench temporal_flickering 的 cal_score：相邻帧像素 MAE 的均值 → (255 - MAE) / 255。原分辨率，无权重。"""
    import numpy as np
    info = media.probe(video)
    w, h = info["width"], info["height"]
    if not w or not h:
        return None
    frame_bytes = w * h * 3
    proc = subprocess.Popen([media.FFMPEG, "-hide_banner", "-nostdin", "-loglevel", "error", "-i", str(video),
                             "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE,
                            stdin=subprocess.DEVNULL)
    prev, diffs = None, []
    assert proc.stdout
    while True:
        buf = proc.stdout.read(frame_bytes)
        if len(buf) < frame_bytes:
            break
        cur = np.frombuffer(buf, dtype=np.uint8).astype(np.float32)
        if prev is not None:
            diffs.append(float(np.abs(cur - prev).mean()))
        prev = cur
    proc.wait()
    if not diffs:
        return None
    return round((255.0 - float(np.mean(diffs))) / 255.0, 6)


def static_shots(shot_files: List[Path]) -> Optional[List[str]]:
    """静止段过滤（文档：VBench static_filter.py，RAFT）。通过 STUDIO_STATIC_FILTER_CMD 接入：
    JSON argv 模板，占位符 {videos_dir} {output}；命令把静止镜头的文件名列表写成 JSON 到 {output}。"""
    if not env("STUDIO_STATIC_FILTER_CMD") or not shot_files:
        return None
    with tempfile.TemporaryDirectory(prefix="static_") as tmp:
        result = Path(tmp) / "static.json"
        run_argv_template("STUDIO_STATIC_FILTER_CMD", {"videos_dir": str(shot_files[0].parent), "output": str(result)},
                          timeout=env_int("STUDIO_STATIC_FILTER_TIMEOUT", 3600))
        names = json.loads(result.read_text(encoding="utf-8"))
    return [Path(n).name for n in names]


def temporal_flickering(shot_files: List[Path]) -> Dict[str, Any]:
    try:
        static = static_shots(shot_files)
    except Exception as exc:  # noqa: BLE001
        return {"value": None, "reason": "静止段过滤失败：%s" % exc}
    if static is None:
        return {"value": None, "reason": "未接静止段过滤（RAFT 权重不在时按文档先空着；配置 STUDIO_STATIC_FILTER_CMD）"}
    picked = [p for p in shot_files if p.name in set(static)]
    if not picked:
        return {"value": None, "static_shots": [], "reason": "没有静止镜头"}
    scores = [s for s in (flicker_cal_score(p) for p in picked) if s is not None]
    return {"value": round(sum(scores) / len(scores), 6) if scores else None,
            "static_shots": [p.name for p in picked], "per_shot": scores}


def _energy_mean(values: List[Optional[float]]) -> Optional[float]:
    vals = [float(v) for v in values if v is not None and not math.isinf(v)]
    if not vals:
        return None
    return round(10 * math.log10(sum(10 ** (v / 10) for v in vals) / len(vals)), 2)


def vocal_unmasked(final: Path, mixes: List[Dict[str, Any]], work: Path) -> Dict[str, Any]:
    """成片的 Integrated LUFS 离人声轨更近（相对各非人声轨，配乐先乘 mix 里同一个 gain）则通过。
    只有 mix_audio 叠了第二条音轨（replace=false）时才算。"""
    if not any(not m["replace"] for m in mixes):
        return {"applicable": False, "value": None, "reason": "没有用 mix_audio 叠第二条音轨"}
    voice = [m for m in mixes if m["role"] == "voice"]
    others = [m for m in mixes if m["role"] in ("music", "sfx", "other")]
    if not voice or not others:
        return {"applicable": False, "value": None, "reason": "混音里缺人声轨或缺非人声轨，无从比较"}

    def lufs(m: Dict[str, Any]) -> Optional[float]:
        try:
            return media.loudness(work / m["audio"], gain=m["gain"])["lufs"]
        except Exception:  # noqa: BLE001
            return None

    final_l = media.loudness(final)["lufs"]
    voice_l = _energy_mean([lufs(m) for m in voice])
    by_role: Dict[str, Optional[float]] = {}
    for role in ("music", "sfx", "other"):
        tracks = [m for m in others if m["role"] == role]
        if tracks:
            by_role[role] = _energy_mean([lufs(m) for m in tracks])
    if final_l is None or voice_l is None or not any(v is not None for v in by_role.values()):
        return {"applicable": True, "value": None, "reason": "响度测不出来"}
    d_voice = abs(final_l - voice_l)
    d_other = min(abs(final_l - v) for v in by_role.values() if v is not None)
    return {"applicable": True, "value": 1.0 if d_voice < d_other else 0.0, "lufs_final": final_l,
            "lufs_voice": voice_l, "lufs_other": by_role}
