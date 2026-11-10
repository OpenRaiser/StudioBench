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


def test_import_csv_board_and_exclusion(tmp_path):
    from studiobench.leaderboard.import_csv import build

    board = tmp_path / "board.csv"
    board.write_text(
        "rank,model,submitted,score,rank_lo,rank_hi,lead_next,AUD,SEM,VIS,AV\n"
        "1,Claude,156,92.22,1,3,0.5,95,96,86,89\n"
        "2,Qwen-27b,160,82.80,6,7,1.0,90,92,63,81\n"
        "3,Hy4,158,68.35,8,9,0.9,49,90,76,45\n", encoding="utf-8")
    rows = build(board, None, {"qwen3.8-27b"})
    assert [r["name"] for r in rows] == ["Claude Opus 5.5", "Hy4"]
    assert [r["display_rank"] for r in rows] == [1, 2]
