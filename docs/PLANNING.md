# Planning Phase Playbook

Questions you will be asked during boot camp planning, answers worth giving,
and the topics nobody warns you about.

> **Extending this document.** Parts are numbered and independent. Add a new
> numbered question under the relevant part, or append a new part at the end.
> Record the change in the log below so it stays obvious what moved.

| Version | Date | Change |
|---|---|---|
| 1.0 | 2026-08-15 | Initial. Parts 1–6, written after building and deploying the reference service through Phase 5. |

**Reference project:** build, test, containerize, and deploy a cyber inventory
service through an automated pipeline.

**Stated learning goals:**

1. Source control and peer review practices across teams
2. Diagnose failures and trace a change from commit → build → deploy → production
3. Build community across the cohort

Everything below is aimed at those three. When a planning conversation drifts
into domain modeling for three hours, the second goal is the one being
neglected — and it's the one that's graded.

---

# Part 1 — Questions you will be asked

## 1.1 Scope and requirements

**"What is this service actually for?"**

Answer in terms of who uses it and what breaks if it's wrong. For an inventory
service: *security and ops teams need an authoritative list of assets so
vulnerability findings can be attributed to an owner. If it's incomplete,
findings land on nobody's desk and go unfixed.*

Avoid answering with a feature list. Features are the second question.

**"What does done look like?"**

Push for something testable. "A user can register an asset and retrieve it by
hostname, and the service is deployed and reachable" is done. "The inventory
works well" is not.

The strongest version names what's explicitly *out* of scope, because a
two-week project fails by scope creep far more often than by difficulty.

**"What are the must-haves versus the nice-to-haves?"**

Have an opinion ready. For this kind of project the must-haves are: the
service runs, it's tested, it's containerized, it deploys automatically, and
you can trace what's running. Authentication, a real database, a UI, and
multi-region are all nice-to-haves that eat the whole clock.

**"How many users? How much data?"**

Usually nobody knows, and that's fine — but ask anyway, because the answer
changes whether you need a database at all. Ten thousand assets fits in memory.
Ten million doesn't.

## 1.2 Architecture

**"Why this stack?"**

Have a real reason, not "it's what I know." Reasonable answers: *Python because
the cohort knows it and the ecosystem for APIs is mature; FastAPI because
validation and OpenAPI docs come free; Cloud Run because it's the shortest
path from container to URL and scales to zero.*

**"Do we need a database?"**

The honest answer for a two-week project is usually no, and saying so is
worth points. A database adds migrations, connection pooling, test fixtures,
a Cloud SQL instance, and an IAM path — none of which teach you anything about
the pipeline, which is the actual subject.

Put storage behind a narrow interface so swapping it later is contained. That's
the answer that shows judgment: not "no database" but "a seam where the
database will go."

**"Monolith or microservices?"**

Monolith. For a ten-day project with a cohort learning the pipeline, splitting
services multiplies the deployment surface without teaching anything extra.
Anyone advocating microservices here should be asked what problem it solves.

**"REST, GraphQL, or gRPC?"**

REST, unless there's a stated reason otherwise. It's the least surprising, the
easiest to test with curl, and the easiest for a reviewer to reason about.

**"How do we handle configuration?"**

Environment variables, injected by the platform. Never config files baked into
the image, never secrets in the repo. Cloud Run sets `PORT`; read it rather
than hardcoding.

## 1.3 Data and domain

**"What are the core entities?"**

For an asset inventory: an asset with hostname, IP, type, owner, criticality,
and timestamps. Resist adding more. Every field is validation, tests, and
migration surface.

**"What's the uniqueness key?"**

An easy question that catches people. Hostname is the intuitive answer and
it's mostly right — but normalize case first, or `WEB-01` and `web-01` become
two assets and the inventory silently double-counts.

**"What happens on duplicate submission?"**

`409 Conflict`, not `400 Bad Request`. The request was well-formed; it clashes
with existing state. Clients retry those differently.

**"Where does the data come from?"**

Ask this early. Manual entry, an agent, a scanner import, an existing CMDB —
each implies a different ingestion path, and "we'll figure that out later" is
how you discover on day eight that you needed a bulk import endpoint.

## 1.4 Security

Expect this to be weighted heavily given the framing. Being the person with
crisp answers here is the cheapest way to stand out.

**"How do we authenticate users?"**

For an internal service on Cloud Run the options, roughly in order of effort:
platform-level (Cloud Run IAM, no app code), Identity-Aware Proxy, an API key
in a header, or full OIDC. Start with the platform doing it. Application-level
auth is code you have to get right; platform-level auth is configuration.

**"How does the pipeline authenticate to the cloud?"**

**Workload Identity Federation. No service account keys.** A downloaded key
never expires, works from anywhere, and ends up in shell history and backups.
Leaked service account keys are one of the most common causes of cloud
compromise. WIF gives GitHub a short-lived token instead — nothing to leak,
nothing to rotate.

If someone proposes pasting a JSON key into a GitHub secret, this is the
moment to speak up.

**"What are our supply chain risks?"**

Three concrete ones, with a real example each:

- **Dependencies** — vulnerable packages. Scan on every build; fail on
  HIGH/CRITICAL that have fixes available.
- **Base images** — you inherit everything in them. Use slim variants,
  multi-stage builds, and strip build tooling from the runtime image.
- **CI actions** — a third-party action runs on your runner with your
  secrets. In March 2026, attackers force-pushed 76 of 77 version tags of
  `aquasecurity/trivy-action` to malicious commits that harvested runner
  secrets. **Pin actions to commit SHAs**; a tag is a label and labels move.

**"What are we doing about secrets?"**

Nothing in the repo, nothing in the image (files deleted in a later layer stay
recoverable in image history — a leaked `.env` in an image is permanent), and
a secret scanner in CI. Use the platform's secret manager for anything real.

**"What's our blast radius if the service is compromised?"**

The question behind the question is *what identity does the service run as?*
Cloud Run defaults to the Compute Engine service account, which holds project
Editor — a compromised request would have broad write access to the entire
project. A dedicated runtime service account with no roles is one line and a
dramatic reduction.

**"Do we need to worry about prompt injection?"**

Only if there's an LLM in the request path. If there is: an attacker who
controls any text the model reads can attempt to redirect it. There is no
clean fix. You mitigate with least privilege, human confirmation on
consequential actions, and sandboxed tools. Say this plainly rather than
implying it's solved.

## 1.5 Operations

**"How do we know it's healthy?"**

Two endpoints, and they're different. Liveness answers *is the process alive*
and must check nothing external — a liveness probe that checks a database
restarts every healthy container during a database blip, escalating a partial
outage to a total one. Readiness answers *should this instance get traffic*
and is where dependency checks go.

On Cloud Run, do **not** name them `/healthz` and `/readyz` — Google's frontend
reserves paths ending in "z" and 404s them before they reach your container.

**"How do we know what's deployed?"**

The single most valuable thing you can build, and it belongs in the first
commit rather than the last. Stamp the commit SHA into the image at build
time, expose it at `/version`, and have the pipeline assert that production
reports the commit it just deployed. Then "did my fix go out?" is one curl
instead of an argument.

**"How do we roll back?"**

Tag images with the commit SHA, never rely on `:latest`. Rollback becomes
deploying a specific earlier tag. `:latest` means "whatever was pushed most
recently," which is useless during an incident.

**"What about logging?"**

Structured JSON to stdout. Cloud Logging parses it and treats `severity`
specially. Plain text costs you level filtering and field search precisely
when you need them. Include a request ID on every line, and reuse the
platform's trace header when present so your logs correlate with the
platform's.

**"What's our alerting story?"**

For a two-week project, honestly: uptime checks on the health endpoint and
error-rate alerts. Anything more is scope you won't finish.

## 1.6 Team process

**"How do we branch?"**

Short-lived feature branches off `main`, PR to merge, `main` always
deployable. Trunk-based with branch protection. Anything more elaborate —
gitflow, release branches — costs more than it returns in ten days.

**"What's our review policy?"**

At least one approving review, CI must pass, no direct pushes to `main`.
Enforce it with branch protection rather than good intentions.

**"Squash or merge commits?"**

A real decision with no universal right answer. Squash keeps `main` tidy;
merge commits preserve the reasoning in individual commit messages. Pick one,
write it down, apply it consistently.

**"How do we split the work?"**

By pipeline stage rather than by file, so people aren't editing the same
things. One person on service and tests, one on container and CI, one on CD
and cloud setup, rotating so everybody touches every stage. That serves the
"build community" goal better than assigning by expertise, which just
reinforces who already knows what.

---

# Part 2 — Questions you should ask

These make you visibly useful in the first hour. Most of them have long lead
times, which is exactly why asking early matters.

**"Who owns the firewall and has the egress request been filed?"**
If the network can't reach the cloud API or a container registry, nothing
works and the fix takes days.

**"Do we have a GCP project with billing enabled, and who's the owner?"**
You cannot enable a single API without it.

**"What data classification are we allowed to use?"**
Determines whether you can use real data or need synthetic.

**"Is there an existing CI/CD standard I should follow?"**
Reinventing one that conflicts with the org's is wasted work.

**"Who reviews our PRs — inside the cohort or outside?"**
Changes turnaround time and therefore how you sequence work.

**"What does the final demo have to show?"**
Work backwards from it. If the demo needs a UI, you needed to know on day one.

**"Are we deploying to a shared environment or isolated ones?"**
Shared means collisions and coordination overhead nobody plans for.

**"What's the definition of done for a task — merged, or deployed?"**
These are very different commitments.

---

# Part 3 — Topics that will come up that nobody warns you about

**Estimation.** You'll be asked how long something takes. Estimate in
"complexity relative to something we've done" rather than hours, and always
name the assumption: *"about a day, assuming the cloud project already
exists."*

**The demo is a deliverable.** Teams routinely build something good and
present it badly. Budget real time for it, and rehearse the failure case —
what you say when the live demo breaks matters more than whether it breaks.

**Somebody will suggest a scope increase in week two.** Have language ready:
*"That's a good idea. What comes out to make room?"* Not a no, but a trade.

**The environment will fight you and that's the actual work.** Expect to lose
meaningful time to firewall rules, IAM propagation, credential scopes, and
platform quirks. That's not a distraction from the project; in an enterprise,
that *is* the project.

**Pair debugging beats solo debugging.** When you're stuck for more than
twenty minutes, pull someone in. It serves the "build community" goal and
usually resolves in five minutes because explaining it out loud does most of
the work.

**Documentation counts.** A README that lets someone run the thing in five
minutes is worth more than another feature nobody demos.

---

# Part 4 — Decisions to make in the first 48 hours

Have a recommended default ready for each. Deciding fast beats deciding
perfectly.

| Decision | Recommended default | Why |
|---|---|---|
| Language / framework | Python + FastAPI | Cohort knows it; validation and docs are free |
| Storage | In-memory behind an interface | Database adds cost with no pipeline learning |
| Deploy target | Cloud Run | Shortest container → URL path; scales to zero |
| CI/CD | GitHub Actions | Same place as the code; easy to demo |
| Cloud auth | Workload Identity Federation | No key to leak |
| Branching | Short-lived branches + PR + protection | Simple, enforceable |
| Image tags | Commit SHA | Deterministic rollback |
| Config | Environment variables | Platform-native, no secrets in image |
| Health endpoints | `/health` and `/ready` | Cloud Run reserves paths ending in "z" |
| Provenance | `/version`, in the first commit | Retrofitting means touching every stage |

---

# Part 5 — Red flags to raise early

Raising these takes thirty seconds and can save the project days.

- **"We'll add tests at the end."** They won't. And CI has nothing to gate on
  until then.
- **"Just paste the service account key into a GitHub secret."** Creates a
  permanent credential. Use WIF.
- **"We'll figure out deployment in week two."** Deployment reveals problems
  that change your design. Deploy something trivial on day two.
- **"Let's use `:latest`."** Undeployable rollback story.
- **"We don't need branch protection, we're a small team."** The protection is
  what makes review real rather than optional.
- **Nobody can say what the demo shows.** Means the scope isn't agreed, only
  assumed.
- **The whole first week is planning.** For a ten-day build, planning past day
  two is avoidance.

---

# Part 6 — One-page pre-flight checklist

Print this. Walk it in the first planning session.

```
SCOPE
[ ] One sentence: who uses this and what breaks if it's wrong
[ ] Testable definition of done
[ ] Explicit out-of-scope list
[ ] What the final demo must show

ACCESS  (long lead times -- ask on day one)
[ ] GCP project + billing owner identified
[ ] Network egress to cloud APIs and container registries confirmed
[ ] Data classification confirmed
[ ] GitHub org / repo permissions confirmed

ARCHITECTURE
[ ] Stack chosen, with a reason
[ ] Storage decision (and the seam if it changes)
[ ] Config via environment variables
[ ] Health + readiness endpoint names (no trailing "z" on Cloud Run)

TRACEABILITY  -- decide before the first commit
[ ] Commit SHA stamped into the image at build time
[ ] /version endpoint
[ ] Pipeline asserts production reports the deployed commit

SECURITY
[ ] Workload Identity Federation, no downloadable keys
[ ] Dedicated runtime service account with no roles
[ ] Dependency + image + secret scanning in CI
[ ] CI actions pinned to commit SHAs
[ ] Dependabot enabled to offset pinning

PROCESS
[ ] Branching model agreed
[ ] Review policy agreed and enforced by branch protection
[ ] Squash vs merge decided and written down
[ ] Work split by pipeline stage, rotating
```

---

# Part 7 — *(reserved)*

*Add material from Phase 6 (source control and review practices) and Phase 7
(failure diagnosis drills) here, or as new numbered parts above.*
