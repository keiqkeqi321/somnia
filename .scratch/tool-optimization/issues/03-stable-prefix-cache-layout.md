# 03 — Stable-first request layout + Anthropic tools breakpoint

**What to build:** The request layout is ordered so byte-stable content leads and
dynamic content trails: core prompt and runtime environment first, skills /
MCP / repo-instruction sections last. On the Anthropic chain, the tools array
gets its own explicit cache breakpoint (a third breakpoint, within the 4-breakpoint
budget), so tool definitions are cache-read even when later system sections
change. The OpenAI chain needs no markers — it benefits from the shared stable
ordering via automatic prefix caching; its behavior is regression-checked only.

**Blocked by:** None — can start immediately.

**Status:** done

- [x] System prompt section order is stable-first / dynamic-last on both providers
- [x] Anthropic payload carries an explicit cache breakpoint at the end of the tools array; existing system and last-message breakpoints keep working
- [x] A mid-session skill load or AGENTS.md edit invalidates only the trailing sections, not the tools/core prefix (verified structurally in payload tests)
- [x] OpenAI payload shape is unchanged except for the shared section reordering
- [x] Tests assert breakpoint placement in the serialized Anthropic payload; full unittest suite passes

## Comments

Completed 2026-08-03. Changes:

- `runtime/system_prompt.py` — section order now A. Core / B. Runtime / C. MCP /
  D. Skill / E. Repo; A–C flagged session-stable (`dynamic=False`), D/E volatile.
- `runtime/prompt_sections.py` — `STABLE_SECTION_TITLE_PREFIXES` ("A.","B.","C.") +
  `section_title_is_stable()` replace the old "only A. is stable" parse rule.
- `providers/anthropic_provider.py` — `_to_anthropic_tools` always marks the last
  tool with `cache_control` (tools tier leads Anthropic's `tools → system →
  messages` cache prefix); `_has_cache_control` removed. 3 breakpoints/request
  (tools, stable-system, last message) of the max 4.
- `tests/test_runtime_tool_output.py` — updated 3 tests to the new layout;
  added `test_anthropic_provider_dynamic_system_sections_leave_tools_and_stable_prefix_untouched`
  proving D/E edits leave the tools tier and A–C system prefix byte-identical.
- `Docs/Core/16-Provider缓存命中优化.md` — breakpoint/layering description updated.

Verification: full suite 740 tests OK (exit 0). One earlier full-suite run showed
a single failure in `test_desktop_remote` pairing — passes standalone (21/21),
timing flake unrelated to this change; rerun was green. Not committed — left for
user review.
