import os

BRAND_NAME = "Vyapaar"
TAGLINE = "Know what your business needs. Recover money faster. Keep stock under control."

# On Vercel, the filesystem is read-only except /tmp
_on_vercel = bool(os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"))
DB_PATH = "/tmp/vyapaar.db" if _on_vercel else os.environ.get("DB_PATH", "vyapaar.db")

REMINDER_COOLDOWN_DAYS = 3