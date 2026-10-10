defmodule Shop.Orders.Notifier do
  @moduledoc "Tells someone about a placed order."
  @callback notify(Shop.Orders.Order.t()) :: {:ok, String.t()} | {:error, term()}
end
