// Package customer knows who buys.
package customer

import "example.com/demo/order/status"

// Customer places orders.
type Customer struct {
	Name string
}

// LastState returns the state of the customer's last order.
func (c *Customer) LastState() status.Status { return status.Open }
