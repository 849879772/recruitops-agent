"""Persistent Edge active browser-bridge domain primitives."""

from .models import (
    BrowserOperationName,
    BrowserOperationStatus,
    BrowserConnectionStatus,
    OperationName,
    OperationState,
    OperationStatus,
    TERMINAL_STATUSES,
    WebPageData,
    WebPageEvidence,
    WebPageNetworkRequest,
    WebPageStatus,
    bounded_json,
    normalize_operation,
    normalize_status,
    validate_bridge_payload,
    validate_web_page_data,
)
from .store import BrowserBridgeOperationStore, BrowserBridgeStore, BrowserOperationStore
from .server import (
    AUTH_DOMAIN,
    BROWSER_BRIDGE_PATH,
    BRIDGE_PROTOCOL_VERSION,
    BrowserBridgeConnection,
    BrowserBridgeServer,
    build_auth_message,
    compute_auth_signature,
    create_auth_signature,
    create_browser_bridge_router,
    install_browser_bridge,
    normalize_device_id,
    verify_auth_signature,
)

from packages.storage.models import (
    BrowserBridgeDevice,
    BrowserOperation,
    BrowserOperationEvent,
    BrowserOutbox,
    BrowserOutboxCursor,
)

Operation = BrowserOperation
OperationEvent = BrowserOperationEvent
OutboxMessage = BrowserOutbox

__all__ = [
    "BrowserBridgeOperationStore",
    "BrowserBridgeConnection",
    "BrowserBridgeDevice",
    "BrowserBridgeStore",
    "BrowserBridgeServer",
    "BrowserOperation",
    "BrowserConnectionStatus",
    "BrowserOperationEvent",
    "BrowserOperationName",
    "BrowserOperationStore",
    "BrowserOperationStatus",
    "BrowserOutbox",
    "BrowserOutboxCursor",
    "AUTH_DOMAIN",
    "BROWSER_BRIDGE_PATH",
    "BRIDGE_PROTOCOL_VERSION",
    "OperationName",
    "Operation",
    "OperationEvent",
    "OperationState",
    "OperationStatus",
    "OutboxMessage",
    "TERMINAL_STATUSES",
    "WebPageData",
    "WebPageEvidence",
    "WebPageNetworkRequest",
    "WebPageStatus",
    "bounded_json",
    "build_auth_message",
    "compute_auth_signature",
    "create_auth_signature",
    "create_browser_bridge_router",
    "install_browser_bridge",
    "normalize_device_id",
    "normalize_operation",
    "normalize_status",
    "validate_bridge_payload",
    "validate_web_page_data",
    "verify_auth_signature",
]
