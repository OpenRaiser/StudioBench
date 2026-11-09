# Tool contract

This is the interface the evaluated model sees and the semantics every backend must preserve.
The same contract is mirrored (not shared as code) in the StudioFlow repository; changes here
must be reflected there.

## Conventions

- Tools operate inside a **work directory**. Paths in arguments are relative to it; absolute
  paths outside it are rejected.
- A tool that writes a file accepts an optional `out`. If omitted, the name is generated
  (`<tool>_001.mp4`, ...) and never overwrites an existing file.
- Video outputs are H.264/AAC MP4. Editing tools normalise to 24 fps and 48 kHz when inputs
  disagree.
- `sample_frames` returns a contact sheet image; images are returned to vision-capable models.
- Errors are returned as `{"ok": false, "error": "..."}` with a human-readable reason. A tool
  that cannot run on this machine is reported as unavailable in `tools/list` and on call.
- The **only** delivery path is `export`. Calling it multiple times is allowed; the last one
  wins. A run without `export` is not submitted.

## Generation

| tool | key arguments | notes |
|---|---|---|
| `t2v` | `prompt`, `duration` (5 or 8), `aspect`, `seed` | text to video |
| `i2v` | `image`, `prompt`, `duration` (5 or 8), `seed` | first-frame conditioned |
| `ref_video` | `image`, `prompt`, `duration` | reference-image conditioned |
| `t2i` | `prompt`, `aspect`, `seed` | text to image |
| `image_edit` | `src`, `prompt`, optional `ref`, `seed` | instruction-based edit |
| `tts` | `text`, optional `voice_ref`, `voice_ref_text`, `speaker`, `instruct`, `language` | speech; cloning when `voice_ref` is given |
| `music` | `prompt`, `duration`, optional `lyrics` | music bed |
| `sfx` | `video`, `prompt`, optional `duration` | sound effects for a shot |
| `lipsync` | `video`, `audio` | mouth sync |
| `asr` | `audio`, optional `language` | transcript with timestamps |

## Editing

`trim(video, start, end)`, `concat(videos)`, `overlay(video, subtitles|srt|srt_text)`,
`mix_audio(video, audio, gain, start, replace)`, `transition(a, b, kind, duration)`,
`reframe(video, aspect, mode)`, `ken_burns(image, duration, zoom)`, `speed(video, factor)`,
`color(video, preset|eq|temperature|lut)`, `composite(base, layers)`,
`audio_mix(tracks)`, `audio_edit(audio, start, end, gain, fade)`.

## Rendering and analysis

`render_html(html, duration, fps, times)`, `render_three(code, duration, fps)`,
`render_blender(script, duration|frames)`, `scene_detect(video)`, `beat_detect(audio)`,
`remove_bg(image)`, `fetch_url(url)`.

## Inspection and delivery

`sample_frames(video, fps, start, end)` — contact sheet with time labels;
`inspect(path)` — duration, aspect, streams, file list;
`export(src)` — copy to `final.mp4` and mark the run submitted.

## Backend selection

Each generation tool resolves a backend from environment configuration. If unset, the tool is
unavailable rather than silently mocked. `STUDIO_BACKEND=mock` is an explicit test mode and
marks results invalid for benchmarking.

## Manifest

A delivered run also carries the tool-call log (`.calls.jsonl`). It is the source of truth for
lineage: every produced file, its tool, arguments, backend, parent assets, and duration. The
editor UI and the evaluator both consume this log; keep field names stable.
