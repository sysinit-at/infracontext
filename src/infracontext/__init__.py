"""Infracontext: Infrastructure documentation and troubleshooting CLI."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Single source of truth is pyproject.toml — read the installed package
    # metadata so __version__, `ic --version`, and the rendered-artifact
    # provenance marker can never drift apart again.
    __version__ = version("infracontext")
except PackageNotFoundError:  # pragma: no cover - only when running un-installed
    __version__ = "0.0.0+unknown"
