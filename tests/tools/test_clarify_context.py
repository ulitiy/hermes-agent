"""Context reaches every UI without changing the question echoed back to the model."""
import json

import pytest

from tools.clarify_tool import clarify_tool
from tools.registry import registry


@pytest.mark.parametrize("multi_select", [False, True])
def test_context_precedes_choices_through_real_registry(multi_select):
    seen = []

    def native(questions):
        for entry in questions:
            seen.append((entry["question"], entry["choices"], entry["multi_select"]))
        return {"answers": {"q0": ["staging"] if multi_select else "staging"}, "outcome": "submitted"}

    item = {"question": "Where?", "context": " Staging is reversible; prod affects users. ",
            "choices": ["staging", "prod"], "multi_select": multi_select}
    result = json.loads(registry.dispatch("clarify", {"questions": [item]}, callback=native))
    answer = result["responses"][0]
    assert answer["question"] == item["question"]
    assert answer["user_response"] == (["staging"] if multi_select else "staging")
    assert answer["status"] == "answered"
    assert result["outcome"] == "submitted"
    assert seen == [(f"{item['context'].strip()}\n\nWhere?", ["staging (Recommended)", "prod"], multi_select)]
