"""Tests for the cold-path timing helper."""

import logging

from pep_oracle.timing import timed


def test_timed_logs_phase_and_duration(caplog):
    with caplog.at_level(logging.INFO, logger="pep_oracle.timing"):
        with timed("unit.phase"):
            pass
    rec = next(r for r in caplog.records if "unit.phase" in r.getMessage())
    msg = rec.getMessage()
    assert "phase=unit.phase" in msg
    assert "ms=" in msg


def test_timed_appends_extra_fields(caplog):
    with caplog.at_level(logging.INFO, logger="pep_oracle.timing"):
        with timed("unit.sized", chunks=42, bytes=1000):
            pass
    msg = next(r.getMessage() for r in caplog.records if "unit.sized" in r.getMessage())
    assert "chunks=42" in msg
    assert "bytes=1000" in msg


def test_timed_logs_even_on_exception(caplog):
    with caplog.at_level(logging.INFO, logger="pep_oracle.timing"):
        try:
            with timed("unit.boom"):
                raise ValueError("boom")
        except ValueError:
            pass
    assert any("phase=unit.boom" in r.getMessage() for r in caplog.records)
