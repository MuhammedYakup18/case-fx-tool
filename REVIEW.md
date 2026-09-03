# Review of tool.py

## 1. The cache and reported date can silently produce a plausible wrong rate

The cache key contains only the currency pair. After the first request, every
date for that pair receives the first rate. On both cache hits and fresh
requests, `rate_date` is built from the requested date instead of the upstream
payload's `date`. A customer can therefore receive Friday's rate labelled as
Saturday's, or one historical rate labelled as another day. I would verify this
with a fake upstream and two same-pair requests for different dates, plus a
weekend response whose payload date is the preceding Friday.

## 2. Failures are presented as successful zero-value conversions

The broad exception handler returns HTTP 200, `rate: 0.0`, and `result: 0.0` for
timeouts, 5xx responses, invalid currencies, missing keys, and malformed JSON.
An agent cannot distinguish an outage from a real conversion and may tell a
paying customer that their money is worth zero. I would point the upstream at a
closed port, then repeat with a fake 500 and a non-JSON body; each currently
returns a successful-looking object instead of a non-2xx error envelope.

## 3. The public request contract is not the implemented contract

FastAPI exposes `from_` and `on`, while callers are told to send `from` and
`date`. Unknown query parameters are ignored, so the documented request may use
the default EUR base and latest rate without warning. The response also omits
required `asked_date`. I would call the exact documented URL with a non-EUR
source and a historical date, then inspect both the outbound upstream request
and the response schema.

## 4. Upstream handling and financial arithmetic are unsafe

The real host is hardcoded, so review and outage-routing configuration is
ignored. HTTP status is never checked before parsing, and a missing rate falls
back to today's latest rate even for a historical request. Finally, the rate is
rounded to two decimals before multiplication, creating avoidable conversion
error. I would run with `FX_UPSTREAM_BASE` set to a fake server, simulate 404 and
500 responses, and compare a conversion using `47.1234` with the expected
full-precision calculation.

## The one I would fix before shipping tonight

I would fix finding 1 first: key the cache by pair and requested date, and take
`rate_date` only from the validated upstream payload. Plausible but incorrectly
dated financial data is difficult for an agent or customer to detect and
directly violates the service's central trust guarantee.

## Things that look suspicious but are fine

Using the most recent earlier ECB rate for a weekend or holiday is reasonable
if `asked_date` and the upstream's real `rate_date` are both returned. An async
HTTP client and a small process-local cache are also proportionate for this
service; their existence is not the defect—the missing lifecycle cleanup and
incomplete cache key are.
