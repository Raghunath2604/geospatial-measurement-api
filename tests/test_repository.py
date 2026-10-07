"""Unit tests for SQLite repository storage layer."""

from __future__ import annotations

import os
import tempfile

import pytest

from app.domain import (
    FileStatus,
    Measurement,
    MeasurementStatus,
    ParsedFeature,
)
from app.storage.repository import Repository


@pytest.fixture
def repo() -> Repository:
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = os.path.join(tmp_dir, "test_repo.db")
        yield Repository(db_path)


def test_repository_lifecycle_and_transaction(repo: Repository) -> None:
    """Test full file lifecycle from creation to atomic completion."""
    file_id = "test-file-123"
    rec = repo.create_file(file_id=file_id, filename="test.kml")
    assert rec.id == file_id
    assert rec.status == FileStatus.PROCESSING
    assert rec.feature_count == 0

    # Build features & measurements
    f0 = ParsedFeature(
        index=0,
        geometry_type="Polygon",
        geometry={"type": "Polygon", "coordinates": []},
        properties={"name": "Parcel A"},
    )
    m0 = Measurement(
        feature_index=0,
        geometry_type="Polygon",
        status=MeasurementStatus.MEASURED,
        area_sq_m=50000.0,
        measurement_crs="EPSG:32643",
    )

    f1 = ParsedFeature(
        index=1,
        geometry_type="Point",
        geometry={"type": "Point", "coordinates": [0, 0]},
        properties={"name": "Point B"},
    )
    m1 = Measurement(
        feature_index=1,
        geometry_type="Point",
        status=MeasurementStatus.NOT_APPLICABLE,
    )

    f2 = ParsedFeature(
        index=2,
        geometry_type="LineString",
        geometry={"type": "LineString", "coordinates": []},
        properties={"name": "Road C"},
    )
    m2 = Measurement(
        feature_index=2,
        geometry_type="LineString",
        status=MeasurementStatus.MEASURED,
        length_m=1250.0,
        measurement_crs="EPSG:32643",
    )

    completed = repo.complete_file(
        file_id=file_id,
        crs="EPSG:4326",
        features_and_measurements=[(f0, m0), (f1, m1), (f2, m2)],
        warnings=["Test warning"],
    )

    assert completed.status == FileStatus.COMPLETED
    assert completed.feature_count == 3
    assert completed.crs == "EPSG:4326"
    assert completed.warnings == ["Test warning"]

    # Verify whole-file SQL summary
    summary, features, total = repo.get_measurements(file_id=file_id, limit=2, offset=0)
    assert total == 3
    assert len(features) == 2  # Limit 2
    assert summary.feature_count == 3
    assert summary.measured_count == 2
    assert summary.not_applicable_count == 1
    assert summary.total_area_sq_m == 50000.0
    assert summary.total_length_m == 1250.0

    # Test pagination offset
    _, features_page2, _ = repo.get_measurements(file_id=file_id, limit=2, offset=2)
    assert len(features_page2) == 1
    assert features_page2[0].feature_index == 2


def test_repository_fail_file(repo: Repository) -> None:
    """Verify file failure persistence."""
    file_id = "fail-file-456"
    repo.create_file(file_id=file_id, filename="bad.kml")
    failed = repo.fail_file(
        file_id=file_id, error="XML parse error", warnings=["bad line"]
    )

    assert failed.status == FileStatus.FAILED
    assert failed.error == "XML parse error"
    assert failed.warnings == ["bad line"]
