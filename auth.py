"""Auth: CSRF/session before_request hooks, password login, passkeys (Face ID)."""
import time
from datetime import datetime
from urllib.parse import urlsplit

from flask import jsonify, request, render_template, session, redirect, Response
from werkzeug.security import generate_password_hash, check_password_hash

from db import get_db, get_setting


def block_cross_site_writes():
    """CSRF guard: browsers attach an Origin/Referer header to requests a web
    page triggers. Writes must come from this app's own pages - a request
    started by some other website gets rejected. Requests without either
    header (curl, scripts) are allowed; browsers always send one cross-site."""
    if request.method in ("POST", "PUT", "DELETE"):
        src = request.headers.get("Origin") or request.headers.get("Referer") or ""
        if not src:
            return
        # own address, the address a reverse proxy (tailscale serve) forwarded
        # for, or any tailnet HTTPS name - only Peter's tailnet can reach this
        ok_hosts = {request.host, request.headers.get("X-Forwarded-Host", "")}
        netloc = urlsplit(src).netloc
        if src == "null" or (netloc not in ok_hosts
                             and not netloc.split(":")[0].endswith(".ts.net")):
            return jsonify({"error": "cross-site request blocked"}), 403


SHORT_SESSION_IDLE = 30 * 60   # mobile: sign in again after 30 min idle


def finish_login(short):
    """Set up the session after a successful password or passkey sign-in.
    Mobile devices get a short sliding session; desktops keep the 30-day one."""
    user_row = get_setting("username") or "peter"
    session.permanent = True
    session["user"] = user_row
    if short:
        session["short"] = True
        session["exp"] = time.time() + SHORT_SESSION_IDLE
    else:
        session.pop("short", None)
        session.pop("exp", None)


def require_login():
    if (request.path.startswith("/static/")
            or request.path in ("/login", "/favicon.ico",
                                "/api/passkey/auth/options", "/api/passkey/auth/verify")):
        return
    if not get_db().execute("SELECT 1 FROM settings WHERE key='password_hash'").fetchone():
        return  # no account set up yet -> app stays open (localhost-style)
    if session.get("user"):
        if not session.get("short"):
            return
        if time.time() <= session.get("exp", 0):
            session["exp"] = time.time() + SHORT_SESSION_IDLE  # sliding window
            return
        session.clear()  # idle too long on a mobile device -> re-auth
    if request.path.startswith("/api/"):
        return jsonify({"error": "auth required"}), 401
    return redirect("/login")


_login_failures = {"count": 0, "locked_until": 0.0}  # in-memory brute-force lockout


def login():
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    if not row:
        return redirect("/")
    error = None
    if request.method == "POST":
        if time.time() < _login_failures["locked_until"]:
            error = "Too many wrong attempts — wait a minute and try again."
            return render_template("login.html", error=error)
        user_row = db.execute("SELECT value FROM settings WHERE key='username'").fetchone()
        username = user_row["value"] if user_row else "peter"
        if (request.form.get("username", "").strip().lower() == username
                and check_password_hash(row["value"], request.form.get("password", ""))):
            _login_failures.update(count=0, locked_until=0.0)
            finish_login(short=request.form.get("mobile") == "1")
            return redirect("/")
        time.sleep(0.7)  # slow down password guessing
        _login_failures["count"] += 1
        if _login_failures["count"] >= 5:  # 5 misses -> 60s lockout
            _login_failures.update(count=0, locked_until=time.time() + 60)
        error = "Wrong username or password."
    return render_template("login.html", error=error)


def logout():
    session.clear()
    return redirect("/login")


def api_change_password():
    d = request.get_json(force=True)
    db = get_db()
    row = db.execute("SELECT value FROM settings WHERE key='password_hash'").fetchone()
    if not row or not check_password_hash(row["value"], d.get("current", "")):
        return jsonify({"error": "Current password is incorrect."}), 400
    new = d.get("new", "")
    if len(new) < 6:
        return jsonify({"error": "New password must be at least 6 characters."}), 400
    # pbkdf2: the default (scrypt) is missing from this Mac's Python build
    db.execute("UPDATE settings SET value=? WHERE key='password_hash'",
               (generate_password_hash(new, method="pbkdf2:sha256:600000"),))
    db.commit()
    return jsonify({"ok": True})


# ---------------------------------------------------------------- passkeys (Face ID / Touch ID)

def _webauthn_ctx():
    """Relying-party id + origin from the request (works behind tailscale serve)."""
    host = request.headers.get("Host", request.host)
    proto = request.headers.get("X-Forwarded-Proto", request.scheme)
    return host.split(":")[0], "{}://{}".format(proto, host)


def api_pk_reg_options():
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json
    from webauthn.helpers.structs import (PublicKeyCredentialDescriptor,
        AuthenticatorSelectionCriteria, ResidentKeyRequirement, UserVerificationRequirement)
    db = get_db()
    rp_id, _ = _webauthn_ctx()
    user = get_setting("username") or "peter"
    exclude = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
               for r in db.execute("SELECT credential_id FROM passkeys")]
    opts = webauthn.generate_registration_options(
        rp_id=rp_id, rp_name="Crypto Tracker",
        user_id=user.encode(), user_name=user, user_display_name=user.title(),
        exclude_credentials=exclude,
        authenticator_selection=AuthenticatorSelectionCriteria(
            resident_key=ResidentKeyRequirement.PREFERRED,
            user_verification=UserVerificationRequirement.REQUIRED))
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")


def api_pk_reg_verify():
    import webauthn
    from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
    d = request.get_json(force=True)
    rp_id, origin = _webauthn_ctx()
    challenge = session.pop("pk_challenge", "")
    if not challenge:
        return jsonify({"error": "No registration in progress - try again."}), 400
    try:
        v = webauthn.verify_registration_response(
            credential=d["credential"],
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin)
    except Exception as e:
        return jsonify({"error": "Registration failed: " + str(e)[:150]}), 400
    db = get_db()
    db.execute("INSERT OR REPLACE INTO passkeys (credential_id, public_key, sign_count, device_name, created) "
               "VALUES (?,?,?,?,?)",
               (bytes_to_base64url(v.credential_id), bytes_to_base64url(v.credential_public_key),
                v.sign_count, (d.get("device_name") or "Device")[:40],
                datetime.now().strftime("%Y-%m-%d %H:%M")))
    db.commit()
    return jsonify({"ok": True})


def api_pk_auth_options():
    import webauthn
    from webauthn.helpers import bytes_to_base64url, base64url_to_bytes, options_to_json
    from webauthn.helpers.structs import PublicKeyCredentialDescriptor, UserVerificationRequirement
    db = get_db()
    creds = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(r["credential_id"]))
             for r in db.execute("SELECT credential_id FROM passkeys")]
    if not creds:
        return jsonify({"error": "No passkeys registered yet - sign in with your password, "
                                 "then add this device in Settings."}), 400
    rp_id, _ = _webauthn_ctx()
    opts = webauthn.generate_authentication_options(
        rp_id=rp_id, allow_credentials=creds,
        user_verification=UserVerificationRequirement.REQUIRED)
    session["pk_challenge"] = bytes_to_base64url(opts.challenge)
    return Response(options_to_json(opts), mimetype="application/json")


def api_pk_auth_verify():
    import webauthn
    from webauthn.helpers import base64url_to_bytes
    d = request.get_json(force=True)
    cred = d.get("credential") or {}
    db = get_db()
    row = db.execute("SELECT * FROM passkeys WHERE credential_id=?",
                     (cred.get("id", ""),)).fetchone()
    challenge = session.pop("pk_challenge", "")
    if not row or not challenge:
        time.sleep(0.5)
        return jsonify({"error": "Unknown passkey or no sign-in in progress."}), 400
    rp_id, origin = _webauthn_ctx()
    try:
        v = webauthn.verify_authentication_response(
            credential=cred,
            expected_challenge=base64url_to_bytes(challenge),
            expected_rp_id=rp_id, expected_origin=origin,
            credential_public_key=base64url_to_bytes(row["public_key"]),
            credential_current_sign_count=row["sign_count"])
    except Exception as e:
        time.sleep(0.5)
        return jsonify({"error": "Sign-in failed: " + str(e)[:150]}), 400
    db.execute("UPDATE passkeys SET sign_count=?, last_used=? WHERE id=?",
               (v.new_sign_count, datetime.now().strftime("%Y-%m-%d %H:%M"), row["id"]))
    db.commit()
    finish_login(short=bool(d.get("mobile")))
    return jsonify({"ok": True})


def api_pk_list():
    rows = get_db().execute(
        "SELECT id, device_name, created, last_used FROM passkeys ORDER BY id").fetchall()
    return jsonify([dict(r) for r in rows])


def api_pk_delete(pk_id):
    db = get_db()
    db.execute("DELETE FROM passkeys WHERE id=?", (pk_id,))
    db.commit()
    return jsonify({"ok": True})


def register(app):
    """Attach the auth hooks and routes to the app. The two before_request
    hooks run in this order: CSRF guard first, then the login check."""
    app.before_request(block_cross_site_writes)
    app.before_request(require_login)
    app.add_url_rule("/login", view_func=login, methods=["GET", "POST"])
    app.add_url_rule("/logout", view_func=logout)
    app.add_url_rule("/api/change_password", view_func=api_change_password, methods=["POST"])
    app.add_url_rule("/api/passkey/register/options", view_func=api_pk_reg_options, methods=["POST"])
    app.add_url_rule("/api/passkey/register/verify", view_func=api_pk_reg_verify, methods=["POST"])
    app.add_url_rule("/api/passkey/auth/options", view_func=api_pk_auth_options, methods=["POST"])
    app.add_url_rule("/api/passkey/auth/verify", view_func=api_pk_auth_verify, methods=["POST"])
    app.add_url_rule("/api/passkey/list", view_func=api_pk_list)
    app.add_url_rule("/api/passkey/<int:pk_id>", view_func=api_pk_delete, methods=["DELETE"])
