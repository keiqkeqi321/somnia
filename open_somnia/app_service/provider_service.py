from __future__ import annotations

from open_somnia.app_service.models import ModelDescriptor, ProviderDescriptor
from open_somnia.config.models import ModelTraits
from open_somnia.config.settings import _materialize_provider
from open_somnia.providers.base import ProviderError
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
                    max_tokens=traits.max_tokens,
                    reasoning_level=normalize_reasoning_level(traits.reasoning_level),
                    supports_reasoning=traits.supports_reasoning,
                    supports_adaptive_reasoning=traits.supports_adaptive_reasoning,
                    is_default=model_name == profile.default_model,
                    is_active=normalized_provider == current_provider and model_name == current_model,
                    is_vision=(
                        normalized_provider == str(getattr(self.runtime.settings, "vision_provider", "") or "").strip().lower()
                        and model_name == str(getattr(self.runtime.settings, "vision_model", "") or "").strip()
                    ),
                )
            )
        return descriptors

    def switch_provider_model(self, provider_name: str, model: str) -> str:
        return self.runtime.switch_provider_model(provider_name, model)

    def set_vision_model(self, vision_provider: str | None, vision_model: str | None, *, scope: str = "project") -> str:
        return self.runtime.set_vision_model(vision_provider, vision_model, scope=scope)

    def set_reasoning_level(self, reasoning_level: str | None) -> str:
        return self.runtime.set_reasoning_level(reasoning_level)

    def debug_model_connection(self, provider_name: str, model: str) -> dict[str, str | bool]:
        normalized_provider = str(provider_name or "").strip().lower()
        normalized_model = str(model or "").strip()
        profiles = self.runtime.configured_provider_profiles()
        if normalized_provider not in profiles:
            raise ValueError(f"Provider '{normalized_provider}' is not configured.")
        profile = profiles[normalized_provider]
        if normalized_model not in profile.models:
            raise ValueError(f"Model '{normalized_model}' is not configured for provider '{normalized_provider}'.")
        provider = self.runtime._instantiate_provider(_materialize_provider(profile, normalized_model))
        try:
            turn = provider.complete(
                "You are a connection probe. Reply with OK.",
                [{"role": "user", "content": "Reply with OK."}],
                [],
                max_tokens=8,
            )
        except ProviderError as exc:
            return {"ok": False, "message": str(exc)}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}
        preview = " ".join(str(block) for block in getattr(turn, "text_blocks", []) or []).strip()
        return {"ok": True, "message": preview or "OK"}
