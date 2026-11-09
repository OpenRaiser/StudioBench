# -*- coding: utf-8 -*-
"""v2 工具共用的小东西：工具箱路径、取 registry 的路径助手（延迟导入，避免和 registry 循环导入）。"""
from __future__ import annotations

import os
from pathlib import Path

TOOLBOX = Path(os.environ.get("STUDIO_TOOLBOX", str(Path.cwd() / ".toolbox")))


def R():
    """registry 模块：用它的 _resolve / _in / _out / _rel / _part / _commit / WORKDIR（调用时再取）。"""
    from .. import registry
    return registry
