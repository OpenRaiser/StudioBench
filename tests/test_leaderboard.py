"""Leaderboard aggregation ignores missing scores and reports coverage."""
from studiobench.leaderboard.build import summarize, to_markdown


def _row(model, film, visual, audio, av, profile=("music",)):
    return {"model": model, "valid_for_bench": True, "scores": {
        "film": film, "visual": visual, "audio": audio, "av": av}, "_profile": list(profile)}


def test_summary_means_and_nulls():
    rows = [_row("a", 1.0, 0.5, 0.8, None), _row("b", 1.0, None, 1.0, 1.0)]
    summary = {s["model"]: s for s in summarize(rows)}
    assert summary["a"]["scores"]["film"] == 1.0
    assert summary["a"]["scores"]["visual"] == 0.5
    assert summary["a"]["scores"]["av"] is None
    assert summary["b"]["coverage"]["visual"] == 0
    assert summary["a"]["audio_by_profile"]["music"] == {"score": 0.8, "n": 1}


def test_markdown_has_row_per_model():
    rows = [_row("a", 0.5, 0.5, 0.5, 0.5)]
    md = to_markdown(summarize(rows))
    assert "| a |" in md
    assert md.count("\n") >= 2
