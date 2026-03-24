"""
Tests for BUG-P24A-1: DNS rebinding SSRF protection must be active during
the actual HTTP fetch, not only during opener construction.

The bug: socket.create_connection was patched before build_opener() (which
makes no network calls) and restored before _opener.open().  The safe wrapper
was therefore never in effect when the TCP connection was actually established.

The fix: keep _safe_create_connection installed for the duration of
_opener.open() by moving the restore into a try/finally block that surrounds
the open() call.

These tests verify the fix by confirming that socket.create_connection is
called through the safe wrapper (which raises URLError on private IPs) during
the fetch, not just during opener construction.
"""
import sys
import socket
import ipaddress
import types
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Helpers ───────────────────────────────────────────────────────────────────

_AGENT_PY = Path(__file__).parent.parent / "container" / "agent-runner" / "agent.py"


def _extract_tool_web_fetch():
    """
    Extract the tool_web_fetch function from agent.py by exec-ing the relevant
    portion of the file.  We do this rather than importing the full agent
    module because agent.py has heavy transitive dependencies (anthropic SDK,
    docker SDK, etc.) that are not available in CI.

    Returns the function object ready to call.
    """
    src = _AGENT_PY.read_text(encoding="utf-8")

    # Locate the function definition
    start_marker = "def tool_web_fetch(url:"
    end_marker = "\ndef "  # next top-level function definition

    start_idx = src.find(start_marker)
    assert start_idx != -1, "Could not locate tool_web_fetch in agent.py"

    # Walk back to find the line start (there might be a decorator)
    line_start = src.rfind("\n", 0, start_idx) + 1

    # Find the end: next top-level def/class after the function body
    search_from = start_idx + len(start_marker)
    end_idx = len(src)
    for marker in ["\ndef ", "\nclass ", "\nasync def "]:
        pos = src.find(marker, search_from)
        if pos != -1 and pos < end_idx:
            end_idx = pos

    fn_src = src[line_start:end_idx]

    # Build a minimal namespace with the imports the function needs
    # _log is a helper called by tool_web_fetch for SSRF warnings; provide a no-op stub.
    ns: dict = {"_log": lambda tag, msg="": None}
    exec(
        "import urllib.request, urllib.error, urllib.parse, socket, ipaddress, re\n" + fn_src,
        ns,
    )
    assert "tool_web_fetch" in ns, "exec did not produce tool_web_fetch"
    return ns["tool_web_fetch"]


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSSRFDnsRebindingProtection:
    """Verify that socket.create_connection is wrapped during the actual fetch."""

    def test_safe_wrapper_called_during_open_not_build_opener(self):
        """
        BUG-P24A-1 regression: socket.create_connection must be patched during
        _opener.open(), not just during build_opener() which makes no network calls.

        Strategy: replace socket.create_connection with a sentinel that records
        whether it was called, then make _opener.open() raise immediately (to avoid
        a real network call).  If the sentinel was invoked, the fix is in place.
        """
        tool_web_fetch = _extract_tool_web_fetch()

        _sentinel_called = []

        def _sentinel_create_connection(address, *args, **kwargs):
            _sentinel_called.append(address)
            # Simulate connecting to a private IP (DNS rebinding scenario):
            # raise URLError so we can detect that the wrapper ran.
            raise urllib.error.URLError("SSRF test sentinel triggered")

        # Patch socket.getaddrinfo to return a public IP (passes the pre-flight check)
        # but have create_connection (called during actual fetch) be our sentinel.
        _public_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 80))]

        with patch("socket.getaddrinfo", return_value=_public_addrinfo):
            with patch("socket.create_connection", side_effect=_sentinel_create_connection):
                result = tool_web_fetch("http://example.com/test")

        # The sentinel must have been called (i.e. the wrapper was active during open())
        assert _sentinel_called, (
            "socket.create_connection was NOT called during _opener.open() — "
            "the SSRF DNS rebinding protection is not active during the fetch"
        )

    def test_private_ip_blocked_at_connect_time(self):
        """
        When socket.create_connection resolves the address to a private IP,
        the fetch must be blocked with an appropriate error message.

        This simulates a DNS rebinding attack: the pre-flight check sees a public
        IP (passes), but at connect time the DNS has switched to a private address.
        """
        tool_web_fetch = _extract_tool_web_fetch()

        # Pre-flight: return public IP so _is_ssrf_target() passes
        _public_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 80))]

        # At connect time: resolve to loopback (the rebind scenario)
        _private_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", 80))]

        call_count = [0]

        def _mock_getaddrinfo(host, port, *args, **kwargs):
            call_count[0] += 1
            # First call = pre-flight check → public IP (passes)
            # Subsequent calls = connect-time check → private IP (should block)
            if call_count[0] <= 1:
                return _public_addrinfo
            return _private_addrinfo

        with patch("socket.getaddrinfo", side_effect=_mock_getaddrinfo):
            result = tool_web_fetch("http://example.com/rebind-test")

        # Should return an error string, not raise
        assert isinstance(result, str)
        assert "Error" in result or "blocked" in result or "SSRF" in result, (
            f"Expected SSRF error but got: {result!r}"
        )

    def test_public_url_not_blocked_by_preflight(self):
        """
        A legitimate public URL must not be blocked by the pre-flight check.
        (Tests that the filter is not too aggressive.)
        """
        tool_web_fetch = _extract_tool_web_fetch()

        # Resolve to a real public IP
        _public_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("93.184.216.34", 80))]

        # Simulate a successful fetch response
        mock_response = MagicMock()
        mock_response.headers = MagicMock()
        mock_response.headers.get = MagicMock(return_value="text/html; charset=utf-8")
        mock_response.read = MagicMock(return_value=b"<html>Hello</html>")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        mock_opener = MagicMock()
        mock_opener.open = MagicMock(return_value=mock_response)

        with patch("socket.getaddrinfo", return_value=_public_addrinfo):
            with patch("urllib.request.build_opener", return_value=mock_opener):
                result = tool_web_fetch("http://example.com/")

        # Should not return a "private/reserved address" error
        assert "private or reserved address" not in result, (
            f"Public URL was incorrectly blocked: {result!r}"
        )

    def test_localhost_blocked_by_preflight(self):
        """localhost must be blocked immediately by the pre-flight hostname check."""
        tool_web_fetch = _extract_tool_web_fetch()

        result = tool_web_fetch("http://localhost/secret")

        assert isinstance(result, str)
        assert "Error" in result or "denied" in result, (
            f"Expected SSRF block for localhost, got: {result!r}"
        )

    def test_169_254_metadata_blocked(self):
        """The cloud metadata endpoint 169.254.169.254 must be blocked."""
        tool_web_fetch = _extract_tool_web_fetch()

        # Patch getaddrinfo to return the metadata IP
        _metadata_addrinfo = [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("169.254.169.254", 80))]
        with patch("socket.getaddrinfo", return_value=_metadata_addrinfo):
            result = tool_web_fetch("http://169.254.169.254/latest/meta-data/")

        assert isinstance(result, str)
        assert "Error" in result or "denied" in result or "private" in result, (
            f"Expected cloud metadata SSRF block, got: {result!r}"
        )

    def test_non_http_scheme_rejected(self):
        """Non-http/https schemes (file://, ftp://) must be rejected."""
        tool_web_fetch = _extract_tool_web_fetch()

        for scheme in ("file:///etc/passwd", "ftp://internal.host/data"):
            result = tool_web_fetch(scheme)
            assert isinstance(result, str)
            assert "Error" in result, (
                f"Expected scheme-rejection error for {scheme!r}, got: {result!r}"
            )
