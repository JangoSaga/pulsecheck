"""End-to-end smoke test against a running `docker compose up` stack.

    python tools/smoke_test.py

Uses only the standard library. Talks to the API on :8001 and Mailpit on :8025, and
monitors the compose `target` container (which is reachable because compose enables
ALLOW_PRIVATE_TARGETS for development).
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

API = "http://localhost:8001"
MAILPIT = "http://localhost:8025"
TARGET = "http://target:9000"


def call(method, url, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        return exc.code, (json.loads(raw) if raw else None)


def wait_for(description, fn, timeout=90, interval=1.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            print(f"  ok: {description}")
            return value
        time.sleep(interval)
    raise SystemExit(f"FAIL: timed out waiting for {description}")


def expect(description, condition):
    if not condition:
        raise SystemExit(f"FAIL: {description}")
    print(f"  ok: {description}")


def mails_for(email):
    query = urllib.parse.quote(f"to:{email}")
    _, body = call("GET", f"{MAILPIT}/api/v1/search?query={query}")
    return [m["Subject"] for m in body["messages"]]


def main():
    suffix = uuid.uuid4().hex[:8]
    email, other = f"smoke-{suffix}@example.com", f"other-{suffix}@example.com"
    password = "smoke-test-password"

    print("auth")
    for e in (email, other):
        status, _ = call("POST", f"{API}/api/v1/auth/register", {"email": e, "password": password})
        expect(f"register {e}", status == 201)
    token = call("POST", f"{API}/api/v1/auth/login", {"email": email, "password": password})[1][
        "access_token"
    ]
    other_token = call("POST", f"{API}/api/v1/auth/login", {"email": other, "password": password})[
        1
    ]["access_token"]

    print("monitor creation and SSRF guard")
    status, _ = call(
        "POST",
        f"{API}/api/v1/monitors",
        {"name": "meta", "target_url": "http://169.254.169.254/latest/meta-data/"},
        token,
    )
    expect("cloud metadata target rejected (422)", status == 422)
    status, mon = call(
        "POST",
        f"{API}/api/v1/monitors",
        {
            "name": "smoke target",
            "target_url": f"{TARGET}/status/200",
            "interval_seconds": 5,
            "timeout_seconds": 3,
            "failure_threshold": 2,
            "recovery_threshold": 2,
        },
        token,
    )
    expect("monitor created (201)", status == 201)
    mid = mon["id"]
    url = f"{API}/api/v1/monitors/{mid}"

    print("ownership")
    expect("other user gets 404", call("GET", url, token=other_token)[0] == 404)
    expect("no token gets 401", call("GET", url)[0] == 401)

    print("healthy checks are recorded")
    wait_for(
        "healthy check stored",
        lambda: any(
            c["outcome"] == "healthy" for c in call("GET", f"{url}/checks", token=token)[1]["items"]
        ),
    )
    expect("monitor status is up", call("GET", url, token=token)[1]["status"] == "up")

    print("outage opens one incident and sends mail")
    call("PATCH", url, {"target_url": f"{TARGET}/status/500"}, token)
    incident = wait_for(
        "incident opened",
        lambda: next(
            iter(call("GET", f"{url}/incidents", token=token)[1]["items"]),
            None,
        ),
    )
    expect("incident is open", incident["status"] == "open")
    wait_for("DOWN mail in Mailpit", lambda: any("DOWN" in s for s in mails_for(email)))
    time.sleep(8)  # more failures must not duplicate the incident or the mail
    incidents = call("GET", f"{url}/incidents", token=token)[1]
    expect("still exactly one incident", incidents["total"] == 1)
    expect("exactly one DOWN mail", sum("DOWN" in s for s in mails_for(email)) == 1)

    print("recovery resolves the incident and sends mail")
    call("PATCH", url, {"target_url": f"{TARGET}/status/200"}, token)
    wait_for(
        "incident resolved",
        lambda: call("GET", f"{url}/incidents", token=token)[1]["items"][0]["status"] == "resolved",
    )
    wait_for("RECOVERED mail in Mailpit", lambda: any("RECOVERED" in s for s in mails_for(email)))
    notes = call("GET", f"{url}/incidents", token=token)[1]["items"][0]["notifications"]
    expect(
        "both notifications recorded as sent",
        sorted((n["event"], n["status"]) for n in notes)
        == [("opened", "sent"), ("resolved", "sent")],
    )

    print("analytics")
    status, up = call("GET", f"{API}/api/v1/analytics/uptime?monitor_id={mid}", token=token)
    stats = up["monitors"][0]
    expect("analytics 200", status == 200)
    expect(
        "analytics counts failures and the incident",
        stats["failed_checks"] >= 2
        and stats["successful_checks"] >= 1
        and stats["incident_count"] == 1
        and 0 < stats["uptime_percentage"] < 100,
    )

    print("pause, resume, delete")
    expect("pause", call("POST", f"{url}/pause", token=token)[1]["is_active"] is False)
    time.sleep(1)
    before = call("GET", f"{url}/checks", token=token)[1]["total"]
    time.sleep(12)
    after = call("GET", f"{url}/checks", token=token)[1]["total"]
    expect("no checks run while paused", before == after)
    expect("resume", call("POST", f"{url}/resume", token=token)[1]["is_active"] is True)
    wait_for(
        "checks resume",
        lambda: call("GET", f"{url}/checks", token=token)[1]["total"] > after,
    )
    expect("delete (204)", call("DELETE", url, token=token)[0] == 204)
    expect("deleted monitor is gone", call("GET", url, token=token)[0] == 404)

    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    sys.exit(main())
