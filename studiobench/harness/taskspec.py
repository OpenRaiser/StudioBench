# -*- coding: utf-8 -*-
"""题目（框架文档 §1 的标注格式）：校验、拷素材、拼用户消息。

用户消息只放 期望 / 素材 / 参考 / 给定音频（文档 §2），外加时长与画幅这两条硬格式
（成片分里「时长不在区间里算一条失败」，不告诉模型就没法做对）。must / forbid /
plot_beats / facts / constraints 绝不进用户消息。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Tuple

DOMAINS = ("narrative", "commercial", "product", "education", "music", "documentary", "creator", "dialogue")
TASKS = ("open", "constraint", "assets", "reference", "audio-led")
AUDIO = ("speech", "music", "sfx", "dialogue")
RUBRIC_KEYS = ("must", "forbid", "constraints", "plot_beats", "facts")
DOC_DURATION = (60, 180)                      # 文档：每题 1–3 分钟，题目可再收窄区间


def load(path: Path) -> Dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate(task: Dict[str, Any]) -> List[Tuple[str, str]]:
    """返回 [(级别, 说明)]。error：这道题不能按文档跑/评；warn：能跑，但评分会缺项。"""
    issues: List[Tuple[str, str]] = []
    for key in ("id", "domain", "task", "wish", "duration_sec", "aspect"):
        if not task.get(key):
            issues.append(("error", "缺字段 %s" % key))
    if task.get("domain") and task["domain"] not in DOMAINS:
        issues.append(("error", "domain 不在 8 个片种里：%s" % task["domain"]))
    if task.get("task") and task["task"] not in TASKS:
        issues.append(("error", "task 不在 5 种任务里：%s" % task["task"]))
    bad_audio = [a for a in task.get("audio_profile") or [] if a not in AUDIO]
    if bad_audio or not task.get("audio_profile"):
        issues.append(("warn", "audio_profile 应为 speech/music/sfx/dialogue 的非空子集：%s"
                       % (task.get("audio_profile"),)))
    dur = task.get("duration_sec") or []
    if len(dur) == 2:
        if not (DOC_DURATION[0] <= dur[0] <= dur[1] <= DOC_DURATION[1]):
            issues.append(("error" if not task.get("smoke") else "warn",
                           "时长区间 %s 不在文档要求的 60–180 秒内" % (dur,)))
    if all(not task.get(k) for k in ("must", "plot_beats", "facts", "constraints")):
        issues.append(("warn", "must / plot_beats / facts / constraints 全空，成片分（命中条数/应判条数）只剩时长一条"))
    if task.get("task") == "assets" and not task.get("assets"):
        issues.append(("error", "Asset-conditioned 题没有 assets"))
    if task.get("task") == "reference" and not task.get("references"):
        issues.append(("error", "Reference-conditioned 题没有 references"))
    if task.get("task") == "audio-led" and not task.get("given_audio") and "自己出音乐" not in task.get("wish", ""):
        issues.append(("warn", "Audio-led 题没有 given_audio"))
    for key in ("assets", "references"):
        for item in task.get(key) or []:
            if not Path(item).is_file():
                issues.append(("error", "%s 文件不存在：%s" % (key, item)))
    if task.get("given_audio") and not Path(str(task["given_audio"])).is_file():
        issues.append(("error", "given_audio 不存在：%s" % task["given_audio"]))
    return issues


def copy_inputs(task: Dict[str, Any], work: Path) -> Dict[str, List[str]]:
    """素材 / 参考 / 给定音频拷进 work/inputs/，返回模型可见的相对路径。重名时加序号。"""
    inputs = work / "inputs"
    inputs.mkdir(parents=True, exist_ok=True)
    seen: Dict[str, int] = {}
    mapped: Dict[str, List[str]] = {"assets": [], "references": [], "given_audio": []}
    groups = [("assets", task.get("assets") or []), ("references", task.get("references") or []),
              ("given_audio", [task["given_audio"]] if task.get("given_audio") else [])]
    for key, items in groups:
        for item in items:
            src = Path(str(item))
            if not src.is_file():
                continue
            name = src.name
            if name in seen:
                seen[name] += 1
                name = "%s_%d%s" % (src.stem, seen[src.name], src.suffix)
            else:
                seen[name] = 0
            shutil.copy2(src, inputs / name)
            mapped[key].append("inputs/" + name)
    return mapped


def user_message(task: Dict[str, Any], mapped: Dict[str, List[str]]) -> str:
    lines = ["期望：%s" % task["wish"].strip()]
    dur = task.get("duration_sec")
    if dur:
        lines.append("时长：%s–%s 秒" % (dur[0], dur[1]))
    if task.get("aspect"):
        lines.append("画幅：%s" % task["aspect"])
    if mapped.get("assets"):
        lines.append("必须使用的素材：" + "、".join(mapped["assets"]))
    if mapped.get("references"):
        lines.append("参考：" + "、".join(mapped["references"]))
    if mapped.get("given_audio"):
        lines.append("给定音频：" + "、".join(mapped["given_audio"]))
    return "\n".join(lines) + "\n"
