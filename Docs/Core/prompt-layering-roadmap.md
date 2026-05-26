# Prompt Layering Roadmap

This document records the full staged plan for making Somnia provider payloads easier to inspect and safer to evolve. Phase 1 is implemented first; later phases should preserve provider compatibility unless a phase explicitly changes it.

## Goals

- Make the prompt payload readable in debug logs.
- Separate permanent policy from per-turn runtime state.
- Keep skills lazy-loaded instead of injecting large skill bodies by default.
- Make MCP usage policy visible without duplicating large tool schemas in the system prompt.
- Load repository guidance by scope so near-directory instructions can override broad root guidance.
- Preserve the provider-facing `system_prompt` string until all providers and tests support richer prompt structures.

## Target Layers

### A. Core System Prompt

Permanent baseline prompt. It should include stable identity, high-level coding-agent behavior, and durable safety or quality principles.

Do not include workspace path, provider/model, execution mode, MCP status, loaded skills, repository guidance, todo reminders, or current working-file cache.

### B. Runtime Injection

Dynamic per-turn context owned by the runtime. It should include:

- actor and role
- workspace root
- provider and model identity
- execution mode guidance
- OS and shell guidance
- workflow/tool-selection guidance
- transient reminders such as todo reconciliation
- current working-file cache
- context-governance hints

This layer may change every turn.

### C. Skill Prompt

Skill discovery only. The default prompt should contain a concise skill index, not full skill bodies.

Full skill instructions remain lazy-loaded through `load_skill`. The long-term target is:

- concise skill name and trigger summary in the default prompt
- full skill body only after explicit or inferred load
- loaded skill content recorded as its own debug section

### D. MCP Prompt

MCP strategy and status only. Tool schemas remain in the provider `tools` field.

This layer should describe:

- enabled MCP servers at a compact level
- project-specific MCP precedence over overlapping generic tools
- fallback behavior when a preferred MCP tool is unavailable or stale

It should not duplicate every MCP tool description, because those already exist in the provider tool schema.

### E. Repo Prompt

Repository instructions loaded by scope.

Initial behavior should preserve root `AGENTS.md` / `CLAUDE.md`. Later behavior should load additional guidance based on the current task and working paths:

- root repo guidance is broad baseline guidance
- nested guidance closer to the target file or directory has higher precedence
- user and runtime safety rules still override repo guidance
- repo-specified tools override overlapping generic tool preferences

## Phase Plan

### Phase 1: Structured Debug Sections

Status: implemented in the current working tree.

Scope:

- Add `PromptSection` and `PromptBundle`.
- Build the existing provider-facing prompt from ordered sections.
- Keep `build_system_prompt()` returning a single string.
- Add `system_prompt_sections` to provider payload debug logs.
- Keep existing `system_prompt` in logs for compatibility.
- Make generic `tree` / `grep` / `read_file` guidance conditional fallback when repo guidance specifies overlapping tools.

Validation:

- Prompt still contains the existing required guidance.
- Debug payload includes A/B/C/D/E sections.
- Provider request shape is unchanged.
- Existing system prompt and project-instruction tests pass.

### Phase 2: Repo Prompt Scope Loading

Scope:

- Extend `ProjectInstructionsLoader` beyond root-only loading.
- Load root guidance plus relevant nested `AGENTS.md` / `CLAUDE.md`.
- Determine relevance from current working file, explicit mentioned paths, and recent edited/read files.
- Preserve deterministic ordering: root first, then increasingly specific directories.
- Mark each repo section with source path and scope.
- Add truncation per file and total repo prompt budget.

Validation:

- Root-only behavior remains unchanged when no nested guidance exists.
- Nested guidance appears when the task targets that directory.
- Nearer guidance appears after broader guidance and is documented as higher precedence.
- Large guidance files are truncated with explicit markers.

### Phase 3: Skill Prompt Compaction

Scope:

- Add a concise skill listing API, separate from full skill bodies.
- Replace default `skill_loader.descriptions()` injection with compact index content.
- Add debug payload section metadata for loaded skills.
- Ensure `load_skill` remains the path for full skill content.

Validation:

- Default prompt token count drops.
- Skill discovery remains possible.
- Explicit skill requests still load full skill content.
- Debug payload distinguishes skill index from loaded skill bodies.

### Phase 4: MCP Prompt and Tool Gating

Scope:

- Add compact MCP status into the MCP section.
- Avoid repeating long MCP tool descriptions in system prompt.
- Consider task-aware MCP tool gating for provider `tools`:
  - always keep core tools needed for runtime operation
  - expose project-required MCP tools first
  - expose generic tools as fallback
  - allow full tool exposure when needed for discovery or debugging

Validation:

- GitNexus-style project guidance leads to GitNexus MCP tools before `tree` / `grep` / `read_file`.
- Provider `tools` payload is smaller for common turns.
- Tool gating has a fallback path when the model asks for unavailable but relevant tools.

### Phase 5: Prompt Payload Observability

Scope:

- Expand debug payload with:
  - rendered `system_prompt`
  - `system_prompt_sections`
  - section token estimates
  - tool schema token estimates
  - repo prompt sources
  - loaded skills
  - MCP servers and exposed tool counts
- Add a small helper command or UI endpoint to inspect the latest provider payload summary.

Validation:

- A developer can answer "why did this model see this instruction?" from one payload file.
- The summary does not require reading the full raw provider request.

### Phase 6: Provider-Native Prompt Support

Scope:

- Keep string rendering as default.
- Evaluate provider-specific support for richer instruction separation.
- If supported, map sections to provider-native fields without changing the internal section model.

Validation:

- OpenAI-compatible and Anthropic-compatible providers continue passing existing tests.
- Debug logs show both internal sections and final provider request.

## Risk Notes

- `build_system_prompt` is a high-impact runtime entry point. Treat changes as high risk even when text-only.
- Changing generic tool guidance can alter model behavior across many workflows.
- Repo prompt scope loading can accidentally inject irrelevant guidance if path relevance is too broad.
- MCP tool gating can break workflows if core tools or fallback tools are hidden too aggressively.
- Skill compaction can reduce discoverability if summaries are too short.

## Current Follow-Ups

- Commit Phase 1 after review.
- Decide whether nested repo guidance should use only `AGENTS.md` / `CLAUDE.md`, or also `.open_somnia` scoped files.
- Define a prompt budget policy per layer.
- Add a payload-summary command or desktop inspector for `provider_payloads`.
