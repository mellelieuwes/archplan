// Package status holds the order states.
package status

// Status is the state of an order.
type Status int

// The order states.
const (
	Open Status = iota
	Shipped
)
