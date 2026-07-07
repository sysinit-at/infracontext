"""Tests for shared query-base helpers: resolve_verify_ssl, resolve_bearer_token."""

from unittest.mock import patch

from infracontext.query.base import (
    QueryPlugin,
    QueryResult,
    resolve_bearer_token,
    resolve_verify_ssl,
)

# ── resolve_verify_ssl ────────────────────────────────────────────


class TestResolveVerifySsl:
    def test_defaults_to_true(self):
        assert resolve_verify_ssl({}) is True

    def test_explicit_true(self):
        assert resolve_verify_ssl({"verify_ssl": True}) is True

    def test_explicit_false(self):
        assert resolve_verify_ssl({"verify_ssl": False}) is False

    def test_tls_skip_verify_forces_false(self):
        """tls_skip_verify is an explicit override, even if verify_ssl=True."""
        assert resolve_verify_ssl({"verify_ssl": True, "tls_skip_verify": True}) is False

    def test_tls_skip_verify_without_verify_ssl_key(self):
        assert resolve_verify_ssl({"tls_skip_verify": True}) is False

    def test_returns_plain_bool(self):
        """A truthy non-bool (e.g. 1) must collapse to bool, since requests
        accepts only bool/CA-bundle-path for verify."""
        assert resolve_verify_ssl({"verify_ssl": 1}) is True
        assert type(resolve_verify_ssl({"verify_ssl": 1})) is bool


# ── resolve_bearer_token ──────────────────────────────────────────


class TestResolveBearerToken:
    def test_returns_none_when_neither_set(self):
        assert resolve_bearer_token({}) is None

    def test_falls_back_to_plaintext_bearer_token(self):
        assert resolve_bearer_token({"bearer_token": "tok-123"}) == "tok-123"

    def test_prefers_keychain_over_plaintext(self):
        """credential_key (keychain) wins over bearer_token (config)."""
        with patch("infracontext.credentials.keychain.get_credential", return_value="secret-from-keychain"):
            result = resolve_bearer_token(
                {"credential_key": "prom-prod", "bearer_token": "plaintext-fallback"}
            )
        assert result == "secret-from-keychain"

    def test_falls_back_to_plaintext_when_keychain_empty(self):
        with patch("infracontext.credentials.keychain.get_credential", return_value=None):
            result = resolve_bearer_token(
                {"credential_key": "prom-prod", "bearer_token": "plaintext-fallback"}
            )
        assert result == "plaintext-fallback"

    def test_keychain_lookup_uses_credential_key_value(self):
        """The configured account name is what gets looked up."""
        with patch("infracontext.credentials.keychain.get_credential", return_value=None) as m:
            resolve_bearer_token({"credential_key": "loki-prod"})
        m.assert_called_once_with("loki-prod")


# ── QueryPlugin.session reuse ─────────────────────────────────────


class TestSessionReuse:
    def test_session_is_cached_on_instance(self):
        """A plugin reuses one Session across calls (connection pooling)."""

        class _Probe(QueryPlugin):
            source_type = "probe"

            def query(self, source_config, node_selector, query_type="status", **kwargs):
                return QueryResult(True, "probe", "probe")

        p = _Probe()
        s1 = p.session
        s2 = p.session
        assert s1 is s2

    def test_session_is_lazy(self):
        """Accessing .session creates the requests.Session on first use."""

        class _Probe(QueryPlugin):
            source_type = "probe"

            def query(self, source_config, node_selector, query_type="status", **kwargs):
                return QueryResult(True, "probe", "probe")

        p = _Probe()
        assert p._session is None
        s = p.session
        assert p._session is s
