"""Admission gate: decide whether one more agent process may start on this machine.

The gate looks at two things and nothing else:

* **pressure** of the machine right now (CPU load per core, swap in use), and
* a **ledger** of recent starts on this machine (a JSON-lines file shared by every
  caller, guarded by an exclusive file lock).

Pressure picks a tier; each tier caps how many starts are allowed inside a rolling
window. Above ``deny_at`` nothing starts.

Design choices that came from real failures:

* **Fail open, but say so.** The gate sits in front of every start. If a probe or
  the ledger cannot be read, a broken gate would stop all work, which is worse than
  one extra start. The decision is still ``allow``, and the reason list says which
  input was missing, so the gap is visible instead of silent.
* **"No swap configured" is not "out of swap".** Right after a reboot some systems
  report swap total 0 / free 0. Treating that as "free < threshold" once stopped every
  job; total 0 means no swap pressure.
* **Count starts per machine, not per caller.** Several independent sessions share one
  machine, so the ledger is machine-wide; a caller that started nothing can still be
  refused because others did.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import platform
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

DEFAULT_WINDOW = 3600.0
DEFAULT_TIERS = ((0.70, 6), (0.85, 3), (0.95, 1))  # (pressure >=, max starts per window)
DEFAULT_DENY_AT = 1.0


@dataclass
class Policy:
    window_seconds: float = DEFAULT_WINDOW
    tiers: tuple = DEFAULT_TIERS
    deny_at: float = DEFAULT_DENY_AT
    load_per_core_limit: float = 2.0  # load/cores at which load pressure reaches 1.0


@dataclass
class Decision:
    allow: bool
    pressure: Optional[float]
    starts_in_window: Optional[int]
    cap: Optional[int]
    reasons: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "allow": self.allow,
            "pressure": self.pressure,
            "starts_in_window": self.starts_in_window,
            "cap": self.cap,
            "reasons": self.reasons,
        }


# ---------------------------------------------------------------- probes
def load_pressure(limit: float) -> float:
    """Return 1-minute load per core divided by ``limit`` (1.0 == at the limit)."""
    one, _five, _fifteen = os.getloadavg()
    cores = os.cpu_count() or 1
    return (one / cores) / limit


def parse_swap(total: float, used: float) -> float:
    """Fraction of swap in use. total == 0 means no swap configured: no pressure."""
    if total <= 0:
        return 0.0
    return max(0.0, min(1.0, used / total))


def swap_pressure() -> float:
    system = platform.system()
    if system == "Darwin":
        out = subprocess.run(
            ["/usr/sbin/sysctl", "-n", "vm.swapusage"], capture_output=True, text=True, check=True
        ).stdout
        nums = dict(re.findall(r"(total|used)\s*=\s*([0-9.]+)M", out))
        return parse_swap(float(nums["total"]), float(nums["used"]))
    if system == "Linux":
        info = {}
        with open("/proc/meminfo") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                info[key] = float(rest.split()[0])
        total = info.get("SwapTotal", 0.0)
        return parse_swap(total, total - info.get("SwapFree", 0.0))
    raise RuntimeError(f"no swap probe for {system}")


Probe = Callable[[], float]


def measure(policy: Policy, probes: Optional[dict] = None) -> tuple:
    """Return (pressure or None, reasons). A failing probe is reported, not fatal."""
    probes = (
        probes
        if probes is not None
        else {
            "load": lambda: load_pressure(policy.load_per_core_limit),
            "swap": swap_pressure,
        }
    )
    values, reasons = [], []
    for name, probe in probes.items():
        try:
            v = float(probe())
        except Exception as exc:  # noqa: BLE001 - any probe failure is reported, never raised
            reasons.append(f"probe {name} unavailable ({type(exc).__name__}); not counted")
            continue
        values.append(v)
        reasons.append(f"{name}={v:.2f}")
    return (max(values) if values else None), reasons


# ---------------------------------------------------------------- ledger
@contextlib.contextmanager
def locked(path: str):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def read_starts(path: str, now: float, window: float) -> list:
    if not os.path.exists(path):
        return []
    starts = []
    with open(path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if now - float(rec["t"]) < window:
                starts.append(rec)
    return starts


def record_start(path: str, now: float, label: str, window: float) -> None:
    """Append one start and drop entries older than the window (caller holds the lock)."""
    keep = read_starts(path, now, window)
    keep.append({"t": now, "label": label})
    tmp = f"{path}.tmp.{os.getpid()}"
    old_umask = os.umask(0o077)
    try:
        with open(tmp, "w") as fh:
            for rec in keep:
                fh.write(json.dumps(rec) + "\n")
        os.replace(tmp, path)
    finally:
        os.umask(old_umask)


# ---------------------------------------------------------------- decision
def cap_for(pressure: float, policy: Policy) -> Optional[int]:
    """Max starts per window for this pressure; None means no cap."""
    cap = None
    for threshold, limit in sorted(policy.tiers):
        if pressure >= threshold:
            cap = limit
    return cap


def decide(
    ledger: str,
    policy: Policy,
    now: Optional[float] = None,
    probes: Optional[dict] = None,
    record_label: Optional[str] = None,
) -> Decision:
    now = time.time() if now is None else now
    pressure, reasons = measure(policy, probes)
    try:
        with locked(ledger):
            starts = read_starts(ledger, now, policy.window_seconds)
            n = len(starts)
            if pressure is None:
                reasons.append("no pressure signal: fail-open")
                d = Decision(True, None, n, None, reasons)
            elif pressure >= policy.deny_at:
                reasons.append(f"pressure {pressure:.2f} >= deny_at {policy.deny_at:.2f}")
                d = Decision(False, pressure, n, 0, reasons)
            else:
                cap = cap_for(pressure, policy)
                if cap is not None and n >= cap:
                    reasons.append(f"{n} starts in the last {policy.window_seconds:.0f}s, cap {cap} at this pressure")
                    d = Decision(False, pressure, n, cap, reasons)
                else:
                    d = Decision(True, pressure, n, cap, reasons)
            if d.allow and record_label is not None:
                record_start(ledger, now, record_label, policy.window_seconds)
            return d
    except (OSError, ValueError, KeyError) as exc:
        reasons.append(f"ledger unreadable ({type(exc).__name__}): fail-open")
        return Decision(True, pressure, None, None, reasons)


# ---------------------------------------------------------------- CLI / hook
def _default_ledger() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.environ.get("AGENTKIT_GATE_LEDGER") or os.path.join(base, "agentkit", "starts.jsonl")


def _hook(args, policy: Policy) -> int:
    """PreToolUse-style hook: read a tool call as JSON on stdin, answer on stdout."""
    try:
        call = json.load(sys.stdin)
        tool = call.get("tool_name", "")
    except (ValueError, AttributeError):
        print("agentkit gate: unreadable hook input; allowing", file=sys.stderr)
        return 0
    if tool not in args.tools:
        return 0
    d = decide(args.ledger, policy, record_label=tool)
    if not d.allow:
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "admission gate: " + "; ".join(d.reasons),
            }
        }
        print(json.dumps(out))
    elif any("fail-open" in r or "unavailable" in r for r in d.reasons):
        print("agentkit gate: " + "; ".join(d.reasons), file=sys.stderr)
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentkit-gate", description=__doc__.splitlines()[0])
    p.add_argument("--ledger", default=_default_ledger())
    p.add_argument("--window", type=float, default=DEFAULT_WINDOW)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="print a decision; exit 0 allow / 1 deny")
    c.add_argument("--record", metavar="LABEL", help="if allowed, record one start")
    c.add_argument("--json", action="store_true")
    h = sub.add_parser("hook", help="PreToolUse hook mode (stdin JSON)")
    h.add_argument("--tools", nargs="+", default=["Agent", "Task"])
    args = p.parse_args(argv)
    policy = Policy(window_seconds=args.window)
    if args.cmd == "hook":
        return _hook(args, policy)
    d = decide(args.ledger, policy, record_label=args.record)
    if args.json:
        print(json.dumps(d.as_dict()))
    else:
        print(("ALLOW" if d.allow else "DENY") + ": " + "; ".join(d.reasons))
    return 0 if d.allow else 1


if __name__ == "__main__":
    sys.exit(main())
