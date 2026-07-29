from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Column, Float, ForeignKey, LargeBinary, MetaData, String, Table, create_engine, delete, insert, select, update
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

    def close(self) -> None:
        self.engine.dispose()
