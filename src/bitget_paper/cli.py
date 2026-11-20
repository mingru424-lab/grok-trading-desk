"""Command line for the local paper book.

``--run`` polls until stopped. ``--once`` is a single cycle. ``--smoke`` only
reads the public quote. ``--control`` writes the pause / emergency file and
does not trade. Nothing here accepts an API key or a live flag.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from pathlib import Path

from .config import BitgetPaperConfigError, load_raw_config, settings_from_root
from .engine import run_cycle
from .loop import run_loop
from .models import StateError
from .store import read_control, write_control

_CONTROLS = {
    "pause": "paused",
    "resume": "running",
    "emergency-stop": "emergency_stop",
    "clear-emergency": "running",
}


def main(argv: list[str] | None = None, transport=None, sleep=None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Bitget BTCUSDT spot public quotes with a local paper book. "
            "Does not accept API keys and does not place exchange orders."
        )
    )
    parser.add_argument("--config", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="one local paper cycle")
    mode.add_argument("--smoke", action="store_true", help="public quote only; no fill")
    mode.add_argument("--run", action="store_true", help="poll until stopped")
    mode.add_argument(
        "--control",
        choices=sorted(_CONTROLS),
        help="write pause or emergency state; does not trade",
    )
    mode.add_argument("--status", action="store_true", help="print the last status file")
    parser.add_argument("--max-cycles", type=int, default=None)
    args = parser.parse_args(argv)
    if args.max_cycles is not None and not args.run:
        print(
            json.dumps({"status": "config_rejected", "reason": "max_cycles_requires_run"}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2

    try:
        config = load_raw_config(args.config)
        if args.control:
            settings = settings_from_root(config)
            write_control(Path(settings.control_path), _CONTROLS[args.control])
            print(json.dumps({"status": "control_written", "mode": _CONTROLS[args.control], "execution": "not_started"}, ensure_ascii=False))
            return 0
        if args.status:
            settings = settings_from_root(config)
            path = Path(settings.status_path)
            if not path.exists():
                print(json.dumps({"status": "status_missing", "fill_written": False}, ensure_ascii=False), file=sys.stderr)
                return 4
            print(path.read_text(encoding="utf-8"))
            return 0
        if args.run:
            stop = asyncio.Event()

            async def _run():
                loop = asyncio.get_running_loop()
                for sig in (signal.SIGINT, signal.SIGTERM):
                    try:
                        loop.add_signal_handler(sig, stop.set)
                    except NotImplementedError:
                        pass
                return await run_loop(
                    config,
                    transport=transport,
                    max_cycles=args.max_cycles,
                    sleep=sleep,
                    stop_event=stop,
                )

            summary = asyncio.run(_run())
            print(json.dumps({"status": "stopped", "cycles": summary["cycles"], "execution": "simulate", "authenticated": False}, ensure_ascii=False))
            return 0
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
        code = 5 if exc.reason == "already_running" else 4
        print(
            json.dumps(
                {"status": "state_rejected", "reason": exc.reason, "fill_written": False},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return code
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


def observed_control(config_path: str) -> str:
    settings = settings_from_root(load_raw_config(config_path))
    return read_control(Path(settings.control_path)).mode
