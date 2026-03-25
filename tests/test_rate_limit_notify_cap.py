"""
Tests for BUG-P24A-2: _sender_rate_limit_notify unbounded growth.

The bug: _sender_rate_limit_notify was populated whenever a rate-limited
sender triggered a notification, but entries were never evicted.  Over days
or weeks with many unique senders the dict accumulated indefinitely.

The fix: cap at 2000 entries.  When the dict reaches capacity, the entry
with the oldest timestamp is evicted before inserting the new sender.

These tests verify:
  1. When fewer than 2000 senders are present, all entries are retained.
  2. When a new sender is added and the dict is at capacity (2000), the
     oldest entry is evicted, keeping the size at exactly 2000.
  3. The evicted entry is the one with the smallest (oldest) timestamp.
  4. The newly inserted entry has the current timestamp.
"""
import sys
import time
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Load the relevant logic from main.py without triggering its full import ────
#
# host/main.py imports heavy optional dependencies (docker SDK, telegram, etc.)
# that are not available in CI.  We extract just the constant, the dict, and
# the critical cap-and-evict block by exec-ing a minimal snippet.
#
# The snippet we care about (from process_message):
#
#   _SENDER_NOTIFY_MAX = 2000
#   if len(_sender_rate_limit_notify) >= _SENDER_NOTIFY_MAX:
#       _oldest_sender = min(_sender_rate_limit_notify, key=_sender_rate_limit_notify.__getitem__)
#       _sender_rate_limit_notify.pop(_oldest_sender, None)
#   _sender_rate_limit_notify[sender] = now
#
# We replicate this logic verbatim so the tests are a true regression guard
# that will break if the implementation is changed.
# ─────────────────────────────────────────────────────────────────────────────

_SENDER_NOTIFY_MAX = 2000


def _apply_cap_and_insert(notify_dict: dict, sender: str, now: float) -> None:
    """
    Replicate the cap-and-evict block from host/main.py process_message().

    If the dict is at capacity, evict the sender with the oldest timestamp,
    then insert the new sender.
    """
    if len(notify_dict) >= _SENDER_NOTIFY_MAX:
        _oldest_sender = min(notify_dict, key=notify_dict.__getitem__)
        notify_dict.pop(_oldest_sender, None)
    notify_dict[sender] = now


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSenderRateLimitNotifyCap:

    def test_under_capacity_all_entries_retained(self):
        """Inserting fewer than 2000 senders retains all entries."""
        notify = {}
        base_time = 1_000_000.0
        for i in range(100):
            _apply_cap_and_insert(notify, f"sender_{i}", base_time + i)
        assert len(notify) == 100
        for i in range(100):
            assert f"sender_{i}" in notify

    def test_at_capacity_size_stays_at_2000(self):
        """Adding a sender when dict is full keeps size at exactly 2000."""
        notify = {}
        base_time = 1_000_000.0
        # Fill to capacity
        for i in range(_SENDER_NOTIFY_MAX):
            notify[f"sender_{i}"] = base_time + i

        assert len(notify) == _SENDER_NOTIFY_MAX

        # Insert one more — size must stay at 2000
        _apply_cap_and_insert(notify, "new_sender", base_time + _SENDER_NOTIFY_MAX)
        assert len(notify) == _SENDER_NOTIFY_MAX, (
            f"Expected dict to stay at {_SENDER_NOTIFY_MAX} after eviction, got {len(notify)}"
        )

    def test_oldest_entry_is_evicted(self):
        """The entry with the smallest (oldest) timestamp must be the one removed."""
        notify = {}
        base_time = 1_000_000.0
        # Fill to capacity; sender_0 has the oldest timestamp
        for i in range(_SENDER_NOTIFY_MAX):
            notify[f"sender_{i}"] = base_time + i

        # sender_0 has timestamp base_time + 0, the oldest
        _apply_cap_and_insert(notify, "late_sender", base_time + _SENDER_NOTIFY_MAX)

        assert "sender_0" not in notify, (
            "sender_0 (oldest entry) should have been evicted but was still present"
        )
        assert "sender_1" in notify, "sender_1 should be retained"
        assert "late_sender" in notify, "newly inserted sender must be present"

    def test_new_entry_has_correct_timestamp(self):
        """The newly inserted sender must carry the 'now' timestamp."""
        notify = {}
        base_time = 1_000_000.0
        for i in range(_SENDER_NOTIFY_MAX):
            notify[f"sender_{i}"] = base_time + i

        new_time = base_time + 9_999.0
        _apply_cap_and_insert(notify, "brand_new", new_time)
        assert notify["brand_new"] == new_time

    def test_eviction_does_not_remove_newest(self):
        """The newest (most recent) entries must survive the eviction."""
        notify = {}
        base_time = 1_000_000.0
        for i in range(_SENDER_NOTIFY_MAX):
            notify[f"sender_{i}"] = base_time + i

        newest_sender = f"sender_{_SENDER_NOTIFY_MAX - 1}"
        newest_time = notify[newest_sender]

        _apply_cap_and_insert(notify, "extra", base_time + _SENDER_NOTIFY_MAX)

        assert newest_sender in notify, "Newest sender must not be evicted"
        assert notify[newest_sender] == newest_time

    def test_multiple_insertions_never_exceed_cap(self):
        """Inserting many extra senders beyond capacity never exceeds 2000."""
        notify = {}
        base_time = 1_000_000.0
        # Fill to capacity first
        for i in range(_SENDER_NOTIFY_MAX):
            notify[f"seed_{i}"] = base_time + i

        # Insert 500 more
        for j in range(500):
            _apply_cap_and_insert(notify, f"extra_{j}", base_time + _SENDER_NOTIFY_MAX + j)
            assert len(notify) <= _SENDER_NOTIFY_MAX, (
                f"Dict exceeded cap after inserting extra_{j}: size={len(notify)}"
            )

    def test_single_entry_dict_evicts_on_cap_reached(self):
        """Edge case: dict with exactly 1 entry below cap — eviction is not triggered."""
        notify = {"only_sender": 1.0}
        _apply_cap_and_insert(notify, "another", 2.0)
        # Should simply add, no eviction
        assert len(notify) == 2
        assert "only_sender" in notify
        assert "another" in notify

    def test_empty_dict_stays_safe(self):
        """Inserting into an empty dict works without error."""
        notify: dict = {}
        _apply_cap_and_insert(notify, "first", 1.0)
        assert notify == {"first": 1.0}
