#!/bin/bash
# Blocks the coder from editing test files.
# Runs as a PreToolUse hook on Edit|Write: reads the hook JSON on stdin,
# extracts the target path, and exits 2 (block) if it looks like a test file.
# Requires: jq
#
# Tune the regex below to match YOUR project's test layout.

# Ensure jq is on PATH (winget installs may not be visible in Git Bash)
for p in "$LOCALAPPDATA/Microsoft/WinGet/Packages"/jqlang.jq_*/; do
  [ -d "$p" ] && export PATH="$PATH:$p"
done

INPUT=$(cat)
FILE=$(echo "$INPUT" | jq -r '.tool_input.file_path // empty')

# No file path (e.g. a Bash write) -> allow; nothing to guard.
if [ -z "$FILE" ]; then
  exit 0
fi

# Common test conventions across languages:
#   tests/  test/  __tests__/  spec/      (test directories)
#   *.test.*  *.spec.*  *_test.*           (suffix conventions)
#   test_*.py                              (pytest prefix)
#   *Test.java *Tests.cs                   (xUnit-style class files)
if echo "$FILE" | grep -iqE '(^|/)(tests?|__tests__|spec)/|\.(test|spec)\.[a-z0-9]+$|_test\.[a-z0-9]+$|(^|/)test_[^/]+\.py$|Tests?\.[a-z]+$'; then
  echo "Blocked: the coder must not modify test files (matched: $FILE)." >&2
  echo "Tests are owned by test-writer; raise wrong tests with the orchestrator." >&2
  exit 2
fi

exit 0
