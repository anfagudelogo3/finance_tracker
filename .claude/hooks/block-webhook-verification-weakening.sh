#!/usr/bin/env bash
# PreToolUse hook (Edit|Write matcher, filtered to file paths containing
# "webhook.py"): a TRIPWIRE against weakening Twilio webhook signature
# verification. Per CLAUDE.md, webhook.verify_signature is
# security-critical and must never be bypassed or disabled, even to make
# a failing test pass.
#
# IMPORTANT — this is pattern matching, not a semantic guarantee. It
# catches common, obvious weakening patterns: a stubbed "return True", a
# commented-out call site, or bypass phrasing in comments/code. It does
# NOT understand what the code actually does. A sophisticated rewrite —
# an equivalent-looking function that silently always validates, logic
# split across multiple edits so no single diff trips a pattern, a
# renamed helper, etc. — can slip straight past this. Treat it as a
# speed bump that catches careless or lazy weakening, not a substitute
# for a human reviewing any change to this file.
set -euo pipefail

input="$(cat)"
file_path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // empty')"

if [[ -z "$file_path" || "$file_path" != *webhook.py* ]]; then
    exit 0
fi

tool_name="$(printf '%s' "$input" | jq -r '.tool_name // empty')"
case "$tool_name" in
    Write)
        new_content="$(printf '%s' "$input" | jq -r '.tool_input.content // empty')"
        ;;
    Edit)
        new_content="$(printf '%s' "$input" | jq -r '.tool_input.new_string // empty')"
        ;;
    *)
        exit 0
        ;;
esac

if [[ -z "$new_content" ]]; then
    exit 0
fi

reason=""

# 1. A hardcoded "return True" appearing near a verify_signature reference
#    (a few lines either side — same function, not just same file).
context="$(printf '%s\n' "$new_content" | grep -inB3 -A3 "verify_signature" 2>/dev/null | grep -v '^--$' || true)"
if [[ -n "$context" ]] && printf '%s' "$context" | grep -qiE 'return[[:space:]]+true'; then
    reason="a hardcoded 'return True' near a verify_signature reference"
fi

# 2. The verify_signature call/definition site commented out.
if [[ -z "$reason" ]] && printf '%s\n' "$new_content" | grep -qiE '^[[:space:]]*#.*verify_signature'; then
    reason="a commented-out line referencing verify_signature"
fi

# 3. Common bypass phrasing in comments or code.
if [[ -z "$reason" ]] && printf '%s\n' "$new_content" | grep -qiE 'skip verification|disable verification|bypass signature'; then
    reason="bypass phrasing ('skip verification' / 'disable verification' / 'bypass signature')"
fi

if [[ -n "$reason" ]]; then
    echo "Blocked: this change to '$file_path' looks like it may weaken webhook signature verification ($reason). This is security-critical per CLAUDE.md — webhook.verify_signature must never be bypassed or disabled, even to make a failing test pass. If this is intentional, ask the user to confirm explicitly before proceeding." >&2
    exit 2
fi

exit 0
