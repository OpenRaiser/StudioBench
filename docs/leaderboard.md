# Leaderboard

The leaderboard is generated from a **snapshot**: a directory of completed runs containing
`eval/eval.json`. It never re-scores and never reads a live experiment tree, so a published
version can be rebuilt byte-for-byte from the snapshot.

## Snapshot layout

```
snapshot/
  <task-id>/<model>/
    result.json          # run metadata, validity flags
    eval/eval.json       # scores, judge config, per-item verdicts
```

`eval.json` must contain at least:

```json
{
  "model": "example-model",
  "valid_for_bench": true,
  "scores": {"film": 0.82, "visual": 0.71, "audio": 0.9, "av": 0.66},
  "judge": {"model": "...", "version": "..."},
  "harness_version": "...",
  "budget": {"calls": 200, "wall_hours": 4}
}
```

Rules:

- only `valid_for_bench=true` runs are published by default;
- null means "not scored", never zero; a submitted-but-failed metric is reported as `null`
  with a coverage count;
- the overall score is the equal-weight mean of the available main scores (film, visual,
  audio, A/V); if a score is null it is excluded from that model's mean and the coverage is
  reported next to each column. Changing the formula is a leaderboard version change;
- the judge version and harness version are part of the leaderboard version.

## Build

```bash
python -m studiobench leaderboard --runs ./snapshot --out ./site --title "StudioBench v1"
```

Outputs `index.html`, `data.json` and `leaderboard.md`. The HTML is self-contained and can be
served from any static host.

## Case explorer

The qualitative explorer (process view: calls, generated assets, shots, final cut) consumes
the same snapshot plus an artifact directory. It is built separately and linked from each
leaderboard cell; it is not part of the quantitative score.
