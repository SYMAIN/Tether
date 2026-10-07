# Task Short Names + Nag Buttons Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every task gets a short handle (`[endcard1]`) shown in all Discord output and accepted as an exact reference, and overdue nags become one embed per task with persistent Done / Keep / Push buttons.

**Architecture:** Pure logic (short-name derivation, query normalisation, collision detection, button custom_id format) lives in a new dependency-free module `short_names.py` so it can be unit-tested with pytest locally. `belki_import.py` parses a new optional `short::` card field and stamps it into `[TETHER_META]` as `short=`. `main.py` derives the effective short name from meta at display time (`short=` override, else middle segment of `belki_id=`), uses it in `task_matches`, and gets a small `actions` refactor so text commands and buttons share one implementation of complete / keep / push.

**Tech Stack:** Python 3.11, discord.py 2.x (`discord.ui.DynamicItem` for persistent per-task buttons, `discord.ui.Modal`), Google Calendar API, pytest (new, dev-only).

## Global Constraints

- Do NOT rename Belki ids. Calendar events reconcile on `belki_id`; `depends_on::` references ids.
- Effective short name = `short::` if present, else middle segment of the Belki id (`task-endcard1-4h7m2q` → `endcard1`). Ad-hoc (non-Belki) events with neither have no short name; display them without a prefix.
- Short names are lowercase; matching is case-insensitive, strips whitespace, tolerates surrounding `[ ]`.
- Exact short-name match wins first; otherwise the existing title/project word match is unchanged.
- Text commands must keep working identically.
- Only `DISCORD_USER_ID` may press buttons.
- Buttons must survive process restarts (Tether restarts often).
- Cap nag embeds at 10, ordered by `priority_score` (highest first); the rest go in one plain text line of `[short] name` entries.
- Do not touch `midnight_nag_persist` reset or `on_ready` state-reset behaviour (Simon decided not to change them 2026-10-06).
- Match existing code style and comment density (comments explain *why*, cite the decision/incident).
- Local machine has no runtime deps for `main.py` (no apscheduler). Only `short_names.py` and `belki_import.py` are pytest-tested; `main.py` changes are verified with `python -m py_compile main.py` plus the reviewer reading the diff.
- Don't push, deploy, or merge.

---

## File Structure

- Create `short_names.py` — pure helpers: `derive_short`, `normalize_query`, `find_collisions`, `button_custom_id`, `parse_custom_id`. No imports beyond `re`.
- Create `tests/test_short_names.py`, `tests/test_belki_short.py`, `tests/conftest.py`.
- Create `requirements-dev.txt` (`pytest`).
- Modify `belki_import.py` — parse `short::`, meta regex, reconcile/insert pass-through, collision notes.
- Modify `main.py` — `build_meta` `short=`, `_insert_deadline` / `update_deadline_content` `short` kwarg, `event_short()` / `task_label()`, `task_matches`, display prefixes, intent prompt line, action refactor, nag embeds + buttons.
- Modify `test_jobs.py` — `preview_insert` accepts `short=None`.
- Modify `CLAUDE.md`, `CHANGELOG.md`.

---

### Task 1: `short_names.py` pure helpers + pytest scaffold

**Files:**
- Create: `short_names.py`, `tests/conftest.py`, `tests/test_short_names.py`, `requirements-dev.txt`

**Interfaces:**
- Produces:
  - `derive_short(belki_id: str | None, override: str | None) -> str | None`
  - `normalize_query(q: str) -> str`
  - `find_collisions(pairs: list[tuple[str, str]]) -> dict[str, list[str]]` — input `(short, task_name)`, output `{short: [names...]}` only for shorts used ≥2 times
  - `button_custom_id(action: str, event_id: str) -> str` → `"tether:<action>:<event_id>"`
  - `CUSTOM_ID_RE: re.Pattern` with groups `action`, `eid`; `ACTIONS = ("done", "keep", "push")`
  - `parse_custom_id(cid: str) -> tuple[str, str] | None`

- [ ] **Step 1: Write the failing tests**

`requirements-dev.txt`:
```
pytest
```

`tests/conftest.py`:
```python
import os
import sys

# Tests import repo-root modules (short_names, belki_import) directly.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

`tests/test_short_names.py`:
```python
from short_names import (
    button_custom_id,
    derive_short,
    find_collisions,
    normalize_query,
    parse_custom_id,
)


def test_derive_from_id_middle_segment():
    assert derive_short("task-endcard1-4h7m2q", None) == "endcard1"


def test_override_wins_and_is_lowercased():
    assert derive_short("task-1hs4mvme-qxfpw0", " Replay ") == "replay"


def test_no_id_no_override_is_none():
    assert derive_short(None, None) is None
    assert derive_short("", "") is None


def test_id_without_three_segments_falls_back_to_whole_id_minus_prefix():
    assert derive_short("task-legacy", None) == "legacy"
    assert derive_short("weird", None) == "weird"


def test_normalize_query_strips_brackets_space_case():
    assert normalize_query("  [EndCard1] ") == "endcard1"
    assert normalize_query("endcard1") == "endcard1"


def test_find_collisions_only_duplicates():
    pairs = [("a", "Task A"), ("b", "Task B"), ("a", "Task A2")]
    assert find_collisions(pairs) == {"a": ["Task A", "Task A2"]}


def test_custom_id_round_trip():
    cid = button_custom_id("push", "abc123def")
    assert cid == "tether:push:abc123def"
    assert parse_custom_id(cid) == ("push", "abc123def")


def test_parse_custom_id_rejects_unknown():
    assert parse_custom_id("tether:delete:abc") is None
    assert parse_custom_id("other:done:abc") is None
    assert parse_custom_id("tether:done:") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pip install -r requirements-dev.txt; python -m pytest tests/test_short_names.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'short_names'`

- [ ] **Step 3: Implement `short_names.py`**

```python
"""Short task handles and Discord button ids — pure helpers, no I/O.

Kept out of main.py so they're unit-testable without Discord/Google/apscheduler.
A short name is the handle shown as `[endcard1]` in every DM and accepted as an
exact task reference. Decided 2026-10-06 (reversing the earlier alias:: rejection):
derived from the Belki id by default so most cards need nothing, with an optional
`short::` override for cards whose id slug is random.
"""

import re

ACTIONS = ("done", "keep", "push")
CUSTOM_ID_RE = re.compile(r"^tether:(?P<action>done|keep|push):(?P<eid>[A-Za-z0-9_-]+)$")


def derive_short(belki_id: str | None, override: str | None) -> str | None:
    if override and override.strip():
        return override.strip().lower()
    if not belki_id:
        return None
    parts = belki_id.split("-")
    if len(parts) >= 3:
        return parts[1].lower()
    # Legacy/odd ids: drop a leading "task-" and use the rest.
    return (parts[1] if len(parts) == 2 and parts[0] == "task" else belki_id).lower()


def normalize_query(q: str) -> str:
    return (q or "").strip().strip("[]").strip().lower()


def find_collisions(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for short, name in pairs:
        if short:
            seen.setdefault(short, []).append(name)
    return {s: names for s, names in seen.items() if len(names) > 1}


def button_custom_id(action: str, event_id: str) -> str:
    return f"tether:{action}:{event_id}"


def parse_custom_id(cid: str) -> tuple[str, str] | None:
    m = CUSTOM_ID_RE.match(cid or "")
    return (m.group("action"), m.group("eid")) if m else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_short_names.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add short_names.py tests/ requirements-dev.txt
git commit -m "feat: short_names helpers for task handles and button ids"
```

---

### Task 2: Belki `short::` field → meta, reconcile, collision notes

**Files:**
- Modify: `belki_import.py:132-134` (known keys), `:135-138` (meta regexes), `:177-188` (task dict), `:212-224` (field parse), `:768` (notes), `:1060-1084` (reconcile), `:1205-1216` (insert)
- Modify: `main.py:94-128` (`build_meta`), `main.py:184-194` (`_insert_deadline`), `main.py:253-297` (`update_deadline_content`)
- Modify: `test_jobs.py` `preview_insert`
- Test: `tests/test_belki_short.py`

**Interfaces:**
- Consumes: `short_names.derive_short`, `short_names.find_collisions`
- Produces: task dicts carry `"short": str | None` (the explicit override only); meta line `short=<slug>` written when an override exists; `_insert_deadline(..., short=None)`, `update_deadline_content(..., short=None)`; insert/update callbacks passed `short=t["short"]`; sync notes contain `short name "<s>" shared by: <names>`.

- [ ] **Step 1: Write the failing tests**

`tests/test_belki_short.py`:
```python
import belki_import

CARD = """- [ ] End card thing
  id:: task-endcard1-4h7m2q
  project:: Tiffy's Classroom
  priority:: P4
- [ ] Replay probe
  id:: task-1hs4mvme-qxfpw0
  short:: replay
  project:: ACC Race Engineer
- [ ] Another end card
  id:: task-endcard1-zzzzzz
  project:: Tiffy's Classroom
"""


def _parse(tmp_path):
    p = tmp_path / "2026-10.md"
    p.write_text(CARD, encoding="utf-8")
    return belki_import.parse_data_file(str(p))


def test_short_field_parsed_and_not_flagged_unknown(tmp_path):
    tasks, _skipped, notes = _parse(tmp_path)
    by_name = {t["name"]: t for t in tasks}
    assert by_name["Replay probe"]["short"] == "replay"
    assert by_name["End card thing"]["short"] is None
    assert not any("short" in n and "unknown" in n.lower() for n in notes)


def test_short_collision_notes(tmp_path):
    tasks, _s, _n = _parse(tmp_path)
    notes = belki_import.short_name_collision_notes(tasks)
    assert len(notes) == 1
    assert '"endcard1"' in notes[0]
    assert "End card thing" in notes[0] and "Another end card" in notes[0]


def test_collision_ignores_done_tasks(tmp_path):
    tasks, _s, _n = _parse(tmp_path)
    tasks[2]["done"] = True
    assert belki_import.short_name_collision_notes(tasks) == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_belki_short.py -v`
Expected: FAIL — `KeyError: 'short'` / `AttributeError: ... short_name_collision_notes`

- [ ] **Step 3: Implement in `belki_import.py`**

1. Add `"short"` to `_KNOWN_TASK_KEYS`.
2. After `_META_PRIORITY_RE` add:
```python
_META_SHORT_RE = re.compile(r"^short=(.+)$", re.M)
```
3. In the task dict literal add `"short": None,` after `"status": None,`.
4. In the field branch, after the `status` elif:
```python
            elif key == "short":
                current["short"] = val.strip().lower() or None
```
5. Add, next to `annotate_dependencies`:
```python
def short_name_collision_notes(tasks: list[dict]) -> list[str]:
    """Flags open tasks sharing an effective short name.

    Two cards answering to `[endcard1]` make the exact-match lookup ambiguous,
    which is exactly what short names exist to prevent — surface it on sync so
    the fix (a `short::` on one of them) happens at the source.
    """
    pairs = [
        (derive_short(t["id"], t.get("short")), t["name"])
        for t in tasks
        if not t["done"]
    ]
    return [
        f'short name "{s}" shared by: ' + ", ".join(n[:60] for n in names)
        for s, names in find_collisions(pairs).items()
    ]
```
   with `from short_names import derive_short, find_collisions` at the top imports.
6. At line 768: `notes = notes + annotate_dependencies(tasks) + short_name_collision_notes(tasks)`
7. In reconcile (after the `cur_priority` lines):
```python
                cur_short_match = _META_SHORT_RE.search(cur_desc)
                cur_short = cur_short_match.group(1).strip() if cur_short_match else None
                short_changed = (t["short"] or None) != cur_short
```
   add `or short_changed` to the `if not (...)` condition, and pass `short=t["short"]` to `update_deadline(...)`.
8. In the insert call pass `short=t["short"],`.

- [ ] **Step 4: Implement in `main.py` and `test_jobs.py`**

`build_meta`: add parameter `short="",` after `priority=""`, and after the priority block:
```python
    # Optional Belki short:: override for the [handle] shown in DMs. Absent →
    # the handle is derived from belki_id at display time (event_short()).
    if short not in ("", None):
        meta += f"short={short}\n"
```
`_insert_deadline`: add `short: str | None = None,` and pass `short=short` into its `build_meta(...)` call.
`update_deadline_content`: add `short: str | None = None,` and, because `short_changed` can mean *removed*, set unconditionally:
```python
    meta["short"] = short or ""
```
`test_jobs.py` `preview_insert`: add `short=None` to the signature.

- [ ] **Step 5: Verify**

Run: `python -m pytest tests -v; python -m py_compile main.py belki_import.py test_jobs.py`
Expected: all pass, no compile output.

- [ ] **Step 6: Commit**

```bash
git add belki_import.py main.py test_jobs.py tests/test_belki_short.py
git commit -m "feat: Belki short:: override stored in TETHER_META, collision warnings on sync"
```

---

### Task 3: Display `[short]` everywhere + exact match in `task_matches`

**Files:**
- Modify: `main.py` — new helpers after `task_matches` (~:452), `task_matches` body, `INTENT_PARSER_PROMPT` (~:650-686), every DM/reply that renders a task name: `nag_message` (:587), `handle_push_with_reason` (:1291), `handle_keep` (:1360), complete/query/list/next branches of `on_message` (:1600+), `morning_briefing` (:921), `eod_sweep` (:970), `deadline_warning` (:978), `weekly_queue_summary` (:991), "Multiple matches" lists.

**Interfaces:**
- Consumes: `short_names.derive_short`, `short_names.normalize_query`, `parse_meta`
- Produces: `event_short(event: dict) -> str | None`, `task_label(event: dict) -> str` (returns `"[short] name"` or `"name"`), `resolve_task(query: str, events: list[dict]) -> list[dict]`

- [ ] **Step 1: Add helpers and rewrite matching**

```python
def event_short(event: dict) -> str | None:
    meta = parse_meta(event)
    return derive_short(meta.get("belki_id"), meta.get("short"))


def task_label(event: dict) -> str:
    """`[endcard1] End card: …` — the handle Simon can type back verbatim."""
    name = clean_name(event.get("summary", ""))
    short = event_short(event)
    return f"[{short}] {name}" if short else name


def resolve_task(query: str, events: list[dict]) -> list[dict]:
    # Exact short-name hit wins outright: the whole point of a handle is that
    # it names one task even when a project has 17 open cards and the
    # project-word match below would return "Multiple matches".
    q = normalize_query(query)
    exact = [e for e in events if q and event_short(e) == q]
    if exact:
        return exact
    return [e for e in events if task_matches(query, e)]
```
Import `from short_names import derive_short, normalize_query` at the top. Leave `task_matches` itself unchanged. Replace every `[e for e in all_tasks if task_matches(task_title, e)]` (push, keep, complete, query, delete branches) with `resolve_task(task_title, all_tasks)`.

- [ ] **Step 2: Intent prompt**

Add under the rules in `INTENT_PARSER_PROMPT`:
```
- task_title: if the user writes a short handle like "endcard1" or "[endcard1]", return it verbatim as task_title (without brackets)
```

- [ ] **Step 3: Prefix every task line**

Replace the inline `event["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()` / `clean_name(e["summary"])` used **for display** with `task_label(e)` in all functions listed under Files. In `nag_message`, pass `name=task_label(event)` into `template.format`. Keep `clean_name` where the name is used for logic or ledger (`resolve_push_date` arguments, log lines may keep plain names).

Grep check afterwards:
Run: `grep -n 'replace("— DUE", "")' main.py`
Expected: only `task_matches` and `priority_score` (logic, not display) remain.

- [ ] **Step 4: Verify**

Run: `python -m py_compile main.py; python -m pytest tests -v`
Expected: compiles; tests pass.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: show [short] handle on every task line and match it exactly first"
```

---

### Task 4: Share complete / keep / push logic between text and buttons

**Files:**
- Modify: `main.py:1291-1387` (`handle_push_with_reason`, `handle_keep`), `main.py:1600-1655` (complete branch)

**Interfaces:**
- Consumes: `resolve_task`, `task_label`
- Produces (all return the reply string; none touch Discord):
  - `do_complete(e: dict) -> str`
  - `do_keep(e: dict) -> str`
  - `do_push(e: dict, push_reason: str, command: dict, content: str) -> str`
  - `handle_push_with_reason` / `handle_keep` / complete branch become: resolve → no/multiple-match replies (unchanged text, names via `task_label`) → `await message.reply(do_x(...), mention_author=False)`.

- [ ] **Step 1: Extract `do_push`**

Move everything in `handle_push_with_reason` from `event_id = e["id"]` through building the reply text into:
```python
def do_push(e: dict, push_reason: str, command: dict, content: str) -> str:
    """Push one resolved event. Shared by the text `push` command and the Push button."""
    event_id = e["id"]
    meta = parse_meta(e)
    ...  # existing body unchanged, through ledger.record_pushed / discard / pop
    log(...)  # existing log line
    return (
        f"📅 **{task_label(e)}** pushed to **{target_date}**. Reason logged: _{push_reason}_. "
        f"Push #{meta['pushes']}.{fallback_note}{cap_note}"
    )
```
`handle_push_with_reason` keeps its signature; after the single-match check: `await message.reply(do_push(e, push_reason, command, content), mention_author=False)`.

- [ ] **Step 2: Extract `do_keep`**

```python
def do_keep(e: dict) -> str:
    event_id = e["id"]
    unacknowledged_overdue.discard(event_id)
    # (keep existing comment about kept_today verbatim)
    kept_today.add(event_id)
    ledger.record_kept(e)
    log(f"[KEEP] {clean_name(e['summary'])} acknowledged, suppressed until midnight")
    return f"✅ Got it — **{task_label(e)}** stays. I won't nag about it again today."
```

- [ ] **Step 3: Extract `do_complete`**

```python
def do_complete(e: dict) -> str:
    get_service().events().delete(calendarId="primary", eventId=e["id"]).execute()
    ledger.record_completed(e)
    unacknowledged_overdue.discard(e["id"])
    task_nag_counts.pop(e["id"], None)
    log(f"[COMPLETE] {clean_name(e['summary'])}")
    remaining = get_tether_deadlines()
    if remaining:
        nxt = rank_deadlines(remaining)[0]
        return (
            f"✅ **{task_label(e)}** done. Up next: **{task_label(nxt)}** — "
            f"due {nxt['start']['dateTime'][:10]}."
        )
    return f"✅ **{task_label(e)}** done. Queue is clear."
```
The complete branch in `on_message` calls `await message.reply(do_complete(e), mention_author=False)`.

Note: `do_*` must declare nothing `global` — they only mutate the module-level set/dict in place (`discard`, `add`, `pop`), same as the current handlers.

- [ ] **Step 4: Verify**

Run: `python -m py_compile main.py; python -m pytest tests -v`
Expected: compiles; tests pass. Reviewer diffs old vs new reply strings: identical except `[short]` prefix.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "refactor: extract do_complete/do_keep/do_push so buttons reuse text-command logic"
```

---

### Task 5: Nag embeds with persistent Done / Keep / Push buttons

**Files:**
- Modify: `main.py` — new section `# --- NAG BUTTONS ---` after `dm_user` (~:820); `send_overdue_nag` send block (:874-883); `on_ready` (:1390) registration line.

**Interfaces:**
- Consumes: `do_complete`, `do_keep`, `do_push`, `task_label`, `priority_score`, `short_names.button_custom_id`, `nag_message`, `parse_meta`, `get_service`
- Produces: `NAG_EMBED_CAP = 10`, `TaskActionButton` (DynamicItem), `PushReasonModal`, `build_nag_embed(e, session_nag_count) -> discord.Embed`, `nag_view(event_id) -> discord.ui.View`

- [ ] **Step 1: Buttons, modal, embed builder**

```python
# --- NAG BUTTONS ---
# One embed per overdue task with Done / Keep / Push buttons (2026-10-06).
# DynamicItem + a templated custom_id makes every button persistent: after a
# restart (frequent — see tether.log) bot.add_dynamic_items re-binds old
# buttons by parsing the event id out of custom_id, no per-message state.
NAG_EMBED_CAP = 10


def _fetch_live_event(event_id: str) -> dict | None:
    try:
        e = get_service().events().get(calendarId="primary", eventId=event_id).execute()
    except Exception:
        return None
    return None if e.get("status") == "cancelled" else e


async def _guard(interaction: discord.Interaction) -> bool:
    if interaction.user.id != DISCORD_USER_ID:
        await interaction.response.send_message("Not your queue.", ephemeral=True)
        return False
    return True


class PushReasonModal(discord.ui.Modal, title="Push task"):
    reason = discord.ui.TextInput(label="Why are you pushing it?", required=True, max_length=300)
    when = discord.ui.TextInput(
        label="New date (optional)", required=False, placeholder="Friday / 2026-10-12",
        max_length=40,
    )

    def __init__(self, event_id: str):
        super().__init__()
        self.event_id = event_id

    async def on_submit(self, interaction: discord.Interaction):
        e = _fetch_live_event(self.event_id)
        if not e:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        reason = str(self.reason.value).strip()
        when = str(self.when.value).strip()
        # resolve_push_date reads the raw text for date hints, same as a typed
        # "push X to Friday because …" — so feed it an equivalent sentence.
        content = f"push {clean_name(e['summary'])}" + (f" to {when}" if when else "") + f" because {reason}"
        command = {"action": "push", "task_title": clean_name(e["summary"]),
                   "push_reason": reason, "target_date": None}
        await interaction.response.send_message(do_push(e, reason, command, content))


class TaskActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"tether:(?P<action>done|keep|push):(?P<eid>[A-Za-z0-9_-]+)",
):
    _STYLE = {
        "done": ("✅ Done", discord.ButtonStyle.success),
        "keep": ("📌 Keep", discord.ButtonStyle.secondary),
        "push": ("⏩ Push", discord.ButtonStyle.primary),
    }

    def __init__(self, action: str, event_id: str):
        label, style = self._STYLE[action]
        super().__init__(discord.ui.Button(
            label=label, style=style, custom_id=button_custom_id(action, event_id),
        ))
        self.action = action
        self.event_id = event_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], match["eid"])

    async def callback(self, interaction: discord.Interaction):
        if not await _guard(interaction):
            return
        if self.action == "push":
            # The modal must be the first response; it re-checks liveness on submit.
            await interaction.response.send_modal(PushReasonModal(self.event_id))
            return
        e = _fetch_live_event(self.event_id)
        if not e:
            await interaction.response.send_message("Already handled.", ephemeral=True)
            return
        reply = do_complete(e) if self.action == "done" else do_keep(e)
        await interaction.response.send_message(reply)


def nag_view(event_id: str) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    for action in ("done", "keep", "push"):
        view.add_item(TaskActionButton(action, event_id))
    return view


def build_nag_embed(e: dict, session_nag_count: int) -> discord.Embed:
    meta = parse_meta(e)
    embed = discord.Embed(
        title=task_label(e)[:256],
        description=nag_message(e, session_nag_count)[:4096],
    )
    embed.add_field(name="Priority", value=meta.get("priority") or "—")
    embed.add_field(name="Due", value=e["start"]["dateTime"][:10])
    embed.add_field(name="Project", value=meta.get("project") or "—")
    embed.add_field(
        name="Pressure",
        value=f"{meta['pushes']} pushes · {meta['nag_ignored'] + session_nag_count} ignored",
    )
    return embed
```
Add `from short_names import button_custom_id` to the imports. Verify `_DISCORD`-side import names exist in discord.py 2.7 (`discord.ui.DynamicItem`, `bot.add_dynamic_items`) — if not, STOP and report BLOCKED.

- [ ] **Step 2: Rewrite the send block in `send_overdue_nag`**

Replace from `lines = [nag_summary_header(nag_count)]` through `await dm_user("\n".join(lines))` with:
```python
    user = await bot.fetch_user(DISCORD_USER_ID)
    await dm_user(nag_summary_header(nag_count).strip())
    ranked = sorted(pending, key=priority_score, reverse=True)
    for e in ranked[:NAG_EMBED_CAP]:
        eid = e["id"]
        await user.send(embed=build_nag_embed(e, task_nag_counts.get(eid, 0)), view=nag_view(eid))
        task_nag_counts[eid] = task_nag_counts.get(eid, 0) + 1
    rest = ranked[NAG_EMBED_CAP:]
    if rest:
        await dm_user(f"…and {len(rest)} more: " + ", ".join(task_label(e) for e in rest))
        for e in rest:
            task_nag_counts[e["id"]] = task_nag_counts.get(e["id"], 0) + 1
```
(Confirm `priority_score` sorts higher = more urgent by reading `rank_deadlines` at :507 — match its direction.)

- [ ] **Step 3: Register in `on_ready`**

After the `_started = True` line add:
```python
    # Re-bind nag buttons from earlier messages (they outlive the process).
    bot.add_dynamic_items(TaskActionButton)
```

- [ ] **Step 4: Verify**

Run: `python -m py_compile main.py; python -m pytest tests -v`
Expected: compiles; tests pass.
If a local env with requirements is available (`pip install -r requirements.txt` in a venv), also run `python -c "import os; os.environ.setdefault('DISCORD_USER_ID','1'); import main; v=main.nag_view('abc'); print([c.custom_id for c in v.children])"` → `['tether:done:abc', 'tether:keep:abc', 'tether:push:abc']`.

- [ ] **Step 5: Commit**

```bash
git add main.py
git commit -m "feat: overdue nags as per-task embeds with persistent Done/Keep/Push buttons"
```

---

### Task 6: Docs

**Files:**
- Modify: `CLAUDE.md`, `CHANGELOG.md`

- [ ] **Step 1:** In `CLAUDE.md`, document: the `short::` card field and derivation rule; `[short]` is accepted in any command; nag DMs are embeds with buttons (cap 10, persistent via `TaskActionButton`); `short_names.py` + `tests/` + `pytest` (`pip install -r requirements-dev.txt; python -m pytest tests`).
- [ ] **Step 2:** `CHANGELOG.md` new top entry dated 2026-10-06 with the two features and the action refactor.
- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md CHANGELOG.md
git commit -m "docs: short names, nag buttons, pytest suite"
```
