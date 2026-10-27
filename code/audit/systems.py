"""Canonical system ownership of resources.

Activity rows name whichever system performed an operation (a support message is
"indexed" by search_service), but a resource always belongs to one system, and
that owner is the system_id the output uses. Ownership follows the resource-id
prefix.
"""

import re

RESOURCE_SYSTEMS = {
    "account": "account_service",
    "analytics": "analytics_platform",
    "audit_record": "security_service",
    "backup": "backup_service",
    "cache": "cache_service",
    "data_flow": "integration_service",
    "email_delivery": "email_delivery_service",
    "export": "export_service",
    "legal_hold": "legal_service",
    "marketing": "marketing_platform",
    "object": "object_storage",
    "search": "search_service",
    "support": "support_platform",
    "tombstone": "identity_service",
}

RESOURCE_ID_RE = re.compile(r"^(?P<prefix>[a-z_]+?)_\d+$")


def system_for_resource(resource_id: str | None) -> str | None:
    if not resource_id:
        return None
    match = RESOURCE_ID_RE.match(resource_id)
    return RESOURCE_SYSTEMS.get(match.group("prefix")) if match else None
