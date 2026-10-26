import hmac
import json
import logging
import os
import secrets

from flask import Flask, abort, flash, g, redirect, render_template, request, session, jsonify
from werkzeug.exceptions import HTTPException

import actions
import agent
import db
import services
import tools
from config import BRAND_NAME, TAGLINE
from domain import inr, invoice_rows, summary, to_minor
from vyapaar_agent_ui import (
    render_pulse,
    get_status,
    build_intro_html,
    compute_overdue_move,
    compute_stock_move,
    dismiss_move,
    is_move_dismissed,
    record_trail_step,
)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(16)
app.jinja_env.filters["inr"] = inr
db.init_db()


def conn():
    if "conn" not in g:
        g.conn = db.connect()
    return g.conn


@app.teardown_appcontext
def _close(_):
    c = g.pop("conn", None)
    if c:
        c.close()


@app.before_request
def csrf_protect():
    session.setdefault("csrf", secrets.token_hex(16))
    if request.method == "POST":
        # Allow internal API endpoints with same-origin header if csrf is not sent in json
        if request.path.startswith("/api/"):
            return
        sent = request.form.get("csrf", "")
        if not hmac.compare_digest(sent.encode(), session["csrf"].encode()):
            abort(400)


@app.context_processor
def inject():
    c = conn()
    m_status = get_status(c)
    return dict(
        BRAND=BRAND_NAME,
        TAGLINE=TAGLINE,
        csrf=session.get("csrf", ""),
        pending_count=tools.pending_count(c),
        model_status=m_status,
        render_pulse=render_pulse,
        current_path=request.path,
    )


@app.errorhandler(Exception)
def on_error(e):
    if isinstance(e, HTTPException):
        return e
    rid = secrets.token_hex(4)
    logging.exception("request %s failed", rid)
    return f"Something went wrong. Reference: {rid}", 500


@app.get("/intro_frame")
def intro_frame():
    st = get_status(conn())
    return build_intro_html(st, native_skip=False)


@app.post("/api/intro_done")
def api_intro_done():
    session["vy_intro_done"] = True
    session["vy_replay"] = False
    return jsonify({"ok": True})


@app.get("/replay")
def replay():
    session["vy_intro_done"] = False
    session["vy_replay"] = True
    return redirect("/?replay=1")


@app.post("/api/dismiss_move")
def api_dismiss_move():
    fp = request.form.get("fingerprint") or (request.get_json(silent=True) or {}).get("fingerprint")
    if fp:
        dismiss_move(session, fp)
        session.modified = True
    return jsonify({"ok": True})


@app.get("/")
def overview():
    c = conn()
    rows = invoice_rows(c)
    move = compute_overdue_move(c)
    if move and is_move_dismissed(session, move.fingerprint):
        move = None
    if not move:
        stock_move = compute_stock_move(c)
        if stock_move and not is_move_dismissed(session, stock_move.fingerprint):
            move = stock_move

    trail = session.get("vy_trail", [])
    return render_template(
        "overview.html",
        s=summary(rows),
        items=tools.briefing(c),
        low=len(tools.low_stock(c)),
        move=move,
        trail=trail,
    )


@app.get("/money")
def money():
    rows = invoice_rows(conn())
    open_rows = sorted([r for r in rows if r["outstanding_minor"] > 0], key=lambda r: -r["score"])
    return render_template("money.html", rows=open_rows, s=summary(rows), key=secrets.token_hex(8))


@app.post("/money/remind/<int:invoice_id>")
def remind(invoice_id):
    row = next((r for r in invoice_rows(conn()) if r["id"] == invoice_id), None)
    if not row:
        abort(404)
    aid, note = actions.draft_reminder(conn(), row["number"], created_by="user")
    flash(note + (" Review it below." if aid else ""), "ok" if aid else "err")
    return redirect("/approvals" if aid else "/money")


@app.post("/money/pay/<int:invoice_id>")
def pay(invoice_id):
    try:
        replay = services.record_payment(
            conn(),
            invoice_id,
            to_minor(request.form.get("amount", "")),
            request.form.get("key", ""),
            request.form.get("method", "cash"),
        )
        row = next(r for r in invoice_rows(conn()) if r["id"] == invoice_id)
        flash(
            f"{'Already recorded. ' if replay else 'Payment recorded (manually). '}"
            f"{row['number']} balance is now {inr(row['outstanding_minor'])}.",
            "ok",
        )
    except ValueError as e:
        flash(str(e), "err")
    return redirect("/money")


@app.route("/sale", methods=["GET", "POST"])
def sale():
    if request.method == "POST":
        try:
            cid = request.form.get("customer_id") or None
            s, replay = services.record_sale(
                conn(),
                request.form.get("key", ""),
                int(cid) if cid else None,
                int(request.form.get("product_id", 0)),
                int(request.form.get("qty", 0)),
                to_minor(request.form.get("paid", "0") or "0"),
            )
            flash(
                f"{'Already saved (duplicate click ignored). ' if replay else 'Sale saved. '}"
                f"Total {inr(s['total_minor'])}.",
                "ok",
            )
            return redirect("/sale")
        except ValueError as e:
            flash(str(e), "err")
    prods = [dict(r) for r in conn().execute("SELECT * FROM product ORDER BY name")]
    custs = [dict(r) for r in conn().execute("SELECT * FROM customer ORDER BY name")]
    return render_template("sale.html", prods=prods, custs=custs, key=secrets.token_hex(8))


@app.get("/inventory")
def inventory():
    prods = [dict(r) for r in conn().execute("SELECT * FROM product ORDER BY name")]
    return render_template("inventory.html", prods=prods)


@app.route("/assistant", methods=["GET", "POST"])
def assistant():
    c = conn()
    history = session.setdefault("vy_history", [])
    trail = session.setdefault("vy_trail", [])
    res, q = None, ""
    greeted = session.get("vy_greeted", False)
    m_status = get_status(c)

    if request.method == "POST":
        q = request.form.get("q", "")[:500].strip()
        if q:
            req_id = f"req-{secrets.token_hex(4)}"
            record_trail_step(trail, req_id, requested=q, status="Processing")
            session.modified = True

            res = agent.answer(c, q)

            if res.get("trail_info"):
                info = res["trail_info"]
                record_trail_step(
                    trail,
                    req_id,
                    records_checked=info.get("records_checked"),
                    finding=info.get("finding"),
                    suggested_action=info.get("suggested_action"),
                    status=info.get("status", "Completed"),
                )
                session.modified = True

            history.append({"role": "user", "text": q})
            history.append({
                "role": "assistant",
                "text": res["text"],
                "based_on": res.get("based_on", []),
                "proposal": res.get("proposal"),
                "pulse_state": res.get("pulse_state", "idle"),
                "pulse_label": res.get("pulse_label", "Ask Vyapaar"),
                "pulse_channel": res.get("pulse_channel", ""),
            })
            session.modified = True
            session["vy_greeted"] = True

    return render_template(
        "assistant.html",
        res=res,
        q=q,
        history=history,
        trail=trail,
        first_visit=not greeted and len(history) == 0,
        model_status=m_status,
    )


@app.get("/approvals")
def approvals():
    acts = []
    for r in conn().execute("SELECT * FROM pending_action ORDER BY id DESC"):
        a = dict(r)
        a["p"] = json.loads(a["payload"])
        acts.append(a)
    return render_template("approvals.html", acts=acts)


@app.post("/approvals/<int:aid>/decide")
def decide(aid):
    ok, msg = actions.decide(
        conn(), aid, request.form.get("choice") == "approve", request.form.get("message")
    )
    flash(msg, "ok" if ok else "err")
    return redirect("/approvals")


@app.get("/audit")
def audit_log():
    rows = conn().execute("SELECT * FROM audit_event ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("audit.html", rows=rows)


@app.post("/reset")
def reset():
    db.init_db(reset=True)
    session.pop("vy_history", None)
    session.pop("vy_trail", None)
    session.pop("vy_greeted", None)
    session.pop("vy_dismissed_moves", None)
    flash("Demo data reset.", "ok")
    return redirect("/")


if __name__ == "__main__":
    app.run(debug=True)