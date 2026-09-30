# Native quickstart — 0.2.0rc5

The supported CSV journey runs locally on Linux x64 without Python, Deno,
a cloud account, API key or sibling repository when using the compiled
binaries. All shipped examples are synthetic.

## Download and verify

Download the Linux x64 archive and `SHA256SUMS` from the
[GitHub release](https://github.com/JosephDeepIntelli/vowdo-agent/releases/tag/v0.2.0rc5).

```sh
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf vowdo-agent-0.2.0rc5-linux-x64.tar.gz
cd vowdo-agent-0.2.0rc5-linux-x64
./bin/vowdo version
./bin/vowdo modes
```

Keep both binaries in `bin/`. CSV reconciliation needs no model provider.
Scripted worker execution needs `vowdo-worker` beside `vowdo`.

## Compare and inspect

```sh
./bin/vowdo examples --out samples
./bin/vowdo init --task-only --project work --purpose "supplier sync"
./bin/vowdo reconcile --project work \
  --left "samples/产品 目录.csv" --right "samples/supplier feed.csv" --join-key sku
./bin/vowdo runs --project work
./bin/vowdo run-status --project work RUN_ID
./bin/vowdo export-run --project work RUN_ID --out work/export
./bin/vowdo verify-export work/export
```

Replace `RUN_ID` with the returned ID. For your own inputs, replace the file
paths and join key. Input snapshots and results are persisted locally.
Changing an exported artifact causes verification to fail. Digests detect
changed bytes; they do not establish source truth or authorship. Task-only
workspaces refuse improvement commands rather than inventing approval owners.

## Build from source

Use Deno 2.9.7 on Linux x64 (the verified toolchain):

```sh
cd typescript
deno task fmt
deno task lint
deno task check
deno task test
deno task compile-all
bash scripts/verify-native.sh dist
```

The default test suite has one explicitly ignored integration test because
it requires an independently installed Choose runner. `VOWDO_TEST_CHOOSE_ROOT`
configures that optional runner; it is not needed for the native CSV workflow.
The source imports only built-ins; no remote modules are required.

## Runtime boundaries

The controller has host-wide filesystem read/write and subprocess authority.
It has no network or environment permissions in the compiled distribution.
The worker starts without grants, revokes all permission classes at startup,
and probes effective denial. Its inherited environment is cleared.
This is not a verified OS/VM sandbox for arbitrary untrusted code.

When available, `prlimit` applies a CPU-seconds cap. Worker memory has no
enforced OS cap. Message-size limits and deadlines do not bound its heap.
All model-task execution uses deterministic scripted fixtures. Live model
execution, applied-runner-config receipt validation, measured model
improvement and TUI are unsupported in this release.

## Existing workspaces

New workspaces use `.vowdo/` and the Vowdo tool identity. A workspace from
an earlier release is not silently reset or opened for new writes. Keep it
intact and use its matching historical release to inspect or export it.
Initialize a distinct new Vowdo workspace for new runs. Historical exports
can be verified read-only by the new release. Renaming a data directory
is not a migration.

The existing `vowdo import-python --from OLD_DIRECTORY --to NEW_DIRECTORY`
command remains an explicit import into a new directory; preserve the
original while reviewing the result. There is no automatic in-place
conversion of older native workspace state.
