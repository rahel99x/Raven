"""Shared count and byte admission for source and prepared metric caches.

A visible Advisor workspace can read dozens of small job histories per frame.
Allow that working set without increasing either cache's retained-byte budget.
"""

MAX_SERIES_CACHE_ENTRIES = 128
MAX_SERIES_CACHE_BYTES = 24 << 20
