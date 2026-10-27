# Usage Report

Final full-dataset run that produced `output.csv`: `uv run --project code python code/main.py run`, finished 2026-10-25 11:47 UTC, 195 s wall time.

## Models

| Provider | Model | Used for |
| --- | --- | --- |
| anthropic | claude-sonnet-5-5 | PDF/screenshot evidence extraction (once per file, cached); request-intent classification (batched, cached) |

All other stages (ingestion, identity resolution, discovery, correlation, state reconstruction, policy, step planning) are deterministic code and use no model.

## Per model and purpose

| Model | Purpose | Calls | Input tokens | Output tokens | Total tokens | Est. cost |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| anthropic:claude-sonnet-5-5 | request-intent classification (batched, cached) | 20 | 47,951 | 20,681 | 68,632 | $0.3027 |
| anthropic:claude-sonnet-5-5 | PDF/screenshot evidence extraction (once per file, cached) | 15 | 75,462 | 9,147 | 84,609 | $0.2424 |

## Overall

| Metric | Value |
| --- | ---: |
| Model calls | 35 |
| Input tokens | 123,413 |
| Output tokens | 29,828 |
| Total tokens | 153,241 |
| Evaluation requests audited | 518 |
| Average tokens per evaluation request | 295.8 |
| Estimated total cost | $0.5451 |
| Estimated cost per evaluation request | $0.0011 |

## Notes

- Classification covers all 550 requests in both request files (the sample requests share the activity log used for workflow correlation); the per-request averages divide by the 518 evaluation requests only.
- Request texts are deduplicated and sent 20 per call; identical texts are classified once.
- Both model stages are cached on disk keyed by input, prompt and model. A rerun with warm caches makes no model calls and costs nothing; this report counts the calls actually made in this run.
- Costs use list prices per million tokens (input / output): claude-haiku-5-5 $0.10 / $0.50, claude-opus-5-5 $4.00 / $20.00, claude-sonnet-5-5 $2.00 / $10.00.
