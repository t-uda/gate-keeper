"""External-tool adapters that integrate behind ``Backend.EXTERNAL``.

Each adapter is a per-tool concrete consumer of
``gate_keeper.backends.external.ExternalAdapter``. Adapters register
themselves at CLI entry rather than at import time so test isolation in
``tests/test_external_backend.py`` is preserved.
"""
