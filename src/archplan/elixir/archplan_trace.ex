# ExUnit formatter that records one call trace per test, for archplan
# sequences. It does not change the project: load it from the command line.
#
#   elixir -r /path/to/archplan_trace.ex -S mix test --only feature --max-cases 1 \
#     --formatter ArchplanTrace --formatter ExUnit.CLIFormatter
#
# For each test it enables Erlang call tracing on the project's own modules
# (remote calls only, arity only: argument values are never recorded),
# rebuilds the call tree per process, and writes one JSON file per test to
# ARCHPLAN_TRACE_DIR (default: <repo root>/.archplan/sequences).
# archplan_export.exs turns these files into archmodel sequences.
#
# Environment:
#   ARCHPLAN_TRACE_DIR      output directory
#   ARCHPLAN_CONFIG         config file (default: .archplan/elixir.json)
#   ARCHPLAN_TRACE_TEST=1   also record calls made by the test process itself

Code.require_file("archplan_common.ex", __DIR__)

defmodule ArchplanTrace do
  @moduledoc false
  use GenServer
  alias ArchplanElixir.Common, as: C

  # ------------------------------------------------------- formatter

  @impl true
  def init(_opts) do
    t0 = System.monotonic_time(:millisecond)
    project_dir = File.cwd!()
    root = C.repo_root(project_dir)
    {cfg, _} = C.load_config(System.get_env("ARCHPLAN_CONFIG"), project_dir, root)
    tcfg = cfg["trace"]
    dir = System.get_env("ARCHPLAN_TRACE_DIR") || Path.join(root, ".archplan/sequences")

    mods =
      C.project_modules(cfg, project_dir)
      |> Map.keys()
      |> Enum.reject(fn m -> Enum.any?(tcfg["exclude_modules"], &C.prefix?(C.mod_name(m), &1)) end)

    # Inert until a process gets the call flag at test start.
    ms = [{:_, [], [{:exception_trace}]}]
    skip = C.globs_regex(tcfg["exclude_functions"])

    for m <- mods,
        name = C.mod_name(m),
        f <- m.module_info(:exports) |> Enum.map(&elem(&1, 0)) |> Enum.uniq(),
        f != :module_info,
        skip == nil or not Regex.match?(skip, name <> "." <> Atom.to_string(f)) do
      :erlang.trace_pattern({m, f, :_}, ms, [:global])
    end

    tracer =
      spawn_link(fn ->
        tracer_loop(new_state(tcfg, MapSet.new(mods)))
      end)

    setup_ms = System.monotonic_time(:millisecond) - t0
    IO.puts(:stderr, "archplan: tracing #{length(mods)} modules (setup #{setup_ms} ms), writing to #{Path.relative_to_cwd(dir)}")

    {:ok, %{dir: dir, root: root, project_dir: project_dir, tracer: tracer, written: 0, tcfg: tcfg}}
  end

  @impl true
  # ExUnit sends events without waiting for formatters, so a test can start
  # before tracing is on. A test that has already finished by the time its
  # start event arrives is skipped, so its trace cannot pick up calls of the
  # next test.
  def handle_cast({:test_started, %ExUnit.Test{} = test}, state) do
    {:messages, queued} = Process.info(self(), :messages)

    done? =
      Enum.any?(queued, fn
        {:"$gen_cast", {:test_finished, %ExUnit.Test{module: m, name: n}}} -> m == test.module and n == test.name
        _ -> false
      end)

    if done? do
      {:noreply, Map.put(state, :skip, {test.module, test.name})}
    else
      send(state.tracer, {:start, {test.module, test.name}})
      :erlang.trace(:all, true, [:call, :arity, {:tracer, state.tracer}])
      {:noreply, Map.put(state, :skip, nil)}
    end
  end

  def handle_cast({:test_finished, %ExUnit.Test{module: m, name: n}}, %{skip: {m, n}} = state) do
    {:noreply, %{state | skip: nil}}
  end

  def handle_cast({:test_finished, %ExUnit.Test{} = test}, state) do
    :erlang.trace(:all, false, [:call, :arity])
    ref = make_ref()
    send(state.tracer, {:finish, self(), ref})

    result =
      receive do
        {^ref, result} -> result
      end

    if test.state == nil or match?({:failed, _}, test.state) do
      write(test, result, state)
      {:noreply, %{state | written: state.written + 1}}
    else
      {:noreply, state}
    end
  end

  def handle_cast({:suite_finished, _}, state) do
    IO.puts(:stderr, "archplan: wrote #{state.written} trace files to #{Path.relative_to_cwd(state.dir)}")
    {:noreply, state}
  end

  def handle_cast(_event, state), do: {:noreply, state}

  defp write(test, result, state) do
    file = test.tags[:file] && Path.relative_to(test.tags.file, state.root)
    stem = (test.tags[:file] || "") |> Path.basename(".exs") |> String.replace_suffix("_test", "")
    name = test.name |> Atom.to_string() |> String.replace(~r/^(test|feature|doctest) /, "")
    id = C.slug(stem <> " " <> name)

    trees = Enum.sort_by(result.trees, & &1.order)

    data = %{
      "format" => "archplan-elixir-trace/1",
      "id" => id,
      "title" => name,
      "test" => C.mod_name(test.module),
      "file" => file,
      "line" => test.tags[:line],
      "state" => if(test.state == nil, do: "passed", else: "failed"),
      "timeMs" => div(test.time, 1000),
      "calls" => result.calls,
      "truncated" => result.truncated,
      "entry" =>
        case trees do
          [%{root: [m, f, a, _]} | _] -> "#{m}.#{f}/#{a}"
          [] -> nil
        end,
      "trees" =>
        Enum.map(trees, fn t ->
          %{"actor" => t.actor, "async" => t.async, "root" => t.root}
        end)
    }

    File.mkdir_p!(state.dir)
    File.write!(Path.join(state.dir, id <> ".json"), C.json_encode(data))
  end

  # ---------------------------------------------------------- tracer

  defp new_state(tcfg, mods) do
    %{
      tcfg: tcfg,
      mods: mods,
      test: nil,
      stacks: %{},
      actors: %{},
      trees: [],
      order: 0,
      calls: 0,
      truncated: false
    }
  end

  defp tracer_loop(st) do
    receive do
      {:trace, pid, :call, {m, f, a}} ->
        tracer_loop(call(st, pid, m, f, a))

      {:trace, pid, :return_from, _mfa, _value} ->
        tracer_loop(ret(st, pid))

      {:trace, pid, :exception_from, _mfa, _value} ->
        tracer_loop(ret(st, pid))

      {:start, test} ->
        tracer_loop(%{new_state(st.tcfg, st.mods) | test: test})

      {:finish, from, ref} ->
        st = drain(st, :erlang.trace_delivered(:all))
        st = Enum.reduce(Map.keys(st.stacks), st, &close_all/2)
        send(from, {ref, %{trees: st.trees, calls: st.calls, truncated: st.truncated}})
        tracer_loop(%{new_state(st.tcfg, st.mods) | test: nil})

      _ ->
        tracer_loop(st)
    end
  end

  # Process trace messages until all that were sent before the test
  # finished have arrived.
  defp drain(st, ref) do
    receive do
      {:trace_delivered, :all, ^ref} -> st
      {:trace, pid, :call, {m, f, a}} -> drain(call(st, pid, m, f, a), ref)
      {:trace, pid, :return_from, _, _} -> drain(ret(st, pid), ref)
      {:trace, pid, :exception_from, _, _} -> drain(ret(st, pid), ref)
    end
  end

  # A frame is {:node, mod, fun, arity, order, children} for a call into
  # another module, or {:same, mod} for a call within the module on top.
  defp call(st, pid, m, f, a) do
    {actor, st} = actor(st, pid, m)

    if actor == :skip do
      st
    else
      stack = Map.get(st.stacks, pid, [])
      full = st.calls >= st.tcfg["max_calls"]

      frame =
        case stack do
          [top | _] when elem(top, 1) == m ->
            {:same, m}

          _ when full ->
            {:same, m}

          _ ->
            {:node, m, f, a, st.order, []}
        end

      st =
        case frame do
          {:node, _, _, _, _, _} -> %{st | calls: st.calls + 1, order: st.order + 1}
          _ -> if full, do: %{st | truncated: true}, else: st
        end

      %{st | stacks: Map.put(st.stacks, pid, [frame | stack])}
    end
  end

  defp ret(st, pid) do
    case Map.get(st.stacks, pid, []) do
      [] -> st
      [{:same, _} | rest] -> %{st | stacks: Map.put(st.stacks, pid, rest)}
      [{:node, _, _, _, _, _} = node | rest] -> finish_node(st, pid, node, rest)
    end
  end

  defp finish_node(st, pid, {:node, m, f, a, order, kids}, rest) do
    node = [C.mod_name(m), Atom.to_string(f), a, Enum.reverse(kids)]

    case add_child(rest, node) do
      {:ok, rest} ->
        %{st | stacks: Map.put(st.stacks, pid, rest)}

      :root ->
        {actor, async} = st.actors[pid]
        tree = %{order: order, actor: actor, async: async, root: node}
        %{st | stacks: Map.put(st.stacks, pid, rest), trees: [tree | st.trees]}
    end
  end

  # Add a finished node to the nearest node frame below.
  defp add_child([], _node), do: :root

  defp add_child([{:node, m, f, a, o, kids} | rest], node), do: {:ok, [{:node, m, f, a, o, [node | kids]} | rest]}

  defp add_child([same | rest], node) do
    case add_child(rest, node) do
      {:ok, rest} -> {:ok, [same | rest]}
      :root -> :root
    end
  end

  # Close the calls still open at the end of the test (a process that is
  # still running, or one that was killed).
  defp close_all(pid, st) do
    case Map.get(st.stacks, pid, []) do
      [] -> st
      _ -> st |> ret(pid) |> then(&close_all(pid, &1))
    end
  end

  # Who started the calls in a process: the test itself, a browser request
  # or LiveView, an Oban job, or another process. Decided on first sight,
  # from the process's initial call, else from the first project module it
  # calls.
  defp actor(st, pid, m) do
    case st.actors do
      %{^pid => {a, _}} ->
        {a, st}

      _ ->
        info = process_info(pid)

        {a, async} =
          cond do
            info.label == st.test and info.label != nil ->
              if include_test?(st), do: {"test", nil}, else: {:skip, nil}

            match?({Phoenix.LiveView, _, _}, info.label) or web_process?(info.initial) or web_entry?(m) ->
              {"browser", nil}

            String.starts_with?(info.initial, "Oban") or oban_worker?(m) ->
              {"oban", "oban"}

            true ->
              {"process", "process"}
          end

        if a == :skip, do: :erlang.trace(pid, false, [:call])
        {a, %{st | actors: Map.put(st.actors, pid, {a, async})}}
    end
  end

  defp include_test?(st), do: System.get_env("ARCHPLAN_TRACE_TEST") == "1" or st.tcfg["include_test_process"] == true

  # A process that ended before the tracer saw its first call cannot be
  # inspected; it counts as "process".
  defp process_info(pid) do
    case Process.info(pid, [:dictionary, :initial_call]) do
      [dictionary: dict, initial_call: initial] ->
        mod =
          case dict[:"$initial_call"] || initial do
            {m, _, _} -> if function_exported?(m, :__live__, 0), do: "Phoenix.LiveView", else: C.mod_name(m)
            _ -> ""
          end

        %{label: dict[:"$process_label"], initial: mod}

      _ ->
        %{label: nil, initial: ""}
    end
  end

  @web ~w(Phoenix.LiveView Phoenix.Channel Phoenix.Socket Phoenix.Endpoint Plug Bandit ThousandIsland cowboy ranch)

  defp web_process?(initial), do: Enum.any?(@web, &String.starts_with?(initial, &1))

  defp web_entry?(m) do
    name = C.mod_name(m)

    function_exported?(m, :__live__, 0) or
      Enum.any?(~w(Controller Endpoint Router Plug Socket Channel), &String.ends_with?(name, &1)) or
      (function_exported?(m, :call, 2) and function_exported?(m, :init, 1))
  end

  defp oban_worker?(m), do: function_exported?(m, :perform, 1) and function_exported?(m, :__opts__, 0)
end
