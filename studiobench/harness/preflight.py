#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查本机每个工具的后端是否就绪。GPU 机上开跑前先跑它。

    python3 preflight.py             # 表格：工具 / 后端 / 状态 / 说明
    python3 preflight.py --deep      # 另外在各模型环境里试 import、查 CUDA（慢一些）
    python3 preflight.py --json

工具后端用 --python（默认 STUDIO_PYTHON，即 MCP server 的解释器）检查，和 run 开跑前的检查完全一致。
退出码：核心工具 t2v / i2v / t2i 都可用时为 0。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from ..tools import backends, media
from ..tools.backends import cosyvoice, mmaudio, qwen3tts
from . import run as runner

CORE = ("t2v", "i2v", "t2i")


def _py(python: str, code: str, env: Optional[Dict[str, str]] = None, timeout: int = 240) -> Tuple[bool, str]:
    run_env = dict(os.environ)
    run_env.update(env or {})
    try:
        proc = subprocess.run([python, "-c", code], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, env=run_env)
    except subprocess.TimeoutExpired:
        return False, "超时（%ds）：%s" % (timeout, python)
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    tail = (proc.stdout.strip().splitlines() or [""])[-1] if proc.returncode == 0 else \
        "\n".join((proc.stderr or proc.stdout).strip().splitlines()[-3:])
    return proc.returncode == 0, tail[:400]


def _ffmpeg() -> Tuple[bool, str]:
    try:
        version = subprocess.run([media.FFMPEG, "-version"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL).stdout.splitlines()[0]
        filters = subprocess.run([media.FFMPEG, "-hide_banner", "-filters"], capture_output=True, text=True,
                                 stdin=subprocess.DEVNULL).stdout
        encoders = subprocess.run([media.FFMPEG, "-hide_banner", "-encoders"], capture_output=True, text=True,
                                  stdin=subprocess.DEVNULL).stdout
    except Exception as exc:  # noqa: BLE001
        return False, "ffmpeg 不可用：%s" % exc
    need = [f for f in ("subtitles", "ebur128", "amix", "apad", "adelay", "volume") if " %s " % f not in filters]
    if "libx264" not in encoders:
        need.append("libx264")
    return (not need), (version if not need else "缺少：%s（%s）" % (", ".join(need), version))


def _font() -> Tuple[bool, str]:
    from ..tools import registry
    try:
        fontsdir, family = registry._font()   # noqa: SLF001
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    note = "" if "Droid" in family else "（文档要求 DroidSansFallback.ttf；可设 STUDIO_FONT_FILE）"
    return True, "%s @ %s%s" % (family, fontsdir, note)


def _claude() -> Tuple[bool, str]:
    exe = os.environ.get("STUDIO_CLAUDE") or shutil.which("claude")
    if not exe:
        return False, "找不到 claude CLI"
    ok, ver = _py(sys.executable, "import subprocess;print(subprocess.run([%r,'--version'],capture_output=True,"
                                  "text=True).stdout.strip())" % exe, timeout=60)
    base = os.environ.get("STUDIO_BASE_URL", "http://127.0.0.1:8788")
    u = urlparse(base)
    try:
        socket.create_connection((u.hostname, u.port or (443 if u.scheme == "https" else 80)), timeout=3).close()
        reach = "代理 %s 可连" % base
    except OSError as exc:
        return False, "%s；代理 %s 连不上：%s" % (ver, base, exc)
    return ok, "%s；%s" % (ver, reach)


def _deep(python: str) -> List[Tuple[str, bool, str]]:
    """各项并行跑（每项单独超时），共享盘上首次 import torch 可能要一两分钟。"""
    jobs = []
    jobs.append(("MCP 解释器 CUDA", python, "import torch;print('cuda=%s n=%d torch=%s' % "
                 "(torch.cuda.is_available(), torch.cuda.device_count(), torch.__version__))", None))
    jobs.append(("diffusers 管线", python, "import diffusers as d;print(d.__version__, "
                 "[c for c in ('QwenImagePipeline','QwenImageEditPipeline','QwenImageEditPlusPipeline') "
                 "if hasattr(d,c)])", None))
    jobs.append(("transformers", python, "import transformers as t;print(t.__version__)", None))
    if backends.tts_backend() is qwen3tts:                 # 默认：Qwen3-TTS
        qt = qwen3tts.config()
        jobs.append(("Qwen3-TTS 环境", qt["python"], "import torch, soundfile, transformers;"
                     "from qwen_tts import Qwen3TTSModel;"
                     "print('ok cuda=%s tf=%s' % (torch.cuda.is_available(), transformers.__version__))", None))
    else:                                                  # 回退：CosyVoice3
        cv = cosyvoice.config()
        jobs.append(("CosyVoice 环境", cv["python"], "import sys;sys.path[:0]=[%r,%r];"
                     "from cosyvoice.cli.cosyvoice import AutoModel;import soundfile;"
                     "import torch;print('ok cuda=%%s' %% torch.cuda.is_available())"
                     % (cv["repo"], cv["repo"] + "/third_party/Matcha-TTS"), None))
    mm = mmaudio.config()
    jobs.append(("MMAudio 环境", mm["python"], "import mmaudio, open_clip, torchdiffeq, torchaudio, torch;"
                 "print('ok cuda=%s' % torch.cuda.is_available())", {"PYTHONPATH": mm["repo"]}))
    with ThreadPoolExecutor(max_workers=len(jobs)) as pool:
        futures = [(name, pool.submit(_py, py, code, env)) for name, py, code, env in jobs]
        rows = [(name, *fut.result()) for name, fut in futures]
    lacking = mmaudio.missing_hf_models(mm["hf_cache"])
    rows.append(("MMAudio HF 缓存", not lacking,
                 ("齐全：%s" % mm["hf_cache"]) if not lacking else "缺 %s（缓存目录 %s）" % (", ".join(lacking), mm["hf_cache"])))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deep", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--python", default=os.environ.get("STUDIO_PYTHON") or sys.executable)
    args = ap.parse_args()
    if os.environ.get("STUDIO_CUDA_COMPAT"):          # 同 run.py：--deep 的 CUDA 检查要带上 cuda-compat，否则恒为 False
        os.environ["LD_LIBRARY_PATH"] = os.environ["STUDIO_CUDA_COMPAT"] + ":" + os.environ.get("LD_LIBRARY_PATH", "")

    status = runner._preflight(dict(runner._backend_env(argparse.Namespace(mock=backends.mock_mode())),  # noqa: SLF001
                                    _PYTHON=args.python))
    if "_error" in status:
        print(status["_error"]["detail"])
        return 1
    extra = [("ffmpeg", *_ffmpeg()), ("中文字体", *_font()), ("claude CLI", *_claude())]
    if args.deep:
        extra += _deep(args.python)

    if args.json:
        print(json.dumps({"tools": status, "checks": [{"name": n, "ok": ok, "detail": d} for n, ok, d in extra],
                          "mock": backends.mock_mode()}, ensure_ascii=False, indent=2))
    else:
        print("%-14s %-26s %-5s %s" % ("工具", "后端", "状态", "说明"))
        for tool, st in status.items():
            if st["backend"] == "ffmpeg":
                continue
            print("%-14s %-26s %-5s %s" % (tool, st["backend"], "OK" if st["ok"] else "缺", st["detail"]))
        print("%-14s %-26s %-5s %s" % ("ffmpeg 工具", "ffmpeg", "OK",
                                       " ".join(t for t, st in status.items() if st["backend"] == "ffmpeg")))
        print()
        for name, ok, detail in extra:
            print("%-14s %-5s %s" % (name, "OK" if ok else "缺", detail))
        if backends.mock_mode():
            print("\n注意：STUDIO_BACKEND=mock，所有生成工具都是占位实现。")
    return 0 if all(status[t]["ok"] for t in CORE) else 1


if __name__ == "__main__":
    sys.exit(main())
