#!/usr/bin/env bats
# Tests for bin/safe-commit in a throw-away repository.

setup() {
  SAFE_COMMIT="$BATS_TEST_DIRNAME/../../bin/safe-commit"
  REPO="$BATS_TEST_TMPDIR/repo"
  [ -n "$REPO" ] || return 1
  mkdir -p "$REPO"
  cd "$REPO" || return 1
  git init -q
  git config user.email test@example.invalid
  git config user.name test
  git config commit.gpgsign false
  printf 'a\n' > mine.txt
  printf 'b\n' > theirs.txt
  git add mine.txt theirs.txt
  git commit -q -m base
}

@test "commits only the named file and leaves another agent's staged change staged" {
  printf 'a2\n' > mine.txt
  printf 'b2\n' > theirs.txt
  git add theirs.txt                     # someone else staged this
  run "$SAFE_COMMIT" -m "mine" -- mine.txt
  [ "$status" -eq 0 ]
  [ "$(git show --name-only --format= HEAD)" = "mine.txt" ]
  [ "$(git diff --cached --name-only)" = "theirs.txt" ]
}

@test "adds and commits a new untracked file" {
  printf 'new\n' > new.txt
  run "$SAFE_COMMIT" -m "new" -- new.txt
  [ "$status" -eq 0 ]
  [ "$(git show --name-only --format= HEAD)" = "new.txt" ]
}

@test "works from a subdirectory" {
  mkdir -p sub && printf 's\n' > sub/s.txt
  cd sub
  run "$SAFE_COMMIT" -m "sub" -- s.txt
  [ "$status" -eq 0 ]
  [ "$(git show --name-only --format= HEAD)" = "sub/s.txt" ]
}

@test "reports exit 3 when a hook slips another file into the commit" {
  printf 'b3\n' > theirs.txt
  printf '#!/bin/sh\ngit add theirs.txt\n' > .git/hooks/pre-commit
  chmod +x .git/hooks/pre-commit
  printf 'a3\n' > mine.txt
  run "$SAFE_COMMIT" -m "mine" -- mine.txt
  [ "$status" -eq 3 ]
  [[ "$output" == *"did not name: theirs.txt"* ]]
  [[ "$output" == *"nothing was rewritten"* ]]
}

@test "refuses directories, missing paths, absolute paths and --amend" {
  mkdir d
  run "$SAFE_COMMIT" -m x -- d
  [ "$status" -eq 2 ]
  run "$SAFE_COMMIT" -m x -- nope.txt
  [ "$status" -eq 2 ]
  run "$SAFE_COMMIT" -m x -- /etc/hosts
  [ "$status" -eq 2 ]
  run "$SAFE_COMMIT" --amend -m x -- mine.txt
  [ "$status" -eq 2 ]
}

@test "requires a message and at least one path" {
  run "$SAFE_COMMIT" -- mine.txt
  [ "$status" -eq 2 ]
  run "$SAFE_COMMIT" -m x --
  [ "$status" -eq 2 ]
}

@test "treats paths literally: globs and pathspec magic do not widen the commit" {
  printf 'a4\n' > mine.txt
  printf 'b4\n' > theirs.txt
  before=$(git rev-parse HEAD)
  run "$SAFE_COMMIT" -m x -- '*.txt'
  [ "$status" -eq 2 ]
  run "$SAFE_COMMIT" -m x -- ':(glob)*.txt'
  [ "$status" -eq 2 ]
  [ "$(git rev-parse HEAD)" = "$before" ]
}
