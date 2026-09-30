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
`scripts/vowdo/test-vectors.json`; Vowdo consumes the same file in tests)
covers Chinese text, quote/backslash escaping, `null` vs `false`, `0` vs
`-0` vs small/large numerics — proving Python and TypeScript agree byte-for-byte.

## 5. Optional runner extensions

Some independently maintained runners advertise `apply-config` and
`providerTransport` extensions. An application receipt can bind a requested
configuration digest, baseline identity, applied digest and transport
observations to the same run/attempt/case/workflow identity tuple.

The native preview implements transport for these frames. It does **not**
validate deep applied-configuration receipts or claim that a runner really
applied a candidate configuration. An extension being recognized is not
evidence of model improvement. Inspect the runner's advertised capabilities
and treat unsupported or unverified behavior as unsupported.

Live model execution is outside this release's scope. Shipped executions
use synthetic fixtures; simulated quantities remain distinct from real
priced usage. Credentials, when explicitly authorized for an external
runner, belong to its caller and must never travel in protocol frames or
exported evidence.
