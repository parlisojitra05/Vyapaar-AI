"""Vyapaar Agent UI package.
Provides the Pulse indicator, Intro scene animation, Action approvals state machine,
Action Trail, and Next Best Move deterministic calculations.
"""
from .pulse import (
    STATES,
    TOOL_CHANNEL,
    reduce_state,
    render_pulse,
    pulse_activity,
    get_status,
    get_pulse_css,
)
from .intro import (
    build_intro_html,
    decide_intro,
    intro_finished,
    get_intro_raw_html,
)
from .actions import (
    Action,
    new_action,
    transition,
    approve,
    payload_hash,
    start_execution,
    STATUSES,
    ALLOWED,
)
from .next_move import (
    Move,
    compute_overdue_move,
    compute_stock_move,
    dismiss_move,
    is_move_dismissed,
)
from .trail import (
    TrailEntry,
    record_trail_step,
)

__all__ = [
    "STATES",
    "TOOL_CHANNEL",
    "reduce_state",
    "render_pulse",
    "pulse_activity",
    "get_status",
    "get_pulse_css",
    "build_intro_html",
    "decide_intro",
    "intro_finished",
    "get_intro_raw_html",
    "Action",
    "new_action",
    "transition",
    "approve",
    "payload_hash",
    "start_execution",
    "STATUSES",
    "ALLOWED",
    "Move",
    "compute_overdue_move",
    "compute_stock_move",
    "dismiss_move",
    "is_move_dismissed",
    "TrailEntry",
    "record_trail_step",
]
