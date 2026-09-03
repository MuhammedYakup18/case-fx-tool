# FX conversion tool

A small FastAPI service that converts a positive monetary amount using daily
ECB reference rates provided by Frankfurter v1. It fails closed: an unavailable
or malformed upstream response never becomes a made-up conversion.

## Setup and run

Python 3.11+ is recommended.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
./run.sh
```

The service listens on `PORT` (default `8080`). `FX_UPSTREAM_BASE` selects the
upstream host (default `https://api.frankfurter.dev`); `/v1/<date>` is appended
by the service.

```bash
PORT=9000 FX_UPSTREAM_BASE=http://localhost:9999 ./run.sh
```

Example:

```bash
curl 'http://localhost:8080/tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28'
```

## Tests

```bash
./test.sh
```

The tests fake every upstream response and make no network calls. They cover a
successful conversion, weekend rate attribution, caching, invalid inputs,
out-of-range dates, upstream failures, malformed JSON, and timeouts.

## Behaviour

| Case | Behaviour |
|---|---|
| Working day | Return the upstream rate and its date. |
| Weekend or holiday | Accept Frankfurter's previous published rate, while keeping the requested date in `asked_date` and the actual publication date in `rate_date`. |
| Future date | Return `422 future_date`. |
| Before 1999-01-04 | Return `422 date_before_series`. |
| Invalid currency format | Return `422 invalid_currency`; well-formed but unavailable pairs return `422 rate_unavailable`. |
| Same source and target | Return `422 same_currency` rather than attributing a synthetic `1.0` rate to the ECB. |
| Missing, non-numeric, zero, or negative amount | Return a `422` error. |
| More than two amount decimal places | Return `422 invalid_amount_precision`; the tool treats the input as a monetary amount. |
| Slow, unreachable, 5xx, or malformed upstream | Return a `502`/`504` error and no conversion. |

Currency codes are trimmed and normalised to uppercase. Calculations use
`Decimal`; the full upstream rate is used and only the final converted amount
is rounded to two decimals with `ROUND_HALF_UP`.

Rates are cached in process by `(from, to, asked_date)`. Therefore an immediate
repeat does not call the upstream again, while requests for different dates
cannot share a rate.

## Error codes

Every failure has a non-2xx status and the same body shape:

```json
{"error": "short_machine_code", "message": "A readable sentence."}
```

| Status | Code | Meaning |
|---|---|---|
| 422 | `invalid_request` | A required query value is missing or malformed. |
| 422 | `invalid_amount` | Amount is not finite and greater than zero. |
| 422 | `invalid_amount_precision` | Amount has more than two decimal places. |
| 422 | `invalid_currency` | A currency code is not exactly three letters. |
| 422 | `same_currency` | Source and target are the same. |
| 422 | `future_date` | The requested date is in the future. |
| 422 | `date_before_series` | The requested date predates the ECB series. |
| 422 | `rate_unavailable` | Frankfurter has no rate for the pair and date. |
| 502 | `upstream_unavailable` | The upstream could not be reached. |
| 502 | `upstream_error` | The upstream returned an error status. |
| 502 | `invalid_upstream_response` | JSON, date, or rate data was unusable. |
| 504 | `upstream_timeout` | The upstream exceeded the five-second timeout. |
