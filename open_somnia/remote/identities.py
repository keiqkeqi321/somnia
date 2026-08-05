from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
import time
from typing import Callable

from sqlalchemy.exc import IntegrityError

from open_somnia.remote.auth_store import AuthMetadataStore, StoredIdentity


Clock = Callable[[], float]


class IdentityTaken(ValueError):
    pass


class LastAuthMethodError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Identity:
    account_id: str
    provider: str
    provider_user_id: str
    provider_username: str
    created_at: float


class IdentityRegistry:
    """Maps external OAuth identities (e.g. GitHub users) to Relay accounts.

    Kept separate from ``RemoteAuth`` on purpose: the registry owns only the
    (provider, provider_user_id) → account binding and never reaches back
    into account/session logic. ``unbind`` receives ``has_other_login`` from
    the caller so this module does not depend on ``RemoteAuth``.
    """

    def __init__(self, metadata_store: AuthMetadataStore | None = None, *, clock: Clock = time.time) -> None:
        self._clock = clock
        self._metadata_store = metadata_store
        self._lock = RLock()
        self._identities: dict[tuple[str, str], Identity] = {}
        if metadata_store is not None:
            for stored in metadata_store.load_identities():
                identity = Identity(
                    account_id=stored.account_id,
                    provider=stored.provider,
                    provider_user_id=stored.provider_user_id,
                    provider_username=stored.provider_username,
                    created_at=stored.created_at,
                )
                self._identities[(identity.provider, identity.provider_user_id)] = identity

    def resolve(self, provider: str, provider_user_id: str) -> Identity | None:
        with self._lock:
            return self._identities.get((str(provider), str(provider_user_id)))

    def bind(self, account_id: str, provider: str, provider_user_id: str, provider_username: str) -> Identity:
        key = (str(provider), str(provider_user_id))
        with self._lock:
            existing = self._identities.get(key)
            if existing is not None:
                if existing.account_id == str(account_id):
                    return existing
                raise IdentityTaken("This identity is already linked to another account.")
            identity = Identity(
                account_id=str(account_id),
                provider=key[0],
                provider_user_id=key[1],
                provider_username=str(provider_username),
                created_at=self._clock(),
            )
            if self._metadata_store is not None:
                try:
                    self._metadata_store.save_identity(
                        StoredIdentity(
                            account_id=identity.account_id,
                            provider=identity.provider,
                            provider_user_id=identity.provider_user_id,
                            provider_username=identity.provider_username,
                            created_at=identity.created_at,
                        )
                    )
                except IntegrityError:
                    # Another Relay instance bound this identity first.
                    raise IdentityTaken("This identity is already linked to another account.") from None
            self._identities[key] = identity
            return identity

    def list_for_account(self, account_id: str) -> list[Identity]:
        with self._lock:
            return [identity for identity in self._identities.values() if identity.account_id == str(account_id)]

    def unbind(self, account_id: str, provider: str, *, has_other_login: bool) -> Identity | None:
        with self._lock:
            owned = [identity for identity in self._identities.values() if identity.account_id == str(account_id)]
            target = next((identity for identity in owned if identity.provider == str(provider)), None)
            if target is None:
                return None
            if not has_other_login and len(owned) <= 1:
                raise LastAuthMethodError("Cannot unlink the last remaining sign-in method.")
            if self._metadata_store is not None:
                self._metadata_store.delete_identity(target.provider, target.provider_user_id)
            self._identities.pop((target.provider, target.provider_user_id), None)
            return target
