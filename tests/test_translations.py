"""Translation files cover every form, error and exception the integration uses."""

import ast
import json
import unittest
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[1] / "custom_components" / "maverick_music_flow"
STRINGS = json.loads((COMPONENT / "strings.json").read_text(encoding="utf-8"))
EN = json.loads((COMPONENT / "translations" / "en.json").read_text(encoding="utf-8"))
FLOW_TREE = ast.parse((COMPONENT / "config_flow.py").read_text(encoding="utf-8"))
FLOW_CLASSES = {"HomeiiFlowConfigFlow": "config", "HomeiiFlowOptionsFlow": "options"}


def _strings_in(node: ast.AST) -> set[str]:
    """Return the string constants an expression can evaluate to (not those in conditions)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.IfExp):
        return _strings_in(node.body) | _strings_in(node.orelse)
    return set()


def _flow_usage(class_name: str) -> tuple[set[str], set[str]]:
    """Return the step IDs and error keys a flow class sets as literals."""
    flow = next(
        node
        for node in FLOW_TREE.body
        if isinstance(node, ast.ClassDef) and node.name == class_name
    )
    steps: set[str] = set()
    errors: set[str] = set()
    for node in ast.walk(flow):
        if isinstance(node, ast.keyword) and node.arg == "step_id":
            steps |= _strings_in(node.value)
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.targets[0], ast.Subscript)
            and isinstance(node.targets[0].value, ast.Name)
            and node.targets[0].value.id == "errors"
        ):
            errors |= _strings_in(node.value)
    return steps, errors


def _validation_errors() -> set[str]:
    """Return the error keys the shared MA validation helpers can return."""
    found: set[str] = set()
    for node in FLOW_TREE.body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name.startswith("_validate_"):
            for child in ast.walk(node):
                if isinstance(child, ast.Return) and child.value is not None:
                    found |= _strings_in(child.value)
    return found


class TranslationTests(unittest.TestCase):
    def test_english_translation_matches_strings_json(self):
        """translations/en.json is what Home Assistant shows; it must equal the source."""
        self.assertEqual(STRINGS, EN)

    def test_every_flow_step_and_error_is_translated(self):
        for class_name, section in FLOW_CLASSES.items():
            steps, errors = _flow_usage(class_name)
            with self.subTest(flow=section):
                self.assertTrue(steps)
                self.assertLessEqual(steps, set(STRINGS[section]["step"]))
                self.assertLessEqual(errors | _validation_errors(), set(STRINGS[section]["error"]))

    def test_abort_and_exception_keys_are_translated(self):
        self.assertIn("already_configured", STRINGS["config"]["abort"])
        self.assertIn("not_loaded", STRINGS["exceptions"])


if __name__ == "__main__":
    unittest.main()
