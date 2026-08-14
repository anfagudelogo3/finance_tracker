#!/usr/bin/env bash
# PreToolUse hook (Bash matcher): blocks any Bash command that mentions
# "databricks" (case-insensitive). This project does not use Databricks —
# exit 2 tells Claude Code to block the tool call and shows this message.
set -euo pipefail

input="$(cat)"
command="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

if printf '%s' "$command" | grep -qi "databricks"; then
    echo "Blocked: this command references 'databricks', which is not used in this project. If you actually need Databricks, ask the user first." >&2
    exit 2
fi

exit 0
