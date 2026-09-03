from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app as fx_app


client = TestClient(fx_app.app)


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    fx_app._rate_cache.clear()


def upstream_response(status_code: int = 200, **kwargs: object) -> httpx.Response:
    request = httpx.Request("GET", "http://fake-upstream/v1/rate")
    return httpx.Response(status_code, request=request, **kwargs)


def test_success_uses_precise_rate(monkeypatch: pytest.MonkeyPatch) -> None:
    request_rate = AsyncMock(
        return_value=upstream_response(
            json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.1234}}
        )
    )
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    response = client.get(
        "/tools/convert",
        params={"amount": "250", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "amount": 250,
        "from": "EUR",
        "to": "TRY",
        "rate": 47.1234,
        "result": 11780.85,
        "rate_date": "2026-08-28",
        "asked_date": "2026-08-28",
        "source": "ECB via frankfurter.dev",
    }


def test_weekend_response_exposes_the_actual_rate_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(
            return_value=upstream_response(
                json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.1234}}
            )
        ),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-29"},
    )

    assert response.status_code == 200
    assert response.json()["asked_date"] == "2026-08-29"
    assert response.json()["rate_date"] == "2026-08-28"


def test_repeated_question_uses_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    request_rate = AsyncMock(
        return_value=upstream_response(
            json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.1234}}
        )
    )
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)
    params = {"amount": "10", "from": "EUR", "to": "TRY", "date": "2026-08-28"}

    assert client.get("/tools/convert", params=params).status_code == 200
    assert client.get("/tools/convert", params=params).status_code == 200
    assert request_rate.await_count == 1


def test_cache_does_not_mix_different_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    request_rate = AsyncMock(
        side_effect=[
            upstream_response(
                json={"base": "EUR", "date": "2026-08-27", "rates": {"TRY": 46.5}}
            ),
            upstream_response(
                json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.0}}
            ),
        ]
    )
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    first = client.get(
        "/tools/convert",
        params={"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-27"},
    )
    second = client.get(
        "/tools/convert",
        params={"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert first.json()["rate"] == 46.5
    assert second.json()["rate"] == 47
    assert request_rate.await_count == 2


@pytest.mark.parametrize("amount", ["0", "-1", "1.1234567890", "not-a-number"])
def test_invalid_amounts_fail_without_calling_upstream(
    monkeypatch: pytest.MonkeyPatch, amount: str
) -> None:
    request_rate = AsyncMock()
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    response = client.get(
        "/tools/convert",
        params={"amount": amount, "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 422
    assert set(response.json()) == {"error", "message"}
    request_rate.assert_not_awaited()


def test_missing_amount_uses_error_envelope() -> None:
    response = client.get(
        "/tools/convert",
        params={"from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "invalid_request"


@pytest.mark.parametrize(
    ("source", "target", "error_code"),
    [("EU", "TRY", "invalid_currency"), ("EUR", "EUR", "same_currency")],
)
def test_invalid_currency_inputs(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
    target: str,
    error_code: str,
) -> None:
    request_rate = AsyncMock()
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": source, "to": target, "date": "2026-08-28"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == error_code
    request_rate.assert_not_awaited()


@pytest.mark.parametrize(
    ("asked_date", "error_code"),
    [("2999-01-01", "future_date"), ("1999-01-03", "date_before_series")],
)
def test_out_of_range_dates_fail_before_upstream(
    monkeypatch: pytest.MonkeyPatch, asked_date: str, error_code: str
) -> None:
    request_rate = AsyncMock()
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": "EUR", "to": "TRY", "date": asked_date},
    )

    assert response.status_code == 422
    assert response.json()["error"] == error_code
    request_rate.assert_not_awaited()


def test_upstream_500_is_not_a_fake_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fx_app, "_request_rate", AsyncMock(return_value=upstream_response(500, json={}))
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "upstream_error"


def test_non_json_upstream_response_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(return_value=upstream_response(content=b"not-json")),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "invalid_upstream_response"


@pytest.mark.parametrize(
    "payload",
    [
        {"base": "USD", "date": "2026-08-28", "rates": {"TRY": 47.1234}},
        {"base": "EUR", "date": "2026-08-29", "rates": {"TRY": 47.1234}},
    ],
)
def test_inconsistent_upstream_data_is_rejected(
    monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(return_value=upstream_response(json=payload)),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "invalid_upstream_response"


def test_timeout_returns_a_readable_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(side_effect=httpx.ReadTimeout("too slow")),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "10", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 504
    assert response.json()["error"] == "upstream_timeout"


def test_upstream_base_comes_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FX_UPSTREAM_BASE", "http://fake-upstream.local/")

    assert (
        fx_app._upstream_url(fx_app.date(2026, 8, 28))
        == "http://fake-upstream.local/v1/2026-08-28"
    )
