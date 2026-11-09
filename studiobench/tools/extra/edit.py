# -*- coding: utf-8 -*-
"""v2 新增的剪辑 / 音频 / 分析工具（ffmpeg 为主，beat_detect 用工具箱 Python 里的 librosa）。

v1 的 7 个 ffmpeg 工具只能裁、接、烧字幕、单轨混音。这里补上成片常用、v1 做不到的操作：
图层合成、转场、改画幅、图片推拉（Ken Burns）、变速倒放、多轨混音（带闪避）、音频处理、
镜头切分、节拍检测、调色、按 URL 下载素材。所有路径都限制在工作目录内（registry 的路径助手）。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from .. import concurrency, media
from ._base import TOOLBOX, R

_VENC = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p"]
_AENC = ["-c:a", "aac", "-b:a", "192k", "-ar", str(media.STD_RATE), "-ac", "2"]
_WAV = ["-c:a", "pcm_s16le", "-ar", str(media.STD_RATE), "-ac", "2"]
_FAST = ["-movflags", "+faststart"]


def _even(n: float) -> int:
    return max(2, int(round(float(n))) // 2 * 2)


def _ffmpeg_to(out: Path, args: List[str], timeout: float = 3600) -> None:
    """ffmpeg 写临时文件再替换，失败不留半截文件。"""
    part = R()._part(out)
    try:
        media.run(["-y", *args, str(part)], timeout=timeout)
        R()._commit(part, out)
    finally:
        if part.exists():
            part.unlink()


def _video_info(path: Path) -> Dict[str, Any]:
    info = media.probe(path)
    if not info["has_video"]:
        raise ValueError("%s 没有视频轨" % path.name)
    return info


def _result(out: Path, **extra: Any) -> Dict[str, Any]:
    if media.is_image(out):
        w, h = R()._image_size(out)
        res = {"file": R()._rel(out), "width": w, "height": h}
        res.update(extra)
        return res
    if media.is_audio(out):
        res = {"file": R()._rel(out), "duration_s": media.duration(out)}
        res.update(extra)
        return res
    return R()._clip_info(out, extra)


# --------------------------------------------------------------------------- composite
_POSITIONS = ("full", "center", "top-left", "top-right", "bottom-left", "bottom-right", "top", "bottom")


def _xy(position: str, margin: int) -> tuple:
    m = int(margin)
    table = {"full": ("0", "0"), "center": ("(W-w)/2", "(H-h)/2"),
             "top-left": (str(m), str(m)), "top-right": ("W-w-%d" % m, str(m)),
             "bottom-left": (str(m), "H-h-%d" % m), "bottom-right": ("W-w-%d" % m, "H-h-%d" % m),
             "top": ("(W-w)/2", str(m)), "bottom": ("(W-w)/2", "H-h-%d" % m)}
    return table[position]


def composite(video: str, layer: str, out: Optional[str] = None, start: float = 0,
              end: Optional[float] = None, position: str = "full", x: Optional[float] = None,
              y: Optional[float] = None, width: Optional[int] = None, opacity: float = 1.0,
              margin: int = 40, chroma_key: Optional[str] = None, similarity: float = 0.3,
              blend: float = 0.1) -> Dict[str, Any]:
    base, lay = R()._in(video), R()._in(layer)
    info = _video_info(base)
    W, H, D = info["width"], info["height"], float(info["duration_s"] or 0)
    st = float(start or 0)
    en = float(end) if end is not None else D
    if not 0 <= st < en:
        raise ValueError("要求 0 ≤ start < end")
    if position not in _POSITIONS:
        raise ValueError("position 只支持 %s，或者直接给 x / y（像素）" % "、".join(_POSITIONS))
    if not 0 < float(opacity) <= 1:
        raise ValueError("opacity 要在 (0, 1] 之间")
    path = R()._out(out, "composite", ".mp4")
    args: List[str] = ["-i", str(base)]
    if media.is_image(lay):
        args += ["-loop", "1", "-i", str(lay)]
    else:
        if lay.suffix.lower() == ".webm":            # 带 alpha 的 VP9 只有 libvpx 解码器保留透明通道
            args += ["-c:v", "libvpx-vp9"]
        args += ["-i", str(lay)]
    chain = "[1:v]format=rgba"
    if chroma_key:                                   # 抠绿幕/蓝幕：把这个颜色变透明
        key = str(chroma_key).strip().lstrip("#")
        if not re.fullmatch(r"[0-9a-fA-F]{6}", key):
            raise ValueError("chroma_key 要写成 #00ff00 这样的颜色")
        chain += ",colorkey=0x%s:%.3f:%.3f" % (key, float(similarity), float(blend))
    if not media.is_image(lay):
        chain += ",setpts=PTS-STARTPTS+%.3f/TB" % st          # 图层从 start 开始播放
    if position == "full" and x is None and y is None and not width:
        chain += ",scale=%d:%d" % (W, H)
    elif width:
        chain += ",scale=%d:-2" % _even(width)
    if float(opacity) < 1:
        chain += ",colorchannelmixer=aa=%.3f" % float(opacity)
    px, py = _xy(position, margin)
    if x is not None:
        px = "%d" % int(x)
    if y is not None:
        py = "%d" % int(y)
    graph = ("%s[ov];[0:v][ov]overlay=x=%s:y=%s:enable='between(t,%.3f,%.3f)':eof_action=pass,"
             "format=yuv420p[v]" % (chain, px, py, st, en))
    args += ["-filter_complex", graph, "-map", "[v]", "-map", "0:a?", "-t", "%.3f" % D,
             *_VENC, "-c:a", "copy", *_FAST]
    _ffmpeg_to(path, args)
    return _result(path, backend="ffmpeg", start=st, end=en)


# --------------------------------------------------------------------------- transition
TRANSITIONS = ("fade", "fadeblack", "fadewhite", "dissolve", "wipeleft", "wiperight", "wipeup", "wipedown",
               "slideleft", "slideright", "slideup", "slidedown", "smoothleft", "smoothright", "smoothup",
               "smoothdown", "circleopen", "circleclose", "circlecrop", "rectcrop", "radial", "pixelize",
               "zoomin", "distance", "hblur", "diagtl", "diagbr", "hlslice", "vuslice", "squeezeh", "squeezev")


def transition(videos: List[str], kind: Any = "fade", duration: float = 0.5, out: Optional[str] = None,
               width: Optional[int] = None, height: Optional[int] = None,
               fps: Optional[int] = None) -> Dict[str, Any]:
    srcs = [R()._in(v) for v in videos or []]
    if len(srcs) < 2:
        raise ValueError("至少要 2 段")
    kinds = list(kind) if isinstance(kind, list) else [kind] * (len(srcs) - 1)
    if len(kinds) != len(srcs) - 1:
        raise ValueError("kind 给列表时要有 %d 个（每个接缝一个）" % (len(srcs) - 1))
    for k in kinds:
        if k not in TRANSITIONS:
            raise ValueError("不认识的转场：%s。可选：%s" % (k, "、".join(TRANSITIONS)))
    d = float(duration)
    first = _video_info(srcs[0])
    W, H = _even(width or first["width"]), _even(height or first["height"])
    rate = int(fps or round(first["fps"] or media.STD_FPS))
    tmp = Path(tempfile.mkdtemp(prefix="sb_xfade_"))
    try:
        norm = []
        for i, s in enumerate(srcs):
            n = tmp / ("%02d.mp4" % i)
            media.normalize_clip(s, n, W, H, fps=rate)
            norm.append(n)
        lens = [media.duration(n) for n in norm]
        if not 0 < d < min(lens):
            raise ValueError("转场时长要大于 0、小于最短的一段（%.2fs）" % min(lens))
        args: List[str] = []
        for n in norm:
            args += ["-i", str(n)]
        parts, vlast, alast, offset = [], "[0:v]", "[0:a]", 0.0
        for i in range(1, len(norm)):
            offset += lens[i - 1] - d
            vout, aout = "[v%d]" % i, "[a%d]" % i
            parts.append("%s[%d:v]xfade=transition=%s:duration=%.3f:offset=%.3f%s"
                         % (vlast, i, kinds[i - 1], d, offset, vout))
            parts.append("%s[%d:a]acrossfade=d=%.3f%s" % (alast, i, d, aout))
            vlast, alast = vout, aout
        path = R()._out(out, "transition", ".mp4")
        args += ["-filter_complex", ";".join(parts), "-map", vlast, "-map", alast, *_VENC, *_AENC, *_FAST]
        _ffmpeg_to(path, args)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return _result(path, backend="ffmpeg", transitions=kinds, transition_s=d)


# --------------------------------------------------------------------------- reframe
_ANCHOR = {"center": ("(iw-ow)/2", "(ih-oh)/2"), "left": ("0", "(ih-oh)/2"), "right": ("iw-ow", "(ih-oh)/2"),
           "top": ("(iw-ow)/2", "0"), "bottom": ("(iw-ow)/2", "ih-oh")}


def reframe(video: str, width: int, height: int, mode: str = "crop", anchor: str = "center",
            fps: Optional[float] = None, out: Optional[str] = None) -> Dict[str, Any]:
    src = R()._in(video)
    W, H = _even(width), _even(height)
    if anchor not in _ANCHOR:
        raise ValueError("anchor 只支持 %s" % "、".join(_ANCHOR))
    cx, cy = _ANCHOR[anchor]
    if mode == "crop":
        vf = "scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d:%s:%s" % (W, H, W, H, cx, cy)
    elif mode == "pad":
        vf = "scale=%d:%d:force_original_aspect_ratio=decrease,pad=%d:%d:(ow-iw)/2:(oh-ih)/2:color=black" % (
            W, H, W, H)
    elif mode == "stretch":
        vf = "scale=%d:%d" % (W, H)
    elif mode == "blur":
        vf = None
    else:
        raise ValueError("mode 只支持 crop（裁满）、pad（加黑边）、blur（模糊背景铺满）、stretch（拉伸）")
    image = media.is_image(src)
    ext = src.suffix.lower() if image else ".mp4"
    path = R()._out(out, "reframe", ext)
    if mode == "blur":
        graph = ("[0:v]split[a][b];[a]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
                 "boxblur=24:4[bg];[b]scale=%d:%d:force_original_aspect_ratio=decrease[fg];"
                 "[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1%s[v]"
                 % (W, H, W, H, W, H, "" if image else ",format=yuv420p" + (",fps=%g" % float(fps) if fps else "")))
        args = ["-i", str(src), "-filter_complex", graph, "-map", "[v]"]
    else:
        vf += ",setsar=1"
        if not image:
            vf += (",fps=%g" % float(fps) if fps else "") + ",format=yuv420p"
        args = ["-i", str(src), "-vf", vf]
        if not image:
            args += ["-map", "0:v:0"]
    if image:
        args += ["-frames:v", "1"]
    else:
        args += ["-map", "0:a?", *_VENC, "-c:a", "copy", *_FAST]
    _ffmpeg_to(path, args)
    return _result(path, backend="ffmpeg", mode=mode)


# --------------------------------------------------------------------------- ken_burns
_PANS = {"center": ((0.5, 0.5), (0.5, 0.5)), "left-to-right": ((0.0, 0.5), (1.0, 0.5)),
         "right-to-left": ((1.0, 0.5), (0.0, 0.5)), "top-to-bottom": ((0.5, 0.0), (0.5, 1.0)),
         "bottom-to-top": ((0.5, 1.0), (0.5, 0.0))}


def ken_burns(image: str, duration: float, out: Optional[str] = None, zoom_from: float = 1.0,
              zoom_to: float = 1.15, pan: str = "center", width: int = 1920, height: int = 1080,
              fps: int = 24) -> Dict[str, Any]:
    src = R()._in(image)
    if not media.is_image(src):
        raise ValueError("image 必须是图片")
    dur = float(duration)
    if not 0.5 <= dur <= 60:
        raise ValueError("duration 要在 0.5–60 秒之间")
    if pan not in _PANS:
        raise ValueError("pan 只支持 %s" % "、".join(_PANS))
    zf, zt = float(zoom_from), float(zoom_to)
    if not (1.0 <= zf <= 4 and 1.0 <= zt <= 4):
        raise ValueError("zoom_from / zoom_to 要在 1–4 之间（1 = 原图全幅）")
    W, H, rate = _even(width), _even(height), int(fps)
    frames = max(2, int(round(dur * rate)))
    (x0, y0), (x1, y1) = _PANS[pan]
    up = 3                                            # 先放大再推拉：zoompan 按整像素取景，原尺寸推拉会抖
    p = "((1-cos(PI*on/%d))/2)" % (frames - 1)          # 缓入缓出
    z = "(%f+(%f)*%s)" % (zf, zt - zf, p)
    cx = "(%f+(%f)*%s)" % (x0, x1 - x0, p)
    cy = "(%f+(%f)*%s)" % (y0, y1 - y0, p)
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,"
          "zoompan=z='%s':x='(iw-iw/zoom)*%s':y='(ih-ih/zoom)*%s':d=%d:s=%dx%d:fps=%d,format=yuv420p"
          % (W * up, H * up, W * up, H * up, z, cx, cy, frames, W, H, rate))
    path = R()._out(out, "ken_burns", ".mp4")
    _ffmpeg_to(path, ["-i", str(src), "-vf", vf, "-frames:v", str(frames), *_VENC, *_FAST])
    return _result(path, backend="ffmpeg", pan=pan, zoom=[zf, zt])


# --------------------------------------------------------------------------- speed
def _atempo(factor: float) -> str:
    steps, f = [], float(factor)
    while f > 2.0:
        steps.append(2.0)
        f /= 2.0
    while f < 0.5:
        steps.append(0.5)
        f /= 0.5
    steps.append(f)
    return ",".join("atempo=%.6f" % s for s in steps)


def speed(video: str, factor: float = 1.0, reverse: bool = False, out: Optional[str] = None) -> Dict[str, Any]:
    src = R()._in(video)
    info = _video_info(src)
    f = float(factor)
    if not 0.1 <= f <= 10:
        raise ValueError("factor 要在 0.1–10 之间（2 = 两倍速，0.5 = 慢放一半）")
    if reverse and float(info["duration_s"] or 0) > 60:
        raise ValueError("倒放只支持 60 秒以内的片段，先 trim")
    vf = ("reverse," if reverse else "") + "setpts=PTS/%.6f" % f
    path = R()._out(out, "speed", ".mp4")
    args = ["-i", str(src), "-filter:v", vf]
    if info["has_audio"]:
        args += ["-filter:a", ("areverse," if reverse else "") + _atempo(f), *_AENC]
    else:
        args += ["-an"]
    _ffmpeg_to(path, args + _VENC + _FAST)
    return _result(path, backend="ffmpeg", factor=f, reverse=bool(reverse))


# --------------------------------------------------------------------------- 音频
def _audio_chain(trim_start: Optional[float], trim_end: Optional[float], fade_in: float, fade_out: float,
                 gain_db: float, length: Optional[float]) -> List[str]:
    chain = []
    if trim_start is not None or trim_end is not None:
        a = "atrim=start=%.3f" % float(trim_start or 0)
        if trim_end is not None:
            a += ":end=%.3f" % float(trim_end)
        chain += [a, "asetpts=PTS-STARTPTS"]
    if fade_in:
        chain.append("afade=t=in:st=0:d=%.3f" % float(fade_in))
    if fade_out:
        if length is None:
            raise ValueError("fade_out 需要知道这段的时长")
        chain.append("afade=t=out:st=%.3f:d=%.3f" % (max(0.0, length - float(fade_out)), float(fade_out)))
    if gain_db:
        chain.append("volume=%.2fdB" % float(gain_db))
    return chain


def _seg_len(src: Path, trim_start: Optional[float], trim_end: Optional[float]) -> float:
    total = media.duration(src)
    a = float(trim_start or 0)
    b = min(total, float(trim_end)) if trim_end is not None else total
    if not 0 <= a < b:
        raise ValueError("%s：要求 0 ≤ trim_start < trim_end ≤ 时长（%.2fs）" % (src.name, total))
    return b - a


def _check_lufs(lufs: Optional[float]) -> None:
    if lufs is not None and not -70 <= float(lufs) <= -5:
        raise ValueError("normalize_lufs 是目标响度（LUFS），要在 -70 到 -5 之间，常用 -14（网络平台）到 -23（广播）")


def audio_mix(tracks: List[Dict[str, Any]], out: Optional[str] = None, duration: Optional[float] = None,
              duck: Optional[int] = None, duck_db: float = 12.0,
              normalize_lufs: Optional[float] = None) -> Dict[str, Any]:
    if not tracks:
        raise ValueError("tracks 不能为空")
    if duck is not None and not 0 <= int(duck) < len(tracks):
        raise ValueError("duck 是 tracks 里人声那一条的下标（0 起）")
    _check_lufs(normalize_lufs)
    args: List[str] = []
    parts: List[str] = []
    for i, t in enumerate(tracks):
        src = R()._in(str(t.get("file") or ""))
        if not media.probe(src)["has_audio"]:
            raise ValueError("%s 没有音轨" % src.name)
        args += ["-i", str(src)]
        length = _seg_len(src, t.get("trim_start"), t.get("trim_end"))
        chain = ["aformat=sample_rates=%d:channel_layouts=stereo" % media.STD_RATE]
        chain += _audio_chain(t.get("trim_start"), t.get("trim_end"), float(t.get("fade_in") or 0),
                              float(t.get("fade_out") or 0), float(t.get("gain_db") or 0), length)
        start_ms = int(round(float(t.get("start") or 0) * 1000))
        if start_ms:
            chain.append("adelay=delays=%d:all=1" % start_ms)
        parts.append("[%d:a]%s[t%d]" % (i, ",".join(chain), i))
    labels = ["[t%d]" % i for i in range(len(tracks))]
    others = [i for i in range(len(tracks)) if duck is not None and i != int(duck)]
    if duck is not None and others:          # 只有人声一条轨时没东西可压，duck 不起作用
        k = int(duck)
        parts.append("[t%d]asplit=2[key][keymix]" % k)
        if others:
            # 闪避：人声一出来，其它轨按 duck_db 压下去（sidechaincompress 由人声触发）
            ratio = max(2.0, min(20.0, 10 ** (float(duck_db) / 20.0)))
            if len(others) > 1:
                parts.append("%samix=inputs=%d:normalize=0:duration=longest[bed]"
                             % ("".join("[t%d]" % i for i in others), len(others)))
            else:
                parts.append("[t%d]anull[bed]" % others[0])
            parts.append("[bed][key]sidechaincompress=threshold=0.02:ratio=%.2f:attack=15:release=350[ducked]"
                         % ratio)
            labels = ["[ducked]", "[keymix]"]
    if len(labels) > 1:
        parts.append("%samix=inputs=%d:normalize=0:duration=longest[mix]" % ("".join(labels), len(labels)))
    else:
        parts.append("%sanull[mix]" % labels[0])
    tail = "[mix]"
    post = []
    if normalize_lufs is not None:
        post.append("loudnorm=I=%.1f:TP=-1.5:LRA=11" % float(normalize_lufs))
    if duration:
        post += ["apad", "atrim=0:%.3f" % float(duration)]
    if post:
        parts.append("%s%s[outa]" % (tail, ",".join(post)))
        tail = "[outa]"
    path = R()._out(out, "audio_mix", ".wav")
    codec = _WAV if path.suffix.lower() == ".wav" else _AENC
    _ffmpeg_to(path, args + ["-filter_complex", ";".join(parts), "-map", tail, *codec])
    return _result(path, backend="ffmpeg", tracks=len(tracks), loudness=media.loudness(path))


def audio_edit(audio: str, out: Optional[str] = None, start: Optional[float] = None, end: Optional[float] = None,
               fade_in: float = 0, fade_out: float = 0, gain_db: float = 0,
               normalize_lufs: Optional[float] = None, speed: Optional[float] = None,
               pad_to: Optional[float] = None, denoise: bool = False) -> Dict[str, Any]:
    src = R()._in(audio)
    if not media.probe(src)["has_audio"]:
        raise ValueError("%s 没有音轨" % src.name)
    _check_lufs(normalize_lufs)
    length = _seg_len(src, start, end)
    if speed:
        length /= float(speed)
    chain = _audio_chain(start, end, 0, 0, gain_db, length)
    if denoise:
        chain += ["highpass=f=70", "afftdn=nf=-25"]
    if speed:
        chain.append(_atempo(float(speed)))
    if fade_in:
        chain.append("afade=t=in:st=0:d=%.3f" % float(fade_in))
    if fade_out:
        chain.append("afade=t=out:st=%.3f:d=%.3f" % (max(0.0, length - float(fade_out)), float(fade_out)))
    if normalize_lufs is not None:
        chain.append("loudnorm=I=%.1f:TP=-1.5:LRA=11" % float(normalize_lufs))
    if pad_to:
        chain += ["apad", "atrim=0:%.3f" % float(pad_to)]
    path = R()._out(out, "audio_edit", ".wav")
    codec = _WAV if path.suffix.lower() == ".wav" else _AENC
    args = ["-i", str(src), "-vn"]
    if chain:
        args += ["-af", ",".join(chain)]
    _ffmpeg_to(path, args + codec)
    return _result(path, backend="ffmpeg", loudness=media.loudness(path))


# --------------------------------------------------------------------------- 分析
_PTS = re.compile(r"pts_time:([\d.]+)")


def scene_detect(video: str, threshold: float = 0.3, out_dir: Optional[str] = None) -> Dict[str, Any]:
    src = R()._in(video)
    info = _video_info(src)
    total = float(info["duration_s"] or 0)
    th = float(threshold)
    if not 0.05 <= th <= 0.9:
        raise ValueError("threshold 要在 0.05–0.9 之间（越小切得越碎）")
    log = media.run(["-i", str(src), "-vf", "select='gt(scene,%.3f)',showinfo" % th, "-an", "-f", "null", "-"],
                    timeout=1800)
    cuts = sorted({round(float(m.group(1)), 3) for m in _PTS.finditer(log)})
    cuts = [c for c in cuts if 0.2 < c < total - 0.2]
    bounds = [0.0] + cuts + [total]
    shots = [{"start": round(a, 3), "end": round(b, 3), "duration_s": round(b - a, 3)}
             for a, b in zip(bounds, bounds[1:])]
    target = R()._resolve(out_dir) if out_dir else R().WORKDIR / "frames" / (src.stem + "_shots")
    with concurrency.keyed("dir:" + str(target)):          # 同一个切镜目录同一时刻只让一个调用写
        return _scene_sheet(src, target, shots, cuts, th, total)


def _scene_sheet(src: Path, target: Path, shots: List[Dict[str, Any]], cuts: List[float], th: float,
                 total: float) -> Dict[str, Any]:
    target.mkdir(parents=True, exist_ok=True)
    shown = shots if len(shots) <= 16 else [shots[int(i * len(shots) / 16.0)] for i in range(16)]
    images, labels = [], []
    for i, s in enumerate(shown):
        t = min(s["start"] + min(0.3, s["duration_s"] / 2), max(0.0, total - 0.05))
        img = target / ("shot_%03d.jpg" % i)
        media.run(["-y", "-ss", "%.3f" % t, "-i", str(src), "-frames:v", "1", "-q:v", "3", str(img)])
        images.append(img)
        labels.append("#%d %.1fs" % (shots.index(s) + 1, s["start"]))
    sheet = media.contact_sheet(images, labels, target / "sheet.jpg")
    return {"file": R()._rel(src), "shots": shots, "count": len(shots), "cuts": cuts, "threshold": th,
            "sheet": R()._rel(sheet), "backend": "ffmpeg", "_images": [sheet]}


_BEAT_CODE = r"""
import json, sys
import numpy as np, librosa
path, offset, dur = sys.argv[1], float(sys.argv[2]), (float(sys.argv[3]) if sys.argv[3] != "-" else None)
y, sr = librosa.load(path, sr=22050, mono=True, offset=offset, duration=dur)
env = librosa.onset.onset_strength(y=y, sr=sr)
tempo, beats = librosa.beat.beat_track(onset_envelope=env, sr=sr, units="time")
onsets = librosa.onset.onset_detect(onset_envelope=env, sr=sr, units="frames")
strength = env[onsets] if len(onsets) else np.array([])
top = onsets[np.argsort(-strength)[:24]] if len(onsets) else []
times = librosa.frames_to_time(np.sort(top), sr=sr) if len(top) else []
print(json.dumps({"bpm": round(float(np.atleast_1d(tempo)[0]), 2),
                  "beats": [round(float(b) + offset, 3) for b in beats],
                  "strong_onsets": [round(float(t) + offset, 3) for t in times],
                  "analyzed_s": round(len(y) / float(sr), 3)}))
"""


def _toolbox_python() -> Path:
    """工具箱 Python：共享盘上是单个压缩包，sb-toolbox 第一次用时解到本机缓存。"""
    tool = TOOLBOX / "bin" / "sb-toolbox"
    if tool.is_file():
        proc = subprocess.run([str(tool), "ensure", "py"], capture_output=True, text=True, timeout=900)
        lines = proc.stdout.strip().splitlines()
        if proc.returncode == 0 and lines:
            return Path(lines[-1]) / "bin" / "python3"
    return TOOLBOX / "py" / "bin" / "python3"


_BEAT_OK: Optional[tuple] = None


def beat_check() -> tuple:
    global _BEAT_OK
    if _BEAT_OK is None:
        if not (TOOLBOX / "py-3.11-env.tar.gz").is_file() and not (TOOLBOX / "py" / "bin").is_dir():
            _BEAT_OK = (False, "工具箱 Python 包不在：%s" % (TOOLBOX / "py-3.11-env.tar.gz"))
            return _BEAT_OK
        py = _toolbox_python()
        if not py.exists():
            _BEAT_OK = (False, "工具箱 Python 不存在：%s" % py)
        else:
            proc = subprocess.run([str(py), "-c", "import librosa"], capture_output=True, text=True, timeout=300)
            _BEAT_OK = (proc.returncode == 0, str(py) if proc.returncode == 0 else
                        "librosa 不可用：%s" % proc.stderr.strip()[-200:])
    return _BEAT_OK


def beat_detect(audio: str, start: Optional[float] = None, end: Optional[float] = None) -> Dict[str, Any]:
    src = R()._in(audio)
    ok, detail = beat_check()
    if not ok:
        raise RuntimeError(detail)
    tmp = Path(tempfile.mkdtemp(prefix="sb_beat_"))
    try:
        wav = media.extract_audio(src, tmp / "a.wav", rate=22050, mono=True)
        offset = float(start or 0)
        dur = "%.3f" % (float(end) - offset) if end is not None else "-"
        proc = subprocess.run([str(_toolbox_python()), "-c", _BEAT_CODE, str(wav), "%.3f" % offset, dur],
                              capture_output=True, text=True, timeout=900)
        if proc.returncode != 0:
            raise RuntimeError("节拍分析失败：%s" % proc.stderr.strip()[-400:])
        res = json.loads(proc.stdout.strip().splitlines()[-1])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    beats = res["beats"]
    res.update({"file": R()._rel(src), "beat_count": len(beats), "backend": "librosa"})
    if len(beats) > 400:                                  # 太长的列表只返回前 400 个，免得撑爆上下文
        res["beats"], res["beats_truncated"] = beats[:400], True
    return res


# --------------------------------------------------------------------------- 抠图
_RMBG_CODE = r"""
import sys
from PIL import Image
from rembg import new_session, remove
src, out, view, model = sys.argv[1:5]
cut = remove(Image.open(src).convert("RGBA"), session=new_session(model))
cut.save(out)
bg = Image.new("RGBA", cut.size, (128, 128, 128, 255))
bg.alpha_composite(cut)
bg.convert("RGB").save(view, quality=88)
print(cut.size[0], cut.size[1])
"""
RMBG_MODELS = ("isnet-general-use", "u2net")


def rmbg_check() -> tuple:
    ok, detail = beat_check()
    if not ok:
        return ok, detail
    missing = [m for m in RMBG_MODELS if not (TOOLBOX / "models" / "rembg" / (m + ".onnx")).is_file()]
    return (not missing, "模型齐全" if not missing else "缺模型：%s" % ", ".join(missing))


def remove_bg(image: str, out: Optional[str] = None, model: str = "isnet-general-use") -> Dict[str, Any]:
    src = R()._in(image)
    if not media.is_image(src):
        raise ValueError("image 必须是图片；视频先用 sample_frames 取帧")
    if model not in RMBG_MODELS:
        raise ValueError("model 只支持 %s" % "、".join(RMBG_MODELS))
    ok, detail = rmbg_check()
    if not ok:
        raise RuntimeError(detail)
    path = R()._out(out, "remove_bg", ".png")
    if path.suffix.lower() != ".png":
        raise ValueError("输出要用 .png（带透明通道）")
    view = R().WORKDIR / "frames" / (path.stem + "_view.jpg")
    view.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, U2NET_HOME=str(TOOLBOX / "models" / "rembg"), OMP_NUM_THREADS="8")
    part = R()._part(path)
    proc = subprocess.run([str(_toolbox_python()), "-c", _RMBG_CODE, str(src), str(part), str(view), model],
                          capture_output=True, text=True, timeout=900, env=env)
    if proc.returncode != 0:
        if part.exists():
            part.unlink()
        raise RuntimeError("抠图失败：%s" % proc.stderr.strip()[-400:])
    R()._commit(part, path)
    w, h = (int(v) for v in proc.stdout.split()[-2:])
    return {"file": R()._rel(path), "width": w, "height": h, "model": model, "backend": "rembg",
            "_images": [view]}


# --------------------------------------------------------------------------- 调色
_PRESETS = {
    "warm": "colorbalance=rs=0.08:gs=0.02:bs=-0.08:rm=0.05:bm=-0.05",
    "cool": "colorbalance=rs=-0.08:bs=0.08:rm=-0.04:bm=0.05",
    "teal_orange": "colorbalance=rs=-0.10:gs=-0.02:bs=0.12:rh=0.12:gh=0.02:bh=-0.10,eq=saturation=1.1",
    "bw": "hue=s=0,eq=contrast=1.1",
    "vintage": "curves=preset=vintage,eq=saturation=0.85",
    "film": "curves=preset=medium_contrast,eq=saturation=0.9,noise=alls=6:allf=t",
    "high_contrast": "curves=preset=strong_contrast",
    "faded": "curves=all='0/0.08 1/0.92',eq=saturation=0.8",
}


def color(video: str, preset: Optional[str] = None, brightness: float = 0.0, contrast: float = 1.0,
          saturation: float = 1.0, gamma: float = 1.0, temperature: Optional[int] = None,
          lut: Optional[str] = None, vignette: bool = False, out: Optional[str] = None) -> Dict[str, Any]:
    src = R()._in(video)
    chain: List[str] = []
    if preset:
        if preset not in _PRESETS:
            raise ValueError("preset 只支持 %s" % "、".join(_PRESETS))
        chain.append(_PRESETS[preset])
    if (brightness, contrast, saturation, gamma) != (0.0, 1.0, 1.0, 1.0):
        chain.append("eq=brightness=%.3f:contrast=%.3f:saturation=%.3f:gamma=%.3f"
                     % (float(brightness), float(contrast), float(saturation), float(gamma)))
    if temperature:
        if not 1000 <= int(temperature) <= 40000:
            raise ValueError("temperature 要在 1000–40000 K 之间（6500 为中性，越低越暖）")
        chain.append("colortemperature=temperature=%d" % int(temperature))
    if lut:
        chain.append("lut3d=file='%s'" % str(R()._in(lut)).replace("'", r"\'"))
    if vignette:
        chain.append("vignette=PI/5")
    if not chain:
        raise ValueError("至少给一项：preset / brightness / contrast / saturation / gamma / temperature / lut / vignette")
    image = media.is_image(src)
    path = R()._out(out, "color", src.suffix.lower() if image else ".mp4")
    if image:
        args = ["-i", str(src), "-vf", ",".join(chain), "-frames:v", "1"]
    else:
        args = ["-i", str(src), "-vf", ",".join(chain + ["format=yuv420p"]), "-map", "0:v:0", "-map", "0:a?",
                *_VENC, "-c:a", "copy", *_FAST]
    _ffmpeg_to(path, args)
    res = _result(path, backend="ffmpeg", filters=",".join(chain))
    if image:
        view = media.downscale_image(path, R().WORKDIR / "frames" / (path.stem + "_view.jpg"))
        res["_images"] = [view]
    return res


# --------------------------------------------------------------------------- 下载素材
_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif",
        "image/svg+xml": ".svg", "audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/x-wav": ".wav",
        "audio/ogg": ".ogg", "video/mp4": ".mp4", "video/webm": ".webm", "application/pdf": ".pdf",
        "text/html": ".html", "application/json": ".json", "text/plain": ".txt", "text/csv": ".csv",
        "model/gltf-binary": ".glb", "font/ttf": ".ttf", "font/otf": ".otf", "font/woff2": ".woff2"}


def fetch_check() -> tuple:
    from ..backends import search as sb
    px = sb.proxy()
    return True, ("代理 %s" % px) if px else "未配置代理，按直连下载"


def fetch_url(url: str, out: Optional[str] = None, max_mb: float = 200) -> Dict[str, Any]:
    from ..backends import common, search as sb
    u = str(url or "").strip()
    if not re.match(r"^https?://", u, re.I):
        raise ValueError("只支持 http(s) 地址")
    if common.env("STUDIO_SEARCH_BLOCK_FILE") and sb._blocked({"url": u, "title": "", "snippet": ""}):
        run_dir = common.env("STUDIO_RUN_DIR")
        if run_dir:
            with open(Path(run_dir) / "search_blocked.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": round(time.time(), 1), "fetch": u}, ensure_ascii=False) + "\n")
        raise ValueError("这个地址不能下载")
    req = urllib.request.Request(u, headers={"User-Agent": sb.UA})
    limit = int(float(max_mb) * 1024 * 1024)
    with sb._opener().open(req, timeout=60) as resp:
        ctype = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        ext = Path(urllib.parse.urlparse(resp.geturl()).path).suffix.lower()
        ext = ext if 1 < len(ext) <= 6 else _EXT.get(ctype, ".bin")
        path = R()._out(out, "fetch", ext)
        part = R()._part(path)
        size = 0
        try:
            with open(part, "wb") as fh:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ValueError("文件超过 %g MB，已放弃" % float(max_mb))
                    fh.write(chunk)
            R()._commit(part, path)
        finally:
            if part.exists():
                part.unlink()
    res: Dict[str, Any] = {"file": R()._rel(path), "bytes": size, "content_type": ctype, "url": u,
                           "backend": "fetch"}
    if media.is_image(path):
        try:
            res["_images"] = [media.downscale_image(path, R().WORKDIR / "frames" / (path.stem + "_view.jpg"))]
        except Exception:  # noqa: BLE001  svg 之类 PIL 打不开的图就不给预览
            pass
    elif ctype.startswith("text/html"):
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(
            r"(?is)<(script|style)\b.*?</\1>", " ", path.read_text(encoding="utf-8", errors="replace"))))
        res["text_excerpt"] = text.strip()[:1500]
    return res


# --------------------------------------------------------------------------- 注册
def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_S = {"type": "string"}
_N = {"type": "number"}
_I = {"type": "integer"}
_B = {"type": "boolean"}
_OUT = {"type": "string", "description": "输出文件（相对路径），不给就自动编号"}

TOOLS: Dict[str, Dict[str, Any]] = {
    "composite": {
        "fn": composite, "backend": "ffmpeg",
        "desc": "图层合成：把图片或视频（可带透明通道：png、prores 4444 .mov、VP9 .webm）叠到视频上，"
                "用于片名、角标、字幕条、落版、画中画；chroma_key 给颜色（如 #00ff00）可抠绿幕。position 选预设位置，或直接给 x/y 像素；"
                "width 缩放图层；start/end 控制出现的时间段，图层视频从 start 开始播放。保留底片音轨。",
        "schema": _obj({"video": _S, "layer": _S, "out": _OUT, "start": _N, "end": _N,
                        "position": {"type": "string", "enum": list(_POSITIONS)}, "x": _N, "y": _N,
                        "width": _I, "opacity": _N, "margin": _I, "chroma_key": _S, "similarity": _N,
                        "blend": _N}, ["video", "layer"])},
    "transition": {
        "fn": transition, "backend": "ffmpeg",
        "desc": "带转场地拼接多段（xfade + 音频交叉淡化）。kind 给一个名字或每个接缝一个，"
                "常用 fade/fadeblack/dissolve/wipeleft/slideup/circleopen/zoomin 等；duration 是转场秒数，"
                "总时长 = 各段之和 − 转场数 × duration。分辨率/帧率不同会先统一。",
        "schema": _obj({"videos": {"type": "array", "items": _S},
                        "kind": {"anyOf": [{"type": "string", "enum": list(TRANSITIONS)},
                                           {"type": "array", "items": {"type": "string", "enum": list(TRANSITIONS)}}]},
                        "duration": _N, "out": _OUT, "width": _I, "height": _I, "fps": _I}, ["videos"])},
    "reframe": {
        "fn": reframe, "backend": "ffmpeg",
        "desc": "改画幅/分辨率/帧率：mode=crop 裁满（anchor 选保留哪边）、pad 加黑边、blur 模糊背景铺满（横转竖常用）、"
                "stretch 拉伸。视频和图片都行。",
        "schema": _obj({"video": _S, "width": _I, "height": _I,
                        "mode": {"type": "string", "enum": ["crop", "pad", "blur", "stretch"]},
                        "anchor": {"type": "string", "enum": list(_ANCHOR)}, "fps": _N, "out": _OUT},
                       ["video", "width", "height"])},
    "ken_burns": {
        "fn": ken_burns, "backend": "ffmpeg",
        "desc": "图片推拉（Ken Burns）：静图 → 带缓动推拉摇移的视频（无声）。纪录、讲解里用真实照片/资料图很常用。"
                "zoom_from/zoom_to 是起止缩放（1 为全幅），pan 选摇移方向。",
        "schema": _obj({"image": _S, "duration": _N, "out": _OUT, "zoom_from": _N, "zoom_to": _N,
                        "pan": {"type": "string", "enum": list(_PANS)}, "width": _I, "height": _I, "fps": _I},
                       ["image", "duration"])},
    "speed": {
        "fn": speed, "backend": "ffmpeg",
        "desc": "变速（factor=2 两倍速，0.5 慢放）和倒放（reverse，60 秒以内）。音频同步变速、保持音高。",
        "schema": _obj({"video": _S, "factor": _N, "reverse": _B, "out": _OUT}, ["video"])},
    "audio_mix": {
        "fn": audio_mix, "backend": "ffmpeg",
        "desc": "多轨混音成一条音频：tracks 每项 {file, start（在成片里的起点秒）, gain_db, fade_in, fade_out, "
                "trim_start, trim_end}；duck 给人声那条的下标，其它轨在人声出现时自动压低 duck_db；"
                "normalize_lufs 统一响度（短视频常用 -14）。结果再用 mix_audio(replace=true) 放进视频。",
        "schema": _obj({"tracks": {"type": "array", "items": {"type": "object", "properties": {
                            "file": _S, "start": _N, "gain_db": _N, "fade_in": _N, "fade_out": _N,
                            "trim_start": _N, "trim_end": _N}, "required": ["file"]}},
                        "out": _OUT, "duration": _N, "duck": _I, "duck_db": _N, "normalize_lufs": _N},
                       ["tracks"])},
    "audio_edit": {
        "fn": audio_edit, "backend": "ffmpeg",
        "desc": "单条音频处理：截取 start/end、淡入淡出、增益 gain_db、响度归一 normalize_lufs、变速 speed（保持音高）、"
                "pad_to 补静音到指定时长、denoise 降噪。输入也可以是视频（取其音轨）。",
        "schema": _obj({"audio": _S, "out": _OUT, "start": _N, "end": _N, "fade_in": _N, "fade_out": _N,
                        "gain_db": _N, "normalize_lufs": _N, "speed": _N, "pad_to": _N, "denoise": _B},
                       ["audio"])},
    "scene_detect": {
        "fn": scene_detect, "backend": "ffmpeg",
        "desc": "镜头切分：找出视频里的剪辑点，返回每个镜头的起止时间和一张各镜头首帧的联系表。分析参考片的节奏、镜头数很有用。",
        "schema": _obj({"video": _S, "threshold": _N, "out_dir": _S}, ["video"])},
    "beat_detect": {
        "fn": beat_detect, "backend": "librosa", "check": beat_check,
        "desc": "节拍检测（librosa）：音乐或视频的 BPM、每拍时间点、最强的若干个重音点。卡点剪辑、MV 用。",
        "schema": _obj({"audio": _S, "start": _N, "end": _N}, ["audio"])},
    "remove_bg": {
        "fn": remove_bg, "backend": "rembg", "check": rmbg_check,
        "desc": "抠图：去掉图片背景，输出带透明通道的 png（rembg，默认 isnet-general-use，人像/商品都行）。"
                "返回一张放在灰底上的预览。抠好的图可以用 composite 叠到视频上，或给 render_html / render_blender 当贴图。",
        "schema": _obj({"image": _S, "out": _OUT, "model": {"type": "string", "enum": list(RMBG_MODELS)}},
                       ["image"])},
    "color": {
        "fn": color, "backend": "ffmpeg",
        "desc": "调色：preset（warm/cool/teal_orange/bw/vintage/film/high_contrast/faded），或细调 brightness、"
                "contrast、saturation、gamma、temperature（K，6500 中性），lut 用工作目录里的 .cube，vignette 暗角。"
                "统一多段素材的色调时用同一组参数。视频和图片都行。",
        "schema": _obj({"video": _S, "preset": {"type": "string", "enum": list(_PRESETS)}, "brightness": _N,
                        "contrast": _N, "saturation": _N, "gamma": _N, "temperature": _I, "lut": _S,
                        "vignette": _B, "out": _OUT}, ["video"])},
    "fetch_url": {
        "fn": fetch_url, "backend": "fetch", "check": fetch_check,
        "desc": "按 URL 下载素材到工作目录（图片、音频、视频、网页、数据、字体、glb 模型等，默认上限 200MB）。"
                "图片会直接给你看；网页返回正文摘录。配合 search 找真实资料。",
        "schema": _obj({"url": _S, "out": _OUT, "max_mb": _N}, ["url"])},
}
