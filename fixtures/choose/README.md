# Choose journey fixtures — SYNTHETIC

Everything under `fixtures/choose/` is **explicitly synthetic**. Every JSON
file carries `"synthetic": true`; the adapter refuses to load any file
without that marker.

These fixtures prove the **Vouch pipeline** — the versioned adapter protocol
v1 (describe/prepare/execute/collect/cleanup), frame validation, metering
enforcement, evidence sealing, revision/handoff/session journey shapes,
en/zh and market/currency variants, and honest failure paths.

They **do not** prove anything about model quality, answer quality, or real
product improvement. No model, no network, no Node, and no choose-website
source is involved. Real Choose journeys can only be measured by a
Choose-owned runner (see `docs/choose-integration-gaps.md`).

- `pack.json` — pack manifest: workflow coverage (W-C1..W-C9) and the four
  regression-only guardrail families.
- `scenarios/*.json` — declarative scenario files. Product names ("Synthetic
  Model A"…), prices, and sources (example.com) are invented; claims quote
  literal spans of the synthetic excerpts, mirroring the citation-span shape
  Choose's own evidence contracts use.

Scenario naming follows the stem convention of Choose's own journey-fixture
documentation (`tw-<task>-<class>-<locale>-<market>`) so fixture-covered
workflows map onto the same review matrix without claiming to be it.
