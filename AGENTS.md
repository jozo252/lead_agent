# AGENTS.md

## Working style

- Before modifying code, inspect the relevant files and understand the existing architecture.
- Prefer the simplest solution that solves the problem.
- Do not introduce new libraries unless there is a clear benefit.
- Do not rewrite working parts of the application unnecessarily.
- Preserve existing behavior unless the task explicitly requires changing it.

## Development

- Keep functions small and understandable.
- Follow the existing project structure and naming conventions.
- Prefer existing utilities over creating duplicates.
- Never hardcode API keys, passwords, tokens or secrets.
- Do not modify production data or production configuration without explicit permission.

## Database

- Manage schema changes only through Flask-Migrate revisions in `migrations/`.
- Never restore `db.create_all()` or runtime `ALTER TABLE` helpers in `app.py`.
- Inspect existing revisions before running `flask db migrate`.
- Test upgrades against a database copy and review generated upgrade/downgrade code.
- Avoid destructive migrations unless explicitly requested.

## Verification

Before considering a task complete:
- Run relevant tests.
- Run the application if possible.
- Check for obvious runtime errors.
- Review the git diff.
- Verify that the requested behavior actually works.

## Bugs

When fixing a bug:
1. Identify the root cause.
2. Explain the cause briefly.
3. Implement the smallest reasonable fix.
4. Verify the bug no longer reproduces.
5. Check that the fix did not introduce regressions.

## Large tasks

For large or architectural changes:
- Make a plan before editing files.
- Identify affected components.
- Prefer incremental implementation.
