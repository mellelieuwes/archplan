package main

import (
	"slices"
	"testing"
)

func TestParseGlossary(t *testing.T) {
	src := `---
owns:
  cart: [basket]
  cart item: []
uses: [product]
avoid: [stripe]
---
# Cart

**Cart**: the items a shopper intends to buy.
- **Cart item**: one product line in a cart.
`
	g, err := ParseGlossary([]byte(src))
	if err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(g.Owns["cart"], []string{"basket"}) || len(g.Owns) != 2 {
		t.Errorf("owns = %v", g.Owns)
	}
	if !slices.Equal(g.Uses, []string{"product"}) || !slices.Equal(g.Avoid, []string{"stripe"}) {
		t.Errorf("uses %v avoid %v", g.Uses, g.Avoid)
	}
	if g.Definitions["cart"] != "the items a shopper intends to buy." ||
		g.Definitions["cart-item"] != "one product line in a cart." {

		t.Errorf("definitions = %v", g.Definitions)
	}
}

func TestParseGlossaryWithoutFrontMatter(t *testing.T) {
	g, err := ParseGlossary([]byte("**Order**: what a shopper buys.\n"))
	if err != nil {
		t.Fatal(err)
	}
	if len(g.Owns) != 0 || g.Definitions["order"] != "what a shopper buys." {
		t.Errorf("glossary = %+v", g)
	}
}

func TestParseGlossaryRejectsBadFrontMatter(t *testing.T) {
	for name, src := range map[string]string{
		"unclosed":     "---\nowns: {cart: []}\n",
		"invalid yaml": "---\nowns: [\n---\n",
		"wrong shape":  "---\nowns: [cart]\n---\n",
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := ParseGlossary([]byte(src)); err == nil {
				t.Error("want an error")
			}
		})
	}
}

func TestSplitWords(t *testing.T) {
	tests := []struct {
		in   string
		want []string
	}{
		{"parseHTTPRequest_v2", []string{"parse", "http", "request", "v2"}},
		{"CartItem", []string{"cart", "item"}},
		{"CART_ITEM", []string{"cart", "item"}},
		{"cart item", []string{"cart", "item"}},
		{"// Uses stripe.", []string{"uses", "stripe"}},
		{"HTTPServer", []string{"http", "server"}},
		{"id", []string{"id"}},
		{"", nil},
	}
	for _, tc := range tests {
		t.Run(tc.in, func(t *testing.T) {
			if got := splitWords(tc.in); !slices.Equal(got, tc.want) {
				t.Errorf("splitWords(%q) = %v, want %v", tc.in, got, tc.want)
			}
		})
	}
}

func TestTermKey(t *testing.T) {
	for _, in := range []string{"Cart item", "cartItems", "CART_ITEM", "cart-items"} {
		if got := termKey(in); got != "cartitem" {
			t.Errorf("termKey(%q) = %q, want cartitem", in, got)
		}
	}
	if got := termKey("categories"); got != "category" {
		t.Errorf("termKey(categories) = %q", got)
	}
	if got := termKey("address"); got != "address" {
		t.Errorf("termKey(address) = %q", got)
	}
}

func TestLastSegment(t *testing.T) {
	tests := map[string]string{
		"github.com/jackc/pgx/**":         "pgx",
		"github.com/jackc/pgx/v5":         "pgx",
		"gopkg.in/yaml.v3":                "yaml",
		"github.com/emersion/go-webdav":   "webdav",
		"github.com/teambition/rrule-go":  "rrule",
		"example.com/stripe-go/v76":       "stripe",
		"github.com/modelcontextprotocol": "modelcontextprotocol",
	}
	for in, want := range tests {
		if got := lastSegment(in); got != want {
			t.Errorf("lastSegment(%q) = %q, want %q", in, got, want)
		}
	}
}
