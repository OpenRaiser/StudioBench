# -*- coding: utf-8 -*-
"""占位实现：只在显式设置 STUDIO_BACKEND=mock 时使用，用来在没有 GPU 的机器上验证 harness。

产物规格正确（时长、画幅、音轨），内容是纯色底 + prompt 文字，所以 mock 跑出来的
run 在 result.json 里一律标记为 valid_for_bench=false。
"""
from __future__ import annotations

import os
import random
import zlib
from pathlib import Path
from typing import Any, Dict, Optional

from .. import media

NAME = "mock"
_COLORS = ["0x1b2a41", "0x2d1b3d", "0x1b3d2a", "0x3d2b1b", "0x2b1b3d", "0x1b3d3d"]
_SIZES = {"16:9": (1344, 768), "9:16": (768, 1344), "4:3": (1024, 768),
          "3:4": (768, 1024), "1:1": (768, 768), "21:9": (1792, 768)}


def _fail(tool: str) -> None:
    """按 STUDIO_MOCK_FAIL_RATE 注入失败，用来采"工具报错→恢复"的轨迹。"""
    try:
        rate = float(os.environ.get("STUDIO_MOCK_FAIL_RATE", "0") or 0)
    except ValueError:
        rate = 0.0
    if rate > 0 and random.random() < rate:
        kind = random.choice(["500", "timeout", "empty"])
        if kind == "timeout":
            raise TimeoutError("mock 注入故障：%s 后端超时" % tool)
        raise RuntimeError("mock 注入故障：%s 后端返回 %s" % (tool, kind))


def _color(text: str) -> str:
    return _COLORS[zlib.crc32(text.encode("utf-8")) % len(_COLORS)]   # 进程间稳定，hash() 不稳定


def _size(aspect: Optional[str]) -> tuple:
    return _SIZES.get(aspect or "16:9", _SIZES["16:9"])


def clip(prompt: str, seconds: float, out: Path, aspect: Optional[str] = None,
         tag: str = "", size: Optional[tuple] = None) -> Dict[str, Any]:
    _fail("clip")
    w, h = size or _size(aspect)
    label = ("%s %s" % (tag, prompt)).strip().replace("\n", " ")[:90]
    # 用 lavfi 的 testsrc2 做一块会动的角标，抽帧能看出时间在走；文字用 subtitles 以免依赖 drawtext
    srt = out.with_suffix(".mock.srt")
    srt.write_text("1\n00:00:00,000 --> 00:00:%02d,000\n%s\n" % (max(1, int(seconds) + 1), label),
                   encoding="utf-8")
    vf = ("[0:v][1:v]overlay=W-w-24:24[v0];[v0]subtitles=%s:force_style='FontSize=14'[v]"
          % srt.name)
    media.run(["-y", "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:r=24:d=%s" % (_color(prompt + tag), w, h, seconds),
               "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=%s" % seconds,
               "-f", "lavfi", "-i", "sine=frequency=220:sample_rate=48000:duration=%s" % seconds,
               "-filter_complex", vf, "-map", "[v]", "-map", "2:a",
               "-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-ar", "48000", "-ac", "2", "-t", str(seconds), out.name],
              cwd=out.parent)
    srt.unlink(missing_ok=True)
    return {"backend": NAME}


def image(prompt: str, out: Path, aspect: Optional[str] = None,
          size: Optional[tuple] = None) -> Dict[str, Any]:
    _fail("image")
    w, h = size or _size(aspect)
    media.run(["-y", "-f", "lavfi", "-i", "color=c=%s:s=%dx%d:d=1" % (_color(prompt), w, h),
               "-frames:v", "1", str(out)])
    return {"backend": NAME, "width": w, "height": h}


def audio(kind: str, seconds: float, out: Path) -> Dict[str, Any]:
    _fail("audio")
    if kind == "voice":
        expr = "sin(2*PI*330*t)*0.3*(0.6+0.4*sin(2*PI*3*t))"
    elif kind == "music":
        expr = "(sin(2*PI*220*t)+0.5*sin(2*PI*330*t))/2*0.25"
    else:
        expr = "(random(0)-0.5)*0.15"
    media.run(["-y", "-f", "lavfi", "-i", "aevalsrc=%s:s=48000:d=%s" % (expr, seconds),
               "-c:a", "pcm_s16le", str(out)])
    return {"backend": NAME}


def copy(src: Path, out: Path) -> Dict[str, Any]:
    out.write_bytes(Path(src).read_bytes())
    return {"backend": NAME}


def asr() -> Dict[str, Any]:
    _fail("asr")
    return {"backend": NAME, "text": "[mock asr] 占位转写", "chunks": []}


def search(query: str, max_results: int) -> Dict[str, Any]:
    _fail("search")
    return {"backend": NAME, "results": [{"title": "[mock] %s" % query, "url": "about:blank",
                                          "snippet": "mock 模式没有联网检索"}][:max_results]}
