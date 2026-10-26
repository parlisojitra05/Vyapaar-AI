from dataclasses import dataclass, replace
import hashlib
import json
import uuid

STATUSES = (
    "suggested",
    "draft",
    "awaiting_approval",
    "approved",
    "executing",
    "completed",
    "failed",
    "rejected",
)

ALLOWED = {
    "suggested": {"draft", "rejected"},
    "draft": {"awaiting_approval", "rejected"},
    "awaiting_approval": {"approved", "draft", "rejected"},
    "approved": {"executing"},
    "executing": {"completed", "failed"},
    "failed": {"awaiting_approval"},
    "completed": set(),
    "rejected": set(),
}


def payload_hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Action:
    action_id: str
    kind: str
    payload: dict
    status: str = "suggested"
    phash: str = ""
    executed_key: str | None = None


def new_action(kind: str, payload: dict) -> Action:
    return Action(
        action_id=uuid.uuid4().hex,
        kind=kind,
        payload=payload,
        status="suggested",
        phash=payload_hash(payload),
    )


def transition(a: Action, to: str) -> Action:
    if to not in ALLOWED[a.status]:
        raise ValueError(f"illegal transition {a.status} -> {to}")
    return replace(a, status=to)


def update_payload(a: Action, new_payload: dict) -> Action:
    """Editing draft resets status back to awaiting_approval or draft with new hash."""
    new_h = payload_hash(new_payload)
    base = a
    if a.status == "awaiting_approval":
        base = transition(a, "draft")
    return replace(base, payload=new_payload, phash=new_h)


def approve(a: Action, approving_id: str, seen_hash: str) -> Action:
    """Approval only counts for THIS action and THIS payload. Editing the draft changes the hash and resets approval."""
    if approving_id != a.action_id or seen_hash != a.phash or a.status != "awaiting_approval":
        raise PermissionError("approval does not match the pending action")
    return transition(a, "approved")


def start_execution(a: Action, execution_key: str, seen_keys: set | None = None) -> Action:
    """Transitions approved -> executing, refusing repeat execution with same key."""
    if a.status != "approved":
        raise ValueError(f"Cannot execute action in status {a.status}")
    if a.executed_key == execution_key or (seen_keys is not None and execution_key in seen_keys):
        raise ValueError("A second execution with the same executed_key is refused.")
    if seen_keys is not None:
        seen_keys.add(execution_key)
    return replace(transition(a, "executing"), executed_key=execution_key)
