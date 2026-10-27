"""Ghost Data Still Exists — deletion-audit agent CLI.

Usage (from the repository root):
    uv run --project code python code/main.py run        # audit the 518 evaluation requests -> output.csv
    uv run --project code python code/main.py sample     # audit the 32 sample requests -> output_sample.csv, then score
    uv run --project code python code/main.py score      # re-score output_sample.csv against the sample answers
    uv run --project code python code/main.py evaluate   # sample scoring + hand-built evaluation cases
    uv run --project code python code/main.py ingest     # only rebuild the audit database

Add --no-llm to run, sample or ingest to forbid model calls and use the on-disk cache only.
"""

import argparse
import sys

from audit.ingest import run_ingest
from audit.llm import LLMUnavailable
from audit.paths import CACHE_DIR, CODE_DIR, DATASET_DIR, DB_PATH, OUTPUT_CSV, SAMPLE_OUTPUT_CSV
from audit.pipeline import run
from audit.scoring import format_diffs, format_score, read_answers, score
from audit.usage import write_usage_report

USAGE_REPORT = CODE_DIR / "evaluation" / "usage_report.md"
SAMPLE_DIFF = CACHE_DIR / "sample_diff.txt"


def cmd_ingest(args: argparse.Namespace) -> None:
    conn, skipped = run_ingest(DATASET_DIR, DB_PATH, allow_model=not args.no_llm)
    for table in ("requests", "identifiers", "observations", "obs_identifiers", "coverage"):
        count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"{table:>16}: {count}")
    if skipped:
        print(f"skipped {len(skipped)} PDF/PNG files with no cached extraction: {', '.join(skipped)}")
    print(f"database written to {DB_PATH}")


def _report_run(result) -> None:
    statuses: dict[str, int] = {}
    for row in result.rows:
        statuses[row["deletion_status"]] = statuses.get(row["deletion_status"], 0) + 1
    print(f"wrote {len(result.rows)} rows to {result.output_path}")
    print("statuses: " + ", ".join(f"{name}={count}" for name, count in sorted(statuses.items())))
    print(f"model calls this run: {result.llm_calls}; elapsed {result.seconds:.1f}s; per-case traces in {result.trace_path}")
    if result.problems:
        print(f"contract problems in {len(result.problems)} cases:")
        for case_id, problems in sorted(result.problems.items()):
            print(f"  {case_id}: {'; '.join(problems)}")


def cmd_run(args: argparse.Namespace) -> None:
    result = run("eval", OUTPUT_CSV, allow_model=not args.no_llm)
    _report_run(result)
    classified = len(read_answers(DATASET_DIR / "sample_output.csv")) + len(result.rows)
    write_usage_report(USAGE_REPORT, result.usage, len(result.rows), classified, result.seconds)
    print(f"usage report written to {USAGE_REPORT}")


def _score_sample() -> None:
    expected = read_answers(DATASET_DIR / "sample_output.csv")
    predicted = read_answers(SAMPLE_OUTPUT_CSV)
    result = score(predicted, expected)
    print(format_score(result))
    SAMPLE_DIFF.write_text(format_diffs(result) + "\n", encoding="utf-8")
    print(f"{len(result.case_diffs)} cases differ from the answers; per-case differences in {SAMPLE_DIFF}")


def cmd_sample(args: argparse.Namespace) -> None:
    result = run("sample", SAMPLE_OUTPUT_CSV, allow_model=not args.no_llm)
    _report_run(result)
    print()
    _score_sample()


def cmd_score(args: argparse.Namespace) -> None:
    _score_sample()


def cmd_evaluate(args: argparse.Namespace) -> None:
    from evaluation.custom_cases import run_audit_cases, run_classifier_cases

    cmd_sample(args)
    for title, results in (
        ("custom audit cases", run_audit_cases()),
        ("custom classifier cases", run_classifier_cases(allow_model=not args.no_llm)),
    ):
        passed = sum(result.passed for result in results)
        print(f"\n{title}: {passed}/{len(results)} passed")
        for result in results:
            print(f"  [{'PASS' if result.passed else 'FAIL'}] {result.name} — {result.checks}")
            for line in result.details:
                print(f"         {line}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name, func, help_text in (
        ("run", cmd_run, "audit dataset/deletion_requests.csv and write output.csv"),
        ("sample", cmd_sample, "audit dataset/sample_deletion_requests.csv, write output_sample.csv and score it"),
        ("ingest", cmd_ingest, "rebuild the audit database from dataset/"),
        ("evaluate", cmd_evaluate, "score the samples and run the hand-built evaluation cases"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--no-llm", action="store_true", help="never call the model; use cached results only")
        command.set_defaults(func=func)
    commands.add_parser("score", help="score output_sample.csv against dataset/sample_output.csv").set_defaults(func=cmd_score)
    args = parser.parse_args()
    try:
        args.func(args)
    except LLMUnavailable as error:
        sys.exit(f"error: {error}")


if __name__ == "__main__":
    main()
