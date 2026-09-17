"""Built-in connector class registry (docs/system-design.md section 6.1/6.2): which classes
exist, keyed by manifest key. Purely for existence checks (e.g. "is `postgres` a real
connector key" when installing one) — instantiating a connector for a run needs
request-scoped dependencies (a DB session, the object store) that don't belong on a
class-level registry, so that lives in `relay_core.tools.registry.ToolRegistry` instead.
"""

from relay_core.connectors.base import Connector
from relay_core.connectors.builtin.documents import DocumentsConnector
from relay_core.connectors.builtin.file_upload import FileUploadConnector
from relay_core.connectors.builtin.gmail import GmailConnector
from relay_core.connectors.builtin.google_calendar import GoogleCalendarConnector
from relay_core.connectors.builtin.postgres import PostgresConnector
from relay_core.connectors.builtin.python_sandbox import PythonSandboxConnector

# A plain dict literal, not `{c.key: c for c in [...]}`: mypy's strict mode can't narrow the
# comprehension's loop variable back down from the two classes' common `ABCMeta` metaclass to
# `type[Connector]`, so it loses `.key` — spelling it out avoids that instead of suppressing it.
CONNECTOR_TYPES: dict[str, type[Connector]] = {
    PostgresConnector.key: PostgresConnector,
    FileUploadConnector.key: FileUploadConnector,
    DocumentsConnector.key: DocumentsConnector,
    PythonSandboxConnector.key: PythonSandboxConnector,
    GmailConnector.key: GmailConnector,
    GoogleCalendarConnector.key: GoogleCalendarConnector,
}
