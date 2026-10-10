# archplan-go

archplan-go reads a Go module and writes its architecture as an `archmodel/1`
JSON file for archplan. It works on any Go module.

```bash
go run github.com/vloothuis/archplan/exporters/archplan-go@latest -o arch.archmodel.json
go run /path/to/archplan/exporters/archplan-go -C ~/src/myservice -o myservice.archmodel.json
```

To keep a binary, build it with an explicit name:

```bash
go build -o archplan-go github.com/vloothuis/archplan/exporters/archplan-go
```

`go install github.com/vloothuis/archplan/exporters/archplan-go@latest` installs it as `archplan-go`.

## What it emits

| Section | Source |
|---|---|
| `source` | `go.mod`, and the git remote, commit and dirty state when the module is in a git repository |
| `components` | One component per package, named by its module-relative path. With `-depth n`, packages are grouped by their first `n` path segments. With a go-arch-lint config, its components and vendors. |
| `layers` | Section comments in the `components` block of the go-arch-lint config; none without it |
| `dependencies` | Imports between components, counted per file, with up to three `file:line` sites each |
| `policy` | Without go-arch-lint: default `allow` and no rules. With it: `deps`, `commonComponents` and `commonVendors` as allow rules over a default of `deny`. |
| `types` | `go/types`: structs, interfaces, enums, function types and aliases, with fields, methods, `implements` and `uses` |
| `sequences` | seqtrace `steps.json` files passed with `-steps` |
| `vocabulary` | `GLOSSARY.md` per component, one concept per vendor, where code names another component's concept, and suggested terms |

With a go-arch-lint config, a package that no component matches still gets a
component, in the layer `unmapped`, and the tool prints a warning.

Type IDs are the module-relative package path, a dot and the type name, such
as `internal/store.Store`. Types in the module's root package use the name
alone.

## Flags

| Flag | Default | Meaning |
|---|---|---|
| `-C` | `.` | Module root (the directory with `go.mod`) |
| `-o` | stdout | Output file |
| `-archlint` | `.go-arch-lint.yml` if it exists | go-arch-lint v3 config, relative to `-C`; `none` ignores it |
| `-exclude` | | Module-relative directories to skip; comma-separated and repeatable. Added to go-arch-lint's `exclude`. |
| `-depth` | `0` | Group packages without a go-arch-lint component by this many leading path segments; `0` is one component per package |
| `-steps` | | seqtrace `steps.json` to add as sequences; repeatable |
| `-no-vocab` | false | Skip the `vocabulary` section |
| `-tags` | | Build tags for `go list` |
| `-unexported` | false | Also export unexported types and methods |

Arguments are package patterns, relative to `-C`. The default is `./...`.

The tool runs `go list -deps -export`, which compiles the packages into the
build cache. The first run on a large module can take minutes.

## Examples

```bash
# Whole module, one component per package.
archplan-go -o model.json

# Group by top-level directory, skip generated code.
archplan-go -depth 2 -exclude internal/gen,tools -o model.json

# Use go-arch-lint components and rules, add traced sequences.
archplan-go -C ~/src/rela -steps steps.json -o rela.archmodel.json

# Only the internal packages, without vocabulary.
archplan-go -no-vocab ./internal/...
```

## Vocabulary

A component can declare its domain language in `GLOSSARY.md` in its directory.
The YAML front matter lists the concepts it owns, the concepts of other
components it may name (`uses`), and the concepts it must not name (`avoid`).
Lines of the form `**Name**: definition` define concepts.

```markdown
---
owns:
  cart: [basket]     # concept name: extra terms
  cart item: []
uses: [product]
avoid: [stripe]
---
**Cart**: the items a shopper intends to buy.
```

Each go-arch-lint vendor becomes a concept named by its ID and the last
segment of its import path. The exporter scans identifiers, string literals
and comments, and counts where a component names a concept it does not own. It
also suggests up to ten terms per component by tf-idf over declared names.

## Sequences

`-steps` reads the `steps.json` that seqtrace writes: a JSON array of
scenarios, each with a `name`, a `handler` and a `diagram` of participants and
nested calls. A participant is a package path; it maps to the component that
holds the package. Argument values are dropped, because traces can hold
secrets.

## Limits

- `implements` is structural: a type implements an interface when its method
  set satisfies it.
- Generic instantiations point at the generic type.
- Only one go-arch-lint config is read.
