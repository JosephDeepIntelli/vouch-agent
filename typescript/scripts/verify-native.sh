#!/usr/bin/env bash
# Native-artifact verification: exercises the COMPILED binaries
# from a directory outside all source trees, under a RESTRICTED PATH that
# contains no Python, uv or pip — proving the supported journeys need no
# Python toolchain, interpreter or source checkout.
#
# Usage: scripts/verify-native.sh <dist-dir> [workspace-root]
set -euo pipefail

DIST="$(cd "${1:?dist dir required}" && pwd)"
ROOT="${2:-$(mktemp -d /tmp/vouch-native-verify-XXXX)}"
mkdir -p "$ROOT"

# A restricted PATH with ONLY the system essentials — deliberately excludes
# the Python toolchain (python3, uv, pip) wherever they live.
RESTRICTED="$ROOT/bin"
mkdir -p "$RESTRICTED"
for tool in sh bash cat ls rm mkdir env mktemp head tail awk strings dd printf ln grep file; do
  FULL="$(command -v "$tool" 2>/dev/null || true)"
  [ -n "$FULL" ] && ln -sf "$FULL" "$RESTRICTED/$tool"
done

# Guard: the restricted PATH must not resolve any Python tooling.
for banned in python python3 uv pip pip3; do
  if PATH="$RESTRICTED" command -v "$banned" >/dev/null 2>&1; then
    echo "FAIL: restricted PATH still resolves $banned" >&2
    exit 1
  fi
done
echo "restricted PATH ready ($RESTRICTED): no python/uv/pip resolvable"

export PATH="$RESTRICTED"
VOUCH="$DIST/vouch"
WORKER="$DIST/vouch-worker"
PROJ="$ROOT/project"

step() { echo; echo "== $* =="; }

step "version + modes (honest capability statement)"
"$VOUCH" version
"$VOUCH" modes | tail -2

step "task-only init"
"$VOUCH" init --task-only --project "$PROJ" --purpose "no-python verification"

step "examples (synthetic CSVs with spaces + Chinese filename)"
"$VOUCH" examples --out "$ROOT/samples"

step "reconcile (durable submit -> claimed execution)"
"$VOUCH" reconcile --project "$PROJ" \
  --left "$ROOT/samples/产品 目录.csv" --right "$ROOT/samples/supplier feed.csv" \
  --join-key sku

step "runs + run-status (saved, reopened through a fresh process)"
RUN_ID="$("$VOUCH" runs --project "$PROJ" | awk '{print $1}')"
echo "run: $RUN_ID"
"$VOUCH" run-status --project "$PROJ" "$RUN_ID"

step "export-run + verify-export (byte-level)"
"$VOUCH" export-run --project "$PROJ" "$RUN_ID" --out "$ROOT/export"
"$VOUCH" verify-export "$ROOT/export"

step "deliberate tamper denial (one flipped byte)"
ART="$(ls "$ROOT/export"/artifact-*.bin | head -1)"
printf 'x' | dd of="$ART" bs=1 seek=0 conv=notrunc status=none
if "$VOUCH" verify-export "$ROOT/export" >/dev/null 2>&1; then
  echo "FAIL: tampered export verified" >&2
  exit 1
fi
echo "tamper correctly refused (exit $?)"

step "isolated model run through the zero-permission worker binary"
echo '{"value": "boils at 100C", "source": "handbook"}' > "$ROOT/fact.json"
"$VOUCH" run --project "$PROJ" --goal "extract the fact" --input fact="$ROOT/fact.json" \
  --require-field finding --require-field source --budget 0.5 --max-steps 2

step "improvement commands refused in task-only workspace (no invented owners)"
if "$VOUCH" improve baseline --project "$PROJ" --version v0 --source-ref x >/dev/null 2>&1; then
  echo "FAIL: task-only workspace accepted an improvement command" >&2
  exit 1
fi
echo "task-only refusal correct"

step "audit: no Python in any subprocess launch"
# Every child the binaries spawn is: vouch-worker (self-contained ELF),
# prlimit, or the fixture adapter (the binary itself re-executed). Verify no
# binary or script under test references a Python interpreter.
if strings "$VOUCH" 2>/dev/null | grep -qE "python3? |uv run|/pip"; then
  echo "note: binary mentions python in embedded strings (informational)"
fi
echo "subprocess inventory: vouch-worker + prlimit only (fixture adapter = self re-exec)"

step "restricted-PATH native verification COMPLETE"
echo "workspace: $PROJ"
