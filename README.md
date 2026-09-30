<p align="center">
  <img src="assets/mascot.png" width="200" alt="Vowdo’s jade-green rooster mascot holding an evidence notebook">
</p>

# Vowdo Agent

**Work you can inspect. Results you can verify.**

[Website — coming soon](https://vowdo.dpintelli.com) ·
[Download preview](https://github.com/JosephDeepIntelli/vowdo-agent/releases/tag/v0.2.0rc5) ·
[Contribute](CONTRIBUTING.md)

Vowdo is an open-source, local-first agent core for inspectable execution,
verifiable outputs and durable recovery. Useful work should come with
evidence, clear boundaries and a way to recover.

The **0.2.0rc5 native preview** is built with **TypeScript on Deno**.
Download the Linux x64 binaries and run them locally: no Python, account,
API key or model call is needed for the supported CSV workflow. Compare
two files, inspect what changed, and export a result you can verify
byte-for-byte.

Scripted task execution, recovery, budgets and improvement fixtures are
available for experimentation. Live model execution, applied-runner-config
receipt validation and measured agent-improvement gains are not supported
claims in this release.

[Quickstart](docs/quickstart.md) · [Architecture](docs/architecture.md) ·
[Adapter protocol](docs/adapter-protocol-v1.1.md) · [Security](SECURITY.md)

## Why Vowdo?

<p align="center">
  <img src="assets/evidence-workflow.webp" width="800" alt="Concept illustration of a team defining a task, inspecting evidence, and evaluating a result">
</p>

*Our direction: define the task, inspect the evidence, evaluate the result.
Concept artwork; not a product screenshot or a claim of measured performance.*

An answer is only part of a completed task. You also need to know which
inputs produced it, what was checked, and whether it survived an interrupted
session. Vowdo brings task execution, saved state and verifiable deliverables
into one local workflow.

- **Evidence you can reopen:** input snapshots and exported artifacts carry
  content digests; verification runs in a fresh process.
- **Differences you can act on:** changed values, missing rows, schema
  differences and duplicate keys are reported explicitly.
- **A useful starting point:** run the supported CSV journey without
  a sibling repository, cloud account or paid provider.
- **An inspectable foundation:** TypeScript source, tests, synthetic fixtures and
  public adapter contracts are available under Apache-2.0.

Digest verification checks integrity against the manifest. It does not prove
that an input is true, authenticate its author or replace independent review.

## Try the native preview

Download `vowdo-agent-0.2.0rc5-linux-x64.tar.gz` and `SHA256SUMS` from the
[release page](https://github.com/JosephDeepIntelli/vowdo-agent/releases/tag/v0.2.0rc5).
Verify the download, then extract it:

```sh
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf vowdo-agent-0.2.0rc5-linux-x64.tar.gz
cd vowdo-agent-0.2.0rc5-linux-x64
./bin/vowdo version
./bin/vowdo examples --out samples
./bin/vowdo init --task-only --project work --purpose "supplier sync"
./bin/vowdo reconcile --project work \
  --left "samples/产品 目录.csv" --right "samples/supplier feed.csv" --join-key sku
./bin/vowdo runs --project work
```

The samples are **synthetic**. Use the run ID printed by `reconcile`:

```sh
./bin/vowdo run-status --project work RUN_ID
./bin/vowdo export-run --project work RUN_ID --out work/export
./bin/vowdo verify-export work/export
```

Keep `vowdo` and `vowdo-worker` together in `bin/`. This preview is verified
on **Linux x64**; Windows, macOS, ARM and broader Linux compatibility are
not verified. See the [quickstart](docs/quickstart.md) for permissions,
building from source and the previous Python release.

## What works today

| Surface | Preview status |
| --- | --- |
| Task-only initialization and local CSV reconciliation | Supported |
| Saved-run inspection and native export verification | Supported |
| Recovery inspection and existing task controls | See the quickstart; short tasks may finish before a control request |
| Scripted worker execution, recovery and budget controls | Fixture mode; no live model calls |
| Controlled-improvement experiments | Fixture protocol/lifecycle only; no proven model gains |
| TUI | Deferred |
| Choose adapter experiments and loopback provider simulations | Experimental; external runner prerequisites apply |
| Live model execution | Unsupported in this preview |

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

## License

Vowdo is licensed under [Apache-2.0](LICENSE). Embedded runtime attribution
is included in [third-party notices](THIRD-PARTY-NOTICES.md).

## Project structure

- `typescript/src/`: contracts, orchestration, durable state, adapters and CLI.
- `typescript/tests/`: behavioral tests, adversarial cases and synthetic fixtures.
- `typescript/licenses/`: embedded runtime attribution.
- `docs/`: quickstart, architecture and adapter protocol.

The historical Python implementation remains at
[`v0.1.0rc1`](https://github.com/JosephDeepIntelli/vowdo-agent/tree/v0.1.0rc1).
See [LICENSE](LICENSE), [third-party notices](THIRD-PARTY-NOTICES.md)
and [security reporting](SECURITY.md).

See [contributors](CONTRIBUTORS.md).
