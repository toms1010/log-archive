"""log-archive: a secure Linux utility for archiving log directories.

The public surface is the :mod:`log_archive.cli` module and the
``log-archive`` console script. Importing this package is cheap; submodules
are not loaded until they are needed.
"""

from __future__ import annotations

__all__ = ["__version__"]

#: Single source of truth for the tool version, reported by ``--version``,
#: written into manifests, and stamped into built distributions.
__version__ = "1.0.0"
