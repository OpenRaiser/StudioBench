# -*- coding: utf-8 -*-
"""后端公共部分：不可用异常、配置读取、子进程调用。"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

TOOLS_DIR = Path(__file__).resolve().parent.parent      # studiobench/tools/
WORKERS = TOOLS_DIR / "workers"

# 可选的本地模型/权重根目录。公开仓库不内置任何路径，全部由使用方通过环境变量提供。
MODELS_ROOT = os.environ.get("STUDIO_MODELS_ROOT", "").rstrip("/")
STUDIO_SHARE = os.environ.get("STUDIO_SHARE", "").rstrip("/")


def under_models(*parts: str) -> str:
    """STUDIO_MODELS_ROOT 下的路径；未配置时返回空串（后端会报“未配置”）。"""
    return str(Path(MODELS_ROOT, *parts)) if MODELS_ROOT else ""


def under_share(*parts: str) -> str:
    """STUDIO_SHARE 下的路径；未配置时返回空串。"""
    return str(Path(STUDIO_SHARE, *parts)) if STUDIO_SHARE else ""


class Unavailable(RuntimeError):
    """后端在本机不可用（缺服务、权重、环境或配置）。原因会原样返回给模型。"""


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def env_int(name: str, default: int) -> int:
    try:
        return int(env(name, str(default)))
    except ValueError:
        return default


def is_secret(name: str) -> bool:
    upper = name.upper()
    return any(key in upper for key in ("TOKEN", "KEY", "SECRET", "PASSWORD"))


def check_paths(**paths: str) -> Optional[str]:
    """返回第一个不存在的路径说明；全部存在返回 None。"""
    for label, value in paths.items():
        if not value:
            return "未配置 %s" % label
        if not Path(value).exists():
            return "%s 不存在：%s" % (label, value)
    return None


def _tail(text: str, lines: int = 12, chars: int = 1200) -> str:
    return "\n".join((text or "").strip().splitlines()[-lines:])[-chars:]


def run_subprocess(argv: Sequence[str], timeout: float, cwd: Optional[Path] = None,
                   extra_env: Optional[Mapping[str, str]] = None) -> subprocess.CompletedProcess:
    """跑模型子进程。stdin 关掉，stdout/stderr 收集起来，失败时把尾部日志带进异常。"""
    run_env = dict(os.environ)
    for key in ("PYTHONPATH", "PYTHONHOME"):          # 模型各有自己的环境，别把本包的路径带进去
        run_env.pop(key, None)
    run_env.update({k: v for k, v in (extra_env or {}).items() if v is not None})
    try:
        proc = subprocess.run([str(a) for a in argv], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True, timeout=timeout,
                              cwd=str(cwd) if cwd else None, env=run_env)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("子进程超时（%ds）：%s" % (timeout, Path(str(argv[1] if len(argv) > 1 else argv[0])).name)) from exc
    if proc.returncode != 0:
        raise RuntimeError("子进程失败（rc=%d）：%s" % (proc.returncode, _tail(proc.stderr or proc.stdout)))
    return proc


def run_worker(python: str, script: str, payload: Dict[str, Any], timeout: float,
               extra_env: Optional[Mapping[str, str]] = None, cwd: Optional[Path] = None) -> Dict[str, Any]:
    """在模型自己的 Python 环境里跑 workers/<script>：请求/响应都走临时 JSON 文件，
    不解析 stdout（模型库会往 stdout 打日志）。"""
    with tempfile.TemporaryDirectory(prefix="studio_worker_") as tmp:
        req = Path(tmp) / "request.json"
        resp = Path(tmp) / "response.json"
        req.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        run_subprocess([python, str(WORKERS / script), "--request", str(req), "--response", str(resp)],
                       timeout=timeout, cwd=cwd, extra_env=extra_env)
        if not resp.exists():
            raise RuntimeError("worker 没有写回结果：%s" % script)
        out = json.loads(resp.read_text(encoding="utf-8"))
    if not out.get("ok"):
        raise RuntimeError("worker 报错：%s" % out.get("error"))
    return out


def run_argv_template(var: str, values: Mapping[str, Any], timeout: float) -> None:
    """按环境变量里的 JSON argv 模板跑外部命令（音乐、口型等待定模型）。

    模板是 JSON 字符串数组，每个元素单独 `.format(**values)`，不经过 shell，
    所以 prompt 里的引号、空格不会出问题。字面量花括号写成 {{ }}。
    """
    raw = env(var)
    if not raw:
        raise Unavailable("未配置 %s（命令模板），见 RUNBOOK_GPU.md" % var)
    try:
        template: List[str] = json.loads(raw)
        assert isinstance(template, list) and all(isinstance(x, str) for x in template)
    except Exception as exc:  # noqa: BLE001
        raise Unavailable("%s 不是 JSON 字符串数组：%s" % (var, exc)) from None
    argv = [part.format(**{k: str(v) for k, v in values.items()}) for part in template]
    run_subprocess(argv, timeout=timeout)
