# Native quickstart — 0.2.0rc4

The supported CSV journey runs locally on Linux x64 without Python, Deno,
a cloud account, API key or sibling repository when using the compiled
binaries. All shipped examples are synthetic.

## Download and verify

Download the Linux x64 archive and `SHA256SUMS` from the
[GitHub release](https://github.com/JosephDeepIntelli/vouch-agent/releases/tag/v0.2.0rc4).

```sh
sha256sum --ignore-missing -c SHA256SUMS
tar -xzf vouch-agent-0.2.0rc4-linux-x64.tar.gz
cd vouch-agent-0.2.0rc4-linux-x64
./bin/vouch version
./bin/vouch modes
```

Keep both binaries in `bin/`. CSV reconciliation needs no model provider.
Scripted worker execution needs `vouch-worker` beside `vouch`.

## Compare and inspect

```sh
./bin/vouch examples --out samples
./bin/vouch init --task-only --project work --purpose "supplier sync"
./bin/vouch reconcile --project work \
  --left "samples/产品 目录.csv" --right "samples/supplier feed.csv" --join-key sku
./bin/vouch runs --project work
./bin/vouch run-status --project work RUN_ID
./bin/vouch export-run --project work RUN_ID --out work/export
./bin/vouch verify-export work/export
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
it requires an independently installed Choose runner. `VOUCH_TS_TEST_CHOOSE_ROOT`
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

## Existing Python workspaces

The immutable [Python release](https://github.com/JosephDeepIntelli/vouch-agent/releases/tag/v0.1.0rc1)
remains available. Native workspaces use their own format; do not replace
binaries and assume an existing Python workspace is writable.
`vouch import-python --from OLD_DIRECTORY --to NEW_DIRECTORY` performs an
explicit migration with a read-only source. Native export verification can
also inspect Python exports. Preserve the original data while reviewing a
migration.
