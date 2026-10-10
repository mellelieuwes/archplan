package main

import (
	"cmp"
	"errors"
	"fmt"
	"go/ast"
	"go/token"
	"go/types"
	"io/fs"
	"maps"
	"math"
	"os"
	"path"
	"path/filepath"
	"regexp"
	"slices"
	"strconv"
	"strings"
)

// Limits for the vocabulary section.
const (
	maxTermWords   = 3  // a term matches up to this many consecutive words
	minTermLen     = 3  // shorter vendor terms and suggested words are skipped
	minSuggestTF   = 3  // a suggested word occurs at least this often in its component
	maxSuggestions = 10 // per component
	maxDFDivisor   = 4  // a suggested word occurs in at most N/4 components
)

// genericTerms are path segments too common to stand for a vendor.
var genericTerms = setOf(
	"go", "golang", "x", "sdk", "api", "lib", "util", "utils", "yaml", "json", "sync", "errors",
	"net", "sys", "http", "http2", "html", "image", "color", "text", "time", "log", "io", "os",
	"core", "common", "client", "server", "mail", "crypto", "term", "exp", "tool", "mod", "pkg",
	"cmd", "internal", "proto", "protocol", "v2", "v3", "v4", "v5",
)

// genericWords are Go identifier words too common to suggest as domain terms.
var genericWords = setOf(
	"get", "set", "new", "err", "error", "string", "int", "int64", "bool", "byte", "ctx", "context",
	"name", "type", "value", "key", "list", "map", "test", "func", "handler", "config", "option",
	"result", "data", "item", "info", "the", "and", "for", "with", "from", "all", "add", "remove",
	"delete", "update", "create", "read", "write", "load", "save", "find", "make", "build", "run",
	"start", "stop", "close", "open", "init", "check", "parse", "format", "valid", "validate", "path",
	"file", "dir", "len", "count", "index", "min", "max", "opt", "arg", "param", "request",
	"response", "req", "resp", "url", "json", "yaml", "http", "server", "client", "service", "manager",
	"default", "must", "should", "has", "not", "can", "use", "call", "return", "input", "output",
	"state", "status", "mode", "kind", "field", "store", "cache", "event", "message", "text", "body",
	"line", "time", "date", "size", "limit", "id", "ids", "uri", "api", "util", "helper",
	"wrap", "into", "next", "prev", "first", "last", "old", "one", "two", "base", "raw",
	"ok", "fn", "cb", "buf", "src", "dst", "out", "tmp", "val", "var", "obj", "ref", "node",
)

func setOf(words ...string) map[string]bool {
	m := make(map[string]bool, len(words))
	for _, w := range words {
		m[w] = true
	}
	return m
}

// Usage channels.
const (
	channelIdent = iota
	channelString
	channelComment
)

type usageKey struct{ comp, concept string }

type vocabBuilder struct {
	x      *extractor
	v      *Vocabulary
	gloss  map[string]*Glossary // component ID → its glossary
	byKey  map[string][]string  // term key → concept IDs
	usage  map[usageKey]*Usage
	idents map[string][]string // identifier → concept IDs it names
}

// vocabulary builds the vocabulary section from GLOSSARY.md files, vendor
// components and the parsed sources.
func (x *extractor) vocabulary() {
	b := &vocabBuilder{
		x:      x,
		v:      &Vocabulary{Concepts: map[string]Concept{}, Components: map[string]ComponentTerms{}},
		gloss:  map[string]*Glossary{},
		byKey:  map[string][]string{},
		usage:  map[usageKey]*Usage{},
		idents: map[string][]string{},
	}
	b.glossaries()
	b.vendors()
	b.index()
	b.references()
	b.scan()
	b.suggest()
	x.res.Vocabulary = b.v
}

func (b *vocabBuilder) warn(format string, args ...any) {
	b.x.res.Warnings = append(b.x.res.Warnings, fmt.Sprintf(format, args...))
}

func (b *vocabBuilder) components(kind string) []string {
	var ids []string
	for id, c := range b.x.ix.comps {
		if c.Kind == kind || (kind == KindInternal && c.Kind == "") {
			ids = append(ids, id)
		}
	}
	slices.Sort(ids)
	return ids
}

// glossaries reads each internal component's GLOSSARY.md.
func (b *vocabBuilder) glossaries() {
	for _, id := range b.components(KindInternal) {
		c := b.x.ix.comps[id]
		if c.Path == "" {
			continue
		}
		file := path.Join(c.Path, GlossaryFile)
		src, err := os.ReadFile(filepath.Join(b.x.opt.Root, filepath.FromSlash(file)))
		if errors.Is(err, fs.ErrNotExist) {
			continue
		}
		if err != nil {
			b.warn("%s: %v", file, err)
			continue
		}
		g, err := ParseGlossary(src)
		if err != nil {
			b.warn("%s: %v", file, err)
			continue
		}
		b.gloss[id] = g
		b.v.Components[id] = ComponentTerms{File: file}
		for _, name := range slices.Sorted(maps.Keys(g.Owns)) {
			cid := ConceptID(name)
			if prev, ok := b.v.Concepts[cid]; ok {
				b.warn("%s: concept %q is already owned by %s", file, name, prev.Owner)
				continue
			}
			b.v.Concepts[cid] = Concept{
				Name: name, Owner: id, Terms: dedupeTerms(append([]string{name}, g.Owns[name]...)),
				Kind: ConceptDomain, Definition: g.Definitions[cid], Source: SourceGlossary,
			}
		}
	}
}

// vendors adds a concept for each external component, named by its ID and
// the last segments of its import paths.
func (b *vocabBuilder) vendors() {
	internal := map[string]bool{}
	for _, id := range b.components(KindInternal) {
		internal[termKey(id)] = true
	}
	for _, id := range b.components(KindExternal) {
		c := b.x.ix.comps[id]
		cands := []string{id}
		for _, p := range append([]string{c.Path}, c.Match...) {
			if seg := lastSegment(p); seg != "" {
				cands = append(cands, seg)
			}
		}
		var terms []string
		for _, t := range dedupeTerms(cands) {
			k := termKey(t)
			if len(k) >= minTermLen && !genericTerms[k] && !internal[k] {
				terms = append(terms, t)
			}
		}
		if len(terms) == 0 {
			continue
		}
		cid := ConceptID(id)
		if _, ok := b.v.Concepts[cid]; ok {
			cid += "-vendor"
		}
		b.v.Concepts[cid] = Concept{Name: id, Owner: id, Terms: terms, Kind: ConceptVendor, Source: SourceVendor}
	}
}

var versionSuffix = regexp.MustCompile(`\.v\d+$`)

// lastSegment returns the name an import path is known by:
// "github.com/emersion/go-webdav/**" → "webdav", "gopkg.in/yaml.v3" → "yaml".
func lastSegment(p string) string {
	segs := strings.Split(strings.TrimSuffix(strings.TrimSuffix(p, "/**"), "/*"), "/")
	for len(segs) > 1 && (segs[len(segs)-1] == "*" || isMajorVersion(segs[len(segs)-1])) {
		segs = segs[:len(segs)-1]
	}
	s := versionSuffix.ReplaceAllString(segs[len(segs)-1], "")
	s = strings.TrimPrefix(s, "go-")
	return strings.TrimSuffix(s, "-go")
}

func isMajorVersion(s string) bool {
	_, err := strconv.Atoi(strings.TrimPrefix(s, "v"))
	return strings.HasPrefix(s, "v") && err == nil
}

// dedupeTerms drops terms whose key an earlier term already has.
func dedupeTerms(terms []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, t := range terms {
		t = strings.TrimSpace(t)
		if k := termKey(t); k != "" && !seen[k] {
			seen[k] = true
			out = append(out, t)
		}
	}
	return out
}

func (b *vocabBuilder) index() {
	for _, cid := range slices.Sorted(maps.Keys(b.v.Concepts)) {
		for _, t := range b.v.Concepts[cid].Terms {
			k := termKey(t)
			if !slices.Contains(b.byKey[k], cid) {
				b.byKey[k] = append(b.byKey[k], cid)
			}
		}
	}
}

// references resolves the uses and avoid lists of each glossary to concept
// IDs. A name is a concept name or one of its terms.
func (b *vocabBuilder) references() {
	for id, g := range b.gloss {
		ct := b.v.Components[id]
		ct.Uses = b.resolve(ct.File, g.Uses)
		ct.Avoid = b.resolve(ct.File, g.Avoid)
		b.v.Components[id] = ct
	}
}

func (b *vocabBuilder) resolve(file string, names []string) []string {
	var ids []string
	for _, n := range names {
		cid := ConceptID(n)
		if _, ok := b.v.Concepts[cid]; !ok {
			if found := b.byKey[termKey(n)]; len(found) == 1 {
				cid = found[0]
			} else {
				b.warn("%s: unknown concept %q", file, n)
				continue
			}
		}
		if !slices.Contains(ids, cid) {
			ids = append(ids, cid)
		}
	}
	slices.Sort(ids)
	return ids
}

// scan counts, per component, the identifiers, string literals and comments
// that name a concept the component does not own.
func (b *vocabBuilder) scan() {
	for _, p := range b.x.pkgs {
		for _, f := range p.files {
			if isTestFile(b.x.fset, f) {
				continue
			}
			b.scanFile(p, f)
		}
	}
	keys := slices.SortedFunc(maps.Keys(b.usage), func(a, c usageKey) int {
		return cmp.Or(strings.Compare(a.comp, c.comp), strings.Compare(a.concept, c.concept))
	})
	for _, k := range keys {
		b.v.Usage = append(b.v.Usage, *b.usage[k])
	}
}

func (b *vocabBuilder) scanFile(p *goPkg, f *ast.File) {
	skip := map[ast.Node]bool{}
	for _, d := range f.Decls {
		if g, ok := d.(*ast.GenDecl); ok && g.Tok == token.IMPORT {
			continue
		}
		ast.Inspect(d, func(n ast.Node) bool {
			b.visit(p, skip, n)
			return true
		})
	}
	for _, cg := range f.Comments {
		for _, c := range cg.List {
			b.record(p.comp, channelComment, b.match(c.Text), c.Pos())
		}
	}
}

// visit records one identifier or string literal. skip holds the nodes that
// are not uses: package qualifiers and struct tags.
func (b *vocabBuilder) visit(p *goPkg, skip map[ast.Node]bool, n ast.Node) {
	switch n := n.(type) {
	case *ast.SelectorExpr:
		// The package name in pgx.Connect is the import, not a use.
		if id, ok := n.X.(*ast.Ident); ok && isPkgName(p.info, id) {
			skip[id] = true
		}
	case *ast.Field:
		if n.Tag != nil {
			skip[n.Tag] = true
		}
	case *ast.Ident:
		if !skip[n] {
			b.record(p.comp, channelIdent, b.identConcepts(n.Name), n.Pos())
		}
	case *ast.BasicLit:
		if n.Kind == token.STRING && !skip[n] {
			s, err := strconv.Unquote(n.Value)
			if err != nil {
				s = n.Value
			}
			b.record(p.comp, channelString, b.match(s), n.Pos())
		}
	}
}

func isPkgName(info *types.Info, id *ast.Ident) bool {
	if info == nil {
		return false
	}
	_, ok := info.Uses[id].(*types.PkgName)
	return ok
}

func (b *vocabBuilder) identConcepts(name string) []string {
	ids, ok := b.idents[name]
	if !ok {
		ids = b.match(name)
		b.idents[name] = ids
	}
	return ids
}

// match returns the concepts named in text: a term matches when its key
// equals one to three consecutive stemmed words.
func (b *vocabBuilder) match(text string) []string {
	words := splitWords(text)
	for i, w := range words {
		words[i] = stem(w)
	}
	var ids []string
	for i := range words {
		for n := 1; n <= maxTermWords && i+n <= len(words); n++ {
			for _, cid := range b.byKey[strings.Join(words[i:i+n], "")] {
				if !slices.Contains(ids, cid) {
					ids = append(ids, cid)
				}
			}
		}
	}
	return ids
}

func (b *vocabBuilder) record(comp string, channel int, concepts []string, pos token.Pos) {
	for _, cid := range concepts {
		if b.v.Concepts[cid].Owner == comp {
			continue
		}
		k := usageKey{comp, cid}
		u := b.usage[k]
		if u == nil {
			u = &Usage{Component: comp, Concept: cid}
			b.usage[k] = u
		}
		switch channel {
		case channelIdent:
			u.Idents++
		case channelString:
			u.Strings++
		default:
			u.Comments++
		}
		if len(u.Evidence) < maxEvidence {
			if w := b.x.where(pos); !slices.Contains(u.Evidence, w) {
				u.Evidence = append(u.Evidence, w)
			}
		}
	}
}

// suggest scores the words of declared names (types, functions, methods and
// fields) per internal component by tf-idf, and keeps the top words that are
// not yet a concept term.
func (b *vocabBuilder) suggest() {
	tf := b.declaredWords()
	df := map[string]int{}
	for _, words := range tf {
		for w := range words {
			df[w]++
		}
	}
	n := len(tf)
	for _, comp := range slices.Sorted(maps.Keys(tf)) {
		var sug []Suggestion
		for w, count := range tf[comp] {
			if count < minSuggestTF || df[w]*maxDFDivisor > n || len(b.byKey[w]) > 0 {
				continue
			}
			score := float64(count) * math.Log(float64(n)/float64(df[w]))
			sug = append(sug, Suggestion{Term: w, Score: math.Round(score*10) / 10})
		}
		if len(sug) == 0 {
			continue
		}
		slices.SortFunc(sug, func(a, c Suggestion) int {
			return cmp.Or(cmp.Compare(c.Score, a.Score), strings.Compare(a.Term, c.Term))
		})
		ct := b.v.Components[comp]
		ct.Suggested = sug[:min(len(sug), maxSuggestions)]
		b.v.Components[comp] = ct
	}
}

// declaredWords counts the words of declared names per component.
func (b *vocabBuilder) declaredWords() map[string]map[string]int {
	tf := map[string]map[string]int{}
	for _, p := range b.x.pkgs {
		for _, f := range p.files {
			if isTestFile(b.x.fset, f) {
				continue
			}
			for _, name := range declaredNames(f) {
				for _, w := range splitWords(name) {
					w = stem(w)
					if len(w) < minTermLen || genericWords[w] || isNumber(w) {
						continue
					}
					if tf[p.comp] == nil {
						tf[p.comp] = map[string]int{}
					}
					tf[p.comp][w]++
				}
			}
		}
	}
	return tf
}

func isTestFile(fset *token.FileSet, f *ast.File) bool {
	return strings.HasSuffix(fset.File(f.Pos()).Name(), "_test.go")
}

func isNumber(s string) bool {
	_, err := strconv.Atoi(s)
	return err == nil
}

// declaredNames lists the names a file declares: types, functions, methods,
// struct fields and interface methods.
func declaredNames(f *ast.File) []string {
	var names []string
	for _, d := range f.Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			names = append(names, d.Name.Name)
		case *ast.GenDecl:
			if d.Tok != token.TYPE {
				continue
			}
			for _, s := range d.Specs {
				if ts, ok := s.(*ast.TypeSpec); ok {
					names = append(names, ts.Name.Name)
					names = append(names, memberNames(ts.Type)...)
				}
			}
		}
	}
	return names
}

func memberNames(e ast.Expr) []string {
	var names []string
	ast.Inspect(e, func(n ast.Node) bool {
		var fields *ast.FieldList
		switch n := n.(type) {
		case *ast.FuncType:
			return false
		case *ast.StructType:
			fields = n.Fields
		case *ast.InterfaceType:
			fields = n.Methods
		}
		if fields != nil {
			for _, fld := range fields.List {
				for _, id := range fld.Names {
					names = append(names, id.Name)
				}
			}
		}
		return true
	})
	return names
}
