"""End-to-end run over one request file: ingest, classify, audit every case, write the CSV."""

import csv
import json
import time
from dataclasses import dataclass
from pathlib import Path

from audit.correlation import correlate_workflows
from audit.discovery import identifier_links
from audit.graph import RunContext, build_graph
from audit.identity import resolve_subject
from audit.ingest import run_ingest
from audit.intent import classify_requests, cross_check
from audit.llm import USAGE_LOG
from audit.paths import CACHE_DIR, DATASET_DIR, DB_PATH
from audit.policy import describe, load_rules
from audit.steps import serialize

TRACE_DIR = CACHE_DIR / "traces"


@dataclass
class RunResult:
    rows: list[dict[str, str]]
    problems: dict[str, list[str]]
    output_path: Path
    trace_path: Path
    usage: list[dict]     # one entry per model call made during this run
    seconds: float

    @property
    def llm_calls(self) -> int:
        return len(self.usage)


def case_number(case_id: str) -> int:
    return int(case_id.rsplit("_", 1)[1])


def _usage_entries() -> list[dict]:
    if not USAGE_LOG.exists():
        return []
    return [json.loads(line) for line in USAGE_LOG.open(encoding="utf-8") if line.strip()]


def run(split: str, output_path: Path, allow_model: bool = True, log=print) -> RunResult:
    """Audit every request of a split ('eval' or 'sample') and write output_path."""
    started = time.monotonic()
    calls_before = len(_usage_entries())

    log("1/4 ingesting dataset/ into the audit database ...")
    conn, skipped = run_ingest(DATASET_DIR, DB_PATH, allow_model=allow_model)
    if skipped:
        log(f"    warning: no extraction for {', '.join(skipped)} (run without --no-llm to extract them)")

    # Deletion workflows of sample and evaluation requests share one activity log,
    # so intent and cross-case correlation always cover every request.
    everything = conn.execute("SELECT * FROM requests").fetchall()
    requests = sorted((r for r in everything if r["split"] == split), key=lambda row: case_number(row["case_id"]))
    log(f"2/4 classifying all {len(everything)} requests ({len({r['request_text'] for r in everything})} distinct texts) ...")
    intents = classify_requests([r["request_text"] for r in everything], allow_model=allow_model)
    disagreements = cross_check(conn, everything, intents)
    non_deletion = sum(not intents[r["request_text"]].authorizes_deletion for r in everything)
    log(f"    {non_deletion} non-deletion requests; privacy-intake log disagrees on {len(disagreements)}")
    for line in disagreements:
        log(f"    warning: {line}")

    log(f"3/4 correlating workflows and auditing {len(requests)} {split} cases ...")
    ctx = RunContext(conn=conn, rules=load_rules(conn), intents=intents,
                     workflow_links=_workflow_links(conn, everything, intents))
    graph = build_graph(ctx)
    rows, problems = [], {}
    TRACE_DIR.mkdir(parents=True, exist_ok=True)
    trace_path = TRACE_DIR / f"{split}.jsonl"
    with trace_path.open("w", encoding="utf-8") as trace:
        for request in requests:
            state = graph.invoke({"request": dict(request), "trace": []})
            rows.append({
                "case_id": request["case_id"],
                "deletion_status": state["deletion_status"],
                "steps_json": serialize(state["steps"]),
            })
            if state.get("problems"):
                problems[request["case_id"]] = state["problems"]
            trace.write(json.dumps({
                "case_id": request["case_id"],
                "status": state["deletion_status"],
                "trace": state.get("trace", []),
                "assessments": [describe(a) for a in state.get("assessments", [])],
                "problems": state.get("problems", []),
            }, ensure_ascii=False) + "\n")

    log(f"4/4 writing {output_path} ...")
    write_output(rows, output_path)
    return RunResult(rows, problems, output_path, trace_path, _usage_entries()[calls_before:], time.monotonic() - started)


def _workflow_links(conn, requests, intents) -> dict[str, dict[str, set[str]]]:
    authorized, links = set(), {}
    for request in requests:
        if not intents[request["request_text"]].authorizes_deletion:
            continue
        subject = resolve_subject(conn, request)
        if subject is None:
            continue
        authorized.add(request["case_id"])
        links[request["case_id"]] = set(identifier_links(conn, subject, request["audit_at"]))
    return correlate_workflows(conn, requests, authorized, links)


def write_output(rows: list[dict[str, str]], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["case_id", "deletion_status", "steps_json"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(sorted(rows, key=lambda row: case_number(row["case_id"])))
