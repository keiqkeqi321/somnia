from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from open_somnia.cli.prompting import choose_item_interactively, prompt_provider_details_interactively
from open_somnia.config.models import ProviderProfileSettings
from open_somnia.config.settings import global_config_path


@dataclass(slots=True)
class ProviderProfileSubmission:
    previous_provider_name: str | None
    provider_name: str
    provider_type: str
    base_url: str
    api_key: str
    models: list[str]


def parse_model_ids(raw_value: str) -> list[str]:
    models: list[str] = []
    for chunk in raw_value.split(","):
        model = chunk.strip()
        if model and model not in models:
            models.append(model)
    return models


def default_base_url(provider_type: str) -> str:
    if provider_type == "openai":
        return "https://api.openai.com/v1"
    return "https://api.anthropic.com"


def choose_provider_type_interactively(*, current_type: str | None = None) -> str | None:
    preferred = (current_type or "").strip().lower()
    ordered = ["anthropic", "openai"]
    if preferred in ordered:
        ordered.remove(preferred)
        ordered.insert(0, preferred)
    title = "Provider Type"
    subtitle = "Choose the compatibility mode for this shared provider profile."
    return choose_item_interactively(
        title,
        subtitle,
        [(item, item) for item in ordered],
    )


def choose_provider_target_interactively(existing_profiles: Mapping[str, ProviderProfileSettings]) -> str | None:
    if not existing_profiles:
        return "__add__"
    items = [("__add__", "Add shared provider")]
    items.extend(
        (
            name,
            f"Edit {name} | type={profile.provider_type} | default={profile.default_model} | models={len(profile.models)}",
        )
        for name, profile in sorted(existing_profiles.items())
    )
    return choose_item_interactively(
        "Manage Providers",
        "Add a shared provider or edit an existing one.",
        items,
    )


def collect_provider_profile_interactively(
    existing_profiles: Mapping[str, ProviderProfileSettings],
    *,
    previous_provider_name: str | None = None,
) -> ProviderProfileSubmission | None:
    current_profile = existing_profiles.get(previous_provider_name or "")
    provider_type = choose_provider_type_interactively(
        current_type=current_profile.provider_type if current_profile is not None else None
    )
    if provider_type is None:
        return None

    provider_name = current_profile.name if current_profile is not None else provider_type
    base_url = current_profile.base_url or default_base_url(provider_type) if current_profile is not None else default_base_url(provider_type)
    models = ", ".join(current_profile.models) if current_profile is not None else ""
    existing_api_key = current_profile.api_key if current_profile is not None else ""
    api_key_hint = "Leave blank to keep the existing API key." if existing_api_key else ""

    while True:
        details = prompt_provider_details_interactively(
            provider_type=provider_type,
            default_provider_name=provider_name,
            default_base_url=base_url,
            default_models=models,
            api_key_hint=api_key_hint,
        )
        if details is None:
            return None

        provider_name = details["provider_name"].strip().lower()
        base_url = details["base_url"].strip()
        typed_api_key = details["api_key"].strip()
        models = details["models"].strip()
        normalized_models = parse_model_ids(models)
        api_key = typed_api_key or existing_api_key

        if not provider_name:
            print("Provider Name is required.")
            continue
        if provider_name != (previous_provider_name or "") and provider_name in existing_profiles:
            print(f"Provider '{provider_name}' already exists.")
            continue
        if not base_url:
            print("Base URL is required.")
            continue
        if not api_key:
            print("API Key is required.")
            continue
        if not normalized_models:
            print("At least one model id is required. Use commas to separate models.")
            continue

        confirmation = choose_item_interactively(
            "Confirm Provider Setup",
            (
                f"Provider name: {provider_name}\n"
                f"Provider type: {provider_type}\n"
                f"Base URL: {base_url}\n"
                f"API key: {'*' * min(len(api_key), 8) if api_key else '(empty)'}\n"
                f"Models: {', '.join(normalized_models)}\n"
                f"Config file: {global_config_path()}\n"
                f"{'Update' if current_profile is not None else 'Save'} this shared provider profile?"
            ),
            [
                ("save", "Save and continue"),
                ("cancel", "Cancel"),
            ],
        )
        if confirmation != "save":
            return None
        return ProviderProfileSubmission(
            previous_provider_name=previous_provider_name,
            provider_name=provider_name,
            provider_type=provider_type,
            base_url=base_url,
            api_key=api_key,
            models=normalized_models,
        )
