# Case explorer

Static HTML for qualitative review: one page per run with the tool-call timeline, per-tool
summary, files produced and the final cut.

```bash
python -m studiobench explorer --runs ./runs --out ./explorer_site \
  --media-base http://127.0.0.1:8130
```

`--media-base` is the URL prefix under which the run tree is served; the page then embeds
`<media-base>/<task>/<model>/final.mp4`. Without it, the page shows the local path instead of
a video element, so the site stays portable.

Output:

```
explorer_site/
  index.html                 all runs, filterable by task/model in the browser
  <task>__<model>.html       one page per run
  runs.json                  summaries (same fields as the monitor)
```

The viewer is deliberately simple and dependency-free. The richer process canvas (stage
board, shot alignment, cross-model similarity) can be layered on the same run data without
changing the artifact format.
