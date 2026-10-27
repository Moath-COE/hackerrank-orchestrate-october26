"""Write evaluation/usage_report.md from the model calls logged during one run.

Every structured_call appends one line to the usage log; a run keeps the lines
it added, so the report covers exactly the run that produced the output file.
"""

import time
from collections import defaultdict
from pathlib import Path

from audit.llm import PRICES_PER_MTOK, model_name

PURPOSES = {
    "extract": "PDF/screenshot evidence extraction (once per file, cached)",
    "classify": "request-intent classification (batched, cached)",
}


def _cost(model: str, input_tokens: int, output_tokens: int) -> float | None:
    prices = PRICES_PER_MTOK.get(model.split(":", 1)[-1])
    if prices is None:
        return None
    return input_tokens / 1e6 * prices[0] + output_tokens / 1e6 * prices[1]


def _money(value: float | None) -> str:
    return "n/a" if value is None else f"${value:.4f}"


def write_usage_report(path: Path, usage: list[dict], requests: int, classified: int, seconds: float) -> None:
    groups: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0, 0])
    for entry in usage:
        key = (entry["model"], entry["purpose"].split(":", 1)[0])
        groups[key][0] += 1
        groups[key][1] += entry["input_tokens"]
        groups[key][2] += entry["output_tokens"]

    calls = sum(g[0] for g in groups.values())
    tokens_in = sum(g[1] for g in groups.values())
    tokens_out = sum(g[2] for g in groups.values())
    costs = [_cost(model, g[1], g[2]) for (model, _), g in groups.items()]
    total_cost = None if any(c is None for c in costs) else sum(costs)
    configured = model_name()
    provider, _, name = configured.partition(":")

    lines = [
        "# Usage Report",
        "",
        f"Final full-dataset run that produced `output.csv`: `uv run --project code python code/main.py run`, "
        f"finished {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}, {seconds:.0f} s wall time.",
        "",
        "## Models",
        "",
        "| Provider | Model | Used for |",
        "| --- | --- | --- |",
        f"| {provider or 'n/a'} | {name or configured} | {'; '.join(PURPOSES.values())} |",
        "",
        "All other stages (ingestion, identity resolution, discovery, correlation, state reconstruction, "
        "policy, step planning) are deterministic code and use no model.",
        "",
        "## Per model and purpose",
        "",
        "| Model | Purpose | Calls | Input tokens | Output tokens | Total tokens | Est. cost |",
        "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ]
    for (model, purpose), (n, t_in, t_out) in sorted(groups.items()):
        lines.append(f"| {model} | {PURPOSES.get(purpose, purpose)} | {n} | {t_in:,} | {t_out:,} | "
                     f"{t_in + t_out:,} | {_money(_cost(model, t_in, t_out))} |")
    if not groups:
        lines.append("| — | — | 0 | 0 | 0 | 0 | $0.0000 |")

    per_request = (tokens_in + tokens_out) / requests if requests else 0
    lines += [
        "",
        "## Overall",
        "",
        "| Metric | Value |",
        "| --- | ---: |",
        f"| Model calls | {calls} |",
        f"| Input tokens | {tokens_in:,} |",
        f"| Output tokens | {tokens_out:,} |",
        f"| Total tokens | {tokens_in + tokens_out:,} |",
        f"| Evaluation requests audited | {requests} |",
        f"| Average tokens per evaluation request | {per_request:,.1f} |",
        f"| Estimated total cost | {_money(total_cost)} |",
        f"| Estimated cost per evaluation request | {_money(None if total_cost is None else total_cost / requests)} |",
        "",
        "## Notes",
        "",
        f"- Classification covers all {classified} requests in both request files (the sample requests share the "
        "activity log used for workflow correlation); the per-request averages divide by the "
        f"{requests} evaluation requests only.",
        "- Request texts are deduplicated and sent 20 per call; identical texts are classified once.",
        "- Both model stages are cached on disk keyed by input, prompt and model. A rerun with warm caches makes "
        "no model calls and costs nothing; this report counts the calls actually made in this run.",
        "- Costs use list prices per million tokens (input / output): "
        + ", ".join(f"{model} ${p[0]:.2f} / ${p[1]:.2f}" for model, p in sorted(PRICES_PER_MTOK.items())) + ".",
    ]
    if calls == 0:
        lines.append("- This run was served entirely from cache. Delete `code/.cache/` and rerun to measure a cold run.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
