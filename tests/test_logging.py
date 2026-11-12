import json
import logging

from app.logging import JsonFormatter, set_correlation_id


def format_record(**extra) -> dict:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    for k, v in extra.items():
        setattr(record, k, v)
    return json.loads(JsonFormatter().format(record))


def test_json_log_has_correlation_id_and_fields():
    set_correlation_id("abc123")
    try:
        out = format_record(monitor_id="m1")
    finally:
        set_correlation_id(None)
    assert out["message"] == "hello world"
    assert out["correlation_id"] == "abc123"
    assert out["monitor_id"] == "m1" and out["level"] == "INFO"


def test_sensitive_fields_are_redacted():
    out = format_record(
        password="hunter2", smtp_password="x", jwt_token="t", authorization="Bearer y"
    )
    blob = json.dumps(out)
    for secret in ("hunter2", "Bearer y"):
        assert secret not in blob
    assert out["password"] == "[REDACTED]" and out["jwt_token"] == "[REDACTED]"


async def test_api_echoes_or_generates_correlation_id(client):
    r = await client.get("/healthz")
    generated = r.headers["x-request-id"]
    assert len(generated) == 32
    r2 = await client.get("/healthz", headers={"X-Request-ID": "trace1234"})
    assert r2.headers["x-request-id"] == "trace1234"
    r3 = await client.get("/healthz", headers={"X-Request-ID": "bad id\twith junk!"})
    assert r3.headers["x-request-id"] != "bad id\twith junk!"


async def test_request_logs_carry_the_correlation_id(client, caplog):
    caplog.set_level(logging.INFO, logger="app.request")
    await client.get("/healthz", headers={"X-Request-ID": "trace9999"})
    rec = next(r for r in caplog.records if r.name == "app.request")
    assert rec.path == "/healthz" and rec.status == 200


async def test_worker_logs_correlation_id_from_job(sessionmaker, redis, settings, caplog):
    from app.services.queue import CheckQueue, make_job
    from app.workers.check_worker import process_job
    from tests.conftest import make_monitor, make_user

    user = await make_user(sessionmaker)
    m = await make_monitor(sessionmaker, user.id, target_url="http://10.0.0.1/")
    strict = settings.model_copy(update={"allow_private_targets": False})
    seen = []

    class Capture(logging.Handler):
        def emit(self, record):
            from app.logging import get_correlation_id

            seen.append((record.getMessage(), get_correlation_id()))

    handler = Capture()
    logging.getLogger("app.check_worker").addHandler(handler)
    logging.getLogger("app.check_worker").setLevel(logging.INFO)
    try:
        await process_job(
            make_job(m.id, "corr-77"),
            sessionmaker=sessionmaker,
            queue=CheckQueue(redis),
            settings=strict,
        )
    finally:
        logging.getLogger("app.check_worker").removeHandler(handler)
    assert ("check completed", "corr-77") in seen
