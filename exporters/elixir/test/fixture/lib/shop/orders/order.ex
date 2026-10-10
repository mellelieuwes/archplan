defmodule Shop.Orders.Order do
  @moduledoc "An order placed by a user."
  use Ecto.Schema

  @type t :: %__MODULE__{}

  schema "orders" do
    field(:total, :integer)
    belongs_to(:user, Shop.Accounts.User)
  end

  defimpl Shop.Orders.Priced do
    def price(order), do: order.total
  end
end
