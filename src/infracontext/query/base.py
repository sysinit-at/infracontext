"""Base classes for monitoring query plugins."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class QueryResult:
    """Result from a monitoring query."""

    success: bool
    source_type: str
    source_name: str
    data: dict | list | None = None
    error: str | None = None


class QueryPlugin(ABC):
    """Base class for monitoring query plugins."""

    source_type: str

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
