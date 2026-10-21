import pytest
from kb_core.domain import NodeRecord
from kb_core.registry import default_registry
from kb_core.validation import validate, ValidationError

REG = default_registry()


def _rec(**over):
    base = dict(source="agent", external_id="x", type="note",
               content="hello", slug="hello")
    base.update(over)
    return NodeRecord(**base)


def test_valid_record_passes():
    validate(_rec(), REG)  # no exception


@pytest.mark.parametrize("field,value", [
    ("content", ""),
    ("content", "   "),
    ("source", ""),
    ("external_id", ""),
    ("slug", ""),
])
def test_missing_required_fields_fail(field, value):
    with pytest.raises(ValidationError):
        validate(_rec(**{field: value}), REG)


def test_unknown_type_fails():
    with pytest.raises(ValidationError):
        validate(_rec(type="bogus"), REG)


def test_slug_with_only_punctuation_fails():
    with pytest.raises(ValidationError):
        validate(_rec(slug="!!!"), REG)
