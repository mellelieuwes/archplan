# archplan

Plan architecture changes together with an agent.

archplan exports a model of your code: components, dependencies, types, call
sequences and a glossary. You open the model in a browser editor and change it
into the architecture you want. The editor turns the difference into a change
plan. You comment on any component, type, dependency, call or plan task, and
Claude answers in the same thread. Claude can propose changes to the target
model, and after it changes the code the plan shrinks by what it did.

## Install

### As a Claude Code plugin (recommended)

The plugin includes the `archplan` command, the MCP server, a skill, a monitor
and hooks. It needs Python 3.9 or later and nothing else.

```bash
claude plugin marketplace add vloothuis/archplan
```

```bash
claude plugin install archplan@archplan
```

From a local checkout, use the path instead: `claude plugin marketplace add
/path/to/archplan`. Add `--scope project` to both commands to share the plugin
with everyone who works in a repository.

### As a command only

```bash
pipx install git+https://github.com/vloothuis/archplan
```

Or use `uv tool install`, or put `bin/archplan` from a checkout on your PATH.
To give a Claude Code session the MCP tools without the plugin:

```bash
claude mcp add archplan -- archplan mcp
```

## Start

In Claude Code, run `/archplan` in a repository. The skill sets up the project,
exports the model and opens the editor. By hand:

```bash
archplan init      # creates .archplan/config.json; picks an exporter
archplan export    # code -> .archplan/model.archmodel.json
archplan up        # starts the editor in the background and prints its URL
```

## How you and Claude work together

| You, in the editor | Claude |
|---|---|
| Edit the target: move types, add components, forbid dependencies, rename terms | Sees the plan with `get_plan`, and each item with `get_item` |
| Comment on an item, or shift-click several items and comment on the group | Is told at once: the plugin's monitor sends each comment, and a hook lists waiting threads at every prompt |
| Read replies: orange **decision** threads need your choice (click an option); **feedback** threads only need reading | Replies with `reply`, marks a reply as a decision only when it is blocked on you |
| Review proposed changes in the Diff view; Undo reverts them | Models a refactor with `edit_target` before building it |
| Watch the plan shrink | Implements a task, then calls `refresh_model`; the editor reloads the code model and rebases your target onto it |

Everything is stored in `.archplan/` in the repository:

| File | What it holds |
|---|---|
| `config.json` | export command, model path, port |
| `model.archmodel.json` | the as-is model, written by the exporter |
| `target.archmodel.json` | the target model |
| `base.archmodel.json` | the as-is model the target was last edited against, for rebasing |
| `state.json` | target revision and the plan's intent |
| `plan.md`, `plan.json` | the change plan, written by the editor |
| `threads.json` | the comment threads |

Commit `.archplan/` to share the target, plan and threads with your team.
`server.json` and the logs are ignored.

## Commands

| Command | What it does |
|---|---|
| `archplan init [--export CMD]` | set up `.archplan/` in this repository |
| `archplan export` | run the export command |
| `archplan up [--open]`, `archplan down` | start or stop the editor in the background |
| `archplan serve` | run the editor in the foreground |
| `archplan status` | editor URL, model size, plan size, threads waiting |
| `archplan threads [--waiting agent]` | print threads |
| `archplan mcp` | the MCP server, on stdio |
| `archplan watch` | one line per human comment or plan change |
| `archplan hook` | Claude Code hook: report threads that wait on the agent |
| `archplan export-python` | the built-in Python exporter |

## Exporters

An exporter writes an [archmodel/1](src/archplan/format/archmodel.md) file.
`archplan init` picks one from the files in the repository:

- **Go** (`go.mod`): [`exporters/archplan-go`](exporters/archplan-go), run with
  `go run github.com/vloothuis/archplan/exporters/archplan-go@latest`. It reads
  `.go-arch-lint.yml` when present, and seqtrace call traces with `-steps`.
- **Python** (`pyproject.toml`, `setup.py`): `archplan export-python`, built in.
  It reads import-linter `forbidden` contracts as rules.

For another language, set `export` in `.archplan/config.json` to any command
that writes the model file or prints it on stdout.

## Security

- The editor listens on 127.0.0.1 only and has no login. It refuses writes from
  pages on other origins.
- `archplan export`, the editor's "From code" button and the agent's
  `refresh_model` run the `export` command from `.archplan/config.json` in a
  shell, like a Makefile target. Review that command when you clone a
  repository that has one.

## Development

```bash
python3 -m unittest discover -s tests
```

```bash
claude plugin validate .
```

```bash
claude --plugin-dir .
```

The editor is a single file, `src/archplan/static/index.html`. Add `?sample` to
its URL for the built-in example model.
