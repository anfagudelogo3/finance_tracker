#!/usr/bin/env bash
# PreToolUse hook (Bash matcher): guards against pushing under the wrong
# git account. Only inspects commands containing "git push" or
# "git remote" (pushing, or remote reconfiguration that could point a
# future push at the wrong identity). Blocks if the command references
# the work SSH alias (github-turner — this repo must use the personal
# "github.com" host), or if the live git commit email isn't the personal
# account — either means the push would run under the wrong identity.
set -euo pipefail

input="$(cat)"
command="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

if [[ -z "$command" ]]; then
    exit 0
fi

if ! printf '%s' "$command" | grep -qE "git push|git remote"; then
    exit 0
fi

if printf '%s' "$command" | grep -q "github-turner"; then
    echo "Blocked (wrong SSH host): command references 'github-turner', the work SSH alias. This repo must push under the personal 'github.com' host — fix the remote URL/SSH config before retrying." >&2
    exit 2
fi

email="$(git config user.email 2>/dev/null || true)"
if [[ "$email" != "anfagudelogo@gmail.com" ]]; then
    echo "Blocked (wrong commit email): git config user.email is '${email:-<unset>}', expected 'anfagudelogo@gmail.com'. Commit authorship has drifted from the correct account — run 'git config user.email anfagudelogo@gmail.com' before pushing." >&2
    exit 2
fi

exit 0
