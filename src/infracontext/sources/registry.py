"""Registry for source plugins."""

from infracontext.sources.base import SourcePlugin

# Registry of available source plugins
_plugins: dict[str, type[SourcePlugin]] = {}


def register_plugin(plugin_cls: type[SourcePlugin]) -> type[SourcePlugin]:
    """Decorator to register a source plugin."""
    _plugins[plugin_cls.source_type] = plugin_cls
    return plugin_cls


def get_plugin(source_type: str) -> type[SourcePlugin] | None:
    """Get a plugin class by source type."""
    return _plugins.get(source_type)


def list_plugins() -> list[str]:
    """List all registered plugin types."""
    return list(_plugins.keys())


def get_plugin_instance(source_type: str) -> SourcePlugin | None:
    """Get an instance of a plugin by source type."""
    plugin_cls = get_plugin(source_type)
    if plugin_cls:
        return plugin_cls()
    return None
