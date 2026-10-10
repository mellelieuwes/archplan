defmodule Shop.Orders.Cart do
  @moduledoc "Items a user collects before ordering."
  defstruct [:owner, items: []]

  @type t :: %__MODULE__{owner: Shop.Accounts.User.t() | nil, items: [Shop.Orders.Order.t()]}
end
