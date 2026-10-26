"""Persistence: job rows, idempotency tokens, outputs."""
from __future__ import annotations

from app.db import Database


def _db(tmp_path) -> Database:
    return Database(tmp_path / "studio.db")


def _job(db, **over):
    kw = dict(job_id=over.get("job_id", "j1"), workflow_id="test-turbo",
              workflow_name="Test", params={"prompt": "x"}, graph={"1": {}},
              client_id="c1", client_token=over.get("client_token"))
    kw.update({k: v for k, v in over.items() if k != "client_token"})
    return db.create_job(**kw)


def test_create_and_read_job(tmp_path):
    db = _db(tmp_path)
    _job(db)
    row = db.get_job("j1")
    assert row is not None
    assert row["status"] == "queued"
    assert row["workflow_id"] == "test-turbo"
    assert row["params"] == {"prompt": "x"}
    db.close()


def test_unknown_job_returns_none(tmp_path):
    db = _db(tmp_path)
    assert db.get_job("nope") is None
    db.close()


def test_token_lookup_is_idempotent(tmp_path):
    db = _db(tmp_path)
    _job(db, client_token="tok")
    assert db.job_by_token("tok")["id"] == "j1"
    assert db.job_by_token("other") is None
    db.close()


def test_update_and_finish(tmp_path):
    db = _db(tmp_path)
    _job(db)
    db.update_job("j1", status="running", prompt_id="p1", progress=0.4)
    row = db.get_job("j1")
    assert row["status"] == "running" and row["prompt_id"] == "p1"
    assert abs(row["progress"] - 0.4) < 1e-6

    db.finish_job("j1", "completed")
    row = db.get_job("j1")
    assert row["status"] == "completed"
    assert row["progress"] == 1.0
    assert row["finished_at"]
    db.close()


def test_finish_failed_records_error_and_zero_progress(tmp_path):
    db = _db(tmp_path)
    _job(db)
    db.finish_job("j1", "failed", error="boom")
    row = db.get_job("j1")
    assert row["status"] == "failed"
    assert row["error"] == "boom"
    assert row["progress"] == 0.0
    db.close()


def test_unfinished_ids_excludes_terminal_states(tmp_path):
    db = _db(tmp_path)
    _job(db, job_id="a")
    _job(db, job_id="b")
    db.finish_job("b", "completed")
    assert db.unfinished_job_ids() == ["a"]
    db.close()


def test_count_and_list_jobs(tmp_path):
    db = _db(tmp_path)
    _job(db, job_id="a")
    _job(db, job_id="b")
    db.finish_job("b", "completed")
    assert db.count_jobs() == 2
    assert db.count_jobs("completed") == 1
    assert len(db.list_jobs(limit=10)) == 2
    db.close()


def test_job_survives_reopen(tmp_path):
    path = tmp_path / "studio.db"
    db = Database(path)
    _job(db)
    db.close()
    db2 = Database(path)
    assert db2.get_job("j1") is not None
    db2.close()


def test_outputs_are_recorded_and_listed(tmp_path):
    db = _db(tmp_path)
    _job(db)
    db.add_outputs("j1", "test-turbo", [
        {"kind": "image", "filename": "studio/a.png", "size_bytes": 12},
    ])
    outs = db.list_outputs()
    assert len(outs) == 1
    assert outs[0]["filename"] == "studio/a.png"
    assert db.count_outputs() == 1
    db.close()


def test_duplicate_outputs_are_not_doubled(tmp_path):
    db = _db(tmp_path)
    _job(db)
    for _ in range(2):
        db.add_outputs("j1", "test-turbo", [{"kind": "image", "filename": "studio/a.png"}])
    assert db.count_outputs() == 1
    db.close()


def test_event_log(tmp_path):
    db = _db(tmp_path)
    db.log_event("hello", job_id="j1", level="info")
    events = db.recent_events()
    assert events and events[0]["message"] == "hello"
    db.close()


def test_delete_job(tmp_path):
    db = _db(tmp_path)
    _job(db)
    assert db.delete_job("j1") is True
    assert db.get_job("j1") is None
    assert db.delete_job("j1") is False
    db.close()
