package main

import (
	"context"
	"os"
	"path/filepath"
	"slices"
	"testing"
)

// writeModule writes a small module: domain owns an interface and a struct,
// infra implements the interface and holds the struct, and app is not in
// the arch-lint config.
func writeModule(t *testing.T) string {
	t.Helper()
	files := map[string]string{
		"go.mod": "module example.com/m\n\ngo 1.24\n",
		".go-arch-lint.yml": `version: 3
components:
  # Domain
  domain: { in: internal/domain }

  # Infrastructure
  infra: { in: internal/infra }

deps:
  infra:
    mayDependOn:
      - domain
`,
		"internal/domain/domain.go": `// Package domain is the core.
package domain

// Order is an order. It has an ID.
type Order struct {
	ID    string
	Lines []Line
}

// Line is one order line.
type Line struct{ SKU string }

// Status is the order state.
type Status int

const (
	Open Status = iota
	Closed
)

// Store saves orders.
type Store interface {
	Save(o *Order) error
}

// Alias names Order a second way.
type Alias = Order
`,
		"internal/infra/pg.go": `package infra

import "example.com/m/internal/domain"

// PG stores orders in postgres.
type PG struct {
	last  *domain.Order
	Named domain.Alias
}

// Save implements domain.Store.
func (p *PG) Save(o *domain.Order) error { p.last = o; return nil }

// Load returns the last order.
func (p *PG) Load(id string) (domain.Line, error) { return domain.Line{}, nil }
`,
		"internal/app/app.go": `package app

import "example.com/m/internal/infra"

// New wires the app.
func New() *infra.PG { return &infra.PG{} }
`,
	}
	return writeFiles(t, files)
}

// writeFiles writes files, keyed by slash path, into a new temp directory.
func writeFiles(t *testing.T, files map[string]string) string {
	t.Helper()
	root := t.TempDir()
	for name, src := range files {
		p := filepath.Join(root, filepath.FromSlash(name))
		if err := os.MkdirAll(filepath.Dir(p), 0o750); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(src), 0o600); err != nil {
			t.Fatal(err)
		}
	}
	return root
}

func TestExport(t *testing.T) {
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	root := writeModule(t)
	var warnings []string
	m, err := Export(context.Background(), Options{
		Root: root, ArchLint: ".go-arch-lint.yml", Patterns: []string{"./internal/..."},
		Warn: func(format string, _ ...any) { warnings = append(warnings, format) },
	})
	if err != nil {
		t.Fatal(err)
	}
	if m.Format != Format || m.Source.Module != "example.com/m" {
		t.Errorf("format %q module %q", m.Format, m.Source.Module)
	}

	t.Run("components", func(t *testing.T) {
		app, ok := m.Components["internal/app"]
		if !ok || app.Layer != UnmappedLayer || app.Path != "internal/app" {
			t.Errorf("app = %+v (present %v), want an unmapped component", app, ok)
		}
		if len(warnings) == 0 {
			t.Error("want a warning about the unmapped package")
		}
		if m.Components["domain"].Layer != "domain" || m.Components["infra"].Layer != "infrastructure" {
			t.Errorf("layers: domain %q infra %q", m.Components["domain"].Layer, m.Components["infra"].Layer)
		}
	})

	t.Run("dependencies", func(t *testing.T) {
		got := map[string]Dependency{}
		for _, d := range m.Dependencies {
			got[d.From+"->"+d.To] = d
		}
		d, ok := got["infra->domain"]
		if !ok || d.Weight != 1 || len(d.Evidence) != 1 || d.Evidence[0] != "internal/infra/pg.go:3" {
			t.Errorf("infra->domain = %+v (present %v)", d, ok)
		}
		if _, ok := got["internal/app->infra"]; !ok {
			t.Errorf("missing app->infra in %v", got)
		}
		if len(got) != 2 {
			t.Errorf("dependencies = %v, want 2", got)
		}
	})

	t.Run("types", func(t *testing.T) {
		order := m.Types["internal/domain.Order"]
		if order.Kind != "struct" || order.Doc != "Order is an order." || order.File != "internal/domain/domain.go" {
			t.Errorf("Order = %+v", order)
		}
		if len(order.Fields) != 2 || order.Fields[0].Type != "string" || order.Fields[1].Ref != "internal/domain.Line" || !order.Fields[1].Many {
			t.Errorf("Order fields = %+v", order.Fields)
		}
		if k := m.Types["internal/domain.Status"].Kind; k != "enum" {
			t.Errorf("Status kind = %q", k)
		}
		alias := m.Types["internal/domain.Alias"]
		if alias.Kind != "alias" || !slices.Equal(alias.Uses, []string{"internal/domain.Order"}) {
			t.Errorf("Alias = %+v", alias)
		}
		pg := m.Types["internal/infra.PG"]
		if !slices.Contains(pg.Implements, "internal/domain.Store") {
			t.Errorf("PG implements %v, want domain.Store", pg.Implements)
		}
		// Unexported fields stay: they are where a type holds its dependencies.
		if len(pg.Fields) != 2 || pg.Fields[0].Ref != "internal/domain.Order" || !pg.Fields[0].Ptr || pg.Fields[1].Ref != "internal/domain.Alias" {
			t.Errorf("PG fields = %+v, want *Order and the alias", pg.Fields)
		}
		if !slices.Contains(pg.Uses, "internal/domain.Line") || slices.Contains(pg.Uses, "internal/domain.Alias") {
			t.Errorf("PG uses %v, want Line and not the held Alias", pg.Uses)
		}
		if f := m.Types["internal/app.New"]; f.Name != "" {
			t.Errorf("a function is not a type: %+v", f)
		}
	})

	t.Run("policy", func(t *testing.T) {
		if m.Policy.Default != Deny || len(m.Policy.Rules) != 1 || m.Policy.Rules[0].From != "infra" {
			t.Errorf("policy = %+v", m.Policy)
		}
	})
}
