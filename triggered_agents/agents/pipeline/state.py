"""Canonical state for the pipeline agent."""
from __future__ import annotations

from ...runtime.shared_state import resolve_pipeline_state_dir
from ...runtime.state import AgentState

STATE = AgentState("pipeline", state_dir=resolve_pipeline_state_dir())
PIPELINE_STATE_DIR = STATE.dir
