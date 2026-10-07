# Changelog

## v1.1.0 — 2026-10-06

**Short names:** Belki tasks gain an optional `short::` field to override auto-derived handles. Effective handle = `short::` else middle segment of `id::` (e.g. `task-endcard1-4h7m2q` → `endcard1`); ad-hoc tasks have none. Commands now accept handles as exact targets (`push endcard1 because ...`, `done [endcard1]`), winning before title/project word matching. Sync warns of duplicate handles.

**Nag embeds with buttons:** Overdue nag DMs restructured as a summary header plus embeds (max 10 tasks, remainder in text line), each with priority/due/project/pressure and persistent `Done` / `Keep` / `Push` buttons (registered via `TaskActionButton`, survives restarts). Buttons and text commands share handlers; only `DISCORD_USER_ID` may press.

**Action refactor:** Consolidated `do_complete`, `do_keep`, `do_push` handlers serve both button and text-command flows.

**Infrastructure:** New `short_names.py` module (pure helpers), `tests/` pytest suite, `requirements-dev.txt`.

## v1.0.0 — 2026-08-21

Feature-complete. Future work is patch releases (v1.0.x) for bug fixes only — no new features planned.

Covers: Discord-driven deadline scheduling on Google Calendar, bidirectional Belki task import, project-deadline-pressure weekly allocation, overdue nag loop with quiet hours and `keep` suppression, Belki `priority::`-driven ranking, and Gemini model fallback across intent parsing and the scheduler chat.
