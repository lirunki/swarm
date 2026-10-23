# Worked example: LLM provider reliability

This is the recorded composition from 2026-10-21, not a universal default or an
instruction to enable paging. The saved monitor was
**Lobster — LLM provider failures**, ID
`9e890478-2c69-4c9a-89c0-d9f4bd4e7a67`, in account `Lobster`.
It was created in **Silent** state, then independently read back. Re-read it
before reusing it; do not create another copy or modify it by default.

[Open the recorded monitor](https://portal.microsoftgeneva.com/manage/monitors/monitor?activity=edit-monitor&version=1&action=3&account=Lobster&monitorId=9e890478-2c69-4c9a-89c0-d9f4bd4e7a67).

## Telemetry and scope

The canonical instrumentation is
[`record_llm_provider_request`](https://microsoft.ghe.com/bic/lobster/blob/main/src/lobster_metrics/src/lib.rs),
with outcome classification in
[`llm_usage.rs`](https://microsoft.ghe.com/bic/lobster/blob/main/src/lobster_container_proxy/src/llm_usage.rs).

| Setting | Recorded value |
| --- | --- |
| Account / namespace | `Lobster` / `Lobster` |
| Metric | `lobster_runtime_llm_provider_requests_total` |
| Sampling | Count |
| Time-series dimensions | `ClusterCategory`, `Geo`, `IslandNumber`, `outcome`, `provider`, `provider_source`, `sla_met`, `status_class`, `transport` |
| Resource dimensions | `Monitor.Tenant`, `ClusterCategory`, `IslandNumber`, `provider` |
| Resource inclusions | Wildcards; all emitted values, not a four-island allowlist |
| Resource type | `Monitor.Tenant-ClusterCategory-IslandNumber-provider` |
| Grouped independently | Each category/island/provider tuple |
| Aggregated together | Geographic series, provider source, transport and other non-resource dimensions |

The record had a compatible preaggregate. New monitors must still verify their
live dimensions and coverage. `model` was not an incident resource, avoiding a
separate incident for every model during one provider outage.

## Expressions

Define eligible failure outcomes explicitly:

```text
upstream_status
upstream_error
timeout
stream_error
response_failed
response_missing_terminal
```

The denominator includes exactly those outcomes plus `success`. Policy
rejection, caller cancellation and `response_incomplete` are not automatically
provider failures. An incomplete response can reflect an expected token/output
limit. Revisit the population only against the current source contract, not a
broad `outcome != success` filter.

For all terms, inherit `ClusterCategory`, `IslandNumber` and `provider` from the
resource. Apply Sum across the lookback and Sum across matching series:

```text
F(T) = eligible failed calls over [T-15m,T]
N(T) = eligible failed calls + successes over [T-15m,T]
P(T) = 100 * F(T) / N(T)
```

Evaluation frequency is five minutes. Four expressions implement one AND rule:

| Expression | Multiplier | Numerator / denominator | Lookback | End shift | Threshold |
| --- | --- | --- | --- | --- | --- |
| Failed requests | 1 | Failures / 1 | Global 15m | None | `>= 3` |
| Failure rate percent | 100 | Failures / eligible total | Global 15m | None | `>= 20` |
| Failed requests (5m earlier) | 1 | Failures / 1 | 15m | 5m | `>= 3` |
| Failure rate percent (5m earlier) | 100 | Failures / eligible total | 15m | 5m | `>= 20` |

No additional bucket rule is enabled. Both windows are recomputed at evaluation
time; this is not a persisted history of two actual evaluator runs. Low traffic
can delay detection beyond fifteen minutes. A single transport failure should
not independently satisfy the volume threshold.

The observed denominator-zero and skip flags were false for firing; automatic
incident mitigation was disabled. This monitor does not detect missing metrics,
prove zero-traffic health, or implement the separately proposed recovery policy.

## Routing and diagnostics

- Severity: **3**; unhealthy when the four clauses hold; initial state **Silent**.
- IcM route: `MDM://Project Lobster/Lobster Runtime`, independently checked against
  the existing active Runtime CXP monitor at creation time.
- TSG link: [container proxy target configuration](https://microsoft.ghe.com/bic/lobster/blob/main/docs/reference/container-proxy-target-config.md).
  The incident message also provides bounded triage steps; this reference is not
  evidence that a full provider-specific on-call runbook has been commissioned.
- Title uses the scoped provider, island and category placeholders, not user or
  container identifiers.
- Diagnostic links point to US and EU `CSCAnalytics` and include normally
  attributed Runtime rows plus metadata-missing rows from the incident known as
  #21442. Select `LLM provider activity completed` and the same provider/island.
- Return bounded counts grouped by region, status, classified error and build;
  never project the raw `message`, prompts, identities or credentials.
- Expand the diagnostic window to include the five-minute-shifted evaluation.
  Say "no matching records" rather than "healthy" when the result is empty.
- Both links have `autoExecute: false`; automatic actions and mitigation are off.

## What was and was not proven

**Proven:** authorized PME resource-type creation; monitor POST HTTP 200 and ID;
independent native GET HTTP 200; state `2` (Silent); exact filters, timing,
conditions, outlet and mitigation read-back; refreshed list with one new monitor
and unchanged existing monitors. The portal preview showed historical IA101
request and failure data.

**Not proven:** all-provider/all-island commissioning, actual IcM firing or
recovery, EU query permission for every on-call identity, or successful execution
of the attached diagnostic queries. A diagnostic test was throttled (HTTP 429).
The saved monitor must not be described as active or fully commissioned on the
strength of configuration persistence alone.
