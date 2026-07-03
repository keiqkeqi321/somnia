---
name: somnia-config
description: Configure or explain every Somnia TOML configuration area across user-level and project-level settings, including agent system prompts, provider profiles, model traits, runtime limits, MCP servers, hooks, skills directories, and common requests such as adding an MCP server from another client's JSON config.
---

# Somnia Configuration

Use this skill when a user asks to configure Somnia, asks where settings live, provides a TOML/JSON snippet to add, or asks for the meaning of any `open_somnia.toml` option.

## Scope And Files

Somnia reads two TOML config files:

- User/global: `~/.open_somnia/open_somnia.toml`
- Project/workspace: `<workspace>/.open_somnia/open_somnia.toml`

Merge behavior:

- User config loads first.
- Project config overrides matching scalar/table values.
- `hooks` are appended and then merged by managed hook identity.
- Runtime changes usually require restarting or reconnecting the running Somnia sidecar/session.

Scope choice:

- Use project scope for repo-specific MCP servers, cwd-relative commands, project prompts, project hooks, and workspace behavior.
- Use user scope for personal provider profiles, reusable MCP servers, general runtime defaults, and personal system prompt preferences.
- If scope is ambiguous and changing the wrong scope could surprise the user, ask one concise question.

When editing config, preserve unrelated sections and validate TOML syntax.

## Complete TOML Map

Supported top-level areas:

```toml
[agent]

[providers]
[providers.<name>]

[model_traits.<model>]
[model_traits.<provider>.<model>]

[routing]

[runtime]

[mcp_servers.<name>]
[mcp_servers.<name>.env]
[mcp_servers.<name>.http_headers]

[[hooks]]
[hooks.env]
[hooks.matcher]
```

Skills are configured by directories, not TOML:

- User skills: `~/.open_somnia/skills/<skill-name>/SKILL.md`
- Claude-compatible user skills: `~/.claude/skills/<skill-name>/SKILL.md`
- Project skills: `<workspace>/.open_somnia/skills/<skill-name>/SKILL.md`
- Claude-compatible project skills: `<workspace>/.claude/skills/<skill-name>/SKILL.md`
- Legacy project skills: `<workspace>/skills/<skill-name>/SKILL.md`
- Built-in skills: packaged inside Somnia.

## `[agent]`

Controls the agent name and base system prompt.

```toml
[agent]
name = "Somnia"
system_prompt = "Custom instructions here."
```

Fields:

- `name`: display/name value used by the default system prompt. Default: `"Somnia"`.
- `system_prompt`: replaces Somnia's default base prompt when set. Environment/mode/tool guidance is still appended by runtime code.

Use project scope for repo-specific instructions. Use user scope for personal defaults.

## `[providers]`

Defines provider profiles and the default active provider.

```toml
[providers]
default = "openai"

[providers.openai]
provider_type = "openai"
models = ["gpt-4.1", "gpt-4.1-mini"]
default_model = "gpt-4.1"
api_key = "${OPENAI_API_KEY}"
base_url = "https://api.openai.com/v1"
organization = "org_optional"
context_window_tokens = 1047576
max_tokens = 8000
timeout_seconds = 120
reasoning_level = "medium"
prompt_cache_key = "somnia-main"
prompt_cache_retention = "24h"

[routing]
vision_provider = "openai"
vision_model = "gpt-4.1-mini"
```

Fields:

- `[providers].default`: provider profile name to use by default.
- `provider_type`: `"openai"` or `"anthropic"`.
- `models`: list of model ids available for this provider.
- `default_model`: active/default model for this provider. If absent, Somnia uses the profile default.
- `api_key`: API key. Prefer env placeholders such as `"${OPENAI_API_KEY}"` unless the user explicitly wants a literal key.
- `base_url`: API base URL. OpenAI-compatible providers should usually set this.
- `organization`: optional provider organization id.
- `context_window_tokens`: optional fallback context window for every model in this provider.
- `max_tokens`: max output tokens. Default: `8000`.
- `timeout_seconds`: provider request timeout. Default: `120`.
- `reasoning_level`: optional reasoning preference. Supported normalized values: `auto`, `low`, `medium`, `high`, `deep`; unset/`auto` means automatic/default behavior.
- `prompt_cache_key`: optional OpenAI prompt cache routing key. Sent only to the official `api.openai.com` endpoint.
- `prompt_cache_retention`: optional OpenAI prompt cache retention policy, such as `"in-memory"` or `"24h"`. Sent only to the official `api.openai.com` endpoint.

Provider names are normalized lowercase by the loader.

## `[routing]`

Defines shared routing fallbacks that are independent of the active text provider profile.

Fields:

- `vision_provider` + `vision_model`: optional shared provider/model pair to use for turns that include image inputs. The provider must be configured and the model must be listed under that provider. Project config overrides user config; set both values to empty strings in project config to disable a user-level fallback.

## `[model_traits]`

Overrides per-model capabilities and context window metadata. These can be global by model or scoped under a provider.

Global model traits:

```toml
[model_traits.gpt-4.1]
context_window_tokens = 1047576
supports_reasoning = true
supports_adaptive_reasoning = true
```

Provider-specific model traits:

```toml
[model_traits.openai.gpt-4.1]
context_window_tokens = 1047576
supports_reasoning = true
supports_adaptive_reasoning = true
```

Fields:

- `context_window_tokens`: integer context window. Alias: `cwt`.
- `supports_reasoning`: boolean.
- `supports_adaptive_reasoning`: boolean. Alias: `adaptive_reasoning`.

Use this when Somnia's built-in context window inference is wrong or when a model's reasoning support needs explicit control.

## `[runtime]`

Controls local runtime behavior.

```toml
[runtime]
janitor_trigger_ratio = 0.6
command_timeout_seconds = 120
background_poll_interval_seconds = 2
teammate_idle_timeout_seconds = 60
teammate_poll_interval_seconds = 5
max_tool_output_chars = 50000
max_subagent_rounds = 30
max_agent_rounds = 100
```

Fields:

- `janitor_trigger_ratio`: context cleanup trigger ratio. Default: `0.6`.
- `command_timeout_seconds`: default shell/tool command timeout. Default: `120`.
- `background_poll_interval_seconds`: background job poll interval. Default: `2`.
- `teammate_idle_timeout_seconds`: teammate idle timeout. Default: `60`.
- `teammate_poll_interval_seconds`: teammate polling interval. Default: `5`.
- `max_tool_output_chars`: max retained/rendered tool output characters. Default: `50000`.
- `max_subagent_rounds`: subagent round cap. Default: `30`.
- `max_agent_rounds`: main agent round cap. Default: `100`.

Prefer project scope only when a repo needs specific tool/output limits. Otherwise use user scope.

## `[mcp_servers]`

Defines MCP servers. Somnia supports table form and legacy array form; prefer table form.

Stdio server:

```toml
[mcp_servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
cwd = "."
enabled = true
timeout_seconds = 30
startup_timeout_sec = 30
protocol_version = "2025-11-25"

[mcp_servers.filesystem.env]
DEBUG = "1"
```

HTTP server:

```toml
[mcp_servers.remote]
transport = "http"
url = "https://example.com/mcp"
enabled = true
timeout_seconds = 30
startup_timeout_sec = 30

[mcp_servers.remote.http_headers]
Authorization = "Bearer ${TOKEN}"
```

Fields:

- `transport`: `"stdio"` or `"http"`. If omitted, Somnia infers `"http"` when `url` exists, otherwise `"stdio"`.
- `url`: HTTP MCP endpoint. Used for `transport = "http"`.
- `command`: stdio executable command.
- `args`: stdio command arguments.
- `cwd`: optional working directory. Relative paths resolve from the workspace root.
- `env`: table of environment variables for stdio servers.
- `http_headers`: table of HTTP headers for HTTP servers.
- `enabled`: boolean. Default: `true`.
- `timeout_seconds`: request timeout. Default: `30`. Legacy alias: `request_timeout_sec`.
- `startup_timeout_sec`: startup timeout. Default falls back to `timeout_seconds`, then `30`.
- `protocol_version`: MCP protocol version. Default: `"2025-11-25"`.

Legacy array form:

```toml
[[mcp_servers]]
name = "filesystem"
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

When converting from other clients, common JSON:

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "."],
      "env": { "DEBUG": "1" }
    }
  }
}
```

becomes:

```toml
[mcp_servers.filesystem]
transport = "stdio"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "."]
enabled = true

[mcp_servers.filesystem.env]
DEBUG = "1"
```

Do not invent secrets. If a token is required but not provided, use a placeholder such as `"Bearer ${TOKEN}"` and tell the user what to set.

## `[[hooks]]`

Hooks run commands around Somnia events.

```toml
[[hooks]]
event = "AssistantResponse"
command = "python"
args = ["scripts/notify.py"]
cwd = "."
timeout_seconds = 10
on_error = "continue"
enabled = true
background = true
managed_by = "optional_manager_id"

[hooks.env]
SOMNIA_NOTIFY = "1"

[hooks.matcher]
tool_name = "bash"
actor = "lead"
```

Fields:

- `event`: hook event name. Common built-in notification events include `AssistantResponse`, `UserChoiceRequested`, and `TurnFailed`.
- `command`: executable command. Required.
- `args`: command arguments.
- `cwd`: working directory. Relative paths resolve from workspace root.
- `env`: environment variables.
- `timeout_seconds`: hook timeout. Default: `10`.
- `on_error`: usually `"continue"` or `"fail"`. Default: `"continue"`.
- `enabled`: boolean. Default: `true`.
- `background`: boolean. Default: `false`.
- `managed_by`: optional identity used by managed hooks so project hooks can override global managed hooks with the same event/manager.
- `[hooks.matcher].tool_name`: optional tool filter.
- `[hooks.matcher].actor`: optional actor filter.

Rules:

- `PreToolUse` hooks cannot use `background = true`.
- Background hooks cannot use `on_error = "fail"`.
- User and project hooks are merged. Project hooks with the same `(event, managed_by)` replace matching global managed hooks.

Somnia may install built-in notify hooks into the global config, surrounded by marker comments. Do not remove or rewrite that block unless the user asks.

## Skills

Skills are directory-based, not TOML-based.

```text
~/.open_somnia/skills/my-skill/SKILL.md
~/.claude/skills/my-skill/SKILL.md
<workspace>/.open_somnia/skills/my-skill/SKILL.md
<workspace>/.claude/skills/my-skill/SKILL.md
<workspace>/skills/my-skill/SKILL.md
```

Minimum `SKILL.md`:

```markdown
---
name: my-skill
description: Clear trigger-focused description of when Somnia should use this skill.
---

# My Skill

Use this workflow...
```

Prefer project skills for repo-specific workflows and user skills for reusable personal workflows. Keep the description trigger-focused because it appears in available skill summaries.

## Editing Workflow

When applying a config request:

1. Identify config category: agent, provider, model traits, runtime, MCP, hooks, or skills.
2. Choose user or project scope.
3. Read the target config file if it exists.
4. Modify only the relevant section.
5. Preserve unrelated config and comments where practical.
6. Validate TOML by parsing the final file.
7. Explain exactly what changed and any restart/reload requirement.

When the user only asks for an explanation, show the smallest relevant snippet first. Do not dump the whole reference unless they ask for all config options.

## Response Pattern

When making config changes, answer with:

- Scope chosen and why.
- File path changed.
- Names added or updated.
- Secrets/env vars the user must set.
- Validation performed.
- Restart/reload note if the current Somnia process will not pick up changes immediately.
