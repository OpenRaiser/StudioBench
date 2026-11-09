# Metrics

Scoring runs after inference and never changes the run artifacts. Implementations live in
`studiobench/eval/`; the aggregation rules below are the public definition.

## Hard gates

Computed with ffprobe before any model judge:

- file decodes and has a video stream;
- duration inside the task's `duration_sec` (a miss counts as one failed film item);
- aspect ratio matches the task (tolerance documented in `eval/hard.py`);
- audio track exists when the task's `audio_profile` requires sound;
- not silent (integrated loudness above a floor).

## Four main scores

| score | definition |
|---|---|
| **film** | hits / judged checklist items. `must` + `constraints` + `plot_beats` + `facts` are one class; any `forbid` hit sets the score to 0. Duration is one extra item. If the judge did not answer every item, the score is `null` rather than imputed. |
| **visual** | mean of the VBench dimensions that completed successfully for this run. |
| **audio** | mean of the applicable audio metrics: intelligibility and vocal-unmasked for `speech`/`dialogue`. For speech-led domains, vocal-unmasked = 0 forces audio = 0. |
| **av** | mean of applicable sync metrics: lip-sync for `dialogue`, SFX sync for `sfx`, beat sync for `music`. |

A run that was not submitted scores 0 on all four.

## Aggregation

- Per model: mean of each score over tasks where that score is non-null, with the coverage
  count reported next to it.
- Audio and A-V are additionally broken down by `audio_profile` label so music videos and
  narrated films are not averaged into one number.
- The overall score definition (equal weight per label vs. task-count weighted) is part of the
  benchmark version and must be fixed before publishing a leaderboard.
- Confidence intervals use paired bootstrap over tasks; adjacent ranks are separated into
  tiers only when the ordering holds in at least 90% of resamples.

## Judge stability

The judge model, prompt, and decoding parameters are part of the evaluation config and are
recorded in every `eval.json`. Changing any of them requires a new leaderboard version. A
small human-rated sample is kept for periodic agreement checks.
