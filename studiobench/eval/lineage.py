# -*- coding: utf-8 -*-
"""从 .calls.jsonl 反推成片的血缘：每个镜头在成片时间轴上的起止、混进去的人声/配乐轨。

· 保持时间轴的工具（overlay / mix_audio / lipsync）沿 video 参数往上追
· concat 把各输入按顺序摊到时间轴上，递归展开
· trim 截取一个窗口，子镜头按窗口平移、裁剪
· 生成类工具（t2v / i2v / ref_video）或不是工具产出的文件（inputs/）是叶子镜头
VBench 要求「先切成单段再评」，镜头边界就从这里来；追不出来时 eval_run 退回场景切换检测。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools import media

TIMELINE_TOOLS = ("overlay", "mix_audio", "lipsync", "composite", "color", "reframe")   # v2：后三个也不改时间轴
LEAF_TOOLS = ("t2v", "i2v", "ref_video",
              "render_html", "render_three", "render_blender", "ken_burns")              # v2：代码渲染、图片推拉也是镜头源
ROLE = {"tts": "voice", "music": "music", "sfx": "sfx"}


def load_calls(work: Path) -> List[Dict[str, Any]]:
    path = work / ".calls.jsonl"
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


class Lineage:
    def __init__(self, work: Path, calls: List[Dict[str, Any]]):
        self.work = work
        # 并发执行时日志按完成顺序写，这里按序号 i 排回去
        self.calls = sorted((c for c in calls if c.get("ok") and not c.get("rejected")), key=lambda c: c.get("i", 0))
        self._start = {c.get("i"): c.get("t") for c in calls if c.get("i") is not None}
        self.mixes: List[Dict[str, Any]] = []
        self.tts_texts: List[tuple] = []
        self.unresolved: List[str] = []

    def producer(self, rel: str, before: int) -> Optional[Dict[str, Any]]:
        """在第 before 次调用之前、最后一次写出 rel 的调用。

        并发执行（batch / 同一条消息里的多个调用）时，序号小的调用不一定在 before 开始前就写完了：
        有 t_end 时只认「before 开始时已经结束」的写入者。老日志没有 t_end，按序号判断。"""
        t0 = self._start.get(before)
        for c in reversed(self.calls):
            if c.get("i", 0) >= before or rel not in (c.get("outputs") or []):
                continue
            if t0 is not None and c.get("t_end") is not None and c["t_end"] > t0 + 1e-3:
                continue                           # 还在跑的同批调用，不是它的来源
            return c
        return None

    def audio_producer(self, rel: str, before: int, depth: int = 0) -> Optional[Dict[str, Any]]:
        """追 rel 这条音频的来源，穿过中间那一层：
        audio_edit（跟 audio 参数）、audio_mix（tracks 是多轨，取人声那一条，见 _voice_track）。
        不穿过去的话，中间隔一层人声就被标成 other，旁白文本也丢了。"""
        call = self.producer(rel, before)
        if call is None or depth > 16:
            return call
        tool, args = call["tool"], call.get("args") or {}
        nxt = ""
        if tool == "audio_edit":
            nxt = str(args.get("audio") or "")
        elif tool == "audio_mix":
            tracks = args.get("tracks") or []
            pick = self._voice_track(tracks, call["i"], args.get("duck"), depth)
            nxt = str(tracks[pick].get("file") or "") if pick is not None else ""
        return self.audio_producer(nxt, call["i"], depth + 1) if nxt else call

    def _voice_track(self, tracks: List[Dict[str, Any]], before: int, duck: Any,
                     depth: int = 0) -> Optional[int]:
        """audio_mix 的 tracks 里人声那条的下标：先认 duck（模型自己指的），再逐条追来源找 tts，都没有就取第一条。"""
        if duck is not None:
            try:
                d = int(duck)
                if 0 <= d < len(tracks):
                    return d
            except (TypeError, ValueError):
                pass
        if depth > 16:
            return 0 if tracks else None
        for k, t in enumerate(tracks):
            f = str(t.get("file") or "")
            if f and (self.audio_producer(f, before, depth + 1) or {}).get("tool") == "tts":
                return k
        return 0 if tracks else None

    def _duration(self, rel: str) -> float:
        try:
            return media.duration(self.work / rel)
        except Exception:  # noqa: BLE001
            return 0.0

    def shots(self, rel: str, before: int, depth: int = 0, offset: float = 0.0) -> List[Dict[str, Any]]:
        """rel 这个文件在它自己时间轴上的镜头列表 [{start, end, source, tool}]。
        offset 是 rel 的 0 秒落在成片上的位置，只用来给混音轨排时间顺序。"""
        call = self.producer(rel, before)
        if call is None or depth > 64:
            return [{"start": 0.0, "end": self._duration(rel), "source": rel, "tool": None}]
        tool, args, i = call["tool"], call.get("args") or {}, call["i"]
        if tool in TIMELINE_TOOLS:
            if tool == "mix_audio":
                self._note_mix(call, offset)
            return self.shots(args.get("video", ""), i, depth + 1, offset)
        if tool == "concat":
            out, acc = [], 0.0
            for v in args.get("videos") or []:
                sub = self.shots(v, i, depth + 1, offset + acc)
                length = self._duration(v) or (sub[-1]["end"] if sub else 0.0)
                out += [dict(s, start=s["start"] + acc, end=min(s["end"], length) + acc) for s in sub]
                acc += length
            return out
        if tool == "transition":                         # v2：像 concat，但相邻两段重叠 duration 秒
            out, acc, d = [], 0.0, float(args.get("duration", 0.5) or 0.5)
            videos = args.get("videos") or []
            for k, v in enumerate(videos):
                sub = self.shots(v, i, depth + 1, offset + acc)
                length = self._duration(v) or (sub[-1]["end"] if sub else 0.0)
                out += [dict(s, start=s["start"] + acc, end=min(s["end"], length) + acc) for s in sub]
                acc += length - (d if k < len(videos) - 1 else 0.0)
            return out
        if tool == "speed":                              # v2：时间轴按倍速缩放，倒放时镜像
            f = float(args.get("factor", 1.0) or 1.0)
            sub = self.shots(args.get("video", ""), i, depth + 1, offset)
            total = (sub[-1]["end"] if sub else 0.0)
            if args.get("reverse"):
                sub = [dict(s, start=total - s["end"], end=total - s["start"]) for s in reversed(sub)]
            return [dict(s, start=s["start"] / f, end=s["end"] / f) for s in sub]
        if tool == "trim":
            lo, hi = float(args.get("start", 0)), float(args.get("end", 0))
            sub = self.shots(args.get("video", ""), i, depth + 1, offset - lo)
            return [dict(s, start=max(s["start"], lo) - lo, end=min(s["end"], hi) - lo)
                    for s in sub if s["end"] > lo and s["start"] < hi]
        if tool in LEAF_TOOLS:
            return [{"start": 0.0, "end": self._duration(rel), "source": rel, "tool": tool}]
        self.unresolved.append("%s ← %s" % (rel, tool))
        return [{"start": 0.0, "end": self._duration(rel), "source": rel, "tool": tool}]

    def _note_mix(self, call: Dict[str, Any], offset: float) -> None:
        args = call.get("args") or {}
        audio = args.get("audio", "")
        src = self.audio_producer(audio, call["i"])          # 穿过 audio_edit / audio_mix
        role = ROLE.get(src["tool"]) if src else None
        at = round(offset + float(args.get("start", 0.0) or 0.0), 3)
        if src and src["tool"] == "tts":
            self.tts_texts.append((at, str((src.get("args") or {}).get("text", ""))))
        self.mixes.append({"i": call["i"], "audio": audio, "role": role or "other", "at": at,
                           "gain": float(args.get("gain", 1.0)), "replace": bool(args.get("replace", False))})


def trace_final(work: Path, calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    """成片（最后一次 export）的镜头切分和混音信息。"""
    lin = Lineage(work, calls)
    exports = [c for c in lin.calls if c["tool"] == "export"]
    if not exports:
        return {"ok": False, "reason": "没有 export"}
    last = exports[-1]
    src = (last.get("args") or {}).get("src", "")
    shots = [s for s in lin.shots(src, last["i"]) if s["end"] - s["start"] > 0.2]
    return {"ok": bool(shots), "export_src": src, "shots": shots, "mixes": sorted(lin.mixes, key=lambda m: m["i"]),
            "tts_texts": [t for _, t in sorted(lin.tts_texts, key=lambda x: x[0])], "unresolved": lin.unresolved,
            "generated_shots": sum(1 for s in shots if s["tool"] in LEAF_TOOLS)}
