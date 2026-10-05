"""Deterministic, Decimal-based price selection for catalog quote lines."""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from enum import StrEnum

from trade_agent.catalog import PriceTier, SyntheticCatalog
from trade_agent.contracts import (
    CanonicalUnit,
    Currency,
    ProductSnapshot,
    QuoteLineSnapshot,
)


class PricingIssue(StrEnum):
    PRICE_MISSING = "PRICE_MISSING"
    PRICE_EXPIRED = "PRICE_EXPIRED"
    PRICE_NOT_EFFECTIVE = "PRICE_NOT_EFFECTIVE"
    UNIT_MISMATCH = "UNIT_MISMATCH"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"


class PricingConfigurationError(ValueError):
    """Raised when a price list cannot make a unique, valid price selection."""


class QuoteQualityError(ValueError):
    """Raised when price results cannot safely form a quote."""


@dataclass(frozen=True, slots=True)
class PricingRequest:
    """Normalized RFQ inputs needed to price one line."""

    sku: str
    quantity: Decimal
    unit: CanonicalUnit | str
    as_of: date
    currency: Currency | str = Currency.USD
    rfq_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class PricingResult:
    """Immutable selected-price snapshot, ready to populate a quote line."""

    sku: str
    rfq_item_id: str | None
    quantity: Decimal
    unit: str
    currency: str
    price_list_version: str
    price_list_source: str
    price_unit: str | None
    unit_price: Decimal | None
    line_amount: Decimal | None
    selected_minimum_quantity: int | None
    issues: tuple[PricingIssue, ...] = ()

    @property
    def is_priced(self) -> bool:
        return not self.issues and self.unit_price is not None

    def to_quote_line_snapshot(
        self,
        *,
        quote_item_id: str,
        product: ProductSnapshot,
    ) -> QuoteLineSnapshot:
        """Create the immutable contract snapshot from a successful result."""
        if not self.is_priced:
            issue_codes = ", ".join(issue.value for issue in self.issues)
            raise QuoteQualityError(
                f"line is not priced: {issue_codes or 'incomplete'}"
            )
        if self.rfq_item_id is None:
            raise QuoteQualityError("line is missing its RFQ item association")
        if (
            self.price_unit is None
            or self.line_amount is None
            or self.unit_price is None
        ):
            raise QuoteQualityError("line is missing required price values")

        return QuoteLineSnapshot(
            quote_item_id=quote_item_id,
            rfq_item_id=self.rfq_item_id,
            product=product,
            quantity=self.quantity,
            unit=self.unit,
            price_list_version=self.price_list_version,
            price_list_source=self.price_list_source,
            unit_price=self.unit_price,
            price_unit=self.price_unit,
            currency=self.currency,
            line_amount=self.line_amount,
        )


def price_line(
    catalog: SyntheticCatalog,
    request: PricingRequest,
) -> PricingResult:
    """Select the effective quantity tier and calculate its rounded line amount.

    ``as_of`` is explicit so the same catalog and request always produce the
    same result. Unit and currency values must already be normalized by callers.
    """
    _validate_request(catalog, request)
    base = _result_fields(catalog, request)
    date_issue = _effective_date_issue(catalog, request.as_of)
    if date_issue is not None:
        return _result(base, date_issue)

    sku_tiers = tuple(tier for tier in catalog.prices if tier.sku == request.sku)
    if not sku_tiers:
        return _result(base, PricingIssue.PRICE_MISSING)
    if _value(request.currency) != Currency.USD.value:
        return _result(base, PricingIssue.CURRENCY_MISMATCH)

    currency_tiers, issues = _matching_tiers(
        sku_tiers, _value(request.unit), _value(request.currency), request.sku
    )
    if issues:
        return _result(base, *issues)
    selected = _select_tier(currency_tiers, request.quantity)
    if selected is None:
        return _result(base, PricingIssue.PRICE_MISSING)
    return _priced_result(base, selected, request.quantity)


def _validate_request(catalog: SyntheticCatalog, request: PricingRequest) -> None:
    _validate_quantity(request.quantity)
    if not request.sku.strip():
        raise ValueError("sku must not be empty")
    if not isinstance(request.as_of, date) or isinstance(request.as_of, datetime):
        raise TypeError("as_of must be a date")
    if (
        catalog.effective_to is not None
        and catalog.effective_from > catalog.effective_to
    ):
        raise PricingConfigurationError("price-list effective date range is invalid")
    _value(request.unit)
    _value(request.currency)


def _result_fields(
    catalog: SyntheticCatalog, request: PricingRequest
) -> dict[str, object]:
    return {
        "sku": request.sku,
        "rfq_item_id": request.rfq_item_id,
        "quantity": request.quantity,
        "unit": _value(request.unit),
        "currency": _value(request.currency),
        "price_list_version": catalog.price_list_version,
        "price_list_source": catalog.source,
    }


def _effective_date_issue(
    catalog: SyntheticCatalog, as_of: date
) -> PricingIssue | None:
    if as_of < catalog.effective_from:
        return PricingIssue.PRICE_NOT_EFFECTIVE
    if catalog.effective_to is not None and as_of > catalog.effective_to:
        return PricingIssue.PRICE_EXPIRED
    return None


def _matching_tiers(
    sku_tiers: tuple[PriceTier, ...],
    requested_unit: str,
    requested_currency: str,
    sku: str,
) -> tuple[tuple[PriceTier, ...], tuple[PricingIssue, ...]]:
    _validate_tiers(sku_tiers)
    minimums = [tier.minimum_quantity for tier in sku_tiers]
    if len(minimums) != len(set(minimums)):
        raise PricingConfigurationError(f"duplicate price tier for SKU {sku}")

    unit_tiers = tuple(tier for tier in sku_tiers if tier.unit == requested_unit)
    currency_tiers = tuple(
        tier for tier in unit_tiers if tier.currency == requested_currency
    )
    issues: list[PricingIssue] = []
    if not unit_tiers:
        issues.append(PricingIssue.UNIT_MISMATCH)
    currency_mismatch = bool(unit_tiers) and not currency_tiers
    if not unit_tiers:
        currency_mismatch = not any(
            tier.currency == requested_currency for tier in sku_tiers
        )
    if currency_mismatch:
        issues.append(PricingIssue.CURRENCY_MISMATCH)
    return currency_tiers, tuple(issues)


def _select_tier(tiers: tuple[PriceTier, ...], quantity: Decimal) -> PriceTier | None:
    eligible_tiers = tuple(
        tier for tier in tiers if Decimal(tier.minimum_quantity) <= quantity
    )
    return max(eligible_tiers, key=lambda tier: tier.minimum_quantity, default=None)


def _priced_result(
    base: dict[str, object], tier: PriceTier, quantity: Decimal
) -> PricingResult:
    return PricingResult(
        **base,
        price_unit=tier.unit,
        unit_price=tier.unit_price,
        line_amount=_rounded_line_amount(quantity, tier.unit_price),
        selected_minimum_quantity=tier.minimum_quantity,
    )


def validate_quote_quality(
    lines: Sequence[PricingResult],
    *,
    expected_rfq_item_ids: Iterable[str],
    as_of: date,
    valid_until: date | None,
    expected_currency: Currency | str = Currency.USD,
) -> None:
    """Reject a missing, duplicate, unpriced, or incomplete set of quote lines."""
    if not lines:
        raise QuoteQualityError("quote must include at least one line")

    _validate_quote_validity(as_of, valid_until)
    line_ids = _validated_line_ids(lines)
    _validate_expected_ids(line_ids, expected_rfq_item_ids)

    requested_currency = _value(expected_currency)
    if requested_currency != Currency.USD.value:
        raise QuoteQualityError("quote currency must be USD")
    for line in lines:
        _validate_priced_line(line, requested_currency)


def _validated_line_ids(lines: Sequence[PricingResult]) -> list[str]:
    line_ids = [line.rfq_item_id for line in lines]
    if any(not isinstance(line_id, str) or not line_id.strip() for line_id in line_ids):
        raise QuoteQualityError("every line must have an RFQ item association")
    if len(set(line_ids)) != len(line_ids):
        raise QuoteQualityError("RFQ item lines must not be duplicated")
    return [line_id for line_id in line_ids if line_id is not None]


def _validate_expected_ids(
    line_ids: list[str], expected_rfq_item_ids: Iterable[str]
) -> None:
    expected_ids = tuple(expected_rfq_item_ids)
    if len(set(expected_ids)) != len(expected_ids):
        raise QuoteQualityError("expected RFQ item IDs must be unique")
    actual_ids = set(line_ids)
    if actual_ids != set(expected_ids):
        missing_ids = sorted(set(expected_ids) - actual_ids)
        extra_ids = sorted(actual_ids - set(expected_ids))
        raise QuoteQualityError(
            f"quote lines do not match RFQ items; missing={missing_ids}, "
            f"unexpected={extra_ids}"
        )


def _validate_quote_validity(as_of: date, valid_until: date | None) -> None:
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise QuoteQualityError("quote as_of must be a date")
    if valid_until is None:
        raise QuoteQualityError("quote valid_until is required")
    if not isinstance(valid_until, date) or isinstance(valid_until, datetime):
        raise QuoteQualityError("quote valid_until must be a date")
    if valid_until < as_of:
        raise QuoteQualityError("quote valid_until is in the past")


def _validate_priced_line(line: PricingResult, requested_currency: str) -> None:
    if line.issues:
        issue_codes = ", ".join(issue.value for issue in line.issues)
        raise QuoteQualityError(
            f"RFQ item {line.rfq_item_id} is not priced: {issue_codes}"
        )
    _validate_provenance(line)
    if (
        not isinstance(line.quantity, Decimal)
        or not line.quantity.is_finite()
        or line.quantity <= 0
    ):
        raise QuoteQualityError(f"RFQ item {line.rfq_item_id} has an invalid quantity")
    if line.unit != line.price_unit:
        raise QuoteQualityError(
            f"RFQ item {line.rfq_item_id} has a price-unit mismatch"
        )
    if line.currency != requested_currency:
        raise QuoteQualityError(f"RFQ item {line.rfq_item_id} has a currency mismatch")
    _validate_amounts(line)


def _validate_provenance(line: PricingResult) -> None:
    required_values = (
        line.sku,
        line.price_list_version,
        line.price_list_source,
        line.unit,
        line.price_unit,
    )
    if any(
        not isinstance(value, str) or not value.strip() for value in required_values
    ):
        raise QuoteQualityError(
            f"RFQ item {line.rfq_item_id} has incomplete price provenance"
        )


def _validate_amounts(line: PricingResult) -> None:
    if (
        not isinstance(line.unit_price, Decimal)
        or not line.unit_price.is_finite()
        or line.unit_price <= 0
    ):
        raise QuoteQualityError(f"RFQ item {line.rfq_item_id} is missing a valid price")
    if not isinstance(line.line_amount, Decimal) or not _valid_line_amount(
        line.line_amount
    ):
        raise QuoteQualityError(f"RFQ item {line.rfq_item_id} is missing a valid price")
    if line.line_amount != _rounded_line_amount(line.quantity, line.unit_price):
        raise QuoteQualityError(
            f"RFQ item {line.rfq_item_id} has an inconsistent line amount"
        )


def _valid_line_amount(amount: Decimal) -> bool:
    return amount.is_finite() and amount >= 0


def _validate_quantity(quantity: Decimal) -> None:
    if not isinstance(quantity, Decimal):
        raise TypeError("quantity must be Decimal")
    if not quantity.is_finite() or quantity <= 0:
        raise ValueError("quantity must be a finite value greater than zero")


def _validate_tiers(tiers: tuple[PriceTier, ...]) -> None:
    for tier in tiers:
        if (
            not isinstance(tier.minimum_quantity, int)
            or isinstance(tier.minimum_quantity, bool)
            or tier.minimum_quantity <= 0
        ):
            raise PricingConfigurationError(
                "minimum_quantity must be a positive integer"
            )
        if (
            not isinstance(tier.unit_price, Decimal)
            or not tier.unit_price.is_finite()
            or tier.unit_price <= 0
        ):
            raise PricingConfigurationError(
                "tier unit_price must be finite and positive"
            )
        if (
            not isinstance(tier.unit, str)
            or not tier.unit.strip()
            or not isinstance(tier.currency, str)
            or not tier.currency.strip()
        ):
            raise PricingConfigurationError("tier unit and currency must not be empty")


def _rounded_line_amount(quantity: Decimal, unit_price: Decimal) -> Decimal:
    precision = max(
        28,
        len(quantity.as_tuple().digits) + len(unit_price.as_tuple().digits) + 2,
        max(1, quantity.adjusted() + unit_price.adjusted() + 3) + 2,
    )
    with localcontext() as context:
        context.prec = precision
        return (quantity * unit_price).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _result(base: dict[str, object], *issues: PricingIssue) -> PricingResult:
    return PricingResult(
        **base,
        price_unit=None,
        unit_price=None,
        line_amount=None,
        selected_minimum_quantity=None,
        issues=tuple(issues),
    )


def _value(value: CanonicalUnit | Currency | str) -> str:
    if isinstance(value, (CanonicalUnit, Currency)):
        return value.value
    if not isinstance(value, str) or not value.strip():
        raise ValueError("unit and currency values must not be empty")
    return value
