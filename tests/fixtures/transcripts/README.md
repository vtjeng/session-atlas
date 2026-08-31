# Synthetic transcript fixtures

These fixtures follow the directory and JSONL layouts used by local Claude Code
and Codex CLI transcripts. They contain invented prompts, paths, identifiers,
token counts, and repository metadata. Documentation screenshots and parser
tests use them instead of files from a developer's home directory.

The fixed values exercise these parser paths:

- `/home/demo/src/example-project` appears in both transcript formats so the
  fixture site must merge their sessions into one project.
- `/home/demo/src/docs-site` appears only in Claude Code so the project index
  includes both a mixed-source project and a single-source project.
- March 12–15 timestamps keep the documentation images within one compact four-day range.
- Supported model identifiers and invented agentic-session token counts produce
  a visible multi-dollar cost estimate without reproducing local usage totals.
- `main`, `https://example.com/`, and repeated UUID digits are recognizable
  synthetic metadata rather than copies of local branches, remotes, or session
  identifiers.
