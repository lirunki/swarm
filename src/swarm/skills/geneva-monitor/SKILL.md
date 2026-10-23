---
name: geneva-monitor
description: |
  Propose, create, update, or verify a Lobster Geneva/MDM metric monitor with
  IcM routing. Use for "create an IcM monitor", "add a provider failure alert",
  "configure a Geneva monitor", or guided monitor setup in an authenticated
  Edge session. Supports user-provided browser MCP tools, defaults new monitors
  to Silent, and verifies the persisted native configuration. Not for querying
  an incident alone, changing application telemetry, creating Grafana alerts,
  or deploying Runtime/container images. Proposal-only requests make no writes;
  enabling paging requires explicit authorization.
---

# Geneva / IcM monitor

This is a **Swarm companion workflow** stored beside the canonical Swarm skill at
`geneva-monitor/SKILL.md`. The canonical `swarm.md` tells managed agents when to
read it; it is intentionally not copied into Pi's user skill-discovery directory.
Do not create or switch sessions just to use this workflow.

Bundled relative links resolve from this skill directory. Repository documentation/source
citations are absolute GitHub URLs and are not bundled files. When source is
needed, use the intended Lobster checkout if available, or read the linked
repository content with the authorized GitHub/browser tools. Do not assume the
current working directory is Lobster or clone a repository automatically.

Create a native Geneva metric monitor, not a polling service or an IcM incident.
Reuse the proven procedure below; re-derive live permissions, metric coverage,
resource values, duplicates, and saved state. Historical instructions are not
permission to skip those checks.

## Inputs and authority

Establish only the decisions that materially affect the monitor:

- Proposal only, guided clicks, or authorized creation/update? A proposal makes
  no external writes. Do not treat creation as permission to enable paging.
- Monitoring account/namespace, metric, counted event, eligible population,
  grouping dimensions, failure conditions, timing, severity and owning route.
- For updates, the exact monitor ID and approved delta. Export its current
  configuration; preserve unrelated fields and history.
- Authorized browser session or existing operational tooling. A supplied MCP
  endpoint is session-specific: discover its tools, verify the selected URL
  and signed-in identity, and keep credentials inside the established flow.

New monitors start **Silent** unless the user explicitly authorizes another
state. This is a configuration-only operation: do not rotate keys, restart
pods, change ECS, or deliberately break production to prove an alert.

## Known entry points

- [Geneva Monitors](https://portal.microsoftgeneva.com/manage/monitors?account=Lobster)
  — account `Lobster`; create **Monitor V2 (Metrics)**.
- [Existing reliability monitor contract](https://microsoft.ghe.com/bic/lobster/blob/main/docs/operations/cxp-reliability-monitors.md)
  — native configuration, routing and publication precedent.
- [MDM authoring contract](https://microsoft.ghe.com/bic/lobster/blob/main/docs/operations/grafana-mdm-dashboard-authoring.md)
  — metric semantics, preaggregates and admission; not a Grafana-alert workflow.
- [Portal and native configuration recipe](references/portal.md)
  — exact sequence, MCP pitfalls, enum mappings and save/read-back protocol.
- [LLM provider example](references/llm-provider.md)
  — proven composition, not universal alert thresholds.

Use these references rather than rediscovering portal mechanics. Follow the
nearest `AGENTS.md` and the current metric's owning source if instrumentation
or interpretation is uncertain. Source instrumentation alone is not proof
that the deployed metric is arriving.

## 1. Inspect without changing anything

1. Open/select the requested tab and verify `location.href` and title. Browser
   tools reporting success does not prove the intended tab is selected.
2. Open Profile and confirm the intended operator identity. A corporate account
   can read monitors but lack resource-type creation rights. PME worked in the
   recorded workflow, but being a PME account is not itself authorization.
3. List existing monitors for the account. Compare names, metric, grouping and
   effective conditions; reuse the approved target instead of creating a twin.
4. Verify the live metric, sampling type, admitted dimensions and preaggregate.
   Inspect representative data and candidate resource tuples. Do not infer
   zeros from access-denied, missing-series or empty-preview results.
5. Read back a relevant existing monitor's route if reusing the on-call team.
   `MDM://Project Lobster/Lobster Runtime` is an observed Runtime route, not the
   default owner for every Lobster subsystem.

On authorization failure, preserve the draft and exact error. Ask an authorized
owner to create the required resource type or supply an approved session/role.
Do not change identity or use another credential without user authorization,
weaken resource grouping to get past a denial, or bypass permission checks.

## 2. Freeze the detection contract

Write a compact decision table before composing the rule:

| Decision | Required precision |
| --- | --- |
| Event count | Is this one sample per request, a gauge, histogram, or exported delta? Verify the correct sampling/aggregation. |
| Numerator | Explicit failure states; distinguish provider failures from policy rejection, cancellation and expected incomplete work. |
| Denominator | Same resource/time scope; successes plus eligible failures, not an unrelated global count. |
| Grouping | Each independently actionable incident; keep outcomes inside the expression, not separate incident resources. |
| Timing | Window, evaluation interval, persistence and ingestion delay; name what each setting actually measures. |
| Thresholds | Both volume and rate where needed; establish low-volume detection latency and expected noise. |
| Missing data | Unknown/no evidence is not healthy. Define skip/zero behavior and whether automatic mitigation is allowed. |
| Ownership | Route, severity, actionable runbook, commissioning and explicit activation owner. |

Important distinctions:

- Resource grouping partitions evaluations and incidents. If this is an alert
  **per provider**, `provider` is a resource dimension and expressions inherit
  it. For an Anthropic-only alert, a literal provider filter may suffice.
- Dropping `Geo` combines matching geographic series. Never assume it is
  redundant: inspect the remaining resource tuples and agree on aggregation.
- A percentage is `100 * sum(failures) / sum(eligible calls)`, not an average
  of per-series percentages. Apply identical inherited resource bindings.
- Native "N of M buckets" is not necessarily N consecutive evaluations of a
  rolling window. Use the verified native timing model in the portal reference.

Replay the candidate logic against a bounded baseline when evidence is
available. Label log replay separately from live MDM preview and actual incident
routing. Neither a historical outage nor a valid JSON document proves delivery.

## 3. Compose in the authorized session

Follow [the portal recipe](references/portal.md). Configure Basics, Time Series,
Resources, Expressions, Alert Conditions, Incident Routing, Diagnostic Data,
and Alert Reaction. Confirm each control's resulting value before proceeding.

- Keep state Silent throughout. Never temporarily enable Active to test.
- Reuse an exact matching resource type. Create one only when necessary for
  the agreed grouping and authorized; resource-type creation is a separate
  persistent side effect even if monitor creation later fails.
- Keep distinct failures and totals, with explicit filters and inherited
  resource bindings. Adding a label does not add a filter.
- Use bounded, aggregate-only diagnostics. Include the relevant regional query
  sources and honest missing-data behavior. Do not put raw prompts, content,
  tokens or per-user identifiers into incidents.
- Leave automatic enrichment, remediation and incident mitigation off unless
  specifically agreed and validated. Attach diagnostic links without claiming
  that query execution or enrichment has been tested.
- Supply the actual TSG/runbook link and an actionable incident title/message.
  Do not invent a TSG ID or unrelated on-call route to satisfy a required field.

## 4. Save once and independently verify

Before submitting, verify the native representation or complete review:

- correct account, name, target dimensions, metric and sampling;
- exact filter values and resource inheritance in **every** term;
- aggregation, percentage multiplier, denominator, timing and AND/OR semantics;
- severity, outlet ID binding and routing ID;
- Silent state, missing-data/divide-by-zero policy, automation and mitigation;
- diagnostic parameters, data minimization and auto-execution state.

Save once using a uniquely identified visible control. Record response status,
monitor ID and time immediately. On a timeout, navigation glitch or lost
response, inspect the live monitor list/configuration before any retry. A
resource type may exist even when no monitor was saved.

Independently GET the saved native configuration by returned ID with
`Accept: application/json`, then refresh the list. Compare the effective fields,
not server-normalized metadata or merely the displayed form. Confirm no duplicate
was created. Preserve a safe native read-back artifact, not browser session data.

A successful save and read-back prove configuration persistence. They do **not**
prove that every resource emits metrics or that an IcM incident was delivered.

## 5. Close out and activate separately

Return the monitor link/ID, effective grouping/conditions, state, routing,
read-back evidence, additional resources created, and remaining proof gaps.
Say explicitly whether paging is enabled. Stop after Silent creation unless
activation/monitoring was requested; do not silently begin an ongoing watcher.

Before authorized activation: establish live coverage, inspect the Silent
baseline, validate meaningful recovery/no-data behavior, and verify delivery
through the owner's approved test route. Do not claim a test incident succeeded
without its receipt. Rollback is the agreed state transition to Silent/Disabled,
not deleting an existing monitor or disabling unrelated alerts.

If repository changes are requested to retain the definition, follow that
checkout's implementation/publication contract separately. This personal skill
is not a repository change and does not require a Lobster PR. Validate skill
metadata and bundled links with Pi's loader; do not add broad builds, prose
tests, live monitor writes or browser tests as routine validation.
