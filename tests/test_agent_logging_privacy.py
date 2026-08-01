import ast
from pathlib import Path


SENSITIVE_LOG_VALUES = {
    "_last_user",
    "_retrieval_query",
    "round_response",
    "resp_preview",
}


def _logger_calls(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if not isinstance(func, ast.Attribute):
            continue
        if func.attr not in {
            "debug",
            "info",
            "warning",
            "error",
            "exception",
        }:
            continue
        if isinstance(func.value, ast.Name) and func.value.id == "logger":
            yield node


def _unsafe_sensitive_names(node):
    # Lengths are safe metadata: len(prompt) does not expose prompt content.
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "len"
        and len(node.args) == 1
        and not node.keywords
    ):
        return set()

    if isinstance(node, ast.Name):
        if node.id in SENSITIVE_LOG_VALUES:
            return {node.id}
        return set()

    leaked = set()
    for child in ast.iter_child_nodes(node):
        leaked.update(_unsafe_sensitive_names(child))
    return leaked


def test_agent_logs_do_not_include_prompt_or_response_content():
    source = Path("src/agent_loop.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    violations = []
    for call in _logger_calls(tree):
        leaked = set()

        for argument in call.args:
            leaked.update(_unsafe_sensitive_names(argument))
        for keyword in call.keywords:
            leaked.update(_unsafe_sensitive_names(keyword.value))

        if leaked:
            violations.append((call.lineno, sorted(leaked)))

    assert violations == []


def test_agent_logs_do_not_restore_known_content_previews():
    source = Path("src/agent_loop.py").read_text(encoding="utf-8")

    forbidden = (
        "Preview:",
        "latest=%r",
        "retrieval_query=%r",
        "resp_preview",
    )

    found = [marker for marker in forbidden if marker in source]
    assert found == []
