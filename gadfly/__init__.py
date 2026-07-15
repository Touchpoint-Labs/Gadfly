"""Gadfly — a Socratic supervision layer for AI coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("gadfly-ai")  # the distribution name, not the console script
except PackageNotFoundError:  # running from a source tree, not installed
    __version__ = "0.1.0+dev"
