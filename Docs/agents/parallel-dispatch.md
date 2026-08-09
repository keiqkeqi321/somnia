# Parallel Tool Dispatch (order-preserving segment parallelism)

Detailed reference for the parallel execution policy. Kept out of `AGENTS.md`
to keep it lean; `AGENTS.md` carries only the short summary.

Independent read-only tool calls in a single turn run concurrently; writes and
state-changing calls stay serial. `open_somnia/runtime/parallel_dispatch.py`
implements the policy; both call sites — the Lead main loop
(`open_somnia/runtime/agent.py`) and `SessionlessRoundRunner.run_round`
(`open_somnia/runtime/round_runner.py`) — dispatch through it.

## Algorithm

`segment_tool_calls(tool_calls)` scans the calls in input order and yields
maximal runs of consecutive **parallel-safe** call indices. Each segment runs
on a process-lifetime singleton `ThreadPoolExecutor`
(`max_workers = min(8, runtime.parallel_tool_max_workers)`). Results are
re-collected and **reordered to input order** before being paired back to the
provider's `tool_use` blocks (Anthropic/OpenAI pair results positionally/by
id). Segments are concatenated in input order, so the observable behaviour —
result order, transcript order, counters, guards, turn-boundary interrupts —
is identical to serial execution. Serial fast path: segments of length ≤ 1 (a
lone safe call, or any unsafe call) execute inline exactly as before.

## Whitelist

`PARALLEL_SAFE_TOOL_NAMES`, a strict subset of the read-only tools:
`read_file`, `read_image`, `grep`, `glob`, `tree`, `find_symbol`, `web_fetch`,
`task_get`, `task_list`, `list_teammates`, `check_background`. Everything else
is conservatively serial: `TodoWrite` (unlocked `session.todo_items` write),
`request_authorization`/`request_mode_switch` (blocking handshake + control
flow), `submit_plan`/`compress`/`load_skill`/`request_original_context`
(context mutation), `write_file`/`edit_file`/`bash`/`background_run` (writes /
side effects), `subagent`/`spawn_teammate` (nested agent loops), all
task-mutation and team-collaboration tools, and all `mcp__*` tools (no
read-only marker). GIL releases during I/O in the whitelisted tools make the
threading worthwhile.

## Explore-subagent parallelism

An `agent_type=Explore` `subagent` call is also parallel-safe (via
`is_explore_subagent_safe`, *not* membership in `PARALLEL_SAFE_TOOL_NAMES` —
that set stays a pure read-only *tool* list). Consecutive Explore-subagent
calls in one turn run concurrently so the lead can fan out three explorations
and pay max(latency), not sum(latency). `general-purpose` subagents (which
carry `write_file`/`edit_file`) stay serial. Two dispatch pools, deliberately
separate: read-only tools run on `_POOL` (`dispatch_parallel_segment`);
Explore subagents run on `_SUBAGENT_POOL` (`run_parallel_explore_subagents`).
A subagent is a nested agent loop whose internal rounds submit read-only tools
to `_POOL`, so the subagent calls themselves must **not** consume `_POOL`
workers or they deadlock (all workers busy holding subagent loops waiting for
a worker). The lead loop bounds a maximal parallel run by *kind*
(`_parallel_safe_kind`: `tool` vs `subagent`) so the two pools never mix in
one segment. `SkillLoader` is guarded by an `RLock` (its `reload` reassigns
`self.skills` wholesale; parallel subagents calling `load_skill` could
otherwise observe a half-rebuilt dict).

## Explore-subagent read-only bash

An Explore subagent's only write vector is `bash` (it has no
`write_file`/`edit_file`). To keep parallel Explore subagents free of write
races, the subagent runner registers a **gated** `bash`
(`register_readonly_shell_tool`) that refuses mutating commands via
`is_readonly_shell_command` (allow-list of read-only prefixes mirroring
`EXPLORATION_SHELL_PREFIXES`, plus a write-syntax deny-list for `>`/`|tee`/
`rm`/`git checkout`/`git reset`/`git pull`/`git push`/`git commit`/
`npm install`/etc.). Non-read-only commands return an error naming the write
op and suggesting a read-only alternative or a general-purpose subagent /
lead-loop `bash`. The lead loop's `bash` and general-purpose subagents' `bash`
are **unrestricted**.

## Lead loop (three-stage)

Stage A is a deterministic, I/O-free pre-scan (`_plan_lead_tool_calls`) that
reproduces the serial guard/counter sequence (flood guard,
malformed/unknown-name dedup with drop-on-repeat, exploration budget
streak/total) and yields a plan with a decision per call plus the computed
`is_parallel_safe`/`is_exploration`/`end_turn_after` flags. Stages B and C are
**fused into a single cursor loop**: each iteration determines the segment
(maximal parallel-safe run, or a single call), executes it
(`dispatch_parallel_segment` or inline), then applies all side effects for
that segment in input order — `print_tool_started`, repair hints,
`print_tool_event`, transcript append, `reported_tool_calls`/exploration
counters, `used_todo`/`manual_compact`, and the stateful interrupts
(`is_turn_boundary`, `prepare_next_loop_user_message`, `end_turn_after`) which
break the loop exactly as the serial loop did. Fusion is required because
`prepare_next_loop_user_message` moves pending→ready context injections and
cannot be pre-scanned.

## Locking

`PermissionManager` wraps its worker-once / lead-once counter mutations in an
`RLock` as defensive insurance (the safe set never needs a once grant). UI
rendering, transcript, and `tool_results` appends all happen in stage C / the
round-runner post-segment hooks, which are single-threaded and
order-preserving — no extra locks needed.

## Switches

`runtime.parallel_tool_dispatch` (default `True`) and
`runtime.parallel_tool_max_workers` (default `8`) live in `RuntimeSettings`
(`open_somnia/config/models.py`). The escape hatch `SOMNIA_NO_PARALLEL_TOOLS=1`
forces fully serial execution regardless of config (troubleshooting). The
system prompt tells the model that independent read-only calls run
concurrently and sequencing only matters across result dependencies.
