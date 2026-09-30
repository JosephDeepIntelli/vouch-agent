# Quickstart — the supported native journey (0.1.0rc1)

Everything here runs from an **installed wheel**, outside any source
checkout, with no Choose repository, no credentials, no workflow ids and no
owner identities. Verified on Linux with Python 3.12/3.13; no other platform
is claimed.

## 1. Install and get sample data

```sh
# stdlib venv (needs the python3-venv/ensurepip distribution package), or:
#   uv venv --seed --python 3.13 /tmp/vouch-preview
#   uv pip install --python /tmp/vouch-preview/bin/python vouch_agent-0.1.0rc1-py3-none-any.whl
python3.12 -m venv /tmp/vouch-preview && /tmp/vouch-preview/bin/pip install vouch_agent-0.1.0rc1-py3-none-any.whl
export PATH="/tmp/vouch-preview/bin:$PATH"
cd "$(mktemp -d)"
vouch version && vouch modes        # modes states honestly: live execution unsupported
vouch examples --out samples
```

`vouch examples` writes SYNTHETIC sample CSVs — invented data, deliberately
including a space + Chinese filename (`产品 目录.csv`) and a supplier feed
(`supplier feed.csv`) with one changed price, one row missing on each side,
an ambiguous duplicate key and a right-only column.

## 2. Initialize a task-only workspace

```sh
vouch init --task-only --project work --purpose "supplier sync"
```

Task-only mode records the purpose, needs no workflows or owner identities,
and refuses the improvement/approval commands (those need a default-mode
workspace with named owners — identities are never invented).

## 3. Reconcile, inspect, export, verify

```sh
vouch reconcile --project work \
    --left "samples/产品 目录.csv" --right "samples/supplier feed.csv" --join-key sku
vouch runs --project work
vouch run-status --project work <run-id>
vouch export-run --project work <run-id> --out work/export
vouch verify-export work/export
```

What you can rely on:

- **Deterministic + sensitive**: identical inputs produce identical report
  digests; any changed input row produces a new run and different export
  bytes. Inputs are snapshotted immutably by digest.
- **Honest reporting**: duplicate join keys are reported as ambiguous (never
  silently deduplicated), schema differences between the CSVs are listed,
  and missing rows are attributed to the correct side.
- **Byte-level verification**: `vouch verify-export` re-hashes every
  artifact against the export manifest; a tampered or truncated export
  fails. Exports reopen in a fresh process with the same verification.
- **Recovery**: `vouch resume --project work` shows recovery posture and
  supports explicit reconciliation of interrupted work.

Rollback/uninstall: delete the project directory (`work/`) and the virtual
environment. Nothing else is written outside the project directory.

## Experimental surfaces (developer-only, not this journey)

The improvement vertical, JAZ/fixed-fact tasks (`vouch run`), TUI (`vouch tui`),
evidence importers and pilot simulations are experimental. Choose-specific
experiments need a prepared runner configured through `VOUCH_CHOOSE_RUNNER_DIR`;
the TUI and local demonstrations do not all need that checkout. See
[architecture](architecture.md) and [adapter protocol](adapter-protocol-v1.1.md).
None makes a live-model claim; live execution is not implemented here.
