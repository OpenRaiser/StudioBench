"""Static Pages builder: curated media, explorer pages and landing page."""
import json
import subprocess

import pytest

from studiobench.site.build import build_site
from studiobench.tools.media import FFMPEG


def _run(root, task, model):
    run = root / task / model
    (run / "work").mkdir(parents=True)
    (run / "result.json").write_text(json.dumps({
        "task": task, "model": model, "submitted": True, "valid_for_bench": True,
        "returncode": 0, "seconds": 2.0, "tool_calls": 1, "tool_errors": 0}))
    (run / "prompt.txt").write_text("brief")
    (run / "work" / ".calls.jsonl").write_text(
        json.dumps({"i": 1, "tool": "t2v", "ok": True, "sec": 0.5}) + "\n")
    (run / "final.mp4").write_bytes(b"stub")
    media = root / "media-src" / task / model
    media.mkdir(parents=True)
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=red:s=160x120:d=1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    str(media / "final_720p.mp4")], check=True)
    return run


def test_site_build(tmp_path):
    try:
        subprocess.run([FFMPEG, "-version"], capture_output=True, check=True)
    except Exception:  # noqa: BLE001
        pytest.skip("ffmpeg not installed")
    runs = tmp_path / "runs"
    _run(runs, "T-1", "model-a")
    _run(runs, "T-1", "model-b")
    manifest = build_site(runs, tmp_path / "site", max_mb=50, profile="540",
                          media_from=[runs / "media-src"], title="T")
    assert len(manifest["runs"]) == 2
    site = tmp_path / "site"
    assert (site / "index.html").is_file()
    assert (site / "explorer" / "T-1__model-a.html").is_file()
    assert (site / "explorer" / "index.html").is_file()
    assert (site / "leaderboard" / "index.html").is_file()
    video = site / "media" / "T-1" / "model-a" / "final.mp4"
    assert video.is_file() and video.stat().st_size > 0
    page = (site / "explorer" / "T-1__model-a.html").read_text()
    assert "../media/T-1/model-a/final.mp4" in page
