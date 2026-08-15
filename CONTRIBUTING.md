# Contributing

The working agreement for this repository. In a cohort, this is the document
you write together in the first hour and then actually follow — most of the
friction in a two-week team project comes from these questions being answered
differently by different people rather than from anything technical.

---

## Branching

Short-lived branches off `main`. `main` is always deployable and is never
committed to directly.

```
main ────●────────●────────●──────▶  always deployable, always deployed
          \      /        /
           ●────●        ●            feat/asset-tags, fix/duplicate-409
```

**Branch naming** — `type/short-description`, matching the commit types:

```
feat/bulk-import          fix/hostname-normalization
ci/pin-action-shas        docs/planning-playbook
chore/bump-deps           test/store-concurrency
```

**Keep branches short-lived — a day or two.** A branch alive for a week
accumulates conflicts, and the review is so large that nobody reviews it
properly. Big PRs get rubber-stamped; that is the observed behaviour, not a
moral failing. If a change is genuinely large, split it into a sequence of
small ones behind a flag.

---

## Commits

**Conventional commits.** The prefix is not decoration — it makes history
skimmable, supports changelog generation, and forces you to notice when a
commit is doing two unrelated things.

```
feat:     new capability visible to a user
fix:      corrects broken behaviour
docs:     documentation only
test:     tests only
ci:       pipeline and workflow changes
build:    build system, Dockerfile, dependencies
chore:    maintenance with no behaviour change
refactor: restructuring with no behaviour change
```

**Write the body for the person debugging this in six months.** The diff
already shows *what* changed. The message is the only place *why* survives.

Good:

```
fix: strip build tooling from system site-packages too

The previous fix cleaned the virtualenv but not the base image's own
site-packages, which carries a second copy of pip, setuptools, and wheel.
The same two findings survived: wheel privilege escalation and
jaraco.context path traversal.

Resolves the path at build time rather than hardcoding it, so a future
base image bump cannot silently turn this into a no-op.
```

Not good:

```
fixed stuff
```

---

## Pull requests

1. Push your branch and open a PR against `main`
2. Fill in the template — especially **how to verify** and **risk**
3. CI must be green; it runs automatically
4. At least one approving review
5. Merge, delete the branch

### Reviewing

**Review the change, not the person.** Say "this drops the error case" rather
than "you forgot the error case." It reads the same to you and completely
differently to them.

**Distinguish blocking from non-blocking.** Prefix accordingly:

- **blocking:** this is wrong or unsafe, please change it
- **suggestion:** I'd do it differently, your call
- **question:** I don't understand this, help me
- **nit:** trivial, ignore if you like

Without those labels, every comment reads as a demand and reviews get
adversarial. With them, a review with eight nits and no blockers is obviously
an approval.

**Approve with comments rather than blocking on trivia.** If the only issues
are nits, approve and let the author decide. Holding a PR hostage over naming
is how a cohort stops enjoying reviews, and the "build community" goal is
graded too.

**Review in a day.** A PR waiting three days blocks the author and grows
conflicts. If you can't get to it, say so, so someone else picks it up.

### What to look for that CI cannot

CI checks that the code runs, is formatted, is tested, and has no known
vulnerable dependencies. It cannot check:

- Does the change do what the description claims?
- Are the tests testing behaviour, or just raising the coverage number?
- Is a suppressed lint rule or scanner finding **explained**? A rule disabled
  without a written reason is a rule the next person re-enables.
- Are new actions pinned to commit SHAs?
- Does this widen permissions, blast radius, or the attack surface?
- Could you debug this from the logs it produces, at 2am, without the author?

---

## Merge strategy

**This repository uses merge commits, deliberately.** The commit messages here
carry the reasoning behind decisions, and squashing collapses them into one.

Squash is the more common team default and keeps `main` tidy — it's a
legitimate choice. What matters is that the team picks one, writes it down,
and applies it consistently. Mixed strategies produce a history nobody can
read.

---

## Branch protection

`main` is protected. You cannot push to it directly, and a PR cannot merge
until CI passes and someone approves. See `scripts/setup-branch-protection.sh`
for the exact configuration.

This is deliberate. Every rule in it exists because "we all agreed not to do
that" reliably fails under deadline pressure, and week two of a two-week
project is nothing but deadline pressure. Automate the agreement.

---

## Local development

```bash
make install     # venv + dev dependencies
make test        # pytest with coverage
make lint        # ruff
make run         # local server with provenance injected
make build       # container image, stamped with the current commit
make smoke       # verify a running instance is alive AND traceable
```

Run `make lint && make test` before pushing. CI will catch it either way, but
finding out in twenty seconds locally beats finding out in two minutes on a
runner.

### Windows

Use **Git Bash or WSL2**, not PowerShell, so these commands work verbatim.
`.gitattributes` enforces LF line endings — without it, shell scripts and
workflow `run:` blocks fail inside Linux containers with `bash\r: command not
found`. See `docs/BUILD-MANUAL.md` §2.6.

---

## Definition of done

A task is done when it is **merged and deployed**, not when a PR is opened.

- Code merged to `main`
- CI green
- Deploy workflow succeeded
- `/version` in production reports the commit
- Docs updated if behaviour changed

That fourth item is the one people skip. It's also the only one that proves
the change actually reached users.
