# Notes

## Decisions

I accept Frankfurter's previous published rate for weekends and ECB holidays,
because the payload provides the date the rate actually belongs to. The response
keeps that as `rate_date` and the caller's date as `asked_date`; a future date or
a date before 1999-01-04 is rejected. I reject same-currency requests instead of
claiming a synthetic `1.0` rate came from the ECB. Amounts are positive decimals
with at most two fractional digits and a maximum of 1,000,000,000. Calculations
retain the full upstream rate and round only the final result. A value too large
to preserve cents as a JSON number is rejected instead of being silently changed.

Expected upstream and input failures return a stable non-2xx error envelope.
The cache key is `(source, target, asked_date)`, which satisfies repeat requests
without allowing one date's rate to be used for another.

## With another day

I would give today's cache entries a bounded lifetime because today's
Frankfurter value can change when the ECB publishes, add a maximum cache size,
and collapse simultaneous identical misses into one upstream request. I would
also reuse one lifespan-managed `AsyncClient`, add structured request logging,
and query/cache `/v1/currencies` so an unknown currency can be distinguished
from a historically unavailable valid pair.

## AI tools

I used OpenAI Codex to inspect the brief, draft the implementation and tests,
and challenge the error-handling and date-provenance decisions. I verified the
critical weekend and series-boundary assumptions against live Frankfurter v1
responses, reviewed every generated path, and kept the solution intentionally
small.

## One thing the AI got wrong

The first AI-generated draft used `Decimal` for the calculation but converted
non-integer values to `float` for JSON without limiting their magnitude. An
adversarial review found that `100000000000000.01` was returned as
`100000000000000.02`, while `1e30` caused an unhandled 500. I reproduced both,
added an explicit amount and safe-result limit, returned a stable 422 error, and
kept both inputs as regression tests.
