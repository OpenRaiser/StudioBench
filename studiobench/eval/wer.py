# -*- coding: utf-8 -*-
"""§4.4 人声可懂：台词对 ASR 文本，1 - WER，下限 0。中文按字算（CER），其它语言按词算。

参考台词优先用题目标注（task["lines"]），没有就用成片血缘里混进去的 tts 文本（按时间顺序）。
"""
from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, Dict, List

from ..tools.backends import whisper

_CJK = re.compile(r"[㐀-鿿]")


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if not unicodedata.category(ch).startswith(("P", "S", "Z")) and not ch.isspace())


def _tokens(text: str) -> List[str]:
    if _CJK.search(text):
        return list(_normalize(text))
    return [w for w in re.split(r"\s+", unicodedata.normalize("NFKC", text).lower()) if _normalize(w)]


def _edit_distance(a: List[str], b: List[str]) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, y in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y))
        prev = cur
    return prev[-1]


def score(reference: str, hypothesis: str) -> Dict[str, Any]:
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    if not ref:
        return {"value": None, "reason": "参考台词为空"}
    err = _edit_distance(ref, hyp) / float(len(ref))
    return {"value": round(max(0.0, 1.0 - err), 4), "method": "cer" if _CJK.search(reference) else "wer",
            "error_rate": round(err, 4)}


def intelligibility(final: Path, task: Dict[str, Any], tts_texts: List[str]) -> Dict[str, Any]:
    lines = task.get("lines") or tts_texts
    source = "task.lines" if task.get("lines") else "tts 文本"
    if not lines:
        return {"value": None, "reason": "题目没标注台词，成片里也没有混进 tts 旁白/对白"}
    ok, why = whisper.available()
    if not ok:
        return {"value": None, "reason": "ASR 不可用：%s" % why}
    hyp = whisper.transcribe(final)
    res = score("".join(lines) if _CJK.search("".join(lines)) else " ".join(lines), hyp["text"])
    res.update({"reference_source": source, "hypothesis": hyp["text"][:2000], "asr": hyp["backend"],
                "chunks": hyp["chunks"]})
    return res
