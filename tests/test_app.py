from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

import app as fx_app


client = TestClient(fx_app.app)


@pytest.fixture(autouse=True)
def clear_cache() -> None:
    fx_app._rate_cache.clear()
    fx_app._inflight_requests.clear()


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


def test_today_cache_is_reused_before_ttl_and_refreshed_after_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [1000.0]
    monkeypatch.setattr(fx_app, "_today_utc", lambda: fx_app.date(2026, 8, 28))
    monkeypatch.setattr(fx_app, "monotonic", lambda: now[0])
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
    params = {"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-28"}

    first = client.get("/tools/convert", params=params)
    second = client.get("/tools/convert", params=params)

    assert first.json()["rate_date"] == "2026-08-27"
    assert second.json()["rate_date"] == "2026-08-27"
    assert request_rate.await_count == 1

    now[0] += fx_app.TODAY_CACHE_TTL_SECONDS
    refreshed = client.get("/tools/convert", params=params)

    assert refreshed.json()["rate_date"] == "2026-08-28"
    assert request_rate.await_count == 2


def test_concurrent_identical_misses_share_one_upstream_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        request_rate = AsyncMock()

        async def delayed_response(*_args: object) -> httpx.Response:
            started.set()
            await release.wait()
            return upstream_response(
                json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.0}}
            )

        request_rate.side_effect = delayed_response
        monkeypatch.setattr(fx_app, "_request_rate", request_rate)
        asked_date = fx_app.date(2026, 8, 28)
        tasks = [
            asyncio.create_task(fx_app._get_rate("EUR", "TRY", asked_date))
            for _ in range(5)
        ]

        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.sleep(0)
        assert request_rate.await_count == 1

        release.set()
        records = await asyncio.gather(*tasks)

        assert all(record.rate == fx_app.Decimal("47.0") for record in records)
        assert request_rate.await_count == 1

    asyncio.run(exercise())


def test_failed_shared_request_is_cleared_and_next_request_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        call_count = 0

        async def fail_then_succeed(*_args: object) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                started.set()
                await release.wait()
                raise httpx.ReadTimeout("too slow")
            return upstream_response(
                json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47.0}}
            )

        monkeypatch.setattr(fx_app, "_request_rate", fail_then_succeed)
        asked_date = fx_app.date(2026, 8, 28)
        tasks = [
            asyncio.create_task(fx_app._get_rate("EUR", "TRY", asked_date))
            for _ in range(5)
        ]

        await asyncio.wait_for(started.wait(), timeout=1)
        release.set()
        failures = await asyncio.gather(*tasks, return_exceptions=True)

        assert call_count == 1
        assert all(
            isinstance(error, fx_app.ConversionError)
            and error.code == "upstream_timeout"
            for error in failures
        )

        await asyncio.sleep(0)
        retry = await fx_app._get_rate("EUR", "TRY", asked_date)

        assert retry.rate == fx_app.Decimal("47.0")
        assert call_count == 2

    asyncio.run(exercise())


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


@pytest.mark.parametrize("amount", ["100000000000000.01", "1e30"])
def test_oversized_amounts_fail_without_losing_precision_or_raising_500(
    monkeypatch: pytest.MonkeyPatch, amount: str
) -> None:
    request_rate = AsyncMock()
    monkeypatch.setattr(fx_app, "_request_rate", request_rate)

    response = client.get(
        "/tools/convert",
        params={"amount": amount, "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 422
    assert response.json()["error"] == "amount_too_large"
    request_rate.assert_not_awaited()


def test_conversion_result_too_large_for_safe_json_number_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(
            return_value=upstream_response(
                json={
                    "base": "EUR",
                    "date": "2026-08-28",
                    "rates": {"TRY": 70000.01},
                }
            )
        ),
    )

    response = client.get(
        "/tools/convert",
        params={
            "amount": "1000000000",
            "from": "EUR",
            "to": "TRY",
            "date": "2026-08-28",
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == "amount_too_large"


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


def test_rate_that_underflows_when_encoded_as_json_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(
            return_value=upstream_response(
                json={
                    "base": "EUR",
                    "date": "2026-08-28",
                    "rates": {"TRY": "1e-400"},
                }
            )
        ),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
    )

    assert response.status_code == 502
    assert response.json()["error"] == "invalid_upstream_response"


def test_upstream_date_before_the_ecb_series_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(
            return_value=upstream_response(
                json={
                    "base": "EUR",
                    "date": "1998-12-31",
                    "rates": {"TRY": 47.1234},
                }
            )
        ),
    )

    response = client.get(
        "/tools/convert",
        params={"amount": "1", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
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


def test_openapi_documents_the_real_success_and_error_shapes() -> None:
    operation = client.get("/openapi.json").json()["paths"]["/tools/convert"]["get"]
    responses = operation["responses"]

    success_schema = responses["200"]["content"]["application/json"]["schema"]
    assert success_schema["$ref"].endswith("/ConversionResponse")

    for status_code in ("422", "502", "504"):
        error_schema = responses[status_code]["content"]["application/json"]["schema"]
        assert error_schema["$ref"].endswith("/ErrorResponse")


@pytest.mark.parametrize(
    ("raw_rate", "expected_result"),
    [
        ("1.00000000000499999", 1000000000),
        ("1.000000000004999999999999999999999999", 1000000000),
        ("1.000000000005", 1000000000.01),
    ],
)
def test_raw_json_rate_preserves_cent_rounding(
    monkeypatch: pytest.MonkeyPatch, raw_rate: str, expected_result: int | float
) -> None:
    # A Python float fixture would discard the precision before the test starts.
    body = (
        '{"base":"EUR","date":"2026-08-28","rates":{"TRY":'
        + raw_rate
        + "}}"
    ).encode()
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(return_value=upstream_response(content=body)),
    )

    response = client.get(
        "/tools/convert",
        params={
            "amount": "1000000000",
            "from": "EUR",
            "to": "TRY",
            "date": "2026-08-28",
        },
    )

    assert response.status_code == 200
    assert response.json()["result"] == expected_result


@pytest.mark.parametrize(
    "body",
    [
        b'{"extra":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}",
        b'{"base":"EUR","date":"2026-08-28",'
        b'"rates":{"TRY":1e9999999999999999999}}',
    ],
    ids=["excessive-nesting", "unrepresentable-decimal-exponent"],
)
def test_unparseable_upstream_json_uses_error_envelope(
    monkeypatch: pytest.MonkeyPatch, body: bytes
) -> None:
    monkeypatch.setattr(
        fx_app,
        "_request_rate",
        AsyncMock(return_value=upstream_response(content=body)),
    )

    with TestClient(fx_app.app, raise_server_exceptions=False) as api:
        response = api.get(
            "/tools/convert",
            params={"amount": "250", "from": "EUR", "to": "TRY", "date": "2026-08-28"},
        )

    assert response.status_code == 502
    assert response.headers["content-type"] == "application/json"
    assert response.json()["error"] == "invalid_upstream_response"
    assert isinstance(response.json()["message"], str)


def test_total_deadline_stops_dripping_response_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_client = httpx.AsyncClient
    monkeypatch.setattr(fx_app, "UPSTREAM_TIMEOUT_SECONDS", 0.05)

    async def exercise() -> None:
        stream_closed = asyncio.Event()
        calls = 0

        class DrippingStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                # Each gap is below the read timeout, but the full body is slow.
                body = b'{"base":"EUR","date":"2026-08-28","rates":{"TRY":47}}'
                for byte in body:
                    yield bytes([byte])
                    await asyncio.sleep(0.01)

            async def aclose(self) -> None:
                stream_closed.set()

        def upstream(_request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls == 1:
                return httpx.Response(200, stream=DrippingStream())
            return httpx.Response(
                200, json={"base": "EUR", "date": "2026-08-28", "rates": {"TRY": 47}}
            )

        transport = httpx.MockTransport(upstream)
        monkeypatch.setattr(
            fx_app.httpx,
            "AsyncClient",
            lambda **kwargs: original_client(transport=transport, **kwargs),
        )
        params = {"amount": "250", "from": "EUR", "to": "TRY", "date": "2026-08-28"}
        async with original_client(
            transport=httpx.ASGITransport(app=fx_app.app), base_url="http://testserver"
        ) as api:
            responses = await asyncio.wait_for(
                asyncio.gather(
                    api.get("/tools/convert", params=params),
                    api.get("/tools/convert", params=params),
                ),
                timeout=2,
            )
            assert calls == 1
            for response in responses:
                assert response.status_code == 504
                assert response.json()["error"] == "upstream_timeout"
            assert stream_closed.is_set()

            retry = await api.get("/tools/convert", params=params)
            assert retry.status_code == 200
            assert retry.json()["result"] == 11750
            assert calls == 2

    asyncio.run(exercise())
