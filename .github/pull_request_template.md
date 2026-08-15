<!--
This template is pre-filled into every new pull request.

Its real job is not paperwork. It is to make the author write down the two
things reviewers cannot infer from a diff: WHY this change exists, and HOW
they can tell it works. A diff shows what changed; it never shows what you
were trying to do or what you already ruled out.

Delete any section that genuinely does not apply. An honest "n/a" is better
than a box ticked out of habit.
-->

## What and why

<!-- One or two sentences. What does this change, and what problem does it
solve? Link the issue or task if there is one. -->

## How to verify

<!-- Exact steps a reviewer can run. "Tests pass" is not enough -- CI already
says that. Give the curl, the command, the URL, the thing to look at. -->

```bash
# example
make test
curl localhost:8080/version
```

## Risk

<!-- What could this break? What did you consider and reject? If it touches
the pipeline, the Dockerfile, or dependencies, say so explicitly. -->

- [ ] Touches CI/CD workflows or the Dockerfile
- [ ] Adds or upgrades a dependency
- [ ] Changes an API contract (new/renamed/removed endpoint or field)
- [ ] Changes IAM, permissions, or anything security-relevant
- [ ] None of the above — application logic only

## Rollback

<!-- If this is deployed and goes wrong, what happens? Usually "redeploy the
previous image SHA", but say so if it isn't -- a database migration or a
config change may not roll back cleanly. -->

## Checklist

- [ ] Tests added or updated for the behaviour that changed
- [ ] `make lint` and `make test` pass locally
- [ ] Commit messages follow the convention (`feat:`, `fix:`, `docs:`, `chore:`, `ci:`, `test:`, `build:`)
- [ ] No secrets, keys, or credentials added — including in test fixtures
- [ ] Docs updated if behaviour changed (README, PHASES.md, docs/)

---

<!--
REVIEWER NOTES

Things worth checking that CI cannot:

  * Does the change do what the description says it does?
  * Are the tests testing behaviour, or just raising coverage?
  * If a lint rule or a scanner finding was suppressed, is there a written
    reason? A rule disabled without explanation is a rule someone re-enables.
  * If an action version changed, is it pinned to a commit SHA?
  * Does anything here widen permissions or blast radius?
  * Would you be able to debug this at 2am from the logs it produces?
-->
