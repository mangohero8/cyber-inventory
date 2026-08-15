# Plain-English Glossary

Terms as they come up, defined the way a teammate would explain them at a
whiteboard. Growing as we build.

## The pipeline

**CI (Continuous Integration)** — A robot that checks your code every time you
push it. Runs the tests, runs the linter, tells you if you broke something.
Catches problems in minutes instead of at demo time.

**CD (Continuous Deployment/Delivery)** — The same robot, continuing on: if the
checks pass, it packages your code and puts it on a live server. Delivery means
it stops just short and waits for a human to approve; Deployment means it goes
all the way automatically.

**Pipeline** — The whole assembly line: push → test → build → package → deploy.

**GitHub Actions** — GitHub's robot. You describe the assembly line in a YAML
file inside your repo, and GitHub runs it on every push.

**Artifact** — Any file the pipeline produces and keeps. Usually the container
image, but also test reports and scan results.

## Containers

**Container** — Your program sealed in a box with everything it needs to run:
the right Python version, the right libraries, the right files. The box behaves
identically on your laptop and on the server. This is the cure for "works on my
machine."

**Image** — The box before you start it. A saved template.

**Container** (running) — An image that's actually running. One image, many
containers, same as one class and many objects.

**Dockerfile** — The recipe for building the image, one instruction per line.

**Registry** — The warehouse where images are stored. Google's is called
Artifact Registry.

**Cloud Run** — Google's service that runs your container for you. You hand it
an image, it gives you back a URL. Scales down to zero when nobody's using it,
so idle costs are near nothing.

## Traceability

**Commit SHA** — The unique 40-character ID Git gives every commit. The
fingerprint of one exact version of your code. Usually shortened to the first
7 characters.

**Provenance** — The paper trail: which commit produced this running program,
when it was built, and by what. Answers "what's actually deployed right now?"

**Build arg** — A value you pass into `docker build` that the recipe can use.
We use it to stamp the commit SHA into the image at build time.

**Environment variable** — A setting handed to a program when it starts, from
outside the program. How the container tells the app which commit it is.

## Reliability

**Liveness probe** (`/healthz`) — "Are you breathing?" If this fails, the
platform restarts your container. Must not check anything external.

**Readiness probe** (`/readyz`) — "Are you ready for customers?" If this fails,
the platform stops sending you traffic but doesn't kill you. This is where
database checks belong.

**Structured logging** — Writing log lines as JSON instead of plain sentences,
so you can search and filter them like a spreadsheet instead of reading a
novel.

**Smoke test** — A quick check after deploying that the thing you just shipped
is actually alive and is the version you expected.

## Testing and code quality

**Unit test** — Tests one small piece in isolation.

**Integration test** — Tests several pieces working together. Our API tests are
these: real request in, real response out.

**Fixture** — Reusable setup for tests. Our `client` and `sample_asset` are
fixtures.

**conftest.py** — A special pytest file. Anything defined in it is available to
every test in that folder automatically, no import needed.

**Coverage** — What percentage of your code the tests actually execute. Useful
as a floor to catch entirely untested code. Easy to game, so never a goal in
itself — you can hit 100% with tests that assert nothing.

**Linter** — A tool that reads your code and flags likely mistakes and style
inconsistencies without running it. Ours is `ruff`.

**Flaky test** — A test that passes sometimes and fails sometimes without the
code changing. Usually caused by leftover state between tests or by depending
on ordering. Corrosive, because people start ignoring red builds.

## Source control

**Branch** — Your own copy of the code to work on without disturbing anyone.

**Pull request (PR)** — "Please review my branch and merge it." Where peer
review happens.

**Branch protection** — Rules on the shared branch: no direct pushes, PR
required, tests must pass, someone must approve.

**CODEOWNERS** — A file that says who must review changes to which folders.
GitHub adds them as reviewers automatically.

**Conventional commits** — A commit-message convention: `feat:`, `fix:`,
`docs:`, `chore:`. Makes history skimmable and can auto-generate changelogs.

## API details

**HTTP 201 Created** — Made a new thing.
**HTTP 204 No Content** — Worked, and there's deliberately nothing to send back.
**HTTP 404 Not Found** — No such thing.
**HTTP 409 Conflict** — Your request was fine, but it clashes with what already
exists. Used for duplicate hostnames.
**HTTP 422 Unprocessable Entity** — Your request was malformed. Used for a bad
IP address.

**Idempotent** — Doing it twice has the same effect as doing it once. Important
because pipelines retry.
