"""A ground station attached to a DB with no `detections` table (e.g. a
spectrum-only node where no BLE/WiFi scanner ever created it) must degrade
gracefully to empty results, not raise -- mirroring how the spectrum_hits
queries already tolerate their table being absent."""

from __future__ import annotations

from db import Database


def test_detection_queries_tolerate_missing_table(tmp_path):
    # Database only creates device_tags; nothing creates `detections` here.
    db = Database(str(tmp_path / "d.sqlite3"))
    assert db.max_detection_id() == 0
    assert db.detections_since(0) == []
    assert db.device_summary() == []
    assert db.localizations() == {}
    assert db.heatmap() == []
