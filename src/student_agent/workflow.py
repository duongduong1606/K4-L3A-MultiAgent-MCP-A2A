from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PRIMARY_ISSUES = {
    "canceled_order_paid",
    "unavailable_order_paid",
    "late_delivery_seller",
    "late_delivery_logistics",
    "valid_split_payment",
    "payment_mismatch",
    "duplicate_charge",
    "refund_pending",
    "refund_failed",
    "unsupported_claim",
    "insufficient_evidence",
}


async def _consume_evidence(
    *,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    case_id: str,
    actor: str,
    tool_name: str,
    **arguments: str,
) -> dict[str, Any] | None:
    try:
        evidence = await gateway.call(tool_name, case_id=case_id, **arguments)
    except Exception:
        return None

    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool_name,
        evidence_refs=[evidence["evidence_ref"]],
    )
    return evidence


async def _order_item_agent(
    case_id: str,
    order_id: str,
    include_items: bool,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="order_item_agent",
        attributes={"order_id": order_id},
    )
    result = {
        "order": await _consume_evidence(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="order_item_agent",
            tool_name="get_order",
            order_id=order_id,
        ),
    }
    if include_items:
        result["items"] = await _consume_evidence(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="order_item_agent",
            tool_name="get_order_items",
            order_id=order_id,
        )
    return result


async def _payment_agent(
    case_id: str,
    order_id: str,
    include_refund: bool,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="payment_agent",
        attributes={"order_id": order_id},
    )
    result = {
        "timeline": await _consume_evidence(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="payment_agent",
            tool_name="get_payment_timeline",
            order_id=order_id,
        ),
    }
    if include_refund:
        result["refund"] = await _consume_evidence(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="payment_agent",
            tool_name="get_refund_timeline",
            order_id=order_id,
        )
    return result


async def _shipment_agent(
    case_id: str,
    order_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any]:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="shipment_agent",
        attributes={"order_id": order_id},
    )
    return {
        "shipment": await _consume_evidence(
            gateway=gateway,
            trace=trace,
            case_id=case_id,
            actor="shipment_agent",
            tool_name="get_shipment_summary",
            order_id=order_id,
        )
    }


async def _policy_agent(
    case_id: str,
    policy_version: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
) -> dict[str, Any] | None:
    trace.emit(
        case_id=case_id,
        event_type="task_assigned",
        actor="coordinator",
        target="policy_agent",
        attributes={"policy_version": policy_version},
    )
    return await _consume_evidence(
        gateway=gateway,
        trace=trace,
        case_id=case_id,
        actor="policy_agent",
        tool_name="get_policy",
        policy_version=policy_version,
    )


def _data(evidence: dict[str, Any] | None, default: Any) -> Any:
    if not evidence:
        return default
    return evidence.get("data", default)


def _refs(*groups: Any) -> list[str]:
    result: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict) and "evidence_ref" in value:
            ref = value["evidence_ref"]
            if ref not in result:
                result.append(ref)
        elif isinstance(value, dict):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    for group in groups:
        visit(group)
    return result


def _money(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return Decimal("0")


def _payment_rows(evidence: dict[str, Any]) -> list[dict[str, Any]]:
    direct = _data(evidence.get("payment", {}).get("payments"), [])
    if direct:
        return direct
    timeline = _data(evidence.get("payment", {}).get("timeline"), {})
    if isinstance(timeline, dict):
        payments = timeline.get("payments", [])
        if isinstance(payments, list):
            return payments
    return []


def _select_primary_issue(claims: list[dict[str, Any]], evidence: dict[str, Any]) -> str:
    topics = [str(claim.get("topic", "")).lower() for claim in claims]
    for topic in topics:
        if topic in PRIMARY_ISSUES and topic != "insufficient_evidence":
            return topic

    order = _data(evidence.get("order", {}).get("order"), {})
    payments = _payment_rows(evidence)
    shipment = _data(evidence.get("shipment", {}).get("shipment"), {})

    order_status = str(order.get("order_status", "")).lower()
    payment_total = sum(_money(payment.get("payment_value")) for payment in payments)
    if order_status == "canceled" and payment_total > 0:
        return "canceled_order_paid"

    shipment_events = shipment.get("events", []) if isinstance(shipment, dict) else []
    for event in shipment_events:
        if event.get("event_type") == "delivered_late":
            return (
                "late_delivery_seller"
                if event.get("actor") == "seller"
                else "late_delivery_logistics"
            )

    return "insufficient_evidence"


def _entities(order_id: str | None, evidence: dict[str, Any]) -> dict[str, list[str]]:
    items = _data(evidence.get("order", {}).get("items"), [])
    payments = _payment_rows(evidence)

    item_ids = sorted(
        {
            str(item.get("order_item_id"))
            for item in items
            if isinstance(item, dict) and item.get("order_item_id")
        }
    )
    seller_ids = {
        str(item.get("seller_id"))
        for item in items
        if isinstance(item, dict) and item.get("seller_id")
    }
    payment_refs = sorted(
        {
            str(payment.get("payment_sequential"))
            for payment in payments
            if isinstance(payment, dict) and payment.get("payment_sequential")
        }
    )

    return {
        "order_ids": [order_id] if order_id else [],
        "item_ids": item_ids,
        "seller_ids": sorted(seller_ids),
        "payment_references": payment_refs,
        "shipment_ids": [],
    }


def _policy_rule(policy: dict[str, Any] | None, primary_issue: str) -> dict[str, Any]:
    rules = _data(policy, {}).get("rules", {}) if policy else {}
    return rules.get(
        primary_issue,
        {
            "case_status": "needs_investigation",
            "recommended_action": "collect_more_evidence",
            "refund_brl": 0,
            "responsible_parties": [{"party_type": "unknown", "party_id": None}],
        },
    )


def _responsible_parties(
    rule: dict[str, Any],
    primary_issue: str,
    entities: dict[str, list[str]],
) -> list[dict[str, Any]]:
    parties = [
        dict(party)
        for party in rule.get(
            "responsible_parties",
            [{"party_type": "unknown", "party_id": None}],
        )
    ]
    if primary_issue in {"late_delivery_seller", "unavailable_order_paid"}:
        seller_ids = entities.get("seller_ids", [])
        if seller_ids:
            for party in parties:
                if party.get("party_type") == "seller":
                    party["party_id"] = seller_ids[0]
    return parties


def _calibrate_confidence(
    *,
    primary_issue: str,
    policy_evidence: dict[str, Any] | None,
    evidence_refs: list[str],
    has_payment_evidence: bool,
    has_shipment_evidence: bool,
) -> float:
    if primary_issue == "insufficient_evidence":
        return 0.45

    confidence = 0.65

    if policy_evidence:
        confidence += 0.15

    if len(evidence_refs) >= 3:
        confidence += 0.10

    if primary_issue in {
        "canceled_order_paid",
        "unavailable_order_paid",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
        "valid_split_payment",
    } and has_payment_evidence:
        confidence += 0.10

    if primary_issue in {
        "late_delivery_seller",
        "late_delivery_logistics",
    } and has_shipment_evidence:
        confidence += 0.10

    return min(confidence, 0.95)

def _claim_assessments(
    claims: list[dict[str, Any]],
    primary_issue: str,
    refund_brl: float,
    evidence_refs: list[str],
    confidence: float,
) -> list[dict[str, Any]]:
    assessments: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = claim.get("claim_id")
        if not claim_id:
            continue
        topic = str(claim.get("topic", "")).lower()
        verdict = "unsupported"
        if topic == primary_issue:
            verdict = "supported"
        elif topic == "requested_full_refund" and refund_brl > 0:
            verdict = "partially_supported"
        elif primary_issue == "insufficient_evidence":
            verdict = "insufficient_evidence"
        assessments.append(
            {
                "claim_id": claim_id,
                "verdict": verdict,
                "confidence": confidence,
                "evidence_refs": evidence_refs,
            }
        )
    return assessments


def _verify_output(
    output: dict[str, Any],
    trace: TraceWriter,
    case_id: str,
    evidence_refs: list[str],
) -> None:
    if output["case_id"] != case_id:
        raise ValueError(f"output case_id mismatch for {case_id}")

    if not evidence_refs:
        raise ValueError(f"no MCP evidence collected for {case_id}")

    confidence = output["assessment"]["confidence"]
    if not 0 <= confidence <= 1:
        raise ValueError(f"invalid confidence for {case_id}")

    refund = output["financial_resolution"]["recommended_refund_brl"]
    if refund < 0:
        raise ValueError(f"negative refund for {case_id}")

    primary_issue = output["assessment"]["primary_issue"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    party_types = {party["party_type"] for party in parties}

    if primary_issue.startswith("late_delivery") and "payment_provider" in party_types:
        raise ValueError(f"inconsistent party for delivery issue in {case_id}")

    if (
        primary_issue in {"refund_failed", "refund_pending", "duplicate_charge"}
        and "payment_provider" not in party_types
        and "unknown" not in party_types
    ):
        raise ValueError(f"inconsistent party for payment issue in {case_id}")

    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier_agent",
        decision_code="schema_ready",
        evidence_refs=evidence_refs[:20],
        attributes={"primary_issue": primary_issue},
    )


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    case_id = case["case_id"]
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id")
    claims = request.get("claims", [])
    policy_version = case.get("policy_version", "EC_POLICY_V1")
    if not order_id:
        raise ValueError(f"case {case_id} does not include claimed_order_id")

    claim_topics = {str(claim.get("topic", "")).lower() for claim in claims}
    payment_topics = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }
    shipment_topics = {"late_delivery_seller", "late_delivery_logistics"}
    refund_topics = {"refund_pending", "refund_failed"}
    item_topics = {"unavailable_order_paid", "late_delivery_seller"}

    order_evidence = await _order_item_agent(
        case_id,
        order_id,
        bool(claim_topics & item_topics),
        gateway,
        trace,
    )
    payment_evidence = (
        await _payment_agent(
            case_id,
            order_id,
            bool(claim_topics & refund_topics),
            gateway,
            trace,
        )
        if claim_topics & payment_topics
        else {}
    )
    shipment_evidence = (
        await _shipment_agent(case_id, order_id, gateway, trace)
        if claim_topics & shipment_topics
        else {}
    )
    policy_evidence = await _policy_agent(case_id, policy_version, gateway, trace)

    evidence = {
        "order": order_evidence,
        "payment": payment_evidence,
        "shipment": shipment_evidence,
        "policy": policy_evidence,
    }
    evidence_refs = _refs(evidence)
    primary_issue = _select_primary_issue(claims, evidence)
    rule = _policy_rule(policy_evidence, primary_issue)
    refund_brl = float(_money(rule.get("refund_brl")))
    entities = _entities(order_id, evidence)
    confidence = _calibrate_confidence(
        primary_issue=primary_issue,
        policy_evidence=policy_evidence,
        evidence_refs=evidence_refs,
        has_payment_evidence=any(payment_evidence.values()),
        has_shipment_evidence=any(shipment_evidence.values()),
    )
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy_agent",
        decision_code=primary_issue,
        evidence_refs=[policy_evidence["evidence_ref"]] if policy_evidence else evidence_refs[:1],
        attributes={"refund_brl": refund_brl},
    )
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="policy_agent",
        target="verifier_agent",
        decision_code=primary_issue,
        evidence_refs=evidence_refs[:20],
    )

    output = {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case_id,
        "assessment": {
            "primary_issue": primary_issue,
            "case_status": rule.get("case_status", "needs_investigation"),
            "confidence": confidence,
        },
        "affected_entities": entities,
        "claim_assessments": _claim_assessments(
            claims, primary_issue, refund_brl, evidence_refs, confidence
        ),
        "root_cause_analysis": {
            "ranked_causes": [
                {"cause_code": primary_issue.upper(), "rank": 1},
            ],
            "responsible_parties": _responsible_parties(rule, primary_issue, entities),
        },
        "evidence_refs": evidence_refs,
        "data_conflicts": [],
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund_brl,
            "refund_lines": [
                {
                    "reason_code": primary_issue.upper(),
                    "amount_brl": refund_brl,
                    "entity_id": order_id,
                }
            ]
            if refund_brl > 0
            else [],
        },
        "resolution_actions": [
            str(rule.get("recommended_action", "collect_more_evidence")),
        ],
    }
    _verify_output(output, trace, case_id, evidence_refs)
    return output
