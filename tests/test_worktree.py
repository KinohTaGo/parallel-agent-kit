import io
import json

import pytest

from agentkit import worktree
from agentkit.worktree import check_command


def rules(cmd):
    return [f.rule for f in check_command(cmd)]


@pytest.mark.parametrize(
    "cmd,rule",
    [
        ("git stash", "stash"),
        ("git stash push -m wip", "stash"),
        ("git reset --hard origin/main", "reset-hard"),
        ("git checkout main", "checkout"),
        ("git checkout -- src/app.py", "checkout"),
        ("git checkout HEAD~1 -- .", "checkout"),
        ("git switch main", "switch"),
        ("git restore src/app.py", "restore"),
        ("git restore --staged --worktree a.txt", "restore"),
        ("git clean -fd", "clean"),
        ("git commit --amend --no-edit", "amend"),
        ("git commit -a -m wip", "commit-all"),
        ("git commit -am wip", "commit-all"),
        ("git add -A", "add-all"),
        ("git add .", "add-all"),
        ("git add -u", "add-all"),
        ("git pull --rebase origin main", "pull-rebase"),
        ("git rebase origin/main", "rebase"),
        ("git push --force origin main", "force-push"),
        ("git push -f", "force-push"),
        ("git push origin +main", "force-push"),
        ("git push --force-with-lease", "force-push"),
    ],
)
def test_dangerous_commands_are_reported(cmd, rule):
    assert rule in rules(cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "git status --short",
        "git diff -- src/app.py",
        "git add src/app.py tests/test_app.py",
        "git add -p src/app.py",
        'git commit -m "add a feature" -- src/app.py',
        "git commit -F msg.txt -- src/app.py",
        "git stash list",
        "git stash show -p",
        "git checkout -b feature/x",
        "git switch -c feature/y",
        "git restore --staged src/app.py",
        "git reset --soft HEAD",
        "git pull --ff-only",
        "git push origin HEAD:main",
        "git show origin/main:src/app.py",
        "git merge --no-edit origin/main",
        "git log --oneline | head -5",
        "echo git stash",
        "grep -n 'git stash' notes.md",
    ],
)
def test_safe_commands_pass(cmd):
    assert rules(cmd) == []


def test_global_options_before_the_subcommand():
    assert rules("git -C repo stash") == ["stash"]
    assert rules("git -c core.pager=cat reset --hard") == ["reset-hard"]


def test_compound_and_wrapped_commands():
    assert rules("cd repo && git add -A && git commit -m x") == ["add-all"]
    assert rules("FOO=1 git stash") == ["stash"]
    assert rules("sudo -u build git clean -f") == ["clean"]
    assert rules("bash -c 'git reset --hard'") == ["reset-hard"]
    assert rules("echo $(git stash)") == ["stash"]


def test_redirections_are_not_arguments():
    assert rules("git checkout -b topic 2>/dev/null") == []
    assert rules("git stash > /dev/null") == ["stash"]


def test_heredoc_body_is_data_not_commands():
    cmd = "cat > notes.md <<'EOF'\nnever run git stash here\ngit reset --hard is banned\nEOF\ngit status"
    assert rules(cmd) == []


def test_backticks_in_a_double_quoted_message_are_caught():
    assert "message-substitution" in rules('git commit -m "fix `make` target" -- a.py')
    assert "message-substitution" in rules('git commit -am "now $(date)"')
    # single quotes are not expanded by the shell
    assert rules("git commit -m 'fix `make` target' -- a.py") == []


def test_unparseable_line_fails_open():
    assert rules('git commit -m "unterminated') == []


def test_hook_mode_denies_bash_and_ignores_other_tools(monkeypatch, capsys):
    call = {"tool_name": "Bash", "tool_input": {"command": "git stash"}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call)))
    assert worktree.main(["hook"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "stash" in out["hookSpecificOutput"]["permissionDecisionReason"]

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"tool_name": "Edit", "tool_input": {}})))
    assert worktree.main(["hook"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert worktree.main(["hook"]) == 0
    assert capsys.readouterr().out == ""


def test_check_exit_codes(capsys):
    assert worktree.main(["check", "git status"]) == 0
    assert worktree.main(["check", "git add -A"]) == 1
    assert "add-all" in capsys.readouterr().out


@pytest.mark.parametrize(
    "cmd,rule",
    [
        ("git reset --ha", "reset-hard"),
        ("git commit --amen --no-edit", "amend"),
        ("git push --forc", "force-push"),
        ("git push --force-w", "force-push"),
        ("git push origin :main", "force-push"),
        ("git add --al", "add-all"),
        ("git add ':(glob)**'", "add-all"),
        ("git pull --reb", "pull-rebase"),
        ("git -c alias.x=stash x", "alias-override"),
        ('eval "git stash"', "stash"),
        ("/usr/bin/env git stash", "stash"),
        ("G=git; $G stash", "dynamic-command"),
        ("git${IFS}stash", "dynamic-command"),
    ],
)
def test_known_bypass_shapes_are_reported(cmd, rule):
    assert rule in rules(cmd)


def test_pull_rebase_false_is_a_merge():
    assert rules("git pull --rebase=false") == []


def test_configured_aliases_are_expanded(tmp_path):
    import subprocess

    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "alias.st", "stash"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "alias.nuke", "!git reset --hard"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "alias.s", "status"], check=True)
    assert rules(f"git -C {repo} st") == ["stash"]
    assert rules(f"git -C {repo} nuke") == ["reset-hard"]
    assert rules(f"git -C {repo} s") == []


@pytest.mark.parametrize(
    "cmd,rule",
    [
        ("nice -n 5 git stash", "stash"),
        ("timeout 10 git reset --hard", "reset-hard"),
        ("stdbuf -oL git clean -f", "clean"),
        ("my-runner --quiet git push --force", "force-push"),
        ("git \\\n  stash", "stash"),
        ("{ git stash; }", "stash"),
        ("if true; then git stash; fi", "stash"),
        ("! git reset --hard", "reset-hard"),
        ('if eval "git stash"; then :; fi', "stash"),
        ("! bash -c 'git clean -f'", "clean"),
    ],
)
def test_wrappers_keywords_and_continuations(cmd, rule):
    assert rule in rules(cmd)


@pytest.mark.parametrize("cmd", ["cd git && ls", "printf '%s\\n' git stash", "test -d git"])
def test_git_as_data_is_not_a_command(cmd):
    assert rules(cmd) == []
