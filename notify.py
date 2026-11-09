"""Notification channels for Restic Console: desktop, email (SMTP), webhook.

Channel settings live in config.json under settings.notifications; the SMTP password
lives in its own root-only file (settings.notifications.email.password_file).
send() never raises: it returns one (channel, ok, detail) tuple per enabled channel.
"""
import json
import os
import pwd
import smtplib
import socket
import ssl
import subprocess
import urllib.request
from email.message import EmailMessage

LEVEL_ICON = {"failure": "dialog-error", "warning": "dialog-warning", "success": "emblem-default", "test": "dialog-information"}
LEVEL_EMOJI = {"failure": ":red_circle:", "warning": ":warning:", "success": ":white_check_mark:", "test": ":information_source:"}


def _desktop(c, title, body, level):
    user = c.get("user")
    if not user:
        return False, "no desktop user set"
    uid = pwd.getpwnam(user).pw_uid
    bus = f"/run/user/{uid}/bus"
    if not os.path.exists(bus):
        return False, f"{user} is not logged in to a desktop session"
    cmd = ["notify-send", "-u", "critical" if level == "failure" else "normal", "-a", "Restic Console",
           "-i", LEVEL_ICON.get(level, "dialog-information"), title, body]
    if os.geteuid() == 0:
        cmd = ["runuser", "-u", user, "--", "env", f"DBUS_SESSION_BUS_ADDRESS=unix:path={bus}", *cmd]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    return p.returncode == 0, p.stderr.strip() or "sent"


def _email(c, title, body, level):
    to = [a.strip() for a in c.get("to", []) if a.strip()]
    if not (c.get("host") and to):
        return False, "SMTP host and at least one recipient are required"
    msg = EmailMessage()
    msg["Subject"] = f"[{socket.gethostname()}] {title}"
    msg["From"] = c.get("from") or c.get("username") or f"restic-console@{socket.getfqdn()}"
    msg["To"] = ", ".join(to)
    msg.set_content(f"{body}\n\n-- \nRestic Console on {socket.gethostname()}\n")
    port = int(c.get("port") or (465 if c.get("security") == "ssl" else 587))
    ctx = ssl.create_default_context()
    if c.get("security") == "ssl":
        s = smtplib.SMTP_SSL(c["host"], port, timeout=30, context=ctx)
    else:
        s = smtplib.SMTP(c["host"], port, timeout=30)
        if c.get("security", "starttls") == "starttls":
            s.starttls(context=ctx)
    with s:
        if c.get("username"):
            pw = ""
            if c.get("password_file") and os.path.exists(c["password_file"]):
                with open(c["password_file"]) as f:
                    pw = f.read().strip()
            s.login(c["username"], pw)
        s.send_message(msg)
    return True, f"sent to {', '.join(to)}"


def _webhook(c, title, body, level):
    url = c.get("url", "")
    if not url.startswith(("https://", "http://")):
        return False, "webhook URL must start with https://"
    text = f"{LEVEL_EMOJI.get(level, '')} *{title}*\n{body}"
    data = json.dumps({"text": text, "content": text[:1900]}).encode()   # Slack/Mattermost use text, Discord content
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "User-Agent": "restic-console"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return 200 <= r.status < 300, f"HTTP {r.status}"


CHANNELS = {"desktop": _desktop, "email": _email, "webhook": _webhook}


def send(settings, title, body, level="failure", only=None):
    n = (settings or {}).get("notifications", {})
    results = []
    for name, fn in CHANNELS.items():
        c = n.get(name, {})
        if only and name != only:
            continue
        if not only and not c.get("enabled"):
            continue
        try:
            ok, detail = fn(c, title, body, level)
        except Exception as e:  # noqa: BLE001
            ok, detail = False, f"{type(e).__name__}: {e}"
        results.append((name, ok, detail))
    return results


def wanted(job, level):
    """Does this job want a notification for this outcome? level: failure|warning|success."""
    on = job.get("notify", {"failure": True})
    return bool(on.get(level, level == "failure"))
