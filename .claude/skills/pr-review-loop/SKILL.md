---
name: pr-review-loop
description: Drive a pull request through automated review to merge. Use when a PR has been opened (or is about to be) and the Claude Code Review workflow needs to be waited on, its findings addressed, and the PR merged once green. Trigger on "open a PR and take it through review", "check the review comments", "address the PR feedback", "is the PR ready to merge".
---

# PR review loop

The writing side of the loop. `.github/workflows/claude-review.yml` runs the
reviewing side in GitHub Actions with a cold context — it has not seen this
conversation and does not know what you meant. Treat its findings as coming from
a stranger who has only the diff.

## 1. Open the PR

Rebuild CSS first if any template or `tailwind.css` changed, or the reviewer will
block on a stale `output.css`:

```
.\bin\tailwindcss.exe -i backend\static\css\tailwind.css -o backend\static\css\output.css --minify
```

Run the suite before pushing — a review of code that does not pass its own tests
wastes a round trip:

```
cd backend; ..\venv\Scripts\python.exe manage.py test --settings=config.test_settings
```

Then push the branch and open the PR against `master`. The PR body should say
what changed and why; the reviewer reads it.

## 2. Wait for the review

The workflow triggers on `opened`, `synchronize`, `reopened` and
`ready_for_review`. Drafts are skipped — mark the PR ready or it will never be
reviewed.

```
gh run list --workflow=claude-review.yml --limit 1
gh run watch <run-id>
```

If the run fails, read the log before assuming the code is fine:
`gh run view <run-id> --log-failed`.

## 3. Read the findings

The verdict and the inline comments come from different endpoints. Read both.

```
gh pr view <n> --json reviewDecision,statusCheckRollup
gh api repos/listenlouder/endurance-planner/pulls/<n>/reviews --jq '.[] | {user: .user.login, state, body}'
gh api repos/listenlouder/endurance-planner/pulls/<n>/comments --jq '.[] | {id, path, line, body}'
```

`reviewDecision` is `CHANGES_REQUESTED`, `APPROVED`, or empty. A pushed commit
does **not** clear `CHANGES_REQUESTED` on its own — only a later review from the
same reviewer replaces it, which the next run produces automatically.

## 4. Address each finding

For every comment marked **Blocking**, do one of two things and never a third:

- Fix it, and add a test that fails without the fix where the finding was a
  correctness bug.
- Disagree, and reply on the thread saying why, with the file and line that makes
  the case:

```
gh api repos/listenlouder/endurance-planner/pulls/comments/<comment-id>/replies -f body="..."
```

Do not silently ignore a finding. Do not fix a finding you think is wrong just to
clear the review — say so instead, and let the user decide.

**Nit** comments are optional. Batch the ones worth doing into the same push.

Report to the user what you accepted and what you pushed back on, before pushing.

## 5. Push and re-review

Push. `synchronize` fires a fresh review, which reports blocking findings only on
a second pass. Go back to step 3.

If the loop reaches a third round on the same finding, stop and bring it to the
user — that is a disagreement, not a defect.

## 6. Merge

Merge only when `reviewDecision` is `APPROVED` and every check has passed.

Confirm with the user before merging unless they have already said in this
session to merge when it goes green. Merging to `master` deploys to Railway
immediately — it is a production release, not a bookkeeping step.

```
gh pr merge <n> --squash --delete-branch
```

Then confirm the Railway deploy succeeded before calling the work done.
