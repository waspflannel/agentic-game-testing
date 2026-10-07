"""Real temporary SQLite/hashed ledger checks; no provider, worker or root-data calls."""
import hashlib
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ashad.app import create_app
from test_controller import FakeCodexBackend, MemoryCredentials


EVENT_COUNT = 3200
RUN_ID = "activity-fixture"


@pytest.fixture
def activity(tmp_path):
    backend = FakeCodexBackend(ready=False)
    app = create_app(tmp_path / "activity", credentials=MemoryCredentials(None),
                     start_services=False, initialize=False, ai_backend=backend)
    store = app.state.store
    frozen = {"id": RUN_ID, "state": "Ended", "players": {}, "events": {"schedule": "retained"},
              "plan": {"frozen": "original"}, "approved_plan": {"frozen": "original"}}
    store.put("runs", frozen)
    entries = []
    previous = None
    for number in range(1, EVENT_COUNT + 1):
        event = {"id": f"evt_fixture_{number}", "version": 1, "run_id": RUN_ID,
                 "seq": number * 2, "timestamp": float(number), "monotonic": float(number),
                 "kind": "investigation_started" if number in {31, 310} else "recorded_activity",
                 "payload": {"message": "Straße: observed" if number % 25 == 0 else "ordinary",
                             "reason": f"Recorded reason {number}"}, "previous_hash": previous}
        if number == 3150:
            event["payload"]["note"] = "investigation_started is prose, not this event's kind"
        event["hash"] = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        previous = event["hash"]
        entries.append(event)
    ledger = store.run_dir(RUN_ID) / "ledger.jsonl"
    ledger.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in entries), encoding="utf-8")
    original_ledger = ledger.read_bytes()
    with store.transaction():
        assert store.recover_ledger(RUN_ID) == []
    queries, fetched = [], []
    store.db.set_trace_callback(queries.append)

    def count_event_rows(cursor, row):
        if len(row) == 1 and isinstance(row[0], str) and '"id": "evt_fixture_' in row[0]:
            fetched.append(row[0])
        return sqlite3.Row(cursor, row)

    store.db.row_factory = count_event_rows
    with TestClient(app) as client:
        yield app, client, entries, queries, fetched
        store.db.row_factory = sqlite3.Row
        retained = store.require("runs", RUN_ID)
        assert {key: retained[key] for key in ("plan", "approved_plan", "events")} == {key: frozen[key] for key in ("plan", "approved_plan", "events")}
        assert store.events(RUN_ID, limit=EVENT_COUNT) == entries
        assert ledger.read_bytes() == original_ledger
        assert backend.requests == []


def get_page(client, **parameters):
    response = client.get(f"/api/runs/{RUN_ID}/events", params=parameters)
    assert response.status_code == 200, response.text
    return response.json()


def event_selects(queries):
    return [query for query in queries if query.startswith("SELECT data FROM events")]


def test_ordinary_page_fetches_only_limit_plus_one_sql_rows(activity):
    app, client, entries, queries, fetched = activity
    page = get_page(client, limit=100)
    assert page == {"events": entries[:100], "next": 200, "before": 2,
                    "total": EVENT_COUNT, "has_more": True, "older_count": 0, "newer_count": 3100}
    assert len(fetched) == 101
    assert event_selects(queries) == [f"SELECT data FROM events WHERE run_id='{RUN_ID}' AND seq>0 ORDER BY seq LIMIT 101"]
    queries.clear()
    fetched.clear()
    assert app.state.store.events(RUN_ID, after=200, limit=7) == entries[100:107]
    assert len(fetched) == 7
    assert event_selects(queries)[0].endswith("LIMIT 7")


def test_forward_cursor_walk_is_exact_at_full_final_page(activity):
    _, client, entries, queries, fetched = activity
    cursor, seen = 0, []
    for start in range(0, EVENT_COUNT, 100):
        queries.clear()
        fetched.clear()
        page = get_page(client, after=cursor, limit=100)
        assert page["events"] == entries[start:start + 100]
        assert page["has_more"] is (start + 100 < EVENT_COUNT)
        assert page["total"] == EVENT_COUNT
        assert page["older_count"] == start and page["newer_count"] == EVENT_COUNT - start - 100
        assert len(fetched) == min(101, EVENT_COUNT - start)
        assert all(query.endswith("LIMIT 101") for query in event_selects(queries))
        seen.extend(event["id"] for event in page["events"])
        cursor = page["next"]
    assert seen == [event["id"] for event in entries]
    fetched.clear()
    empty = get_page(client, after=cursor, limit=100)
    assert empty == {"events": [], "next": cursor, "before": None,
                     "total": EVENT_COUNT, "has_more": False, "older_count": EVENT_COUNT, "newer_count": 0}
    assert not fetched


def test_run_detail_and_older_pages_are_true_bounded_tails(activity):
    app, client, entries, queries, fetched = activity
    original = app.state.store.require("runs", RUN_ID)
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["events"] == entries[-200:]
    assert detail["event_states"] == original["events"]
    assert detail["events_page"] == {"next": 6400, "before": 6002,
                                      "total": EVENT_COUNT, "has_more": True,
                                      "older_count": 3000, "newer_count": 0, "recent_100_older_count": 3100}
    assert len(fetched) == 201
    assert event_selects(queries)[0].endswith("ORDER BY seq DESC LIMIT 201")
    fetched.clear()
    older = get_page(client, before=detail["events_page"]["before"], limit=100)
    assert older["events"] == entries[-300:-200]
    assert older["before"] == 5802 and older["next"] == 6000 and older["has_more"]
    assert older["older_count"] == 2900 and older["newer_count"] == 200
    assert len(fetched) == 101
    tail = get_page(client, tail="true", limit=100)
    assert tail["events"] == entries[-100:]
    assert tail["next"] == 6400 and tail["before"] == 6202 and tail["has_more"]
    assert tail["older_count"] == 3100 and tail["newer_count"] == 0
    first = get_page(client, before=202, limit=100)
    assert first["events"] == entries[:100] and not first["has_more"]
    assert first["older_count"] == 0 and first["newer_count"] == 3100
    empty = get_page(client, before=2, limit=100)
    assert empty["events"] == [] and empty["before"] == 2 and not empty["has_more"]
    assert empty["older_count"] == 0 and empty["newer_count"] == EVENT_COUNT
    assert app.state.store.require("runs", RUN_ID) == original


@pytest.mark.parametrize("search", ["STRASSE", '"message": "Straße', "payload", "no matching activity"])
def test_legacy_serialized_json_unicode_search_and_cursor_compatibility(activity, search):
    app, client, entries, _, _ = activity
    matches = [event for event in entries if event["seq"] > 1000
               and search.casefold() in json.dumps(event, ensure_ascii=False).casefold()]
    page = get_page(client, search=search, after=1000, limit=3)
    assert page["events"] == matches[:3]
    assert page["next"] == (matches[2]["seq"] if len(matches) >= 3 else matches[-1]["seq"] if matches else 1000)
    assert page["has_more"] is (len(matches) > 3)
    assert page["total"] == EVENT_COUNT
    if page["events"]:
        assert page["older_count"] == sum(event["seq"] < page["events"][0]["seq"] for event in entries)
        assert page["newer_count"] == sum(event["seq"] > page["events"][-1]["seq"] for event in entries)
    else:
        assert page["older_count"] == 500 and page["newer_count"] == 2700
    assert app.state.store.events(RUN_ID, after=1000, limit=3, search=search) == matches[:3]
    if matches:
        final_after = matches[-4]["seq"] if len(matches) > 3 else 1000
        final = get_page(client, search=search, after=final_after, limit=3)
        assert final["events"] == matches[-3:] and final["has_more"] is False


def test_search_can_page_backwards_without_changing_match_semantics(activity):
    _, client, entries, _, _ = activity
    matches = [event for event in entries if "strasse" in json.dumps(event, ensure_ascii=False).casefold()]
    tail = get_page(client, search="STRASSE", tail="true", limit=3)
    assert tail["events"] == matches[-3:] and tail["has_more"]
    older = get_page(client, search="STRASSE", before=tail["before"], limit=3)
    assert older["events"] == matches[-6:-3] and older["has_more"]


@pytest.mark.parametrize("state", ["Investigating", "Paused"])
def test_legacy_pause_detail_does_not_load_entire_event_history(activity, state):
    app, client, entries, queries, fetched = activity
    run = app.state.store.require("runs", RUN_ID)
    run["state"] = state
    app.state.store.put("runs", run)
    detail = client.get(f"/api/runs/{RUN_ID}").json()
    assert detail["events"] == entries[-200:]
    assert len(fetched) == (202 if state == "Investigating" else 201)
    assert all("LIMIT " in query for query in event_selects(queries))
    if state == "Investigating":
        assert detail["pause"]["reason"] == "Recorded reason 310"
        assert "json_extract(data, '$.kind')='investigation_started'" in event_selects(queries)[-1]
    else:
        assert detail["pause"]["category"] == "manual_pause"


def test_cursor_validation_and_limit_clamping(activity):
    _, client, entries, queries, fetched = activity
    for parameters in ({"after": 2, "before": 10}, {"after": 2, "tail": "true"}, {"before": "invalid"}):
        assert client.get(f"/api/runs/{RUN_ID}/events", params=parameters).status_code == 422
    page = get_page(client, after=-20, limit=0)
    assert page["events"] == entries[:1] and page["has_more"]
    assert len(fetched) == 2 and event_selects(queries)[-1].endswith("LIMIT 2")
    fetched.clear()
    maximum = get_page(client, limit=10000)
    assert maximum["events"] == entries[:1000] and maximum["has_more"]
    assert len(fetched) == 1001 and event_selects(queries)[-1].endswith("LIMIT 1001")
    assert client.get("/api/runs/missing/events").status_code == 422


def test_empty_run_has_exact_initial_and_tail_metadata(activity):
    app, client, _, _, _ = activity
    run = {"id": "empty-activity", "state": "Ended", "players": {}, "events": {}, "plan": {}}
    app.state.store.put("runs", run)
    initial = client.get("/api/runs/empty-activity/events").json()
    assert initial == {"events": [], "next": 0, "before": None, "total": 0, "has_more": False,
                       "older_count": 0, "newer_count": 0}
    tail = client.get("/api/runs/empty-activity/events?tail=true").json()
    assert tail == initial
    detail = client.get("/api/runs/empty-activity").json()
    assert detail["events"] == []
    assert detail["events_page"] == {**{key: initial[key] for key in initial if key != "events"}, "recent_100_older_count": 0}


def test_sequence_boundary_counts_use_sql_index_without_fetching_event_rows(activity):
    app, client, entries, queries, fetched = activity
    store = app.state.store
    assert store.count_events(RUN_ID, before=203) == 101
    assert store.count_events(RUN_ID, after=203) == 3099
    assert store.count_events(RUN_ID, before=203, after=199) == 2
    assert not fetched
    for operator in ("<", ">"):
        plan = store.db.execute(f"EXPLAIN QUERY PLAN SELECT COUNT(*) FROM events WHERE run_id=? AND seq{operator}?", (RUN_ID, 203)).fetchall()
        assert any("INDEX" in row[3] and "run_id=?" in row[3] and "seq" in row[3] for row in plan)
    page = get_page(client, after=203, limit=100)
    assert page["events"] == entries[101:201]
    assert page["older_count"] == 101 and page["newer_count"] == 2999
    assert len(fetched) == 101
    assert any(query.endswith("AND seq<204") for query in queries if query.startswith("SELECT COUNT(*)"))
    assert any(query.endswith("AND seq>402") for query in queries if query.startswith("SELECT COUNT(*)"))


def test_search_count_metadata_is_all_ledger_not_filtered_matches(activity):
    _, client, entries, _, _ = activity
    page = get_page(client, search="STRASSE", before=3001, limit=3)
    assert [event["seq"] for event in page["events"]] == [2900, 2950, 3000]
    assert page["older_count"] == 1449 and page["newer_count"] == 1700
    assert page["total"] == EVENT_COUNT and page["has_more"]
    empty = get_page(client, search="not an event", tail="true", limit=100)
    assert empty["older_count"] == EVENT_COUNT and empty["newer_count"] == 0
    assert empty["events"] == [] and not empty["has_more"]


def test_recent_100_count_keeps_legacy_200_tail_for_short_runs(activity):
    app, client, _, _, _ = activity
    store = app.state.store
    store.put("runs", {"id": "short-activity", "state": "Ended", "players": {}, "events": {}, "plan": {}})
    for number in range(150):
        store.event("short-activity", "recorded_activity", {"message": f"Recorded event {number}"})
    detail = client.get("/api/runs/short-activity").json()
    assert len(detail["events"]) == 150
    assert detail["events_page"]["older_count"] == 0
    assert detail["events_page"]["recent_100_older_count"] == 50
    assert detail["events_page"]["newer_count"] == 0 and not detail["events_page"]["has_more"]
