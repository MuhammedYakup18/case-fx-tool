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
without allowing one date's rate to be used for another. Historical records do
not expire; today's record has a five-minute lifetime so a pre-publication value
can refresh. Concurrent misses for one key share one upstream task, and a failed
task is removed so that the next request can retry.

## With another day

I would add a maximum cache size and, for multi-worker deployment, use a shared
cache rather than one cache per process. I would also reuse one lifespan-managed
`AsyncClient`, add structured request logging, and query/cache `/v1/currencies`
so an unknown currency can be distinguished from a historically unavailable
valid pair.

## AI tools

I used ChatGPT to reason about the brief, customer impact, and adversarial edge
cases, and OpenAI Codex to inspect the repository, edit the implementation, and
run the tests. I verified the critical weekend and series-boundary assumptions
against live Frankfurter v1 responses, reviewed the generated paths, and kept
the solution intentionally small.

## One thing the AI got wrong

The first AI-generated draft did not validate every numeric and date boundary.
Adversarial review found that `100000000000000.01` changed by one cent, `1e30`
caused an unhandled 500, `1e-400` became a zero rate, and a pre-1999 upstream
date was accepted. I reproduced the cases, added explicit input/output and
upstream bounds, returned stable errors, and kept every case as a regression
test. This was a useful reminder that passing generated tests is not enough;
external data and serialization boundaries also need to be challenged.
