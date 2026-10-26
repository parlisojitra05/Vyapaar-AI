from dataclasses import dataclass
from datetime import date
from typing import Optional, Set


@dataclass(frozen=True)
class Move:
    key: str
    title: str
    headline: str
    count: int
    total: float
    refs: tuple
    oldest_days: int | None
    why: str
    actions: tuple
    fingerprint: str


def compute_overdue_move(conn, today: Optional[date] = None) -> Optional[Move]:
    today = today or date.today()
    today_iso = today.isoformat()

    # Determine table schema (supports both project schema 'invoice'/'payment' and generic 'invoices')
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN ('invoice', 'invoices')"
    ).fetchall()
    tables = [r[0] for r in table_check]

    if "invoice" in tables:
        query = """
            SELECT i.number, c.name,
                   (i.total_minor - COALESCE((SELECT SUM(amount_minor) FROM payment p WHERE p.invoice_id = i.id), 0)) AS due,
                   i.due_date
            FROM invoice i
            JOIN customer c ON c.id = i.customer_id
            WHERE due > 0 AND due_date < ?
            ORDER BY due_date
        """
        rows = conn.execute(query, (today_iso,)).fetchall()
        # In our schema amounts are in minor units (paise)
        if not rows:
            return None
        total = round(sum(r[2] for r in rows) / 100.0, 2)
        oldest = max((today - date.fromisoformat(r[3][:10])).days for r in rows)
        refs = tuple(r[0] for r in rows[:5])
    elif "invoices" in tables:
        query = """
            SELECT invoice_no, customer_name, (amount - COALESCE(paid,0)) AS due, due_date
            FROM invoices
            WHERE (amount - COALESCE(paid,0)) > 0 AND due_date < ?
            ORDER BY due_date
        """
        rows = conn.execute(query, (today_iso,)).fetchall()
        if not rows:
            return None
        total = round(sum(float(r[2]) for r in rows), 2)
        oldest = max((today - date.fromisoformat(r[3][:10])).days for r in rows)
        refs = tuple(r[0] for r in rows[:5])
    else:
        return None

    n = len(rows)
    fp = f"overdue:{n}:{total}:{refs}"
    return Move(
        key="overdue",
        title="Review overdue payments",
        headline=f"{n} invoice{'s are' if n != 1 else ' is'} overdue according to the available records.",
        count=n,
        total=total,
        refs=refs,
        oldest_days=oldest,
        why="Unpaid invoices past their due date reduce your available cash.",
        actions=("Review records", "Draft reminders", "Ask why this matters"),
        fingerprint=fp,
    )


def compute_stock_move(conn) -> Optional[Move]:
    """Optional second move: surfaces products below minimum stock."""
    table_check = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='product'"
    ).fetchone()
    if not table_check:
        return None

    rows = conn.execute(
        "SELECT sku, name, stock, min_stock FROM product WHERE stock < min_stock ORDER BY (min_stock - stock) DESC"
    ).fetchall()
    if not rows:
        return None

    n = len(rows)
    refs = tuple(r["name"] for r in rows[:5])
    fp = f"stock:{n}:{refs}"
    return Move(
        key="stock",
        title="Restock low inventory",
        headline=f"{n} product{'s are' if n != 1 else ' is'} below minimum threshold.",
        count=n,
        total=0.0,
        refs=refs,
        oldest_days=None,
        why="Low inventory risks missed sales and lost revenue.",
        actions=("View inventory", "Adjust stock", "Ask restocking suggestions"),
        fingerprint=fp,
    )


def dismiss_move(store, fingerprint: str) -> None:
    if isinstance(store, set):
        store.add(fingerprint)
    elif isinstance(store, dict):
        dismissed = store.setdefault("vy_dismissed_moves", [])
        if fingerprint not in dismissed:
            dismissed.append(fingerprint)


def is_move_dismissed(store, fingerprint: str) -> bool:
    if isinstance(store, set):
        return fingerprint in store
    if isinstance(store, dict):
        return fingerprint in store.get("vy_dismissed_moves", [])
    return False
