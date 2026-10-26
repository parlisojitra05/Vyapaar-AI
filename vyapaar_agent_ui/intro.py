import json
import pathlib
import time

INTRO_SECONDS = 6.4  # 5.8 s animation + fade + margin
_HTML_PATH = pathlib.Path(__file__).with_name("intro.html")


def get_intro_raw_html() -> str:
    return _HTML_PATH.read_text(encoding="utf-8")


def build_intro_html(status: str, native_skip: bool = False) -> str:
    status = status if status in ("ready", "connecting", "unavailable") else "connecting"
    html_content = get_intro_raw_html()
    # Safely serialize status and prevent script breakout (escape </ as <\/)
    encoded_status = json.dumps(status).replace("</", r"<\/").replace("<!--", r"<\!--")
    skip_flag = "true" if native_skip else "false"
    inject = f"<script>window.VYAPAAR_STATUS={encoded_status};window.VYAPAAR_NATIVE_SKIP={skip_flag};</script>"
    return html_content.replace("</head>", inject + "</head>", 1)


def decide_intro(first_launch, replay, done_this_session):
    if replay:
        return True
    if done_this_session:
        return False
    return bool(first_launch)


def intro_finished(started_at, skipped, now=None):
    now = time.time() if now is None else now
    return bool(skipped) or round(now - started_at, 4) >= INTRO_SECONDS
