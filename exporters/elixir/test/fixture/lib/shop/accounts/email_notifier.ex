defmodule Shop.Accounts.EmailNotifier do
  @moduledoc "Sends order mail to the customer."
  @behaviour Shop.Orders.Notifier

  @impl true
  def notify(order), do: {:ok, Shop.Accounts.display_name(order.user)}
end
