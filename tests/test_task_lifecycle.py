"""Move G′ Box 1 — pure task lifecycle derivation. No I/O, no CLI."""
from src.core.task_lifecycle import (
    derive_task_state,
    section_for_state,
    QUEUED, DISPATCHING, RUNNING, WAITING_FOR_INPUT,
    SUCCEEDED, FAILED, CANCELLED, CONNECTION_UNKNOWN,
    SECTION_ATTENTION, SECTION_RUNNING, SECTION_QUEUED, SECTION_FAILED,
    SECTION_RECENT,
)


class TestMeshOnlyMapping:
    def test_each_mesh_status_maps(self):
        assert derive_task_state("pending") == QUEUED
        assert derive_task_state("claimed") == DISPATCHING
        assert derive_task_state("processing") == RUNNING
        assert derive_task_state("completed") == SUCCEEDED
        assert derive_task_state("failed") == FAILED
        assert derive_task_state("failed_node_offline") == FAILED
        assert derive_task_state("cancelled") == CANCELLED

    def test_unknown_mesh_status_is_connection_unknown(self):
        assert derive_task_state("garbage") == CONNECTION_UNKNOWN


class TestSessionOverlay:
    def test_awaiting_input_overlays_active_task(self):
        # The value-add: a running task whose session awaits input is supervised
        # as waiting_for_input (a bucket the flat mesh status can't reach).
        assert derive_task_state("processing", "awaiting_input") == WAITING_FOR_INPUT
        assert derive_task_state("claimed", "awaiting_input") == WAITING_FOR_INPUT
        assert derive_task_state("pending", "awaiting_input") == WAITING_FOR_INPUT

    def test_error_session_overlays_active_task(self):
        assert derive_task_state("processing", "error") == FAILED

    def test_terminal_state_ignores_session_overlay(self):
        # A completed task stays completed even if its session moved on to await
        # input for the NEXT turn — terminal is terminal.
        assert derive_task_state("completed", "awaiting_input") == SUCCEEDED
        assert derive_task_state("failed", "awaiting_input") == FAILED
        assert derive_task_state("cancelled", "busy") == CANCELLED

    def test_idle_or_busy_session_does_not_change_active_base(self):
        assert derive_task_state("processing", "busy") == RUNNING
        assert derive_task_state("pending", "idle") == QUEUED

    def test_none_session_is_oneoff_base(self):
        assert derive_task_state("processing", None) == RUNNING


class TestSectioning:
    def test_attention_section(self):
        # Attention = genuinely BLOCKED on a human, still actionable. FAILED is
        # terminal (its own section), NOT attention — see test_failed_section.
        assert section_for_state(WAITING_FOR_INPUT) == SECTION_ATTENTION
        assert section_for_state(CONNECTION_UNKNOWN) == SECTION_ATTENTION

    def test_failed_section(self):
        assert section_for_state(FAILED) == SECTION_FAILED

    def test_running_section(self):
        assert section_for_state(RUNNING) == SECTION_RUNNING
        assert section_for_state(DISPATCHING) == SECTION_RUNNING

    def test_queued_section(self):
        assert section_for_state(QUEUED) == SECTION_QUEUED

    def test_recent_section(self):
        assert section_for_state(SUCCEEDED) == SECTION_RECENT
        assert section_for_state(CANCELLED) == SECTION_RECENT
