from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import (
    Column,
    Float,
    ForeignKey,
    LargeBinary,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Table,
    create_engine,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine


@dataclass(frozen=True, slots=True)
class StoredAccount:
    id: str
    username: str
    password_hash: str


@dataclass(frozen=True, slots=True)
class StoredDevice:
    id: str
    account_id: str
    name: str
    public_key: bytes
    created_at: float
    revoked_at: float | None


@dataclass(frozen=True, slots=True)
class StoredBrowserSession:
    account_id: str
    access_digest: str
    refresh_digest: str
    access_expires_at: float
    refresh_expires_at: float


@dataclass(frozen=True, slots=True)
class StoredIdentity:
    account_id: str
    provider: str
    provider_user_id: str
    provider_username: str
    created_at: float


class AuthMetadataStore:
    """Persists administrator/Device identity metadata and browser sessions (token digests only)."""

    def __init__(self, database_url: str) -> None:
        self.engine: Engine = create_engine(str(database_url), future=True)
        metadata = MetaData()
        self.accounts = Table(
            "remote_accounts",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("username", String(128), nullable=False, unique=True),
            Column("password_hash", String(512), nullable=False),
        )
        self.devices = Table(
            "remote_devices",
            metadata,
            Column("id", String(64), primary_key=True),
            Column("account_id", String(64), ForeignKey("remote_accounts.id"), nullable=False, index=True),
            Column("name", String(80), nullable=False),
            Column("public_key", LargeBinary(32), nullable=False),
            Column("created_at", Float, nullable=False),
            Column("revoked_at", Float, nullable=True),
        )
        self.browser_sessions = Table(
            "remote_browser_sessions",
            metadata,
            Column("access_digest", String(64), primary_key=True),
            Column("refresh_digest", String(64), nullable=False, unique=True, index=True),
            Column("account_id", String(64), ForeignKey("remote_accounts.id"), nullable=False, index=True),
            Column("access_expires_at", Float, nullable=False),
            Column("refresh_expires_at", Float, nullable=False),
        )
        self.identities = Table(
            "remote_identities",
            metadata,
            Column("provider", String(32), nullable=False),
            Column("provider_user_id", String(128), nullable=False),
            Column("account_id", String(64), ForeignKey("remote_accounts.id"), nullable=False, index=True),
            Column("provider_username", String(128), nullable=False),
            Column("created_at", Float, nullable=False),
            PrimaryKeyConstraint("provider", "provider_user_id"),
        )
        metadata.create_all(self.engine)

    def load_accounts(self) -> list[StoredAccount]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.accounts)).mappings().all()
        return [
            StoredAccount(id=row["id"], username=row["username"], password_hash=row["password_hash"])
            for row in rows
        ]

    def save_account(self, account: StoredAccount) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(self.accounts).values(
                    id=account.id,
                    username=account.username,
                    password_hash=account.password_hash,
                )
            )

    def update_account_password(self, account_id: str, password_hash: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                update(self.accounts)
                .where(self.accounts.c.id == str(account_id))
                .values(password_hash=str(password_hash))
            )

    def load_devices(self) -> list[StoredDevice]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.devices)).mappings().all()
        return [
            StoredDevice(
                id=row["id"],
                account_id=row["account_id"],
                name=row["name"],
                public_key=bytes(row["public_key"]),
                created_at=float(row["created_at"]),
                revoked_at=float(row["revoked_at"]) if row["revoked_at"] is not None else None,
            )
            for row in rows
        ]

    def save_device(self, device: StoredDevice) -> None:
        values = {
            "account_id": device.account_id,
            "name": device.name,
            "public_key": device.public_key,
            "created_at": device.created_at,
            "revoked_at": device.revoked_at,
        }
        with self.engine.begin() as connection:
            existing = connection.execute(
                select(self.devices.c.id).where(self.devices.c.id == device.id)
            ).first()
            if existing is None:
                connection.execute(insert(self.devices).values(id=device.id, **values))
            else:
                connection.execute(update(self.devices).where(self.devices.c.id == device.id).values(**values))

    def load_browser_sessions(self) -> list[StoredBrowserSession]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.browser_sessions)).mappings().all()
        return [
            StoredBrowserSession(
                account_id=row["account_id"],
                access_digest=row["access_digest"],
                refresh_digest=row["refresh_digest"],
                access_expires_at=float(row["access_expires_at"]),
                refresh_expires_at=float(row["refresh_expires_at"]),
            )
            for row in rows
        ]

    def save_browser_session(self, session: StoredBrowserSession) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(self.browser_sessions).values(
                    account_id=session.account_id,
                    access_digest=session.access_digest,
                    refresh_digest=session.refresh_digest,
                    access_expires_at=session.access_expires_at,
                    refresh_expires_at=session.refresh_expires_at,
                )
            )

    def delete_browser_session(self, access_digest: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(self.browser_sessions).where(self.browser_sessions.c.access_digest == access_digest)
            )

    def load_identities(self) -> list[StoredIdentity]:
        with self.engine.connect() as connection:
            rows = connection.execute(select(self.identities)).mappings().all()
        return [
            StoredIdentity(
                account_id=row["account_id"],
                provider=row["provider"],
                provider_user_id=row["provider_user_id"],
                provider_username=row["provider_username"],
                created_at=float(row["created_at"]),
            )
            for row in rows
        ]

    def save_identity(self, identity: StoredIdentity) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                insert(self.identities).values(
                    account_id=identity.account_id,
                    provider=identity.provider,
                    provider_user_id=identity.provider_user_id,
                    provider_username=identity.provider_username,
                    created_at=identity.created_at,
                )
            )

    def delete_identity(self, provider: str, provider_user_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                delete(self.identities).where(
                    self.identities.c.provider == provider,
                    self.identities.c.provider_user_id == provider_user_id,
                )
            )

    def close(self) -> None:
        self.engine.dispose()
