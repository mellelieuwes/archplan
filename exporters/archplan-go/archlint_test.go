package main

import (
	"testing"
)

func TestMatchPattern(t *testing.T) {
	tests := []struct {
		pattern, pkg string
		want         bool
	}{
		{"internal/store", "internal/store", true},
		{"internal/store", "internal/store/pgstore", false},
		{"internal/store/**", "internal/store/pgstore", true},
		{"internal/store/**", "internal/store/pgstore/sql", true},
		{"internal/store/**", "internal/store", false},
		{"internal/*/api", "internal/x/api", true},
		{"internal/*/api", "internal/x/y/api", false},
		{"internal/*/**", "internal/x/y", true},
	}
	for _, tc := range tests {
		t.Run(tc.pattern+"~"+tc.pkg, func(t *testing.T) {
			if got := MatchPattern(tc.pattern, tc.pkg); got != tc.want {
				t.Errorf("MatchPattern(%q, %q) = %v, want %v", tc.pattern, tc.pkg, got, tc.want)
			}
		})
	}
}

const archLintSample = `version: 3
exclude:
  - internal/testutil

components:
  # Entry points
  cli: { in: cmd/** }

  # Domain
  # The entity model.
  entity: { in: internal/entity }
  store: { in: [internal/store, internal/store/**] }

vendors:
  # YAML parsing.
  yaml: { in: gopkg.in/yaml.v3 }

commonComponents:
  - entity

commonVendors:
  - yaml

deps:
  cli:
    anyProjectDeps: true
  # Storage may only see the domain.
  store:
    mayDependOn:
      # needs entity IDs
      - entity
    canUse:
      - yaml
`

func TestParseArchLint(t *testing.T) {
	al, err := ParseArchLint([]byte(archLintSample), ".go-arch-lint.yml")
	if err != nil {
		t.Fatal(err)
	}
	if got := len(al.Layers); got != 2 || al.Layers[0].Name != "Entry points" || al.Layers[1].ID != "domain" {
		t.Errorf("layers = %+v, want Entry points and Domain", al.Layers)
	}
	if c := al.Components["cli"]; c.Layer != "entry-points" || c.Path != "cmd" || c.Kind != KindInternal {
		t.Errorf("cli = %+v", c)
	}
	if c := al.Components["entity"]; c.Layer != "domain" || c.Doc != "The entity model." {
		t.Errorf("entity = %+v", c)
	}
	if c := al.Components["store"]; c.Doc != "Storage may only see the domain." || len(c.Match) != 2 {
		t.Errorf("store = %+v", c)
	}
	if c := al.Components["yaml"]; c.Kind != KindExternal || c.Doc != "YAML parsing." {
		t.Errorf("yaml = %+v", c)
	}
	if len(al.Exclude) != 1 || al.Exclude[0] != "internal/testutil" {
		t.Errorf("exclude = %v", al.Exclude)
	}
	if al.Policy.Default != Deny || al.Policy.Enforcer == nil || al.Policy.Enforcer.Tool != "go-arch-lint" {
		t.Errorf("policy = %+v", al.Policy)
	}
	want := map[ruleKey]string{
		{"*", "entity"}:     "commonComponents",
		{"*", "yaml"}:       "commonVendors",
		{"cli", "*"}:        "anyProjectDeps",
		{"store", "entity"}: "needs entity IDs",
		{"store", "yaml"}:   "",
	}
	got := map[ruleKey]string{}
	for _, r := range al.Policy.Rules {
		if r.Effect != Allow {
			t.Errorf("rule %+v is not an allow rule", r)
		}
		got[ruleKey{r.From, r.To}] = r.Note
	}
	for k, note := range want {
		if g, ok := got[k]; !ok || g != note {
			t.Errorf("rule %v: note %q (present %v), want %q", k, g, ok, note)
		}
	}
	if len(got) != len(want) {
		t.Errorf("rules = %v, want %d", got, len(want))
	}
}

func TestParseArchLintRejectsBadYAML(t *testing.T) {
	if _, err := ParseArchLint([]byte("components: [\n"), "x.yml"); err == nil {
		t.Fatal("want an error")
	}
}
