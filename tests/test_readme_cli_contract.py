# tests/test_readme_cli_contract.py
"""Contract test pinning README's CLI section to the ACTUAL Typer command surface.

The README ``## CLI`` section is the human-facing reference for every command,
subcommand, and option. These tests lock it to reality so the docs cannot drift
from the code without a test failure:

1. every top-level command (web, init, scan, run, summary, preview, apply, undo,
   status) and the ``auth`` group's subcommands (status, refresh, revoke) are
   documented in the README's CLI section;
2. every option string actually declared on those commands (``scan --all``,
   ``preview --compact/--explain/--json``, ``summary --run``, ``web
   --port/--no-browser``, ...) appears in the README's CLI section;
3. every ``--option`` string written in the README's CLI table is a real option
   on the corresponding command — the docs must never invent an option the CLI
   does not have (guards against copy-paste drift).

Command and option lists are introspected from the live Typer app (via
``inspect.signature`` on each command's callback), so the test adapts to the
actual API and never hardcodes a name list that could itself rot.

Fully offline: no sockets, no Gmail, no config-dir writes, no network. It
imports ``gmail_tidy.cli`` only (which imports rich/typer, already a normal
dependency of the package).
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import typer

from gmail_tidy import cli

# --- Where the documented contract lives ------------------------------------

README_PATH = Path(__file__).resolve().parent.parent / "README.md"


def _cli_section_text() -> str:
    """Text of README's ``## CLI`` section (between it and the next ``##``)."""
    text = README_PATH.read_text(encoding="utf-8")
    match = re.search(r"\n## CLI\n(.*?)(?=\n## )", text, flags=re.DOTALL)
    assert match, "README must contain a '## CLI' section followed by a '## ' heading"
    return match.group(1)


# --- Introspection (adapts to the actual Typer API) --------------------------


def _own_options(func) -> set[str]:
    """Option strings a command's callback declares via typer.Option(...)."""
    own: set[str] = set()
    for param in inspect.signature(func).parameters.values():
        default = param.default
        if isinstance(default, typer.models.OptionInfo):
            for decl in default.param_decls:
                assert decl.startswith("--"), f"option decl {decl!r} is not a --flag"
                own.add(decl)
    return own


def _top_level_commands() -> dict[str, set[str]]:
    """{command name: set of declared --options} for the root typer app."""
    out: dict[str, set[str]] = {}
    for info in cli.app.registered_commands:
        name = info.name or info.callback.__name__
        out[name] = _own_options(info.callback)
    return out


def _auth_subcommands() -> dict[str, set[str]]:
    """{subcommand name: set of declared --options} under the auth group."""
    out: dict[str, set[str]] = {}
    for group in cli.app.registered_groups:
        for sub in group.typer_instance.registered_commands:
            out[sub.name] = _own_options(sub.callback)
    return out


def _readme_option_map(section: str) -> dict[str, set[str]]:
    """Map each documented command verb to the --options named in its table row.

    The command cell is everything from the row's first ``| ``` up to the first
    ``|`` column separator (the cell that holds the usage form, e.g.
    ``` `scan [--limit N] [--all] [--rules ID...]` ```). Flags are matched as
    plain ``--name`` tokens inside that cell — the README does not backtick
    them — so the description column can never leak flags into the map.
    """
    rows: dict[str, set[str]] = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("| `"):
            continue
        cell, _, _ = stripped[2:].partition(" |")  # cut the row's usage column
        name_match = re.match(r"`([^`\s]+)", cell)
        if not name_match:
            continue
        verb = name_match.group(1)
        rows[verb] = set(re.findall(r"--[a-z][a-z0-9-]*", cell))
    return rows


# --- The contract tests ------------------------------------------------------


def test_top_level_commands_documented():
    """Every top-level CLI command has its own row in the README CLI table."""
    section = _cli_section_text()
    documented = set(_readme_option_map(section))
    missing = [
        name for name in _top_level_commands() if name not in documented
    ]
    assert not missing, (
        f"top-level command(s) missing from README '## CLI' section: {sorted(missing)}"
    )


def test_auth_subcommands_documented():
    """Every auth subcommand (status/refresh/revoke) appears in the CLI section."""
    section = _cli_section_text()
    missing = [
        sub for sub in _auth_subcommands()
        if re.search(rf"`auth {sub}`", section) is None
    ]
    assert not missing, (
        f"auth subcommand(s) missing from README '## CLI' section: {sorted(missing)}"
    )


def test_owned_options_documented():
    """Every option the CLI declares is documented under its command's row."""
    section = _cli_section_text()
    map_ = _readme_option_map(section)
    missing: dict[str, list[str]] = {}
    for name, opts in _top_level_commands().items():
        for opt in sorted(opts):
            if opt not in map_.get(name, set()):
                missing.setdefault(name, []).append(opt)
    assert not missing, (
        "option(s) owned by a command missing from its README CLI row: "
        + "; ".join(f"{k}: {v}" for k, v in sorted(missing.items()))
    )


def test_no_undocumented_option_claims():
    """Every `--option` written in the README CLI table is actually declared."""
    section = _cli_section_text()
    declared = {
        opt for opts in _top_level_commands().values() for opt in opts
    }
    claimed = set(re.findall(r"`(--[a-z][a-z0-9-]*)`", section))
    phantom = sorted(claimed - declared)
    assert not phantom, (
        f"README CLI section documents option(s) the CLI does not declare: {phantom}"
    )
