"""Tests for Step 18B — golden questions JSON schema validation.

Derived from .claude/specs/18b-golden-questions.spec.md ONLY (not from the
implementation). This test validates the static golden_questions.json data file
against the spec's requirements for structure, field presence, and constraints.

The file is a data-only artifact consumed by the live verification step (18B)
and later by the Week 5 eval harness (Steps 19-20). This test confirms schema
integrity without running live agent calls.
"""

import json
from pathlib import Path

# Path to the golden questions file
_GOLDEN_QUESTIONS_PATH = Path(__file__).resolve().parent.parent / "demo" / "golden_questions.json"

# Valid MCP tool names per spec and mcp_server/server.py
VALID_TOOL_NAMES = {"query_stats", "semantic_search", "get_transactions", "system_freshness"}


def _load_golden_questions() -> list:
    """Load and parse the golden_questions.json file."""
    with open(_GOLDEN_QUESTIONS_PATH) as f:
        return json.load(f)


# --------------------------------------------------------------------------
# Structure & presence tests
# --------------------------------------------------------------------------


def test_file_is_valid_json():
    """Behavior 1: The file must be valid JSON."""
    # If this fails, the file is not valid JSON at all.
    data = _load_golden_questions()
    assert isinstance(data, list), "Golden questions must be a JSON array"


def test_array_has_exactly_six_entries():
    """Behavior 1: The JSON array must contain exactly 6 entries."""
    data = _load_golden_questions()
    assert len(data) == 6, f"Expected exactly 6 entries, got {len(data)}"


def test_each_entry_is_an_object():
    """Behavior 2: Each entry must be an object (dict)."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        assert isinstance(entry, dict), f"Entry {i} is not a dict: {type(entry)}"


# --------------------------------------------------------------------------
# Required fields presence
# --------------------------------------------------------------------------


def test_all_entries_have_required_fields():
    """Behavior 2: each entry must have id/category/question/context/tools/assertions fields."""
    data = _load_golden_questions()
    required_fields = {
        "id", "category", "question", "incident_context", "tools_expected", "assertions",
    }
    for i, entry in enumerate(data):
        assert required_fields.issubset(
            entry.keys()
        ), f"Entry {i} missing required fields. Has: {set(entry.keys())}, need: {required_fields}"


# --------------------------------------------------------------------------
# Field type and non-emptiness validation
# --------------------------------------------------------------------------


def test_id_field_is_non_empty_string():
    """Behavior 2: `id` must be a non-empty string."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        assert isinstance(
            entry["id"], str
        ), f"Entry {i}: id must be a string, got {type(entry['id'])}"
        assert entry["id"], f"Entry {i}: id must be non-empty"


def test_category_field_is_non_empty_string():
    """Behavior 2: `category` must be a non-empty string."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        assert isinstance(
            entry["category"], str
        ), f"Entry {i}: category must be a string, got {type(entry['category'])}"
        assert len(entry["category"]) > 0, f"Entry {i}: category must be non-empty"


def test_question_field_is_non_empty_string():
    """Behavior 2: `question` must be a non-empty string."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        assert isinstance(
            entry["question"], str
        ), f"Entry {i}: question must be a string, got {type(entry['question'])}"
        assert len(entry["question"]) > 0, f"Entry {i}: question must be non-empty"


def test_incident_context_is_null_or_object_with_type():
    """Behavior 2: `incident_context` must be null, or an object with at least a `type` key."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        context = entry["incident_context"]
        if context is not None:
            assert isinstance(
                context, dict
            ), f"Entry {i}: incident_context must be null or object, got {type(context)}"
            assert "type" in context, f"Entry {i}: incident_context object must have 'type' key"
            assert isinstance(
                context["type"], str
            ), f"Entry {i}: incident_context['type'] must be a string"
            assert context["type"], f"Entry {i}: incident_context['type'] must be non-empty"


def test_tools_expected_is_non_empty_list_of_valid_strings():
    """Behavior 2: `tools_expected` must be a non-empty list of valid MCP tool names."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        tools = entry["tools_expected"]
        assert isinstance(
            tools, list
        ), f"Entry {i}: tools_expected must be a list, got {type(tools)}"
        assert len(tools) > 0, f"Entry {i}: tools_expected must be non-empty"
        for j, tool in enumerate(tools):
            assert isinstance(
                tool, str
            ), f"Entry {i}: tools_expected[{j}] must be a string, got {type(tool)}"
            assert len(tool) > 0, f"Entry {i}: tools_expected[{j}] must be non-empty"
            assert tool in VALID_TOOL_NAMES, (
                f"Entry {i}: tools_expected[{j}]='{tool}' is not a valid tool name. "
                f"Valid names: {VALID_TOOL_NAMES}"
            )


def test_assertions_is_list_of_at_least_two_non_empty_strings():
    """Behavior 2: `assertions` must be a list of at least 2 non-empty strings."""
    data = _load_golden_questions()
    for i, entry in enumerate(data):
        assertions = entry["assertions"]
        assert isinstance(
            assertions, list
        ), f"Entry {i}: assertions must be a list, got {type(assertions)}"
        assert (
            len(assertions) >= 2
        ), f"Entry {i}: assertions must have at least 2 items, got {len(assertions)}"
        for j, assertion in enumerate(assertions):
            assert isinstance(
                assertion, str
            ), f"Entry {i}: assertions[{j}] must be a string, got {type(assertion)}"
            assert (
                len(assertion) > 0
            ), f"Entry {i}: assertions[{j}] must be non-empty"


# --------------------------------------------------------------------------
# Uniqueness and category coverage
# --------------------------------------------------------------------------


def test_all_ids_are_unique():
    """Each entry must have a unique id."""
    data = _load_golden_questions()
    ids = [entry["id"] for entry in data]
    dupes = [i for i in ids if ids.count(i) > 1]
    assert len(ids) == len(set(ids)), f"Duplicate IDs found: {dupes}"


def test_all_categories_are_unique():
    """Each entry should have a unique category (6 categories, 6 entries)."""
    data = _load_golden_questions()
    categories = [entry["category"] for entry in data]
    assert len(categories) == len(set(categories)), f"Duplicate categories: {categories}"


def test_has_all_six_capability_categories():
    """Edge case: The 6 capability categories should all be represented."""
    data = _load_golden_questions()
    # The spec lists: aggregation, rate comparison, structured pattern,
    # semantic/novelty, freshness, negative control
    expected_categories = {
        "Pure aggregation",
        "Rate comparison",
        "Structured pattern",
        "Semantic novelty",
        "Freshness",
        "Negative control",
    }
    actual_categories = {entry["category"] for entry in data}
    assert (
        actual_categories == expected_categories
    ), f"Expected categories {expected_categories}, got {actual_categories}"


# --------------------------------------------------------------------------
# Citation requirement validation
# --------------------------------------------------------------------------


def test_fraud_and_novel_error_entries_cite_transaction_ids():
    """Edge case: fraud/novel-error entries must have an assertion mentioning 'transaction_id'."""
    data = _load_golden_questions()

    # Map categories/ids that require citation (those implying structured/novel patterns)
    citation_required_ids = {"fraud_pattern", "novel_error"}

    for entry in data:
        entry_id = entry["id"]
        if entry_id in citation_required_ids:
            assertions_text = " ".join(entry["assertions"]).lower()
            assert "transaction_id" in assertions_text, (
                f"Entry {entry_id} ({entry['category']}) requires an assertion mentioning "
                f"'transaction_id'. Assertions: {entry['assertions']}"
            )


# --------------------------------------------------------------------------
# incident_context correctness for gated vs ungated categories
# --------------------------------------------------------------------------


def test_ungated_categories_have_null_incident_context():
    """Edge case: no-incident categories (aggregation/freshness/hallucination) get null context."""
    data = _load_golden_questions()
    ungated_ids = {"aggregation", "freshness", "hallucination_control"}

    for entry in data:
        if entry["id"] in ungated_ids:
            assert entry["incident_context"] is None, (
                f"Entry {entry['id']} ({entry['category']}) should have incident_context=null, "
                f"got {entry['incident_context']}"
            )


def test_gated_categories_have_non_null_incident_context():
    """Edge case: incident categories (gateway_rate, fraud_pattern, novel_error) have an object."""
    data = _load_golden_questions()
    gated_ids = {"gateway_rate", "fraud_pattern", "novel_error"}

    for entry in data:
        if entry["id"] in gated_ids:
            assert entry["incident_context"] is not None, (
                f"Entry {entry['id']} ({entry['category']}) should have a non-null incident_context"
            )
            assert isinstance(entry["incident_context"], dict), (
                f"Entry {entry['id']}: incident_context should be an object, "
                f"got {type(entry['incident_context'])}"
            )


def test_incident_context_objects_have_required_keys():
    """Edge case: incident_context objects have 'type' plus the keys expected for that type."""
    data = _load_golden_questions()

    for entry in data:
        if entry["incident_context"] is not None:
            context = entry["incident_context"]
            assert "type" in context, f"Entry {entry['id']}: missing 'type' key in incident_context"

            # Type-specific validation
            incident_type = context["type"]
            eid = entry["id"]
            if incident_type == "gateway_degradation":
                assert "gateway" in context, f"Entry {eid}: gateway_degradation missing 'gateway'"
                assert "duration" in context, f"Entry {eid}: gateway_degradation missing 'duration'"
                assert "severity" in context, f"Entry {eid}: gateway_degradation missing 'severity'"
            elif incident_type == "fraud_burst":
                assert "card_bin" in context, f"Entry {eid}: fraud_burst missing 'card_bin'"
                assert "duration" in context, f"Entry {eid}: fraud_burst missing 'duration'"
            elif incident_type == "novel_error_pattern":
                assert "merchant" in context, f"Entry {eid}: novel_error missing 'merchant'"
                assert "duration" in context, f"Entry {eid}: novel_error missing 'duration'"
                assert "intensity" in context, f"Entry {eid}: novel_error missing 'intensity'"


# --------------------------------------------------------------------------
# JSON serializability and edge cases
# --------------------------------------------------------------------------


def test_entire_file_is_json_serializable():
    """Edge case: the entire file must round-trip through JSON with no data loss."""
    data = _load_golden_questions()
    # Re-serialize and deserialize to confirm no non-JSON-serializable objects
    serialized = json.dumps(data)
    deserialized = json.loads(serialized)
    assert deserialized == data, "File is not properly JSON serializable"


def test_no_fields_contain_placeholder_values():
    """Behavior 3: Every field must be concrete and literal — no placeholders like `<TBD>`."""
    data = _load_golden_questions()
    forbidden_patterns = ["<TBD>", "<todo>", "[TBD]", "[TODO]", "TBD", "TODO"]

    for i, entry in enumerate(data):
        for field_name, field_value in entry.items():
            if isinstance(field_value, str):
                for pattern in forbidden_patterns:
                    assert pattern not in field_value, (
                        f"Entry {i} field '{field_name}' contains placeholder '{pattern}'"
                    )
            elif isinstance(field_value, list):
                for item in field_value:
                    if isinstance(item, str):
                        for pattern in forbidden_patterns:
                            assert pattern not in item, (
                                f"Entry {i} field '{field_name}' has placeholder '{pattern}'"
                            )
            elif isinstance(field_value, dict) and field_value is not None:
                for _key, value in field_value.items():
                    if isinstance(value, str):
                        for pattern in forbidden_patterns:
                            assert pattern not in value, (
                                f"Entry {i} field '{field_name}' has placeholder '{pattern}'"
                            )
