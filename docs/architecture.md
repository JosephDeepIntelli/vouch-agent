# Architecture and trust boundaries

The product runtime is TypeScript on Deno. The native Linux x64 preview
includes a CLI controller and a separate worker executable.

## Supported local task path

```text
CSV inputs -> immutable snapshots -> reconciliation
           -> durable run and report -> export -> digest verification
```

SQLite stores local state and events; content-addressed storage retains
artifact bytes. Task-only workspaces record their purpose without inventing
approval owners. Export verification detects changed bytes, unsafe paths,
linked entries and incomplete packages. It does not authenticate authorship
or prove the underlying input is true.

## Execution and recovery

The controller owns policies, query-budget reservations and durable state.
The separate worker executes scripted fixture steps through a framed
protocol. Subprocess environments are explicitly cleared. The worker
revokes ambient permission grants and functionally checks effective denial
at startup; the controller rejects reported weakened boundaries.

Pause/resume/cancel and recovery tests cover interrupted work and ownership
fencing. An unknown outcome requires reconciliation rather than silently
replaying a potentially completed action. Budget authority stays in the
controller and covers nested queries.

The controller retains host-wide filesystem and process authority. Worker
CPU limits apply only when `prlimit` is available; memory has no enforced
OS cap. This preview is not a verified OS/VM sandbox for arbitrary untrusted
code.

## Improvement fixtures and adapters

Candidates, evaluation runs, evidence, acceptance and approvals bind to
content digests. Versioned subprocess adapters connect independently
prepared runners without importing sibling source. The Choose runner
integration is verified when explicitly configured.

Improvement is a fixture protocol/lifecycle demonstration. Validation of
applied runner configuration and real model-quality gains is not supported.
All shipped inputs and example outcomes are synthetic. Live model execution
and TUI are outside this release's supported scope.

## Source map

Under `typescript/src/`: `contracts/` defines identities and records;
`appservices/` and `orchestrator/` coordinate work; `storage/` owns durable
state, artifacts and budgets; `runtime/` contains controller/worker ports;
`adapters/` contains the public protocol transport; `improvement/` contains
the fixture evaluation lifecycle; `cli/` exposes commands.

See the [adapter protocol](adapter-protocol-v1.1.md) and
[quickstart](quickstart.md). The previous Python implementation remains
available at its immutable public tag as a compatibility reference.
