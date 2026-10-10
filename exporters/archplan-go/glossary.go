package main

import (
	"bytes"
	"errors"
	"fmt"
	"regexp"
	"strings"
	"unicode"

	"gopkg.in/yaml.v3"
)

// GlossaryFile is the name of a component's glossary, in the component's
// directory.
const GlossaryFile = "GLOSSARY.md"

// Glossary is a parsed GLOSSARY.md. The YAML front matter says which concepts
// the component owns, which concepts of other components it may name, and
// which it must not name. The Markdown body defines concepts in lines of the
// form "**Name**: definition".
//
//	---
//	owns:
//	  cart: [basket]    # concept name: extra terms
//	  cart item: []
//	uses: [product]
//	avoid: [stripe]
//	---
//	**Cart**: the items a shopper intends to buy.
type Glossary struct {
	Owns        map[string][]string `yaml:"owns"`
	Uses        []string            `yaml:"uses"`
	Avoid       []string            `yaml:"avoid"`
	Definitions map[string]string   `yaml:"-"` // concept ID → definition
}

var definitionLine = regexp.MustCompile(`(?m)^\s*(?:[-*]\s+)?\*\*([^*]+)\*\*\s*:\s*(.+)$`)

// ParseGlossary reads a GLOSSARY.md. A file without front matter has only
// definitions.
func ParseGlossary(src []byte) (*Glossary, error) {
	g := &Glossary{}
	body := src
	if rest, ok := bytes.CutPrefix(src, []byte("---\n")); ok {
		front, after, found := bytes.Cut(rest, []byte("\n---"))
		if !found {
			return nil, errors.New("front matter has no closing ---")
		}
		if err := yaml.Unmarshal(front, g); err != nil {
			return nil, fmt.Errorf("front matter: %w", err)
		}
		body = after
	}
	g.Definitions = map[string]string{}
	for _, m := range definitionLine.FindAllSubmatch(body, -1) {
		g.Definitions[ConceptID(string(m[1]))] = strings.TrimSpace(string(m[2]))
	}
	return g, nil
}

// ConceptID turns a concept name into its ID: "Cart item" → "cart-item".
func ConceptID(name string) string { return slug(name) }

// splitWords splits identifiers and prose into lowercase words:
// "parseHTTPRequest_v2" → parse, http, request, v2.
func splitWords(s string) []string {
	var words []string
	rs := []rune(s)
	start := -1
	flush := func(end int) {
		if start >= 0 {
			words = append(words, strings.ToLower(string(rs[start:end])))
			start = -1
		}
	}
	for i, r := range rs {
		if !unicode.IsLetter(r) && !unicode.IsDigit(r) {
			flush(i)
			continue
		}
		if start >= 0 && unicode.IsUpper(r) {
			prevLower := unicode.IsLower(rs[i-1]) || unicode.IsDigit(rs[i-1])
			nextLower := i+1 < len(rs) && unicode.IsLower(rs[i+1])
			if prevLower || (unicode.IsUpper(rs[i-1]) && nextLower) {
				flush(i)
			}
		}
		if start < 0 {
			start = i
		}
	}
	flush(len(rs))
	return words
}

// stem folds a plural into its singular, roughly: "carts" → "cart".
func stem(w string) string {
	switch {
	case len(w) > 4 && strings.HasSuffix(w, "ies"):
		return w[:len(w)-3] + "y"
	case len(w) > 3 && strings.HasSuffix(w, "s") && !strings.HasSuffix(w, "ss"):
		return w[:len(w)-1]
	}
	return w
}

// termKey normalizes a term so that "Cart item", "cartItems" and "CART_ITEM"
// all become "cartitem".
func termKey(term string) string {
	words := splitWords(term)
	for i, w := range words {
		words[i] = stem(w)
	}
	return strings.Join(words, "")
}
