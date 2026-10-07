"""Monotonic time budget shared by every phase of the audit.

Adobe requires a typical audit to finish in under five minutes. We design for
<180s and enforce a hard internal deadline (default 240s) so that the process
always has time left to serialise a valid report.

Nothing in this module sleeps except the polite inter-request pacing in
`http.py`; the budget is purely advisory arithmetic that callers must consult.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

# Phase reservations (seconds of *remaining* budget below which a phase stops
# starting new optional work). Tuned so report serialisation is never starved.
RESERVE_FOR_REPORT = 15.0       # compose + validate + serialise
RESERVE_FOR_ANALYSIS = 45.0     # deterministic analyzers over the snapshot
RESERVE_FOR_OPTIONAL = 75.0     # below this, skip render / off-site research


@dataclass
class Budget:
    """A monotonic deadline with helpers for bounded, degradable work."""

    total: float = 240.0
    started_at: float = field(default_factory=time.monotonic)
    _marks: dict = field(default_factory=dict)

    @property
    def deadline(self) -> float:
        return self.started_at + self.total

    def elapsed(self) -> float:
        return time.monotonic() - self.started_at

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def has(self, seconds: float) -> bool:
        """True when at least `seconds` of budget remain."""
        return self.remaining() >= seconds

    def allow_new_fetches(self) -> bool:
        """Stop discovering new pages once analysis + report time is at risk."""
        return self.remaining() > (RESERVE_FOR_ANALYSIS + RESERVE_FOR_REPORT)

    def allow_optional(self) -> bool:
        """Optional work (JS render, off-site corroboration) needs real headroom."""
        return self.remaining() > RESERVE_FOR_OPTIONAL

    def allow_analysis(self) -> bool:
        return self.remaining() > RESERVE_FOR_REPORT

    def clamp_timeout(self, wanted: float) -> float:
        """Never let a single socket outlive the audit."""
        return max(0.5, min(wanted, max(0.5, self.remaining() - RESERVE_FOR_REPORT)))

    def mark(self, name: str) -> None:
        """Record a phase boundary for `audit_metrics`."""
        self._marks[name] = round(self.elapsed() * 1000)

    def marks(self) -> dict:
        return dict(self._marks)

    def duration_ms(self) -> int:
        return round(self.elapsed() * 1000)
