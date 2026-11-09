# -*- coding: utf-8 -*-
"""留痕审计：跑完后从 transcript 里核对被测模型碰过哪些路径、写过什么代码。

· 内置工具（Bash / Read / Write …，--builtin-tools 给了才有）：扫命令和路径参数
· render_blender：它是唯一能跑任意代码的地方。脚本本身由 workers/blender_entry.py 里的
  sys.addaudithook 拦（subprocess / socket / 越界读写），这里再扫一遍 code 文本做兜底——
  钩子拦不住的（比如 C 侧的文件访问）至少能留个标记
· 其余 mcp__ 原子工具不扫：参数都是工作目录里的相对路径，工具侧已经查了越界

碰了工作目录和工具箱以外的评测数据，或者脚本里有越界意图，就记进 flags，run.py 据此判 valid_for_bench=false。
单层的 `..`（回到自己的运行目录）只计数不判；`../..`（够到同题别的模型）和敏感绝对路径才判。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List

BUILTIN_PATH_KEYS = ("file_path", "path", "notebook_path")
SENSITIVE_ROOTS = tuple(p for p in os.environ.get(
    "STUDIO_SENSITIVE_ROOTS", "/root/,/home/,/data/").split(",") if p)
_ABS = re.compile(r"(?<![\w.])((?:%s)(?:/[^\s'\"`;|&<>()]*)?)" % "|".join(
    re.escape(p) for p in SENSITIVE_ROOTS))
_UP2 = re.compile(r"\.\./\.\.")
_UP1 = re.compile(r"(?:^|[\s'\"=/(])\.\.(?:/|\s|$|['\")])")
_NET = re.compile(r"\b(curl|wget|pip3?\s+install|uv\s+pip|npm\s+(?:i|install)|git\s+clone|apt(?:-get)?|yum)\b")
CODE_TOOLS = ("render_blender",)                     # 会把模型写的代码直接 exec 的工具
_CODE_DENY = re.compile(r"final\.mp4|\.calls\.jsonl")
_CODE_ESCAPE = re.compile(r"\b(subprocess\.|os\.system\s*\(|os\.popen\s*\(|os\.exec[a-z]*\s*\(|"
                          r"os\.posix_spawn\s*\(|os\.fork\s*\(|socket\.socket\s*\(|"
                          r"socket\.create_connection\s*\(|multiprocessing\.|pty\.spawn\s*\()")


def _tool_uses(events: Iterable[Dict[str, Any]]) -> Iterable[Dict[str, Any]]:
    for e in events:
        if e.get("type") != "assistant":
            continue
        for block in (e.get("message") or {}).get("content") or []:
            if block.get("type") == "tool_use":
                yield block


def _allowed(path: str, allow: List[str]) -> bool:
    # 先规范化：`/a/b/../c` 这类不还原成 `/a/c` 的话，前缀比对会被 `..` 骗过去
    p = os.path.normpath(path.rstrip("/"))
    return any(p == a or p.startswith(a + "/") for a in allow)


def _flag(flags: List[Dict[str, str]], tool: str, reason: str, snippet: str, path: str = "") -> None:
    flags.append({"tool": tool, "reason": reason, "path": path[:300], "snippet": snippet[:300]})


def _scan_code(tool: str, code: str, allow: List[str], flags: List[Dict[str, str]]) -> None:
    """扫模型写给 render_blender 的脚本：越界路径、碰交卷文件、想开子进程/网络。"""
    for m in _ABS.finditer(code):
        path = m.group(1)
        if path.startswith(SENSITIVE_ROOTS) and not _allowed(path, allow):
            _flag(flags, tool, "脚本里写了工作目录以外的绝对路径", code, path)
    if _UP2.search(code):
        _flag(flags, tool, "脚本里用 ../.. 跳出了自己的运行目录", code)
    if _CODE_DENY.search(code):
        _flag(flags, tool, "脚本里碰了交卷视频或调用记录（final.mp4 / .calls.jsonl）", code)
    esc = _CODE_ESCAPE.search(code)
    if esc:
        _flag(flags, tool, "脚本里想%s" % esc.group(1), code)


def audit(events: List[Dict[str, Any]], work: Path, toolbox: Path) -> Dict[str, Any]:
    allow = [os.path.normpath(p) for p in
             (str(Path(work).resolve()), str(Path(toolbox).resolve()), str(toolbox))]
    counts: Dict[str, int] = {}
    flags: List[Dict[str, str]] = []
    up1 = 0
    net: List[str] = []
    code_scans = 0
    for block in _tool_uses(events):
        name = str(block.get("name") or "")
        inp = block.get("input") or {}
        if name.startswith("mcp__"):
            # 原子工具本身不扫，只有会 exec 模型代码的例外
            if name.rsplit("__", 1)[-1] in CODE_TOOLS and isinstance(inp.get("code"), str):
                code_scans += 1
                _scan_code(name, str(inp["code"]), allow, flags)
            continue
        counts[name] = counts.get(name, 0) + 1
        inp = block.get("input") or {}
        texts: List[str] = []
        if name == "Bash":
            texts.append(str(inp.get("command") or ""))
        for key in BUILTIN_PATH_KEYS:
            if inp.get(key):
                texts.append(str(inp[key]))
        if name == "Grep" and inp.get("glob"):
            texts.append(str(inp["glob"]))
        for text in texts:
            for m in _ABS.finditer(text):
                path = m.group(1)
                if path.startswith(SENSITIVE_ROOTS) and not _allowed(path, allow):
                    _flag(flags, name, "访问工作目录以外的路径", text, path)
            if _UP2.search(text):
                _flag(flags, name, "用 ../.. 跳出了自己的运行目录", text)
            elif _UP1.search(text):
                up1 += 1
            if name == "Bash" and _NET.search(text):
                net.append(text[:200])
    return {"builtin_calls": counts, "flags": flags[:50], "flag_count": len(flags),
            "parent_dir_refs": up1, "network_like_commands": net[:20], "code_scans": code_scans}
