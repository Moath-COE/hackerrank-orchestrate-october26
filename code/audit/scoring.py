"""Score predictions against labelled answers (dataset/sample_output.csv).

Metrics:
  status accuracy    — share of cases with the right deletion_status;
  step P / R / F1    — steps compared as whole tuples (phase, system_id,
                       resource_id, operation, state, evidence_id), ignoring
                       step_no, matched as multisets per case;
  field accuracy     — expected steps aligned to predicted ones by
                       (phase, resource_id, operation); for each field, the share
                       of expected steps whose aligned prediction has the same
                       value (an unaligned expected step counts as wrong);
  sequence accuracy  — share of cases whose full ordered step array is exactly
                       right (step_no included), plus mean longest-common-
                       subsequence similarity of the ordered step tuples;
  resource recall    — share of expected (case, resource_id) pairs predicted at all.
"""

import csv
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

TUPLE_FIELDS = ("phase", "system_id", "resource_id", "operation", "state", "evidence_id")
ALIGN_FIELDS = ("phase", "resource_id", "operation")
COMPARED_FIELDS = ("step_no", "system_id", "state", "evidence_id")


def read_answers(path: Path) -> dict[str, tuple[str, list[dict]]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return {row["case_id"]: (row["deletion_status"], json.loads(row["steps_json"])) for row in csv.DictReader(handle)}


def _tuple(step: dict, fields=TUPLE_FIELDS) -> tuple:
    return tuple(step.get(name) for name in fields)


def _lcs(a: list, b: list) -> int:
    table = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i, x in enumerate(a):
        for j, y in enumerate(b):
            table[i + 1][j + 1] = table[i][j] + 1 if x == y else max(table[i][j + 1], table[i + 1][j])
    return table[-1][-1]


@dataclass
class Score:
    cases: int = 0
    status_correct: int = 0
    expected_steps: int = 0
    predicted_steps: int = 0
    matched_steps: int = 0
    field_correct: Counter = field(default_factory=Counter)
    exact_sequences: int = 0
    lcs_total: float = 0.0
    expected_resources: int = 0
    found_resources: int = 0
    confusion: Counter = field(default_factory=Counter)
    case_diffs: dict[str, list[str]] = field(default_factory=dict)

    @property
    def precision(self) -> float:
        return self.matched_steps / self.predicted_steps if self.predicted_steps else 0.0

    @property
    def recall(self) -> float:
        return self.matched_steps / self.expected_steps if self.expected_steps else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def summary(self) -> dict[str, float]:
        n, steps = self.cases or 1, self.expected_steps or 1
        return {
            "cases": self.cases,
            "status_accuracy": self.status_correct / n,
            "step_precision": self.precision,
            "step_recall": self.recall,
            "step_f1": self.f1,
            **{f"field_accuracy.{name}": self.field_correct[name] / steps for name in COMPARED_FIELDS},
            "exact_sequence_accuracy": self.exact_sequences / n,
            "mean_sequence_similarity": self.lcs_total / n,
            "resource_recall": self.found_resources / (self.expected_resources or 1),
        }


def score(predicted: dict[str, tuple[str, list[dict]]], expected: dict[str, tuple[str, list[dict]]]) -> Score:
    result = Score()
    for case_id, (exp_status, exp_steps) in sorted(expected.items()):
        pred_status, pred_steps = predicted.get(case_id, ("MISSING", []))
        result.cases += 1
        result.status_correct += pred_status == exp_status
        result.confusion[(exp_status, pred_status)] += 1

        exp_tuples, pred_tuples = [_tuple(s) for s in exp_steps], [_tuple(s) for s in pred_steps]
        matched = sum((Counter(exp_tuples) & Counter(pred_tuples)).values())
        result.expected_steps += len(exp_tuples)
        result.predicted_steps += len(pred_tuples)
        result.matched_steps += matched

        aligned = {_tuple(s, ALIGN_FIELDS): s for s in pred_steps}
        for step in exp_steps:
            other = aligned.get(_tuple(step, ALIGN_FIELDS))
            for name in COMPARED_FIELDS:
                result.field_correct[name] += other is not None and other.get(name) == step.get(name)

        exact = exp_status == pred_status and [_tuple(s) + (s["step_no"],) for s in exp_steps] == \
            [_tuple(s) + (s["step_no"],) for s in pred_steps]
        result.exact_sequences += exact
        longest = max(len(exp_tuples), len(pred_tuples))
        result.lcs_total += _lcs(exp_tuples, pred_tuples) / longest if longest else 1.0

        exp_resources = {s["resource_id"] for s in exp_steps}
        result.expected_resources += len(exp_resources)
        result.found_resources += len(exp_resources & {s["resource_id"] for s in pred_steps})

        if not exact:
            diff = [f"status expected={exp_status} predicted={pred_status}"]
            diff += [f"  missing   {' '.join(map(str, t))}" for t in (Counter(exp_tuples) - Counter(pred_tuples)).elements()]
            diff += [f"  unexpected {' '.join(map(str, t))}" for t in (Counter(pred_tuples) - Counter(exp_tuples)).elements()]
            result.case_diffs[case_id] = diff
    return result


def format_score(result: Score) -> str:
    lines = [f"{name:>28}: {value:.3f}" if isinstance(value, float) else f"{name:>28}: {value}"
             for name, value in result.summary().items()]
    lines.append("     status confusion (expected -> predicted):")
    lines += [f"        {exp:<15} -> {pred:<15} {count}" for (exp, pred), count in sorted(result.confusion.items())]
    return "\n".join(lines)


def format_diffs(result: Score) -> str:
    return "\n".join(f"{case_id}\n" + "\n".join(lines) for case_id, lines in result.case_diffs.items())
