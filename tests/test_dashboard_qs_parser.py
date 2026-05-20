"""
Regression test for dashboard.py query-string parser.

Bug: the inline qs parser in `_Handler.do_GET` (host/dashboard.py) split
`self.path` on '&'/'=' but never URL-decoded keys/values.  Frontend
`URLSearchParams({jid: "tg:8259652816"})` produces `jid=tg%3A8259652816`,
which the backend then handed to `db.get_hot_memory("tg%3A8259652816")` —
no DB row matched and the 記憶查看器 UI rendered as empty.

The fix exposes a module-level helper `_parse_query(path: str) -> dict[str, str]`
that decodes both keys and values using `urllib.parse.unquote_plus` (same as
URLSearchParams round-trip).
"""
from host.dashboard import _parse_query


class TestParseQuery:
    """Coverage for the encoded characters that broke /api/memory."""

    def test_percent_encoded_colon_in_jid_is_decoded(self):
        # Telegram jids contain ':', URLSearchParams encodes them as '%3A'
        qs = _parse_query("/api/memory?jid=tg%3A8259652816&days=7")
        assert qs["jid"] == "tg:8259652816"
        assert qs["days"] == "7"

    def test_multi_colon_discord_jid_decoded(self):
        # Discord jid has two ':' → '%3A' twice.
        qs = _parse_query(
            "/api/memory?jid=dc%3A1483349924245409812%3A1483359220484149250&days=14"
        )
        assert qs["jid"] == "dc:1483349924245409812:1483359220484149250"
        assert qs["days"] == "14"

    def test_plus_decoded_as_space(self):
        # URLSearchParams encodes ' ' as '+'.  unquote_plus restores it.
        qs = _parse_query("/api/memory?jid=tg%3A123&search=hello+world")
        assert qs["search"] == "hello world"

    def test_percent20_decoded_as_space(self):
        # Manual encoding may also use '%20' for space.
        qs = _parse_query("/api/memory?search=hello%20world")
        assert qs["search"] == "hello world"

    def test_missing_query_returns_empty_dict(self):
        assert _parse_query("/api/memory") == {}
        assert _parse_query("/") == {}

    def test_unencoded_values_pass_through(self):
        # Old call sites already supplied unencoded values; behaviour must
        # not regress for them.
        qs = _parse_query("/api/logs?since=0&level=INFO&limit=100")
        assert qs == {"since": "0", "level": "INFO", "limit": "100"}

    def test_empty_value_kept(self):
        # `?jid=&days=7` is a legitimate request (caller forgot to populate
        # jid).  Backend then returns 400 — but only if the parser actually
        # surfaces the empty string instead of dropping the pair.
        qs = _parse_query("/api/memory?jid=&days=7")
        assert qs["jid"] == ""
        assert qs["days"] == "7"

    def test_repeated_key_takes_first(self):
        # parse_qs returns a list; helper flattens to the first value to
        # preserve the previous str-typed API.
        qs = _parse_query("/api/memory?jid=tg%3A1&jid=tg%3A2")
        assert qs["jid"] == "tg:1"

    def test_key_with_percent_encoded_punctuation(self):
        # Defensive: also decode the key itself.  Unlikely in practice but
        # keeps the helper symmetric with values.
        qs = _parse_query("/api/memory?j%69d=tg%3A1")
        assert qs["jid"] == "tg:1"
