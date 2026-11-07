import os
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest

from agentkit.mailbox import AddressError, Mailbox

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
A1 = "build#aaaaaaaa01"
A2 = "build#aaaaaaaa02"
B1 = "docs#bbbbbbbb01"


def at(seconds):
    return T0 + timedelta(seconds=seconds)


@pytest.fixture
def box(tmp_path):
    return Mailbox(str(tmp_path / "mb"))


def ids(msgs):
    return [m["id"] for m in msgs]


def test_peek_does_not_consume(box):
    m = box.send(B1, "build", "hi", "body", now=at(1))
    assert ids(box.peek(A1)) == [m]
    assert ids(box.peek(A1)) == [m]


def test_skipped_message_is_not_lost_when_later_ones_are_delivered(box):
    m1 = box.send(B1, "build", "one", "", now=at(1))
    m2 = box.send(B1, "build", "two", "", now=at(2))
    m3 = box.send(B1, "build", "three", "", now=at(3))
    box.deliver(A1, [m1, m3])  # m2 was filtered out / not shown
    assert ids(box.peek(A1)) == [m2]
    box.deliver(A1, [m2])
    assert box.peek(A1) == []


def test_floor_only_advances_over_contiguous_handled_messages(box):
    m1 = box.send(B1, "build", "one", "", now=at(1))
    box.send(B1, "build", "two", "", now=at(2))
    m3 = box.send(B1, "build", "three", "", now=at(3))
    box.deliver(A1, [m1, m3])
    state = box._read_json(box._reader_path("build", "aaaaaaaa01"), {})
    assert state["floor"] == m1


def test_not_addressed_messages_do_not_hold_the_floor(box):
    box.send(B1, "other", "x", "", now=at(1))
    m2 = box.send(B1, "build", "y", "", now=at(2))
    box.deliver(A1, [m2])
    state = box._read_json(box._reader_path("build", "aaaaaaaa01"), {})
    assert state["floor"] == m2


def test_session_address_reaches_only_that_session(box):
    m = box.send(B1, "build#aaaaaaaa01", "just you", "", now=at(1))
    assert ids(box.peek(A1)) == [m]
    assert box.peek(A2) == []


def test_session_prefix_of_8_or_more_matches(box):
    m = box.send(B1, "build#aaaaaaaa", "prefix", "", now=at(1))
    assert ids(box.peek(A1)) == [m]


def test_short_session_id_is_refused(box):
    with pytest.raises(AddressError):
        box.send(B1, "build#abc", "too short", "")


def test_broadcast_reaches_every_lane_but_not_the_sender(box):
    m = box.send(A1, "all", "everyone", "", now=at(1))
    assert ids(box.peek(B1)) == [m]
    assert ids(box.peek(A2)) == [m]
    assert box.peek(A1) == []


def test_new_session_starts_from_the_lane_floor(box):
    old = box.send(B1, "build", "old", "", now=at(1))
    box.deliver(A1, [old])
    later = box.send(B1, "build", "sent while A2 did not exist", "", now=at(2))
    # A2 is new: not flooded with what the lane already handled, but gets what nobody read
    assert ids(box.peek(A2)) == [later]


def test_late_message_below_the_floor_is_still_offered(box):
    m1 = box.send(B1, "build", "one", "", now=at(100))
    box.deliver(A1, [m1])
    late = box.send(B1, "build", "clock behind", "", now=at(40))  # 60 s behind
    assert ids(box.peek(A1)) == [late]


def test_message_files_are_private(box):
    m = box.send(B1, "build", "s", "b", now=at(1))
    path = os.path.join(box.msg_dir, m + ".json")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


def test_deliver_rejects_unknown_ids(box):
    with pytest.raises(KeyError):
        box.deliver(A1, ["20260101T000000000000Z-abcd"])


def _cli(root, *args, input_text=None):
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    env = dict(os.environ, PYTHONPATH=src)
    return subprocess.run(
        [sys.executable, "-m", "agentkit.mailbox", "--root", root, *args],
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
    )


def test_cli_subject_file_keeps_shell_metacharacters(tmp_path):
    root = str(tmp_path / "mb")
    subj = tmp_path / "subject.txt"
    subj.write_text("run `make test` and $(date)\n")
    r = _cli(
        root,
        "send",
        "--from",
        B1,
        "--to",
        "build",
        "--subject-file",
        str(subj),
        "--body-file",
        "-",
        input_text="body\n",
    )
    assert r.returncode == 0, r.stderr
    shown = _cli(root, "show", r.stdout.strip())
    assert "subject: run `make test` and $(date)" in shown.stdout


def test_cli_deliver_into_a_closed_pipe_keeps_the_rest_pending(tmp_path):
    root = str(tmp_path / "mb")
    for i in range(30):
        Mailbox(root).send(B1, "build", f"m{i}", "x\n" * 2000, now=at(i))
    src = os.path.join(os.path.dirname(__file__), "..", "src")
    env = dict(os.environ, PYTHONPATH=src)
    reader = subprocess.Popen(
        [sys.executable, "-m", "agentkit.mailbox", "--root", root, "inbox", "--as", A1, "--deliver"],
        stdout=subprocess.PIPE,
        env=env,
    )
    first = reader.stdout.read(100)  # read a little, then go away like `| head -c 100`
    reader.stdout.close()
    reader.wait(timeout=30)
    assert first
    left = len(Mailbox(root).peek(A1))
    assert 0 < left < 30


def test_cli_inbox_without_deliver_is_a_peek(tmp_path):
    root = str(tmp_path / "mb")
    Mailbox(root).send(B1, "build", "s", "b", now=at(1))
    r = _cli(root, "inbox", "--as", A1)
    assert "1 pending" in r.stdout
    assert len(Mailbox(root).peek(A1)) == 1


def test_cli_peek_show_mark_is_exact(tmp_path):
    root = str(tmp_path / "mb")
    m1 = Mailbox(root).send(B1, "build", "one", "b", now=at(1))
    m2 = Mailbox(root).send(B1, "build", "two", "b", now=at(2))
    assert _cli(root, "show", m2).returncode == 0
    assert _cli(root, "mark", "--as", A1, m2).returncode == 0
    assert ids(Mailbox(root).peek(A1)) == [m1]
    assert _cli(root, "mark", "--as", A1, "20260101T000000000000Z-ffff").returncode == 2
