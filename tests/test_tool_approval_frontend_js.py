"""Runs the Node-based tool-approval frontend suite (tests/frontend/*.test.mjs).

Covers the browser half of the approval reload contract: what
``static/js/chat.js`` renders when ``sessions.js`` calls
``chatModule.recoverToolApprovals(id)`` after a page reload or session switch.

The backend half lives in ``tests/test_tool_approval_routes.py`` (persistence and
``GET /api/tool-approvals``) and ``tests/test_tool_approval_chat_stream.py``
(resume through the stream). This wrapper pins the piece in between: session
filtering, status filtering, reload idempotency, and the approve/reject/resume
affordances on a recovered card.

The approval logic is sliced out of the real ``chat.js`` by marker text and run
against a minimal DOM shim, mirroring the source-surgery loader in
``tests/streaming/markdownHarness.mjs``. No browser, no extra dependencies.
Skipped when node is unavailable, mirroring
``tests/test_streaming_segmenter_js.py``.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent
_HAS_NODE = shutil.which("node") is not None


@pytest.mark.skipif(not _HAS_NODE, reason="node binary not on PATH")
def test_tool_approval_frontend_suite():
    test_files = sorted(str(p) for p in (_REPO / "tests" / "frontend").glob("*.test.mjs"))
    assert test_files, "no frontend test files found"

    result = subprocess.run(
        ["node", "--test", *test_files],
        cwd=_REPO,
        capture_output=True,
        timeout=180,
        text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            f"node --test failed:\nSTDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        )
