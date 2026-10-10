defmodule Shop.MixProject do
  use Mix.Project

  def project do
    [app: :shop, version: "0.1.0", elixir: "~> 1.15", deps: [{:ecto, "~> 3.10"}]]
  end

  def application, do: [extra_applications: [:logger]]
end
