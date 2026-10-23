# Portal and native configuration recipe

This sequence was exercised in Geneva/Jarvis on 2026-10-21. Use it as a known
procedure, not a promise that selectors or permissions never change. It requires
an authenticated, authorized operator session. Do not embed a personal account,
private MCP address, browser target ID or session ID in reusable automation.

## Browser/MCP connection

1. Use the user-supplied endpoint or the host's configured browser tools. For
   Streamable HTTP MCP, initialize, retain the returned session identifier,
   send `notifications/initialized`, then discover `tools/list`. Use negotiated
   protocol headers; support JSON and SSE responses and check tool `isError`.
2. The exercised server exposed `list_tabs`, `select_tab`, `new_tab`, `eval`,
   `click`, `type`, `press_key` and `screenshot`. Discover their current schemas;
   names/arguments are not an authorization contract.
3. List tabs, select the exact target, then independently evaluate
   `({url: location.href, title: document.title})`. Check the Geneva URL before
   any modification. Do not keep running actions after selection fails.
4. If an old tab cannot be selected, preserve its draft. With permission, create
   a fresh tab at `https://portal.microsoftgeneva.com/manage/monitors?account=Lobster`.
   Explicitly select and verify the new tab; new-tab success alone is insufficient.
5. If the user switches to PME, verify the Profile identity in the new session
   and list monitors again. Do not assume old target IDs or cookies still apply.

A harmless popup can diagnose JavaScript execution if the user requests it.
Success in the Swarm or another tab proves only that tab works, not Geneva
attachment. Do not navigate away from an unsaved draft to fix tab selection.
Never extract browser cookies, bearer tokens, passwords or account caches.

## Creation sequence

### New Monitor → Basics

On **Manage Monitors**, choose **New Monitor**, select the authorized account
and **Monitor V2 (Metrics)**, then **OK**. First compare the saved monitor list
with the requested scope to prevent duplicates.

Set Name, State **Silent**, and Description. Include scope, eligibility,
thresholds, timing and commissioning limitations. Keep creation distinct from
activation. Native state values observed: `1 = Active`, `2 = Silent`,
`0 = Disabled`; verify the serialized state rather than relying on a label.

### Monitor Logic → Time Series

Set a short time-series Name, then Account, Namespace, Metric and Sampling Type.
Wait for each dependent selector to load before touching the next. Select an
existing compatible preaggregate/dimension set; don't create a different metric
view speculatively.

For the Runtime one-observation-per-request counter, **Count** is the verified
sampling choice. **Average** is the portal default and is wrong for that count.
Do not generalize this to all instruments; some metrics require Sum or other
semantics. The owning [MDM authoring contract](https://microsoft.ghe.com/bic/lobster/blob/main/docs/operations/grafana-mdm-dashboard-authoring.md)
remains authoritative.

### Resources

Use **+ Dimension** for each agreed grouping dimension. `Monitor.Tenant` is
provided by the portal. Inclusions `*` mean all observed values, not only a
previously discussed island list; restrict them when the approved scope is
narrower. Do not partition the resource by `outcome` when computing a ratio.

The portal finds resource types by the identifier-dimension set. If it shows
**No related resource type**, inspect that set, then use **Click here** to create
a matching type only with authorization. The dialog supplies Name, Identifier
dimensions, optional Environment/Region, Tree Name and Tree Hierarchy.

An observed generated type was
`Monitor.Tenant-ClusterCategory-IslandNumber-provider`. This is an example, not
a mandatory global resource type. Leave optional mapping fields unset unless
there is an established mapping; don't equate a geography label with a physical
region arbitrarily.

`Access is denied` / `ID4266` is a permissions failure. Record it and stop the
write; an authorized PME session resolved the recorded case. Do not loop on
Create, weaken the type, or attempt privilege changes yourself. Record a
successful type/tree creation immediately: it persists independently of the
monitor.

### Expressions

Set **Frequency** and **Lookback** in minutes. For each expression:

- Give it a descriptive name. A name such as "Failed requests" does not filter
  anything by itself.
- Select the time series, **Lookback Aggregation**, and
  **Multi-time-series Aggregation**. Request totals use **Sum / Sum**.
- Open **Where**. Verify **Inherit from resource** for every resource dimension
  in every numerator/denominator term. Non-grouped dimensions can aggregate
  across `*` only when that matches the contract.
- For an enumerated failure population, switch the inclusion filter from Glob
  to **Value** and select each exact outcome as a separate tag. Do not enter a
  concatenated string, use a blanket `!= success`, or make up a dimension value.
- A count has multiplier `1` and denominator `1`. A percentage has multiplier
  `100`, failure-count numerator and eligible-total denominator. Use the blue
  addition control **below the fraction** to add the denominator term; the one
  above it adds to the numerator.

**Duplicate expression** inserts the copy adjacent to the source. Re-find it by
its current expression name; do not assume it was appended to the end. Copied
filters must still be checked independently.

#### Timing and missing data

Expression **Advanced Options** exposes missing-data behavior, **End time shift**
and **Lookback**. The exercised rolling-window construction was:

| Expression pair | Lookback | End time shift |
| --- | --- | --- |
| Current count/rate | Global 15 minutes | None |
| Earlier count/rate | 15 minutes | 5 minutes |

At evaluation time `T`, these refer to `[T-15m,T]` and `[T-20m,T-5m]`.
Requiring both pairs to breach implements persistence across adjacent scheduled
window endpoints. It is a same-evaluation comparison of two historical windows,
not proof that a prior evaluator actually ran. Late-arriving data can change the
result relative to stored prior evaluation results. Don't describe it as two
non-overlapping 15-minute buckets.

Leave **Enable Bucket** off for that construction. If using native buckets
instead, agree their exact width and N-of-M semantics. Don't combine both forms
of persistence accidentally.

Choose missing-data behavior deliberately. The example retained the portal's
ignore-missing-data behavior; it is **not a separate telemetry-absence monitor**.
Its rule has `fireOnSkip=false`, `fireOnDivideByZero=false`, and automatic incident
mitigation disabled. No observed traffic must never be presented as proof of
provider health. A positive-evidence recovery policy needs separate validation;
it is not implemented merely by those flags.

### Alert Conditions

Within **one** condition, set name, health status, severity, expression,
**Static Threshold**, comparator and threshold. **+ Clause** adds AND conditions.
Creating another alert condition is not equivalent to adding an AND clause.
For "at least three," choose `>= 3`, not `> 3`.

Verify the native representation: `comparator: 2` was `>=`, aggregation `0` was
Sum, and regular dimension filters used exact-value `filterType: 0` versus glob
`filterType: 1`. Inherited dimensions used `type: 1`. These mappings document the
observed native schema; always compare labels and read-back if the schema changes.

### Customize Incident

Choose **Route Incident Using → IcM**, enter the verified Routing ID, and set
Title, Message and the required **TSG ID / link**. Reuse an actual appropriate
runbook, not a fabricated number. Resource template placeholders include:

```text
{Monitor.Dimension.provider}
{Monitor.Dimension.IslandNumber}
{Monitor.Dimension.ClusterCategory}
```

The recorded UI review displayed "Routing ID configured in Resource Type
Configuration" despite the editor holding an explicit IcM outlet. Native
serialization/read-back resolved the discrepancy: verify `outlets[].routingId`
and the condition's `alertOutletIds`, not only the review prose. Do not silently
accept a missing route on the assumption that the resource type supplies it.

### Diagnostic Data → Alert Reaction

**Kusto Queries → Add** accepts Title, Cluster, Database and Query. Relevant
sources for the example were US CSCAnalytics and EU CSCAnalytics; choose sources
for the actual monitored resources rather than always querying only US.

Templates include `{Monitor.DataStartTime}`, `{Monitor.DataEndTime}` and resource
dimensions. Include shifted lookback time where necessary. Return bounded
aggregate status/error/build counts, never raw messages or customer content.
A zero-row result or 403 must not be reported as healthy. Validate template
expansion and KQL separately; an HTTP 429 test failure is not passing evidence.

Leave **Embed the result** off unless authorized and verified. A saved query link
is still useful; native `autoExecute: false` proves that automatic enrichment is
disabled, not that the query works.

In **Alert Reaction**, leave **Auto-mitigate incident** unchecked unless an agreed
positive-evidence recovery rule was validated. Do not add actions/workflows as a
side effect of monitor creation.

## UI automation pitfalls

Prefer current tool schemas and visible controls, not screen coordinates or
hardcoded widget indexes. After asynchronous UI changes, re-read the DOM.

- Kendo selectors load asynchronously. Wait for options and read resulting values.
- React tag inputs require selecting the dropdown item. In the observed MCP,
  Enter alone did not commit tags; subsequent typing concatenated the values.
  Read the actual selected tags. `input.select()` followed by the supported
  typing tool can replace text when a Ctrl+A shortcut is unreliable.
- Review and edit can expose duplicate/hidden controls. A broad Create selector
  matched a hidden button; MCP reported a click at `(0,0)` and navigated home.
  Select a uniquely identified **visible** control, check its bounding rectangle
  and enabled state, and confirm the URL. A reported click is not a save receipt.
- Read-only JavaScript can inspect DOM values. If ordinary DOM events do not
  update the framework state, use the supported control and inspect native
  serialization rather than changing private application internals.
- Use the editor's **More Options → Export/Import** to preserve/reuse a safe native
  draft. File selection can use supported upload tooling. When only `eval` is
  available, a browser `File`/`DataTransfer` can populate the observed import
  input with the already verified JSON. That restores a draft; it does not save
  or activate it. Recheck name, account, identity, IDs and Silent state afterward.
- Do not copy session-specific scripts that patch XMLHttpRequest/prototypes as
  routine automation. If an explicitly scoped local preflight interception is
  necessary, prove it blocked the write before retrying and restore instrumentation.
  Never claim an intercepted request is a backend receipt.

## Native save and independent read-back

The exercised same-origin portal endpoints were:

```text
POST /user-api/v1/config/monitor/tenant/{account}/configuration
GET  /user-api/v1/config/monitor/tenant/{account}/configuration/{monitorId}
```

The POST returned HTTP 200 with the new monitor ID. Do not guess an update/delete
verb or replay a creation POST after a lost response. Inspect the live list by
name/scope and GET the candidate ID first. An explicitly approved native write
must use the same authorized operator path and checks as a UI write.

After a successful create, GET using the authenticated portal session and
`Accept: application/json`. Without that header, the response may be XML; don't
mistake successful XML for a failed save. A native browser read needs no token
extraction:

```javascript
const response = await fetch(
  `/user-api/v1/config/monitor/tenant/${encodeURIComponent(account)}/configuration/${encodeURIComponent(monitorId)}`,
  { credentials: 'same-origin', headers: { Accept: 'application/json' } }
);
if (!response.ok) throw new Error(`Monitor read-back HTTP ${response.status}`);
const saved = await response.json();
```

Verify `state`, resource bindings, time-series sampling, each term's filters and
aggregation, multipliers, `endTimeShift`, condition expression IDs/comparators,
severity, outlet IDs/route, skip/divide-by-zero flags, enrichment and mitigation.
Check the refreshed account list and obtain the actual saved monitor link.
Server-normalized empty lookbacks may become `null`; compare effective timing.
Record successful side effects immediately so a resumed session does not create
a duplicate. Keep safe configuration/receipt artifacts, never credentials.
