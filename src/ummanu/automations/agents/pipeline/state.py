"""Canonical state for the pipeline agent."""

from __future__ import annotations

from ummanu.runtime.shared_state import resolve_pipeline_state_dir
from ummanu.runtime.state import AgentState

STATE = AgentState("pipeline", state_dir=resolve_pipeline_state_dir())
