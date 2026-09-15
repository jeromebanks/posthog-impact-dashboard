# Coding agent session transcript

`claude-code-session-transcript.jsonl` is the raw session log (Claude Code's internal
JSONL format — one JSON event per line: user/assistant messages, tool calls and their
results, in chronological order) for the single continuous session that built this
entire project, from initial planning through the final bug-fix pass.

It is a straight copy of the file Claude Code itself stores locally for this session —
nothing has been edited, redacted, or reordered, beyond a pre-commit scan confirming no
API keys or tokens are present in it.
