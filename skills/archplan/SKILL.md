---
name: archplan
description: "Work with the human on this repository's architecture in archplan: set it up, open the editor, answer comment threads, model refactors in the target, and implement the plan. Use when the user invokes /archplan, mentions archplan or its comments, or asks to plan or review architecture changes together in the editor."
argument-hint: "[init | up | status | watch]"
allowed-tools: Bash(archplan:*), Bash(${CLAUDE_PLUGIN_ROOT}/bin/archplan:*)
---

# archplan

archplan is a browser editor for a repository's architecture. The human edits a
target model (components, types, dependency rules, call sequences, glossary),
and the editor turns the difference with the code into a change plan. Both of
you comment on items in threads. You read and answer them with the `archplan`
MCP tools; the human sees your replies in the editor within a second.

Everything lives in `.archplan/` in the repository root. The `archplan`
command is on your PATH (from this plugin's `bin/`); if it is not, use
`${CLAUDE_PLUGIN_ROOT}/bin/archplan`.

Arguments: $ARGUMENTS

## 1. Set up (once per repository)

Run `archplan status`. If it says there is no project:

1. `archplan init` in the repository root. It picks an exporter for Go
   (`go.mod`) and Python (`pyproject.toml`, `setup.py`). For other languages,
   tell the user that `export` in `.archplan/config.json` must name a command
   that writes an archmodel/1 file (format: `src/archplan/format/archmodel.md`
   in this plugin), and stop.
2. `archplan export`. Report the counts. If it fails, show the error.
3. Ask whether `.archplan/` should be committed. Committing it shares the
   target, plan and threads with the team; `server.json` and logs stay
   ignored.

## 2. Open the editor

Run `archplan up`. It starts the editor in the background (or finds the
running one) and prints its URL. Give the user the URL. If you have a browser
pane, open it there.

## 3. Stay in the conversation

The human works in the editor while you work here. You learn about their
comments three ways:

- The plugin's `archplan` monitor starts with this skill and sends one event
  per new human comment, resolved thread or plan change. Act on each event.
- At each user prompt a hook lists threads that wait on you.
- `wait_for_comments` blocks until the human writes, when you have nothing else
  to do and the user asked you to keep watching.

If the monitor is not running (for example in a session without monitors),
start `archplan watch` with the Monitor tool yourself.

## 4. Answer a thread

1. `read_thread` gives the messages and the model item the thread is about.
2. Investigate in the code. Be concrete: name files, types and call sites.
3. `reply` in that thread. Keep it short and plain.
   - Set `needs_decision=true` with two to four short `options` only when you
     cannot continue without the human's choice. End with one clear question.
   - Everything else (findings, reports, answers) is feedback: leave
     `needs_decision` false.
   - Set `resolve=true` when the question is settled or the work is done.
4. To raise something yourself, `open_thread` on the item (see the anchor
   formats in the tool description). Use a `group:` anchor for a finding about
   several items at once, such as an import cycle.

## 5. Model before you build

When the human asks you to model a change, use `edit_target` (add or move
components and types, forbid or permit dependencies). The editor shows it in
its Diff view, and the human can undo it. Then `get_plan` shows the tasks the
change implies; summarise them in the thread and ask whether to implement.

## 6. Implement

- Work on a new branch or in a git worktree, never on the user's current
  branch without asking. Commit only when the user agreed; never push without
  asking.
- Follow the plan task's acceptance checks (build, tests, dependency rules).
- After a change, call `refresh_model`. The editor reloads the code's model,
  rebases the target onto it, and the plan shrinks by what you did. Report in
  the thread what changed and what is left.

## Commands

| Command | What it does |
|---|---|
| `archplan status` | editor URL, model size, plan size, threads waiting |
| `archplan up` / `down` | start or stop the editor in the background |
| `archplan export` | re-export the as-is model from the code |
| `archplan threads --waiting agent` | threads that wait on you, as text |
