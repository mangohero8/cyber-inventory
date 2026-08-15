"""Build provenance.

This module is the linchpin of commit-to-production traceability.

Nothing here is computed at runtime from the source tree -- the values are
injected as environment variables by the container build, which in turn gets
them from the CI system, which gets them from the git commit that triggered it.

    git commit  ->  GitHub Actions  ->  docker build --build-arg  ->  ENV  ->  /version

That chain is the whole point. When something is broken in production, the
first question is always "which commit is actually running?" and this endpoint
answers it in one HTTP call instead of an afternoon of guessing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict

# "unknown" is a deliberate default. If you ever see it in a deployed
# environment, the build did not inject provenance and your traceability
# chain is broken -- that is a bug worth failing a smoke test over.
UNKNOWN = "unknown"


@dataclass(frozen=True)
class BuildInfo:
    """Immutable description of the artifact currently running."""

    service: str
    version: str
    commit: str
    commit_short: str
    build_time: str
    environment: str

    def as_dict(self) -> dict[str, str]:
        return asdict(self)

    @property
    def is_traceable(self) -> bool:
        """True when the build injected real provenance."""
        return self.commit != UNKNOWN


def _env(name: str, default: str = UNKNOWN) -> str:
    """Read an env var, treating empty strings as absent.

    Empty-string handling matters: CI systems frequently set a variable to ""
    rather than leaving it unset, and `os.environ.get(name, default)` would
    happily hand back the empty string.
    """
    value = os.environ.get(name, "").strip()
    return value or default


def get_build_info() -> BuildInfo:
    """Assemble build provenance from the environment."""
    commit = _env("GIT_COMMIT")
    return BuildInfo(
        service=_env("SERVICE_NAME", "cyber-inventory"),
        version=_env("APP_VERSION", "0.1.0"),
        commit=commit,
        commit_short=commit[:7] if commit != UNKNOWN else UNKNOWN,
        build_time=_env("BUILD_TIME"),
        environment=_env("ENVIRONMENT", "local"),
    )
