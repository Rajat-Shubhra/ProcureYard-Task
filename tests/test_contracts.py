import json
from pathlib import Path

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str) -> dict[str, object]:
    return json.loads((ROOT / relative_path).read_text(encoding="utf-8"))


def test_model_interpretation_example_matches_schema() -> None:
    schema = load_json("contracts/model_interpretation.schema.json")
    example = load_json("contracts/model_interpretation.example.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)


def test_runtime_classification_example_matches_schema() -> None:
    schema = load_json("project-analysis/response_classification.schema.json")
    example = load_json("project-analysis/response_classification.example.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)


def test_multi_complaint_model_example_matches_v2_schema() -> None:
    schema = load_json("contracts/model_interpretation.v2.schema.json")
    example = load_json("contracts/model_interpretation.v2.example.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    assert len(example["complaints"]) == 2  # type: ignore[arg-type]
    _assert_strict_objects(schema)


def test_multi_complaint_runtime_example_matches_v2_schema() -> None:
    schema = load_json("project-analysis/response_classification.v2.schema.json")
    example = load_json("project-analysis/response_classification.v2.example.json")
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(example)
    assert len(example["complaints"]) == 2  # type: ignore[arg-type]
    assert len(example["decisions"]) == 2  # type: ignore[arg-type]


def test_policy_contract_has_expected_frozen_versions() -> None:
    policy = load_json("contracts/payout_policy.v1.json")

    assert policy["policy_version"] == "payout-policy-v1"
    assert policy["normalization_version"] == "source-normalization-v1"
    assert policy["auto_payment"]["maximum_rupees_per_verified_dispute"] == 200


def _assert_strict_objects(value: object) -> None:
    if isinstance(value, dict):
        if value.get("type") == "object":
            properties = value.get("properties", {})
            assert value.get("additionalProperties") is False
            assert set(value.get("required", [])) == set(properties)
        for child in value.values():
            _assert_strict_objects(child)
    elif isinstance(value, list):
        for child in value:
            _assert_strict_objects(child)
