"""Where the library database lives by default: production data is reachable
only from the packaged helper."""

from __future__ import annotations

import sys

from scanny_boy.library import db


def test_override_wins(monkeypatch, tmp_path):
    target = tmp_path / "elsewhere" / "library.db"
    monkeypatch.setenv("SCANNY_BOY_LIBRARY_DB", str(target))
    assert db.library_db_path() == target


def test_source_run_defaults_to_development_data(monkeypatch, tmp_path):
    monkeypatch.delenv("SCANNY_BOY_LIBRARY_DB")
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(db.Path, "home", lambda: tmp_path)
    assert db.library_db_path() == (
        tmp_path / "Library" / "Application Support" / "ScannyBoy-Dev" / "library.db"
    )


def test_packaged_helper_defaults_to_production_data(monkeypatch, tmp_path):
    monkeypatch.delenv("SCANNY_BOY_LIBRARY_DB")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(db.Path, "home", lambda: tmp_path)
    assert db.library_db_path() == (
        tmp_path / "Library" / "Application Support" / "ScannyBoy" / "library.db"
    )
