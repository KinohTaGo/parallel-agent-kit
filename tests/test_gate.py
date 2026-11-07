import io
import json

import pytest

from agentkit import gate
from agentkit.gate import Policy, decide, parse_swap

NOW = 1_800_000_000.0


def probes(load=0.1, swap=0.0):
    return {"load": lambda: load, "swap": lambda: swap}


@pytest.fixture
def ledger(tmp_path):
    return str(tmp_path / "state" / "starts.jsonl")


def test_low_pressure_allows_and_records(ledger):
    d = decide(ledger, Policy(), now=NOW, probes=probes(), record_label="a")
    assert d.allow and d.cap is None
    assert len(gate.read_starts(ledger, NOW, 3600)) == 1


def test_tier_cap_counts_starts_in_window(ledger):
    pol = Policy(tiers=((0.5, 2),))
    for i in range(2):
        assert decide(ledger, pol, now=NOW + i, probes=probes(load=0.6), record_label=f"s{i}").allow
    d = decide(ledger, pol, now=NOW + 2, probes=probes(load=0.6), record_label="s2")
    assert not d.allow and d.starts_in_window == 2 and d.cap == 2
    assert len(gate.read_starts(ledger, NOW + 2, 3600)) == 2  # a refused start is not recorded


def test_old_starts_leave_the_window(ledger):
    pol = Policy(tiers=((0.5, 1),), window_seconds=60)
    assert decide(ledger, pol, now=NOW, probes=probes(load=0.6), record_label="x").allow
    assert not decide(ledger, pol, now=NOW + 30, probes=probes(load=0.6)).allow
    assert decide(ledger, pol, now=NOW + 61, probes=probes(load=0.6)).allow


def test_deny_at_refuses_regardless_of_ledger(ledger):
    d = decide(ledger, Policy(), now=NOW, probes=probes(swap=1.0))
    assert not d.allow and any("deny_at" in r for r in d.reasons)


def test_pressure_is_the_worst_signal(ledger):
    d = decide(ledger, Policy(tiers=((0.9, 0),)), now=NOW, probes=probes(load=0.1, swap=0.93))
    assert not d.allow and d.pressure == pytest.approx(0.93)


def test_failing_probe_is_reported_not_fatal(ledger):
    def boom():
        raise OSError("no sysctl")

    d = decide(ledger, Policy(), now=NOW, probes={"load": lambda: 0.2, "swap": boom})
    assert d.allow
    assert any("swap unavailable" in r for r in d.reasons)


def test_no_signal_at_all_fails_open_and_says_so(ledger):
    def boom():
        raise RuntimeError

    d = decide(ledger, Policy(), now=NOW, probes={"load": boom})
    assert d.allow and d.pressure is None
    assert any("fail-open" in r for r in d.reasons)


def test_corrupt_ledger_fails_open_and_says_so(ledger, tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "starts.jsonl").write_text("not json\n")
    d = decide(ledger, Policy(), now=NOW, probes=probes())
    assert d.allow and any("ledger unreadable" in r for r in d.reasons)


def test_zero_swap_total_means_no_pressure():
    # right after a reboot some systems report total 0 / used 0
    assert parse_swap(0, 0) == 0.0
    assert parse_swap(1000, 250) == 0.25


def test_ledger_file_is_private(ledger):
    import os
    import stat

    decide(ledger, Policy(), now=NOW, probes=probes(), record_label="a")
    assert stat.S_IMODE(os.stat(ledger).st_mode) == 0o600


def test_hook_denies_with_reason(ledger, monkeypatch, capsys):
    monkeypatch.setattr(gate, "measure", lambda policy, probes=None: (1.5, ["load=1.50"]))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Agent", "tool_input": {}})))
    assert gate.main(["--ledger", ledger, "hook"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_hook_ignores_other_tools_and_bad_input(ledger, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Read"})))
    assert gate.main(["--ledger", ledger, "hook"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert gate.main(["--ledger", ledger, "hook"]) == 0
    assert capsys.readouterr().out == ""


def test_check_exit_codes(ledger, monkeypatch):
    monkeypatch.setattr(gate, "measure", lambda policy, probes=None: (0.1, ["load=0.10"]))
    assert gate.main(["--ledger", ledger, "check"]) == 0
    monkeypatch.setattr(gate, "measure", lambda policy, probes=None: (2.0, ["load=2.00"]))
    assert gate.main(["--ledger", ledger, "check"]) == 1


def test_real_probes_return_a_fraction_or_say_unsupported():
    import platform

    assert gate.load_pressure(2.0) >= 0.0
    if platform.system() in ("Darwin", "Linux"):
        assert 0.0 <= gate.swap_pressure() <= 1.0
    else:
        with pytest.raises(RuntimeError):
            gate.swap_pressure()
