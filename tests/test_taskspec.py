"""The shipped demo task must satisfy the public schema."""
import json
from pathlib import Path

from studiobench.harness import taskspec

DEMO = Path(__file__).resolve().parents[1] / "tasks" / "demo" / "DEMO-001.json"


def test_demo_task_validates():
    task = json.loads(DEMO.read_text(encoding="utf-8"))
    issues = taskspec.validate(task)
    errors = [msg for level, msg in issues if level == "error"]
    assert not errors, errors
    assert task["domain"] in taskspec.DOMAINS
    assert task["task"] in taskspec.TASKS
    lo, hi = task["duration_sec"]
    assert taskspec.DOC_DURATION[0] <= lo <= hi <= taskspec.DOC_DURATION[1]
