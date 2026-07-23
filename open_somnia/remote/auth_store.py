from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import Column, Float, ForeignKey, LargeBinary, MetaData, String, Table, create_engine, insert, select, update
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


class AuthMetadataStore:
    """Persists only administrator and Device identity metadata."""

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

    def close(self) -> None:
        self.engine.dispose()
