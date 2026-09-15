# e2e

Browser-driven specs against the demo project, using Playwright via `pytest-playwright` (the `e2e`
extra). Meaningful from Phase 7 onward — earlier phases have no user-facing surface and
`make e2e-up` is a no-op that exits 0.

## Running

```sh
make e2e-up                          # migrates, seeds (admin/admin), starts the server on :8000
uv run --extra e2e pytest e2e -q     # the suite
make e2e-down                        # kills the server, frees port 8000
```

The suite **attaches to** the demo server; it never starts or stops one itself. `server_available`
(session-scoped, in `conftest.py`) skips every browser case with a clear message if
`http://127.0.0.1:8000/admin/login/` does not answer — run `make e2e-up` first. `test_harness.py`'s
two urllib-only cases have no such fixture and run without Playwright installed at all; they exist
to keep the harness itself honest even when the `e2e` extra is missing.

Override the target with `ADMIN_ERRORS_E2E_BASE_URL` if the demo runs somewhere other than
`127.0.0.1:8000`. There is exactly one runnable surface in this project: the demo Django project
(`demo/`), so there is no `surfaces.toml` — `E2E_BASE_URL` in `conftest.py` is that surface's one
address.

## The tree

```
e2e/
  plans/<feature>.plan.yaml   the cases and the oracle for each feature, written before the spec
  conftest.py                 base_url / server_available fixtures, the RESULTS.md reporter
  test_harness.py             sanity checks on the harness itself (no [qa:] tag, not a feature)
  test_*.py                   one file per feature, grouped by what they drive
  RESULTS.md                  generated after every run — gitignored
  artifacts/                  screenshots captured on failure — gitignored
```

## Adding a case

1. Add or extend `e2e/plans/<feature>.plan.yaml` first: the oracle (quoting the spec or acceptance
   criterion it comes from), the case list with priorities, and anything deliberately left out
   under `deferred_not_authored` with a reason. See `.autodev/guides/qa-oracles.md`.
2. Write the spec in `e2e/test_<feature>.py`. Every test function's docstring opens with the tag
   `[qa:<feature>:<case-id>]` followed by a one-line description — the reporter reads this to
   populate `RESULTS.md`, and it is how a reader matches a red row back to its plan.
3. Assert what a real user would see — rendered text, a visible control, a response status — never
   internal state a browser cannot reach. Prefer semantic locators (`get_by_text`,
   `input[name=...]`) over structural CSS chains.
4. Run `uv run --extra e2e pytest e2e -q` and check `e2e/RESULTS.md`.

## When a case fails

Treat it as a product bug until proven otherwise (`.autodev/guides/systematic-debugging.md`). Fix
the product, keep the assertion. If the assertion itself was wrong — it pinned behaviour the spec
never promised — fix the plan and the spec together and record why in `.autodev/DECISIONS.md`.
