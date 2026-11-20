"""Book files: one writer, versioned state, a separate control file.

The state file is replaced atomically. A non-blocking flock on a sibling lock
file stops a second process from writing the same book. Version 1 files from
the first paper build are migrated in memory to version 2. Any other version
is rejected and left on disk.
"""

from __future__ import annotations

import fcntl
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from .models import EquityPoint, PaperState, StateError, money_str


class BookLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            raise StateError("already_running") from None
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def lock_path_for(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + ".lock")


_V1_FIELDS = {
    "version",
    "label",
    "symbol",
    "category",
    "cash_usd",
    "quantity_btc",
    "cost_basis_usd",
    "realized_pnl_usd",
    "fees_paid_usd",
    "fill_count",
    "last_mark_price",
    "last_unrealized_pnl_usd",
    "updated_at",
}


def migrate_raw_state(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise StateError("state_invalid")
    version = raw.get("version")
    if version == 2:
        return raw
    if version != 1:
        raise StateError("state_version_unsupported")
    extra = set(raw) - _V1_FIELDS
    if extra:
        raise StateError("state_invalid")
    migrated = dict(raw)
    migrated["version"] = 2
    migrated["peak_equity_usd"] = str(raw.get("cash_usd", "0"))
    migrated["applied_ids"] = []
    migrated["last_bar_ms"] = None
    migrated["last_action"] = "NONE"
    migrated["cycle_seq"] = 0
    migrated["emergency_flattened"] = False
    migrated["equity_tail"] = []
    return migrated


def load_versioned_state(path: Path) -> tuple[PaperState, bool]:
    """Return the book and whether a v1 file was migrated in memory."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise StateError("state_invalid") from None
    migrated = raw.get("version") == 1 if isinstance(raw, dict) else False
    try:
        payload = migrate_raw_state(raw)
        state = PaperState.model_validate(payload)
    except StateError:
        raise
    except (TypeError, ValueError):
        raise StateError("state_invalid") from None
    return state, migrated


class ControlState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: str = "running"

    def checked(self) -> "ControlState":
        if self.mode not in {"running", "paused", "emergency_stop"}:
            raise StateError("control_invalid")
        return self


def read_control(path: Path) -> ControlState:
    if not path.exists():
        return ControlState(mode="running")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise StateError("control_invalid")
        return ControlState.model_validate(raw).checked()
    except StateError:
        raise
    except (OSError, json.JSONDecodeError, ValueError):
        raise StateError("control_invalid") from None


def write_control(path: Path, mode: str) -> None:
    state = ControlState(mode=mode).checked()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state.model_dump(), ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def equity_of(state: PaperState, unrealized: Decimal) -> Decimal:
    if state.quantity_btc == 0:
        return state.cash_usd
    return state.cash_usd + state.cost_basis_usd + unrealized


def remember_mark(
    state: PaperState,
    *,
    equity: Decimal,
    unrealized: Decimal,
    now_iso: str,
    action: str | None = None,
    bar_ms: int | None = None,
    correlation_id: str | None = None,
    fill: bool = False,
) -> PaperState:
    peak = state.peak_equity_usd if equity <= state.peak_equity_usd else equity
    point = EquityPoint(
        ts=now_iso,
        equity_usd=money_str(equity, signed=True),
        cash_usd=money_str(state.cash_usd),
        realized_usd=money_str(state.realized_pnl_usd, signed=True),
        unrealized_usd=money_str(unrealized, signed=True),
    )
    tail = [*state.equity_tail, point][-120:]
    applied = list(state.applied_ids)
    if fill and correlation_id and correlation_id not in applied:
        applied.append(correlation_id)
    applied = applied[-64:]
    update: dict[str, Any] = {
        "peak_equity_usd": peak,
        "equity_tail": tail,
        "applied_ids": applied,
    }
    if action is not None:
        update["last_action"] = action
    if fill and bar_ms:
        update["last_bar_ms"] = bar_ms
    return state.model_copy(update=update)


def drawdown_ratio(peak: Decimal, equity: Decimal) -> Decimal:
    if peak <= 0:
        return Decimal("0")
    if equity >= peak:
        return Decimal("0")
    return (peak - equity) / peak


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(payload, ensure_ascii=False, default=str)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
