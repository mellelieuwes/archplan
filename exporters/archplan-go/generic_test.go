package main

import (
	"context"
	"path/filepath"
	"slices"
	"testing"
)

// exportDemo exports testdata/demo, a module without a go-arch-lint config:
// order holds a customer, and customer and order both use order/status.
func exportDemo(t *testing.T, depth int) *Model {
	t.Helper()
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	root, err := filepath.Abs(filepath.Join("testdata", "demo"))
	if err != nil {
		t.Fatal(err)
	}
	m, err := Export(context.Background(), Options{Root: root, Patterns: []string{"./..."}, Depth: depth})
	if err != nil {
		t.Fatal(err)
	}
	return m
}

func depSet(m *Model) []string {
	var out []string
	for _, d := range m.Dependencies {
		out = append(out, d.From+"->"+d.To)
	}
	return out
}

func TestExportWithoutArchLint(t *testing.T) {
	m := exportDemo(t, 0)
	if m.Source.Module != "example.com/demo" || m.Source.Generator != Generator {
		t.Errorf("source = %+v", m.Source)
	}
	if _, ok := m.Source.Commands["rules"]; ok {
		t.Error("a rules command without a go-arch-lint config")
	}

	t.Run("components", func(t *testing.T) {
		want := []string{"customer", "order", "order/status"}
		var got []string
		for id, c := range m.Components {
			got = append(got, id)
			if c.Path != id || !slices.Equal(c.Match, []string{id}) || c.Kind != KindInternal || c.Layer != "" {
				t.Errorf("component %s = %+v", id, c)
			}
		}
		slices.Sort(got)
		if !slices.Equal(got, want) {
			t.Errorf("components = %v, want %v", got, want)
		}
		if len(m.Layers) != 0 {
			t.Errorf("layers = %+v, want none", m.Layers)
		}
	})

	t.Run("dependencies", func(t *testing.T) {
		want := []string{"customer->order/status", "order->customer", "order->order/status"}
		if got := depSet(m); !slices.Equal(got, want) {
			t.Errorf("dependencies = %v, want %v", got, want)
		}
	})

	t.Run("policy", func(t *testing.T) {
		if m.Policy == nil || m.Policy.Default != Allow || m.Policy.Rules == nil || len(m.Policy.Rules) != 0 || m.Policy.Enforcer != nil {
			t.Errorf("policy = %+v, want allow with no rules", m.Policy)
		}
	})

	t.Run("types", func(t *testing.T) {
		if len(m.Types) != 3 {
			t.Errorf("types = %v, want 3", len(m.Types))
		}
		order := m.Types["order.Order"]
		if order.Kind != "struct" || order.Component != "order" || len(order.Fields) != 3 {
			t.Fatalf("Order = %+v", order)
		}
		if f := order.Fields[1]; f.Ref != "customer.Customer" || !f.Ptr {
			t.Errorf("Order.Customer = %+v, want *customer.Customer", f)
		}
		if f := order.Fields[2]; f.Ref != "order/status.Status" || f.Ptr {
			t.Errorf("Order.State = %+v", f)
		}
		if k := m.Types["order/status.Status"].Kind; k != "enum" {
			t.Errorf("Status kind = %q", k)
		}
		if u := m.Types["customer.Customer"].Uses; !slices.Equal(u, []string{"order/status.Status"}) {
			t.Errorf("Customer uses %v", u)
		}
	})
}

func TestExportDepthMakesCycle(t *testing.T) {
	m := exportDemo(t, 1)
	order, ok := m.Components["order"]
	if !ok || !slices.Equal(order.Match, []string{"order", "order/**"}) || len(m.Components) != 2 {
		t.Errorf("components = %+v", m.Components)
	}
	want := []string{"customer->order", "order->customer"}
	if got := depSet(m); !slices.Equal(got, want) {
		t.Errorf("dependencies = %v, want the cycle %v", got, want)
	}
	if c := m.Types["order/status.Status"].Component; c != "order" {
		t.Errorf("Status component = %q, want order", c)
	}
}

func TestExportExclude(t *testing.T) {
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	root, _ := filepath.Abs(filepath.Join("testdata", "demo"))
	m, err := Export(context.Background(), Options{Root: root, Patterns: []string{"./..."}, Exclude: []string{"order"}})
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := m.Components["order/status"]; ok || len(m.Components) != 1 {
		t.Errorf("components = %+v, want only customer", m.Components)
	}
}

func TestGroupOf(t *testing.T) {
	tests := []struct {
		rel   string
		depth int
		want  string
	}{
		{"internal/store/pg", 0, "internal/store/pg"},
		{"internal/store/pg", 1, "internal"},
		{"internal/store/pg", 2, "internal/store"},
		{"cmd", 2, "cmd"},
		{".", 1, "."},
	}
	for _, tc := range tests {
		if got := groupOf(tc.rel, tc.depth); got != tc.want {
			t.Errorf("groupOf(%q, %d) = %q, want %q", tc.rel, tc.depth, got, tc.want)
		}
	}
}

func TestArchLintConfig(t *testing.T) {
	dir := t.TempDir()
	for flagValue, want := range map[string]string{"": "", "none": "", "x.yml": "x.yml"} {
		if got, _ := archLintConfig(dir, flagValue); got != want {
			t.Errorf("archLintConfig(%q) = %q, want %q", flagValue, got, want)
		}
	}
	root := writeFiles(t, map[string]string{defaultArchLint: "version: 3\n"})
	if got, _ := archLintConfig(root, ""); got != defaultArchLint {
		t.Errorf("archLintConfig with a config = %q", got)
	}
	if got, _ := archLintConfig(root, "none"); got != "" {
		t.Errorf("archLintConfig none = %q", got)
	}
}

func TestGoModule(t *testing.T) {
	root := writeFiles(t, map[string]string{"go.mod": "// comment\nmodule \"example.com/q\" // trailing\n\ngo 1.24\n"})
	if got, err := goModule(root); err != nil || got != "example.com/q" {
		t.Errorf("goModule = %q, %v", got, err)
	}
	if _, err := goModule(t.TempDir()); err == nil {
		t.Error("want an error without go.mod")
	}
}
