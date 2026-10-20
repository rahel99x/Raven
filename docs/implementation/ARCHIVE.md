# Historical implementation evidence

The 52 implementation reports in this directory are retained historical records.
Their original scheduler traces, command captures, generated output, profiling
artifacts and benchmark matrices are preserved at the Git reference
[`evidence/pre-review-2026-10-20`](https://github.com/rahel99x/Raven/tree/evidence/pre-review-2026-10-20/docs/implementation),
which points to reviewed commit `b98742fb967f18131905696f4afd3a61f2ad03f2`.

The archive contains 774 raw files totalling 19,534,267 bytes. Before removing
them from the active tree, every file was verified to match that commit's Git
blob. Markdown links to archived files now point to that reference. Paths inside
historical commands and prose describe the original run; they are not current
installation instructions or evidence that a later revision passed those checks.

To inspect the original evidence without changing an active checkout:

```bash
git fetch origin
git show origin/evidence/pre-review-2026-10-20:docs/implementation/review-2026-10/evidence/W34.md
raven_archive_dir="$(mktemp -d)"
git archive origin/evidence/pre-review-2026-10-20 docs/implementation | tar -x -C "$raven_archive_dir"
```

Only the four model/result JSON files used by deterministic negative-efficacy
regressions remain active data. They now live under
[`tests/fixtures/review-temporal-holdout`](../../tests/fixtures/review-temporal-holdout),
with their original bytes and assertions preserved. New release evidence belongs
in CI artifacts or a dedicated evidence reference; raw run output should not be
added to the main source tree or source distributions. Current review outcomes
and bounded summaries live under [`docs/reviews`](../reviews).
