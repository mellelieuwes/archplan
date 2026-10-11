# archplan contrib: framework profiles and exporters

This folder holds data that archplan ships but does not hard-code:

- `profiles/*.json`: architecture defaults per framework (units, roles, rules
  and layers). `archplan profile infer` proposes the best match.
- `exporters/*.json`: the exporter registry. `archplan init` picks the export
  command from it.

A project can add or replace entries with files of the same shape in
`.archplan/profiles/` and `.archplan/exporters/`. A project file with the same
`name` as a built-in one wins. archplan treats every file as untrusted data.
It checks the size, the names, the patterns and the colours before use.
Project exporter checks never run. A project exporter command still needs
`archplan trust`. Set `"runs_repo_code": true` on an exporter that runs code
from the repository (its build files, its compiler package or its
toolchain): `archplan init` then leaves the command for `archplan trust`.

To add a profile or an exporter, open a pull request with one JSON file and
a test fixture.

## Framework profiles

```json
{
  "name": "rails",
  "title": "Rails app",
  "language": "ruby",
  "description": "What the profile assumes, in one or two sentences.",
  "detect": {
    "priority": 50,
    "any": [
      {"deps": ["rails", "railties"], "files": ["**/app/models/**/*.rb"]},
      {"files": ["**/config/routes.rb", "**/app/controllers/**/*.rb"]}
    ]
  },
  "units": [{"kind": "App", "glob": "**/app", "require": ["controllers/", "models/"]}],
  "roles": [{"id": "models", "name": "Models", "patterns": ["models/**"]}],
  "rules": [{"id": "models-stay-low", "type": "calls", "from": "models", "to": ["controllers"],
             "allow": false, "scope": "any", "text": "Models do not reference controllers"}],
  "layers": [{"name": "Web", "match": ["*Web", "*Web.*"]}, {"name": "Domain", "match": ["*"]}]
}
```

- **name**: lower-case letters, digits and dashes. `archplan profile use <name>`
  applies it.
- **detect**: when the profile applies. Give either `files` and `deps`, or a list
  of such alternatives under `any`. An alternative matches when every file glob
  matches at least one file and, when `deps` are given, a manifest declares one
  of them.
  - A file glob can be `{"glob": "**/apps.py", "min": 2}` to need two matches.
  - `**` spans folders, and `*` stays inside one folder name.
  - Manifests are `pyproject.toml`, `requirements*.txt`, `setup.cfg`,
    `package.json`, `pom.xml`, `build.gradle(.kts)`, `Gemfile`,
    `composer.json`, `*.csproj`, `mix.exs`, `go.mod` and `Cargo.toml`, up to
    three folders deep.
  - A dependency name also matches its sub-packages: `spring-boot` matches
    `spring-boot-starter-web`.
  - `priority` breaks ties between matching profiles. Use 60 for a framework
    that builds on another one (Next.js on React), 50 for a full-stack
    framework, 40 for a library framework and 30 or less for a loose
    convention.
- **base** (optional): file names, such as `["apps.py", "models.py"]`. `{base}` in
  a unit glob becomes the common parent folder of the folders that hold them.
- **units, roles, rules**: as in `.archplan/architecture.json`; see the profile
  format in `architecture.py`.
  - Units take an extra `except` list of folder globs to skip.
  - Use `exclude` for test files.
  - Role colours and names are optional.
  - When a file matches patterns of several roles, the pattern with the most
    literal characters wins.
- **layers** (optional): ordered from the outside in. `match` holds globs on
  component ids or paths, and the first matching layer wins. When fewer than
  half of the components match, archplan infers the layers from the model
  instead.

Inference picks the matching profile with the highest priority that finds
units. It keeps the inferred generic profile only when that profile gives a
role to more than 1.5 times as many files. `archplan profile list` shows the
matches and the share of files each one gives a role.

Keep a profile small and true to the framework's own conventions: 2 to 5
rules that hold in a typical, healthy project. A rule that most projects break
is noise.

## Exporters

```json
{
  "name": "elixir",
  "title": "Elixir (Mix compiler)",
  "languages": ["elixir"],
  "priority": 60,
  "detect": {"manifests": {"mix.exs": []}, "subdirs": true, "weight": ["lib/**/*.ex"]},
  "command": "archplan export-elixir{chdir} -o {out}",
  "check": {"programs": ["mix"]},
  "capabilities": ["components", "types", "fields", "deps", "files", "calls"],
  "install": "Install Elixir and run mix deps.get in the project."
}
```

- **detect**: an exporter matches a folder when one of its `files` globs matches
  and one of its `manifests` exists there. A manifest's list names
  dependencies it must declare; an empty list means the file only has to
  exist. A glob without a slash looks at the folder itself.
- **subdirs**: also try the direct subfolders. The one with the most files
  matching `weight` wins.
- **command**: a one-line shell command, run from the repository root.
  - `{out}` is the model path.
  - `{root}` is the matched folder.
  - `{chdir}` is ` -C <folder>`, or nothing when the folder is the root.
- **check**: `{"programs": [...], "files": [...]}` tests that the programs are
  on the PATH and that the files exist in the matched folder. A string runs
  as a command (exit 0 means installed), but only for built-in entries.
- **capabilities**: what the exporter fills in. See
  [`format/exporter-plugins.md`](../format/exporter-plugins.md).

`archplan exporters` lists all entries. It shows which ones match and which
are installed, with install hints. `archplan init` takes the highest-priority
match that is installed. If none is installed, it takes `lite` when that
matches, and else the highest-priority match.
