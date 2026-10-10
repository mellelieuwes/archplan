defmodule ArchplanElixir.Common do
  @moduledoc false
  # Shared by archplan_export.exs and archplan_trace.ex: configuration,
  # the module -> component mapping, and a small JSON encoder with stable
  # key order. Uses only the Elixir standard library.

  @default_config %{
    "depth" => 2,
    "min_modules" => 2,
    "components" => [],
    "exclude_paths" => ["test/**", "deps/**"],
    "exclude_modules" => ["Mix.Tasks"],
    "types" => %{"modules" => false},
    "compile_refs" => true,
    "trace" => %{
      "exclude_modules" => [],
      # "Module.function" globs; * matches anything. Generated reflection
      # functions, protocol dispatch and endpoint plumbing add noise.
      "exclude_functions" => [
        "*.__*",
        "*.impl_for",
        "*.impl_for!",
        "*.Endpoint.config",
        "*.Endpoint.script_name",
        "*.Endpoint.static_path",
        "*.Endpoint.url",
        "*.Endpoint.path"
      ],
      "max_calls" => 50_000,
      "max_messages" => 400,
      "max_depth" => 12,
      "include_test_process" => false
    }
  }

  # ---------------------------------------------------------------- config

  @doc "The configuration: defaults merged with the first config file found."
  def load_config(explicit, project_dir, root) do
    candidates =
      if explicit,
        do: [explicit],
        else: [
          Path.join([project_dir, ".archplan", "elixir.json"]),
          Path.join([root, ".archplan", "elixir.json"])
        ]

    case Enum.find(candidates, &File.regular?/1) do
      nil ->
        if explicit, do: raise("config file not found: #{explicit}")
        {@default_config, nil}

      file ->
        user = file |> File.read!() |> json_decode()
        {deep_merge(@default_config, user), file}
    end
  end

  defp deep_merge(a, b) when is_map(a) and is_map(b),
    do: Map.merge(a, b, fn _k, x, y -> deep_merge(x, y) end)

  defp deep_merge(_a, b), do: b

  @doc "The repository root: git toplevel of dir, else dir."
  def repo_root(dir) do
    case System.cmd("git", ["rev-parse", "--show-toplevel"], cd: dir, stderr_to_stdout: true) do
      {out, 0} -> String.trim(out)
      _ -> dir
    end
  rescue
    _ -> dir
  end

  # ------------------------------------------------------------ modules

  @doc "Module name without the Elixir. prefix."
  def mod_name(mod) when is_atom(mod) do
    case Atom.to_string(mod) do
      "Elixir." <> rest -> rest
      other -> other
    end
  end

  @doc "Whether a module name matches a prefix on a segment boundary."
  def prefix?(name, prefix) do
    prefix = String.trim_trailing(prefix, ".*")
    name == prefix or String.starts_with?(name, prefix <> ".")
  end

  @doc "Whether a path (relative to the project dir) matches one of the globs."
  def path_excluded?(rel, globs), do: Enum.any?(globs, &glob_match?(rel, &1))

  def glob_match?(path, glob) do
    re =
      glob
      |> Regex.escape()
      |> String.replace("\\*\\*/", "(?:.*/)?")
      |> String.replace("\\*\\*", ".*")
      |> String.replace("\\*", "[^/]*")
      |> String.replace("\\?", "[^/]")

    Regex.match?(~r/^#{re}$/, path)
  end

  @doc """
  The project's own modules: [{module, beam_path}] for every app in the
  current Mix project (each app of an umbrella).
  """
  def project_beams do
    build = Mix.Project.build_path()

    apps =
      if Mix.Project.umbrella?(),
        do: Mix.Project.apps_paths() |> Map.keys(),
        else: [Mix.Project.config()[:app]]

    for app <- apps,
        beam <- Path.wildcard(Path.join([build, "lib", to_string(app), "ebin", "*.beam"])) do
      {beam |> Path.basename(".beam") |> String.to_atom(), beam}
    end
  end

  @doc "Absolute source file of a loaded module, or nil."
  def source(mod) do
    case mod.module_info(:compile)[:source] do
      nil -> nil
      s -> List.to_string(s)
    end
  rescue
    _ -> nil
  end

  @doc """
  The project's modules after exclusions, as %{module => source}. Modules
  are loaded.
  """
  def project_modules(cfg, project_dir) do
    for {mod, _beam} <- project_beams(),
        Code.ensure_loaded?(mod),
        src = source(mod),
        src != nil,
        rel = Path.relative_to(src, project_dir),
        not path_excluded?(rel, cfg["exclude_paths"]),
        not Enum.any?(cfg["exclude_modules"], &prefix?(mod_name(mod), &1)),
        into: %{} do
      {mod, src}
    end
  end

  # --------------------------------------------------------- components

  @doc """
  Map every module to a component ID. Returns %{module => component}.

  Rules, first match wins:
  1. A boundary (from the `boundary` library) that contains the module.
  2. The longest matching `components` rule from the config: a rule with
     `id` names the component, a rule with `depth` groups by that many
     namespace segments.
  3. The first `depth` namespace segments (default 2).
  A protocol implementation belongs to the component of the type it is for.
  A group made by depth with fewer than `min_modules` modules folds into
  its parent namespace, unless its files have a directory of their own.
  """
  def components(mods, cfg, boundaries \\ []) do
    rules =
      cfg["components"]
      |> Enum.sort_by(&(-String.length(&1["prefix"] || "")))

    base =
      for mod <- Map.keys(mods), into: %{} do
        {mod, assign(mod_name(mod), rules, cfg["depth"], boundaries)}
      end

    min = cfg["min_modules"] || 1

    groups = Enum.group_by(base, fn {_, {id, _}} -> id end, fn {mod, _} -> mods[mod] end)

    folded =
      for {mod, {id, how}} <- base, into: %{} do
        parts = String.split(id, ".")

        if how == :depth and length(parts) > 1 and length(groups[id]) < min and
             not own_dir?(groups[id], Map.values(mods)),
           do: {mod, Enum.join(Enum.drop(parts, -1), ".")},
           else: {mod, id}
      end

    # protocol implementations follow the type they are for
    Map.new(folded, fn {mod, id} ->
      case impl_for(mod) do
        {_proto, target} when is_map_key(folded, target) -> {mod, folded[target]}
        {proto, _} when is_map_key(folded, proto) -> {mod, folded[proto]}
        _ -> {mod, id}
      end
    end)
  end

  # Whether the group's files sit in a directory that holds no other files.
  defp own_dir?(files, all_files) do
    dir = files |> Enum.map(&Path.dirname/1) |> common_dir()
    others = Enum.reject(all_files, &(&1 in files))
    dir != "." and not Enum.any?(others, &String.starts_with?(&1, dir <> "/"))
  end

  @doc "The longest common directory of a list of directories."
  def common_dir(dirs) do
    dirs
    |> Enum.map(&Path.split/1)
    |> Enum.reduce(fn a, b ->
      Enum.zip(a, b) |> Enum.take_while(fn {x, y} -> x == y end) |> Enum.map(&elem(&1, 0))
    end)
    |> case do
      [] -> "."
      parts -> Path.join(parts)
    end
  end

  defp assign(name, rules, depth, boundaries) do
    case Enum.find(boundaries, &prefix?(name, &1)) do
      nil ->
        case Enum.find(rules, &prefix?(name, &1["prefix"])) do
          %{"id" => id} -> {id, :fixed}
          %{"depth" => d} -> {take(name, d), :depth}
          _ -> {take(name, depth), :depth}
        end

      b ->
        {b, :fixed}
    end
  end

  defp take(name, d), do: name |> String.split(".") |> Enum.take(d) |> Enum.join(".")

  @doc "{protocol, for} when mod is a protocol implementation."
  def impl_for(mod) do
    if function_exported?(mod, :__impl__, 1),
      do: {mod.__impl__(:protocol), mod.__impl__(:for)},
      else: nil
  end

  @doc """
  Boundaries declared with the `boundary` library: [{name, %{deps:, exports:}}],
  innermost first. Empty when the project does not use it.
  """
  def boundaries(mods) do
    for mod <- Map.keys(mods),
        spec = boundary_spec(mod),
        spec != nil do
      {mod_name(mod), spec}
    end
    |> Enum.sort_by(fn {n, _} -> -String.length(n) end)
  end

  defp boundary_spec(mod) do
    attrs = mod.module_info(:attributes)

    case Keyword.get(attrs, Boundary) do
      [spec | _] when is_list(spec) or is_map(spec) -> Map.new(spec)
      _ -> nil
    end
  rescue
    _ -> nil
  end

  # --------------------------------------------------------------- JSON

  def json_decode(bin) do
    cond do
      Code.ensure_loaded?(JSON) -> apply(JSON, :decode!, [bin])
      Code.ensure_loaded?(Jason) -> apply(Jason, :decode!, [bin])
      true -> raise "no JSON decoder: needs Elixir 1.18+ or Jason"
    end
  end

  @key_order ~w(format source layers components dependencies policy types sequences vocabulary notes
                id name title repo ref dirty languages module generator generatedAt commands
                path match layer kind component file line doc entry from to label depth weight
                evidence default rules effect fields methods implements uses participants fragments
                messages type sig)

  @doc "Pretty JSON with a stable key order (known keys first, then sorted)."
  def json_encode(term), do: IO.iodata_to_binary([enc(term, 0), "\n"])

  defp enc(nil, _), do: "null"
  defp enc(true, _), do: "true"
  defp enc(false, _), do: "false"
  defp enc(a, _) when is_atom(a), do: str(Atom.to_string(a))
  defp enc(s, _) when is_binary(s), do: str(s)
  defp enc(n, _) when is_integer(n), do: Integer.to_string(n)
  defp enc(n, _) when is_float(n), do: :erlang.float_to_binary(n, [:short])
  defp enc([], _), do: "[]"

  defp enc(list, ind) when is_list(list) do
    if Enum.all?(list, &scalar?/1) and length(list) <= 12 do
      ["[", Enum.map_intersperse(list, ", ", &enc(&1, ind)), "]"]
    else
      pad = String.duplicate("  ", ind + 1)

      ["[\n", Enum.map_intersperse(list, ",\n", &[pad, enc(&1, ind + 1)]), "\n",
       String.duplicate("  ", ind), "]"]
    end
  end

  defp enc(map, ind) when is_map(map) do
    if map_size(map) == 0 do
      "{}"
    else
      pad = String.duplicate("  ", ind + 1)
      keys = map |> Map.keys() |> Enum.map(&to_string/1) |> Enum.sort_by(&key_rank/1)
      byname = Map.new(map, fn {k, v} -> {to_string(k), v} end)

      if ind >= 2 and Enum.all?(Map.values(map), &scalar?/1) and map_size(map) <= 8 do
        ["{", Enum.map_intersperse(keys, ", ", &[str(&1), ": ", enc(byname[&1], ind + 1)]), "}"]
      else
        ["{\n",
         Enum.map_intersperse(keys, ",\n", &[pad, str(&1), ": ", enc(byname[&1], ind + 1)]),
         "\n", String.duplicate("  ", ind), "}"]
      end
    end
  end

  defp scalar?(x), do: not is_map(x) and not is_list(x)

  defp key_rank(k) do
    case Enum.find_index(@key_order, &(&1 == k)) do
      nil -> {1, k}
      i -> {0, i}
    end
  end

  defp str(s) do
    escaped =
      for <<c::utf8 <- s>>, into: "" do
        case c do
          ?" -> "\\\""
          ?\\ -> "\\\\"
          ?\n -> "\\n"
          ?\r -> "\\r"
          ?\t -> "\\t"
          c when c < 0x20 -> "\\u" <> String.pad_leading(Integer.to_string(c, 16), 4, "0")
          c -> <<c::utf8>>
        end
      end

    [?", escaped, ?"]
  end

  @doc "A regex for a list of \"Module.function\" globs (* matches anything)."
  def globs_regex([]), do: nil

  def globs_regex(globs) do
    alts = Enum.map_join(globs, "|", fn g -> g |> Regex.escape() |> String.replace("\\*", ".*") end)
    Regex.compile!("^(?:" <> alts <> ")$")
  end

  @doc "A slug: lower case words joined by dashes."
  def slug(s) do
    s
    |> String.downcase()
    |> String.replace(~r/[^a-z0-9]+/, "-")
    |> String.trim("-")
  end
end
