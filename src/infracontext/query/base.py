"""Base classes and shared helpers for monitoring query plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests


@dataclass
class QueryResult:
    """Result from a monitoring query."""

    success: bool
    source_type: str
    source_name: str
    data: dict | list | None = None
    error: str | None = None


class QueryPlugin(ABC):
    """Base class for monitoring query plugins.

    Subclasses share a lazily-created :class:`requests.Session` via
    :attr:`session` so that multi-request commands (e.g. ``query status``,
    which fans out across several plugins, or ``query prometheus --type
    status``, which issues five queries in a loop) reuse a single underlying
    connection pool instead of opening a fresh TCP+TLS connection per call.
    """

    source_type: str
    _session: Any = None

    @property
    def session(self) -> requests.Session:
        """A per-plugin-instance ``requests.Session`` created on first use.

        Importing ``requests`` lazily keeps the module importable in
        environments that don't need HTTP (e.g. unit tests for pure helpers).
        """
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session  # type: ignore[no-any-return]

    @abstractmethod
    def query(
        self,
        source_config: dict,
        node_selector: str,
        query_type: str = "status",
        **kwargs,
    ) -> QueryResult:
        """Query the monitoring source for a node.

        Args:
            source_config: Source configuration from sources/*.yaml
            node_selector: How to find this node (instance, host_name, selector)
            query_type: Type of query (status, metrics, logs, alerts)
            **kwargs: Additional query parameters

        Returns:
            QueryResult with data or error
        """
        ...


# ── shared config-derived helpers ──────────────────────────────────


def resolve_verify_ssl(source_config: dict) -> bool:
    """Resolve the TLS verification flag for a ``requests`` call.

    ``verify_ssl`` defaults to True (secure by default). ``tls_skip_verify``
    is an explicit override that forces verification off, intended for
    self-signed monitoring endpoints. This collapses the three identical
    inline copies that previously lived in the prometheus, loki, and checkmk
    plugins.

    Note: the monit plugin's direct-HTTP mode intentionally does NOT use this
    -- it derives verification from the URL scheme (``https://``) rather than
    from config, because monit's per-node URL may be either scheme.
    """
    verify_ssl = source_config.get("verify_ssl", True)
    if source_config.get("tls_skip_verify"):
        return False
    return bool(verify_ssl)


def describe_http_error(url: str, response: requests.Response) -> str:
    """Summarize a ``>= 400`` HTTP response into an actionable error string.

    Called *before* parsing the body as JSON so that an HTML 502 from a proxy
    or an empty-bodied 401 no longer masquerades as the misleading "Invalid
    JSON response". Prefers a structured message from a JSON body (Prometheus
    and Loki both return ``{"error": ...}`` on failure); otherwise falls back
    to a trimmed snippet of the raw body so the operator sees what the server
    actually said.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("error") or payload.get("message")
        if message:
            return f"HTTP {response.status_code} from {url}: {message}"
    snippet = (response.text or "").strip()[:200]
    if snippet:
        return f"HTTP {response.status_code} from {url}: {snippet}"
    return f"HTTP {response.status_code} from {url}"


def resolve_bearer_token(source_config: dict) -> str | None:
    """Resolve a bearer token, preferring the keychain over inline config.

    Looks up ``credential_key`` in the system keychain first (the secure
    path), falling back to a plaintext ``bearer_token`` in the config dict
    for backward compatibility. Returns ``None`` when neither is set.

    Used by the prometheus and loki plugins, which previously carried
    byte-for-byte identical private copies of this logic.
    """
    if credential_key := source_config.get("credential_key"):
        from infracontext.credentials.keychain import get_credential

        token = get_credential(credential_key)
        if token:
            return token
    return source_config.get("bearer_token")
