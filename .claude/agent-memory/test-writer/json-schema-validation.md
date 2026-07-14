---
name: json-schema-validation
description: Static JSON data file schema validation test patterns — load, structure, field presence, type checking, constraint validation, uniqueness, placeholder detection
metadata:
  type: reference
---

## Pattern: JSON schema validation for static data files

When testing a static JSON data artifact (e.g., `demo/golden_questions.json`), validate:

### Structure & presence (required)
- File exists and is valid JSON (can be loaded via `json.load()`)
- Top level is expected type (array, object, etc.)
- Expected number of entries (exact count tests catch omissions/duplicates)
- Each entry is correct type (usually dict for array of objects)

### Required fields
- All required fields present on each entry
- Use `set.issubset()` to check field presence across all entries at once

### Field types & constraints (individual)
- Type checks: `isinstance(value, str)`, `isinstance(value, list)`, etc.
- Non-emptiness: strings, lists should have `len() > 0`
- Enumerations: if value must be from a fixed set, check membership in `VALID_*` constant
- Nested objects: if field can be null OR object, test both cases
- Nested required fields: if field is object, validate required keys within it

### Nested list items
- Each item in a list has correct type
- Each item in a list is non-empty (if strings)
- Each item in a list matches expected values (if enumerated)
- List has minimum length (e.g., at least 2 assertions)

### Uniqueness & coverage
- Set difference to detect duplicates: `len(items) == len(set(items))`
- Coverage tests: expected categories/types all present, no extras

### Domain-specific rules
- Citation requirements: if any assertion contains "transaction_id", flag entries that need it
- Gated vs ungated: some entries should have incident_context, others should not
- Placeholder detection: scan all string fields for forbidden patterns like `<TBD>`, `[TODO]`
- Re-serialization check: `json.loads(json.dumps(data)) == data` catches non-serializable objects

### File layout
- Single test file per data artifact (e.g., `tests/test_golden_questions.py`)
- Constants for valid values (`VALID_TOOL_NAMES = {"query_stats", ...}`)
- Load helper at module level (`_load_golden_questions() -> list`)
- Group tests by concern: structure, fields, types, uniqueness, domain rules, serialization
- ~19 test cases for a 6-entry artifact with medium complexity

### Example test structure
```python
def test_artifacts_has_exactly_N_entries():
    data = _load_file()
    assert len(data) == N

def test_all_entries_have_required_fields():
    data = _load_file()
    required = {"field1", "field2", ...}
    for i, entry in enumerate(data):
        assert required.issubset(entry.keys())

def test_field_name_is_non_empty_string():
    data = _load_file()
    for i, entry in enumerate(data):
        assert isinstance(entry["field"], str)
        assert len(entry["field"]) > 0

def test_list_field_contains_only_valid_enums():
    data = _load_file()
    valid = {"option_a", "option_b", ...}
    for i, entry in enumerate(data):
        for j, item in enumerate(entry["list_field"]):
            assert item in valid
```

## Why this approach works
- **Deterministic**: no live dependencies, no flaky waits
- **Comprehensive**: structure + presence + types + constraints + domain rules
- **Maintainable**: organized by concern, easy to add new categories
- **Independent**: tests are derived from spec, not from code/implementation details
- **Clear failures**: specific assertion messages pinpoint what's wrong (entry index, field name, expected vs got)
