from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ProviderPreset:
    id: str
    label: str
    provider_name: str
    provider_type: str
    base_url: str
    models: tuple[str, ...]
    default_model: str
    api_key_url: str = ""
    notes: str = ""


PROVIDER_PRESETS: tuple[ProviderPreset, ...] = (
    ProviderPreset(
        id="deepseek",
        label="DeepSeek",
        provider_name="deepseek",
        provider_type="openai",
        base_url="https://api.deepseek.com",
        models=("deepseek-v4-flash", "deepseek-v4-pro"),
        default_model="deepseek-v4-flash",
        api_key_url="https://platform.deepseek.com/api_keys",
        notes="Official OpenAI-compatible endpoint.",
    ),
    ProviderPreset(
        id="glm-coding-plan",
        label="GLM Coding Plan",
        provider_name="glm-coding-plan",
        provider_type="openai",
        base_url="	https://open.bigmodel.cn/api/coding/paas/v4",
        models=("GLM-5.2", "GLM-5-Turbo", "GLM-4.7"),
        default_model="GLM-5.2",
        api_key_url="https://bigmodel.cn/usercenter/proj-mgmt/apikeys",
        notes="Z.ai / BigModel OpenAI-compatible API.",
    ),
    ProviderPreset(
        id="bailian-token-plan",
        label="Bailian Token Plan",
        provider_name="bailian-token-plan",
        provider_type="openai",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        models=(
            "qwen3.7-max",
            "qwen3.7-plus",
            "qwen3.6-flash",
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "glm-5.2",
            "kimi-k2.7-code",
            "MiniMax-M3",
            "mimo-v2.5-pro",
        ),
        default_model="qwen3.7-max",
        api_key_url="https://bailian.console.aliyun.com/",
        notes="Alibaba Cloud Bailian / DashScope compatible-mode API.",
    ),
    ProviderPreset(
        id="mimo-token-plan",
        label="MiMo Token Plan",
        provider_name="mimo-token-plan",
        provider_type="openai",
        base_url="https://token-plan-cn.xiaomimimo.com/v1",
        models=("mimo-v2.5-pro","mimo-v2.5"),
        default_model="mimo-v2.5-pro",
        api_key_url="https://platform.xiaomimimo.com/",
        notes="Xiaomi MiMo Token Plan OpenAI-compatible API.",
    ),
    ProviderPreset(
        id="kimi-code",
        label="Kimi Coding Plan",
        provider_name="kimi-code",
        provider_type="openai",
        base_url="https://api.moonshot.cn/v1",
        models=("kimi-k2.5","kimi-k2.6","kimi-k2.7-code","kimi-k2.7-code-highspeed"),
        default_model="kimi-k2.6",
        api_key_url="https://platform.moonshot.cn/console/api-keys",
        notes="Moonshot AI OpenAI-compatible API.",
    ),
    ProviderPreset(
        id="minimax-token-plan",
        label="MiniMax Token Plan",
        provider_name="minimax-token-plan",
        provider_type="openai",
        base_url="https://api.minimaxi.com/v1",
        models=("minimax-m3", "minimax-m2.7", "minimax-m2.7-highspeed", "minimax-m2.5", "minimax-m2.5-highspeed"),
        default_model="minimax-m3",
        api_key_url="https://platform.minimaxi.com/",
        notes="MiniMax OpenAI-compatible API.",
    ),
    ProviderPreset(
        id="openai",
        label="OpenAI",
        provider_name="openai",
        provider_type="openai",
        base_url="https://api.openai.com/v1",
        models=("gpt-4.1", "gpt-4.1-mini", "gpt-4.1-nano"),
        default_model="gpt-4.1",
        api_key_url="https://platform.openai.com/api-keys",
        notes="Official OpenAI API.",
    ),
    ProviderPreset(
        id="anthropic",
        label="Anthropic Claude",
        provider_name="anthropic",
        provider_type="anthropic",
        base_url="https://api.anthropic.com",
        models=("claude-sonnet-4-5", "claude-opus-4-1", "claude-haiku-4-5"),
        default_model="claude-sonnet-4-5",
        api_key_url="https://console.anthropic.com/settings/keys",
        notes="Official Anthropic API.",
    ),
)


def list_provider_presets() -> list[ProviderPreset]:
    return list(PROVIDER_PRESETS)


def provider_preset_by_id(preset_id: str) -> ProviderPreset | None:
    normalized = str(preset_id or "").strip().lower()
    return next((preset for preset in PROVIDER_PRESETS if preset.id == normalized), None)


def serialize_provider_preset(preset: ProviderPreset) -> dict[str, object]:
    return {
        "id": preset.id,
        "label": preset.label,
        "provider_name": preset.provider_name,
        "provider_type": preset.provider_type,
        "base_url": preset.base_url,
        "models": list(preset.models),
        "default_model": preset.default_model,
        "api_key_url": preset.api_key_url,
        "notes": preset.notes,
    }
