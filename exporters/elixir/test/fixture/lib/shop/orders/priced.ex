defprotocol Shop.Orders.Priced do
  @moduledoc "Anything with a price."
  def price(item)
end
