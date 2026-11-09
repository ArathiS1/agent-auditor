"""Alembic: upgrading an empty database reproduces the models exactly, and downgrading removes them."""
from pathlib import Path

import pytest

pytest.importorskip("alembic")

from alembic import command  # noqa: E402
from alembic.autogenerate import compare_metadata  # noqa: E402
from alembic.config import Config  # noqa: E402
from alembic.migration import MigrationContext  # noqa: E402
from sqlalchemy import create_engine, inspect  # noqa: E402

from agent_auditor import models  # noqa: E402,F401
from agent_auditor.db import Base  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def config_for(url: str) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    cfg.set_main_option("prepend_sys_path", str(ROOT / "src"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def test_upgrade_matches_models_and_downgrade_removes_them(tmp_path):
    url = f"sqlite:///{tmp_path / 'm.db'}"
    cfg = config_for(url)
    command.upgrade(cfg, "head")
    engine = create_engine(url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert tables == set(Base.metadata.tables) | {"alembic_version"}
        with engine.connect() as conn:
            diff = compare_metadata(MigrationContext.configure(conn, opts={"compare_type": False}), Base.metadata)
        assert diff == [], f"models and migrations disagree: {diff}"
        command.downgrade(cfg, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    finally:
        engine.dispose()


def test_upgrade_twice_is_a_no_op(tmp_path):
    cfg = config_for(f"sqlite:///{tmp_path / 'm.db'}")
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")
