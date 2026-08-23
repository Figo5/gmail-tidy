# Single source of truth for the package version. pyproject.toml derives its
# `version` from this file via hatch's dynamic versioning, and the CLI's
# `--version` flag prints exactly this value — so package metadata and the
# command surface can never drift apart.
__version__ = "0.1.0"
