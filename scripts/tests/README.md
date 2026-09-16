# Functional-service test harness

Automation for the matrix in [`docs/FUNCTIONAL_SERVICE_AUDIT.md`](../../docs/FUNCTIONAL_SERVICE_AUDIT.md).
Finding IDs (`F-04`, `F-05`, ...) and matrix IDs (`CRUD-01`, `LST-05`, ...) in
test names and xfail reasons refer to that document.

| Layer | What it proves | Command |
|---|---|---|
| `tests/contract/` (pytest) | the API contract, in-process, no network | `make test` |
| `scripts/tests/local-integration.sh` | the real server: sockets, JSON on the wire, stdout logs | `make test-integration` |
| `scripts/tests/local-integration.sh --docker` | the shipped container image, including `$PORT` and SIGTERM | `make test-integration-docker` |
| `scripts/tests/destructive/persistence-local.sh` | what data is lost across a restart (destructive, local) | `make test-persistence` |
| `scripts/tests/cloudrun-functional.sh` | the deployed private service (no deploy, traffic or IAM changes) | `make test-cloudrun` |

`make test` is unchanged and is still exactly what CI runs.

## Known defects are strict xfails

A confirmed defect is encoded as the **approved** contract and marked
`@known_defect("F-xx", "why it fails today")`, which is a strict xfail. The
suite stays green, the defect appears in every run summary, and if the defect
is fixed the test XPASSes and the run **fails** — forcing whoever fixed it to
remove the marker deliberately. The shell harness mirrors this with
`Reporter.known_defect`, which reports `XPASS` and exits non-zero if a known
defect stops reproducing.

If a known defect does not reproduce, report it. Do not edit the check to keep
the expected failure.

## Safety properties

- Every record carries a unique run id in its hostname **and** its tags.
  Cleanup deletes only records matching both, so a run can never remove data
  it did not create.
- The Cloud Run script routes every `gcloud` call through an allowlist of
  read-only verbs; anything else aborts the run.
- Bearer tokens are passed by environment variable name, never on a command
  line.
- The persistence script starts its own instance on a free port and has no
  `--base-url` option, so it cannot restart anything else.

## Cloud Run usage

```bash
# see exactly what would run, without contacting anything
scripts/tests/cloudrun-functional.sh --plan

# real run (needs roles/run.invoker on the service)
scripts/tests/cloudrun-functional.sh --project <project> --region us-central1
```

It writes a handful of uniquely named test assets to the live inventory and
deletes them again, verifying each deletion. If more than one instance is
serving, reads may miss records written to another instance — the checks flag
that as `PER-05/F-02` rather than pretending it is a normal failure.
