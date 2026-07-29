"""Modular, exchange-agnostic quantitative trading platform.

The package is organised as a hexagonal architecture: :mod:`quantplatform.core` holds the
domain models, ports and pure utilities that every other package depends on, and no core
module depends on any of them in return.
"""

from typing import Final

__version__: Final[str] = "0.1.0"

__all__ = ["__version__"]
