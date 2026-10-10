package main

import (
	"context"
	"slices"
	"testing"
)

// writeShop writes a module where cart owns a glossary, catalog owns
// product, and cart leaks the stripe vendor in an identifier and a comment.
func writeShop(t *testing.T) string {
	t.Helper()
	return writeFiles(t, map[string]string{
		"go.mod": `module example.com/shop

go 1.24

require example.com/stripe-go/v76 v76.0.0

replace example.com/stripe-go/v76 => ./third_party/stripe
`,
		"third_party/stripe/go.mod": "module example.com/stripe-go/v76\n\ngo 1.24\n",
		"third_party/stripe/stripe.go": `package stripe

// Charge charges a card.
func Charge(ref string) error { return nil }
`,
		".go-arch-lint.yml": `version: 3
components:
  cart: { in: internal/cart }
  catalog: { in: internal/catalog }
  pay: { in: internal/pay }
  promo: { in: internal/promo }
  report: { in: internal/report }
vendors:
  stripe: { in: example.com/stripe-go/v76 }
deps:
  cart:
    mayDependOn: [catalog]
    canUse: [stripe]
`,
		"internal/cart/GLOSSARY.md": `---
owns:
  cart: [basket]
  cart item: []
uses: [product]
avoid: [stripe]
---
**Cart**: the items a shopper intends to buy.
`,
		"internal/cart/cart.go": `package cart

import "example.com/stripe-go/v76"

// Cart holds items. It is paid through stripe.
type Cart struct {
	Items     []CartItem
	ProductID string ` + "`json:\"stripe\"`" + `
}

// CartItem is one line.
type CartItem struct{ SKU string }

// Pay pays the cart.
func (c *Cart) Pay() error { return stripe.Charge(stripeKey()) }

func stripeKey() string { return "key" }
`,
		"internal/catalog/GLOSSARY.md": "---\nowns:\n  product: []\n---\n",
		"internal/catalog/catalog.go": `package catalog

// Product is for sale.
type Product struct{ Title string }
`,
		"internal/pay/pay.go": `package pay

// Payment settles a cart.
type Payment struct{ Amount int }

// ChargeCart charges a basket.
func ChargeCart(cartItems []string) {}
`,
		"internal/promo/promo.go": `package promo

// Voucher gives a discount.
type Voucher struct{ VoucherCode string }

// RedeemVoucher uses a voucher.
func RedeemVoucher(v Voucher) {}
`,
		"internal/report/report.go": `package report

// Report sums sales.
type Report struct{ Total int }
`,
	})
}

func TestExportVocabulary(t *testing.T) {
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	root := writeShop(t)
	var warnings []string
	m, err := Export(context.Background(), Options{
		Root: root, ArchLint: ".go-arch-lint.yml", Patterns: []string{"./internal/..."},
		Warn: func(format string, _ ...any) { warnings = append(warnings, format) },
	})
	if err != nil {
		t.Fatal(err)
	}
	v := m.Vocabulary
	if v == nil {
		t.Fatalf("no vocabulary; warnings %v", warnings)
	}

	t.Run("concepts", func(t *testing.T) {
		cart := v.Concepts["cart"]
		if cart.Owner != "cart" || cart.Kind != ConceptDomain || cart.Source != SourceGlossary ||
			!slices.Equal(cart.Terms, []string{"cart", "basket"}) ||
			cart.Definition != "the items a shopper intends to buy." {

			t.Errorf("cart = %+v", cart)
		}
		if c := v.Concepts["cart-item"]; c.Owner != "cart" {
			t.Errorf("cart-item = %+v", c)
		}
		stripe := v.Concepts["stripe"]
		if stripe.Owner != "stripe" || stripe.Kind != ConceptVendor || !slices.Equal(stripe.Terms, []string{"stripe"}) {
			t.Errorf("stripe = %+v", stripe)
		}
	})

	t.Run("components", func(t *testing.T) {
		ct := v.Components["cart"]
		if ct.File != "internal/cart/GLOSSARY.md" || !slices.Equal(ct.Uses, []string{"product"}) ||
			!slices.Equal(ct.Avoid, []string{"stripe"}) {

			t.Errorf("cart terms = %+v", ct)
		}
	})

	t.Run("usage", func(t *testing.T) {
		got := map[string]Usage{}
		for _, u := range v.Usage {
			got[u.Component+">"+u.Concept] = u
		}
		// The import and the stripe qualifier are not uses; stripeKey is, twice.
		s, ok := got["cart>stripe"]
		if !ok || s.Idents != 2 || s.Comments != 1 || s.Strings != 0 {
			t.Errorf("cart>stripe = %+v (present %v)", s, ok)
		}
		if !slices.Contains(s.Evidence, "internal/cart/cart.go:5") || !slices.Contains(s.Evidence, "internal/cart/cart.go:17") {
			t.Errorf("cart>stripe evidence = %v", s.Evidence)
		}
		if _, ok := got["cart>cart"]; ok {
			t.Error("a component's use of its own concept is reported")
		}
		if u := got["cart>product"]; u.Idents != 1 {
			t.Errorf("cart>product = %+v", u)
		}
		if u := got["pay>cart"]; u.Idents != 2 || u.Comments != 2 {
			t.Errorf("pay>cart = %+v", u)
		}
		if u := got["pay>cart-item"]; u.Idents != 1 {
			t.Errorf("pay>cart-item = %+v", u)
		}
	})

	t.Run("suggestions", func(t *testing.T) {
		sug := v.Components["promo"].Suggested
		if len(sug) == 0 || sug[0].Term != "voucher" || sug[0].Score != 4.8 {
			t.Errorf("promo suggestions = %+v", sug)
		}
		for _, s := range v.Components["cart"].Suggested {
			if s.Term == "cart" {
				t.Error("a concept term is suggested")
			}
		}
	})
}

func TestExportNoVocab(t *testing.T) {
	if testing.Short() {
		t.Skip("runs go list -export")
	}
	m, err := Export(context.Background(), Options{
		Root: writeShop(t), ArchLint: ".go-arch-lint.yml", Patterns: []string{"./internal/..."},
		NoVocab: true, Warn: func(string, ...any) {},
	})
	if err != nil {
		t.Fatal(err)
	}
	if m.Vocabulary != nil {
		t.Errorf("vocabulary = %+v, want none", m.Vocabulary)
	}
}
