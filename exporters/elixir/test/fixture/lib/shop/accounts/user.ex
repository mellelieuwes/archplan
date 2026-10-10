defmodule Shop.Accounts.User do
  @moduledoc "A customer."
  use Ecto.Schema

  schema "users" do
    field(:email, :string)
    field(:role, Ecto.Enum, values: [:customer, :admin])
    has_many(:orders, Shop.Orders.Order)
    timestamps()
  end
end
