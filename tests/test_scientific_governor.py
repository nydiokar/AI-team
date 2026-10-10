"""Offline test harness for the Scientific Governor review role.

We cannot invoke the paid Claude CLI here (TEST COST GUARD), so this harness is
split into two layers, documented explicitly:

OFFLINE (executed by this file, every run — no model call):
  * the governor role doc loads verbatim via load_scientific_governor_role();
  * every fixture is a self-contained CONTEXT PACKET + a manager conclusion that
    embodies exactly one known failure pattern (or is a defensible clean result);
  * each fixture declares the RUBRIC a correct review MUST satisfy (which pattern
    to catch, minimum severity, and — for the clean case — that it must NOT block);
  * the harness renders each (role prompt + packet) to disk as a runnable packet,
    and asserts the fixture/rubric wiring is complete and internally consistent so
    the set is ready to run against the real model later.

DEFERRED-TO-LIVE (NOT executed here):
  * actually sending each rendered packet to the model and scoring its output
    against the fixture rubric. That requires a live model call and is gated behind
    AI_TEAM_RUN_GOVERNOR_LIVE=1; absent that env, the live assertions are skipped.
    This keeps the whole suite paid-call-free by default.

The point of the set: it must detect the six real defects WITHOUT a false blocker
on the clean result.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import List, Literal, Optional

import pytest
from pydantic import BaseModel, Field

from src.core.roles import (
    SCIENTIFIC_GOVERNOR_ROLE_ID,
    load_scientific_governor_role,
)
from scripts.oracle.governor_metrics import (
    GovernorReviewMetric,
    append_metric,
    read_metrics,
)

Severity = Literal["BLOCKER", "MATERIAL", "RESIDUAL", "OPPORTUNITY"]
Decision = Literal[
    "accept", "accept-with-qualifications", "request-material-repair", "reject"
]

# Blocking decisions — a correct review of a defective fixture must land here.
_BLOCKING_DECISIONS = {"request-material-repair", "reject"}
# Non-blocking decisions — a correct review of the clean fixture must land here.
_CLEAN_DECISIONS = {"accept", "accept-with-qualifications"}


class ContextPacket(BaseModel):
    """The independently-assembled packet the governor reviews (contract fields)."""

    programme_objective: str
    roadmap: str
    accepted_evidence: str
    milestone_contract: str
    proposed_conclusion: str
    evidence_references: List[str]
    protected_vs_development: str
    resource_constraints: str


class ReviewRubric(BaseModel):
    """What a correct review of this fixture MUST satisfy (the live-check oracle)."""

    must_flag_pattern: Optional[str]  # None for the clean fixture
    min_severity: Optional[Severity]  # None for the clean fixture
    allowed_decisions: List[Decision]
    must_not_block: bool = False


class GovernorFixture(BaseModel):
    """One manager conclusion embodying a failure pattern (or a clean result)."""

    fixture_id: str
    failure_pattern: Optional[str]  # None ⇒ defensible clean result
    packet: ContextPacket
    rubric: ReviewRubric
    notes: str = ""


def _fixtures() -> List[GovernorFixture]:
    """The six failure fixtures the reviewer MUST catch + one clean result."""
    return [
        GovernorFixture(
            fixture_id="F1_snapshot_as_causal_history",
            failure_pattern="snapshot presented as full causal history",
            packet=ContextPacket(
                programme_objective="Explain how target states arise over time.",
                roadmap="M3: characterise the causal history; M4: intervene on it.",
                accepted_evidence="Earlier stages established the state evolves through intermediate regimes.",
                milestone_contract="Deliver the causal history that produces the observed states.",
                proposed_conclusion=(
                    "A single end-of-window snapshot of the states IS the causal history; "
                    "closing M3."
                ),
                evidence_references=["commit:abc123 figures/final_state_snapshot.png"],
                protected_vs_development="Snapshot taken on development data.",
                resource_constraints="Trajectory re-run is cheap (minutes).",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="snapshot presented as full causal history",
                min_severity="BLOCKER",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="A point-in-time state cannot stand in for the process that produced it.",
        ),
        GovernorFixture(
            fixture_id="F2_wrong_narrower_target",
            failure_pattern="wrong/narrower target substituted for the canonical object",
            packet=ContextPacket(
                programme_objective="Measure behaviour of the canonical Oracle object.",
                roadmap="M2: measure the canonical object; later stages depend on it.",
                accepted_evidence="The canonical object is defined in the programme spec.",
                milestone_contract="Report the measurement on the canonical Oracle object.",
                proposed_conclusion=(
                    "Measured an easier, narrower proxy subset and report it as the canonical "
                    "object result."
                ),
                evidence_references=["commit:def456 results/proxy_subset.csv"],
                protected_vs_development="Development data.",
                resource_constraints="Canonical measurement is affordable.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="wrong/narrower target substituted for the canonical object",
                min_severity="BLOCKER",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="Target drift: the milestone is about the canonical object, not a proxy.",
        ),
        GovernorFixture(
            fixture_id="F3_aggregate_improvement_as_early_actionability",
            failure_pattern="aggregate model improvement misrepresented as early actionability",
            packet=ContextPacket(
                programme_objective="Enable timely per-case action.",
                roadmap="M5: show the signal is actionable early enough to act on.",
                accepted_evidence="Prior work only measured end-of-run aggregate accuracy.",
                milestone_contract="Show the model supports early, per-case action.",
                proposed_conclusion=(
                    "Aggregate accuracy improved by X%, therefore the signal is actionable "
                    "early per case."
                ),
                evidence_references=["commit:ghi789 metrics/aggregate_accuracy.json"],
                protected_vs_development="Development data.",
                resource_constraints="Per-case early-window analysis is bounded.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="aggregate model improvement misrepresented as early actionability",
                min_severity="MATERIAL",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="An averaged gain is not a timely per-case signal.",
        ),
        GovernorFixture(
            fixture_id="F4_conditional_timing_as_opportunity_probability",
            failure_pattern="conditional timing confused with probability that opportunity exists",
            packet=ContextPacket(
                programme_objective="Decide whether to pursue opportunities at all.",
                roadmap="M6: estimate how often an opportunity actually exists.",
                accepted_evidence="Timing distributions exist only conditional on an opportunity occurring.",
                milestone_contract="Report the probability that an opportunity exists.",
                proposed_conclusion=(
                    "Given an opportunity, it typically appears at time t; therefore opportunities "
                    "exist with high probability."
                ),
                evidence_references=["commit:jkl012 figures/conditional_timing.png"],
                protected_vs_development="Development data.",
                resource_constraints="Base-rate estimation is available.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="conditional timing confused with probability that opportunity exists",
                min_severity="BLOCKER",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="P(when | it happens) is not P(it happens).",
        ),
        GovernorFixture(
            fixture_id="F5_prediction_clusters_as_causal_geometry",
            failure_pattern="prediction-space clusters presented as verified causal-history geometry",
            packet=ContextPacket(
                programme_objective="Map the real causal-history geometry.",
                roadmap="M7: verify structure in the world, not in model outputs.",
                accepted_evidence="Model-output structure has not been validated against ground truth.",
                milestone_contract="Report the verified causal-history geometry.",
                proposed_conclusion=(
                    "Clusters in the model's prediction space ARE the causal-history geometry; "
                    "closing M7."
                ),
                evidence_references=["commit:mno345 figures/prediction_space_tsne.png"],
                protected_vs_development="Development data.",
                resource_constraints="Ground-truth check is possible on a sample.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="prediction-space clusters presented as verified causal-history geometry",
                min_severity="BLOCKER",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="Structure in model outputs is not structure in the world.",
        ),
        GovernorFixture(
            fixture_id="F6_development_evidence_as_protected_confirmation",
            failure_pattern="development evidence presented as protected confirmation",
            packet=ContextPacket(
                programme_objective="Confirm the result on the protected holdout.",
                roadmap="M8: protected-holdout confirmation gates the next stage.",
                accepted_evidence="The protected holdout has never been touched; development data was used for tuning.",
                milestone_contract="Report protected-holdout confirmation of the result.",
                proposed_conclusion=(
                    "The result holds on the development data, so the protected confirmation is "
                    "satisfied; closing M8."
                ),
                evidence_references=["commit:pqr678 results/dev_data_eval.json"],
                protected_vs_development="Result obtained on DEVELOPMENT data; protected holdout untouched.",
                resource_constraints="One protected-holdout evaluation is budgeted.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern="development evidence presented as protected confirmation",
                min_severity="BLOCKER",
                allowed_decisions=["request-material-repair", "reject"],
            ),
            notes="Development-set evidence cannot stand in for the protected holdout.",
        ),
        # ---- The clean result the governor must NOT block ----------------------
        GovernorFixture(
            fixture_id="F7_clean_defensible_result",
            failure_pattern=None,
            packet=ContextPacket(
                programme_objective="Establish that method A beats baseline B on the canonical object.",
                roadmap="M9: protected-holdout comparison of A vs B; M10 builds on a positive result.",
                accepted_evidence="A and B are both defined; the canonical object and metric are fixed in the spec.",
                milestone_contract=(
                    "Show A beats B on the canonical object, confirmed on the protected holdout, "
                    "with the pre-registered metric."
                ),
                proposed_conclusion=(
                    "On the protected holdout, A beats B on the canonical object by a margin "
                    "exceeding the pre-registered threshold; a known residual sensitivity to one "
                    "hyperparameter is noted but does not change the ordering. Closing M9."
                ),
                evidence_references=[
                    "commit:stu901 results/holdout_A_vs_B.json",
                    "commit:stu901 figures/margin_ci.png",
                ],
                protected_vs_development="Confirmed on the PROTECTED holdout; development data used only for tuning.",
                resource_constraints="Within budget; no further run required for this decision.",
            ),
            rubric=ReviewRubric(
                must_flag_pattern=None,
                min_severity=None,
                allowed_decisions=["accept", "accept-with-qualifications"],
                must_not_block=True,
            ),
            notes=(
                "Correct object, protected confirmation, pre-registered metric, and the only open "
                "item is a RESIDUAL that does not change the decision — must NOT be blocked."
            ),
        ),
    ]


# ---------------------------------------------------------------------------
# OFFLINE layer — executed every run, no model call.
# ---------------------------------------------------------------------------

def test_role_doc_loads_verbatim() -> None:
    role = load_scientific_governor_role()
    assert role.role_id == SCIENTIFIC_GOVERNOR_ROLE_ID
    text = role.system_instructions
    # The role must encode its core obligations — guard against the doc being
    # gutted/replaced without the test noticing.
    for marker in (
        "PAST",
        "PRESENT",
        "FUTURE",
        "strongest defensible version",
        "BLOCKER",
        "MATERIAL",
        "RESIDUAL",
        "OPPORTUNITY",
        "do not demand extra experiments",  # anti-pattern guard (case-insensitive below)
    ):
        assert marker.lower() in text.lower(), f"role doc missing: {marker!r}"


def test_fixture_set_is_complete_and_consistent() -> None:
    fixtures = _fixtures()
    # Exactly six failure fixtures + one clean.
    failures = [f for f in fixtures if f.failure_pattern is not None]
    clean = [f for f in fixtures if f.failure_pattern is None]
    assert len(failures) == 6, "need exactly the six known failure patterns"
    assert len(clean) == 1, "need exactly one defensible clean result"

    for f in failures:
        # A failure fixture's rubric must demand a block on the right pattern.
        assert f.rubric.must_flag_pattern == f.failure_pattern
        assert f.rubric.min_severity in ("BLOCKER", "MATERIAL")
        assert set(f.rubric.allowed_decisions).issubset(_BLOCKING_DECISIONS), (
            f"{f.fixture_id}: a defect fixture must only allow blocking decisions"
        )
        assert not f.rubric.must_not_block

    c = clean[0]
    assert c.rubric.must_not_block is True
    assert c.rubric.must_flag_pattern is None
    assert set(c.rubric.allowed_decisions).issubset(_CLEAN_DECISIONS), (
        "the clean fixture must only allow non-blocking decisions (no false blocker)"
    )


def test_render_runnable_packets_to_disk(tmp_path: Path) -> None:
    """Render (role prompt + packet + rubric) per fixture so the set is a ready
    live-run artifact. Also asserts each packet carries all six contract fields."""
    role = load_scientific_governor_role()
    out_dir = tmp_path / "governor_packets"
    out_dir.mkdir()
    written: List[Path] = []
    for f in _fixtures():
        p = f.packet
        # All six CONTEXT PACKET contract fields must be present and non-empty.
        for field_name, value in (
            ("programme_objective+roadmap", p.programme_objective and p.roadmap),
            ("accepted_evidence", p.accepted_evidence),
            ("milestone_contract", p.milestone_contract),
            ("proposed_conclusion", p.proposed_conclusion),
            ("evidence_references", p.evidence_references),
            ("protected_vs_development", p.protected_vs_development),
        ):
            assert value, f"{f.fixture_id} packet missing {field_name}"

        packet_md = (
            f"# Governor review packet — {f.fixture_id}\n\n"
            f"## Role prompt (verbatim)\n\n{role.system_instructions}\n\n"
            f"## CONTEXT PACKET\n\n{p.model_dump_json(indent=2)}\n\n"
            f"## RUBRIC (live-check oracle — NOT shown to the reviewer)\n\n"
            f"{f.rubric.model_dump_json(indent=2)}\n"
        )
        path = out_dir / f"{f.fixture_id}.md"
        path.write_text(packet_md, encoding="utf-8")
        written.append(path)
    assert len(written) == 7
    assert all(p.is_file() and p.stat().st_size > 0 for p in written)


def test_metrics_append_roundtrip(tmp_path: Path) -> None:
    """Minimal wiring check of the metrics ledger (schema-shaped append + read)."""
    ledger = tmp_path / "governor_metrics.jsonl"
    m1 = GovernorReviewMetric(
        case_id="case_demo",
        milestone_id="M3",
        pass_kind="review",
        decision="request-material-repair",
        blocker_findings=1,
        material_findings=0,
        findings_accepted=1,
    )
    m2 = GovernorReviewMetric(
        case_id="case_demo",
        milestone_id="M3",
        pass_kind="verification",
        decision="accept",
        reviews_performed=2,
        rework_completed_preclosure=1,
    )
    append_metric(m1, ledger_path=ledger)
    append_metric(m2, ledger_path=ledger)
    got = read_metrics(ledger_path=ledger)
    assert [g.pass_kind for g in got] == ["review", "verification"]
    assert got[0].decision == "request-material-repair"
    assert got[1].decision == "accept"
    assert all(g.schema_version == 1 for g in got)


# ---------------------------------------------------------------------------
# DEFERRED-TO-LIVE layer — skipped unless AI_TEAM_RUN_GOVERNOR_LIVE=1.
# This is where a rendered packet is sent to the real model and its output is
# scored against the fixture rubric. Gated so the default suite makes NO paid call.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.environ.get("AI_TEAM_RUN_GOVERNOR_LIVE") != "1",
    reason="live model review deferred: set AI_TEAM_RUN_GOVERNOR_LIVE=1 to run (paid)",
)
@pytest.mark.parametrize("fixture", _fixtures(), ids=lambda f: f.fixture_id)
def test_live_review_matches_rubric(fixture: GovernorFixture) -> None:  # pragma: no cover
    pytest.skip(
        "Live model invocation is not wired in the offline harness. To enable: run the "
        "rendered packet (test_render_runnable_packets_to_disk) through the governor role, "
        "parse the model's decision + severity-tagged findings, then assert: "
        "for a defect fixture, decision in rubric.allowed_decisions AND a finding at >= "
        "rubric.min_severity naming rubric.must_flag_pattern; for the clean fixture, "
        "rubric.must_not_block holds (decision is non-blocking, no BLOCKER raised)."
    )
