# Review guidelines

Instructions for the automated reviewer in `.github/workflows/claude-review.yml`.
`CLAUDE.md` is the project reference; this file is only about what to flag.

## What blocks a merge

- **Correctness.** A concrete input or state that produces a wrong result, a
  crash, or a 500. State the input in the finding — a claim without one is a
  guess, and should be a nit or left out.
- **Security.** Missing authorization on a destructive endpoint, an admin key
  reaching a log/Sentry/the `ActivityLog` table, a timing-unsafe secret
  comparison, or user input reaching a query or template unescaped.
- **Migrations.** A model change with no migration, or a migration that drops a
  column that live code still reads. Railway runs `migrate` on every deploy, so a
  bad migration is a production outage, not a test failure.
- **Stale `output.css`.** If the PR changes a template or `tailwind.css` and does
  not commit a rebuilt `backend/static/css/output.css`, the change ships
  invisible. Railway cannot build the CSS.
- **A rule in `CLAUDE.md` that this diff breaks.** Those rules are load-bearing
  and each one is there because something went wrong once. Cite the section.
- **Tests.** New behaviour with no test in `backend/events/tests.py`, or a change
  that obviously invalidates an existing test.

## Project-specific traps worth checking every time

- **Availability grid.** Anything comparing a stint time to a stored
  `Availability` slot must use `slot_grid_anchor()`. `_driver_has_conflict()`
  floors independently and the two must agree.
- **Stint timing.** `get_stint_windows()` is the only helper that honours
  `actual_start_utc`. New timing code that recomputes starts by hand is a bug.
- **URL ordering.** Admin sub-routes must stay declared before the
  `<str:admin_key>` entry point in `events/urls.py`.
- **`_ADMIN_SUBROUTES`.** A new `/admin/<literal>/` route must be added to the
  allowlist in `config/logging.py` or that route becomes unreadable in logs.
- **HTMX validation errors** return 422 and use `partials/form_errors.html`.
  A form endpoint returning 200 with errors, or 400, is wrong.
- **HTMX event names** must be kebab-case. `HX-Trigger: {"showToast": ...}` can
  never fire an Alpine `@show-toast.window` listener.
- **`cycle_key()`** belongs only on the admin transition. Calling it on every
  request logs people out of their other tabs.
- **Sentry scrubbers** (`scrub_event`, `scrub_log`) must not catch their own
  exceptions. Failing closed is deliberate.
- **`ActivityLog` writes** must stay inside `try/except`. Analytics must never be
  why a page fails to render.

## What is a nit, not a blocker

Naming, comment wording, import order, a helper that could be shared, a test that
could be tidier. Say it once, mark it `**Nit**`, move on.

## What not to report at all

- Anything in `backend/static/css/output.css` — it is generated.
- Style the project has chosen deliberately: sharp corners, no `rounded-*` on
  interactive elements, exact-pinned requirements, the single large `tests.py`.
- Known and documented items listed under "Feature flags and future work" and
  "Security notes" in `CLAUDE.md`. Splitting `tests.py`, the residual admin-key
  logging risk, and the missing `CSRF_COOKIE_SECURE` are all already decided.
- Speculation about performance without a query count or a measurement.

## Re-reviews

On a second or later pass, report blocking findings only. Do not open a new nit
thread on a PR that has already been through a round.
