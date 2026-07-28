import json
from pathlib import Path

from jsonschema import Draft202012Validator


SCHEMA_DIR = Path(__file__).resolve().parents[1] / "schemas"


def test_all_pipeline_schemas_are_valid_draft_2020_12():
    schemas = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(SCHEMA_DIR.glob("*.schema.json"))
    ]

    assert len(schemas) == 7
    assert len({schema["$id"] for schema in schemas}) == len(schemas)
    for schema in schemas:
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        Draft202012Validator.check_schema(schema)
