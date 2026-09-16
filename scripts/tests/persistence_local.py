#!/usr/bin/env python3
"""DESTRUCTIVE (local only): what happens to inventory data across a restart.

  persistence_local.py --mode process           --confirm-restart
  persistence_local.py --mode container-replace --confirm-restart
  persistence_local.py --mode container-restart --confirm-restart

The script starts its OWN throwaway instance on a free port and restarts only
that. There is deliberately no --base-url option, so it cannot interrupt a
service someone else is using.

Sequence: create a uniquely named asset, record its id, prove it exists,
replace or restart the instance, ask for the same id again, re-register the
same hostname, then record exactly what was lost.

EXPECTED TODAY (finding F-03): the record is gone and the hostname is free
again, because AssetStore keeps everything in process memory. That is
ARCHITECTURAL behaviour of the chosen design, not a code defect -- but the
requirement "inventory survives a restart" is still reported as FAIL, because
it is not met. Do not implement persistence to make this pass.

Exit status:
  0  every requirement met
  1  a requirement failed (expected today)
  2  the harness could not run: a precondition failed
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from functional_checks import API, STATS, Reporter, Service, body_of, new_run_id, seen

ROOT = Path(__file__).resolve().parents[2]
HEALTH_TIMEOUT = 60

MODES = {
    "process": (
        "PER-01",
        "the application process is stopped and started again",
    ),
    "container-replace": (
        "PER-03",
        "the container is stopped, removed and replaced from the same image",
    ),
    "container-restart": (
        "PER-04",
        "the same container is stopped and started again in place",
    ),
}

ARCHITECTURAL = (
    "F-03 ARCHITECTURAL: AssetStore holds records in process memory "
    "(app/store.py), so a new process starts empty. Expected with the current "
    "design; not a code defect. The requirement is still unmet."
)


class HarnessError(RuntimeError):
    """A precondition failed, so the experiment could not be run at all."""


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def run(command: list[str]) -> str:
    """Run a fixed argument list (no shell, no user-supplied strings)."""
    result = subprocess.run(  # noqa: S603 - fixed argv, shell=False, no user input
        command, check=False, text=True, capture_output=True
    )
    if result.returncode != 0:
        raise HarnessError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


class Instance:
    """One throwaway service instance, plus the generations it goes through."""

    def __init__(self, mode: str, commit: str, workdir: Path, run_id: str) -> None:
        self.mode = mode
        self.commit = commit
        self.workdir = workdir
        self.run_id = run_id
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.generation = 0
        self.identities: list[str] = []
        self.exit_codes: list[Any] = []
        self.logs: list[Path] = []
        self.process: subprocess.Popen[bytes] | None = None
        self._log_handle = None
        self.container = f"cyber-inventory-persist-{run_id}"
        self.image = f"cyber-inventory:persist-{run_id}"
        self.docker = shutil.which("docker")

    # -- process mode ----------------------------------------------------
    def _start_process(self) -> None:
        uvicorn = ROOT / ".venv" / "bin" / "uvicorn"
        if not uvicorn.exists():
            raise HarnessError(f"{uvicorn} not found -- run 'make install' first")
        log = self.workdir / f"generation-{self.generation}.log"
        self._log_handle = log.open("w")
        env = {
            **os.environ,
            "GIT_COMMIT": self.commit,
            "BUILD_TIME": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "ENVIRONMENT": "persistence-test",
        }
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, shell=False
            [
                str(uvicorn),
                "app.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
            ],
            cwd=str(ROOT),
            env=env,
            stdout=self._log_handle,
            stderr=subprocess.STDOUT,
        )
        self.logs.append(log)
        self.identities.append(f"pid {self.process.pid}")

    def _stop_process(self) -> None:
        if self.process is None:
            return
        self.process.send_signal(signal.SIGTERM)
        try:
            code = self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait()
        # Popen reports a signalled process as the NEGATIVE signal number,
        # while a shell reports 128+signum for the same thing. Normalize to
        # the shell convention so process and container modes are comparable:
        # -15 becomes 143 (SIGTERM), -9 becomes 137 (SIGKILL).
        self.exit_codes.append(128 + abs(code) if code < 0 else code)
        if self._log_handle is not None:
            self._log_handle.close()
            self._log_handle = None
        self.process = None

    # -- container modes -------------------------------------------------
    def build_image(self) -> None:
        if not self.docker:
            raise HarnessError("docker is not installed")
        run(
            [
                self.docker,
                "build",
                "--build-arg",
                f"GIT_COMMIT={self.commit}",
                "--build-arg",
                f"BUILD_TIME={datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
                "-t",
                self.image,
                str(ROOT),
            ]
        )

    def _container_name(self) -> str:
        # container-restart keeps ONE container and starts it again, so its
        # name must not change with the generation; container-replace makes a
        # new container each time and must not reuse a name.
        if self.mode == "container-restart":
            return self.container
        return f"{self.container}-g{self.generation}"

    def _start_container(self) -> None:
        name = self._container_name()
        run(
            [
                self.docker,
                "run",
                "-d",
                "--name",
                name,
                "-e",
                "ENVIRONMENT=persistence-test",
                "-p",
                f"127.0.0.1:{self.port}:8080",
                self.image,
            ]
        )
        container_id = run([self.docker, "inspect", "-f", "{{.Id}}", name])[:12]
        self.identities.append(f"container {name} ({container_id})")

    def capture_logs(self) -> Path:
        """Write this generation's stdout to a file and return its path.

        Process mode streams straight to the file. Container modes have to ask
        docker, and must do so BEFORE the container is removed. `docker logs`
        on a restarted container carries every generation it has run.
        """
        log = self.workdir / f"generation-{self.generation}.log"
        if self.mode != "process":
            result = subprocess.run(  # noqa: S603 - fixed argv, shell=False
                [self.docker, "logs", self._container_name()],
                check=False,
                text=True,
                capture_output=True,
            )
            log.write_text(result.stdout + result.stderr)
        if log not in self.logs:
            self.logs.append(log)
        return log

    def _stop_container(self, *, remove: bool) -> None:
        name = self._container_name()
        run([self.docker, "stop", "-t", "10", name])
        self.exit_codes.append(
            run([self.docker, "inspect", "-f", "{{.State.ExitCode}}", name])
        )
        self.capture_logs()
        if remove:
            run([self.docker, "rm", name])

    def _restart_container_in_place(self) -> None:
        name = self._container_name()
        run([self.docker, "start", name])
        self.identities.append(f"container {name} (started again in place)")

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self.mode == "process":
            self._start_process()
        else:
            self._start_container()
        self.wait_until_healthy()

    def restart(self) -> None:
        if self.mode == "process":
            self._stop_process()
            self.generation += 1
            self._start_process()
        elif self.mode == "container-replace":
            self._stop_container(remove=True)
            self.generation += 1
            self._start_container()
        else:
            self._stop_container(remove=False)
            self.generation += 1
            self._restart_container_in_place()
        self.wait_until_healthy()

    def wait_until_healthy(self) -> None:
        deadline = time.monotonic() + HEALTH_TIMEOUT
        last = "no response"
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise HarnessError(
                    f"the service exited during startup "
                    f"(code {self.process.returncode}); see {self.logs[-1]}"
                )
            try:
                response = httpx.get(f"{self.base_url}/health", timeout=2.0)
                if response.status_code == 200:
                    return
                last = f"HTTP {response.status_code}"
            except httpx.HTTPError as exc:
                last = type(exc).__name__
            time.sleep(0.5)
        raise HarnessError(f"service never became healthy ({last})")

    def teardown(self) -> None:
        try:
            if self.mode == "process":
                self._stop_process()
            elif self.docker:
                name = self._container_name()
                subprocess.run(  # noqa: S603 - fixed argv, shell=False
                    [self.docker, "rm", "-f", name],
                    check=False,
                    capture_output=True,
                )
                subprocess.run(  # noqa: S603 - fixed argv, shell=False
                    [self.docker, "image", "rm", self.image],
                    check=False,
                    capture_output=True,
                )
        except OSError:
            pass

    def startup_events(self) -> int:
        """How many structured startup lines the current log holds."""
        log = self.capture_logs()
        count = 0
        for line in log.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get("event") == "startup":
                count += 1
        return count


def exercise(instance: Instance, report: Reporter, evidence: dict[str, Any]) -> None:
    requirement, description = MODES[instance.mode]
    svc = Service(instance.base_url, instance.run_id, report)
    tag = svc.suite_tag("persistence")
    try:
        # 1-2. create the asset and record its id
        created = svc.post_asset(svc.payload("persistence", "record"))
        if created.status_code != 201:
            raise HarnessError(f"could not create the test asset: {seen(created)}")
        original = body_of(created)
        asset_id, hostname = original["id"], original["hostname"]
        report.check(
            "PER-STEP-1",
            "create a uniquely named asset",
            "201",
            seen(created),
            True,
        )
        report.info(
            "PER-STEP-2",
            "recorded identity",
            {
                "id": asset_id,
                "hostname": hostname,
                "first_seen": original["first_seen"],
            },
        )

        # 3. prove it exists
        got = svc.http.get(f"{API}/{asset_id}")
        listed = svc.http.get(API, params={"tag": tag})
        stats = body_of(svc.http.get(STATS))
        present = (
            got.status_code == 200
            and body_of(got) == original
            and listed.status_code == 200
            and [item["id"] for item in body_of(listed)] == [asset_id]
            and stats.get("total") == 1
        )
        report.check(
            "PER-STEP-3",
            "the asset is observable before the restart",
            "GET 200 identical, listed once, stats total 1",
            {
                "get": got.status_code,
                "listed": [item["id"] for item in body_of(listed)]
                if listed.status_code == 200
                else seen(listed),
                "total": stats.get("total"),
            },
            present,
        )
        if not present:
            raise HarnessError("the asset was not observable before the restart")
        evidence["before"] = {"asset": original, "stats": stats}

        # 4. replace or restart the instance
        before_identity = instance.identities[-1]
        before_generation = instance.generation
        instance.restart()
        after_identity = instance.identities[-1]
        report.check(
            "PER-STEP-4",
            f"the instance was replaced: {description}",
            "a new generation serving on the same port",
            {
                "before": before_identity,
                "after": after_identity,
                "previous exit code": instance.exit_codes[-1],
            },
            before_identity != after_identity,
        )
        # 143 is 128+SIGTERM: the signal was handled, the shutdown lifespan
        # ran, and the process then exited with the signal status. 137 would
        # mean SIGKILL after the grace period.
        exit_code = str(instance.exit_codes[-1])
        report.check(
            "PER-STEP-4b",
            "the previous generation shut down cleanly",
            "exit code 0 or 143 (SIGTERM handled, not SIGKILL)",
            exit_code,
            exit_code in ("0", "143"),
            note="" if exit_code in ("0", "143") else "137 means SIGKILL",
        )
        started = instance.startup_events()
        report.check(
            "PER-STEP-4c",
            "the new generation logged a fresh startup line",
            ">=1 startup event in the new generation's log",
            started,
            started >= 1,
            note="" if started >= 1 else "log capture may lag behind the container",
        )
        evidence["restart"] = {
            "mode": instance.mode,
            "before": before_identity,
            "after": after_identity,
            "generations": [before_generation, instance.generation],
            "exit_codes": [str(code) for code in instance.exit_codes],
        }

        # 5. ask for the same id
        after = svc.http.get(f"{API}/{asset_id}")
        survived = after.status_code == 200 and body_of(after) == original
        report.check(
            requirement,
            f"the asset survives when {description}",
            f"GET {asset_id} -> 200 with the original record",
            seen(after),
            survived,
            note="" if survived else ARCHITECTURAL,
        )
        stats_after = body_of(svc.http.get(STATS))
        report.check(
            f"{requirement}-STATS",
            "the inventory contents survive the restart",
            {"total": 1},
            {"total": stats_after.get("total")},
            stats_after.get("total") == 1,
            note="" if stats_after.get("total") == 1 else ARCHITECTURAL,
        )

        # 6. hostname uniqueness
        again = svc.post_asset(svc.payload("persistence", "record"))
        reissued = body_of(again).get("id") if again.status_code == 201 else None
        report.check(
            f"{requirement}-UNIQUE",
            "hostname uniqueness survives the restart",
            "409 -- the hostname is still registered to the original asset",
            seen(again),
            again.status_code == 409,
            note=""
            if again.status_code == 409
            else "the same hostname was accepted again under a NEW id: asset "
            "identity is not stable across a restart, so ids held by any "
            "client become permanently wrong. " + ARCHITECTURAL,
        )

        # 7. record the data-loss behaviour
        loss = {
            "record_survived": survived,
            "records_before": 1,
            "records_after": stats_after.get("total"),
            "hostname_reaccepted": again.status_code == 201,
            "original_id": asset_id,
            "reissued_id": reissued,
            "identity_stable": reissued is None or reissued == asset_id,
            "first_seen_before": original["first_seen"],
            "first_seen_after": body_of(again).get("first_seen")
            if again.status_code == 201
            else None,
            "classification": "architectural (in-memory store)"
            if not survived
            else "n/a",
        }
        report.info("PER-STEP-7", "observed data-loss behaviour", loss)
        evidence["data_loss"] = loss
        evidence["after"] = {"stats": stats_after}
    finally:
        print("\n--- cleanup ---")
        try:
            svc.cleanup()
        except httpx.HTTPError as exc:
            report.check(
                "CLEANUP",
                "cleanup completed",
                "no transport errors",
                f"{type(exc).__name__}: {exc}",
                False,
            )
        svc.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Local persistence / restart experiment (destructive)."
    )
    parser.add_argument("--mode", choices=sorted(MODES), default="process")
    parser.add_argument(
        "--confirm-restart",
        action="store_true",
        help="required: acknowledges that this restarts the instance it started",
    )
    parser.add_argument(
        "--evidence-dir",
        help="where to write logs and the evidence file "
        "(default: a fresh temporary directory)",
    )
    opts = parser.parse_args(argv)

    if not opts.confirm_restart:
        print(
            "refusing to run without --confirm-restart.\n"
            "This test stops and restarts a throwaway instance it starts "
            "itself, and is expected to demonstrate data loss (F-03).",
            file=sys.stderr,
        )
        return 2

    run_id = new_run_id("persist")
    workdir = Path(
        opts.evidence_dir or tempfile.mkdtemp(prefix=f"cyber-inventory-{run_id}-")
    )
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        commit = run([shutil.which("git") or "git", "rev-parse", "HEAD"])
    except HarnessError:
        commit = "unknown"

    report = Reporter()
    instance = Instance(opts.mode, commit, workdir, run_id)
    evidence: dict[str, Any] = {
        "run_id": run_id,
        "mode": opts.mode,
        "requirement": MODES[opts.mode][0],
        "commit": commit,
        "started_at": datetime.now(UTC).isoformat(),
    }

    print("=" * 74)
    print("cyber-inventory persistence experiment (DESTRUCTIVE, local only)")
    print(f"  mode:        {opts.mode} -- {MODES[opts.mode][1]}")
    print(f"  requirement: {MODES[opts.mode][0]}")
    print(f"  run id:      {run_id}")
    print(f"  evidence:    {workdir}")
    print("=" * 74)

    harness_failed = False
    try:
        if opts.mode != "process":
            instance.build_image()
        instance.start()
        exercise(instance, report, evidence)
    except HarnessError as exc:
        harness_failed = True
        report.check(
            "HARNESS",
            "the experiment could be set up and run",
            "service starts, is observable, and restarts",
            str(exc),
            False,
        )
    finally:
        instance.teardown()
        evidence["identities"] = instance.identities
        evidence["exit_codes"] = [str(code) for code in instance.exit_codes]
        evidence["logs"] = [str(path) for path in instance.logs]
        evidence["finished_at"] = datetime.now(UTC).isoformat()
        evidence_file = workdir / f"persistence-{run_id}.json"
        evidence_file.write_text(json.dumps(evidence, indent=2, default=str))

    status = report.summary()
    print(f"\n  evidence file: {evidence_file}")
    for path in instance.logs:
        print(f"  service log:   {path}")
    if harness_failed:
        print("\n  VERDICT: harness error -- the experiment did not complete.")
        return 2
    if status != 0:
        print(
            "\n  VERDICT: requirement NOT met. Classification: ARCHITECTURAL,\n"
            "  expected with the current in-memory store (F-03). This is the\n"
            "  evidence that persistence is required; do not 'fix' the test."
        )
    return status


if __name__ == "__main__":
    raise SystemExit(main())
