"""[A88] Process-wide controller-state authority seam.

A process that is NOT the controller (the native/remote worker) installs a
client here at startup. While one is installed:

* ``src.control.db.get_db()`` returns ``None`` — the process never opens,
  creates or migrates a ``mesh.db`` of its own (controller state lives only in
  the controller's database; see ``docs/backend/DATABASE_AUTHORITY.md``);
* runtime-flag registry rows are resolved from the client (the controller's
  registry over authenticated HTTP) instead of a local file;
* controller-ledger operations (Manager boot reconcile) go through the client.

The controller processes (gateway, task-server, local-execution gateway) never
install a client, so their behaviour is unchanged.
"""
from __future__ import annotations

from typing import Protocol

from pydantic import JsonValue


class ControllerStateClient(Protocol):
    def runtime_flag_row(self, flag_name: str) -> dict[str, str] | None:
        """The controller's registry row for ``flag_name`` or None (no row / unknown)."""
        ...

    def boot_reconcile_case(self, case_id: str) -> dict[str, JsonValue]:
        """Run the controller-side Manager boot reconcile for ``case_id``."""
        ...


_active: ControllerStateClient | None = None


def install(client: ControllerStateClient) -> None:
    global _active
    _active = client


def uninstall() -> None:
    global _active
    _active = None


def active() -> ControllerStateClient | None:
    return _active
