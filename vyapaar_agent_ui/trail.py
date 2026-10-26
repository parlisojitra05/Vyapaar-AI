from dataclasses import dataclass, asdict
from datetime import datetime
from typing import List, Dict, Any, Optional


@dataclass
class TrailEntry:
    entry_id: str
    requested: str
    records_checked: str = ""
    finding: str = ""
    suggested_action: str = ""
    status: str = "Received"
    updated_at: str = ""


def record_trail_step(
    trail: List[Dict[str, Any]],
    entry_id: str,
    requested: Optional[str] = None,
    records_checked: Optional[str] = None,
    finding: Optional[str] = None,
    suggested_action: Optional[str] = None,
    status: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Updates an existing entry in place if entry_id exists, otherwise appends.
    Prevents duplicate entries for the same action_id/request."""
    now_str = datetime.now().strftime("%H:%M:%S")

    for item in trail:
        if item.get("entry_id") == entry_id:
            if requested is not None:
                item["requested"] = requested
            if records_checked is not None:
                item["records_checked"] = records_checked
            if finding is not None:
                item["finding"] = finding
            if suggested_action is not None:
                item["suggested_action"] = suggested_action
            if status is not None:
                item["status"] = status
            item["updated_at"] = now_str
            return trail

    # Not found: create new entry
    new_entry = TrailEntry(
        entry_id=entry_id,
        requested=requested or "Business analysis",
        records_checked=records_checked or "",
        finding=finding or "",
        suggested_action=suggested_action or "",
        status=status or "Received",
        updated_at=now_str,
    )
    trail.append(asdict(new_entry))
    return trail
