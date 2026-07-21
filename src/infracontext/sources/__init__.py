"""Infrastructure source plugins."""

# Import plugins to trigger registration via @register_plugin decorator
from infracontext.sources import checkmk as _checkmk  # noqa: F401
from infracontext.sources import netbox as _netbox  # noqa: F401
from infracontext.sources import proxmox as _proxmox  # noqa: F401
from infracontext.sources import redfish as _redfish  # noqa: F401
from infracontext.sources import snmp as _snmp  # noqa: F401
from infracontext.sources import ssh_config as _ssh_config  # noqa: F401
