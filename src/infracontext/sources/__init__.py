"""Infrastructure source plugins."""

# Import plugins to trigger registration via @register_plugin decorator
from infracontext.sources import proxmox as _proxmox  # noqa: F401
from infracontext.sources import ssh_config as _ssh_config  # noqa: F401
