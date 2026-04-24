from __future__ import annotations

from open_somnia.app_service.models import ModelDescriptor, ProviderDescriptor
from open_somnia.config.models import ModelTraits
from open_somnia.reasoning import normalize_reasoning_level
from open_somnia.runtime.agent import OpenAgentRuntime


class ProviderService:
    def __init__(self, runtime: OpenAgentRuntime) -> None:
        self.runtime = runtime

    def list_providers(self) -> list[ProviderDescriptor]:
        current_provider = str(self.runtime.settings.provider.name).strip().lower()
        current_model = str(self.runtime.settings.provider.model).strip()
        profiles = self.runtime.configured_provider_profiles()
        providers: list[ProviderDescriptor] = []
        for name, profile in sorted(profiles.items()):
            is_active = name == current_provider
            providers.append(
                ProviderDescriptor(
                    name=name,
                    provider_type=profile.provider_type,
                    default_model=profile.default_model,
                    models=list(profile.models),
                    active_model=current_model if is_active else None,
                    reasoning_level=normalize_reasoning_level(profile.reasoning_level),
                    is_active=is_active,
                )
            )
        return providers

    def list_models(self, provider_name: str | None = None) -> list[ModelDescriptor]:
        normalized_provider = str(provider_name or self.runtime.settings.provider.name).strip().lower()
        profiles = self.runtime.configured_provider_profiles()
        if normalized_provider not in profiles:
            raise ValueError(f"Provider '{normalized_provider}' is not configured.")
        profile = profiles[normalized_provider]
        current_provider = str(self.runtime.settings.provider.name).strip().lower()
        current_model = str(self.runtime.settings.provider.model).strip()
        descriptors: list[ModelDescriptor] = []
        for model_name in profile.models:
            traits = profile.model_traits.get(model_name, ModelTraits())
            descriptors.append(
                ModelDescriptor(
                    provider_name=normalized_provider,
                    name=model_name,
                    context_window_tokens=traits.context_window_tokens,
                    supports_reasoning=traits.supports_reasoning,
                    supports_adaptive_reasoning=traits.supports_adaptive_reasoning,
                    is_default=model_name == profile.default_model,
                    is_active=normalized_provider == current_provider and model_name == current_model,
                )
            )
        return descriptors

    def switch_provider_model(self, provider_name: str, model: str) -> str:
        return self.runtime.switch_provider_model(provider_name, model)

    def set_reasoning_level(self, reasoning_level: str | None) -> str:
        return self.runtime.set_reasoning_level(reasoning_level)
