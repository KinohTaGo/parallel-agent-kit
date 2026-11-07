"""File-based mailbox between agent sessions, with cursors that do not lose mail.

Messages are JSON files in ``<root>/messages/``; nothing runs in the background and no
session is driven automatically. A reader asks for its pending mail (``peek``), shows it,
and then marks exactly what it showed as delivered (``deliver``).

Addresses
    ``<lane>``           every session of that lane (a lane is a role or a project)
    ``<lane>#<session>`` one session; the session part must be 8+ characters
    ``all``              every lane

Design choices that came from real failures:

* **A cursor is a floor, not a bookmark.** An earlier design moved the reader's cursor to
  the newest message it had shown. Any message that had been skipped in between (filtered
  out, or not shown) fell below the cursor and was never offered again. Here the floor only
  advances across a *contiguous* run of messages that are delivered or not addressed to
  the reader; one undelivered message holds it.
* **Peeking must not consume.** ``peek`` never writes state. ``deliver`` takes the ids that
  were actually shown, and the CLI marks a message only after it has been written and
  flushed, so ``inbox --deliver | head`` does not swallow the messages ``head`` cut off.
* **New sessions start from the lane, not from zero and not from now.** Starting from zero
  floods a new session with history; starting from "now" drops mail sent while nobody of
  that lane was running. A new reader inherits the lane's floor.
* **Short session ids are refused.** A short prefix once matched nothing and the message
  went to the whole lane instead. Ids under 8 characters are rejected at send time.
* **Clocks are not trusted to be monotonic.** Ids start with a UTC timestamp, so a message
  written by a machine whose clock is behind can land just below a reader's floor. Readers
  look back ``LOOKBACK_SECONDS`` below the floor and keep their delivered set for that span.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import secrets
import sys
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

LOOKBACK_SECONDS = 600
MIN_SESSION_LEN = 8
_ID_RE = re.compile(r"^(\d{8}T\d{6}\d{6}Z)-[0-9a-f]{4}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class AddressError(ValueError):
    pass


def new_id(now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.strftime("%Y%m%dT%H%M%S%fZ") + "-" + secrets.token_hex(2)


def id_time(msg_id: str) -> Optional[datetime]:
    m = _ID_RE.match(msg_id)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%S%fZ").replace(tzinfo=timezone.utc)


def lookback_floor(floor: str) -> str:
    """The id below which a reader stops looking (floor minus LOOKBACK_SECONDS)."""
    t = id_time(floor) if floor else None
    if t is None:
        return floor
    return (t - timedelta(seconds=LOOKBACK_SECONDS)).strftime("%Y%m%dT%H%M%S%fZ")


def parse_address(addr: str) -> tuple:
    """Return (lane, session or None). 'all' -> ('all', None)."""
    lane, sep, sid = addr.partition("#")
    if not lane or not _NAME_RE.match(lane):
        raise AddressError(f"bad lane in address: {addr!r}")
    if sep:
        if len(sid) < MIN_SESSION_LEN or not _NAME_RE.match(sid):
            raise AddressError(f"session part must be {MIN_SESSION_LEN}+ characters of [A-Za-z0-9._-]: {addr!r}")
        return lane, sid
    return lane, None


def addressed_to(msg: dict, lane: str, sid: str) -> bool:
    if msg.get("from") == f"{lane}#{sid}":
        return False  # never deliver your own message back to you
    to_lane, to_sid = parse_address(msg["to"])
    if to_lane == "all":
        return True
    if to_lane != lane:
        return False
    return to_sid is None or sid.startswith(to_sid)


def addressed_to_lane(msg: dict, lane: str) -> bool:
    to_lane, to_sid = parse_address(msg["to"])
    return to_sid is None and to_lane in ("all", lane)


class Mailbox:
    def __init__(self, root: str):
        self.root = root
        self.msg_dir = os.path.join(root, "messages")
        self.state_dir = os.path.join(root, "state")

    # ------------------------------------------------------------ storage
    def _write_json(self, path: str, data: dict) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp.{os.getpid()}"
        old = os.umask(0o077)
        try:
            with open(tmp, "w") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)
        finally:
            os.umask(old)

    def _read_json(self, path: str, default: dict) -> dict:
        try:
            with open(path) as fh:
                return json.load(fh)
        except FileNotFoundError:
            return dict(default)

    @contextlib.contextmanager
    def _lock(self):
        os.makedirs(self.root, exist_ok=True)
        fd = os.open(os.path.join(self.root, ".lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def messages(self) -> list:
        if not os.path.isdir(self.msg_dir):
            return []
        out = []
        for name in sorted(os.listdir(self.msg_dir)):
            if name.endswith(".json"):
                with open(os.path.join(self.msg_dir, name)) as fh:
                    out.append(json.load(fh))
        return out

    def get(self, msg_id: str) -> dict:
        if not _ID_RE.match(msg_id):
            raise KeyError(msg_id)
        with open(os.path.join(self.msg_dir, msg_id + ".json")) as fh:
            return json.load(fh)

    # ------------------------------------------------------------ sending
    def send(self, sender: str, to: str, subject: str, body: str, now: Optional[datetime] = None) -> str:
        parse_address(sender)
        parse_address(to)
        msg_id = new_id(now)
        msg = {
            "id": msg_id,
            "from": sender,
            "to": to,
            "subject": subject,
            "body": body,
            "created": (now or datetime.now(timezone.utc)).isoformat(),
        }
        self._write_json(os.path.join(self.msg_dir, msg_id + ".json"), msg)
        return msg_id

    # ------------------------------------------------------------ reading
    def _reader_path(self, lane: str, sid: str) -> str:
        return os.path.join(self.state_dir, "readers", f"{lane}#{sid}.json")

    def _lane_path(self, lane: str) -> str:
        return os.path.join(self.state_dir, "lanes", f"{lane}.json")

    def _reader_state(self, lane: str, sid: str) -> dict:
        st = self._read_json(self._reader_path(lane, sid), {})
        if "floor" not in st:  # a new session inherits the lane's floor
            lane_st = self._read_json(self._lane_path(lane), {"floor": ""})
            st = {"floor": lane_st.get("floor", ""), "delivered": list(lane_st.get("delivered", []))}
        return st

    def peek(self, reader: str) -> list:
        lane, sid = parse_address(reader)
        if sid is None:
            raise AddressError("a reader must be <lane>#<session>")
        st = self._reader_state(lane, sid)
        low, done = lookback_floor(st["floor"]), set(st.get("delivered", []))
        return [m for m in self.messages() if m["id"] >= low and m["id"] not in done and addressed_to(m, lane, sid)]

    @staticmethod
    def _advance(state: dict, msgs: list, is_mine) -> dict:
        """Move the floor across the contiguous run of handled messages."""
        floor, done = state.get("floor", ""), set(state.get("delivered", []))
        for m in msgs:
            if m["id"] <= floor:
                continue
            if is_mine(m) and m["id"] not in done:
                break
            floor = m["id"]
        low = lookback_floor(floor)
        return {"floor": floor, "delivered": sorted(i for i in done if i >= low)}

    def deliver(self, reader: str, ids: Iterable[str]) -> None:
        lane, sid = parse_address(reader)
        if sid is None:
            raise AddressError("a reader must be <lane>#<session>")
        ids = list(ids)
        with self._lock():
            msgs = self.messages()
            known = {m["id"] for m in msgs}
            unknown = [i for i in ids if i not in known]
            if unknown:
                raise KeyError(f"unknown message ids: {unknown}")
            st = self._reader_state(lane, sid)
            st["delivered"] = sorted(set(st.get("delivered", [])) | set(ids))
            self._write_json(
                self._reader_path(lane, sid), self._advance(st, msgs, lambda m: addressed_to(m, lane, sid))
            )
            lane_st = self._read_json(self._lane_path(lane), {"floor": "", "delivered": []})
            lane_ids = [m["id"] for m in msgs if m["id"] in set(ids) and addressed_to_lane(m, lane)]
            lane_st["delivered"] = sorted(set(lane_st.get("delivered", [])) | set(lane_ids))
            self._write_json(self._lane_path(lane), self._advance(lane_st, msgs, lambda m: addressed_to_lane(m, lane)))


# ---------------------------------------------------------------- CLI
def _read_text(value: Optional[str], path: Optional[str]) -> str:
    if path == "-":
        return sys.stdin.read()
    if path:
        with open(path) as fh:
            return fh.read()
    return value or ""


def _render(m: dict) -> str:
    return f"--- {m['id']}\nfrom: {m['from']}\nto: {m['to']}\nsubject: {m['subject']}\n\n{m['body'].rstrip()}\n"


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentkit-mailbox", description=__doc__.splitlines()[0])
    p.add_argument("--root", default=os.environ.get("AGENTKIT_MAILBOX") or "mailbox")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("send")
    s.add_argument("--from", dest="sender", required=True)
    s.add_argument("--to", required=True)
    s.add_argument("--subject")
    s.add_argument("--subject-file", help="safer than --subject when the text has $ or backticks")
    s.add_argument("--body-file", help="path, or - for stdin")
    i = sub.add_parser("inbox")
    i.add_argument("--as", dest="reader", required=True)
    i.add_argument("--deliver", action="store_true", help="mark each message delivered after printing it")
    sh = sub.add_parser("show", help="print one message; marks nothing")
    sh.add_argument("id")
    mk = sub.add_parser("mark", help="mark the given ids delivered (after reading them with show)")
    mk.add_argument("--as", dest="reader", required=True)
    mk.add_argument("ids", nargs="+")
    args = p.parse_args(argv)
    box = Mailbox(args.root)
    try:
        if args.cmd == "send":
            subject = _read_text(args.subject, args.subject_file).strip()
            if not subject:
                p.error("a subject is required")
            print(box.send(args.sender, args.to, subject, _read_text(None, args.body_file)))
            return 0
        if args.cmd == "show":
            sys.stdout.write(_render(box.get(args.id)))
            return 0
        if args.cmd == "mark":
            box.deliver(args.reader, args.ids)
            return 0
        pending = box.peek(args.reader)
        if not args.deliver:
            for m in pending:
                print(f"{m['id']}  {m['from']} -> {m['to']}  {m['subject']}")
            print(f"{len(pending)} pending (peek: nothing marked)")
            return 0
        for m in pending:
            try:
                sys.stdout.write(_render(m))
                sys.stdout.flush()
            except BrokenPipeError:
                # The reader went away: what was not written stays pending. Text already in
                # the pipe buffer was marked; for an exact record use peek + show + mark.
                with contextlib.suppress(OSError):
                    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
                return 0
            box.deliver(args.reader, [m["id"]])
        return 0
    except (AddressError, KeyError) as exc:
        print(f"agentkit-mailbox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
