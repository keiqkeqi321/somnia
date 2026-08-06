from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import hmac
import re
import secrets
from threading import RLock
import time
from typing import Callable, Mapping
import uuid

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from open_somnia.remote.auth_store import AuthMetadataStore, StoredAccount, StoredBrowserSession, StoredDevice


Clock = Callable[[], float]


class PairingCodeInvalid(ValueError):
    pass


class PairingCodeExpired(ValueError):
    pass


class PairingCodeUsed(ValueError):
    pass


class PairingRateLimited(ValueError):
    pass


class PairSessionRateLimited(ValueError):
    pass


class PairSessionSecretInvalid(ValueError):
    pass


class PairSessionExpired(ValueError):
    pass


class LoginRateLimited(ValueError):
    pass


class UsernameRateLimited(ValueError):
    pass


class RegistrationRateLimited(ValueError):
    pass


class UsernameTaken(ValueError):
    pass


class CredentialPolicyError(ValueError):
    pass


class CurrentPasswordRequired(ValueError):
    pass


class CurrentPasswordInvalid(ValueError):
    pass


USERNAME_PATTERN = re.compile(r"[a-zA-Z0-9_.-]{3,32}")


def sanitize_external_username(hint: str) -> str:
    """Best-effort cleanup of an external profile name into a valid username base.

    Illegal characters become ``-``, the result is truncated to 32 characters
    and padded to the 3-character minimum; an empty hint falls back to
    ``user``. The caller deduplicates collisions.
    """
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]", "-", str(hint).strip())[:32]
    if not cleaned:
        return "user"
    return cleaned if len(cleaned) >= 3 else cleaned.ljust(3, "-")


@dataclass(frozen=True, slots=True)
class Account:
    id: str
    username: str
    password_hash: str


@dataclass(frozen=True, slots=True)
class Device:
    id: str
    account_id: str
    name: str
    public_key: bytes
    created_at: float
    revoked_at: float | None = None


@dataclass(frozen=True, slots=True)
class BrowserTokens:
    access_token: str
    refresh_token: str
    access_expires_at: float
    refresh_expires_at: float


@dataclass(slots=True)
class _BrowserSession:
    account_id: str
    access_digest: str
    refresh_digest: str
    access_expires_at: float
    refresh_expires_at: float
    revoked: bool = False


@dataclass(slots=True)
class _Pairing:
    account_id: str
    device_name: str
    code_digest: str
    expires_at: float
    used_at: float | None = None


@dataclass(slots=True)
class _PairSession:
    secret_digest: str
    expires_at: float
    code: str | None = None
    suggested_name: str = ""


class RemoteAuth:
    """Owns browser sessions, pairing grants, and Device identity metadata."""

    def __init__(
        self,
        administrators: Mapping[str, str],
        *,
        secret_key: bytes | None = None,
        clock: Clock = time.time,
        access_ttl_seconds: int = 15 * 60,
        refresh_ttl_seconds: int = 30 * 24 * 60 * 60,
        pairing_ttl_seconds: int = 5 * 60,
        pairing_attempt_limit: int = 10,
        pairing_attempt_window_seconds: int = 60,
        pair_session_attempt_limit: int = 10,
        pair_session_attempt_window_seconds: int = 60 * 60,
        login_attempt_limit: int = 10,
        login_attempt_window_seconds: int = 60,
        login_username_attempt_limit: int = 10,
        login_username_attempt_window_seconds: int = 10 * 60,
        registration_attempt_limit: int = 5,
        registration_attempt_window_seconds: int = 60 * 60,
        max_browser_sessions: int = 10_000,
        max_pairings: int = 10_000,
        max_pair_sessions: int = 10_000,
        max_attempt_sources: int = 10_000,
        metadata_store: AuthMetadataStore | None = None,
    ) -> None:
        self._clock = clock
        self.access_ttl_seconds = int(access_ttl_seconds)
        self.refresh_ttl_seconds = int(refresh_ttl_seconds)
        self.pairing_ttl_seconds = int(pairing_ttl_seconds)
        self.pairing_attempt_limit = int(pairing_attempt_limit)
        self.pairing_attempt_window_seconds = int(pairing_attempt_window_seconds)
        self.pair_session_attempt_limit = int(pair_session_attempt_limit)
        self.pair_session_attempt_window_seconds = int(pair_session_attempt_window_seconds)
        self.login_attempt_limit = int(login_attempt_limit)
        self.login_attempt_window_seconds = int(login_attempt_window_seconds)
        self.login_username_attempt_limit = int(login_username_attempt_limit)
        self.login_username_attempt_window_seconds = int(login_username_attempt_window_seconds)
        self.registration_attempt_limit = int(registration_attempt_limit)
        self.registration_attempt_window_seconds = int(registration_attempt_window_seconds)
        self.max_browser_sessions = max(1, int(max_browser_sessions))
        self.max_pairings = max(1, int(max_pairings))
        self.max_pair_sessions = max(1, int(max_pair_sessions))
        self.max_attempt_sources = max(1, int(max_attempt_sources))
        self._secret_key = secret_key or secrets.token_bytes(32)
        self._password_hasher = PasswordHasher()
        self._metadata_store = metadata_store
        self._lock = RLock()
        self._accounts_by_username: dict[str, Account] = {}
        self._accounts_by_id: dict[str, Account] = {}
        self._sessions_by_access: dict[str, _BrowserSession] = {}
        self._sessions_by_refresh: dict[str, _BrowserSession] = {}
        self._pairings: dict[str, _Pairing] = {}
        self._pairing_attempts: dict[str, list[float]] = {}
        self._pair_sessions: dict[str, _PairSession] = {}
        self._pair_session_attempts: dict[str, list[float]] = {}
        self._login_attempts: dict[str, list[float]] = {}
        self._login_failures: dict[str, list[float]] = {}
        self._registration_attempts: dict[str, list[float]] = {}
        self._devices: dict[str, Device] = {}
        if metadata_store is not None:
            for stored in metadata_store.load_accounts():
                account = Account(id=stored.id, username=stored.username, password_hash=stored.password_hash)
                self._accounts_by_username[account.username.casefold()] = account
                self._accounts_by_id[account.id] = account
            for stored in metadata_store.load_devices():
                self._devices[stored.id] = Device(
                    id=stored.id,
                    account_id=stored.account_id,
                    name=stored.name,
                    public_key=stored.public_key,
                    created_at=stored.created_at,
                    revoked_at=stored.revoked_at,
                )
            # Restore live browser sessions so a Relay restart (e.g. deploy)
            # does not invalidate every signed-in browser. Expired rows are
            # pruned from the store as they are skipped.
            now = self._clock()
            for stored in metadata_store.load_browser_sessions():
                if stored.refresh_expires_at <= now:
                    metadata_store.delete_browser_session(stored.access_digest)
                    continue
                session = _BrowserSession(
                    account_id=stored.account_id,
                    access_digest=stored.access_digest,
                    refresh_digest=stored.refresh_digest,
                    access_expires_at=stored.access_expires_at,
                    refresh_expires_at=stored.refresh_expires_at,
                )
                self._sessions_by_access[session.access_digest] = session
                self._sessions_by_refresh[session.refresh_digest] = session
        for username, password in administrators.items():
            self._add_account(username, password)

    def authenticate_password(self, username: str, password: str, *, source: str) -> Account | None:
        normalized = str(username).strip().casefold()
        with self._lock:
            now = self._clock()
            recent = self._recent_attempts(
                self._login_attempts,
                source,
                now=now,
                window_seconds=self.login_attempt_window_seconds,
            )
            if len(recent) >= self.login_attempt_limit:
                raise LoginRateLimited("Too many login attempts. Try again later.")
            failures = self._recent_attempts(
                self._login_failures,
                normalized,
                now=now,
                window_seconds=self.login_username_attempt_window_seconds,
            )
            if len(failures) >= self.login_username_attempt_limit:
                raise UsernameRateLimited("Too many login attempts. Try again later.")
            recent.append(now)
            account = self._accounts_by_username.get(normalized)
        verified = False
        if account is None or account.password_hash == "":
            # Accounts without a password (OAuth-only) take the same dummy-hash
            # path as unknown usernames so the timing stays uniform.
            self._password_hasher.hash(str(password))
        else:
            try:
                verified = bool(self._password_hasher.verify(account.password_hash, str(password)))
            except (VerifyMismatchError, InvalidHashError):
                verified = False
        with self._lock:
            if verified:
                self._login_attempts.pop(source, None)
                self._login_failures.pop(normalized, None)
            else:
                failures = self._recent_attempts(
                    self._login_failures,
                    normalized,
                    now=now,
                    window_seconds=self.login_username_attempt_window_seconds,
                )
                failures.append(now)
        return account if verified else None

    def register_account(self, username: str, password: str, *, source: str) -> Account:
        normalized = str(username).strip()
        password = str(password)
        if USERNAME_PATTERN.fullmatch(normalized) is None:
            raise CredentialPolicyError(
                "Username must be 3-32 characters using only letters, digits, '_', '.', or '-'."
            )
        if len(password) < 8 or password.casefold() == normalized.casefold():
            raise CredentialPolicyError("Password must be at least 8 characters and differ from the username.")
        with self._lock:
            now = self._clock()
            recent = self._recent_attempts(
                self._registration_attempts,
                source,
                now=now,
                window_seconds=self.registration_attempt_window_seconds,
            )
            if len(recent) >= self.registration_attempt_limit:
                raise RegistrationRateLimited("Too many registration attempts. Try again later.")
            recent.append(now)
        password_hash = self._password_hasher.hash(password)
        with self._lock:
            key = normalized.casefold()
            if key in self._accounts_by_username:
                raise UsernameTaken("Username is already taken.")
            account = Account(id=uuid.uuid4().hex, username=normalized, password_hash=password_hash)
            self._accounts_by_username[key] = account
            self._accounts_by_id[account.id] = account
            if self._metadata_store is not None:
                self._metadata_store.save_account(
                    StoredAccount(id=account.id, username=account.username, password_hash=account.password_hash)
                )
            return account

    def register_external_account(self, *, username_hint: str, source: str) -> Account:
        """Create a passwordless account for an external (OAuth) sign-in.

        The username is derived from ``username_hint`` (see
        ``sanitize_external_username``) with ``-2`` … ``-99`` suffixes on
        casefold collisions and a random suffix as the last resort. The empty
        ``password_hash`` marks the account as having no password login.
        """
        base = sanitize_external_username(username_hint)
        with self._lock:
            now = self._clock()
            recent = self._recent_attempts(
                self._registration_attempts,
                source,
                now=now,
                window_seconds=self.registration_attempt_window_seconds,
            )
            if len(recent) >= self.registration_attempt_limit:
                raise RegistrationRateLimited("Too many registration attempts. Try again later.")
            recent.append(now)
            username = self._available_username(base)
            account = Account(id=uuid.uuid4().hex, username=username, password_hash="")
            self._accounts_by_username[username.casefold()] = account
            self._accounts_by_id[account.id] = account
            if self._metadata_store is not None:
                self._metadata_store.save_account(
                    StoredAccount(id=account.id, username=account.username, password_hash=account.password_hash)
                )
            return account

    def set_password(
        self,
        account_id: str,
        password: str,
        *,
        current_password: str | None = None,
        source: str,
    ) -> Account:
        """Set the password of a passwordless (OAuth) account or change an existing one.

        The policy matches ``register_account``. Accounts that already have a
        password must present the current one; verification attempts share the
        per-``source`` login rate-limit window so brute forcing the current
        password is throttled exactly like password logins.
        """
        password = str(password)
        with self._lock:
            account = self._accounts_by_id.get(str(account_id))
            if account is None:
                raise ValueError("Account not found.")
            if len(password) < 8 or password.casefold() == account.username.casefold():
                raise CredentialPolicyError("Password must be at least 8 characters and differ from the username.")
            has_password = bool(account.password_hash)
            if has_password:
                if not current_password:
                    raise CurrentPasswordRequired("Current password is required.")
                now = self._clock()
                recent = self._recent_attempts(
                    self._login_attempts,
                    source,
                    now=now,
                    window_seconds=self.login_attempt_window_seconds,
                )
                if len(recent) >= self.login_attempt_limit:
                    raise LoginRateLimited("Too many login attempts. Try again later.")
                recent.append(now)
        if has_password:
            try:
                verified = bool(self._password_hasher.verify(account.password_hash, str(current_password)))
            except (VerifyMismatchError, InvalidHashError):
                verified = False
            if not verified:
                raise CurrentPasswordInvalid("Current password is invalid.")
        password_hash = self._password_hasher.hash(password)
        with self._lock:
            updated = Account(id=account.id, username=account.username, password_hash=password_hash)
            self._accounts_by_username[updated.username.casefold()] = updated
            self._accounts_by_id[updated.id] = updated
            if self._metadata_store is not None:
                self._metadata_store.update_account_password(updated.id, updated.password_hash)
            self._login_attempts.pop(source, None)
            return updated

    def _available_username(self, base: str) -> str:
        if base.casefold() not in self._accounts_by_username:
            return base
        for suffix in range(2, 100):
            tail = f"-{suffix}"
            candidate = f"{base[: 32 - len(tail)]}{tail}"
            if candidate.casefold() not in self._accounts_by_username:
                return candidate
        while True:
            candidate = f"{base[:25]}-{secrets.token_hex(3)}"
            if candidate.casefold() not in self._accounts_by_username:
                return candidate

    def issue_browser_tokens(self, account_id: str) -> BrowserTokens:
        now = self._clock()
        access_token = _token()
        refresh_token = _token()
        session = _BrowserSession(
            account_id=account_id,
            access_digest=self._digest(access_token),
            refresh_digest=self._digest(refresh_token),
            access_expires_at=now + self.access_ttl_seconds,
            refresh_expires_at=now + self.refresh_ttl_seconds,
        )
        with self._lock:
            self._prune_sessions(now)
            while len(self._sessions_by_refresh) >= self.max_browser_sessions:
                oldest = next(iter(self._sessions_by_refresh.values()))
                self._remove_session(oldest)
            self._sessions_by_access[session.access_digest] = session
            self._sessions_by_refresh[session.refresh_digest] = session
            if self._metadata_store is not None:
                self._metadata_store.save_browser_session(
                    StoredBrowserSession(
                        account_id=session.account_id,
                        access_digest=session.access_digest,
                        refresh_digest=session.refresh_digest,
                        access_expires_at=session.access_expires_at,
                        refresh_expires_at=session.refresh_expires_at,
                    )
                )
        return BrowserTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=session.access_expires_at,
            refresh_expires_at=session.refresh_expires_at,
        )

    def resolve_access(self, token: str | None) -> Account | None:
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._prune_sessions(now)
            session = self._sessions_by_access.get(self._digest(token))
            if session is None or session.access_expires_at <= now:
                return None
            return self._accounts_by_id.get(session.account_id)

    def rotate_refresh(self, token: str | None) -> BrowserTokens | None:
        if not token:
            return None
        with self._lock:
            now = self._clock()
            self._prune_sessions(now)
            session = self._sessions_by_refresh.get(self._digest(token))
            if session is None or session.refresh_expires_at <= now:
                return None
            account_id = session.account_id
            self._remove_session(session)
        return self.issue_browser_tokens(account_id)

    def revoke_browser_tokens(self, access_token: str | None, refresh_token: str | None) -> None:
        with self._lock:
            sessions = []
            if access_token:
                sessions.append(self._sessions_by_access.get(self._digest(access_token)))
            if refresh_token:
                sessions.append(self._sessions_by_refresh.get(self._digest(refresh_token)))
            for session in sessions:
                if session is not None:
                    self._remove_session(session)

    def create_pairing(self, account_id: str, device_name: str) -> tuple[str, float]:
        code = _pairing_code()
        now = self._clock()
        pairing = _Pairing(
            account_id=account_id,
            device_name=str(device_name).strip(),
            code_digest=self._digest(code),
            expires_at=now + self.pairing_ttl_seconds,
        )
        with self._lock:
            self._prune_pairings(now)
            while len(self._pairings) >= self.max_pairings:
                self._pairings.pop(next(iter(self._pairings)))
            self._pairings[pairing.code_digest] = pairing
        return code, pairing.expires_at

    def claim_pairing(self, code: str, public_key: bytes, *, source: str) -> Device:
        digest = self._digest(str(code).strip().upper())
        with self._lock:
            now = self._clock()
            recent = self._recent_attempts(
                self._pairing_attempts,
                source,
                now=now,
                window_seconds=self.pairing_attempt_window_seconds,
            )
            if len(recent) >= self.pairing_attempt_limit:
                raise PairingRateLimited("Too many pairing attempts. Try again later.")
            recent.append(now)
            pairing = self._pairings.get(digest)
            if pairing is None:
                raise PairingCodeInvalid("Pairing code is invalid.")
            if pairing.used_at is not None:
                raise PairingCodeUsed("Pairing code has already been used.")
            if pairing.expires_at <= self._clock():
                self._pairings.pop(digest, None)
                raise PairingCodeExpired("Pairing code has expired.")
            device = Device(
                id=uuid.uuid4().hex,
                account_id=pairing.account_id,
                name=pairing.device_name,
                public_key=bytes(public_key),
                created_at=self._clock(),
            )
            if self._metadata_store is not None:
                self._metadata_store.save_device(_stored_device(device))
            pairing.used_at = self._clock()
            self._pairing_attempts.pop(source, None)
            self._devices[device.id] = device
            return device

    def create_pair_session(self, *, source: str, suggested_name: str = "") -> tuple[str, str, float]:
        """Create an in-memory device-flow pair session: (session_id, secret, expires_at).

        ``suggested_name`` is the device-side default the approving browser
        pre-fills as the Device name; the user can still edit it.
        """
        session_id = uuid.uuid4().hex
        secret = _token()
        with self._lock:
            now = self._clock()
            recent = self._recent_attempts(
                self._pair_session_attempts,
                source,
                now=now,
                window_seconds=self.pair_session_attempt_window_seconds,
            )
            if len(recent) >= self.pair_session_attempt_limit:
                raise PairSessionRateLimited("Too many pair session requests. Try again later.")
            recent.append(now)
            self._prune_pair_sessions(now)
            while len(self._pair_sessions) >= self.max_pair_sessions:
                self._pair_sessions.pop(next(iter(self._pair_sessions)))
            self._pair_sessions[session_id] = _PairSession(
                secret_digest=self._digest(secret),
                expires_at=now + self.pairing_ttl_seconds,
                suggested_name=str(suggested_name).strip()[:80],
            )
            return session_id, secret, self._pair_sessions[session_id].expires_at

    def pair_session_status(self, session_id: str, secret: str) -> dict[str, str]:
        """Return {"status": pending|approved|expired}; an approved code is returned exactly once."""
        with self._lock:
            session = self._pair_sessions.get(str(session_id))
            if session is None or session.expires_at <= self._clock():
                return {"status": "expired"}
            if not hmac.compare_digest(session.secret_digest, self._digest(str(secret))):
                raise PairSessionSecretInvalid("Pair session secret is invalid.")
            if session.code is None:
                return {"status": "pending", "suggested_name": session.suggested_name}
            code = session.code
            self._pair_sessions.pop(str(session_id), None)
            return {"status": "approved", "code": code}

    def approve_pair_session(self, session_id: str, secret: str, account_id: str, device_name: str) -> str:
        """Bind a fresh pairing code to the session and mark it approved. Returns the code."""
        with self._lock:
            session = self._pair_sessions.get(str(session_id))
            if session is None or session.expires_at <= self._clock():
                self._pair_sessions.pop(str(session_id), None)
                raise PairSessionExpired("Pair session has expired.")
            if not hmac.compare_digest(session.secret_digest, self._digest(str(secret))):
                raise PairSessionSecretInvalid("Pair session secret is invalid.")
            code, _ = self.create_pairing(account_id, device_name)
            session.code = code
            return code

    def device(self, device_id: str) -> Device | None:
        with self._lock:
            return self._devices.get(str(device_id))

    def devices_for_account(self, account_id: str) -> list[Device]:
        with self._lock:
            return [device for device in self._devices.values() if device.account_id == account_id]

    def revoke_device(self, account_id: str, device_id: str) -> Device | None:
        with self._lock:
            device = self._devices.get(str(device_id))
            if device is None or device.account_id != account_id:
                return None
            if device.revoked_at is None:
                device = Device(
                    id=device.id,
                    account_id=device.account_id,
                    name=device.name,
                    public_key=device.public_key,
                    created_at=device.created_at,
                    revoked_at=self._clock(),
                )
                if self._metadata_store is not None:
                    self._metadata_store.save_device(_stored_device(device))
                self._devices[device.id] = device
            return device

    def _add_account(self, username: str, password_or_hash: str) -> None:
        normalized = str(username).strip()
        if not normalized:
            raise ValueError("Administrator username is required.")
        key = normalized.casefold()
        if key in self._accounts_by_username:
            return
        password_hash = str(password_or_hash)
        if not password_hash.startswith("$argon2"):
            password_hash = self._password_hasher.hash(password_hash)
        account = Account(id=uuid.uuid4().hex, username=normalized, password_hash=password_hash)
        self._accounts_by_username[key] = account
        self._accounts_by_id[account.id] = account
        if self._metadata_store is not None:
            self._metadata_store.save_account(
                StoredAccount(id=account.id, username=account.username, password_hash=account.password_hash)
            )

    def _digest(self, value: str) -> str:
        return hmac.new(self._secret_key, value.encode("utf-8"), hashlib.sha256).hexdigest()

    def _prune_sessions(self, now: float) -> None:
        for session in list(self._sessions_by_refresh.values()):
            if session.revoked or session.refresh_expires_at <= now:
                self._remove_session(session)
            elif session.access_expires_at <= now:
                self._sessions_by_access.pop(session.access_digest, None)

    def _remove_session(self, session: _BrowserSession) -> None:
        session.revoked = True
        self._sessions_by_access.pop(session.access_digest, None)
        self._sessions_by_refresh.pop(session.refresh_digest, None)
        if self._metadata_store is not None:
            self._metadata_store.delete_browser_session(session.access_digest)

    def _prune_pairings(self, now: float) -> None:
        for digest, pairing in list(self._pairings.items()):
            if pairing.expires_at <= now:
                self._pairings.pop(digest, None)

    def _prune_pair_sessions(self, now: float) -> None:
        for session_id, session in list(self._pair_sessions.items()):
            if session.expires_at <= now:
                self._pair_sessions.pop(session_id, None)

    def _recent_attempts(
        self,
        attempts_by_source: dict[str, list[float]],
        source: str,
        *,
        now: float,
        window_seconds: int,
    ) -> list[float]:
        cutoff = now - window_seconds
        for candidate, attempts in list(attempts_by_source.items()):
            recent = [attempt for attempt in attempts if attempt > cutoff]
            if recent:
                attempts_by_source[candidate] = recent
            else:
                attempts_by_source.pop(candidate, None)
        if source not in attempts_by_source:
            while len(attempts_by_source) >= self.max_attempt_sources:
                attempts_by_source.pop(next(iter(attempts_by_source)))
            attempts_by_source[source] = []
        return attempts_by_source[source]


def encode_bytes(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_bytes(value: str, *, expected_length: int) -> bytes:
    text = str(value).strip()
    padding = "=" * (-len(text) % 4)
    try:
        decoded = base64.b64decode(text + padding, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("Value is not valid base64url.") from exc
    if len(decoded) != expected_length:
        raise ValueError(f"Value must decode to {expected_length} bytes.")
    return decoded


def device_challenge_payload(device_id: str, nonce: str) -> bytes:
    return f"somnia-device-auth-v1\n{device_id}\n{nonce}".encode("utf-8")


def _token() -> str:
    return secrets.token_urlsafe(32)


def _pairing_code() -> str:
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


def _stored_device(device: Device) -> StoredDevice:
    return StoredDevice(
        id=device.id,
        account_id=device.account_id,
        name=device.name,
        public_key=device.public_key,
        created_at=device.created_at,
        revoked_at=device.revoked_at,
    )
