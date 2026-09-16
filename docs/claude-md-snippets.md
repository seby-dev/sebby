# Shared CLAUDE.md snippets

Copy whichever sections below apply into your project's own `CLAUDE.md`
rather than writing them from scratch — these are the conventions found
duplicated verbatim (or near-verbatim) across `staff2solfa`,
`varrick-chorus-uk`, and `pmp-project`'s CLAUDE.md files.

## Git conventions

```markdown
## Git conventions

- Branch names: `feat/<topic>`, `fix/<topic>`, `chore/<topic>`.
- Conventional commits: `feat:`, `fix:`, `chore:`, `docs:`, `refactor:`,
  `test:`.
- Never force-push. Never skip the pre-push hook.
```

## Quality standards

```markdown
## Quality standards

- Test-driven development: write the failing test before the
  implementation.
- No function over 40 lines; no file over 300 lines, for new code.
- Docstrings explain why, not what — only when that's not evident from the
  name and the code itself.
```

## Advisor & plan review

For complex or risky work, spawn a fresh advisor subagent — pointed at the
specific existing files it needs, not the whole codebase — to review a spec
or plan before implementation starts. The advisor reviews and comments; it
never implements. Skip this for small, well-scoped changes.

```markdown
## Advisor & Plan Review

For complex or risky work, before implementing:
1. Spawn a fresh subagent as an *advisor, not an implementer*, pointed at
   the specific existing files relevant to the change.
2. Ask it to review the spec (before writing the plan) and the plan
   (before implementation starts) for gaps, risks, and alternative
   approaches.
3. Skip this step for small, well-scoped changes — reserve it for work
   with real design ambiguity or blast radius.
```

## Full feature workflow

If a global `~/.claude/CLAUDE.md` already documents a brainstorm → spec →
plan → implement pipeline (e.g. via `superpowers:brainstorming` /
`superpowers:writing-plans` / `superpowers:subagent-driven-development`),
reference it instead of restating it:

```markdown
## Development workflow

Follow the brainstorm → spec → plan → implement pipeline documented in
`~/.claude/CLAUDE.md`'s Full Feature Workflow. Specs and plans go in
`docs/superpowers/specs/` and `docs/superpowers/plans/` respectively.
```
