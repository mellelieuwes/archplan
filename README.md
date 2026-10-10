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
| Preview a suggestion and accept all, some or none of its changes | Proposes a refactor of the target with `edit_target`; learns the outcome from the monitor |
| Look at hotspots and components that change together | Finds hidden coupling in the git history with `get_hotspots` and opens a thread on it |
| Lock in a settled thread as a planned refactor, then go on with the open discussions | Leaves planned refactors alone; they do not wait on it |
| Order the planned refactors and send them all as one handover: all in one go, or ask for a split first | Reads them with `get_handover`; for a split, proposes batches with `propose_batches` |
| Approve or edit the proposed batches | Works refactor by refactor: `set_refactor_state`, code, `refresh_model`, a reply in the thread |
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
| `suggestions.json` | target changes the agent proposed, and what you decided |
| `handovers.json` | planned refactors you sent to the agent, its batches and progress |
| `activity.json` | what the agent did last, shown in the editor |
| `hotspots.json` | the cached git history analysis |

Commit `.archplan/` to share the target, plan and threads with your team.
`server.json`, `activity.json`, `hotspots.json` and the logs are ignored.

In a message, `@[Label](anchor)` mentions a model item, for example
`@[Orders](comp:shop.orders)`. The editor and the agent both use this form.

## Commands

| Command | What it does |
|---|---|
| `archplan init [--export CMD]` | set up `.archplan/` in this repository |
| `archplan export` | run the export command |
| `archplan trust` | approve a cloned repository's export command |
| `archplan up [--open]`, `archplan down` | start or stop the editor in the background |
| `archplan serve` | run the editor in the foreground |
| `archplan status` | editor URL, model size, plan size, threads waiting |
| `archplan threads [--waiting agent]` | print threads |
| `archplan mcp` | the MCP server, on stdio |
| `archplan watch` | one line per human comment, plan change or decided suggestion |
| `archplan hotspots [--since DAYS] [--json]` | components that change most, and pairs that change together without a dependency |
| `archplan hook` | Claude Code hook: report threads that wait on the agent |
| `archplan export-python` | the built-in Python exporter |
| `archplan export-elixir`, `archplan trace-elixir` | the Elixir exporter and test tracer |

## Hotspots

`archplan hotspots` reads the last year of git history (`--since DAYS` to
change it). A hotspot is a component that changes in many commits. Two
components are coupled when they change in the same commits. The degree is
the number of shared commits divided by the commits of the less active of the
two. A coupled pair with no dependency in the model is hidden coupling: the
code's imports do not explain why they change together.

The analysis maps each changed file to a component with the model's `match`
patterns and `path`. Commits that touch more than 50 files are skipped as bulk
changes. Only git runs, with hooks, fsmonitor and signature checks switched
off. The result is cached per HEAD in `.archplan/hotspots.json`.

## Exporters

An exporter writes an [archmodel/1](src/archplan/format/archmodel.md) file.
`archplan init` picks one from the files in the repository:

- **Go** (`go.mod`): [`exporters/archplan-go`](exporters/archplan-go), run with
  `go run github.com/vloothuis/archplan/exporters/archplan-go@latest`. It reads
  `.go-arch-lint.yml` when present, and seqtrace call traces with `-steps`.
- **Python** (`pyproject.toml`, `setup.py`): `archplan export-python`, built in.
  It reads import-linter `forbidden` contracts as rules.
- **Elixir** (`mix.exs`, in the root or a subdirectory):
  `archplan export-elixir`, which needs `mix`. `archplan trace-elixir` runs
  tests and records call sequences. See [`exporters/elixir`](exporters/elixir).

For another language, set `export` in `.archplan/config.json` to any command
that writes the model file or prints it on stdout.

## Security

- The editor listens on 127.0.0.1 only and has no login. It refuses writes from
  pages on other origins.
- The editor answers only requests addressed to localhost, so a web page
  cannot reach it through DNS rebinding.
- The `export` command in `.archplan/config.json` runs in a shell, and a cloned
  repository supplies it. The agent's `refresh_model` and the editor's "From
  code" button run it only after you approved that exact command on this
  machine: `archplan init` approves the command it writes; for a cloned
  repository, run `archplan trust` (or `archplan export`, which asks) in a
  terminal. There is no flag to skip the question, so an agent cannot approve
  a command. Approvals live in `~/.config/archplan/trusted.json`.
- Thread and plan text renders as Markdown without raw HTML; links only open
  http(s) and mailto addresses. The editor's libraries ship with the package.

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
