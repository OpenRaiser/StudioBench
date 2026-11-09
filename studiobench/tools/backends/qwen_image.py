# -*- coding: utf-8 -*-
"""Qwen-Image（t2i）与 Qwen-Image-Edit（image_edit），diffusers 进程内单例。

跑在 MCP server 自己的解释器里（昨晚验证过：H3 的 vllm-omni 环境，diffusers 0.40）。
权重 54G 左右，只从 ceph 读一次；`enable_model_cpu_offload` 让它和 H3 服务共卡。
image_edit 按权重目录 model_index.json 自动选管线：
QwenImageEditPlusPipeline（2509，可多图，支持 ref）或 QwenImageEditPipeline（单图）。
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .common import Unavailable, check_paths, env, under_models

T2I_NAME = "qwen-image"
EDIT_NAME = "qwen-image-edit"

# 与 H3 输出画布一致（32 的倍数），t2i 出的图可以直接当 i2v 首帧
SIZES = {"16:9": (1344, 768), "9:16": (768, 1344), "4:3": (1024, 768),
         "3:4": (768, 1024), "1:1": (768, 768), "21:9": (1792, 768)}

_PIPES: Dict[str, Any] = {}
# 双用模式（STUDIO_IMAGE_DUAL=1，工具池的 image 实例）：t2i 和 edit 两个管线都加载，同一时刻只有一个在 GPU 上，
# 另一个放 CPU 内存；来了另一种请求就互换（各 ~60GB，几秒）。一张卡同时服务 t2i / image_edit 两个池。
_ON_GPU: Optional[str] = None


def t2i_path() -> str:
    return env("QWEN_IMAGE_PATH", under_models("Models/Qwen-Image"))


def edit_path() -> str:
    return env("QWEN_IMAGE_EDIT_PATH")


def _diffusers_ok() -> Optional[str]:
    for mod in ("torch", "diffusers", "transformers"):
        if importlib.util.find_spec(mod) is None:
            return "当前解释器没有 %s（MCP server 要用装了 diffusers 的环境启动）" % mod
    return None


def available_t2i() -> Tuple[bool, str]:
    missing = check_paths(QWEN_IMAGE_PATH=t2i_path()) or _diffusers_ok()
    return (False, missing) if missing else (True, t2i_path())


def available_edit() -> Tuple[bool, str]:
    missing = check_paths(QWEN_IMAGE_EDIT_PATH=edit_path()) or _diffusers_ok()
    return (False, missing) if missing else (True, edit_path())


def _pipeline_class(path: str) -> str:
    try:
        return json.loads((Path(path) / "model_index.json").read_text())["_class_name"]
    except Exception:  # noqa: BLE001
        return ""


def _load(kind: str):
    if kind in _PIPES:
        return _PIPES[kind]
    ok, why = available_t2i() if kind == "t2i" else available_edit()
    if not ok:
        raise Unavailable(why)
    import torch
    import diffusers
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    path = t2i_path() if kind == "t2i" else edit_path()
    cls_name = _pipeline_class(path) or ("QwenImagePipeline" if kind == "t2i" else "QwenImageEditPipeline")
    cls = getattr(diffusers, cls_name, None)
    if cls is None:
        raise Unavailable("diffusers %s 里没有 %s，升级 diffusers" % (diffusers.__version__, cls_name))
    pipe = cls.from_pretrained(path, torch_dtype=torch.bfloat16)
    # 卡号是 MCP 进程内的逻辑编号（受 CUDA_VISIBLE_DEVICES 约束）；不设就是第一张卡。部署包按配置文件换算好
    device = env("STUDIO_T2I_DEVICE" if kind == "t2i" else "STUDIO_EDIT_DEVICE", "cuda")
    offload = env("STUDIO_IMAGE_OFFLOAD", "model")
    if env("STUDIO_IMAGE_DUAL", "0") == "1":
        _PIPES[kind] = pipe                 # 先留在 CPU，_activate 时再上卡
        return pipe
    if offload == "sequential":
        pipe.enable_sequential_cpu_offload(device=device)
    elif offload == "none":
        pipe.to(device)
    else:
        pipe.enable_model_cpu_offload(device=device)
    _PIPES[kind] = pipe
    return pipe


def _activate(kind: str):
    """双用模式：把 kind 的管线换上 GPU、另一个挪回 CPU；非双用模式等同 _load。返回 (管线, 换卡秒数)。"""
    global _ON_GPU
    pipe = _load(kind)
    if env("STUDIO_IMAGE_DUAL", "0") != "1" or _ON_GPU == kind:
        return pipe, 0.0
    import time as _t
    import torch
    t0 = _t.time()
    other = "edit" if kind == "t2i" else "t2i"
    if _ON_GPU == other and other in _PIPES:
        _PIPES[other].to("cpu")
        torch.cuda.empty_cache()
    pipe.to(env("STUDIO_T2I_DEVICE" if kind == "t2i" else "STUDIO_EDIT_DEVICE", "cuda"))
    _ON_GPU = kind
    return pipe, round(_t.time() - t0, 1)


def t2i(prompt: str, aspect: str, out: Path, steps: int = 30, seed: int = 0) -> Dict[str, Any]:
    import torch
    width, height = SIZES.get(aspect, SIZES["16:9"])
    pipe, swap = _activate("t2i")
    image = pipe(prompt=prompt, negative_prompt=" ", width=width, height=height,
                 num_inference_steps=max(4, int(steps)), true_cfg_scale=4.0,
                 generator=torch.Generator("cpu").manual_seed(int(seed))).images[0]
    image.save(out)
    return {"backend": T2I_NAME, "width": width, "height": height, **({"swap_s": swap} if swap else {})}


def image_edit(src: Path, prompt: str, out: Path, ref: Optional[Path] = None,
               steps: int = 40, seed: int = 0) -> Dict[str, Any]:
    import torch
    from PIL import Image
    pipe, swap = _activate("edit")
    multi = type(pipe).__name__ == "QwenImageEditPlusPipeline"
    if ref is not None and not multi:
        raise Unavailable("当前 image_edit 权重（%s）只支持单图，不能带 ref" % type(pipe).__name__)
    source = Image.open(src).convert("RGB")
    images = [source] + ([Image.open(ref).convert("RGB")] if ref is not None else [])
    result = pipe(image=images if multi else source, prompt=prompt, negative_prompt=" ",
                  true_cfg_scale=4.0, num_inference_steps=max(4, int(steps)),
                  generator=torch.Generator("cpu").manual_seed(int(seed))).images[0]
    if result.size != source.size:          # 保持原图尺寸，改完的图能直接接 i2v
        result = result.resize(source.size, getattr(Image, "Resampling", Image).LANCZOS)
    result.save(out)
    return {"backend": EDIT_NAME, "pipeline": type(pipe).__name__,
            "width": result.size[0], "height": result.size[1], **({"swap_s": swap} if swap else {})}
