"""Create immutable RFQ revisions from explicit human clarifications."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation

from trade_agent.contracts import (
    CanonicalUnit,
    FactOrigin,
    FieldFact,
    RFQItem,
    RFQRevision,
)

type ClarificationAnswers = Mapping[str, Mapping[str, object]]


def _quantity_answer(value: object) -> tuple[Decimal, str, str]:
    if isinstance(value, (bool, float)) or not isinstance(value, (str, int, Decimal)):
        raise ValueError("quantity answer must be a finite positive decimal or integer")
    raw_value = str(value).strip()
    try:
        quantity = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ValueError("quantity answer must be a finite positive number") from exc
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("quantity answer must be a finite positive number")
    return quantity, raw_value, format(quantity.normalize(), "f")


def _unit_answer(value: object) -> tuple[CanonicalUnit, str, str]:
    raw_value = value.value if isinstance(value, CanonicalUnit) else value
    if not isinstance(raw_value, str):
        raise ValueError("unit answer must be a supported canonical unit")
    raw_value = raw_value.strip()
    try:
        unit = CanonicalUnit(raw_value)
    except ValueError as exc:
        raise ValueError("unit answer must be a supported canonical unit") from exc
    return unit, raw_value, unit.value


def _specification_answer(value: object) -> tuple[str, str, str]:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("specification answer must be non-empty text")
    normalized_value = value.strip()
    return normalized_value, normalized_value, normalized_value


def _replace_fact(
    facts: tuple[FieldFact, ...], replacement: FieldFact
) -> tuple[FieldFact, ...]:
    facts = tuple(
        replacement if fact.field_name == replacement.field_name else fact
        for fact in facts
    )
    if any(fact.field_name == replacement.field_name for fact in facts):
        return facts
    return (*facts, replacement)


def _apply_item_answers(  # noqa: PLR0914 - field-specific updates need distinct values
    item: RFQItem,
    answers: Mapping[str, object],
    *,
    actor_id: str,
    confirmed_at: datetime,
) -> RFQItem:
    missing = set(item.missing_fields)
    unresolved_fields = {
        *(("quantity",) if item.quantity is None else ()),
        *(("unit",) if item.unit_canonical is None else ()),
        *(spec.name for spec in item.specifications if spec.value is None),
    }
    invalid_fields = set(answers) - missing
    if invalid_fields:
        raise ValueError(
            f"answers include fields that are not missing: {sorted(invalid_fields)}"
        )
    unknown_fields = set(answers) - unresolved_fields
    if unknown_fields:
        raise ValueError(
            f"answers include unknown missing fields: {sorted(unknown_fields)}"
        )

    item_data = item.model_dump(mode="python")
    facts = item.facts
    resolved: set[str] = set()
    specifications = list(item.specifications)

    for field_name, answer in answers.items():
        if field_name == "quantity":
            quantity, raw_value, normalized_value = _quantity_answer(answer)
            item_data["quantity"] = quantity
            fact_name = "quantity"
        elif field_name == "unit":
            unit, raw_value, normalized_value = _unit_answer(answer)
            item_data["unit_canonical"] = unit
            item_data["unit_raw"] = raw_value
            fact_name = "unit"
        else:
            normalized_value, raw_value, _ = _specification_answer(answer)
            specifications = [
                spec.model_copy(update={"value": normalized_value})
                if spec.name == field_name
                else spec
                for spec in specifications
            ]
            fact_name = f"specifications.{field_name}"

        prior_fact = next(
            (fact for fact in item.facts if fact.field_name == fact_name), None
        )
        replacement = FieldFact(
            field_name=fact_name,
            origin=FactOrigin.USER_CONFIRMED,
            raw_value=raw_value,
            normalized_value=normalized_value,
            confirmed_at=confirmed_at,
            confirmed_by=actor_id,
            evidence=prior_fact.evidence if prior_fact is not None else (),
        )
        facts = _replace_fact(facts, replacement)
        resolved.add(field_name)

    item_data["specifications"] = tuple(specifications)
    item_data["facts"] = facts
    item_data["missing_fields"] = tuple(
        field for field in item.missing_fields if field not in resolved
    )
    return RFQItem.model_validate(item_data)


def _updated_items(
    current_revision: RFQRevision,
    answers: ClarificationAnswers,
    *,
    actor_id: str,
    confirmed_at: datetime,
) -> tuple[RFQItem, ...]:
    item_ids = {item.rfq_item_id for item in current_revision.plan.items}
    unknown_item_ids = set(answers) - item_ids
    if unknown_item_ids:
        raise ValueError(
            f"answers include unknown RFQ items: {sorted(unknown_item_ids)}"
        )

    updated_items = []
    for item in current_revision.plan.items:
        item_answers = answers.get(item.rfq_item_id)
        if item_answers is None:
            updated_items.append(item)
            continue
        if not isinstance(item_answers, Mapping) or not item_answers:
            raise ValueError("each answered RFQ item must include field answers")
        updated_items.append(
            _apply_item_answers(
                item,
                item_answers,
                actor_id=actor_id,
                confirmed_at=confirmed_at,
            )
        )
    return tuple(updated_items)


def apply_clarification_answers(  # noqa: PLR0913 - inputs define the requested revision
    current_revision: RFQRevision,
    answers: ClarificationAnswers,
    *,
    actor_id: str,
    revision_id: str,
    revision_no: int,
    created_at: datetime,
) -> RFQRevision:
    """Return a child RFQ revision containing only explicitly supplied answers.

    Answers are keyed by RFQ item ID and then by the field names in that item's
    ``missing_fields``. Partial responses are allowed; unanswered fields remain
    missing in the new revision.
    """
    if not isinstance(answers, Mapping) or not answers:
        raise ValueError("at least one clarification answer is required")
    if not isinstance(actor_id, str) or not actor_id.strip():
        raise ValueError("actor_id must identify the authenticated user")
    if not isinstance(revision_id, str) or not revision_id.strip():
        raise ValueError("revision_id must be a non-empty identifier")
    revision_id = revision_id.strip()
    if revision_no != current_revision.revision_no + 1:
        raise ValueError("revision_no must follow the current revision")
    if revision_id == current_revision.revision_id:
        raise ValueError("revision_id must differ from the current revision")
    if not isinstance(created_at, datetime) or created_at.utcoffset() is None:
        raise ValueError("created_at must include a timezone")

    plan_data = current_revision.plan.model_dump(mode="python")
    plan_data.update({
        "rfq_revision_id": revision_id,
        "items": _updated_items(
            current_revision,
            answers,
            actor_id=actor_id.strip(),
            confirmed_at=created_at,
        ),
    })
    revision_data = current_revision.model_dump(mode="python")
    revision_data.update({
        "revision_id": revision_id,
        "revision_no": revision_no,
        "parent_revision_id": current_revision.revision_id,
        "plan": plan_data,
        "created_at": created_at,
        "created_by": actor_id.strip(),
    })
    return RFQRevision.model_validate(revision_data)
