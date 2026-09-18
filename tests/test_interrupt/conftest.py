from collections.abc import Iterator

import pytest

from conductor.interrupt import listener


@pytest.fixture(autouse=True)
def _reset_baseline_cache() -> Iterator[None]:
    # Requirement: the module-level baseline cache must never leak between
    # tests running in the same pytest process (issue #290). Retiring also
    # closes the baseline's owned descriptor so fds don't leak between tests.
    listener._retire_cached_baseline()
    yield
    listener._retire_cached_baseline()
