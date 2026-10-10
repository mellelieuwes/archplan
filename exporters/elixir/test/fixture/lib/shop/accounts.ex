defmodule Shop.Accounts do
  @moduledoc "Customer accounts."
  alias Shop.Accounts.User

  @spec get_user(integer()) :: User.t()
  def get_user(id), do: %User{id: id, email: "user#{id}@example.org", role: :customer}

  def display_name(%User{email: email}), do: email
end
