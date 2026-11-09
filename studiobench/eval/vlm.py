# -*- coding: utf-8 -*-
"""§4.5 VLM 是非题：按时间抽帧，一条标注一句是非，要求指出时间段。VLM 的分单独列。

接 OpenAI 兼容的 /chat/completions（图片走 data URL）：
STUDIO_VLM_BASE_URL  例如 http://host:port/v1
STUDIO_VLM_MODEL     评审模型名（别用被测模型自己当评审）
STUDIO_VLM_API_KEY   可选
没配置时所有题都不判（value=None），成片分也就算不出来，不会拿别的数顶。
"""
from __future__ import annotations

import base64
import json
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..tools import media
from ..tools.backends.common import env, env_int

JUDGE_SP = ("你是严格、客观的视频评审。给你一支成片按时间顺序抽取的画面（每格左上角是该帧在成片里的时间，单位秒），"
            "有时还附旁白/对白的转写、分镜、素材图。只根据看到和读到的内容回答一个是非题。\n"
            "只输出 JSON：{\"answer\": \"yes\" 或 \"no\", \"start\": 秒, \"end\": 秒, \"reason\": \"一句话\"}。"
            "answer 为 yes 时 start/end 指出依据所在的时间段；看不出来、不确定就答 no。")


def configured() -> bool:
    return bool(env("STUDIO_VLM_BASE_URL") and env("STUDIO_VLM_MODEL"))


def frame_sheets(video: Path, out_dir: Path, every: float = 2.0, per_sheet: int = 16) -> List[Path]:
    frames = media.extract_frames(video, out_dir / "frames", fps=1.0 / every)
    sheets = []
    for k in range(0, len(frames), per_sheet):
        group = frames[k:k + per_sheet]
        sheets.append(media.contact_sheet([f["file"] for f in group], ["%.0fs" % f["t"] for f in group],
                                          out_dir / ("sheet_%02d.jpg" % (k // per_sheet))))
    return sheets


def _data_url(path: Path) -> str:
    if media.is_image(path) and path.suffix.lower() not in (".jpg", ".jpeg"):
        with tempfile.TemporaryDirectory() as tmp:
            path = media.downscale_image(path, Path(tmp) / "x.jpg", max_side=1024)
            data = path.read_bytes()
    else:
        data = path.read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def ask(question: str, sheets: List[Path], context: str = "",
        extra_images: Optional[List[Path]] = None) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = [{"type": "text", "text": "是非题：%s\n%s" % (question, context)}]
    for p in list(extra_images or []) + sheets:
        content.append({"type": "image_url", "image_url": {"url": _data_url(p)}})
    body = {"model": env("STUDIO_VLM_MODEL"), "temperature": 0,
            "messages": [{"role": "system", "content": JUDGE_SP}, {"role": "user", "content": content}]}
    headers = {"Content-Type": "application/json"}
    if env("STUDIO_VLM_API_KEY"):
        headers["Authorization"] = "Bearer %s" % env("STUDIO_VLM_API_KEY")
    req = urllib.request.Request(env("STUDIO_VLM_BASE_URL").rstrip("/") + "/chat/completions",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=env_int("STUDIO_VLM_TIMEOUT", 300)) as resp:
        text = json.loads(resp.read())["choices"][0]["message"]["content"]
    found = re.search(r"\{.*\}", text, re.S)
    try:
        parsed = json.loads(found.group(0)) if found else {}
    except ValueError:
        parsed = {}
    answer = str(parsed.get("answer", "")).lower()
    return {"answer": answer if answer in ("yes", "no") else None, "start": parsed.get("start"),
            "end": parsed.get("end"), "reason": parsed.get("reason"), "raw": text[:500]}


def build_items(task: Dict[str, Any], storyboard: str, has_speech: bool) -> List[Dict[str, Any]]:
    """标注题（进成片分）+ 附加题（单列，不进成片分）。"""
    items: List[Dict[str, Any]] = []
    for kind in ("must", "plot_beats", "facts", "constraints"):
        for text in task.get(kind) or []:
            items.append({"kind": kind, "text": text, "question": "成片是否满足：%s" % text, "scored": True})
    for text in task.get("forbid") or []:
        items.append({"kind": "forbid", "text": text, "question": "成片里是否出现了：%s" % text, "scored": True})
    beats = task.get("plot_beats") or []
    if len(beats) >= 2:
        items.append({"kind": "plot_order", "text": " → ".join(beats),
                      "question": "这些情节是否按这个顺序出现：%s" % " → ".join(beats), "scored": False})
    if has_speech:
        items.append({"kind": "narration_matches", "text": "旁白在讲画面",
                      "question": "旁白/对白讲的内容是否与同一时间段的画面对得上", "scored": False})
    if storyboard.strip():
        items.append({"kind": "camera_follows_storyboard", "text": "运镜按分镜",
                      "question": "成片是否做成了分镜里写的景别和镜头运动（只看景别和运动）", "scored": False})
    if task.get("assets"):
        items.append({"kind": "asset_fidelity", "text": "素材保真",
                      "question": "附上的素材图（最前面几张）是否保真地出现在成片里，没有变形、错字、换样", "scored": False})
    if task.get("references"):
        items.append({"kind": "reference_not_copied", "text": "参考不是复制",
                      "question": "成片是否学了参考图（最前面几张）的风格，同时不是直接复制参考画面", "scored": False})
    return items


def judge(task: Dict[str, Any], final: Path, out_dir: Path, transcript: str = "",
          storyboard: str = "", assets: Optional[List[Path]] = None) -> Dict[str, Any]:
    items = build_items(task, storyboard, bool(transcript))
    if not configured():
        for it in items:
            it.update({"answer": None, "reason": "未配置 STUDIO_VLM_BASE_URL / STUDIO_VLM_MODEL"})
        return {"configured": False, "items": items}
    sheets = frame_sheets(final, out_dir)
    for it in items:
        ctx = []
        if transcript and it["kind"] in ("facts", "narration_matches", "must", "plot_beats"):
            ctx.append("旁白/对白转写（带时间）：\n" + transcript[:4000])
        if it["kind"] == "camera_follows_storyboard":
            ctx.append("分镜（模型自己写的）：\n" + storyboard[:4000])
        extra = (assets or []) if it["kind"] in ("asset_fidelity", "reference_not_copied") else []
        try:
            it.update(ask(it["question"], sheets, "\n\n".join(ctx), extra_images=extra))
        except Exception as exc:  # noqa: BLE001
            it.update({"answer": None, "reason": "VLM 调用失败：%s" % exc})
    return {"configured": True, "model": env("STUDIO_VLM_MODEL"), "sheets": [p.name for p in sheets], "items": items}
