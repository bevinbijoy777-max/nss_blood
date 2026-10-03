import os
import sqlite3
from functools import wraps

from flask import (Flask, render_template, request, session, redirect,
                   url_for, flash, jsonify)
from werkzeug.security import generate_password_hash, check_password_hash
import resend

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-change-me")

BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "AB+", "AB-", "O+", "O-"]
DB_FILE = "nss_blood.db"

# ---------- database ----------
def db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT UNIQUE NOT NULL,
            phone TEXT,
            blood_group TEXT,
            password_hash TEXT NOT NULL,
            is_coordinator INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS requests(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            coordinator_id INTEGER NOT NULL,
            blood_group TEXT NOT NULL,
            units INTEGER NOT NULL,
            hospital TEXT NOT NULL,
            urgency TEXT NOT NULL,
            status TEXT DEFAULT 'open',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notifications(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            request_id INTEGER NOT NULL,
            is_read INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );
        """)

# ---------- email (Resend) ----------
resend.api_key = os.environ.get("RESEND_API_KEY", "")

def send_email(to, subject, html):
    if not resend.api_key:
        return
    try:
        resend.Emails.send({
            "from": "NSS Blood Alert <onboarding@resend.dev>",
            "to": to,
            "subject": subject,
            "html": html,
        })
    except Exception as e:
        app.logger.error("Email failed: %s", e)  # email failure never breaks a request

# ---------- helpers ----------
def current_user():
    if "user_id" not in session:
        return None
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE id=?",
                            (session["user_id"],)).fetchone()

def login_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not current_user():
            return redirect(url_for("login"))
        return f(*a, **kw)
    return wrapper

@app.context_processor
def inject_globals():
    return {"user": current_user(), "blood_groups": BLOOD_GROUPS}

# ---------- auth ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        with db() as conn:
            try:
                conn.execute(
                    "INSERT INTO users(name,email,phone,blood_group,password_hash,is_coordinator) "
                    "VALUES(?,?,?,?,?,?)",
                    (request.form["name"].strip(), email,
                     request.form.get("phone", ""),
                     request.form.get("blood_group", ""),
                     generate_password_hash(request.form["password"]),
                     1 if request.form.get("is_coordinator") else 0),
                )
            except sqlite3.IntegrityError:
                flash("Email already registered.")
                return redirect(url_for("register"))
        flash("Registered! Please log in.")
        return redirect(url_for("login"))
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"].strip().lower()
        with db() as conn:
            user = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if user and check_password_hash(user["password_hash"], request.form["password"]):
            session["user_id"] = user["id"]
            return redirect(url_for("dashboard"))
        flash("Invalid email or password.")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

# ---------- pages ----------
@app.route("/")
@login_required
def dashboard():
    with db() as conn:
        reqs = conn.execute("""
            SELECT r.*, u.name AS coordinator
            FROM requests r JOIN users u ON u.id = r.coordinator_id
            ORDER BY r.created_at DESC
        """).fetchall()
    return render_template("dashboard.html", reqs=reqs)

@app.route("/request/new", methods=["GET", "POST"])
@login_required
def new_request():
    user = current_user()
    if not user["is_coordinator"]:
        flash("Only coordinators can create requests.")
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        with db() as conn:
            cur = conn.execute(
                "INSERT INTO requests(coordinator_id,blood_group,units,hospital,urgency) "
                "VALUES(?,?,?,?,?)",
                (user["id"], request.form["blood_group"],
                 int(request.form["units"]), request.form["hospital"].strip(),
                 request.form["urgency"]),
            )
            req_id = cur.lastrowid
            members = conn.execute("SELECT id, email FROM users WHERE id != ?",
                                   (user["id"],)).fetchall()
            conn.executemany(
                "INSERT INTO notifications(user_id, request_id) VALUES(?,?)",
                [(m["id"], req_id) for m in members],
            )
        for m in members:  # after commit, so failures don't undo the request
            send_email(
                m["email"],
                f"🩸 {request.form['urgency']}: {request.form['blood_group']} blood needed",
                f"<p>Request by {user['name']}:</p>"
                f"<p>Blood group: <b>{request.form['blood_group']}</b> — "
                f"{request.form['units']} unit(s)<br>"
                f"Hospital: {request.form['hospital']}<br>"
                f"Urgency: {request.form['urgency']}</p>",
            )
        flash(f"Request created — {len(members)} members notified.")
        return redirect(url_for("dashboard"))
    return render_template("new_request.html")

@app.route("/notifications")
@login_required
def notifications():
    with db() as conn:
        rows = conn.execute("""
            SELECT n.id, n.is_read, n.created_at, r.blood_group, r.units,
                   r.hospital, r.urgency, u.name AS coordinator
            FROM notifications n
            JOIN requests r ON r.id = n.request_id
            JOIN users u ON u.id = r.coordinator_id
            WHERE n.user_id = ?
            ORDER BY n.created_at DESC
        """, (session["user_id"],)).fetchall()
        conn.execute("UPDATE notifications SET is_read=1 WHERE user_id=?",
                     (session["user_id"],))
    return render_template("notifications.html", rows=rows)

@app.route("/api/unread")
@login_required
def unread_count():
    with db() as conn:
        n = conn.execute(
            "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND is_read=0",
            (session["user_id"],)).fetchone()["c"]
    return jsonify({"unread": n})

@app.route("/request/<int:req_id>/close", methods=["POST"])
@login_required
def close_request(req_id):
    with db() as conn:
        conn.execute("UPDATE requests SET status='closed' WHERE id=? AND coordinator_id=?",
                     (req_id, session["user_id"]))
    return redirect(url_for("dashboard"))

if __name__ == "__main__":
    init_db()
    app.run(debug=True)
