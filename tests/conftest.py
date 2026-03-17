"""Shared test fixtures for Vivosun GrowHub integration tests."""

import pycares

# Pre-warm the pycares shutdown manager thread so it exists before the HA test
# harness snapshots threads_before.  Without this, the first test that triggers
# DNS resolution (via aiohttp -> aiodns -> pycares) creates the thread mid-test,
# causing a spurious "lingering thread" teardown failure.
pycares._shutdown_manager.start()
