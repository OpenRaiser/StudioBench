#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""StudioBench 的 18 个原子工具（框架文档 §2 系统提示、§3 工具）。

每个工具：校验参数 → 调后端（backends/）或 ffmpeg → 返回 dict。返回值约定：
· `backend`  实际用的后端（h3-fl2va / qwen-image / cosyvoice3 / mmaudio / ffmpeg / mock …），会写进调用日志
· `file`     产物路径（相对 WORKDIR）
· `_images`  需要作为图片直接给模型看的文件（sample_frames 用），由 MCP server 编码成 image content
所有路径都限制在 WORKDIR 内。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import concurrency, media
from .backends import cosyvoice, h3, mmaudio, mock, qwen3tts, qwen_image, search as search_backend, template, whisper
from .backends import Unavailable, mock_mode, tts_backend
from ..pool import client as pool  # 设了 STUDIO_POOL_URL 时生成调用分给工具池里的实例，没设就是本机后端

WORKDIR = Path(os.environ.get("STUDIO_WORKDIR", ".")).resolve()
SEGMENT_SECONDS = (5, 8)                     # 文档：单段只许 5 秒或 8 秒
ASPECTS = ["16:9", "9:16", "4:3", "3:4", "1:1", "21:9"]
FINAL = "final.mp4"


def set_workdir(path: Path) -> None:
    global WORKDIR
    WORKDIR = Path(path).resolve()


# --------------------------------------------------------------------------- 路径
def _resolve(path: str) -> Path:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("路径不能为空")
    p = (WORKDIR / path).resolve()
    if p != WORKDIR and WORKDIR not in p.parents:
        raise ValueError("路径越界：%s（只能用工作目录里的相对路径）" % path)
    return p


def _in(path: str) -> Path:
    p = _resolve(path)
    if not p.is_file():
        raise FileNotFoundError("找不到文件：%s" % path)
    return p


def _out(path: Optional[str], tool: str, ext: str) -> Path:
    if path:
        p = _resolve(path)
    else:                                    # 不给 out 时自动编号，避免覆盖之前的产物；并发调用各占各的名字
        p = concurrency.reserve_auto(WORKDIR, tool, ext)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _rel(p: Path) -> str:
    return str(Path(p).resolve().relative_to(WORKDIR))


def _part(out: Path) -> Path:
    """ffmpeg 先写临时文件再替换：输入输出同名也安全，失败不留半截文件。登记到本次调用，结束时清掉残留。"""
    p = out.with_name(out.stem + ".part" + out.suffix)
    concurrency.track_part(p)
    return p


def _commit(part: Path, out: Path) -> None:
    part.replace(out)


def _seconds(duration: Any) -> int:
    try:
        d = float(duration)
    except (TypeError, ValueError):
        raise ValueError("duration 必须是 5 或 8") from None
    if int(round(d)) not in SEGMENT_SECONDS or abs(d - round(d)) > 1e-6:
        raise ValueError("单段视频只许 5 秒或 8 秒（收到 %s）；更长的片子用 concat 接" % duration)
    return int(round(d))


def _aspect(aspect: Optional[str]) -> str:
    a = aspect or "16:9"
    if a not in ASPECTS:
        raise ValueError("aspect 只支持 %s" % "、".join(ASPECTS))
    return a


def _image_input(path: str, role: str) -> Path:
    p = _in(path)
    if not media.is_image(p):
        raise ValueError("%s 必须是图片（png/jpg）。要用视频里的某一帧，先 sample_frames 取帧" % role)
    return p


def _clip_info(out: Path, extra: Dict[str, Any]) -> Dict[str, Any]:
    info = media.probe(out)
    res = {"file": _rel(out), "duration_s": info["duration_s"], "width": info["width"],
           "height": info["height"], "has_audio": info["has_audio"]}
    res.update(extra)
    return res


def _image_size(path: Path) -> tuple:
    info = media.probe(path)
    return info["width"], info["height"]


# --------------------------------------------------------------------------- 视频
def t2v(prompt: str, duration: float = 5, aspect: str = "16:9", out: Optional[str] = None,
        seed: Optional[int] = None) -> Dict[str, Any]:
    secs, asp = _seconds(duration), _aspect(aspect)
    path = _out(out, "t2v", ".mp4")
    if mock_mode():
        extra = mock.clip(prompt, secs, path, asp, tag="[t2v]")
    else:
        with pool.h3_base("t2v", h3.fl2va_base) as base:
            extra = h3.generate(base, "t2va", prompt, secs, path, aspect=asp, seed=seed)
        extra["backend"] = "h3-fl2va"
        if pool.current_instance():
            extra["instance"] = pool.current_instance()
    return _clip_info(path, extra)


def i2v(prompt: str, first_frame: str, last_frame: Optional[str] = None, duration: float = 5,
        out: Optional[str] = None, seed: Optional[int] = None) -> Dict[str, Any]:
    secs = _seconds(duration)
    first = _image_input(first_frame, "first_frame")
    last = _image_input(last_frame, "last_frame") if last_frame else None
    path = _out(out, "i2v", ".mp4")
    if mock_mode():
        extra = mock.clip(prompt, secs, path, tag="[i2v]", size=_image_size(first))
    else:
        refs = [first] + ([last] if last else [])
        with pool.h3_base("i2v", h3.fl2va_base) as base:
            extra = h3.generate(base, "fl2va", prompt, secs, path, references=refs,
                                frame_indices=[0, -1] if last else [0], seed=seed)
        extra["backend"] = "h3-fl2va"
        if pool.current_instance():
            extra["instance"] = pool.current_instance()
    return _clip_info(path, extra)


def ref_video(prompt: str, references: List[str], duration: float = 5, aspect: str = "16:9",
              out: Optional[str] = None, seed: Optional[int] = None) -> Dict[str, Any]:
    secs, asp = _seconds(duration), _aspect(aspect)
    if not isinstance(references, list):
        raise ValueError("references 必须是文件路径数组")
    refs = [_in(r) for r in references]
    h3.check_references(refs)
    path = _out(out, "ref", ".mp4")
    if mock_mode():
        extra = mock.clip(prompt, secs, path, asp, tag="[ref]")
    else:
        with pool.h3_base("ref_video", h3.ref2va_base) as base:
            extra = h3.generate(base, "ref2va", prompt, secs, path, references=refs, aspect=asp, seed=seed)
        extra["backend"] = "h3-ref2va"
        if pool.current_instance():
            extra["instance"] = pool.current_instance()
    return _clip_info(path, extra)


# --------------------------------------------------------------------------- 图像
def t2i(prompt: str, aspect: str = "16:9", out: Optional[str] = None, seed: int = 0,
        steps: int = 30) -> Dict[str, Any]:
    asp = _aspect(aspect)
    path = _out(out, "t2i", ".png")
    if mock_mode():
        extra = mock.image(prompt, path, size=qwen_image.SIZES[asp])
    else:
        extra = pool.routed("t2i", qwen_image.t2i, prompt, asp, path, steps=steps, seed=seed)
    return {"file": _rel(path), **extra}


def image_edit(src: str, prompt: str, ref: Optional[str] = None, out: Optional[str] = None,
               seed: int = 0) -> Dict[str, Any]:
    source = _image_input(src, "src")
    reference = _image_input(ref, "ref") if ref else None
    path = _out(out, "edit", ".png")
    if mock_mode():
        extra = mock.copy(source, path)
    else:
        extra = pool.routed("image_edit", qwen_image.image_edit, source, prompt, path, ref=reference, seed=seed)
    return {"file": _rel(path), **extra}


# --------------------------------------------------------------------------- 声音
# Qwen3-TTS 只认语言全名；asr 等工具用的是 zh/en 这类代码，模型自然会照着传，这里统一换成全名
_TTS_LANG = {"zh": "chinese", "zh-cn": "chinese", "zh-hans": "chinese", "cmn": "chinese", "中文": "chinese",
             "en": "english", "en-us": "english", "ja": "japanese", "jp": "japanese", "ko": "korean",
             "fr": "french", "de": "german", "it": "italian", "pt": "portuguese", "ru": "russian", "es": "spanish"}


def _tts_language(language: Optional[str]) -> Optional[str]:
    if not language:
        return language
    return _TTS_LANG.get(language.strip().lower().replace("_", "-"), language)


def tts(text: str, out: Optional[str] = None, voice_ref: Optional[str] = None,
        voice_ref_text: Optional[str] = None, speed: float = 1.0,
        speaker: Optional[str] = None, instruct: Optional[str] = None,
        language: Optional[str] = None) -> Dict[str, Any]:
    if not text or not text.strip():
        raise ValueError("text 不能为空")
    ref = _in(voice_ref) if voice_ref else None
    path = _out(out, "tts", ".wav")
    if mock_mode():
        extra = mock.audio("voice", max(1.5, len(text) / 4.5), path)
    elif tts_backend() is qwen3tts:
        extra = pool.routed("tts", qwen3tts.tts, text, path, voice_ref=ref, voice_ref_text=voice_ref_text,
                            speed=speed, speaker=speaker, instruct=instruct, language=_tts_language(language))
    else:                                    # CosyVoice3 没有 speaker / instruct / language
        extra = pool.routed("tts", cosyvoice.tts, text, path, voice_ref=ref, voice_ref_text=voice_ref_text, speed=speed)
        ignored = [k for k, v in (("speaker", speaker), ("instruct", instruct), ("language", language)) if v]
        if ignored:
            extra["ignored"] = ignored
    return {"file": _rel(path), "duration_s": media.duration(path), **extra}


def music(prompt: str, duration: float = 30, out: Optional[str] = None,
          lyrics: Optional[str] = None) -> Dict[str, Any]:
    secs = float(duration)
    if not 3 <= secs <= 300:
        raise ValueError("music 时长要在 3–300 秒之间")
    path = _out(out, "music", ".wav")
    if mock_mode():
        extra = mock.audio("music", secs, path)
    elif path.suffix.lower() != ".wav":       # ACE-Step 只会往文件里写 pcm_s16le，mp3/m4a 等先出 wav 再按扩展名转码
        wav = path.with_name(path.stem + ".gen.wav")
        extra = pool.routed("music", template.music, prompt, secs, wav, lyrics=lyrics)
        media.run(["-y", "-i", str(wav), str(path)])
        wav.unlink(missing_ok=True)
    else:
        extra = pool.routed("music", template.music, prompt, secs, path, lyrics=lyrics)
    return {"file": _rel(path), "duration_s": media.duration(path), **extra}


def sfx(video: str, prompt: str, out: Optional[str] = None, duration: Optional[float] = None,
        negative_prompt: str = "") -> Dict[str, Any]:
    src = _in(video)
    secs = float(duration) if duration else media.duration(src)     # 默认跟随镜头时长
    path = _out(out, "sfx", ".wav")
    if mock_mode():
        extra = mock.audio("sfx", secs, path)
    elif path.suffix.lower() != ".wav":       # MMAudio 只会写 pcm wav，mp3/m4a 等先出 wav 再按扩展名转码
        wav = path.with_name(path.stem + ".gen.wav")
        extra = pool.routed("sfx", mmaudio.sfx, src, prompt, wav, seconds=secs, negative_prompt=negative_prompt)
        media.run(["-y", "-i", str(wav), str(path)])
        wav.unlink(missing_ok=True)
    else:
        extra = pool.routed("sfx", mmaudio.sfx, src, prompt, path, seconds=secs, negative_prompt=negative_prompt)
    return {"file": _rel(path), "duration_s": media.duration(path), **extra}


def lipsync(video: str, audio: str, out: Optional[str] = None) -> Dict[str, Any]:
    v, a = _in(video), _in(audio)
    path = _out(out, "lipsync", ".mp4")
    if mock_mode():
        extra = mock.copy(v, path)
    else:
        try:
            extra = pool.routed("lipsync", template.lipsync, v, a, path)
        except Exception as exc:  # noqa: BLE001
            if "Face not detected" in str(exc):
                raise RuntimeError("lipsync 失败：视频里有的帧检测不到人脸。LatentSync 要求每一帧都有清晰可见的正脸，"
                                   "换人脸始终在画面里的镜头，或先 trim / reframe 掉没有人脸的部分再试") from None
            raise
    return _clip_info(path, extra)


def asr(audio: str, language: str = "zh") -> Dict[str, Any]:
    src = _in(audio)
    res = mock.asr() if mock_mode() else pool.routed("asr", whisper.transcribe, src, language=language)
    return {"file": _rel(src), **res}


# --------------------------------------------------------------------------- 其它
def search(query: str, max_results: int = 5) -> Dict[str, Any]:
    if not query or not query.strip():
        raise ValueError("query 不能为空")
    n = max(1, min(5, int(max_results)))
    res = mock.search(query, n) if mock_mode() else search_backend.search(query, n)
    return {"query": query, **res}


def sample_frames(video: str, fps: float = 1, start: Optional[float] = None,
                  end: Optional[float] = None, out_dir: Optional[str] = None) -> Dict[str, Any]:
    src = _in(video)
    target = _resolve(out_dir) if out_dir else WORKDIR / "frames" / src.stem
    with concurrency.keyed("dir:" + str(target)):      # 同一个抽帧目录同一时刻只让一个调用写
        return _sample_frames(src, target, fps, start, end)


def _sample_frames(src: Path, target: Path, fps: float, start: Optional[float],
                   end: Optional[float]) -> Dict[str, Any]:
    if media.is_image(src):                  # 图片直接给模型看（t2i / image_edit 的结果也要能看）
        view = media.downscale_image(src, target / "view.jpg")
        return {"file": _rel(src), "count": 1, "sheet": _rel(view), "backend": "ffmpeg",
                "_images": [view]}
    fps_f = float(fps)
    if not 0 < fps_f <= 4:
        raise ValueError("fps 要在 (0, 4] 之间")
    frames = media.extract_frames(src, target, fps=fps_f, start=start, end=end)
    if not frames:
        raise ValueError("这个时间段没有抽到帧")
    shown = frames
    if len(frames) > 16:                     # 联系表最多放 16 格，均匀挑选；全部帧仍在磁盘上
        step = len(frames) / 16.0
        shown = [frames[int(i * step)] for i in range(16)]
    sheet = media.contact_sheet([f["file"] for f in shown], ["%.1fs" % f["t"] for f in shown],
                                target / "sheet.jpg")
    return {"dir": _rel(target), "count": len(frames),
            "frames": [{"file": _rel(f["file"]), "t": f["t"]} for f in frames],
            "sheet": _rel(sheet), "shown_in_sheet": len(shown), "backend": "ffmpeg",
            "_images": [sheet]}


def trim(video: str, start: float, end: float, out: Optional[str] = None) -> Dict[str, Any]:
    src = _in(video)
    start_f, end_f = float(start), float(end)
    total = media.duration(src)
    if not 0 <= start_f < end_f <= total + 0.05:
        raise ValueError("要求 0 ≤ start < end ≤ 时长（%.2fs）" % total)
    path = _out(out, "trim", ".mp4")
    part = _part(path)
    if media.is_audio(path):
        # 输出是音频文件（比如从参考视频里截原声给 tts 当参考音色）：只导出音轨，按后缀选编码。
        # 以前一律编成 AAC，写成 .wav 时得到的是“WAV 里装 AAC”，libsndfile 读不了
        codec = _AUDIO_CODECS.get(path.suffix.lower(), ["-c:a", "pcm_s16le"])
        media.run(["-y", "-ss", "%.3f" % start_f, "-i", str(src), "-t", "%.3f" % (end_f - start_f),
                   "-vn", *codec, str(part)])
        _commit(part, path)
        return {"file": _rel(path), "duration_s": media.duration(path), "start": start_f, "end": end_f,
                "backend": "ffmpeg"}
    # 逐帧精确裁剪：-c copy 只能落在关键帧上，裁出来的时长会偏
    media.run(["-y", "-ss", "%.3f" % start_f, "-i", str(src), "-t", "%.3f" % (end_f - start_f),
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", "-pix_fmt", "yuv420p",
               "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(part)])
    _commit(part, path)
    return _clip_info(path, {"start": start_f, "end": end_f, "backend": "ffmpeg"})


_AUDIO_CODECS = {".wav": ["-c:a", "pcm_s16le"], ".flac": ["-c:a", "flac"], ".mp3": ["-c:a", "libmp3lame", "-b:a", "192k"],
                 ".m4a": ["-c:a", "aac", "-b:a", "192k"], ".aac": ["-c:a", "aac", "-b:a", "192k"],
                 ".ogg": ["-c:a", "libopus", "-b:a", "128k"], ".opus": ["-c:a", "libopus", "-b:a", "128k"]}


def concat(videos: List[str], out: Optional[str] = None) -> Dict[str, Any]:
    if not isinstance(videos, list) or not videos:
        raise ValueError("videos 不能为空")
    srcs = [_in(v) for v in videos]
    infos = [media.probe(s) for s in srcs]
    first = infos[0]
    if not first["has_video"]:
        raise ValueError("第一个片段没有视频流")

    def _sig(i: Dict[str, Any]) -> tuple:
        return (i["width"], i["height"], round(i["fps"] or 0, 2), i["vcodec"], i["has_audio"], i["audio_rate"])

    path = _out(out, "concat", ".mp4")
    part = _part(path)
    reencoded = len({_sig(i) for i in infos}) > 1
    with tempfile.TemporaryDirectory(prefix="concat_", dir=str(WORKDIR)) as tmp:
        parts = srcs
        if reencoded:                        # 文档：编码不一致时重编码成同一分辨率、帧率、yuv420p 再 -c copy
            w, h = first["width"], first["height"]
            fps = int(round(first["fps"] or media.STD_FPS))
            parts = [media.normalize_clip(s, Path(tmp) / ("%03d.mp4" % i), w, h, fps=fps)
                     for i, s in enumerate(srcs)]
        listing = Path(tmp) / "list.txt"
        listing.write_text("".join("file '%s'\n" % str(p).replace("'", "'\\''") for p in parts),
                           encoding="utf-8")
        media.run(["-y", "-f", "concat", "-safe", "0", "-i", str(listing), "-c", "copy",
                   "-movflags", "+faststart", str(part)])
    _commit(part, path)
    return _clip_info(path, {"segments": len(srcs), "reencoded": reencoded, "backend": "ffmpeg"})


def _font() -> tuple:
    """返回 (fontsdir, FontName)。文档要求 DroidSansFallback.ttf；找不到时用 Noto Sans CJK SC。"""
    from .backends.common import under_models
    candidates = [os.environ.get("STUDIO_FONT_FILE", ""),
                  "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
                  "/usr/share/fonts/truetype/droid/DroidSansFallback.ttf",
                  str(Path(__file__).resolve().parent / "assets" / "fonts" / "DroidSansFallback.ttf"),
                  under_models("WSH/StoryTelling/VGen_N/assets/fonts/NotoSansCJKsc-Regular.otf"),
                  "/usr/share/fonts/google-noto-cjk/NotoSansCJKsc-Regular.otf",
                  "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"]
    for cand in candidates:
        if cand and Path(cand).is_file():
            try:
                from PIL import ImageFont
                family = ImageFont.truetype(cand, 12).getname()[0]
            except Exception:  # noqa: BLE001
                family = "Droid Sans Fallback" if "Droid" in cand else "Noto Sans CJK SC"
            return str(Path(cand).parent), family
    raise RuntimeError("找不到中文字体：设置 STUDIO_FONT_FILE 指向 DroidSansFallback.ttf")


def _srt_time(t: float) -> str:
    ms = int(round(float(t) * 1000))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def overlay(video: str, subtitles: Optional[List[Dict[str, Any]]] = None, srt: Optional[str] = None,
            srt_text: Optional[str] = None, out: Optional[str] = None,
            font_size: int = 18) -> Dict[str, Any]:
    src = _in(video)
    if subtitles:
        body = []
        for i, cue in enumerate(subtitles, start=1):
            s, e = float(cue["start"]), float(cue["end"])
            if e <= s:
                raise ValueError("第 %d 条字幕 end 必须大于 start" % i)
            body.append("%d\n%s --> %s\n%s\n" % (i, _srt_time(s), _srt_time(e), str(cue["text"]).strip()))
        text = "\n".join(body)
    elif srt_text:
        text = srt_text
    elif srt:
        text = _in(srt).read_text(encoding="utf-8")
    else:
        raise ValueError("subtitles、srt_text、srt 至少给一个")
    fontsdir, family = _font()
    path = _out(out, "subtitled", ".mp4")
    part = _part(path)
    with tempfile.TemporaryDirectory(prefix="subs_", dir=str(WORKDIR)) as tmp:
        (Path(tmp) / "subs.srt").write_text(text, encoding="utf-8")
        style = ("FontName=%s,FontSize=%d,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                 "BorderStyle=1,Outline=1,Shadow=0,MarginV=24" % (family, int(font_size)))
        vf = "subtitles=subs.srt:fontsdir=%s:force_style='%s'" % (fontsdir, style)
        media.run(["-y", "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
                   "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "copy", str(part)], cwd=Path(tmp))
    _commit(part, path)
    return _clip_info(path, {"font": family, "backend": "ffmpeg"})


def mix_audio(video: str, audio: str, out: Optional[str] = None, gain: float = 1.0,
              replace: bool = False, start: float = 0.0) -> Dict[str, Any]:
    v, a = _in(video), _in(audio)
    info = media.probe(v)
    if not info["has_video"]:
        raise ValueError("video 没有视频流")
    total = info["duration_s"]
    delay_ms = int(round(max(0.0, float(start)) * 1000))
    # 音轨不足补静音、超出裁掉：输出时长永远等于视频时长（旧实现带 -shortest，会把画面截短）
    a1 = "[1:a]aresample=48000,volume=%s%s,apad,atrim=0:%.3f[a1]" % (
        float(gain), ",adelay=%d:all=1" % delay_ms if delay_ms else "", total)
    if replace or not info["has_audio"]:
        graph, amap = a1, "[a1]"
    else:                                    # 文档：amix=inputs=2:duration=first:normalize=0
        graph = a1 + ";[0:a]aresample=48000[a0];[a0][a1]amix=inputs=2:duration=first:normalize=0[a]"
        amap = "[a]"
    path = _out(out, "mixed", ".mp4")
    part = _part(path)
    media.run(["-y", "-i", str(v), "-i", str(a), "-filter_complex", graph, "-map", "0:v:0",
               "-map", amap, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
               "-t", "%.3f" % total, str(part)])
    _commit(part, path)
    return _clip_info(path, {"audio": _rel(a), "gain": float(gain), "replace": bool(replace),
                             "start": float(start), "backend": "ffmpeg"})


def inspect(path: str) -> Dict[str, Any]:
    d = _resolve(path)
    if d.is_dir():                            # 给的是目录：列出里面的文件，省得模型猜文件名
        items = sorted(d.iterdir(), key=lambda x: (not x.is_dir(), x.name))
        return {"dir": _rel(d), "entries": [x.name + ("/" if x.is_dir() else "") for x in items[:200]],
                "count": len(items), "note": "这是目录；要看媒体信息请传具体文件路径"}
    p = _in(path)
    info = media.probe(p)
    short = min(info["width"], info["height"]) if info["width"] and info["height"] else None
    return {"file": _rel(p), "duration_s": info["duration_s"], "width": info["width"],
            "height": info["height"], "short_side": short, "fps": info["fps"],
            "has_audio": info["has_audio"], "bytes": info["bytes"], "backend": "ffmpeg"}


def export(src: str) -> Dict[str, Any]:
    s = _in(src)
    if not media.probe(s)["has_video"]:
        raise ValueError("src 没有视频流")
    path = WORKDIR / FINAL
    part = _part(path)
    media.run(["-y", "-i", str(s), "-c", "copy", "-movflags", "+faststart", str(part)])   # 文档：-c copy
    _commit(part, path)
    res = inspect(FINAL)
    res.update({"submitted": True, "src": _rel(s)})
    return res


# --------------------------------------------------------------------------- 注册表
def _obj(props: Dict[str, Any], required: List[str]) -> Dict[str, Any]:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


_STR = {"type": "string"}
_DUR = {"type": "number", "enum": list(SEGMENT_SECONDS), "description": "只能 5 或 8"}
_ASP = {"type": "string", "enum": ASPECTS}
_SEED = {"type": "integer", "description": "随机种子；重试时换一个"}
_OUT = {"type": "string", "description": "输出文件（相对路径），不给就自动编号"}

TOOLS: Dict[str, Dict[str, Any]] = {
    "t2v": {"fn": t2v, "desc": "文生视频（H3 fl2va，task=t2va）。单段 5 或 8 秒，短边 768，自带音轨。",
            "schema": _obj({"prompt": _STR, "duration": _DUR, "aspect": _ASP, "out": _OUT, "seed": _SEED},
                           ["prompt"])},
    "i2v": {"fn": i2v, "desc": "首帧生成视频（H3 fl2va）。有尾帧就一并传入。首帧可以是题目素材、t2i 的图，"
                              "或 sample_frames 取出的帧。画幅跟随首帧。",
            "schema": _obj({"prompt": _STR, "first_frame": _STR, "last_frame": _STR, "duration": _DUR,
                            "out": _OUT, "seed": _SEED}, ["prompt", "first_frame"])},
    "ref_video": {"fn": ref_video, "desc": "参考图或参考视频生成（H3 ref2va）。references 顺序对应 prompt 里的 "
                                          "<Image 1>/<Video 1>，最多 9 张图、3 段视频。",
                  "schema": _obj({"prompt": _STR, "references": {"type": "array", "items": _STR},
                                  "duration": _DUR, "aspect": _ASP, "out": _OUT, "seed": _SEED},
                                 ["prompt", "references"])},
    "t2i": {"fn": t2i, "desc": "文生图（Qwen-Image）：定妆、场景、关键帧。尺寸与 H3 一致，可直接当 i2v 首帧。",
            "schema": _obj({"prompt": _STR, "aspect": _ASP, "out": _OUT, "seed": _SEED,
                            "steps": {"type": "integer", "description": "采样步数，默认 30"}}, ["prompt"])},
    "image_edit": {"fn": image_edit, "desc": "改图（Qwen-Image-Edit）：原图 + 文本，可再带一张参考图 ref。"
                                            "要锁同一个人时，先出定妆，再改，再 i2v 或 ref_video。",
                   "schema": _obj({"src": _STR, "prompt": _STR, "ref": _STR, "out": _OUT, "seed": _SEED},
                                  ["src", "prompt"])},
    "tts": {"fn": tts, "desc": "旁白和对白（Qwen3-TTS）。不给 voice_ref 用 bench 固定音色，"
                              "speaker 可换预置音色（Vivian/Serena/Uncle_Fu/Dylan/Eric/Ryan/Aiden/"
                              "Ono_Anna/Sohee），instruct 写语气或风格；给了 voice_ref 就克隆该音色，"
                              "知道参考音频原文就一并传 voice_ref_text 更准，language 不给就自动判断。",
            "schema": _obj({"text": _STR, "out": _OUT, "voice_ref": _STR, "voice_ref_text": _STR,
                            "speed": {"type": "number"}, "speaker": _STR, "instruct": _STR,
                            "language": _STR}, ["text"])},
    "music": {"fn": music, "desc": "配乐：文本描述、可选歌词、时长（秒）→ wav。",
              "schema": _obj({"prompt": _STR, "duration": {"type": "number"}, "out": _OUT, "lyrics": _STR},
                             ["prompt"])},
    "sfx": {"fn": sfx, "desc": "动作声、环境声（MMAudio），按镜头视频生成；时长默认等于视频时长。",
            "schema": _obj({"video": _STR, "prompt": _STR, "out": _OUT, "duration": {"type": "number"},
                            "negative_prompt": _STR}, ["video", "prompt"])},
    "lipsync": {"fn": lipsync, "desc": "画面里有人在说这段台词时，用 tts 的音频对这段视频做口型。",
                "schema": _obj({"video": _STR, "audio": _STR, "out": _OUT}, ["video", "audio"])},
    "asr": {"fn": asr, "desc": "转写音频或视频的人声（带时间戳），检查自己的旁白和对白。",
            "schema": _obj({"audio": _STR, "language": _STR}, ["audio"])},
    "search": {"fn": search, "desc": "联网检索：教育、纪录、产品事实先查再写旁白。返回最多 5 条 title/url/snippet，"
                                    "是资料，不是评分标准。",
               "schema": _obj({"query": _STR, "max_results": {"type": "integer"}}, ["query"])},
    "sample_frames": {"fn": sample_frames, "desc": "看画面：按 fps 抽帧（默认每秒 1 帧），返回带时间标签的联系表图片。"
                                                  "可用 start/end 只看一段（帧越少每格越大）；传图片文件则直接返回该图。"
                                                  "每段生成后都要看，不行就改 prompt 再生成。",
                      "schema": _obj({"video": _STR, "fps": {"type": "number"}, "start": {"type": "number"},
                                      "end": {"type": "number"}, "out_dir": _STR}, ["video"])},
    "trim": {"fn": trim, "desc": "裁剪片段（秒，逐帧精确）。",
             "schema": _obj({"video": _STR, "start": {"type": "number"}, "end": {"type": "number"},
                             "out": _OUT}, ["video", "start", "end"])},
    "concat": {"fn": concat, "desc": "按顺序拼接片段；编码不一致时自动统一分辨率、帧率后再接。",
               "schema": _obj({"videos": {"type": "array", "items": _STR}, "out": _OUT}, ["videos"])},
    "overlay": {"fn": overlay, "desc": "烧字幕（中文字体）。subtitles 传 [{start, end, text}]（秒），"
                                      "或 srt_text 传 SRT 文本，或 srt 传工作目录里的 .srt 文件。",
                "schema": _obj({"video": _STR,
                                "subtitles": {"type": "array", "items": {"type": "object", "properties": {
                                    "start": {"type": "number"}, "end": {"type": "number"}, "text": _STR},
                                    "required": ["start", "end", "text"]}},
                                "srt": _STR, "srt_text": _STR, "out": _OUT,
                                "font_size": {"type": "integer"}}, ["video"])},
    "mix_audio": {"fn": mix_audio, "desc": "混音。replace=true 换掉原生音轨；否则与原音轨混合"
                                          "（amix，normalize=0）。gain 调这条音轨的音量，start 是它在视频里的起点（秒）。"
                                          "输出时长等于视频时长。人声和配乐同时在时，人声要听得清。",
                  "schema": _obj({"video": _STR, "audio": _STR, "out": _OUT, "gain": {"type": "number"},
                                  "replace": {"type": "boolean"}, "start": {"type": "number"}},
                                 ["video", "audio"])},
    "inspect": {"fn": inspect, "desc": "交卷前核对时长、画幅、音轨；给目录时列出目录里的文件。",
                "schema": _obj({"path": _STR}, ["path"])},
    "export": {"fn": export, "desc": "唯一交卷口：把 src 复制成 final.mp4。可以多次调用，以最后一次为准；"
                                    "没有 export 就不算交卷。",
               "schema": _obj({"src": _STR}, ["src"])},
}


def cleanup_parts() -> None:
    """清理异常中断留下的 .part 临时文件。只在没有调用在跑时整目录扫（并发时各调用自己的残留由 concurrency 清）。"""
    if concurrency.in_flight() > 0:
        return
    for p in WORKDIR.glob("*.part.*"):
        try:
            p.unlink()
        except OSError:
            pass


__all__ = ["TOOLS", "Unavailable", "set_workdir", "cleanup_parts", "FINAL"]


# --------------------------------------------------------------------------- v2 新增工具
from .extra import TOOLS as _EXTRA_TOOLS  # noqa: E402  放在末尾：extra 里的工具调用时才回头用这里的路径助手

TOOLS.update(_EXTRA_TOOLS)
