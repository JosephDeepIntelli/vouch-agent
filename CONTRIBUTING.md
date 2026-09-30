# Contributing to Vouch

Help make agent work easier to inspect, verify and recover. Small,
reproducible improvements are especially welcome during the developer preview.

## Set up

Use Linux and Python 3.12 or 3.13 with uv. From your checkout:

```sh
uv sync --locked --group dev --python 3.13
uv run --no-sync vouch --help
uv run --no-sync pytest
uv run --no-sync ruff check src tests
uv run --no-sync mypy src
```

Some integration tests explicitly skip without independently prepared
Choose/Visibility runners. This public repository does not ship those sibling
products. Report exact pass/fail/skip counts; skipped coverage is not verified
behavior. The native CSV journey needs neither runner. Integration tests
declare their external-runner configuration prerequisites.

Run targeted tests while working, then the relevant checks before submitting.
For a bug fix, add a regression that demonstrates the failure and verifies
the intended behavior. Documentation-only edits need accurate commands and
working links, not additional behavior tests.

## Make a contribution

1. Open an issue describing a concrete problem, or discuss a larger change
   before implementing it. Include version, platform, expected/actual behavior
   and a minimal synthetic reproduction.
2. Work on a focused branch. Keep unrelated cleanup separate.
3. Explain the change and verification in your pull request. Mention
   limitations, skipped tests and compatibility impact.

Use Conventional Commit titles where practical, such as
`fix(csv): report ambiguous keys consistently`.

## Boundaries to preserve

- Distinguish the supported preview from experimental and planned features.
- Never include credentials, customer data, raw private traces or workspace
  outputs. Synthetic fixtures must be labeled.
- Do not turn offline replay into live model calls. Spending and data transfer
  require explicit authorization outside this preview.
- Keep policy, budgets and independent acceptance outside executor control.
  A higher grader score alone is not proof of a better result.
- Do not present resource limits or Python hooks as a complete sandbox.
- Add adapters through contracts rather than importing sibling product code.
- Keep tests reproducible without workstation paths.

## Licensing and conduct

Contributions to the public core are under Apache-2.0. Submit only work you
have the right to contribute, and preserve third-party attribution.
Communicate respectfully, critique ideas rather than people, and keep
discussions relevant to the project.

For sensitive vulnerabilities use [SECURITY.md](SECURITY.md), not a public
issue. For service enquiries use contact@dpintelli.com.
