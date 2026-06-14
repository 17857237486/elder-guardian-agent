from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    pass


connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    columns = {column["name"] for column in inspect(engine).get_columns("v2_normalized_events")}
    additions = {
        "source_kind": "VARCHAR(64)",
        "evidence_json": "TEXT DEFAULT '[]'",
        "frame_set_id": "VARCHAR(128)",
        "image_refs_json": "TEXT DEFAULT '[]'",
        "rule_risk_level": "VARCHAR(8)",
        "local_risk_level": "VARCHAR(8)",
        "cloud_risk_level": "VARCHAR(8)",
        "final_risk_level": "VARCHAR(8)",
        "decision_source": "VARCHAR(32) DEFAULT 'rule'",
        "confidence": "FLOAT DEFAULT 0",
        "local_semantics": "VARCHAR(256)",
    }
    with engine.begin() as connection:
        for name, ddl in additions.items():
            if name not in columns:
                connection.execute(text(f"ALTER TABLE v2_normalized_events ADD COLUMN {name} {ddl}"))

