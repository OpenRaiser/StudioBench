#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按 8 片种 × 5 任务批量跑：推理阶段的主入口。

    ./sb batch --tasks <题目目录> --name pilot-opus --plan          # 只看计划：每格选了哪些题、哪些格缺题
    ./sb batch --tasks <题目目录> --name pilot-opus --per-cell 1     # 每格先跑 1 道
    ./sb batch --tasks <题目目录> --name pilot-opus --resume         # 断了接着跑（跳过已完成的题）
    ./sb batch --tasks <题目目录> --name rehearsal --mock --effort low   # 没 GPU 时演练整套流程

H3 一次只服务一个请求，所以串行；题目按格子轮转（每格先跑第 1 道，再跑第 2 道…），
中途停下时覆盖面也是均匀的。目录结构：
  runs/<批次>/<题目>__<模型>__<时间>/   单题全部产物（见 run.py）
  runs/<批次>/batch.json                计划与参数
  runs/<批次>/status.jsonl              每道题一行结果（续跑依据）
  runs/<批次>/summary.md                8×5 表：每格交卷数 / 题数、有效数、平均调用数与耗时
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import run as runner
from . import taskspec

# 文档 §1 的格子：Narrative / Product / Education 没有 Audio-led，其余 5 种任务都有
DOC_GRID = {d: set(taskspec.TASKS) - ({"audio-led"} if d in ("narrative", "product", "education") else set())
            for d in taskspec.DOMAINS}
PER_CELL_DOC = 4                                          # 文档：有题的格子每格 4 条


def _api_down(result: Dict[str, Any]) -> bool:
    """模型 API 故障：会话一开始就报错退出，一次工具都没调用过——这道题等于没跑。"""
    return bool((result.get("llm") or {}).get("is_error")) and not result.get("tool_calls")


def _api_interrupted(result: Dict[str, Any], run_dir: str) -> bool:
    """跑到一半被模型 API 故障打断（已经调过工具、没交卷、没超时）：API 恢复后接着原会话续跑，不从头重跑。"""
    if result.get("submitted") or result.get("timed_out") or not result.get("tool_calls"):
        return False
    flag = result.get("interrupted_by_api")
    return bool(flag) if flag is not None else runner.interrupted_by_api(Path(run_dir))


def should_retry(result: Dict[str, Any]) -> bool:
    """一道题跑完了（不管交没交卷），要不要因为基础设施问题重跑？

    result 是 run.py 写的 result.json，可用字段：
      submitted / timed_out / returncode / tool_calls
      error_kinds = {"usage", "service", "timeout", "unavailable", "other"} → 失败调用数
      llm["is_error"]（模型 API 本身出错）
    """
    # TODO(xenoxu): 定一条重跑规则，5–10 行即可。要权衡的是：
    #   · 服务挂了（service / timeout 很多、llm.is_error）不是模型的错，不重跑会冤枉模型
    #   · 但只要失败就重跑，等于给模型多次机会挑最好的一次（best-of-N），对其它模型不公平
    #   · H3 一段要几分钟到半小时，重跑一整题可能是几个小时的 GPU
    raise NotImplementedError


def _load_tasks(paths: List[str]) -> List[Tuple[Path, Dict[str, Any]]]:
    files: List[Path] = []
    for p in paths:
        path = Path(p)
        files += sorted(path.glob("*.json")) if path.is_dir() else [path]
    out = []
    for f in files:
        try:
            task = taskspec.load(f)
        except Exception as exc:  # noqa: BLE001
            print("跳过 %s：读不了（%s）" % (f, exc), file=sys.stderr)
            continue
        if task.get("id") and task.get("domain") and task.get("task"):
            out.append((f.resolve(), task))
    return out


def plan(tasks: List[Tuple[Path, Dict[str, Any]]], per_cell: int, domains: Optional[List[str]],
         types: Optional[List[str]], allow_invalid: bool,
         all_cells: bool = False) -> Tuple[List[Tuple[Path, Dict[str, Any]]], Dict[str, Any]]:
    cells: Dict[Tuple[str, str], List[Tuple[Path, Dict[str, Any]]]] = {}
    skipped, seen = [], {}
    for path, task in tasks:
        if domains and task["domain"] not in domains or types and task["task"] not in types:
            continue
        if not all_cells and task["task"] not in DOC_GRID.get(task["domain"], set()):
            continue                                   # 文档格子里没有的（如 narrative/audio-led）默认不跑
        if task["id"] in seen:
            skipped.append({"task": task["id"], "errors": ["id 重复：%s 与 %s" % (path, seen[task["id"]])]})
            continue
        seen[task["id"]] = path
        errors = [m for lv, m in taskspec.validate(task) if lv == "error"]
        if errors and not allow_invalid:
            skipped.append({"task": task["id"], "errors": errors})
            continue
        cells.setdefault((task["domain"], task["task"]), []).append((path, task))
    for key in cells:
        cells[key].sort(key=lambda x: x[1]["id"])
        cells[key] = cells[key][:per_cell]
    ordered: List[Tuple[Path, Dict[str, Any]]] = []
    for rank in range(per_cell):                       # 格子轮转
        for d in taskspec.DOMAINS:
            for t in taskspec.TASKS:
                group = cells.get((d, t), [])
                if rank < len(group):
                    ordered.append(group[rank])
    gaps = [{"domain": d, "task": t, "have": len(cells.get((d, t), [])), "want": per_cell}
            for d in taskspec.DOMAINS for t in sorted(DOC_GRID[d])
            if (not domains or d in domains) and (not types or t in types) and len(cells.get((d, t), [])) < per_cell]
    return ordered, {"cells": {"%s/%s" % k: [x[1]["id"] for x in v] for k, v in cells.items()},
                     "gaps": gaps, "skipped_invalid": skipped}


def _read_status(path: Path) -> Dict[str, Dict[str, Any]]:
    latest: Dict[str, Dict[str, Any]] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            latest[row["task"]] = row
    return latest


def summary_md(name: str, model: str, rows: Dict[str, Dict[str, Any]], info: Dict[str, Any]) -> str:
    def cell(d: str, t: str) -> str:
        if t not in DOC_GRID[d]:
            return "（文档无）"
        ids = info["cells"].get("%s/%s" % (d, t), [])
        if not ids:
            return "缺题"
        done = [rows[i] for i in ids if i in rows and rows[i]["status"] == "done"]
        sub = sum(1 for r in done if r.get("submitted"))
        valid = sum(1 for r in done if r.get("valid_for_bench"))
        return "%d/%d 交卷·%d 有效" % (sub, len(ids), valid) if done else "0/%d 未跑" % len(ids)

    lines = ["# 批次 %s（%s）" % (name, model), "",
             "| 片种 \\ 任务 | " + " | ".join(taskspec.TASKS) + " |", "|" + " --- |" * (len(taskspec.TASKS) + 1)]
    for d in taskspec.DOMAINS:
        lines.append("| %s | %s |" % (d, " | ".join(cell(d, t) for t in taskspec.TASKS)))
    done = [r for r in rows.values() if r["status"] == "done"]
    if done:
        lines += ["", "已跑 %d 道：交卷 %d，有效 %d；平均 %.1f 次调用、%.1f 分钟；超时 %d 道。" % (
            len(done), sum(1 for r in done if r.get("submitted")), sum(1 for r in done if r.get("valid_for_bench")),
            sum(r.get("tool_calls", 0) for r in done) / len(done), sum(r.get("seconds", 0) for r in done) / 60 / len(done),
            sum(1 for r in done if r.get("timed_out")))]
    failed = [r for r in rows.values() if r["status"] != "done"]
    if failed:
        lines += ["", "没开跑成功：" + "；".join("%s（%s）" % (r["task"], r.get("error")) for r in failed)]
    if info["gaps"]:
        lines += ["", "缺题（文档要求每格 %d 道）：" % PER_CELL_DOC + "、".join(
            "%s/%s %d/%d" % (g["domain"], g["task"], g["have"], g["want"]) for g in info["gaps"])]
    return "\n".join(lines) + "\n"


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="studiobench batch", description=(__doc__ or "").split("\n\n")[0])
    ap.add_argument("--tasks", nargs="+", required=True, help="题目目录或 json 文件")
    ap.add_argument("--name", required=True, help="批次名，决定 runs/<批次> 目录")
    ap.add_argument("--per-cell", type=int, default=PER_CELL_DOC)
    ap.add_argument("--domains", help="逗号分隔，只跑这些片种")
    ap.add_argument("--types", help="逗号分隔，只跑这些任务：open,constraint,assets,reference,audio-led")
    ap.add_argument("--include-invalid-tasks", action="store_true", help="校验有 error 的题也跑（如冒烟题）")
    ap.add_argument("--all-cells", action="store_true", help="文档格子里没有的组合（如 narrative/audio-led）也跑")
    ap.add_argument("--plan", action="store_true", help="只打印计划，不跑")
    ap.add_argument("--resume", action="store_true", help="跳过 status.jsonl 里已经跑完的题")
    ap.add_argument("--runs-dir", default=runner.RUNS_DIR)
    known, rest = ap.parse_known_args(argv)               # 其余参数（--model --mock --effort …）原样交给 run.py

    tasks = _load_tasks(known.tasks)
    ordered, info = plan(tasks, known.per_cell, known.domains.split(",") if known.domains else None,
                         known.types.split(",") if known.types else None, known.include_invalid_tasks,
                         known.all_cells)
    base_args = runner.build_parser().parse_args(["--task", "_"] + rest)
    batch_dir = Path(known.runs_dir) / known.name
    print("计划：%d 道题，%d 个格子；缺题 %d 格；因题目错误跳过 %d 道" % (
        len(ordered), len(info["cells"]), len(info["gaps"]), len(info["skipped_invalid"])), file=sys.stderr)
    if known.plan:
        print(summary_md(known.name, base_args.model, {}, info))
        return 0

    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "batch.json").write_text(json.dumps({"name": known.name, "argv": sys.argv[1:], "model": base_args.model,
                                                      "mock": base_args.mock, "plan": info,
                                                      "order": [t["id"] for _, t in ordered]},
                                                     ensure_ascii=False, indent=2), encoding="utf-8")
    status_path = batch_dir / "status.jsonl"
    rows = _read_status(status_path)
    retry_rule_missing = False
    exit_code = 0                       # 3 = H3 等核心后端不可用、4 = 模型 API 不可用；整批停下（deploy/lib/lane_loop.sh 会等恢复后自动续跑）
    for k, (path, task) in enumerate(ordered, start=1):
        prev = rows.get(task["id"])
        resume_dir = None
        if known.resume and prev and prev["status"] in ("done", "interrupted"):
            prev_res = json.loads(Path(prev["run_dir"], "result.json").read_text(encoding="utf-8"))
            if _api_down(prev_res):
                again = True                         # 模型 API 故障时一开始就退出了，等于没跑过（下面的刹车规则加上之前记成了 done）
            elif prev["status"] == "interrupted" or _api_interrupted(prev_res, prev["run_dir"]):
                again, resume_dir = True, prev["run_dir"]    # 跑到一半被 API 打断：接着原会话续跑
            else:
                try:
                    again = should_retry(prev_res)
                except NotImplementedError:
                    again, retry_rule_missing = False, True
            if not again:
                continue
        print("[%d/%d] %s/%s %s%s" % (k, len(ordered), task["domain"], task["task"], task["id"],
                                      "（续跑）" if resume_dir else ""), file=sys.stderr)
        args = argparse.Namespace(**vars(base_args))
        args.task, args.runs_dir, args.out = str(path), str(batch_dir), None
        args.resume_run = resume_dir
        started = time.time()
        try:
            res = runner.run_task(args)
        except KeyboardInterrupt:
            print("中断；用 --resume 接着跑", file=sys.stderr)
            return 130
        except Exception as exc:  # noqa: BLE001
            res = {"task": task["id"], "error": "%s: %s" % (type(exc).__name__, exc), "code": 1}
        row = {"task": task["id"], "domain": task["domain"], "type": task["task"], "t": time.time(),
               "status": "error" if "error" in res else "done"}
        if "error" in res:
            row.update({"error": res["error"], "seconds": round(time.time() - started, 1)})
        else:
            row.update({k2: res.get(k2) for k2 in ("run_dir", "submitted", "valid_for_bench", "tool_calls",
                                                    "tool_errors", "seconds", "timed_out", "error_kinds")})
        # 模型 API 故障刹车：会话一开始就报错、一次工具都没调过，等于这道题没跑。记成 error（--resume 会重跑），
        # 并且整批停下，免得 API 故障期间把整份题目列表快速耗光。跑到一半才出错的题不在这里处理，那属于 should_retry()。
        api_down = row["status"] == "done" and _api_down(res)
        if api_down:
            row.update({"status": "error", "error": "模型 API 不可用：会话一开始就报错，没有调用任何工具"})
        # 跑到一半被模型 API 打断：记成 interrupted，整批停下；API 恢复后 --resume 会接着原会话续跑（run.py --resume-run）
        interrupted = row["status"] == "done" and _api_interrupted(res, res.get("run_dir") or "")
        if interrupted:
            row.update({"status": "interrupted", "error": "被模型 API 故障打断，恢复后接着原会话续跑"})
            api_down = True
        with status_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        rows[task["id"]] = row
        (batch_dir / "summary.md").write_text(summary_md(known.name, base_args.model, rows, info), encoding="utf-8")
        if row["status"] == "error" and res.get("code") == 3:
            print("核心后端不可用，整批停下：%s" % res["error"], file=sys.stderr)
            exit_code = 3
            break
        if api_down:
            print("模型 API 不可用，整批停下；恢复后用 --resume 接着跑（这道题会重跑）", file=sys.stderr)
            exit_code = 4
            break
    if retry_rule_missing:
        print("提示：should_retry() 还没定规则，续跑时已完成的题一律不重跑（见 harness/batch.py）", file=sys.stderr)
    print((batch_dir / "summary.md").read_text(encoding="utf-8") if (batch_dir / "summary.md").exists() else "")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
