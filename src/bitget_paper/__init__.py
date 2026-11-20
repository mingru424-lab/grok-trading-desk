"""Local BTCUSDT spot paper book fed by Bitget's public market data.

This package never places an exchange order and never reads an API key.
It does not import the desk's stock or crypto executors. A configuration
error raises, and the caller must not fall through into ``src.desk``.
"""

from .engine import run_cycle

__all__ = ["run_cycle"]
