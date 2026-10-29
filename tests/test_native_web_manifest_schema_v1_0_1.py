"""Versioned native-web manifest schema compatibility contract."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import jsonschema
import pytest


ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "native_triangle_shp"
OLD_SCHEMA_PATH = EXAMPLES / "native_web_manifest.schema.json"
SCHEMA_PATH = EXAMPLES / "native_web_manifest_v1_0_1.schema.json"
EXAMPLE_PATH = EXAMPLES / "native_web_manifest.example.json"
OLD_SCHEMA_SHA256 = "d5684ec0efadfa23a668098cb780df1c584e779d70b4105ce8440bdf1303bd12"
QA_ORIGIN = "SYNTHETIC_SMALL_NATIVE_FIXTURE"


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_v1_0_1_is_a_versioned_minimal_extension_of_legacy_v1():
    old_raw = OLD_SCHEMA_PATH.read_bytes()
    old = json.loads(old_raw.decode("utf-8"))
    schema = _read_json(SCHEMA_PATH)

    assert hashlib.sha256(old_raw).hexdigest() == OLD_SCHEMA_SHA256
    assert old["$id"] == "native-triangle-web-manifest-v1"
    assert schema["$id"] == "native-triangle-web-manifest-v1.0.1"
    assert schema["properties"]["version"]["const"] == "1.0"
    assert schema["required"] == old["required"]
    assert "data_origin" not in schema["required"]
    assert schema["properties"]["data_origin"]["type"] == "string"
    assert schema["properties"]["data_origin"]["enum"] == [QA_ORIGIN]
    assert "default" not in schema["properties"]["data_origin"]
    assert schema["additionalProperties"] is False

    legacy_projection = copy.deepcopy(schema)
    legacy_projection["$id"] = old["$id"]
    legacy_projection["title"] = old["title"]
    del legacy_projection["properties"]["data_origin"]
    legacy_projection.pop("allOf")
    assert legacy_projection == old

    jsonschema.Draft202012Validator.check_schema(schema)


def test_current_native_web_example_validates_under_v1_0_1():
    manifest = _read_json(EXAMPLE_PATH)
    schema = _read_json(SCHEMA_PATH)

    assert manifest["version"] == "1.0"
    assert manifest["data_origin"] == QA_ORIGIN
    assert manifest["qa_fixture"] is True
    assert manifest["live_solver_ran"] is False
    assert manifest["status"] == "qa_fixture_ready"
    jsonschema.Draft202012Validator(schema).validate(manifest)


def test_missing_data_origin_remains_legacy_undeclared_without_mutation():
    manifest = _read_json(EXAMPLE_PATH)
    manifest.pop("data_origin")

    jsonschema.Draft202012Validator(_read_json(SCHEMA_PATH)).validate(manifest)
    assert "data_origin" not in manifest


@pytest.mark.parametrize("value", ["LIVE", "", None, 7])
def test_only_the_fixed_qa_data_origin_is_accepted(value):
    manifest = _read_json(EXAMPLE_PATH)
    manifest["data_origin"] = value

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_read_json(SCHEMA_PATH)).validate(manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [("qa_fixture", False), ("live_solver_ran", True), ("status", "ready")],
)
def test_declared_qa_origin_requires_qa_status_binding(field, value):
    manifest = _read_json(EXAMPLE_PATH)
    manifest[field] = value

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_read_json(SCHEMA_PATH)).validate(manifest)


@pytest.mark.parametrize(
    ("field", "value"),
    [("database_status", "SUCCESS"), ("social_economy_status", "AVAILABLE"), ("run_id", {})],
)
def test_existing_schema_value_constraints_still_reject_invalid_metadata(field, value):
    manifest = _read_json(EXAMPLE_PATH)
    manifest[field] = value

    with pytest.raises(jsonschema.ValidationError):
        jsonschema.Draft202012Validator(_read_json(SCHEMA_PATH)).validate(manifest)
