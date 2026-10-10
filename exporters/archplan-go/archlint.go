package main

import (
	"bufio"
	"bytes"
	"fmt"
	"maps"
	"regexp"
	"slices"
	"strings"

	"gopkg.in/yaml.v3"
)

// ArchLint is what the exporter takes from a go-arch-lint v3 config.
type ArchLint struct {
	Layers     []Layer
	Components map[string]Component
	Policy     Policy
	Exclude    []string // module-relative directories go-arch-lint skips
}

type archLintFile struct {
	Exclude          []string               `yaml:"exclude"`
	Components       map[string]archLintIn  `yaml:"components"`
	Vendors          map[string]archLintIn  `yaml:"vendors"`
	CommonComponents []string               `yaml:"commonComponents"`
	CommonVendors    []string               `yaml:"commonVendors"`
	Deps             map[string]archLintDep `yaml:"deps"`
}

type archLintIn struct {
	In patterns `yaml:"in"`
}

type archLintDep struct {
	MayDependOn    []string `yaml:"mayDependOn"`
	CanUse         []string `yaml:"canUse"`
	AnyProjectDeps bool     `yaml:"anyProjectDeps"`
	AnyVendorDeps  bool     `yaml:"anyVendorDeps"`
}

// patterns accepts a single pattern or a list.
type patterns []string

func (p *patterns) UnmarshalYAML(n *yaml.Node) error {
	if n.Kind == yaml.ScalarNode {
		*p = patterns{n.Value}
		return nil
	}
	var list []string
	if err := n.Decode(&list); err != nil {
		return fmt.Errorf("in: %w", err)
	}
	*p = list
	return nil
}

// ParseArchLint reads a go-arch-lint v3 config. Components keep their
// go-arch-lint names as IDs. Section comments in the components block (a
// comment line after a blank line) become layers; other comments become
// component docs. The config is an allowlist, so the policy defaults to deny.
func ParseArchLint(src []byte, config string) (*ArchLint, error) {
	var f archLintFile
	if err := yaml.Unmarshal(src, &f); err != nil {
		return nil, fmt.Errorf("parse %s: %w", config, err)
	}
	var root yaml.Node
	if err := yaml.Unmarshal(src, &root); err != nil {
		return nil, fmt.Errorf("parse %s: %w", config, err)
	}
	al := &ArchLint{Components: map[string]Component{}, Exclude: f.Exclude}
	sc := al.scanComments(src)
	for _, id := range slices.Sorted(maps.Keys(f.Components)) {
		in := f.Components[id].In
		al.Components[id] = Component{
			Name: id, Path: componentPath(in), Match: in, Layer: sc.layerOf[id], Kind: KindInternal,
			Doc: joinDoc(sc.docs["components."+id], sc.docs["deps."+id]),
		}
	}
	for _, id := range slices.Sorted(maps.Keys(f.Vendors)) {
		in := f.Vendors[id].In
		al.Components[id] = Component{Name: id, Path: componentPath(in), Match: in, Kind: KindExternal, Doc: sc.docs["vendors."+id]}
	}
	al.Policy = Policy{
		Default:  Deny,
		Enforcer: &Enforcer{Tool: "go-arch-lint", Config: config, Command: "go-arch-lint check"},
	}
	al.Policy.Rules = append(al.Policy.Rules, commonRules(f)...)
	al.Policy.Rules = append(al.Policy.Rules, depRules(f, depNotes(&root))...)
	return al, nil
}

func commonRules(f archLintFile) []Rule {
	rules := make([]Rule, 0, len(f.CommonComponents)+len(f.CommonVendors))
	for _, to := range f.CommonComponents {
		rules = append(rules, Rule{From: "*", To: to, Effect: Allow, Note: "commonComponents"})
	}
	for _, to := range f.CommonVendors {
		rules = append(rules, Rule{From: "*", To: to, Effect: Allow, Note: "commonVendors"})
	}
	return rules
}

func depRules(f archLintFile, notes map[ruleKey]string) []Rule {
	var rules []Rule
	for _, from := range slices.Sorted(maps.Keys(f.Deps)) {
		d := f.Deps[from]
		for _, to := range append(slices.Clone(d.MayDependOn), d.CanUse...) {
			rules = append(rules, Rule{From: from, To: to, Effect: Allow, Note: notes[ruleKey{from, to}]})
		}
		if d.AnyProjectDeps {
			rules = append(rules, Rule{From: from, To: "*", Effect: Allow, Note: "anyProjectDeps"})
		}
		if d.AnyVendorDeps {
			rules = append(rules, Rule{From: from, To: "*", Effect: Allow, Note: "anyVendorDeps"})
		}
	}
	return rules
}

type ruleKey struct{ from, to string }

// depNotes collects the comments on mayDependOn and canUse items, keyed by
// the rule they explain.
func depNotes(root *yaml.Node) map[ruleKey]string {
	out := map[ruleKey]string{}
	deps := mappingValue(root, "deps")
	if deps == nil {
		return out
	}
	for i := 0; i+1 < len(deps.Content); i += 2 {
		k, v := deps.Content[i], deps.Content[i+1]
		for _, list := range []string{"mayDependOn", "canUse"} {
			seq := mappingValue(v, list)
			if seq == nil {
				continue
			}
			for _, item := range seq.Content {
				if c := cleanComment(item.HeadComment + "\n" + item.LineComment); c != "" {
					out[ruleKey{k.Value, item.Value}] = c
				}
			}
		}
	}
	return out
}

func mappingValue(n *yaml.Node, key string) *yaml.Node {
	if n.Kind == yaml.DocumentNode && len(n.Content) > 0 {
		n = n.Content[0]
	}
	if n.Kind != yaml.MappingNode {
		return nil
	}
	for i := 0; i+1 < len(n.Content); i += 2 {
		if n.Content[i].Value == key {
			return n.Content[i+1]
		}
	}
	return nil
}

var (
	keyLine      = regexp.MustCompile(`^ {2}([A-Za-z0-9_]+):`)
	topLevel     = regexp.MustCompile(`^[A-Za-z]`)
	entryComment = regexp.MustCompile(`^ {2}#\s?(.*)$`)
)

// maxLayerTitle bounds a section comment; a longer first line after a blank
// line is prose about the next component, not a section title.
const maxLayerTitle = 48

// commentScan is what scanComments found.
type commentScan struct {
	layerOf map[string]string // component ID → layer ID
	docs    map[string]string // "block.id" → comment above that entry
}

// scanComments walks the components, vendors and deps blocks line by line,
// because yaml.v3 does not report blank lines. In the components block, a
// short comment right after a blank line (or the block's first line) is a
// section title and starts a layer. Later blocks may repeat those titles.
// Other comments at entry level document the entry below them.
func (al *ArchLint) scanComments(src []byte) commentScan {
	out := commentScan{layerOf: map[string]string{}, docs: map[string]string{}}
	var block, layer string
	var pending []string
	sectionStart := false
	sc := bufio.NewScanner(bytes.NewReader(src))
	for sc.Scan() {
		line := sc.Text()
		blank := strings.TrimSpace(line) == ""
		switch {
		case topLevel.MatchString(line):
			block, pending = strings.TrimSuffix(strings.Fields(line)[0], ":"), nil
			sectionStart = true
			continue
		case block != "components" && block != "vendors" && block != "deps":
		case blank:
			pending = nil
		case entryComment.MatchString(line):
			text := entryComment.FindStringSubmatch(line)[1]
			switch {
			case sectionStart && len(text) <= maxLayerTitle && block == "components":
				layer = al.addLayer(text)
			case sectionStart && al.hasLayer(text):
				// deps repeats the components block's section titles
			default:
				pending = append(pending, text)
			}
		case keyLine.MatchString(line):
			id := keyLine.FindStringSubmatch(line)[1]
			if block == "components" {
				out.layerOf[id] = layer
			}
			if len(pending) > 0 {
				out.docs[block+"."+id] = strings.Join(pending, " ")
			}
			pending = nil
		}
		sectionStart = blank
	}
	return out
}

func (al *ArchLint) hasLayer(name string) bool {
	return slices.ContainsFunc(al.Layers, func(l Layer) bool { return l.Name == name })
}

func (al *ArchLint) addLayer(name string) string {
	id := slug(name)
	al.Layers = append(al.Layers, Layer{ID: id, Name: name})
	return id
}

// componentPath is the first pattern without its glob suffix.
func componentPath(in []string) string {
	if len(in) == 0 {
		return ""
	}
	return strings.TrimSuffix(strings.TrimSuffix(in[0], "/**"), "/*")
}

func cleanComment(s string) string {
	var parts []string
	for line := range strings.SplitSeq(s, "\n") {
		if t := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "#")); t != "" {
			parts = append(parts, t)
		}
	}
	return strings.Join(parts, " ")
}

func joinDoc(a, b string) string {
	if a == "" || b == "" {
		return a + b
	}
	return a + "\n\n" + b
}

var nonSlug = regexp.MustCompile(`[^a-z0-9]+`)

func slug(s string) string {
	return strings.Trim(nonSlug.ReplaceAllString(strings.ToLower(s), "-"), "-")
}

// MatchPattern reports whether a package path matches a go-arch-lint "in"
// pattern. "*" matches one path segment; a trailing "/**" matches any
// non-empty tail, so "a/**" matches "a/b" but not "a" itself.
func MatchPattern(pattern, pkg string) bool {
	if base, ok := strings.CutSuffix(pattern, "/**"); ok {
		n := strings.Count(base, "/") + 1
		ks := strings.SplitN(pkg, "/", n+1)
		return len(ks) > n && matchSegments(base, strings.Join(ks[:n], "/"))
	}
	return matchSegments(pattern, pkg)
}

func matchSegments(pattern, pkg string) bool {
	ps, ks := strings.Split(pattern, "/"), strings.Split(pkg, "/")
	if len(ps) != len(ks) {
		return false
	}
	for i := range ps {
		if ps[i] != "*" && ps[i] != ks[i] {
			return false
		}
	}
	return true
}
