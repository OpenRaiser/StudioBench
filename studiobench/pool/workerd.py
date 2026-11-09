# -*- coding: utf-8 -*-
"""辅助模型服务：一个进程只服务一个工具、绑一张卡，在实例上执行和本机模式完全相同的后端函数。

    python -m studiobench pool worker --tool t2i --port 30203 [--name t2i-0] [--warm]
    python -m studiobench pool worker --tool t2i --port 30203 --fake 3        # 没 GPU：睡 3 秒 + 写占位产物
    python -m studiobench pool fake-h3 --port 30020 --fake 8                  # 假 H3 服务（同 vllm-omni 的视频接口，串行出片）

卡由启动方用环境变量指定（launch 写好）：t2i / image_edit / asr 用 STUDIO_T2I_DEVICE / STUDIO_EDIT_DEVICE / STUDIO_ASR_DEVICE
= cuda:<物理卡号>，音频类用 STUDIO_GPU_AUDIO=<物理卡号>（包装脚本里转成 CUDA_VISIBLE_DEVICES）。
接口：GET /health → {ok, tool, busy, warm}；POST /run {tool, args, kwargs} → {result, instance} 或 {error[, unavailable]}。
路径用 {"__path__": 绝对路径} 传，调用方和实例在同一台机器（或同一块共享盘）上。同一时刻只跑一个请求。
"""
from __future__ import annotations

import argparse
import itertools
import json
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse

from .client import dec

TOOLS = ("t2i", "image_edit", "asr", "tts", "sfx", "music", "lipsync", "image")
# image：双用实例，一个进程同时服务 t2i 和 image_edit（两个管线一个在卡上、一个在 CPU，按请求互换），
# 在网关里同时登记到 t2i 和 image_edit 两个池，哪边有活就接哪边。
SERVES = {"image": ("t2i", "image_edit")}


def real_fn(tool: str) -> Callable[..., Dict[str, Any]]:
    from ..tools.backends import mmaudio, qwen_image, template, tts_backend, whisper
    return {"t2i": qwen_image.t2i, "image_edit": qwen_image.image_edit, "asr": whisper.transcribe,
            "tts": lambda *a, **k: tts_backend().tts(*a, **k), "sfx": mmaudio.sfx, "music": template.music,
            "lipsync": template.lipsync}[tool]


def warm(tool: str) -> None:
    """起服务时先把模型载进显存，第一次调用不用等加载。"""
    from ..tools.backends import qwen_image, whisper
    if tool == "image":                                   # 两个管线都载进 CPU 内存，图像编辑先上卡（用得多）
        qwen_image._load("t2i")          # noqa: SLF001
        qwen_image._load("edit")         # noqa: SLF001
        qwen_image._activate("edit")     # noqa: SLF001
    elif tool == "t2i":
        qwen_image._load("t2i")          # noqa: SLF001
    elif tool == "image_edit":
        qwen_image._load("edit")         # noqa: SLF001
    elif tool == "asr":
        whisper._pipe()                  # noqa: SLF001


def fake_fn(tool: str, seconds: float) -> Callable[..., Dict[str, Any]]:
    """和真实后端同签名的替身：先睡 seconds 秒，再用 mock 写一个合法的产物。"""
    from ..tools.backends import mock, qwen_image

    def nap() -> None:
        time.sleep(seconds)

    def t2i(prompt, aspect, out, steps=30, seed=0):
        nap()
        return mock.image(prompt, out, size=qwen_image.SIZES[aspect])

    def image_edit(src, prompt, out, ref=None, seed=0):
        nap()
        return mock.copy(src, out)

    def asr(path, language="zh"):
        nap()
        return mock.asr()

    def tts(text, out, **kw):
        nap()
        return mock.audio("voice", max(1.5, len(text) / 4.5), out)

    def sfx(src, prompt, out, seconds=5.0, negative_prompt=""):
        nap()
        return mock.audio("sfx", seconds, out)

    def music(prompt, secs, out, lyrics=None):
        nap()
        return mock.audio("music", secs, out)

    def lipsync(video, audio, out):
        nap()
        return mock.copy(video, out)

    return {"t2i": t2i, "image_edit": image_edit, "asr": asr, "tts": tts, "sfx": sfx, "music": music,
            "lipsync": lipsync}[tool]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: Dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _reply(self, obj: Dict[str, Any], code: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        st = self.state
        if urlparse(self.path).path == "/health":
            code = 200 if st["warm"] != "failed" else 503
            self._reply({"ok": code == 200, "tool": st["tool"], "name": st["name"], "busy": st["lock"].locked(),
                         "warm": st["warm"], "served": st["served"]}, code)
        else:
            self._reply({"error": "未知路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        st = self.state
        if urlparse(self.path).path != "/run":
            self._reply({"error": "未知路径"}, 404)
            return
        n = int(self.headers.get("Content-Length") or 0)
        req = json.loads(self.rfile.read(n) or b"{}")
        fn = st["fns"].get(req.get("tool"))
        if fn is None:
            self._reply({"error": "这个实例只服务 %s，收到 %s" % ("、".join(st["fns"]), req.get("tool"))}, 400)
            return
        from ..tools.backends import Unavailable
        with st["lock"]:                                  # 一张卡一个模型：同一时刻只跑一个
            t0 = time.time()
            try:
                res = fn(*dec(req.get("args") or []), **dec(req.get("kwargs") or {}))
                out = {"result": json.loads(json.dumps(res, default=str)), "instance": st["name"]}
            except Unavailable as exc:
                out = {"error": str(exc), "unavailable": True}
            except Exception as exc:  # noqa: BLE001
                out = {"error": "%s 实例 %s 执行失败：%s" % (st["tool"], st["name"], exc)}
                print(traceback.format_exc(), flush=True)
            st["served"] += 1
            print("%s %s %.1fs %s" % (time.strftime("%H:%M:%S"), req.get("tool"), time.time() - t0,
                                      "ok" if "result" in out else out.get("error", "")[:200]), flush=True)
        self._reply(out)


def serve_worker(tool: str, port: int, name: str, fake: Optional[float], do_warm: bool, bind: str) -> None:
    st = {"tool": tool, "name": name, "lock": threading.Lock(), "served": 0, "warm": "skip",
          "fns": {t: fake_fn(t, fake) if fake is not None else real_fn(t) for t in SERVES.get(tool, (tool,))}}
    _Handler.state = st
    if do_warm and fake is None:
        st["warm"] = "loading"

        def load() -> None:
            try:
                with st["lock"]:
                    warm(tool)
                st["warm"] = "ready"
            except Exception:  # noqa: BLE001
                st["warm"] = "failed"
                print(traceback.format_exc(), flush=True)

        threading.Thread(target=load, daemon=True).start()
    server = ThreadingHTTPServer((bind, port), _Handler)
    server.daemon_threads = True
    print("worker %s（%s）监听 %s:%d%s" % (name, tool, bind, port, "，替身 %gs" % fake if fake is not None else ""),
          flush=True)
    server.serve_forever()


# --------------------------------------------------------------------------- 假 H3（测试用）
class _FakeH3(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    state: Dict[str, Any] = {}

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return

    def _reply(self, body: bytes, ctype: str = "application/json", code: int = 200) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Dict[str, Any], code: int = 200) -> None:
        self._reply(json.dumps(obj).encode("utf-8"), code=code)

    def do_GET(self) -> None:  # noqa: N802
        st, path = self.state, urlparse(self.path).path
        if path == "/health":
            self._json({"ok": True})
        elif path == "/v1/models":
            self._json({"data": [{"id": "fake-h3-%s" % st["name"]}]})
        elif path.startswith("/v1/videos/"):
            parts = path.split("/")
            job = st["jobs"].get(parts[3])
            if job is None:
                self._json({"error": "no job"}, 404)
            elif len(parts) > 4 and parts[4] == "content":
                self._reply(_fake_clip(st["cache"], job["seconds"]), "video/mp4")
            else:
                done = time.time() >= job["ready"]
                self._json({"id": parts[3], "status": "completed" if done else "in_progress",
                            "inference_time_s": st["secs"]})
        else:
            self._json({"error": "未知路径"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        st = self.state
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n)
        seconds = 8 if b'name="seconds"\r\n\r\n8' in body else 5
        with st["lock"]:                                 # 服务端串行出片：排在前一段之后
            start = max(time.time(), st["last"])
            st["last"] = start + st["secs"]
            vid = "v%d" % next(st["seq"])
            st["jobs"][vid] = {"ready": st["last"], "seconds": seconds}
        self._json({"id": vid, "status": "queued"})

    def do_DELETE(self) -> None:  # noqa: N802
        self.state["jobs"].pop(urlparse(self.path).path.split("/")[3], None)
        self._json({})


_CLIP_LOCK = threading.Lock()


def _fake_clip(cache: Path, seconds: int) -> bytes:
    from ..tools.backends import mock
    p = cache / ("fake_%d.mp4" % seconds)
    with _CLIP_LOCK:
        if not p.exists():
            cache.mkdir(parents=True, exist_ok=True)
            tmp = cache / ("tmp_%d_%d.mp4" % (seconds, os.getpid()))
            mock.clip("fake h3", seconds, tmp, "16:9", tag="[fake-h3]")
            tmp.replace(p)
    return p.read_bytes()


def serve_fake_h3(port: int, name: str, secs: float, cache: Path, bind: str) -> None:
    _FakeH3.state = {"name": name, "secs": secs, "lock": threading.Lock(), "last": 0.0, "jobs": {},
                     "seq": itertools.count(1), "cache": cache / name}      # 各实例各自的缓存，免得多进程抢写
    server = ThreadingHTTPServer((bind, port), _FakeH3)
    server.daemon_threads = True
    print("fake-h3 %s 监听 %s:%d，每段 %gs" % (name, bind, port, secs), flush=True)
    server.serve_forever()


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="辅助模型服务 / 假 H3")
    ap.add_argument("--tool", choices=TOOLS)
    ap.add_argument("--fake-h3", action="store_true", help="起一个假 H3 服务（测试用）")
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--name")
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--fake", type=float, help="替身模式：每次调用睡这么多秒，写占位产物")
    ap.add_argument("--warm", action="store_true", help="启动时预载模型")
    ap.add_argument("--cache", default="/tmp/studio_fake_h3", help="假 H3 的占位片缓存目录")
    args = ap.parse_args(argv)
    if args.fake_h3:
        serve_fake_h3(args.port, args.name or "fake", args.fake or 5.0, Path(args.cache), args.bind)
    elif args.tool:
        serve_worker(args.tool, args.port, args.name or args.tool, args.fake, args.warm, args.bind)
    else:
        ap.error("要给 --tool 或 --fake-h3")
    return 0
