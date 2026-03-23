"""
Tests for host.allowlist — sender/mount allowlist security logic.

Critical paths covered:
  - load_sender_allowlist: file-missing deny-all (BUG-AL-1 fix)
  - is_sender_allowed: empty-string bypass prevention (BUG-AL-01 fix)
  - is_sender_allowed: non-string sender_id (BUG-AL-02 fix)
  - load_mount_allowlist: file-missing returns []
  - load_sender_allowlist: corrupt JSON returns deny-all sentinel
  - is_sender_allowed: allow-all when allowlist is empty set
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from host.allowlist import is_sender_allowed, load_sender_allowlist, load_mount_allowlist


# ── is_sender_allowed ─────────────────────────────────────────────────────────

class TestIsSenderAllowed:
    def test_empty_allowlist_allows_all(self):
        """Empty set (allow-all mode) must permit every sender."""
        assert is_sender_allowed("any-sender", set()) is True

    def test_sender_in_allowlist_is_allowed(self):
        """A sender explicitly in the allowlist must be permitted."""
        assert is_sender_allowed("alice", {"alice", "bob"}) is True

    def test_sender_not_in_allowlist_is_denied(self):
        """A sender absent from a non-empty allowlist must be denied."""
        assert is_sender_allowed("mallory", {"alice", "bob"}) is False

    def test_deny_all_sentinel_rejects_real_sender(self):
        """The {"" } deny-all sentinel must reject real sender IDs."""
        assert is_sender_allowed("alice", {""}) is False

    def test_deny_all_sentinel_rejects_empty_string_sender(self):
        """BUG-AL-01 FIX: empty/whitespace sender_id must NOT match sentinel entry.

        Before the fix, sender_id="" would match {""} and be allowed.
        """
        assert is_sender_allowed("", {""}) is False
        assert is_sender_allowed("   ", {""}) is False

    def test_whitespace_only_sender_denied_with_active_allowlist(self):
        """Whitespace-only sender_id is never a valid real sender — always deny."""
        assert is_sender_allowed("   ", {"alice", "bob"}) is False

    def test_none_sender_id_denied(self):
        """BUG-AL-02 FIX: non-string sender_id (None) must not crash and must deny."""
        assert is_sender_allowed(None, {"alice"}) is False  # type: ignore[arg-type]

    def test_integer_sender_id_denied(self):
        """BUG-AL-02 FIX: integer sender_id must be denied safely."""
        assert is_sender_allowed(12345, {"12345"}) is False  # type: ignore[arg-type]

    def test_sender_with_leading_trailing_spaces_normalized(self):
        """Sender IDs with surrounding whitespace should be normalised before lookup."""
        assert is_sender_allowed("  alice  ", {"alice"}) is True


# ── load_sender_allowlist ─────────────────────────────────────────────────────

class TestLoadSenderAllowlist:
    def test_missing_file_returns_deny_all_sentinel(self, tmp_path, monkeypatch):
        """BUG-AL-1 FIX: if allowlist file is missing, return deny-all {""}."""
        monkeypatch.setenv("SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING", "false")
        import host.allowlist as al_mod
        import host.config as cfg_mod
        with patch.object(cfg_mod, "SENDER_ALLOWLIST_FILE", tmp_path / "missing.json"):
            # Reload to pick up the env var
            import importlib
            importlib.reload(al_mod)
            result = al_mod.load_sender_allowlist()
        assert result == {""}, (
            "Missing allowlist file with ALLOW_ALL_IF_MISSING=false should return deny-all sentinel"
        )

    def test_missing_file_with_allow_all_flag_returns_empty_set(self, tmp_path, monkeypatch):
        """ALLOW_ALL_IF_MISSING=true: missing file returns empty set (allow-all)."""
        monkeypatch.setenv("SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING", "true")
        import host.allowlist as al_mod
        import host.config as cfg_mod
        import importlib
        importlib.reload(al_mod)
        with patch.object(cfg_mod, "SENDER_ALLOWLIST_FILE", tmp_path / "missing.json"):
            result = al_mod.load_sender_allowlist()
        assert result == set()

    def test_valid_file_returns_senders(self, tmp_path):
        """A valid allowlist file returns the set of sender IDs."""
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({"senders": ["alice", "bob"]}), encoding="utf-8")

        import host.config as cfg_mod
        with patch.object(cfg_mod, "SENDER_ALLOWLIST_FILE", allowlist_file):
            result = load_sender_allowlist()

        assert result == {"alice", "bob"}

    def test_corrupt_json_returns_deny_all_sentinel(self, tmp_path, monkeypatch):
        """Corrupt JSON in the allowlist file must return deny-all sentinel."""
        monkeypatch.setenv("SENDER_ALLOWLIST_ALLOW_ALL_IF_MISSING", "false")
        import host.allowlist as al_mod
        import importlib
        importlib.reload(al_mod)

        corrupt_file = tmp_path / "corrupt.json"
        corrupt_file.write_text("{not valid json!!!", encoding="utf-8")

        import host.config as cfg_mod
        with patch.object(cfg_mod, "SENDER_ALLOWLIST_FILE", corrupt_file):
            result = al_mod.load_sender_allowlist()

        assert result == {""}, "Corrupt JSON should return deny-all sentinel"

    def test_empty_senders_list_returns_allow_all(self, tmp_path):
        """An allowlist file with an empty senders list means allow-all (operator intent)."""
        allowlist_file = tmp_path / "allowlist.json"
        allowlist_file.write_text(json.dumps({"senders": []}), encoding="utf-8")

        import host.config as cfg_mod
        with patch.object(cfg_mod, "SENDER_ALLOWLIST_FILE", allowlist_file):
            result = load_sender_allowlist()

        assert result == set(), "Empty senders list should return allow-all empty set"


# ── load_mount_allowlist ──────────────────────────────────────────────────────

class TestLoadMountAllowlist:
    def test_missing_file_returns_empty_list(self, tmp_path):
        """Missing mount allowlist file returns an empty list (no mounts allowed)."""
        import host.config as cfg_mod
        with patch.object(cfg_mod, "MOUNT_ALLOWLIST_FILE", tmp_path / "missing.json"):
            result = load_mount_allowlist()
        assert result == []

    def test_valid_file_returns_mounts(self, tmp_path):
        """A valid mount allowlist returns the list of paths."""
        mount_file = tmp_path / "mounts.json"
        mount_file.write_text(json.dumps({"mounts": ["/data/shared", "/opt/tools"]}), encoding="utf-8")
        import host.config as cfg_mod
        with patch.object(cfg_mod, "MOUNT_ALLOWLIST_FILE", mount_file):
            result = load_mount_allowlist()
        assert result == ["/data/shared", "/opt/tools"]

    def test_corrupt_json_returns_empty_list(self, tmp_path):
        """Corrupt mount allowlist JSON returns an empty list (fail-safe)."""
        corrupt_file = tmp_path / "bad.json"
        corrupt_file.write_text("{bad", encoding="utf-8")
        import host.config as cfg_mod
        with patch.object(cfg_mod, "MOUNT_ALLOWLIST_FILE", corrupt_file):
            result = load_mount_allowlist()
        assert result == []
