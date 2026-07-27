from __future__ import annotations

from pathlib import Path
import time

from open_somnia.config.models import AgentSettings, AppSettings, ProviderProfileSettings, ProviderSettings, RuntimeSettings, StorageSettings


def remote_tracer_settings(root: Path) -> AppSettings:
    data_dir = root / ".open_somnia"
    paths = {
        "transcripts_dir": data_dir / "transcripts",
        "sessions_dir": data_dir / "sessions",
        "tasks_dir": data_dir / "tasks",
        "inbox_dir": data_dir / "inbox",
        "team_dir": data_dir / "team",
        "jobs_dir": data_dir / "jobs",
        "requests_dir": data_dir / "requests",
        "logs_dir": data_dir / "logs",
    }
    for path in [data_dir, *paths.values()]:
        path.mkdir(parents=True, exist_ok=True)
    return AppSettings(
        workspace_root=root,
        agent=AgentSettings(name="Somnia"),
        provider=ProviderSettings(
            name="openai",
            provider_type="openai",
            model="fake-model",
            api_key="fake",
            base_url="http://localhost",
        ),
        runtime=RuntimeSettings(),
        storage=StorageSettings(data_dir=data_dir, **paths),
        provider_profiles={
            "openai": ProviderProfileSettings(
                name="openai",
                provider_type="openai",
                models=["fake-model"],
                default_model="fake-model",
                api_key="fake",
                base_url="http://localhost",
            ),
        },
    )


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False
