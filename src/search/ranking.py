"""Compatibility wrapper for the canonical services.search.ranking module.

Rationale: the actual ranking implementation lives in
``services.search.ranking``, which is what the search runtime
(services/search/core.py) imports. This module used to hold a parallel copy;
it now aliases via ``sys.modules`` replacement (mirroring
``src/search/core.py``, ``providers.py``, ``analytics.py``, ``cache.py``,
``content.py`` and ``query.py``) so the two cannot drift out of sync again
while old ``src.search.ranking`` imports keep working.
"""

import sys

from services.search import ranking as _ranking

sys.modules[__name__] = _ranking