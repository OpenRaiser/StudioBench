"""Scoring rules: null means 'not judged', forbid zeroes the film score, duration counts."""
from studiobench.eval.score import audio_scores, film_score, main_scores


def _item(kind, answer):
    return {"kind": kind, "answer": answer, "scored": True}


def test_film_complete():
    items = [_item("must", "yes"), _item("must", "no"), _item("forbid", "no")]
    out = film_score(items, duration_ok=True)
    assert out["complete"] is True
    assert out["value"] == round(2 / 3, 4)   # 1 hit + duration, 2 positive + duration


def test_forbid_hit_zeroes():
    items = [_item("must", "yes"), _item("forbid", "yes")]
    out = film_score(items, duration_ok=True)
    assert out["forbid_hit"] is True
    assert out["value"] == 0.0


def test_incomplete_judging_returns_null():
    items = [_item("must", "yes"), {"kind": "must", "scored": True}]
    out = film_score(items, duration_ok=True)
    assert out["complete"] is False
    assert out["value"] is None


def test_audio_only_uses_applicable_profile():
    task = {"audio_profile": ["music"], "domain": "music"}
    metrics = {"intelligibility": 0.9, "vocal_unmasked": 0.8, "lip_sync": 0.5,
               "sfx_sync": None, "beat_sync": 0.7}
    out = audio_scores(task, metrics)
    assert out["audio"] is None              # no speech/dialogue metric applies
    assert out["av"] == 0.7                  # only beat_sync applies


def test_unsubmitted_scores_zero():
    scores = main_scores(False, {"value": 1.0}, {}, {"audio": 1.0, "av": 1.0})
    assert scores == {"film": 0.0, "visual": 0.0, "audio": 0.0, "av": 0.0}
