# Adapter protocol v1.1 (additive extension of v1)

Public reference for the additive v1.1 adapter contract. External runner
implementations are maintained independently; they are not shipped here.

Version 1 frames remain valid. Version 1.1 adds the following identity,
metering and transport requirements:

## 1. Identity echo on `execute`

Requests carry a full identity tuple; responses echo it verbatim. The client
rejects any response whose echo mismatches the request (kind/seq alone do not
prove which run/attempt/case/version/mode a result belongs to).

```json
"identity": {
  "runId": "...", "attemptId": "...", "workflowId": "...",
  "caseId": "...", "mode": "fixture", "versionDigest": "sha256:..."
}
```

`versionDigest` may be omitted only when genuinely unknown.

## 2. Metering field separation

`usage` never merges simulated quantities into priced cost:

| Key | Meaning |
| --- | --- |
| `tokensScripted` | deterministic provider token counts (simulated) |
| `elapsedMsMeasured` | measured local wall-clock (real measurement, unpriced) |
| `creditsSimulated` | simulated credit outcome (fixture) |
| `costUsd` | real priced cost — **only** when one exists; fixture runs omit it |

At least one numeric field remains mandatory; non-finite numbers are rejected
(`ProtocolFrameError`/`MissingMeteringError` semantics).

## 3. Bounded artifact transfer in `collect`

Artifacts travel in-frame, content-addressed, never as host paths:

```json
{"digest": "sha256:...", "bytes": "<base64>", "kind": "report|evidence|usage"}
```

The client re-verifies every digest against the bytes and enforces a size cap
(default 32 MiB per artifact). A host path is not a read capability.

## 4. Canonical hashing + shared cross-language test vectors

sha256 over UTF-8 canonical JSON: sorted keys, separators `(",", ":")`,
`ensure_ascii=False`. Shared vector file (Choose side:
`scripts/vouch/test-vectors.json`; Vouch consumes the same file in tests)
covers Chinese text, quote/backslash escaping, `null` vs `false`, `0` vs
`-0` vs small/large numerics — proving Python and TypeScript agree byte-for-byte.

## 5. `apply-config` — the candidate application operation (v1.2, additive)

A run-scoped `apply-config-request` applies a digest-bound, versioned
prompt/config delta against the runner's IMMUTABLE baseline evaluation
configuration and executes the real controller with the APPLIED configuration
visible to the transport. The `apply-config-response` carries the application
receipt (requested delta digest, actually-applied config digest, baseline
digest + version, runner/source identity, run/attempt/case/workflow ids,
mode, no-op verdict, observed-config digest, transport-observed flag) plus
the same outputs/usage/identity-echo shape as an execute response.

Frame semantics identical to execute: run-scoped kinds, verbatim identity
echo, mandatory metering, mode agreement kill. Choose's runner implements it
(`scripts/vouch/runner.ts`); Vouch's client side lives in
`vouch_agent.adapters.choose_bundle` (bundle parse/verify, receipt
validation) and `vouch_agent.appservices.choose_apply` (the paired
baseline-noop/candidate-bundle client). The public flow uses it for BOTH
sides of an improvement comparison — baseline runs an explicit empty bundle,
the candidate runs its sealed change bundle — so evidence differences come
from the applied configuration alone. Supported application scope is exactly
the runner's advertised `evaluationCases` with `application: true` (today:
W-C2 `case_apply_config_kettle_en`); the flow refuses unsupported improvement
cases before dispatch instead of executing an unchanged agent.

## 6. `providerTransport` on apply-config (v1.3, additive)

The apply-config payload may carry `providerTransport` — a full
`choose-provider-config` — switching that execution to TRANSPORT SIMULATION:
the model dependency becomes the existing product seam over the existing
`ProviderTransport` (reservations, accounting, retries, cancellation all
real) against a provider endpoint. The runner accepts LOOPBACK endpoints
only, and only with an explicit `allowInsecureLoopback` policy; credentials
resolve by NAME from the runner's `VOUCH_PILOT_CREDENTIAL_*` environment
(the owning trusted service's seam — values never travel in frames or
configs); failures hold their reservation with NO scripted fallback; one
transport per config digest makes its `spendCapUsd` the ROOT budget across
every attempt of an evaluation; usage carries `costUsd` from the transport's
accounting and a sealed `transport-accounting` artifact records the
root-budget view. `authorizedLive` remains unimplemented and is advertised
as such in `describe().providerModes`, alongside the versioned, enforced
`workBudget` envelope a pilot's worst-case budget must derive from.
