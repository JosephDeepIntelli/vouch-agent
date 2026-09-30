# Vouch Agent

**Work you can inspect. Results you can verify.**

Vouch is DeepIntelli’s local-first project for business task execution and
controlled agent improvement. We are building toward a simple promise:
useful work should come with evidence, clear boundaries and a way to recover.

The **0.1.0rc1 developer preview** starts with a concrete workflow: compare
two CSV files, inspect what changed, and export a result you can verify
byte-for-byte. It runs locally without an account, API key or model call.
The broader runtime is available for experimentation; live AI execution
and measured agent-improvement gains are not supported claims in this release.

[Download preview](https://github.com/JosephDeepIntelli/vouch-agent/releases/tag/v0.1.0rc1) ·
[Quickstart](docs/quickstart.md) · [Architecture](docs/architecture.md) ·
[Contribute](CONTRIBUTING.md)

## Why Vouch?

An answer is only part of a completed task. You also need to know which
inputs produced it, what was checked, and whether it survived an interrupted
session. Vouch brings task execution, saved state and verifiable deliverables
into one local workflow.

- **Evidence you can reopen:** input snapshots and exported artifacts carry
  content digests; verification runs in a fresh process.
- **Differences you can act on:** changed values, missing rows, schema
  differences and duplicate keys are reported explicitly.
- **A useful starting point:** run the supported CSV journey without
  a sibling repository, cloud account or paid provider.
- **An inspectable foundation:** Python source, tests, synthetic fixtures and
  public adapter contracts are available under Apache-2.0.

Digest verification checks integrity against the manifest. It does not prove
that an input is true, authenticate its author or replace business review.

## Try it from source

Tested on **Linux with Python 3.12 and 3.13**. Python 3.14 is not supported;
Windows and macOS have not been verified. Install [uv](https://docs.astral.sh/uv/getting-started/installation/),
then run these commands from your checkout:

```sh
uv sync --locked --no-dev --python 3.13
uv run --no-sync vouch version
uv run --no-sync vouch examples --out samples
uv run --no-sync vouch init --task-only --project work --purpose "supplier sync"
uv run --no-sync vouch reconcile --project work \
  --left "samples/产品 目录.csv" --right "samples/supplier feed.csv" --join-key sku
uv run --no-sync vouch runs --project work
```

The samples are **synthetic**. They include a changed price, a row missing
from each side, a duplicate key and a column difference. Use the run ID
printed by `reconcile` in the following commands:

```sh
uv run --no-sync vouch run-status --project work RUN_ID
uv run --no-sync vouch export-run --project work RUN_ID --out work/export
uv run --no-sync vouch verify-export work/export
```

For your own data, substitute your file paths and join key. CSV inputs stay
local; comparing files does not contact a model provider. Installing
dependencies requires network access or a populated package cache.
See the [wheel quickstart](docs/quickstart.md) for installation outside a
source checkout. The [GitHub preview release](https://github.com/JosephDeepIntelli/vouch-agent/releases/tag/v0.1.0rc1)
includes installation artifacts and checksums. No PyPI publication is claimed.

## What works today

| Surface | Preview status |
| --- | --- |
| Task-only initialization and local CSV reconciliation | Supported |
| Saved-run inspection and native export verification | Supported |
| Recovery inspection and existing task controls | See the quickstart; short tasks may finish before a control request |
| TUI, JAZ/fixed-fact demonstrations, controlled-improvement experiments | Experimental |
| Choose adapter experiments and loopback provider simulations | Experimental; external runner prerequisites apply |
| Live model execution, autonomous business operations, cloud accounts and payments | Deferred |

Experimental approval/release commands record local decisions; they do not
publish software or deploy changes. Resource limits and in-process hooks
are not a security sandbox for arbitrary generated code.

## Build with us

We welcome developers who care about reliable agent workflows, not just
impressive demonstrations. Useful contributions include:

- Reproducible CSV edge cases and clearer reports.
- Cross-platform installation findings and accessibility improvements.
- Recovery, budget-accounting and evidence-integrity regressions.
- Adapter-contract feedback and carefully scoped evaluator experiments.

Start with a small issue or reproducible example. Explain the user’s problem,
expected behavior and how to verify it. Use synthetic data rather than
customer files or raw private traces. [CONTRIBUTING.md](CONTRIBUTING.md)
explains setup, checks and the boundaries we preserve.

## Open core, with a useful public core

Vouch’s public code is licensed under **Apache-2.0**. Basic correctness,
permissions, budget controls, evidence integrity and result export belong
in that core. Developers can use and extend it under the license terms.

Separately offered commercial services or extensions have their own terms;
they do not add restrictions to the Apache-2.0 core.

## Project structure

```text
src/vouch_agent/   CLI, runtime, contracts, storage, adapters and TUI
tests/            Logic, lifecycle, failure-path and interaction tests
fixtures/         Explicitly synthetic adapter examples
docs/             Public quickstart, architecture and adapter protocol
```

Vouch uses the pinned [JAZ](https://github.com/jaz-lang/jaz) runtime and
implements its own control and evidence boundaries. See
[third-party notes](THIRD-PARTY-NOTICES.md) and the [Apache-2.0 license](LICENSE).
Report sensitive vulnerabilities through [SECURITY.md](SECURITY.md).

Built by **DeepIntelli**. For pilot enquiries: [contact@dpintelli.com](mailto:contact@dpintelli.com).
