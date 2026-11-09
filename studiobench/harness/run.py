#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跑一道 StudioBench 题（v2）：独立工作目录 + 原子工具（MCP）+ 一次隔离的 Claude Code 会话。

    ./sb run --task <题目.json>                         # 真实后端（GPU 机）
    ./sb run --task <题目.json> --mock                  # 没 GPU 时验证 harness
    ./sb run --task ... --model <被测模型> --base http://127.0.0.1:8788

隔离：被测模型只看得到系统提示（sp.md）、用户消息、studio 的 MCP 工具和 --builtin-tools 列出的内置工具。
· v2：默认**不给内置工具**（--builtin-tools "Bash,Read,..." 是调试开关，不是评测配置）。
                            要跑代码就写在 render_html / render_three / render_blender 的参数里：
                            这样每次调用都有记录、计入预算、能追血缘，和「只给原子 API」不冲突。
                            render_* / beat_detect / remove_bg 后台照旧用工具箱，只是不再把 PATH 交给模型；
                            跑完用 audit.py 扫工具调用里的绝对路径（碰了工作目录和工具箱以外的评测数据就判无效），
                            并扫 render_blender 的 code 参数——没 shell 时它是唯一能跑任意代码的地方
· --setting-sources project + 全新 HOME
                            不加载 ~/.claude 下的 CLAUDE.md、插件、hooks、输出风格。实测不这样做时，
                            被测会话能看到用户的全局 AGENTS.md 和 learning 输出风格
· --strict-mcp-config       只挂 studio 一个 MCP server
· --system-prompt-file      系统提示就是 sp.md，不叠加 Claude Code 默认的编程助手提示（--sp-mode append 可改回）
· 密钥只走进程环境和 0600 临时文件，不写进共享盘上的 mcp.json / settings.json

产物（默认 StudioBench/runs/<task>__<model>__<时间>/）：
  work/              会话工作目录：片段、音频、final.mp4、.calls.jsonl
  final.mp4          交卷副本（有 export 才有）
  transcript.jsonl   Claude Code stream-json 全量事件
  script.md          模型回复里的文字（剧本、分镜），评测「运镜是否按分镜」要用
  result.json        结果、统计、后端清单、有效性判断
  prompt.txt / sp.md / mcp.json / settings.json / mcp_server.log / claude.stderr.log
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools import media, procgroups
from . import audit, taskspec

PACKAGE = Path(__file__).resolve().parents[1]        # studiobench/
REPO = PACKAGE.parent                                 # 含 studiobench/ 的目录，MCP server 的 PYTHONPATH

RUNS_DIR = os.environ.get("STUDIO_RUNS_DIR", str(Path.cwd() / "runs"))
TOOLBOX = Path(os.environ.get("STUDIO_TOOLBOX", str(Path.cwd() / ".toolbox")))
TOOLSET = "v2"
DEFAULT_BUILTINS = ""                                 # v2 不给 shell；--builtin-tools 只留着调试用
PROXY_VARS = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "all_proxy",
              "ftp_proxy", "FTP_PROXY")
SHELL_DROP_PREFIXES = ("STUDIO_", "H3_", "QWEN_", "T2I_", "HF_", "TRANSFORMERS_", "AIHUB")
CORE_TOOLS = ("t2v", "i2v", "t2i")                  # 缺了就没法出片，默认拒绝开跑
CONFIG_PREFIXES = ("STUDIO_", "H3_", "QWEN_", "T2I_", "HF_", "CUDA_", "TRANSFORMERS_")
NO_PASSTHROUGH = {"STUDIO_TOKEN", "STUDIO_WORKDIR", "STUDIO_RUN_DIR", "STUDIO_MAX_CALLS",
                  "STUDIO_DEADLINE", "STUDIO_SECRETS_FILE"}


# 续跑：会话因为平台故障（模型 API、网关）中断后，在原运行目录里用 claude --resume 接着原会话跑，
# 模型带着完整上下文从中断处继续；调用次数和墙钟预算接着原来的算。
RESUME_PROMPT = "（会话因平台故障中断，已恢复。请从中断处继续完成任务，最后用 export 交卷。）"


def _encode_cwd(path: Path) -> str:
    """Claude Code 存会话的目录名：~/.claude/projects/<cwd 里非字母数字的字符换成 ->/"""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def interrupted_by_api(run_dir: Path) -> bool:
    """会话是不是因为模型 API 报错才结束的（Claude Code 会补一条 model=<synthetic> 的 "API Error…" 消息）。"""
    last = None
    for e in _read_jsonl(Path(run_dir) / "transcript.jsonl"):
        if e.get("type") in ("assistant", "user"):
            last = e
    if not last or last.get("type") != "assistant":
        return False
    msg = last.get("message") or {}
    text = "".join(b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict))
    return msg.get("model") == "<synthetic>" and text.startswith("API Error")


def _session_from_transcript(events: List[Dict[str, Any]], prompt: str, cwd: Path,
                             version: str) -> "tuple[str, List[Dict[str, Any]]]":
    """没存会话文件时，从 stream-json transcript 还原 Claude Code 的会话：按顺序重建 user / assistant 的父子链，
    开头补上原题目消息；中途续跑过的地方（又出现 system/init）补上续跑提示；去掉 Claude Code 自己生成的报错消息。"""
    sid = next((e["session_id"] for e in events if e.get("session_id")), None) or str(uuid.uuid4())
    rows: List[Dict[str, Any]] = []
    parent = None

    def add(kind: str, message: Any, uid: Optional[str] = None, ts: Optional[str] = None) -> None:
        nonlocal parent
        uid = uid or str(uuid.uuid4())
        rows.append({"type": kind, "message": message, "uuid": uid, "parentUuid": parent, "sessionId": sid,
                     "cwd": str(cwd), "isSidechain": False, "userType": "external", "version": version,
                     "timestamp": ts or time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())})
        parent = uid

    add("user", {"role": "user", "content": prompt})
    seen_init = False
    for e in events:
        kind = e.get("type")
        if kind == "system" and e.get("subtype") == "init":
            if seen_init:
                add("user", {"role": "user", "content": RESUME_PROMPT})
            seen_init = True
        elif kind == "assistant":
            msg = e.get("message") or {}
            if msg.get("model") == "<synthetic>":
                continue
            add("assistant", msg, e.get("uuid"), e.get("timestamp"))
        elif kind == "user":
            add("user", e.get("message"), e.get("uuid"), e.get("timestamp"))
    while len(rows) > 1 and rows[-1]["message"].get("content") == RESUME_PROMPT:
        rows.pop()                             # 之前续跑失败、什么都没跑成：这次续跑会再发一遍提示，别重复
    return sid, rows


def _session_attachments(cmd: List[str], cwd: Path, env: Dict[str, str]) -> List[Dict[str, Any]]:
    """让 Claude Code 用同样的参数在同一个工作目录里空跑一次（打到一个立刻返回 400 的本地假接口，不花钱），
    取它在会话开头生成的 attachment 记录（环境、模型、日期、系统提示快照…）。还原会话缺了这些，
    Claude Code 会发出一条空的 system 消息，上游直接 400：messages.1: system content must contain at least one block。"""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class _Reject(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            body = b'{"type":"error","error":{"type":"invalid_request_error","message":"capture only"}}'
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_a):  # noqa: D401
            return

    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Reject)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    home = Path(tempfile.mkdtemp(prefix="studio_att_"))
    try:
        run_env = dict(env, HOME=str(home), ANTHROPIC_BASE_URL="http://127.0.0.1:%d" % srv.server_port)
        subprocess.run(cmd, cwd=str(cwd), env=run_env, stdin=subprocess.DEVNULL, capture_output=True, timeout=300)
        rows = [json.loads(l) for f in (home / ".claude" / "projects").glob("*/*.jsonl")
                for l in f.read_text(encoding="utf-8").splitlines() if l.strip()]
        return [r for r in rows if r.get("type") == "attachment"]
    finally:
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def _save_session(fresh_home: Path, out: Path) -> None:
    """把这次的会话文件存进运行目录（临时 HOME 跑完就删），以后被打断了可以原样续跑。"""
    for f in (fresh_home / ".claude" / "projects").glob("*/*.jsonl"):
        (out / "session").mkdir(exist_ok=True)
        shutil.copy2(f, out / "session" / f.name)


def _is_secret(name: str) -> bool:
    return any(k in name.upper() for k in ("TOKEN", "KEY", "SECRET", "PASSWORD"))


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "model"


def _backend_env(args: argparse.Namespace) -> Dict[str, str]:
    """要透传给 MCP server 的后端配置（H3_* / QWEN_* / STUDIO_* …）。"""
    env = {k: v for k, v in os.environ.items()
           if k.startswith(CONFIG_PREFIXES) and k not in NO_PASSTHROUGH}
    if args.mock:
        env["STUDIO_BACKEND"] = "mock"
    else:
        env.pop("STUDIO_BACKEND", None)
    return env


def _preflight(env: Dict[str, str]) -> Dict[str, Any]:
    """用 MCP server 将要拿到的环境跑一遍后端检查（同一个解释器才准确，所以起子进程）。"""
    code = ("import json,sys; sys.path.insert(0, %r); from studiobench.tools import backends; "
            "print(json.dumps(backends.status(deep=True), ensure_ascii=False))" % str(REPO))
    proc_env = dict(os.environ)
    proc_env.update(env)
    try:
        out = subprocess.run([env.get("_PYTHON", sys.executable), "-c", code], capture_output=True,
                             text=True, env=proc_env, stdin=subprocess.DEVNULL, timeout=120)
        return json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        return {"_error": {"ok": False, "detail": "preflight 失败：%s" % exc}}


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    rows = []
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def _transcript_summary(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    init = next((e for e in events if e.get("type") == "system" and e.get("subtype") == "init"), {})
    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    texts: List[str] = []
    for e in events:
        if e.get("type") != "assistant":
            continue
        for block in (e.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text", "").strip():
                texts.append(block["text"].strip())
    tools = init.get("tools") or []
    llm = {k: final.get(k) for k in ("subtype", "is_error", "num_turns", "duration_ms",
                                     "total_cost_usd", "usage", "stop_reason")}
    results = [e for e in events if e.get("type") == "result"]
    if len(results) > 1:                                # 续跑过：花费、轮数、耗时是几段会话之和
        llm["sessions"] = len(results)
        for k in ("num_turns", "duration_ms", "total_cost_usd"):
            llm[k] = round(sum(r.get(k) or 0 for r in results), 6)
    return {
        "texts": texts,
        "isolation": {
            "tools_seen": tools,
            "non_studio_tools": [t for t in tools if not str(t).startswith("mcp__studio__")],
            "mcp_servers": init.get("mcp_servers"),
            "model": init.get("model"),
        },
        "llm": llm,
        "final_text": final.get("result"),
    }


def error_kind(call: Dict[str, Any]) -> str:
    """把失败的工具调用粗分成几类，批量运行时据此区分「模型用错了」和「服务挂了」。"""
    err = str(call.get("error") or "")
    if call.get("unavailable"):
        return "unavailable"
    if err.startswith("参数错误") or "必须" in err or "只许" in err or "不能为空" in err or "找不到文件" in err:
        return "usage"
    if "连不上" in err or "Connection" in err or "HTTP 5" in err:
        return "service"
    if "超时" in err or "Timeout" in err:
        return "timeout"
    return "other"


def _call_stats(calls: List[Dict[str, Any]]) -> Dict[str, Any]:
    executed = [c for c in calls if not c.get("rejected")]
    by_tool: Dict[str, Dict[str, Any]] = {}
    for c in executed:
        row = by_tool.setdefault(c["tool"], {"calls": 0, "errors": 0, "backends": []})
        row["calls"] += 1
        row["errors"] += 0 if c.get("ok") else 1
        if c.get("backend") and c["backend"] not in row["backends"]:
            row["backends"].append(c["backend"])
    return {
        "tool_calls": len(executed),
        "tool_errors": sum(1 for c in executed if not c.get("ok")),
        "rejected_calls": sum(1 for c in calls if c.get("rejected")),
        "by_tool": by_tool,
        "mock_tools_used": sorted({c["tool"] for c in executed if c.get("ok") and c.get("backend") == "mock"}),
        "unavailable_calls": sorted({c["tool"] for c in executed if c.get("unavailable")}),
        "exports": sum(1 for c in executed if c["tool"] == "export" and c.get("ok")),
        "error_kinds": {k: sum(1 for c in executed if not c.get("ok") and error_kind(c) == k)
                        for k in ("usage", "service", "timeout", "unavailable", "other")},
    }


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="studiobench run", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--task", help="题目 json（--resume-run 时不用给）")
    ap.add_argument("--resume-run", help="续跑：被平台故障打断的运行目录，用 claude --resume 接着原会话跑，"
                                         "调用次数和墙钟预算接着原来的算")
    ap.add_argument("--out", help="运行目录；默认 <runs-dir>/<task>__<model>__<时间>")
    ap.add_argument("--runs-dir", default=RUNS_DIR)
    ap.add_argument("--model", default=os.environ.get("STUDIO_MODEL", "claude-opus-5-5"))
    ap.add_argument("--base", default=os.environ.get("STUDIO_BASE_URL", "http://127.0.0.1:8788"),
                    help="ANTHROPIC_BASE_URL（本地代理或网关）；令牌从环境变量 STUDIO_TOKEN 读")
    ap.add_argument("--auth", choices=("api-key", "bearer"), default=os.environ.get("STUDIO_AUTH", "api-key"),
                    help="令牌作为 ANTHROPIC_API_KEY（x-api-key）还是 ANTHROPIC_AUTH_TOKEN（Bearer）")
    ap.add_argument("--max-calls", type=int, default=200, help="文档：200 次调用")
    ap.add_argument("--wall-hours", type=float, default=None,
                    help="墙钟预算（小时）；不给就按题目时长分档：1–2 分钟的题 4 小时，2–3 分钟的题 8 小时")
    ap.add_argument("--grace-min", type=float, default=15.0, help="到点后再给多少分钟让会话收尾，然后强杀")
    ap.add_argument("--effort", default=os.environ.get("STUDIO_EFFORT", "max"), help="CLAUDE_CODE_EFFORT_LEVEL")
    ap.add_argument("--mock", action="store_true", help="所有生成工具走 mock（结果标记为无效）")
    ap.add_argument("--allow-missing", action="store_true", help="核心后端（t2v/i2v/t2i）不可用也照跑")
    ap.add_argument("--python", default=os.environ.get("STUDIO_PYTHON") or sys.executable,
                    help="跑 MCP server 的解释器；真实后端要用装了 torch/diffusers/transformers 的环境")
    ap.add_argument("--cuda-compat", default=os.environ.get("STUDIO_CUDA_COMPAT", ""),
                    help="cuda-compat 目录；宿主驱动比 torch 的 CUDA 旧时必须给")
    ap.add_argument("--sp", default=str(PACKAGE / "sp.md"))
    ap.add_argument("--sp-mode", choices=("replace", "append"), default="replace")
    ap.add_argument("--claude", default=os.environ.get("STUDIO_CLAUDE") or shutil.which("claude") or "claude")
    ap.add_argument("--builtin-tools", default=os.environ.get("STUDIO_BUILTIN_TOOLS", DEFAULT_BUILTINS),
                    help='调试开关：临时给被测模型的内置工具，逗号分隔；默认 ""（v2 不给 shell）')
    ap.add_argument("--shell-net", action="store_true",
                    help="shell 保留出网代理（默认去掉：上网只走 search / fetch_url，它们带原片屏蔽表）")
    return ap


def _builtins(spec: str) -> List[str]:
    return [t.strip() for t in str(spec or "").replace(" ", ",").split(",") if t.strip()]


def _ensure_toolbox(name: str) -> Optional[str]:
    """把工具箱组件解到本机缓存（共享盘上是单个压缩包），返回目录；失败返回 None。"""
    tool = TOOLBOX / "bin" / "sb-toolbox"
    if not tool.is_file():
        return None
    try:
        proc = subprocess.run([str(tool), "ensure", name], capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        return None
    lines = proc.stdout.strip().splitlines()
    return lines[-1] if proc.returncode == 0 and lines else None


def _shell_env(env: Dict[str, str], shell_net: bool, py_dir: Optional[str] = None,
               bin_dir: Optional[str] = None) -> Dict[str, str]:
    """被测会话（以及它的 Bash）看到的环境：去掉密钥、harness 配置、（默认）出网代理，PATH 前面加工具箱。"""
    clean = {k: v for k, v in env.items()
             if not _is_secret(k) and not k.startswith(SHELL_DROP_PREFIXES)
             and (shell_net or (k not in PROXY_VARS and k.lower() not in ("no_proxy",)))}
    front = ([str(Path(py_dir) / "bin")] if py_dir else []) + ([bin_dir] if bin_dir else []) + [str(TOOLBOX / "bin")]
    clean["PATH"] = ":".join([p for p in front if Path(p).is_dir()] + [clean.get("PATH", "/usr/bin:/bin")])
    clean["STUDIO_TOOLBOX"] = str(TOOLBOX)
    return clean


DEFAULT_WALL_HOURS = 8.0     # 题目没写时长时


def wall_hours_for(task: Dict[str, Any]) -> float:
    """按题目时长分档定墙钟预算（看 duration_sec 下限）：1–2 分钟（< 120 秒）4 小时，2–3 分钟（≥ 120 秒）8 小时。"""
    dur = task.get("duration_sec") or []
    try:
        lo = float(dur[0])
    except (IndexError, TypeError, ValueError):
        return DEFAULT_WALL_HOURS
    if lo <= 0:
        return DEFAULT_WALL_HOURS
    return 4.0 if lo < 120 else 8.0


MODEL_BASES_FILE = Path(__file__).with_name("model_bases.json")


def model_base(model: str, default: str) -> str:
    """不走默认代理的模型：{模型名: ANTHROPIC_BASE_URL}。每道题启动时读一次，改文件不用重启 runner。"""
    try:
        bases = json.loads(MODEL_BASES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default
    return bases.get(model) or default


def run_task(args: argparse.Namespace) -> Dict[str, Any]:
    """跑一道题（或续跑一个被打断的运行目录），返回 result.json 的内容；没开跑就失败时返回 {"error", "code"}。"""
    prev: Optional[Dict[str, Any]] = None
    if getattr(args, "resume_run", None):
        out = Path(args.resume_run).resolve()
        prev = json.loads((out / "result.json").read_text(encoding="utf-8"))
        args.task = prev["task_file"]
        for key in ("model", "effort", "sp_mode", "base", "shell_net"):   # 续跑的配置必须和原来一致
            if prev.get(key) is not None:
                setattr(args, key, prev[key])
        if prev.get("builtin_tools") is not None:
            args.builtin_tools = ",".join(prev["builtin_tools"])
        args.max_calls, args.wall_hours = prev["budget"]["calls_max"], prev["budget"]["wall_hours"]
    if not args.task:
        return {"task": None, "error": "要给 --task 或 --resume-run", "code": 2}
    if prev is None:                         # 按模型改走专用网关（如经 nexus 接入的 Hy4），见 model_bases.json
        args.base = model_base(args.model, args.base)
    task_path = Path(args.task).resolve()
    task = taskspec.load(task_path)
    if args.wall_hours is None:                                      # 续跑时上面已从原 result.json 取回
        args.wall_hours = wall_hours_for(task)
    issues = taskspec.validate(task)
    for level, msg in issues:
        print("[题目%s] %s" % ("错误" if level == "error" else "提示", msg), file=sys.stderr)

    if prev is None:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        out = Path(args.out or Path(args.runs_dir) / ("%s__%s__%s%s" % (
            task["id"], _slug(args.model), stamp, "__mock" if args.mock else ""))).resolve()
    work = out / "work"
    if prev is None and (work / ".calls.jsonl").exists():
        print("运行目录已有调用记录，换一个 --out：%s" % out, file=sys.stderr)
        return {"task": task["id"], "error": "运行目录已存在：%s" % out, "code": 2}
    work.mkdir(parents=True, exist_ok=True)

    # ---- 后端检查：先确认真的能出片，避免又跑出一批没有真实画面的 run
    backend_env = _backend_env(args)
    if args.cuda_compat:
        backend_env["LD_LIBRARY_PATH"] = args.cuda_compat + ":" + os.environ.get("LD_LIBRARY_PATH", "")
    status = _preflight(dict(backend_env, _PYTHON=args.python))
    missing_core = [t for t in CORE_TOOLS if not (status.get(t) or {}).get("ok")]
    for tool, st in status.items():
        if not st.get("ok"):
            print("[后端不可用] %-12s %s" % (tool, st.get("detail")), file=sys.stderr)
    if missing_core and not args.allow_missing:
        print("核心工具不可用：%s。先按 RUNBOOK_GPU.md 起服务 / 配后端，或加 --mock / --allow-missing。"
              % ", ".join(missing_core), file=sys.stderr)
        if prev is None:
            shutil.rmtree(out, ignore_errors=True)
        return {"task": task["id"], "error": "核心工具不可用：%s" % ", ".join(missing_core), "code": 3,
                "backends_at_start": status}

    sp_file = out / "sp.md"
    if prev is None:
        mapped = taskspec.copy_inputs(task, work)
        brief = taskspec.user_message(task, mapped)
        (out / "prompt.txt").write_text(brief, encoding="utf-8")
        sp_text = Path(args.sp).read_text(encoding="utf-8")
        sp_text = sp_text.replace("{max_calls}", str(args.max_calls)).replace("{wall_hours}", "%g" % args.wall_hours)
        sp_file.write_text(sp_text, encoding="utf-8")
    else:                                                            # 续跑：素材、题目消息、系统提示都沿用原来的
        brief = (out / "prompt.txt").read_text(encoding="utf-8")

    started = time.time()
    # AI Hub 2.0 显式缓存：同一次运行（含续跑）的所有请求带同一个 cache_task_id，不同运行不同；32 位 md5。
    # 经 Claude Code 的 ANTHROPIC_CUSTOM_HEADERS 发 X-Cache-Task-Id，由 AIHub2claude 代理并进 Authorization。
    cache_task_id = (prev or {}).get("cache_task_id") or hashlib.md5(("%s:%f" % (out, started)).encode()).hexdigest()
    spent = float(prev["seconds"]) if prev else 0.0                 # 续跑：墙钟只给剩下的
    remaining = args.wall_hours * 3600 - spent
    if remaining < 300:
        return {"task": task["id"], "error": "墙钟预算已经用完，不能续跑：%s" % out, "code": 2}
    deadline = started + remaining
    private = Path(tempfile.mkdtemp(prefix="studio_run_"))          # 本机私有：密钥、全新 HOME
    os.chmod(private, 0o700)
    secrets = {k: v for k, v in backend_env.items() if _is_secret(k)}
    secrets_file = private / "secrets.env"
    secrets_file.write_text("".join("%s=%s\n" % kv for kv in secrets.items()), encoding="utf-8")
    os.chmod(secrets_file, 0o600)
    fresh_home = private / "home"
    fresh_home.mkdir()
    saved = sorted((out / "session").glob("*.jsonl"), key=lambda p: p.stat().st_mtime) if prev is not None else []
    if prev is not None:
        transcript_events = _read_jsonl(out / "transcript.jsonl")
        sid = saved[-1].stem if saved else _session_from_transcript(transcript_events, brief, work, "")[0]
        shutil.copy2(out / "result.json", out / ("result.attempt%d.json" % (int(prev.get("resumes") or 0) + 1)))
    else:
        sid = None

    server_env = {k: v for k, v in backend_env.items() if not _is_secret(k)}
    server_env.update({
        "STUDIO_WORKDIR": str(work), "STUDIO_RUN_DIR": str(out),
        "STUDIO_MAX_CALLS": str(args.max_calls), "STUDIO_DEADLINE": "%.0f" % deadline,
        "STUDIO_FFMPEG": media.FFMPEG, "STUDIO_SECRETS_FILE": str(secrets_file),
        "HOME": os.environ.get("HOME", "/root"),                    # 模型缓存在真实 HOME 下
        "PATH": os.environ.get("PATH", ""),
        "PYTHONUNBUFFERED": "1",
    })
    server_env["PYTHONPATH"] = str(REPO)
    # v2 的被测会话环境去掉了出网代理（shell 不该直接上网），MCP server 照旧要：显式写进它自己的环境
    server_env.update({k: os.environ[k] for k in PROXY_VARS + ("no_proxy", "NO_PROXY") if os.environ.get(k)})
    server_env["STUDIO_TOOLBOX"] = str(TOOLBOX)
    # 检索屏蔽表（公平性）：题目目录里的 search_block.json（build_*_tasks 生成：原片链接、片名、视频站域名），
    # search 和 fetch_url 都按它过滤，挡住原片页面。环境里显式给了就用给的；题目目录没有就明确告警。
    block = task_path.parent / "search_block.json"
    if not server_env.get("STUDIO_SEARCH_BLOCK_FILE"):
        if block.is_file():
            server_env["STUDIO_SEARCH_BLOCK_FILE"] = str(block)
        else:
            print("[警告] 题目目录没有 search_block.json，本次运行的检索不屏蔽原片：%s" % task_path.parent, file=sys.stderr)
    mcp_cfg = {"mcpServers": {"studio": {"command": args.python,
                                         "args": ["-m", "studiobench.tools.server"], "env": server_env}}}
    (out / "mcp.json").write_text(json.dumps(mcp_cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    settings = {"env": {"CLAUDE_CODE_EFFORT_LEVEL": args.effort, "CLAUDE_CODE_AUTO_MODE_SERVER": "0"}}
    if "Bash" in _builtins(args.builtin_tools):        # shell 里直接跑 blender / ffmpeg 常常超过默认的 2 分钟
        settings["env"].update({"BASH_DEFAULT_TIMEOUT_MS": "600000", "BASH_MAX_TIMEOUT_MS": "3600000"})
    (out / "settings.json").write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")

    # 会话照常存盘（在临时 HOME 里，跑完拷进运行目录 session/），被平台故障打断时可以 --resume-run 续跑
    builtins = _builtins(args.builtin_tools)
    cmd = [args.claude, "--print", RESUME_PROMPT if prev else brief, *(["--resume", sid] if prev else []),
           "--model", args.model,
           "--system-prompt-file" if args.sp_mode == "replace" else "--append-system-prompt-file", str(sp_file),
           "--tools", ",".join(builtins), "--allowedTools", ",".join(["mcp__studio"] + builtins),
           *(["--add-dir", str(TOOLBOX)] if builtins and TOOLBOX.is_dir() else []),
           "--strict-mcp-config", "--mcp-config", str(out / "mcp.json"),
           "--setting-sources", "project", "--settings", str(out / "settings.json"),
           "--permission-mode", "acceptEdits", "--permission-prompts", "none",
           "--output-format", "stream-json", "--verbose"]

    token = os.environ.get("STUDIO_TOKEN", "local-proxy-token")
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDECODE", "CLAUDE_SESSION_ID", "CLAUDE_CODE_ENTRYPOINT",
                        "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
    py_dir = _ensure_toolbox("py") if builtins else None
    bin_dir = _ensure_toolbox("bin") if builtins else None      # 本机缓存里的 ffmpeg / ffprobe
    if builtins:                               # 有 shell 时，它继承的就是这份环境
        env = _shell_env(env, args.shell_net, py_dir, bin_dir)
    env.update({"HOME": str(fresh_home), "ANTHROPIC_BASE_URL": args.base,
                "ANTHROPIC_API_KEY" if args.auth == "api-key" else "ANTHROPIC_AUTH_TOKEN": token,
                "DISABLE_AUTOUPDATER": "1",
                "ANTHROPIC_CUSTOM_HEADERS": "\n".join(h for h in (os.environ.get("ANTHROPIC_CUSTOM_HEADERS", ""),
                                                                 "X-Cache-Task-Id: " + cache_task_id) if h),
                "MCP_TIMEOUT": "120000",                       # server 启动（含 H3 探活）
                "MCP_TOOL_TIMEOUT": str(int((args.wall_hours * 3600 + 600) * 1000)),   # H3 单卡一段约 30 分钟
                # 流空闲看门狗默认约 5 分钟没收到字节就掐断重试。mimo 推理期间流里不发任何事件（实测一轮静默 505 秒），
                # 会被反复掐断、无限重试，整场零输出。放宽到 30 分钟；真挂死的连接仍有 API 超时兜底
                "CLAUDE_STREAM_IDLE_TIMEOUT_MS": os.environ.get("CLAUDE_STREAM_IDLE_TIMEOUT_MS", "1800000"),
                "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": os.environ.get("CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS", "1800000"),
                "API_TIMEOUT_MS": os.environ.get("API_TIMEOUT_MS", "2400000")})

    if prev is not None:                       # 续跑：把会话放回 Claude Code 找它的位置，优先用存下来的原件
        proj = fresh_home / ".claude" / "projects" / _encode_cwd(work)
        proj.mkdir(parents=True)
        if saved:
            shutil.copy2(saved[-1], proj / saved[-1].name)
        else:                                  # 老版本没存会话文件：从 transcript 还原
            version = subprocess.run([args.claude, "--version"], capture_output=True, text=True).stdout.split()[0]
            sid, rows = _session_from_transcript(transcript_events, brief, work, version)
            att = _session_attachments([args.claude, "--print", brief, *cmd[cmd.index("--model"):]], work, env)
            for a in att:                      # 会话开头的环境信息（工作目录、平台、模型、日期…）接在题目消息后面
                a["sessionId"] = sid
            rows = rows[:1] + att + rows[1:]
            for i in range(1, len(rows)):
                rows[i]["parentUuid"] = rows[i - 1]["uuid"]
            (proj / ("%s.jsonl" % sid)).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                                   encoding="utf-8")

    timed_out = False
    rc = None
    print("%s %s → %s" % ("续跑" if prev else "开跑", task["id"], out), file=sys.stderr)
    mode = "a" if prev else "w"                                       # 续跑：transcript 接在原来的后面
    with (out / "transcript.jsonl").open(mode, encoding="utf-8") as log, \
            (out / "claude.stderr.log").open(mode, encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, cwd=str(work), env=env, stdout=log, stderr=err,
                                stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = proc.wait(timeout=remaining + args.grace_min * 60)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                rc = proc.wait()
    # 收尾兜一次：claude 被 killpg 时 MCP server 可能来不及清它起的子进程组，
    # 正在渲的 blender / chromium 会接着跑（最长 60 分钟），占着 GPU 进下一题
    reaped = procgroups.kill_recorded(out / procgroups.NAME)
    if reaped:
        print("收掉 %d 个还在跑的渲染进程组：%s" % (len(reaped), reaped), file=sys.stderr)
    _save_session(fresh_home, out)
    shutil.rmtree(private, ignore_errors=True)
    seconds = round(time.time() - started + spent, 1)                # 续跑：墙钟是几段之和

    # ---- 汇总
    summary = _transcript_summary(_read_jsonl(out / "transcript.jsonl"))
    (out / "script.md").write_text("\n\n---\n\n".join(summary["texts"]) + "\n", encoding="utf-8")
    stats = _call_stats(_read_jsonl(work / ".calls.jsonl"))
    final_src = work / "final.mp4"
    video = None
    if final_src.is_file() and final_src.stat().st_size > 0:
        shutil.copy2(final_src, out / "final.mp4")
        video = media.probe(out / "final.mp4")

    invalid: List[str] = []
    if args.mock or stats["mock_tools_used"]:
        invalid.append("用了 mock 后端：%s" % (", ".join(stats["mock_tools_used"]) or "全部"))
    unexpected = [t for t in summary["isolation"]["non_studio_tools"] if t not in builtins]
    if unexpected:
        invalid.append("被测会话还能用到计划外的工具：%s" % unexpected)
    trail = audit.audit(_read_jsonl(out / "transcript.jsonl"), work, TOOLBOX)
    if trail["flag_count"]:
        where = "render_blender 的脚本" if trail.get("code_scans") and not trail["builtin_calls"] else "工具调用"
        invalid.append("审计在%s里标了 %d 条（见 result.json audit.flags）" % (where, trail["flag_count"]))
    unavailable = sorted(t for t, st in status.items() if not st.get("ok"))
    if unavailable and not args.mock:
        invalid.append("开跑时这些工具不可用：%s" % ", ".join(unavailable))

    result = {
        "task": task["id"], "task_file": str(task_path), "model": args.model, "base": args.base,
        "effort": args.effort, "sp_mode": args.sp_mode, "mock": args.mock,
        "toolset": TOOLSET, "builtin_tools": builtins, "shell_net": bool(args.shell_net), "toolbox_python": py_dir,
        "audit": trail, "reaped_renderers": reaped,
        "submitted": video is not None, "final": str(out / "final.mp4") if video else None,
        "video": video, "returncode": rc, "timed_out": timed_out, "seconds": seconds,
        "budget": {"calls_max": args.max_calls, "wall_hours": args.wall_hours},
        "cache_task_id": cache_task_id,
        **stats,
        "valid_for_bench": not invalid, "invalid_reasons": invalid,
        "task_issues": [{"level": lv, "msg": m} for lv, m in issues],
        "backends_at_start": status,
        "isolation": summary["isolation"], "llm": summary["llm"],
        "config": {k: v for k, v in server_env.items()
                   if k not in ("PATH", "STUDIO_SECRETS_FILE") and not _is_secret(k)},
        "secrets_passed": sorted(secrets),
        "workdir": str(work),
        "resumes": (int(prev.get("resumes") or 0) + 1) if prev else 0,   # 因平台故障续跑过几次
        "interrupted_by_api": interrupted_by_api(out),                   # 这次是不是又被模型 API 故障打断
    }
    result["run_dir"] = str(out)
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main(argv: Optional[List[str]] = None) -> int:
    res = run_task(build_parser().parse_args(argv))
    if "error" in res:
        return int(res["code"])
    print(json.dumps({k: res[k] for k in ("task", "model", "submitted", "valid_for_bench", "invalid_reasons",
                                          "tool_calls", "tool_errors", "rejected_calls", "seconds", "timed_out",
                                          "run_dir")}, ensure_ascii=False, indent=2))
    return 0 if res["submitted"] else 1


if __name__ == "__main__":
    sys.exit(main())
