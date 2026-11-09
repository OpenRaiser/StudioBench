# -*- coding: utf-8 -*-
"""v2 新增工具（HTML / three.js / Blender / 剪辑扩展）。

每个模块导出 TOOLS = {名字: spec}，spec 字段：
· fn / desc / schema   和 registry.TOOLS 一样
· backend              写进调用日志的后端名（如 "blender-5.2.0"、"chromium-headless"、"ffmpeg"）
· check                可选，() -> (ok, detail)，只做便宜的就绪检查（二进制在不在、版本对不对），不渲染
registry.py 末尾把这里的 TOOLS 并进总表；backends.status() / backend_name() 也读这里。
"""
from __future__ import annotations

from typing import Any, Dict

from . import blender, edit, web

TOOLS: Dict[str, Dict[str, Any]] = {}
for _mod in (web, blender, edit):
    for _name, _spec in _mod.TOOLS.items():
        if _name in TOOLS:
            raise RuntimeError("v2 工具重名：%s" % _name)
        TOOLS[_name] = _spec
