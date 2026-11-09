# -*- coding: utf-8 -*-
"""MiniMax-H3（vllm-omni 部署）：t2v / i2v / ref_video。

请求走 `POST /v1/videos`（multipart）→ 轮询 `/v1/videos/{id}` → `/content` 下载 → DELETE。
任务类型在 `extra_params.task` 里**显式**给出，对应框架文档的 task=t2va|fl2va|ref2va：
vllm-omni 不给 task 时按输入推断（有图→fl2va），只给参考图的 ref2va 会被误判成 fl2va。

· fl2va 服务（H3_ENDPOINT）同时承担 t2va 和 fl2va；首尾帧用 `frame_indices`，只接受 [0]、[-1]、[0,-1]
· ref2va 服务（H3_REF2VA_ENDPOINT）是另一份权重（MiniMax-H3/Ref2VA），需要单独起服务
· 时长 4–15 秒整数，服务端按帧数对齐（5 → 5.17s）；短边固定 768
"""
from __future__ import annotations

import json
import mimetypes
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .. import concurrency
from .common import Unavailable, env, env_int

SHORT_EDGE = 768
MAX_REF_IMAGES, MAX_REF_VIDEOS = 9, 3
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))   # 本机/内网服务，绕开系统代理


def _base(url: str) -> str:
    """把 `http://host:port/v1/videos/sync` 这类写法归一化成 base url。"""
    u = (url or "").strip().rstrip("/")
    for suffix in ("/v1/videos/sync", "/v1/videos", "/v1"):
        if u.endswith(suffix):
            return u[: -len(suffix)]
    return u


def fl2va_base() -> str:
    return _base(env("H3_ENDPOINT"))


def ref2va_base() -> str:
    return _base(env("H3_REF2VA_ENDPOINT"))


def _http(url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
          method: Optional[str] = None, timeout: int = 300) -> bytes:
    hdrs = dict(headers or {})
    token = env("H3_TOKEN")
    if token:
        hdrs.setdefault("Authorization", "Bearer %s" % token)
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with _OPENER.open(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:                                  # failed 任务也是 HTTP 500，但响应体仍是任务 JSON
            json.loads(body)
            return body
        except ValueError:
            raise RuntimeError("H3 HTTP %s：%s" % (exc.code, body[:400].decode("utf-8", "replace"))) from None
    except urllib.error.URLError as exc:
        raise RuntimeError("H3 服务连不上（%s）：%s" % (url, exc.reason)) from None


def ping(base: str, timeout: int = 5) -> Tuple[bool, str]:
    """GET /v1/models，返回 (是否在线, 模型 id 或错误)。"""
    if not base:
        return False, "未配置"
    try:
        data = json.loads(_http(base + "/v1/models", timeout=timeout))
        ids = [m.get("id") for m in data.get("data", [])]
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    # 推理引擎挂掉后（Stage-0 has no live replica），API 进程还在，/v1/models 照样 200，只有 /health 返回 503
    try:
        with _OPENER.open(urllib.request.Request(base + "/health"), timeout=timeout) as resp:
            healthy = resp.status == 200
    except urllib.error.HTTPError as exc:
        healthy = exc.code == 404                      # 老版本没有 /health，按在线处理
    except Exception:  # noqa: BLE001
        healthy = False
    if not healthy:
        return False, "推理引擎不可用（/health 非 200），需要重启 H3 服务"
    return True, ",".join(str(i) for i in ids) or "online"


def _multipart(fields: Dict[str, str], files: Sequence[Tuple[str, Path]]) -> Tuple[bytes, str]:
    boundary = "----studiobench" + uuid.uuid4().hex
    chunks: List[bytes] = []
    for key, val in fields.items():
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, key, val)).encode("utf-8"))
    for key, path in files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                       "Content-Type: %s\r\n\r\n" % (boundary, key, path.name, mime)).encode("utf-8"))
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=%s" % boundary


def _turbo_fields(task: str, extra: Dict[str, Any]) -> Dict[str, str]:
    """服务端加载了 lightx2v Turbo LoRA 时，请求必须带上该 LoRA 蒸馏时的步数和 flow shift，否则服务端拒绝。

    H3_TURBO_STEPS（fl2va：t2va/fl2va）/ H3_REF2VA_TURBO_STEPS（ref2va）= LoRA 文件名里的 N（4 或 8）；
    不设就按原版 50 步跑。768p 版 LoRA 的 flow shift：视频 6、音频 3（vllm-omni minimax_h3/lora.py）。
    """
    steps = env_int("H3_REF2VA_TURBO_STEPS" if task == "ref2va" else "H3_TURBO_STEPS", 0)
    if steps <= 0:
        return {}
    extra["audio_flow_shift"] = float(env("H3_TURBO_AUDIO_SHIFT", "3"))
    return {"num_inference_steps": str(steps), "flow_shift": env("H3_TURBO_VIDEO_SHIFT", "6")}


def generate(base: str, task: str, prompt: str, seconds: int, out: Path,
             references: Sequence[Path] = (), frame_indices: Optional[List[int]] = None,
             aspect: Optional[str] = None, seed: Optional[int] = None) -> Dict[str, Any]:
    if not base:
        raise Unavailable("未配置 %s" % ("H3_REF2VA_ENDPOINT（ref2va 服务）" if task == "ref2va"
                                         else "H3_ENDPOINT（fl2va 服务）"))
    extra: Dict[str, Any] = {"task": task}
    if frame_indices:
        extra["frame_indices"] = frame_indices
    fields = {"prompt": prompt, "seconds": str(int(seconds)), "short_edge": str(SHORT_EDGE)}
    fields.update(_turbo_fields(task, extra))
    fields["extra_params"] = json.dumps(extra)
    if seed is not None:
        fields["seed"] = str(int(seed))
    if task != "fl2va":                      # fl2va 画幅跟随首帧；t2va / ref2va 必须显式给
        fields["aspect_ratio"] = aspect or "16:9"

    body, ctype = _multipart(fields, [("input_references", Path(p)) for p in references])
    job = json.loads(_http(base + "/v1/videos", body, {"Content-Type": ctype}, method="POST", timeout=300))
    vid = job.get("id")
    if not vid:
        raise RuntimeError("H3 没有返回任务 id：%s" % json.dumps(job, ensure_ascii=False)[:300])

    max_wait = env_int("H3_MAX_WAIT", 3600)
    poll = env_int("H3_POLL", 10)
    started = time.time()
    while True:
        if time.time() - started > max_wait:
            raise RuntimeError("H3 超时：%s 超过 %ds 未完成" % (vid, max_wait))
        try:
            job = json.loads(_http(base + "/v1/videos/" + vid, timeout=60))
        except Exception:  # noqa: BLE001  轮询偶发失败不算失败
            job = {"id": vid, "status": "unknown"}
        status = job.get("status")
        if status == "completed":
            break
        if status == "failed":
            err = job.get("error")
            raise RuntimeError("H3 生成失败：%s" % (err if isinstance(err, str) else json.dumps(err, ensure_ascii=False)))
        time.sleep(poll)

    tmp = out.with_name(out.stem + ".part" + out.suffix)
    concurrency.track_part(tmp)
    tmp.write_bytes(_http(base + "/v1/videos/%s/content" % vid, timeout=900))
    tmp.replace(out)
    try:
        _http(base + "/v1/videos/" + vid, method="DELETE", timeout=60)
    except Exception:  # noqa: BLE001  清理失败不影响产物
        pass
    return {"job_id": vid, "inference_time_s": job.get("inference_time_s"),
            "num_frames": job.get("num_frames"), "seconds_requested": int(seconds)}


def check_references(refs: Sequence[Path]) -> None:
    images = [p for p in refs if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp")]
    videos = [p for p in refs if p.suffix.lower() in (".mp4", ".mov", ".webm", ".mkv")]
    others = [p.name for p in refs if p not in images and p not in videos]
    if others:
        raise ValueError("references 只接受图片或视频：%s" % ", ".join(others))
    if not refs:
        raise ValueError("references 不能为空")
    if len(images) > MAX_REF_IMAGES or len(videos) > MAX_REF_VIDEOS:
        raise ValueError("ref2va 最多 %d 张图、%d 段视频" % (MAX_REF_IMAGES, MAX_REF_VIDEOS))
