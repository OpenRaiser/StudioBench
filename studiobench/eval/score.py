# -*- coding: utf-8 -*-
"""§4.4 打分规则。四项主分：成片分、画面分、声音分、声画分。

· 没 export：四项主分记 0
· 成片分 = 命中条数 / 应判条数；forbid 命中一条则为 0；时长不在区间里算一条失败。
  有标注题没判出来（VLM 没配或失败）时不出分，只给部分结果，不拿别的数顶
· 画面分 = VBench 六项里算成功的项平均
· 声音分、声画分只平均该题 audio_profile 里适用、并且真的算出了数的项；
  讲解、纪录、教育如果「人声没被盖住」为 0，声音分记 0
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# 指标 → (归入哪项主分, 适用的 audio_profile 标签)
AUDIO_METRICS = {
    "intelligibility": ("audio", {"speech", "dialogue"}),     # 人声可懂
    "vocal_unmasked": ("audio", {"speech", "dialogue"}),      # 人声没被盖住
    "lip_sync": ("av", {"dialogue"}),                         # 口型
    "sfx_sync": ("av", {"sfx"}),                              # 音效同步
    "beat_sync": ("av", {"music"}),                           # 卡点
}
SPEECH_LED_DOMAINS = {"education", "documentary"}             # 讲解、纪录、教育


def _mean(values: List[Optional[float]]) -> Optional[float]:
    vals = [v for v in values if v is not None]
    return round(sum(vals) / len(vals), 4) if vals else None


def film_score(items: List[Dict[str, Any]], duration_ok: Optional[bool]) -> Dict[str, Any]:
    scored = [it for it in items if it.get("scored")]
    forbid = [it for it in scored if it["kind"] == "forbid"]
    positive = [it for it in scored if it["kind"] != "forbid"]
    forbid_hit = any(it.get("answer") == "yes" for it in forbid)
    judged = [it for it in positive if it.get("answer") in ("yes", "no")]
    hits = sum(1 for it in judged if it["answer"] == "yes")
    total = len(positive) + (1 if duration_ok is not None else 0)
    hits += 1 if duration_ok else 0
    complete = len(judged) == len(positive) and all(it.get("answer") in ("yes", "no") for it in forbid)
    if forbid_hit:
        value: Optional[float] = 0.0
    elif complete and total:
        value = round(hits / float(total), 4)
    else:
        value = None
    return {"value": value, "hits": hits, "total": total, "judged_items": len(judged) + (1 if duration_ok is not None else 0),
            "forbid_hit": forbid_hit, "complete": complete}


def audio_scores(task: Dict[str, Any], metrics: Dict[str, Optional[float]]) -> Dict[str, Any]:
    profile = set(task.get("audio_profile") or [])
    groups: Dict[str, List[Optional[float]]] = {"audio": [], "av": []}
    used: Dict[str, List[str]] = {"audio": [], "av": []}
    for name, (group, labels) in AUDIO_METRICS.items():
        if profile & labels and metrics.get(name) is not None:
            groups[group].append(metrics[name])
            used[group].append(name)
    audio = _mean(groups["audio"])
    if task.get("domain") in SPEECH_LED_DOMAINS and metrics.get("vocal_unmasked") == 0:
        audio = 0.0
    return {"audio": audio, "av": _mean(groups["av"]), "audio_metrics_used": used["audio"],
            "av_metrics_used": used["av"]}


def main_scores(submitted: bool, film: Dict[str, Any], vbench: Dict[str, Dict[str, Any]],
                audio: Dict[str, Any]) -> Dict[str, Optional[float]]:
    if not submitted:
        return {"film": 0.0, "visual": 0.0, "audio": 0.0, "av": 0.0}
    return {"film": film["value"], "visual": _mean([v.get("value") for v in vbench.values()]),
            "audio": audio["audio"], "av": audio["av"]}
