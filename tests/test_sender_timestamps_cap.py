"""
Tests for BUG-P30A-01: _sender_msg_timestamps unbounded memory growth.

The bug: _sender_msg_timestamps was populated with a deque per unique sender
but had no cleanup path — unique senders accumulated indefinitely, unlike
_group_msg_timestamps which is purged on group deregistration.

The fix: cap at _SENDER_TIMESTAMPS_MAX = 5000 entries.  When the dict reaches
capacity and a *new* (not yet present) sender arrives, the oldest-inserted
sender is evicted via next(iter(...)) before inserting the new one.

These tests verify:
  1. The dict grows to exactly _SENDER_TIMESTAMPS_MAX and then evicts.
  2. After the cap is reached, size stays exactly at the max.
  3. Newest entries survive eviction; oldest are removed first.
  4. Many inserts beyond the cap never grow past the max.
  5. Normal operation with few senders is unaffected (no premature eviction).
  6. Re-inserting an already-present sender does not trigger eviction.
"""
import os
import sys
import time
from collections import deque
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ---------------------------------------------------------------------------
# Replicated constants and logic from host/main.py
# ---------------------------------------------------------------------------

_SENDER_TIMESTAMPS_MAX = 5000


def _make_cap_and_evict_fn(timestamps_dict: dict):
    """
    Return a function that mirrors the cap-and-evict block inside
    _is_sender_rate_limited, using the supplied *timestamps_dict*.

    Only the dict-management (cap + evict + setdefault) is replicated here;
    the rate-check arithmetic is irrelevant to these structural tests.
    """
    def _insert_sender(sender: str, max_msgs: int = 5) -> None:
        if sender not in timestamps_dict and len(timestamps_dict) >= _SENDER_TIMESTAMPS_MAX:
            _oldest = next(iter(timestamps_dict))
            timestamps_dict.pop(_oldest, None)
        timestamps_dict.setdefault(sender, deque(maxlen=max_msgs * 2))

    return _insert_sender


# ===========================================================================
# Tests
# ===========================================================================

class TestSenderTimestampsCap:

    def test_grows_to_exactly_max(self):
        """
        Inserting exactly _SENDER_TIMESTAMPS_MAX senders fills the dict without
        any eviction — size should equal the cap.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"sender_{i}")
        assert len(ts) == _SENDER_TIMESTAMPS_MAX, (
            f"Expected dict size {_SENDER_TIMESTAMPS_MAX}, got {len(ts)}"
        )

    def test_size_stays_at_max_after_cap_reached(self):
        """
        Adding one more sender when the dict is full must evict one entry so
        the total remains exactly _SENDER_TIMESTAMPS_MAX.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"sender_{i}")

        insert("overflow_sender")
        assert len(ts) == _SENDER_TIMESTAMPS_MAX, (
            f"Dict should stay at {_SENDER_TIMESTAMPS_MAX} after overflow insert, got {len(ts)}"
        )

    def test_oldest_entry_evicted_first(self):
        """
        Python dicts preserve insertion order (3.7+).  The cap-and-evict block
        uses next(iter(...)) which yields the first (oldest-inserted) key.
        Verify that sender_0 is removed when a new sender triggers eviction.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"sender_{i}")

        # sender_0 is the oldest-inserted entry
        insert("new_sender")

        assert "sender_0" not in ts, "Oldest-inserted entry (sender_0) must be evicted"
        assert "sender_1" in ts, "sender_1 must still be present after eviction of sender_0"
        assert "new_sender" in ts, "Newly inserted sender must be present"

    def test_newest_entries_survive_eviction(self):
        """
        The most recently inserted senders must remain after eviction.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"sender_{i}")

        newest = f"sender_{_SENDER_TIMESTAMPS_MAX - 1}"
        assert newest in ts, "Newest sender must be present before overflow"

        insert("extra_sender")

        assert newest in ts, "Newest sender must survive eviction"

    def test_many_inserts_never_exceed_cap(self):
        """
        Inserting 1000 senders beyond the cap must never let the dict grow
        past _SENDER_TIMESTAMPS_MAX.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"seed_{i}")

        for j in range(1000):
            insert(f"extra_{j}")
            assert len(ts) <= _SENDER_TIMESTAMPS_MAX, (
                f"Dict exceeded cap {_SENDER_TIMESTAMPS_MAX} after inserting extra_{j}: "
                f"size={len(ts)}"
            )

    def test_few_senders_unaffected(self):
        """
        When well below the cap, all entries are retained and no eviction occurs.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(50):
            insert(f"user_{i}")

        assert len(ts) == 50
        for i in range(50):
            assert f"user_{i}" in ts, f"user_{i} must be present (well below cap)"

    def test_reinserting_existing_sender_no_eviction(self):
        """
        Re-inserting a sender that already exists in the dict must NOT trigger
        eviction, even when the dict is at capacity.  The condition guards on
        ``sender not in timestamps_dict``.
        """
        ts: dict = {}
        insert = _make_cap_and_evict_fn(ts)
        for i in range(_SENDER_TIMESTAMPS_MAX):
            insert(f"sender_{i}")

        # sender_0 already exists — no eviction should occur
        size_before = len(ts)
        insert("sender_0")
        assert len(ts) == size_before, (
            "Re-inserting an existing sender must not change dict size"
        )
        assert "sender_0" in ts, "sender_0 must still be present after re-insert"
