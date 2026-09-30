# Architecture and trust boundaries

Vouch separates task execution from the decisions that determine whether its
results can be accepted. The supported CSV workflow is deterministic; the
general agent and improvement paths remain experiments.

The product runtime target is TypeScript on Deno. The published 0.1.0rc1
implementation is Python-based and remains the reference until the native
replacement is verified. Current setup instructions describe that published
implementation, rather than requiring an unimplemented TypeScript build.

## Supported task path

```text
CSV inputs -> immutable material snapshots -> reconciliation
           -> saved run and report -> export manifest -> digest verification
```

Task-only workspaces record their purpose without inventing approval owners.
Improvement and approval commands refuse that workspace mode. Task state,
events and artifact references are persisted locally using SQLite and
content-addressed storage. Verification detects bytes that disagree with the
export manifest; it does not establish source authenticity or truth.

## Experimental improvement path

Contracts represent tasks, candidates, runs, budgets and evidence. The
controller owns policy and decisions; workers execute bounded work. JAZ is
used through a pinned Python dependency. Versioned adapters connect
independently prepared products through subprocess protocols. Runner
locations require explicit configuration.

Independent acceptance, complete cost accounting and recovery from unknown
outcomes are design requirements exercised by failure-path tests. They are
not a claim that arbitrary generated code is safely sandboxed. Resource
limits and Python hooks alone do not provide that guarantee.

## Evaluators

The planned evaluator boundary separates execution, deterministic validation,
advisory model grading and final acceptance policy. Specialist graders may
receive authorized reference evidence; they do not acquire action authority.
Final acceptance examples stay separate from training and feedback data.
No live evaluator integration or model-quality improvement is supported in
0.1.0rc1. See the [preview scope](../README.md#what-works-today).

## Source map

- `contracts/`: typed identities, tasks, candidates and evidence records.
- `appservices/` and `controller/`: workflow orchestration and control.
- `storage/` and `ledger/`: durable state, budgets and accounting.
- `runtime/` and `orchestrator/`: bounded execution and lifecycle.
- `adapters/`: external protocol boundaries.
- `cli/` and `tui/`: CLI and experimental terminal UI.

Public protocol reference: [adapter protocol](adapter-protocol-v1.1.md).
