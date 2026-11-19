"""Command line for the local paper book.

``--smoke`` reads the public quote and does not create a position.
``--once`` runs one deterministic demo cycle. There is no live flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from .config import BitgetPaperConfigError, load_raw_config
from .engine import run_cycle
from .models import StateError


def main(argv: list[str] | None = None, transport=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bitget BTCUSDT spot public quotes with a local demo fill. "
            "Does not accept API keys and does not place exchange orders."
        )
    )
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one local demo cycle")
    mode.add_argument("--smoke", action="store_true", help="public quote only; no fill")
    args = parser.parse_args(argv)

    try:
        config = load_raw_config(args.config)
        result = asyncio.run(run_cycle(config, transport=transport, smoke=args.smoke))
    except BitgetPaperConfigError as exc:
        print(
            json.dumps(
                {"status": "config_rejected", "reason": exc.reason, "execution": "not_started"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    except StateError as exc:
        print(
            json.dumps(
                {"status": "state_rejected", "reason": exc.reason, "fill_written": False},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 4
    except Exception:
        print(
            json.dumps(
                {"status": "failed", "reason": "unexpected_error", "fill_written": False},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result.summary, ensure_ascii=False))
    return result.exit_code
