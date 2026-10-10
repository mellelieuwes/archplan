// Package order takes orders.
package order

import (
	"example.com/demo/customer"
	"example.com/demo/order/status"
)

// Order is placed by a customer.
type Order struct {
	ID       string
	Customer *customer.Customer
	State    status.Status
}
