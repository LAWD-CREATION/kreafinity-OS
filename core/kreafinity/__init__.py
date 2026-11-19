"""Kreafinity OS core: AI system assistant with a free choice of LLM provider."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("kreafinity-os")
except PackageNotFoundError:  # running from a source checkout without install
    __version__ = "0.0.0"

__all__ = ["__version__"]
