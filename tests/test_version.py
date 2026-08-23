# tests/test_version.py
"""Offline tests for the app-level ``--version`` flag.

``gmail-tidy --version`` (and ``python -m gmail_tidy --version``) prints the
package version and exits 0 — with NO config dir, token, OAuth, or network
involvement. The contract locked here:

- ``--version`` is an app-level (callback) option, NOT a subcommand flag;
  ``status --version`` is a usage error (exit 2).
- there is deliberately no ``-V`` shorthand.
- the printed version is ``gmail_tidy.__version__`` — the single source of
  truth that ``pyproject.toml`` also derives from (hatch dynamic version), so
  docs, package metadata, and the CLI can never drift.
- the eager callback prints the version and exits BEFORE any command body runs,
  so no config/token/Gmail code path is ever reached.

Fully offline: no sockets, no Gmail, no config-dir writes, no network. The
module-level tests use the Typer CliRunner; the module-entry test shells out to
``python -m gmail_tidy`` exactly like ``test_module_main_runs_help_offline``.
"""

from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys

import typer
from typer.testing import CliRunner

import gmail_tidy
from gmail_tidy import cli

runner = CliRunner()

VERSION_LINE_RE = re.compile(r"gmail-tidy, version (?P<version>[^\s]+)\s*$")


# --- CliRunner-level (fast, no subprocess) ----------------------------------


def test_version_flag_prints_single_source_version():
    """`--version` prints gmail_tidy.__version__ and nothing else (exit 0)."""
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    match = VERSION_LINE_RE.search(result.output)
    assert match, f"output is not a single 'gmail-tidy, version ...' line: {result.output!r}"
    assert match.group("version") == gmail_tidy.__version__


def test_version_flag_needs_no_config_or_token(tmp_path, monkeypatch):
    """`--version` with an empty, brand-new config dir still exits 0."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(cli.app, ["--version"])
    assert result.exit_code == 0
    assert gmail_tidy.__version__ in result.output


def test_version_flag_exits_before_any_command(tmp_path, monkeypatch):
    """Eager callback fires first: `--version status` prints version only."""
    monkeypatch.setenv("GMAIL_TIDY_CONFIG", str(tmp_path))
    result = runner.invoke(cli.app, ["--version", "status"])
    assert result.exit_code == 0
    assert gmail_tidy.__version__ in result.output
    assert "config dir" not in result.output  # status() body never ran


def test_version_flag_not_a_subcommand_flag():
    """`status --version` is a usage error: --version lives on the app only."""
    result = runner.invoke(cli.app, ["status", "--version"])
    assert result.exit_code == 2


def test_no_dash_v_shorthand():
    """There is deliberately no `-V` shorthand for --version."""
    result = runner.invoke(cli.app, ["-V"])
    assert result.exit_code == 2
    assert gmail_tidy.__version__ not in result.output


def test_callback_declares_version_option_only():
    """The app callback declares exactly one option: --version, eager."""
    callback = cli.app.registered_callback.callback
    decls = []
    for param in inspect.signature(callback).parameters.values():
        info = param.default
        if isinstance(info, typer.models.OptionInfo):
            assert getattr(info, "is_eager", False), "callback options must be eager"
            decls.extend(info.param_decls)
    assert decls == ["--version"]


# --- Subprocess-level (`python -m gmail_tidy --version`) --------------------


def test_module_main_prints_version_offline(tmp_path):
    """`python -m gmail_tidy --version` works without build, config, or Gmail."""
    env = dict(os.environ)
    env.pop("GMAIL_TIDY_CONFIG", None)  # module path must not read user config
    env["GMAIL_TIDY_CONFIG"] = str(tmp_path)  # empty dir: proves no config needed
    result = subprocess.run(
        [sys.executable, "-m", "gmail_tidy", "--version"],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0
    assert gmail_tidy.__version__ in (result.stdout + result.stderr)
