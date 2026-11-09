"""Credential-context loading rules shared by the two-identity suite.

Kept separate from ``conftest.py`` so the tests can import the same helpers the
fixtures use (and so ``import conftest`` never has to resolve against whichever
conftest pytest happens to be running).

Rules this enforces, all of which are prerequisites for any isolation claim:

* a credential is a *path* to a file supplied by a Secret volume, never an
  environment value and never a command-line argument;
* the file must not be group/other accessible, otherwise a leaked context would
  silently invalidate the whole run;
* nothing here prints a credential: the value is returned to the caller and the
  caller's job is to keep it out of output (see ``redact`` in the test module).
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

import pytest

# Substrings that must never appear in reported output.
REDACTION_MARKERS = ("access_token", "id_token", "refresh_token", "client_secret")


@dataclass(frozen=True)
class Context:
    """One identity: a token file path plus the label under test."""

    name: str
    token_file: Path

    def token(self) -> str:
        """Read the credential. Callers must never print the result."""
        try:
            mode = stat.S_IMODE(self.token_file.stat().st_mode)
        except OSError as exc:
            pytest.skip(f"context '{self.name}' token file unreadable ({exc.strerror})")
        if mode & 0o077:
            pytest.fail(
                f"context '{self.name}' token file is group/other-readable "
                f"(mode {oct(mode)}): refusing to run identity tests against a "
                f"credential that is not private to the harness"
            )
        value = self.token_file.read_text(encoding="utf-8").strip()
        if not value:
            pytest.skip(f"context '{self.name}' token file is empty")
        return value


def load_context(env_var: str, label: str) -> Context:
    """Build a :class:`Context` from ``<env_var>``, or skip/fail precisely."""
    path = (os.environ.get(env_var) or "").strip()
    value_var = env_var.replace("_FILE", "_TOKEN")
    if os.environ.get(value_var):
        pytest.fail(
            f"{value_var} carries a credential value: secrets must arrive from a Secret-volume "
            f"file referenced by {env_var}, never from the environment or a command line"
        )
    if not path:
        pytest.skip(f"{env_var} is not set: context '{label}' is required (two distinct identities)")
    resolved = Path(path)
    if not resolved.is_file():
        pytest.skip(f"{env_var} points at {path}, which is not a file (Secret volume not mounted?)")
    return Context(label, resolved)
