"""Ingest operational log exports.

Every line becomes (timestamp, fields) regardless of format:
  bracketed  2026-07-28T05:51:18Z [DEBUG] [account_service] [tr-1] event: k="v" ...
  pipe       2026-07-03T07:51:32Z | DEBUG | security_service | tr-1 | event | k="v" ...
  syslog     2026-03-25T08:51:15Z <15>1 node-1 support_platform tr-1 - - k="v" ...
  json       2026-04-19T06:51:13Z {"service": ..., "event": ..., ...}
  cloud      2026-06-08T06:52:09Z requestId=tr-1 level=INFO function=svc message="k=\\"v\\" ..."
  key=value  2026-09-15T08:51:45Z level=DEBUG service=svc trace_id=tr-1 event=... k=v ...

A lookup is spread over several lines: the *_started line carries the identifier
being searched (lookup_key, match_key, subject_key, ...) and a later line with
the same trace_id carries the resource and its outcome. Lines are grouped by
trace so each outcome observation also records which identifiers led to it.
"""

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path
from typing import Any

from audit.ingest.common import Observation, evidence_files, infer_state, insert_observation

KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')
BRACKETED_RE = re.compile(r"\[(?P<level>\w+)\] \[(?P<service>[\w-]+)\] \[(?P<trace>[\w-]+)\] (?P<event>\w+):?\s*(?P<rest>.*)")
SYSLOG_RE = re.compile(r"<\d+>\d+ \S+ (?P<service>\S+) (?P<trace>\S+) - - (?P<rest>.*)")

LOOKUP_KEYS = ("lookup_key", "match_key", "subject_key", "subject_ref", "scan_key", "owner_key")
# Transport/runtime detail that says nothing about the data itself.
NOISE_KEYS = {"level", "severity", "logger", "instance", "pod", "region", "attempt", "duration_ms",
              "latency_ms", "took_ms", "shard", "seq", "trace_id", "service", "event"}


def _kv(text: str) -> dict[str, str]:
    return {
        key: value[1:-1].replace('\\"', '"') if value.startswith('"') else value
        for key, value in KV_RE.findall(text)
    }


def parse_line(line: str) -> tuple[str, dict[str, Any]] | None:
    line = line.strip()
    if not line or " " not in line:
        return None
    timestamp, rest = line.split(" ", 1)

    if rest.startswith("{"):
        fields = json.loads(rest)
        fields.setdefault("service", fields.get("logger"))
        return timestamp, fields

    if rest.startswith("["):
        match = BRACKETED_RE.match(rest)
        fields = _kv(match["rest"])
        fields.update(level=match["level"], service=match["service"], trace_id=match["trace"], event=match["event"])
        return timestamp, fields

    if rest.startswith("| "):
        parts = [part.strip() for part in rest[2:].split(" | ", 4)]
        fields = _kv(parts[4]) if len(parts) > 4 else {}
        fields.update(level=parts[0], service=parts[1], trace_id=parts[2], event=parts[3])
        return timestamp, fields

    if rest.startswith("<"):
        match = SYSLOG_RE.match(rest)
        fields = _kv(match["rest"])
        fields.update(service=match["service"], trace_id=match["trace"])
        return timestamp, fields

    fields = _kv(rest)
    if "requestId" in fields and "message" in fields:
        fields.update(_kv(fields.pop("message")))
        fields["trace_id"] = fields.pop("requestId")
        fields["service"] = fields.pop("function", None)
    return timestamp, fields


def load_log_file(conn: sqlite3.Connection, evidence_id: str, path: Path, captured_at: str) -> None:
    events: list[tuple[int, str, dict[str, Any]]] = []
    trace_keys: dict[str, set[str]] = defaultdict(set)

    for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = parse_line(line)
        if parsed is None:
            continue
        timestamp, fields = parsed
        events.append((line_no, timestamp, fields))
        trace = fields.get("trace_id")
        if trace:
            for key in LOOKUP_KEYS:
                if fields.get(key) and fields[key] != "null":
                    trace_keys[trace].add(fields[key])

    for line_no, timestamp, fields in events:
        event = fields.get("event") or ""
        if not fields.get("resource_id") or event.endswith("_started"):
            continue
        record = {key: value for key, value in fields.items() if key not in NOISE_KEYS}
        record.pop("resource_id")
        record["event"] = event
        keys = trace_keys.get(fields.get("trace_id") or "", set())
        if keys:
            record["trace_lookup_keys"] = sorted(keys)
        obs = Observation(
            evidence_id=evidence_id, evidence_kind="file", captured_at=captured_at, observed_at=timestamp,
            system_id=record.pop("system", None) or fields.get("service"),
            resource_id=fields["resource_id"], store=fields.get("service"),
            record_type=record.pop("record_type", None), state=infer_state(record),
            locator=f"{path.name}:{line_no}",
        )
        insert_observation(conn, obs.fill_from_record(record))


def load_logs(conn: sqlite3.Connection, dataset_dir: Path) -> int:
    files = evidence_files(conn, ".log")
    for file in files:
        load_log_file(conn, file["file_id"], dataset_dir / file["file_path"], file["captured_at"])
    return len(files)
