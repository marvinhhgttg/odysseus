"""Child process that boots the real application once and reports what happened.

Run by ``tests/test_runtime_smoke.py`` via ``subprocess``. It must stay import-free
of test helpers: it is executed with the repository on ``PYTHONPATH`` but with the
working directory pointed at a throwaway data directory, so that the smoke test can
prove the app does not depend on being started from inside its own checkout.

Prints exactly one ``__ODYSSEUS_SMOKE__<json>`` line on success. Everything else on
stdout/stderr is application logging and is surfaced by the test on failure.
"""
from __future__ import annotations

import json
import socket
import sys

MARKER = "__ODYSSEUS_SMOKE__"

# Record every bind before anything imports the app, so a stray server start
# during import cannot hide. Ephemeral binds (port 0) are normal for probes.
_observed_binds: list[list] = []
_original_bind = socket.socket.bind


def _recording_bind(self, address):
    try:
        if isinstance(address, tuple) and len(address) >= 2:
            _observed_binds.append([str(address[0]), int(address[1])])
        else:
            _observed_binds.append([str(address), -1])
    except Exception:
        _observed_binds.append([repr(address), -1])
    return _original_bind(self, address)


socket.socket.bind = _recording_bind  # type: ignore[method-assign]


def main() -> int:
    import app

    routes = sorted({getattr(route, "path", "") for route in app.app.routes})
    methods = {}
    for route in app.app.routes:
        path = getattr(route, "path", "")
        for method in getattr(route, "methods", None) or ():
            methods.setdefault(path, set()).add(method)

    payload = {
        "routes": routes,
        "route_count": len(routes),
        "methods": {path: sorted(values) for path, values in methods.items()},
        "binds": _observed_binds,
        "cwd_is_repo": False,
    }
    print(MARKER + json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
