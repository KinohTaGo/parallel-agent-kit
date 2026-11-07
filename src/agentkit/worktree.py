"""Guard for a git working tree that several agents edit at the same time.

When more than one agent works in the *same* checkout, the working tree and the index
are shared. Commands that are harmless for one person become destructive:

* ``git stash`` / ``reset --hard`` / ``checkout <ref>`` / ``restore`` / ``clean -f``
  rewrite files that other agents are still editing;
* ``commit -a`` / ``add -A`` / ``add .`` sweep other agents' unfinished edits into
  your commit;
* ``commit --amend`` rewrites HEAD, which may already be someone else's commit;
* ``pull --rebase`` / ``rebase`` / ``push --force`` rewrite history under others.

``check_command`` reads a shell command line *before* the shell runs it and reports
these. It also catches a quoted commit message containing backticks or ``$(``: the
shell executes that text before git starts, so no git hook can see or stop it.

The guard is deliberately fail-open on input it cannot parse: a pre-execution hook that
crashes would block every command, which is worse than missing one.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass

WRAPPERS = {"sudo", "command", "env", "nohup", "time", "exec", "xargs"}
SHELLS = {"bash", "sh", "zsh", "dash"}
SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "|&"}
_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_MSG_SUBST_RE = re.compile(r"""(?:\s-[a-zA-Z]*m\s*|--message[=\s]\s*)"[^"]*(?:`|\$\()""")


@dataclass(frozen=True)
class Finding:
    rule: str
    detail: str
    command: str

    def __str__(self) -> str:
        return f"[{self.rule}] {self.detail}: {self.command}"


def _strip_heredocs(text: str) -> list:
    """Return command lines with heredoc bodies removed (their text is data, not commands)."""
    out, terminator = [], None
    for line in text.splitlines():
        if terminator is not None:
            if line.strip() == terminator:
                terminator = None
            continue
        out.append(line)
        m = _HEREDOC_RE.search(line)
        if m:
            terminator = m.group(2)
    return out


def _simple_commands(line: str) -> list:
    lex = shlex.shlex(line, posix=True, punctuation_chars=True)
    lex.whitespace_split = True
    lex.commenters = "#"
    words, cmds, skip_next = [], [], False
    for tok in lex:
        if skip_next:  # the target of a redirection is not an argument
            skip_next = False
            continue
        if tok in SEPARATORS:
            if words:
                cmds.append(words)
            words = []
        elif tok and set(tok) <= set("<>&"):
            if words and words[-1].isdigit():
                words.pop()  # '2>' -> drop the fd number too
            skip_next = True
        else:
            words.append(tok)
    if words:
        cmds.append(words)
    return cmds


def _unwrap(words: list) -> list:
    """Drop leading VAR=value words and wrappers such as sudo/env/xargs."""
    i = 0
    while i < len(words) and _ASSIGN_RE.match(words[i]):
        i += 1
    words = words[i:]
    if words and words[0].rsplit("/", 1)[-1] in WRAPPERS:
        for j, w in enumerate(words):
            if w.rsplit("/", 1)[-1] in SHELLS | {"git"}:
                return words[j:]
        return []
    return words


def _short_flags(arg: str) -> str:
    """'-am' -> 'am'; '--all' -> ''."""
    return arg[1:] if arg.startswith("-") and not arg.startswith("--") else ""


def _has_short(args: list, letter: str, stop_at: str = "mFCctSu") -> bool:
    for a in args:
        if a == "--":
            return False
        for ch in _short_flags(a):
            if ch == letter:
                return True
            if ch in stop_at:  # the rest of this word is that option's argument
                break
    return False


def _positionals(args: list) -> list:
    return [a for a in args if not a.startswith("-")]


def _has_long(args: list, *names: str, refuse_values=("false", "no")) -> bool:
    """True if an option before '--' is one of names, or an unambiguous-looking prefix of one.

    git accepts abbreviated long options ('--ha' for '--hard'), so exact matching is not enough.
    """
    for a in args:
        if a == "--":
            return False
        if not a.startswith("--") or len(a) < 4:
            continue
        opt, _, value = a.partition("=")
        if value.lower() in refuse_values:
            continue
        if any(n.startswith(opt) for n in names):
            return True
    return False


DANGEROUS_SUBS = {"stash", "reset", "checkout", "switch", "restore", "clean", "commit", "add", "pull", "rebase", "push"}
KNOWN_BUILTINS = DANGEROUS_SUBS | {
    "status",
    "diff",
    "log",
    "show",
    "fetch",
    "merge",
    "branch",
    "tag",
    "rev-parse",
    "config",
    "remote",
    "ls-files",
    "grep",
    "blame",
    "cherry-pick",
    "revert",
    "worktree",
    "init",
    "clone",
    "apply",
    "am",
    "bisect",
    "describe",
    "mv",
    "rm",
    "notes",
    "submodule",
    "format-patch",
    "shortlog",
    "reflog",
    "rev-list",
    "cat-file",
    "ls-tree",
    "merge-base",
    "for-each-ref",
    "help",
    "version",
    "gc",
}


def _git_rules(sub: str, args: list) -> list:
    """Return (rule, detail) pairs for one git subcommand."""
    hits = []
    pos = _positionals(args)
    if sub == "stash" and (not pos or pos[0] not in ("list", "show")):
        hits.append(("stash", "moves every agent's uncommitted edits out of the tree"))
    elif sub == "reset" and _has_long(args, "--hard", "--merge", "--keep"):
        hits.append(("reset-hard", "overwrites files other agents are editing"))
    elif sub == "checkout":
        creating = _has_short(args, "b") or _has_short(args, "B")
        if "--" in args or (pos and not (creating and len(pos) == 1)):
            hits.append(
                ("checkout", "switches or overwrites the shared tree; read old versions with git show <ref>:<path>")
            )
    elif (
        sub == "switch"
        and pos
        and not (_has_long(args, "--create", "--force-create") or _has_short(args, "c") or _has_short(args, "C"))
    ):
        hits.append(("switch", "switches the shared tree under other agents"))
    elif sub == "restore" and not (
        _has_long(args, "--staged") and not _has_long(args, "--worktree") and not _has_short(args, "W")
    ):
        hits.append(("restore", "discards working-tree edits that may not be yours"))
    elif sub == "clean" and (_has_long(args, "--force") or _has_short(args, "f")):
        hits.append(("clean", "deletes untracked files other agents just created"))
    elif sub == "commit":
        if _has_long(args, "--amend"):
            hits.append(("amend", "rewrites HEAD, which may be another agent's commit"))
        if _has_long(args, "--all") or _has_short(args, "a"):
            hits.append(("commit-all", "sweeps other agents' edits into your commit; use commit -- <paths>"))
    elif sub == "add" and (
        _has_long(args, "--all", "--update")
        or _has_short(args, "A")
        or _has_short(args, "u")
        or any(p in (".", ":/", "*") or p.startswith(":") for p in pos)
    ):
        hits.append(("add-all", "stages other agents' unfinished edits; add named paths"))
    elif sub == "pull" and (_has_long(args, "--rebase") or _has_short(args, "r")):
        hits.append(("pull-rebase", "rewrites local commits under other agents; fetch then merge"))
    elif sub == "rebase":
        hits.append(("rebase", "rewrites the shared tree and history"))
    elif sub == "push" and (
        _has_long(args, "--force", "--force-with-lease", "--force-if-includes", "--mirror", "--delete")
        or _has_short(args, "f")
        or _has_short(args, "d")
        or any(p.startswith("+") or p.startswith(":") for p in pos)
    ):
        hits.append(("force-push", "replaces or deletes remote history others have built on"))
    return hits


def _git_split(words: list):
    """('git', globals..., sub, args...) -> (sub, args, workdir, inline_aliases) or None."""
    i, workdir, aliases = 1, None, []
    while i < len(words):
        w = words[i]
        if w in ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"):
            val = words[i + 1] if i + 1 < len(words) else ""
            if w == "-C":
                workdir = val
            if w in ("-c", "--config-env") and val.lower().startswith("alias."):
                aliases.append(val)
            i += 2
            continue
        if w.startswith("-"):
            i += 1
            continue
        return w, words[i + 1 :], workdir, aliases
    return None


def _configured_alias(sub: str, workdir) -> str:
    """Expansion of a configured git alias, or '' (also '' when git cannot be asked: fail-open)."""
    try:
        out = subprocess.run(
            ["git", "config", "--get", f"alias.{sub}"],
            cwd=workdir or None,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _check_git(words: list, depth: int) -> list:
    split = _git_split(words)
    if not split:
        return []
    sub, args, workdir, aliases = split
    text = " ".join(words)
    if aliases:
        return [Finding("alias-override", "an inline alias (-c alias.*) hides what git will run", text)]
    if sub not in KNOWN_BUILTINS and depth < 3:
        expansion = _configured_alias(sub, workdir)
        if expansion.startswith("!"):
            return check_command(expansion[1:], depth + 1)
        if expansion:
            return _check_git(["git"] + shlex.split(expansion) + args, depth + 1)
    return [Finding(r, d, text) for r, d in _git_rules(sub, args)]


_DANGER_WORD_RE = re.compile(r"\b(" + "|".join(sorted(DANGEROUS_SUBS)) + r")\b")


def check_command(command: str, _depth: int = 0) -> list:
    """Return the list of Findings for a shell command line (empty list == allowed)."""
    findings = []
    for line in _strip_heredocs(command):
        try:
            cmds = _simple_commands(line)
        except ValueError:
            continue  # unbalanced quotes etc.: the shell will reject it anyway (fail-open)
        for raw in cmds:
            words = _unwrap(raw)
            if not words:
                continue
            prog = words[0].rsplit("/", 1)[-1]
            if (prog in SHELLS and "-c" in words) or prog == "eval":
                if _depth < 3:
                    inner = words[words.index("-c") + 1 :][:1] if prog != "eval" else [" ".join(words[1:])]
                    if inner:
                        findings += check_command(inner[0], _depth + 1)
                continue
            if ("$" in words[0] or "`" in words[0]) and _DANGER_WORD_RE.search(" ".join(words)):
                findings.append(
                    Finding(
                        "dynamic-command", "the command name is computed at run time; spell out git", " ".join(words)
                    )
                )
                continue
            if prog == "git":
                findings += _check_git(words, _depth)
        if re.search(r"\bgit\b.*\bcommit\b", line) and _MSG_SUBST_RE.search(line):
            findings.append(
                Finding(
                    "message-substitution",
                    "backticks or $( ) inside a double-quoted -m run before git starts; use -F <file>",
                    line.strip(),
                )
            )
    return findings


# ---------------------------------------------------------------- CLI / hook
def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="agentkit-worktree-guard", description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check a command line; exit 0 ok / 1 findings")
    c.add_argument("command")
    sub.add_parser("hook", help="PreToolUse hook mode: Bash tool call as JSON on stdin")
    args = p.parse_args(argv)
    if args.cmd == "check":
        found = check_command(args.command)
        for f in found:
            print(f)
        return 1 if found else 0
    try:
        call = json.load(sys.stdin)
        command = call["tool_input"]["command"] if call.get("tool_name") == "Bash" else ""
    except (ValueError, KeyError, TypeError, AttributeError):
        print("agentkit worktree guard: unreadable hook input; allowing", file=sys.stderr)
        return 0
    found = check_command(command or "")
    if found:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": "shared working tree: " + "; ".join(str(f) for f in found),
                    }
                }
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
