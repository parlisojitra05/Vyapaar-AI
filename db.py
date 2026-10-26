import os
import sqlite3
from datetime import date, datetime, timedelta

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS customer(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, phone TEXT);
CREATE TABLE IF NOT EXISTS product(
  id INTEGER PRIMARY KEY, sku TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
  price_minor INTEGER NOT NULL, cost_minor INTEGER,
  stock INTEGER NOT NULL DEFAULT 0 CHECK(stock >= 0), min_stock INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS invoice(
  id INTEGER PRIMARY KEY, number TEXT UNIQUE NOT NULL,
  customer_id INTEGER NOT NULL REFERENCES customer(id),
  total_minor INTEGER NOT NULL, issue_date TEXT NOT NULL, due_date TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS payment(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoice(id),
  amount_minor INTEGER NOT NULL CHECK(amount_minor > 0), paid_on TEXT NOT NULL,
  method TEXT, idem_key TEXT UNIQUE);
CREATE TABLE IF NOT EXISTS sale(
  id INTEGER PRIMARY KEY, idem_key TEXT UNIQUE NOT NULL, customer_id INTEGER,
  product_id INTEGER NOT NULL, qty INTEGER NOT NULL, total_minor INTEGER NOT NULL,
  invoice_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS inventory_movement(
  id INTEGER PRIMARY KEY, product_id INTEGER NOT NULL, delta INTEGER NOT NULL,
  reason TEXT NOT NULL, ref TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reminder(
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL, message TEXT NOT NULL,
  status TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS pending_action(
  id INTEGER PRIMARY KEY, type TEXT NOT NULL, payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL, reason TEXT, created_by TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN
    ('proposed','approved','rejected','executing','executed','failed','stale')),
  created_at TEXT NOT NULL, decided_at TEXT, executed_at TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS audit_event(
  id INTEGER PRIMARY KEY, action TEXT NOT NULL, entity TEXT, detail TEXT, ts TEXT NOT NULL);
"""


def now_iso():
    # Local time (assumed IST on your machine); documented simplification.
    return datetime.now().isoformat(timespec="seconds")


def connect():
    conn = sqlite3.connect(config.DB_PATH if False else DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


DB_PATH = config.DB_PATH


def audit(conn, action, entity="", detail=""):
    conn.execute(
        "INSERT INTO audit_event(action,entity,detail,ts) VALUES(?,?,?,?)",
        (action, entity, detail, now_iso()),
    )


def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = connect()
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 0:
        seed(conn)
    conn.close()


def seed(conn):
    today = date.today()
    d = lambda n: (today + timedelta(days=n)).isoformat()
    with conn:
        conn.executemany("INSERT INTO customer(name,phone) VALUES(?,?)", [
            ("Ramesh Traders", "9800000001"), ("Anita Kirana", "9800000002"),
            ("Mehta Hardware", "9800000003"), ("Sunita Stores", "9800000004")])
        conn.executemany(
            "INSERT INTO product(sku,name,price_minor,cost_minor,stock,min_stock) VALUES(?,?,?,?,?,?)", [
            ("RICE5", "Basmati Rice 5kg", 45000, 38000, 40, 10),
            ("OIL1", "Sunflower Oil 1L", 16000, 14000, 3, 10),   # low stock
            ("SUG1", "Sugar 1kg", 4800, 4200, 60, 20),
            ("TEA250", "Tea 250g", 12000, None, 25, 8),          # cost missing
            ("SOAP", "Bath Soap", 3500, 2600, 80, 20),
            ("DAL1", "Toor Dal 1kg", 15500, 13500, 30, 10)])
        conn.executemany(
            "INSERT INTO invoice(number,customer_id,total_minor,issue_date,due_date) VALUES(?,?,?,?,?)", [
            ("INV-0001", 1, 1200000, d(-50), d(-35)),   # 35 days overdue
            ("INV-0002", 2, 850000, d(-30), d(-10)),    # 10 days overdue, part paid
            ("INV-0003", 3, 500000, d(-3), d(7)),       # not yet due
            ("INV-0004", 4, 200000, d(-20), d(-5))])    # fully paid
        conn.executemany(
            "INSERT INTO payment(invoice_id,amount_minor,paid_on,method,idem_key) VALUES(?,?,?,?,?)", [
            (2, 300000, d(-5), "upi", "seed-1"), (4, 200000, d(-4), "cash", "seed-2")])
        audit(conn, "seed", "demo", "Demo data created")