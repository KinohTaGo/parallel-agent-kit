# parallel-agent-kit

Tested building blocks for running several coding agents side by side on one machine:
an **admission gate**, a **file-based mailbox** between sessions, and **guards for a shared
git working tree**.

[日本語の README](docs/ja/README.md)

**Status:** 0.1.0. Python 3.9+ standard library only (macOS and Linux). Tests: pytest + bats.

## Why

One agent in one checkout is easy. Several agents on the same machine, often in the same
checkout, fail in ways a single agent never does: the machine runs out of memory because
everyone started at once, a message to another session is silently skipped, or one agent's
`git stash` takes away another agent's unfinished work.

Each piece here is a small, general rewrite of a fix for a failure that actually happened
in a long-running multi-agent setup. The rule behind every piece is the same:
**a check must be able to say "I could not check"** — a silent pass is the most expensive
kind of bug, because nobody goes looking for it.

## What is inside

| Piece | The failure it addresses | Entry point |
|---|---|---|
| Admission gate | Too many agents start at once; swap fills up and every session slows down together | `python3 -m agentkit.gate check` / `hook` |
| Mailbox | A message between sessions is shown to nobody, or a new session is flooded with old mail | `python3 -m agentkit.mailbox send / inbox / show / mark` |
| Worktree guard | `stash`, `reset --hard`, `commit -a`, `--amend`, `pull --rebase` destroy or capture other agents' work | `python3 -m agentkit.worktree check` / `hook` |
| `safe-commit` | A commit picks up files another agent staged | `bin/safe-commit -m MSG -- path...` |

### Admission gate (`src/agentkit/gate.py`)

* Reads **pressure** (1-minute load per core, swap in use) and a machine-wide **ledger** of
  recent starts (JSON lines under an exclusive file lock).
* Pressure picks a tier, and each tier caps the number of starts in a rolling window
  (defaults: 6 per hour from 0.70, 3 from 0.85, 1 from 0.95, none at 1.0).
* **Fails open, and says so.** If a probe or the ledger cannot be read, the start is allowed
  and the reason names the missing input. A gate that crashes would block all work.
* **Swap total 0 means "no swap configured", not "no swap left".** Right after a reboot some
  systems report total 0 / free 0; reading that as "free below threshold" once stopped every job.

### Mailbox (`src/agentkit/mailbox.py`)

* Messages are JSON files (mode 600). Addresses: `<lane>`, `<lane>#<session>` (8+ characters),
  or `all`. A *lane* is a role or a project; several sessions can serve one lane.
* **The cursor is a floor, not a bookmark.** It only moves across a contiguous run of messages
  that were delivered or not addressed to you. A message you skipped holds it, so it is offered
  again instead of disappearing below a "last read" mark.
* **Peek does not consume.** `inbox` without `--deliver` changes nothing. `show <id>` then
  `mark --as <reader> <id>` records exactly what was read. `inbox --deliver` marks a message
  only after writing it, so `| head` does not swallow what it cut off (text already sitting in
  the pipe buffer is the one exception; use show + mark when you need an exact record).
* **New sessions start from the lane:** not from zero (no flood of history), not from "now"
  (mail sent while nobody of that lane was running is still delivered).
* **Clock skew:** ids start with a UTC timestamp; readers look back 10 minutes below their floor,
  so a message from a machine whose clock is behind is not lost.
* Short session ids are refused at send time (a too-short id once matched nobody and fell back
  to the whole lane).

### Worktree guard (`src/agentkit/worktree.py`) and `safe-commit`

* Reads a shell command line **before** the shell runs it and reports git commands that are
  harmful in a shared tree, including inside `&&` chains, `bash -c '…'`, `$( … )`, behind
  wrappers such as `sudo`/`env`/`timeout`/`nice`, `eval`, shell keywords, line continuations,
  and with `git -C dir`. Heredoc bodies are treated as data.
* Knows that git accepts abbreviated long options (`--ha` is `--hard`), expands configured
  aliases with `git config`, and refuses inline `-c alias.*=` it cannot see through.
* Catches `git commit -m "… \`cmd\` …"`: the shell runs the backticked text before git starts,
  so no git hook can see it. Use `-F <file>` or single quotes.
* `safe-commit` commits only the files you name (`git commit -- <paths>`, which ignores other
  agents' staged work; paths are literal, never globs), adds new files first, then **checks the commit**: if anything else got
  in (a hook, a concurrent writer), it exits 3 and rewrites nothing.

## Quick start

```sh
git clone https://github.com/KinohTaGo/parallel-agent-kit
cd parallel-agent-kit
export PYTHONPATH=$PWD/src

python3 -m agentkit.gate check                # ALLOW: load=0.31; swap=0.00
python3 -m agentkit.gate check --record build # also record one start in the ledger

MB=$(mktemp -d)
echo "tests are red on main" | python3 -m agentkit.mailbox --root "$MB" \
  send --from docs#reviewer-01 --to build --subject "please look" --body-file -
python3 -m agentkit.mailbox --root "$MB" inbox --as build#worker-0001            # peek
python3 -m agentkit.mailbox --root "$MB" inbox --as build#worker-0001 --deliver  # read + mark

python3 -m agentkit.worktree check "git add -A && git commit -m wip"
# [add-all] stages other agents' unfinished edits; add named paths: git add -A

bin/safe-commit -m "Fix parser" -- src/parser.py tests/test_parser.py
```

## Optional: wiring into an agent tool as pre-execution hooks

Both the gate and the worktree guard have a `hook` mode that reads one tool call as JSON on
stdin and prints a `deny` decision when needed (the `PreToolUse` hook format of Claude Code;
other tools with a similar hook can use the same scripts):

```json
{
  "hooks": {
    "PreToolUse": [
      { "matcher": "Bash",
        "hooks": [{ "type": "command", "command": "python3 /path/to/src/agentkit/worktree.py hook" }] },
      { "matcher": "Agent|Task",
        "hooks": [{ "type": "command", "command": "python3 /path/to/src/agentkit/gate.py hook" }] }
    ]
  }
}
```

After editing a hook script, run `python3 -m py_compile <file>`: a pre-execution hook that
fails to start can block every tool call, and then it cannot be fixed from inside the session.

## Scope

* The gate limits the load **you** put on **your own machine**. Nothing here touches accounts,
  credentials or any service's limits, and nothing drives an agent automatically.
* The mailbox is a local directory. Syncing it between machines (for example through a git
  repository) is up to you; the clock-skew lookback exists for that case.
* The worktree guard catches mistakes. It is not a security boundary: it reads text, so a
  command name built at run time (`$G stash`) is only reported when a git subcommand word
  appears next to it.
* Windows is not supported (`fcntl` file locks).

## Tests

```sh
python3 -m pytest -q tests     # 107 tests
bats tests/bats                # 7 tests for safe-commit
```

Each design rule above has a test, and each of those tests was checked by breaking the rule
in a copy of the code and watching the test fail (for example: letting the mailbox floor jump
like a bookmark, treating swap total 0 as full, removing the `stash` rule, skipping the
`safe-commit` verification).

## License

MIT
