"""HTTP currency-conversion tool backed by Frankfurter's ECB rates."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, DecimalException, InvalidOperation, ROUND_HALF_UP
from typing import Any

import httpx
from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


DEFAULT_UPSTREAM_BASE = "https://api.frankfurter.dev"
FIRST_ECB_RATE_DATE = date(1999, 1, 4)
UPSTREAM_TIMEOUT_SECONDS = 5.0
MAX_AMOUNT = Decimal("1000000000")
# Non-integral Decimal values are emitted as JSON numbers (Python floats).
# Staying below this magnitude keeps cent-level values distinguishable in an
# IEEE-754 double. Larger conversions fail instead of silently changing value.
MAX_SAFE_JSON_NUMBER = Decimal("70000000000000")
_CURRENCY_CODE = re.compile(r"^[A-Z]{3}$")

app = FastAPI(title="fx-tool", version="1.0.0")


class ConversionError(Exception):
    """An expected error that is safe to return to a tool caller."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RateRecord:
    rate: Decimal
    rate_date: date


# Historical rates do not change. Including every input that selects a rate in
# the key prevents a cached rate from leaking into a request for another date.
_rate_cache: dict[tuple[str, str, date], RateRecord] = {}


@app.exception_handler(ConversionError)
async def conversion_error_handler(
    _request: Request, exc: ConversionError
) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": exc.code, "message": exc.message},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request, _exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": "invalid_request",
            "message": (
                "Provide amount, from, to, and date using the documented formats."
            ),
        },
    )


def _upstream_url(rate_date: date) -> str:
    base = os.getenv("FX_UPSTREAM_BASE", DEFAULT_UPSTREAM_BASE).rstrip("/")
    return f"{base}/v1/{rate_date.isoformat()}"


def _normalise_currency(raw_code: str) -> str:
    code = raw_code.strip().upper()
    if not _CURRENCY_CODE.fullmatch(code):
        raise ConversionError(
            422,
            "invalid_currency",
            "Currency codes must contain exactly three letters.",
        )
    return code


def _parse_amount(raw_amount: str) -> Decimal:
    try:
        amount = Decimal(raw_amount.strip())
    except (InvalidOperation, AttributeError):
        raise ConversionError(
            422, "invalid_amount", "Amount must be a valid decimal number."
        ) from None

    if not amount.is_finite() or amount <= 0:
        raise ConversionError(
            422, "invalid_amount", "Amount must be greater than zero."
        )

    decimal_places = max(-amount.as_tuple().exponent, 0)
    if decimal_places > 2:
        raise ConversionError(
            422,
            "invalid_amount_precision",
            "Amount may have at most two decimal places.",
        )
    if amount > MAX_AMOUNT:
        raise ConversionError(
            422,
            "amount_too_large",
            f"Amount must not exceed {MAX_AMOUNT}.",
        )
    return amount


def _validate_date(asked_date: date) -> None:
    today_utc = datetime.now(timezone.utc).date()
    if asked_date > today_utc:
        raise ConversionError(
            422, "future_date", "A rate cannot be requested for a future date."
        )
    if asked_date < FIRST_ECB_RATE_DATE:
        raise ConversionError(
            422,
            "date_before_series",
            f"ECB rates are available from {FIRST_ECB_RATE_DATE.isoformat()} onward.",
        )


async def _request_rate(
    source: str, target: str, asked_date: date
) -> httpx.Response:
    timeout = httpx.Timeout(UPSTREAM_TIMEOUT_SECONDS)
    async with httpx.AsyncClient(timeout=timeout) as client:
        return await client.get(
            _upstream_url(asked_date),
            params={"base": source, "symbols": target},
        )


def _read_rate_payload(
    response: httpx.Response, source: str, target: str, asked_date: date
) -> RateRecord:
    if response.status_code in {400, 404, 422}:
        raise ConversionError(
            422,
            "rate_unavailable",
            "No rate is available for the requested currencies and date.",
        )
    if response.status_code >= 500:
        raise ConversionError(
            502,
            "upstream_error",
            "The exchange-rate provider returned an error.",
        )
    if not 200 <= response.status_code < 300:
        raise ConversionError(
            502,
            "upstream_error",
            "The exchange-rate provider returned an unexpected response.",
        )

    try:
        payload: Any = response.json()
    except ValueError:
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider did not return valid JSON.",
        ) from None

    if not isinstance(payload, dict):
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid payload.",
        )

    payload_base = payload.get("base")
    if not isinstance(payload_base, str) or payload_base.upper() != source:
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned a rate for the wrong base currency.",
        )

    try:
        actual_date = date.fromisoformat(payload["date"])
        raw_rate = payload["rates"][target]
        rate = Decimal(str(raw_rate))
    except (KeyError, TypeError, ValueError, InvalidOperation):
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider response did not contain a usable rate.",
        ) from None

    if (
        actual_date > asked_date
        or not rate.is_finite()
        or rate <= 0
        or rate > MAX_SAFE_JSON_NUMBER
    ):
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned an invalid rate or date.",
        )
    return RateRecord(rate=rate, rate_date=actual_date)


def _calculate_result(amount: Decimal, rate: Decimal) -> Decimal:
    try:
        result = (amount * rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
    except DecimalException:
        raise ConversionError(
            502,
            "invalid_upstream_response",
            "The exchange-rate provider returned a rate that cannot be used safely.",
        ) from None

    if result > MAX_SAFE_JSON_NUMBER:
        raise ConversionError(
            422,
            "amount_too_large",
            "The converted result is too large; use a smaller amount.",
        )
    return result


async def _get_rate(source: str, target: str, asked_date: date) -> RateRecord:
    cache_key = (source, target, asked_date)
    if cache_key in _rate_cache:
        return _rate_cache[cache_key]

    try:
        response = await _request_rate(source, target, asked_date)
    except httpx.TimeoutException:
        raise ConversionError(
            504,
            "upstream_timeout",
            "The exchange-rate provider did not respond in time.",
        ) from None
    except httpx.RequestError:
        raise ConversionError(
            502,
            "upstream_unavailable",
            "The exchange-rate provider could not be reached.",
        ) from None

    record = _read_rate_payload(response, source, target, asked_date)
    _rate_cache[cache_key] = record
    return record


def _json_number(value: Decimal) -> int | float:
    if value == value.to_integral_value():
        return int(value)
    return float(value)


@app.get("/tools/convert")
async def convert(
    amount_raw: str = Query(..., alias="amount"),
    source_raw: str = Query(..., alias="from"),
    target_raw: str = Query(..., alias="to"),
    asked_date: date = Query(..., alias="date"),
) -> dict[str, int | float | str]:
    """Convert a positive monetary amount using an ECB reference rate."""

    amount = _parse_amount(amount_raw)
    source = _normalise_currency(source_raw)
    target = _normalise_currency(target_raw)
    if source == target:
        raise ConversionError(
            422,
            "same_currency",
            "Source and target currencies must be different.",
        )
    _validate_date(asked_date)

    record = await _get_rate(source, target, asked_date)
    result = _calculate_result(amount, record.rate)
    return {
        "amount": _json_number(amount),
        "from": source,
        "to": target,
        "rate": _json_number(record.rate),
        "result": _json_number(result),
        "rate_date": record.rate_date.isoformat(),
        "asked_date": asked_date.isoformat(),
        "source": "ECB via frankfurter.dev",
    }
