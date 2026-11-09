#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ffmpeg / 图像小工具，工具后端和评测共用。

只依赖 ffmpeg 可执行文件和 Pillow，不 import torch。
所有子进程都关掉 stdin：MCP server 用 stdin 收 JSON-RPC，ffmpeg 继承 stdin 会吞掉请求。
"""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence


def _find_ffmpeg() -> str:
    if os.environ.get("STUDIO_FFMPEG"):
        return os.environ["STUDIO_FFMPEG"]
    found = shutil.which("ffmpeg")
    if found:
        return found
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        pass
    raise RuntimeError(
        "找不到 ffmpeg：请安装系统 ffmpeg，或 pip install imageio-ffmpeg，或设置 STUDIO_FFMPEG")


FFMPEG = _find_ffmpeg()
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
AUDIO_SUFFIXES = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

# 片段统一规格：与 H3 输出一致（16:9 为 1344x768，24fps）
STD_FPS = 24
STD_RATE = 48000


class MediaError(RuntimeError):
    pass


def run(args: Sequence[str], cwd: Optional[Path] = None, timeout: Optional[float] = None) -> str:
    """跑一条 ffmpeg 命令，返回 stderr（ffmpeg 的日志都在 stderr）。"""
    cmd = [FFMPEG, "-hide_banner", "-nostdin", *[str(a) for a in args]]
    proc = subprocess.run(cmd, capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          cwd=str(cwd) if cwd else None, timeout=timeout)
    if proc.returncode != 0:
        tail = "\n".join(proc.stderr.strip().splitlines()[-8:])
        raise MediaError("ffmpeg 失败：%s" % tail[-800:])
    return proc.stderr


def is_image(path: Path) -> bool:
    return Path(path).suffix.lower() in IMAGE_SUFFIXES


def is_audio(path: Path) -> bool:
    return Path(path).suffix.lower() in AUDIO_SUFFIXES


# --------------------------------------------------------------------------- probe
_RE_DUR = re.compile(r"Duration: (\d+):(\d+):([\d.]+)")
_RE_SIZE = re.compile(r", (\d{2,5})x(\d{2,5})[ ,\[]")      # 文档口径：避开 0x31637661 这类 tag
_RE_FPS = re.compile(r"([\d.]+) fps")
_RE_RATE = re.compile(r"(\d+) Hz")


def _probe_log(path: Path) -> str:
    """跑 `ffmpeg -i` 拿解析日志。工具池的 worker 在别的 Pod 上写文件，本机共享盘可能还没刷到：
    文件不在、或是刚写的（60 秒内）却解析不了，就隔一会儿再试，最多等约 6 秒。"""
    for attempt in range(5):
        if path.exists():
            log = subprocess.run([FFMPEG, "-hide_banner", "-nostdin", "-i", str(path)],
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL).stderr
            if "Invalid data found" not in log and "Stream #" in log:
                return log
            fresh = time.time() - path.stat().st_mtime < 60
        else:
            log, fresh = "", True
        if attempt == 4 or not fresh:
            break
        time.sleep(1.5)
    if not path.exists():
        raise MediaError("文件不存在：%s" % path.name)
    raise MediaError("无法解析媒体文件：%s" % path.name)


def probe(path: Path) -> Dict[str, Any]:
    """`ffmpeg -i` 解析时长、画幅、帧率、音轨。图片的 duration 为 None。"""
    path = Path(path)
    log = _probe_log(path)
    info: Dict[str, Any] = {"duration_s": None, "width": None, "height": None, "fps": None,
                            "has_video": False, "has_audio": False, "audio_rate": None,
                            "vcodec": None, "bytes": path.stat().st_size}
    dur = _RE_DUR.search(log)
    if dur:
        info["duration_s"] = round(int(dur.group(1)) * 3600 + int(dur.group(2)) * 60
                                   + float(dur.group(3)), 3)
    for line in log.splitlines():
        if "Stream #" not in line:
            continue
        if "Video:" in line and not info["has_video"]:
            if "attached pic" in line:
                continue
            info["has_video"] = True
            codec = re.search(r"Video: (\w+)", line)
            info["vcodec"] = codec.group(1) if codec else None
            size = _RE_SIZE.search(line)
            if size:
                info["width"], info["height"] = int(size.group(1)), int(size.group(2))
            fps = _RE_FPS.search(line)
            if fps:
                info["fps"] = float(fps.group(1))
        elif "Audio:" in line and not info["has_audio"]:
            info["has_audio"] = True
            rate = _RE_RATE.search(line)
            info["audio_rate"] = int(rate.group(1)) if rate else None
    if is_image(path):
        info["duration_s"] = None
    return info


def duration(path: Path) -> float:
    d = probe(path).get("duration_s")
    if not d:
        raise MediaError("读不到时长：%s" % Path(path).name)
    return float(d)


_RE_I = re.compile(r"I:\s+(-?[\d.]+|-inf) LUFS")
_RE_LRA = re.compile(r"LRA:\s+(-?[\d.]+) LU")
_RE_PEAK = re.compile(r"Peak:\s+(-?[\d.]+|-inf) dBFS")


def loudness(path: Path, gain: float = 1.0) -> Dict[str, Optional[float]]:
    """ebur128 的 Integrated LUFS / LRA / true peak。gain≠1 时先乘增益再测（文档 4.1 口径）。"""
    af = "ebur128=peak=true"
    if abs(gain - 1.0) > 1e-9:
        af = "volume=%s,%s" % (gain, af)
    log = run(["-i", str(path), "-vn", "-af", af, "-f", "null", "-"])
    tail = log[log.rfind("Summary:"):] if "Summary:" in log else log

    def _num(regex: "re.Pattern[str]") -> Optional[float]:
        found = regex.search(tail)
        if not found:
            return None
        val = found.group(1)
        return -math.inf if val == "-inf" else float(val)

    return {"lufs": _num(_RE_I), "lra": _num(_RE_LRA), "true_peak_db": _num(_RE_PEAK)}


# --------------------------------------------------------------------------- 抽帧与联系表
def extract_frames(video: Path, out_dir: Path, fps: float = 1.0,
                   start: Optional[float] = None, end: Optional[float] = None) -> List[Dict[str, Any]]:
    """按 fps 抽帧到 out_dir/%02d.png（文档 sample_frames 口径），返回每帧文件与时间点。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.png"):
        old.unlink()
    args: List[str] = ["-y"]
    t0 = float(start or 0.0)
    if start:
        args += ["-ss", "%.3f" % start]
    args += ["-i", str(video)]
    if end is not None:
        args += ["-t", "%.3f" % max(0.05, float(end) - t0)]
    args += ["-vf", "fps=%s" % fps, str(out_dir / "%02d.png")]
    run(args)
    frames = sorted(out_dir.glob("*.png"))
    return [{"file": f, "t": round(t0 + i / float(fps), 2)} for i, f in enumerate(frames)]


def _font(size: int):
    from PIL import ImageFont
    try:
        return ImageFont.load_default(size=size)
    except TypeError:                       # Pillow < 10.1
        return ImageFont.load_default()


def open_image(path: Path, tries: int = 5):
    """读一张图并真正解完（PIL 的 open 是懒的，截断要到 load 时才报）。

    工具池的 worker 在别的 Pod 上写文件，本机共享盘可能还没刷完整：刚写的图读出来会报
    "image file is truncated"。和 _probe_log 一样，刚写的（60 秒内）就隔一会儿再试。
    实测这类失败在 sample_frames 上两天里出现 60 次。不开 LOAD_TRUNCATED_IMAGES——
    那样会把半张图当成功返回，模型看到的是坏帧，比报错更糟。
    """
    from PIL import Image

    path = Path(path)
    for attempt in range(tries):
        try:
            img = Image.open(path)
            img.load()                       # 强制解码：截断在这一步才暴露
            return img
        except (OSError, ValueError) as exc:
            fresh = path.exists() and time.time() - path.stat().st_mtime < 60
            if attempt == tries - 1 or not fresh:
                raise MediaError("读不了图片 %s：%s" % (path.name, exc)) from None
            time.sleep(1.5)
    raise MediaError("读不了图片 %s" % path.name)      # 不会走到，为类型检查保留


def contact_sheet(images: Sequence[Path], labels: Sequence[str], out: Path,
                  max_width: int = 1568, max_cols: int = 4) -> Path:
    """把若干帧拼成一张带时间标签的联系表（JPEG）。帧越少，每格越大。"""
    from PIL import Image, ImageDraw

    if not images:
        raise MediaError("没有可拼接的帧")
    frames = [open_image(p).convert("RGB") for p in images]
    cols = min(max_cols, len(frames)) if len(frames) > 1 else 1
    rows = int(math.ceil(len(frames) / float(cols)))
    cell_w = min(max_width // cols, frames[0].width)
    cell_h = int(round(cell_w * frames[0].height / float(frames[0].width)))
    sheet = Image.new("RGB", (cell_w * cols, cell_h * rows), (16, 16, 16))
    draw = ImageDraw.Draw(sheet)
    font = _font(max(14, cell_w // 18))
    for i, (img, label) in enumerate(zip(frames, labels)):
        x, y = (i % cols) * cell_w, (i // cols) * cell_h
        sheet.paste(img.resize((cell_w, cell_h)), (x, y))
        if label:
            box = draw.textbbox((x + 6, y + 4), label, font=font)
            draw.rectangle((box[0] - 4, box[1] - 2, box[2] + 4, box[3] + 2), fill=(0, 0, 0))
            draw.text((x + 6, y + 4), label, fill=(255, 255, 255), font=font)
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out, "JPEG", quality=85)
    return out


def downscale_image(src: Path, out: Path, max_side: int = 1568) -> Path:
    img = open_image(src).convert("RGB")
    scale = min(1.0, max_side / float(max(img.width, img.height)))
    if scale < 1.0:
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out, "JPEG", quality=88)
    return out


# --------------------------------------------------------------------------- 统一规格与静音
def normalize_clip(src: Path, out: Path, width: int, height: int,
                   fps: int = STD_FPS, rate: int = STD_RATE) -> Path:
    """重编码成指定分辨率/帧率/yuv420p + 48k 立体声 AAC；没有音轨就补静音。"""
    info = probe(src)
    vf = ("scale=%d:%d:force_original_aspect_ratio=decrease,"
          "pad=%d:%d:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=%d,format=yuv420p"
          % (width, height, width, height, fps))
    args: List[str] = ["-y", "-i", str(src)]
    if not info["has_audio"]:
        args += ["-f", "lavfi", "-i", "anullsrc=r=%d:cl=stereo" % rate, "-shortest"]
        amap = ["-map", "0:v:0", "-map", "1:a:0"]
    else:
        amap = ["-map", "0:v:0", "-map", "0:a:0"]
    args += ["-vf", vf, *amap, "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
             "-c:a", "aac", "-b:a", "192k", "-ar", str(rate), "-ac", "2",
             "-movflags", "+faststart", str(out)]
    run(args)
    return out


def make_silence(seconds: float, out: Path, rate: int = STD_RATE) -> Path:
    run(["-y", "-f", "lavfi", "-i", "anullsrc=r=%d:cl=stereo" % rate,
         "-t", "%.3f" % seconds, "-c:a", "pcm_s16le", str(out)])
    return out


def extract_audio(src: Path, out: Path, rate: int = 16000, mono: bool = True) -> Path:
    args = ["-y", "-i", str(src), "-vn", "-ar", str(rate)]
    if mono:
        args += ["-ac", "1"]
    run(args + ["-c:a", "pcm_s16le", str(out)])
    return out
