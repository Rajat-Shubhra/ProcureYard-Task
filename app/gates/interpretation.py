from __future__ import annotations

from app.agent.contracts import ModelInterpretation
from app.gates.reports import GateReport, RuleResult

CHECKER_VERSION = "interpretation-gate-v1"
BLOCKING_SAFETY_FLAGS = {
    "identity_switch_attempt",
    "cross_account_reference",
    "sensitive_data_request",
}


class InterpretationGate:
    def check(
        self,
        interpretation: ModelInterpretation,
        *,
        open_dispute_ids: set[str],
    ) -> GateReport:
        results: list[RuleResult] = []
        global_blocks: list[str] = []
        accepted: list[int] = []

        schema_ok = interpretation.schema_version == "2.0.0"
        results.append(
            _rule(
                "interpretation.schema_version",
                schema_ok,
                "Model output uses the active v2 extraction contract.",
                "Unsupported model schema version.",
            )
        )
        if not schema_ok:
            global_blocks.append("unsupported_model_schema")

        flags = set(interpretation.safety_flags)
        safety_shape_ok = not ("none" in flags and len(flags) > 1)
        results.append(
            _rule(
                "interpretation.safety_flag_shape",
                safety_shape_ok,
                "Safety flags are internally consistent.",
                "Safety flag none cannot be combined with another flag.",
            )
        )
        if not safety_shape_ok:
            global_blocks.append("invalid_safety_flags")

        blocking_flags = sorted(flags & BLOCKING_SAFETY_FLAGS)
        identity_safe = not blocking_flags
        results.append(
            _rule(
                "identity.envelope_authority",
                identity_safe,
                "No identity or cross-account override is present.",
                "Identity/cross-account content cannot enter evidence or action flow.",
                {"flags": blocking_flags},
            )
        )
        if not identity_safe:
            global_blocks.append("identity_or_privacy_risk")

        prompt_injection = "prompt_injection" in flags
        results.append(
            RuleResult(
                rule_id="security.prompt_injection",
                status="fail" if prompt_injection else "pass",
                detail=(
                    "Prompt-injection text is untrusted and cannot authorize an action."
                    if prompt_injection
                    else "No prompt-injection flag was detected."
                ),
            )
        )
        if prompt_injection:
            global_blocks.append("prompt_injection")

        indexes = [item.complaint_index for item in interpretation.complaints]
        indexes_unique = len(indexes) == len(set(indexes))
        results.append(
            _rule(
                "complaints.unique_indexes",
                indexes_unique,
                "Complaint indexes are unique.",
                "Duplicate complaint indexes would overwrite independent complaints.",
            )
        )
        if not indexes_unique:
            global_blocks.append("duplicate_complaint_index")

        for position, complaint in enumerate(interpretation.complaints):
            prefix = f"complaint.{position}"
            relation_ok = (
                complaint.relation_kind == "new" and complaint.target_dispute_id is None
            ) or (
                complaint.relation_kind in {"follow_up", "revision"}
                and complaint.target_dispute_id in open_dispute_ids
            )
            results.append(
                _rule(
                    f"{prefix}.relation",
                    relation_ok,
                    "Complaint relation targets a valid open dispute or is new.",
                    "Follow-up/revision target is missing, closed, or belongs to another context.",
                )
            )
            group_ok = (
                complaint.proposed_policy_cause == complaint.complaint_type
                and complaint.proposed_service_date_ist == complaint.service_date_ist
            )
            results.append(
                _rule(
                    f"{prefix}.grouping_proposal",
                    group_ok,
                    "Grouping proposal matches the complaint's date and cause.",
                    "Grouping proposal conflicts with the extracted complaint.",
                )
            )
            grounded = (
                complaint.grounding_status == "grounded"
                and not complaint.missing_fields
                and complaint.complaint_type != "unknown"
            )
            results.append(
                _rule(
                    f"{prefix}.grounding",
                    grounded,
                    "Complaint has enough grounding for deterministic evidence lookup.",
                    "Complaint needs one focused clarification before evidence lookup.",
                    {"missing_fields": list(complaint.missing_fields)},
                )
            )
            if not global_blocks and relation_ok and group_ok and grounded:
                accepted.append(complaint.complaint_index)

        blocking = list(global_blocks)
        rejected = sorted(set(indexes) - set(accepted))
        if rejected:
            blocking.append("complaints_need_clarification")
        return GateReport(
            gate="interpretation",
            checker_version=CHECKER_VERSION,
            results=tuple(results),
            blocking_reasons=tuple(dict.fromkeys(blocking)),
            accepted_complaint_indexes=tuple(accepted),
        )


def _rule(
    rule_id: str,
    passed: bool,
    pass_detail: str,
    fail_detail: str,
    details: dict[str, object] | None = None,
) -> RuleResult:
    return RuleResult(
        rule_id=rule_id,
        status="pass" if passed else "fail",
        detail=pass_detail if passed else fail_detail,
        details=details or {},
    )
