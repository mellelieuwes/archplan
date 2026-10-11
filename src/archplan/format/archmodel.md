# archmodel v1

`archmodel` is the interchange format between language-specific extractors and
archplan. An extractor reads a codebase and writes one JSON file. archplan
reads it as the as-is model. The edited target model is saved in the same
format, so it can live in the repository and be diffed in review.

The format describes architecture, not syntax. Every language maps its own
concepts onto six sections: components, dependencies, policy, types,
sequences and vocabulary. Each section is optional. An extractor fills what its language and
tooling can provide.

JSON Schema: [`archmodel.schema.json`](archmodel.schema.json).

## Top level

```json
{
  "format": "archmodel/1",
  "source": { ... },
  "layers": [ ... ],
  "components": { "<id>": { ... } },
  "dependencies": [ ... ],
  "files": { "<path>": { ... } },
  "policy": { ... },
  "types": { "<id>": { ... } },
  "sequences": { "<id>": { ... } },
  "vocabulary": { ... },
  "notes": { "<target>": "text" }
}
```

## IDs

IDs are opaque strings. They must be unique within their section and stable
across extractions of the same code: archplan diffs two models by ID, so an ID
that changes between runs shows up as a removal plus an addition.

Recommended IDs:

| Section | Recommended ID | Example |
|---|---|---|
| component | name used by the project's own boundary tool, else the module-relative path | `dataentry`, `internal/dataentry` |
| type | module-relative package or module path, a dot, the type name | `internal/store.Store`, `MyApp.Accounts.User`, `app/models/user.User` |
| sequence | slug of the scenario name | `read-task-as-manager` |
| message | `from>to:label#n`, where `n` counts repeats of the same call in that sequence | `acl>store:AtFaces#1` |
| concept | slug of the concept name | `cart-item` |

## source

Where the model came from, and how to check work against it.

```json
"source": {
  "repo": "sourcehaven-bv/rela",
  "ref": "6dec192e1",
  "dirty": false,
  "languages": ["go"],
  "module": "github.com/Sourcehaven-BV/rela",
  "generator": "rela tools/archexport",
  "generatedAt": "2026-10-10T09:00:00Z",
  "commands": {
    "build": "go build ./...",
    "test": "go test ./{path}/...",
    "rules": "just arch-lint",
    "sequences": "just seqtrace-compare {ref}"
  }
}
```

`commands` are what the plan puts into acceptance checks. `{path}` is replaced
with a component path and `{ref}` with the base ref. All are optional.

## layers

Ordered groups of components, outermost first. Used for grouping and colour.

```json
"layers": [{ "id": "web-ui", "name": "Web / UI layer" }]
```

## components

The unit of dependency rules: a package, module, namespace, app or directory.

```json
"dataentry": {
  "name": "dataentry",
  "path": "internal/dataentry",
  "match": ["internal/dataentry"],
  "layer": "web-ui",
  "kind": "internal",
  "doc": "HTTP data-entry API and SPA host."
}
```

- `path` is where a developer finds the code.
- `match` lists the patterns the component covers (`**` matches any depth).
- `kind` is `internal` (default) or `external` for third-party code.

## dependencies

Observed dependencies between components. Extractors must report all of them,
not only those explained by the types section.

```json
{ "from": "dataentry", "to": "store", "weight": 14,
  "evidence": ["internal/dataentry/handler.go:12"] }
```

`weight` is the number of files (or import sites) that cause the edge.
`evidence` lists some of them as `file:line`.

## files

Optional. File-level references, for architecture profiles: which project
files each source file imports or calls. Keys are paths relative to the
repository root, like type `file` values.

```json
"internal/dataentry/handler.go": {
  "component": "dataentry",
  "refs": [{ "to": "internal/store/store.go", "line": 12 }]
}
```

- `refs` lists each other project file once, with the line of the first
  reference when known. References to the file itself and to code outside
  the project are left out.
- A model without `files` still works. archplan then knows a file's role in
  a profile, but cannot check who calls whom.

## policy

Allowed and forbidden dependencies.

```json
"policy": {
  "default": "deny",
  "rules": [
    { "from": "cmdServer", "to": "dataentry", "effect": "allow" },
    { "from": "*", "to": "entity", "effect": "allow", "note": "common component" }
  ],
  "enforcer": { "tool": "go-arch-lint", "config": ".go-arch-lint.yml", "command": "just arch-lint" }
}
```

- With `default: "allow"`, a dependency is a violation when a `deny` rule
  matches it.
- With `default: "deny"`, a dependency is a violation unless an `allow` rule
  matches it.
- `*` matches any component.
- `enforcer` names the tool that checks the policy in CI. The plan then edits
  that tool's config instead of generating a new test.

## types

Data types, classes, interfaces and their relations.

```json
"internal/store.Store": {
  "name": "Store",
  "component": "store",
  "kind": "interface",
  "file": "internal/store/store.go",
  "line": 41,
  "doc": "Store is the persistence port.",
  "fields": [
    { "name": "db", "type": "*pgxpool.Pool" },
    { "name": "cache", "ref": "internal/cache.Cache", "ptr": true },
    { "name": "items", "ref": "internal/order.LineItem", "many": true }
  ],
  "methods": [{ "name": "Get", "sig": "(ctx context.Context, id string) (entity.Entity, error)" }],
  "implements": ["internal/store.Reader"],
  "uses": ["internal/entity.Entity"]
}
```

- `kind` is one of `struct`, `class`, `interface`, `record`, `enum`, `alias`,
  `function`, `module`.
- A field with `ref` points at another type in the model. A field with only
  `type` points outside it. `many` marks a collection.
- `implements` lists interfaces the type satisfies, whether declared or
  structural.
- `uses` lists types that appear in method signatures but not in fields. For
  an `alias`, it names the aliased type when that type is in the model.
- `sig` is free text in the source language.

## sequences

Ordered calls for one scenario, usually from traces.

```json
"read-task-as-manager": {
  "title": "read task as manager",
  "entry": "internal/dataentry.(*App).handleV1GetEntity",
  "source": "seqtrace",
  "participants": ["Caller", "dataentry", "acl", "store"],
  "fragments": { "L1": { "kind": "loop", "label": "×12", "parent": null } },
  "messages": [
    { "id": "Caller>dataentry:(*App).handleV1GetEntity#1", "from": "Caller", "to": "dataentry",
      "label": "(*App).handleV1GetEntity(w, r)", "depth": 0 },
    { "id": "dataentry>acl:FromContext#1", "from": "dataentry", "to": "acl",
      "label": "FromContext()", "depth": 1 },
    { "id": "acl>store:AtFaces#1", "from": "acl", "to": "store",
      "label": "AtFaces(faces)", "depth": 2, "fragment": "L1", "async": true, "via": "go" }
  ]
}
```

- A participant is a type ID, a component ID, or a free name for an outside
  actor (`Caller`, `Browser`, `Cron`).
- `depth` is the call depth. A message at depth `d + 1` directly after one at
  depth `d` is a call made inside it.
- `fragment` names the innermost loop or alternative the message is in.
- `async` marks a call on another process, goroutine, task or queue. `via`
  says how.
- Labels must not contain argument values. Traces can carry secrets.

## vocabulary

The domain language of the code: which component owns which concept, and
where code names a concept it does not own.

```json
"vocabulary": {
  "concepts": {
    "cart-item": { "name": "Cart item", "owner": "cart", "terms": ["cart item", "basket"],
      "kind": "domain", "definition": "One product line in a cart.", "source": "glossary" },
    "stripe": { "name": "stripe", "owner": "stripe", "terms": ["stripe"], "kind": "vendor", "source": "vendor" }
  },
  "components": {
    "cart": { "file": "internal/cart/GLOSSARY.md", "uses": ["product"], "avoid": ["stripe"],
      "suggested": [{ "term": "voucher", "score": 4.2 }] }
  },
  "usage": [
    { "component": "cart", "concept": "stripe", "idents": 3, "strings": 1, "comments": 2,
      "evidence": ["internal/cart/pay.go:12"] }
  ]
}
```

- A concept has one `owner`, a component ID. `terms` are the words code uses
  for it; the first is usually the name.
- `kind` is `domain`, `vendor` (named after third-party code) or `technical`.
- `source` says where the concept came from: `glossary` (written by a
  developer), `vendor` (derived from an external component) or `suggested`
  (proposed by a tool, not yet accepted).
- `components` holds per-component glossary data. `uses` lists concepts of
  other components the component may name. `avoid` lists concepts it must not
  name. `suggested` lists frequent words that are not a concept yet, with a
  relevance score.
- `usage` counts how often a component names a concept it does not own, per
  channel: identifiers, string literals and comments. A term matches a run of
  words, so `cartItems` and "cart items" both match the term `cart item`.
  `evidence` lists some sites as `file:line`.

When an exporter writes no `vocabulary` section, archplan adds one after the
export. It reads each component's `GLOSSARY.md`, ranks `suggested` terms by
tf-idf over the component's files, and counts `usage`.

An extractor reports usage; the reader decides which usage is a leak. A
component's use of a concept is:

1. fine when the component owns the concept;
2. a leak when the concept is in the component's `avoid`;
3. fine when the concept is in the component's `uses`;
4. otherwise a leak when the policy does not allow a dependency from the
   component to the concept's owner.

## notes

Review notes keyed by target: `type:<id>`, `comp:<id>`, `msg:<sequence>:<id>`.
Notes in the target model become open questions in the plan.

## Mapping per language

| | Go | Elixir | Python | TypeScript |
|---|---|---|---|---|
| component | package, or a go-arch-lint component | OTP app, or a `boundary` | package | directory, or workspace package |
| dependencies | `go list -json` imports | `mix xref graph` | `grimp` / import graph | `dependency-cruiser --output-type json` |
| policy | `.go-arch-lint.yml`, depguard | `boundary` deps and exports | import-linter contracts | dependency-cruiser `forbidden` rules |
| types | `go/types` | structs, `@type`, behaviours as interfaces | classes, dataclasses, `Protocol` and ABCs as interfaces | classes, interfaces, type aliases (ts-morph) |
| implements | structural (`types.Implements`) | `@behaviour` | base classes, Protocol conformance | `implements`, structural if checked |
| sequences | seqtrace, OpenTelemetry | `:telemetry`, OpenTelemetry | OpenTelemetry, `sys.setprofile` | OpenTelemetry |

OpenTelemetry spans map to sequences in every language: a span is a message
from the parent span's service or component to its own, and the span tree
gives the depth.
