"""Per-case audit workflow as a LangGraph state graph.

    START → classify_request ─┬─(not a deletion)──────────────────→ not_applicable → validate_output → END
                              └─(deletion)→ resolve_identity ─┬─(unknown subject)→ unresolved ─┤
                                                              └→ discover_resources → reconstruct_state
                                                                 → apply_policy → plan_steps → validate_output

Every node is deterministic. The one model-backed decision, request intent, is
computed for all requests before the graph runs (batched and cached) and read
here from the run context; work that needs a view across all cases (such as
correlating anonymous deletion-workflow activity) is prepared the same way.
"""

import sqlite3
from dataclasses import dataclass, field
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from audit.discovery import identifier_links
from audit.identity import Subject, resolve_subject
from audit.intent import RequestClassification
from audit.policy import Assessment, assess, case_status, describe
from audit.reconstruct import Reconstructor, ResourceEvidence
from audit.steps import plan_steps, validate_steps


@dataclass
class RunContext:
    conn: sqlite3.Connection
    rules: dict[str, tuple[str, str]]
    intents: dict[str, RequestClassification]                 # request text -> classification
    workflow_links: dict[str, dict[str, set[str]]] = field(default_factory=dict)  # case -> resource -> activity ids
    reconstructor: Reconstructor | None = None

    def __post_init__(self) -> None:
        self.reconstructor = self.reconstructor or Reconstructor(self.conn)


class CaseState(TypedDict, total=False):
    request: dict[str, Any]
    intent: dict[str, Any]
    subject: Subject | None
    links: dict[str, dict[str, list[str]]]          # resource -> {source: evidence ids}
    resources: list[ResourceEvidence]
    assessments: list[Assessment]
    deletion_status: str
    steps: list[dict]
    problems: list[str]
    trace: list[str]


def build_graph(ctx: RunContext):
    def classify_request(state: CaseState) -> CaseState:
        intent = ctx.intents[state["request"]["request_text"]]
        return {"intent": intent.model_dump(exclude={"request_id"}),
                "trace": [f"intent={intent.intent} authorizes_deletion={intent.authorizes_deletion}"]}

    def not_applicable(state: CaseState) -> CaseState:
        return {"deletion_status": "NOT_APPLICABLE", "steps": []}

    def resolve_identity(state: CaseState) -> CaseState:
        subject = resolve_subject(ctx.conn, state["request"])
        note = "subject unresolved" if subject is None else (
            f"subject={subject.user_id} identifiers={len(subject.identifiers)} window_start={subject.window_start}")
        return {"subject": subject, "trace": state["trace"] + [note]}

    def unresolved(state: CaseState) -> CaseState:
        # Nothing can be attributed or verified for an unknown subject.
        return {"deletion_status": "INCOMPLETE", "steps": []}

    def discover_resources(state: CaseState) -> CaseState:
        request = state["request"]
        links: dict[str, dict[str, list[str]]] = {}
        for resource_id, evidence in identifier_links(ctx.conn, state["subject"], request["audit_at"]).items():
            links.setdefault(resource_id, {})["identifier"] = sorted(evidence)
        for resource_id, activity in ctx.workflow_links.get(request["case_id"], {}).items():
            links.setdefault(resource_id, {})["workflow"] = sorted(activity)
        return {"links": links, "trace": state["trace"] + [f"linked={sorted(links)}"]}

    def reconstruct_state(state: CaseState) -> CaseState:
        request = state["request"]
        resources = [
            ctx.reconstructor.resource(rid, state["subject"], request["requested_at"], request["audit_at"])
            for rid in sorted(state["links"])
        ]
        return {"resources": resources}

    def apply_policy(state: CaseState) -> CaseState:
        audit_at = state["request"]["audit_at"]
        assessments = [assess(resource, ctx.rules, audit_at) for resource in state["resources"]]
        status = case_status(assessments)
        notes = [f"{a['resource_id']}: {a['category']}/{a['action']} {a['verdict']} {a['evidence']}"
                 for a in map(describe, assessments)]
        return {"assessments": assessments, "deletion_status": status, "trace": state["trace"] + notes}

    def plan(state: CaseState) -> CaseState:
        return {"steps": plan_steps(state["assessments"], state["deletion_status"])}

    def validate_output(state: CaseState) -> CaseState:
        return {"problems": validate_steps(state["deletion_status"], state["steps"])}

    graph = StateGraph(CaseState)
    graph.add_node("classify_request", classify_request)
    graph.add_node("not_applicable", not_applicable)
    graph.add_node("resolve_identity", resolve_identity)
    graph.add_node("unresolved", unresolved)
    graph.add_node("discover_resources", discover_resources)
    graph.add_node("reconstruct_state", reconstruct_state)
    graph.add_node("apply_policy", apply_policy)
    graph.add_node("plan_steps", plan)
    graph.add_node("validate_output", validate_output)

    graph.add_edge(START, "classify_request")
    graph.add_conditional_edges(
        "classify_request",
        lambda s: "resolve_identity" if s["intent"]["authorizes_deletion"] else "not_applicable",
        ["resolve_identity", "not_applicable"],
    )
    graph.add_conditional_edges(
        "resolve_identity",
        lambda s: "discover_resources" if s["subject"] is not None else "unresolved",
        ["discover_resources", "unresolved"],
    )
    graph.add_edge("discover_resources", "reconstruct_state")
    graph.add_edge("reconstruct_state", "apply_policy")
    graph.add_edge("apply_policy", "plan_steps")
    graph.add_edge("plan_steps", "validate_output")
    graph.add_edge("not_applicable", "validate_output")
    graph.add_edge("unresolved", "validate_output")
    graph.add_edge("validate_output", END)
    return graph.compile()
