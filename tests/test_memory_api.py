from fastapi.testclient import TestClient

from app.main import create_app


def client(tmp_path):
    return TestClient(create_app(str(tmp_path / "firmsight.db")))


def proposal_payload():
    return {
        "type": "ENGINEERING_FACT",
        "statement": "ADS1115 is exclusively accessed by measurement_task.",
        "scope": {"type": "COMPONENT", "component": "measurement"},
        "evidence": [
            {"symbol": "measurement_task", "file": "main/measurement.c", "line": 42},
            {"symbol": "ads1115_read", "file": "drivers/ads1115.c", "line": 80},
        ],
        "source": {"type": "ENGINEER_CONFIRMED", "finding_id": "FS-120", "engineer_note": "Confirmed during review."},
    }


def approve(api, proposal_id):
    response = api.post(f"/api/memory/proposals/{proposal_id}/approve", json={"approved_by": "adri"})
    assert response.status_code == 201, response.text
    return response.json()


def test_memory_requires_engineer_approval_and_active_context_only(tmp_path):
    api = client(tmp_path)
    proposed = api.post("/api/projects/ground-checker/memory/proposals", json=proposal_payload())
    assert proposed.status_code == 201
    assert api.get("/api/projects/ground-checker/memory/context").json() == {"memories": []}

    memory = approve(api, proposed.json()["id"])
    context = api.get("/api/projects/ground-checker/memory/context?symbol=ads1115_read")
    assert context.status_code == 200
    assert [item["id"] for item in context.json()["memories"]] == [memory["id"]]

    disabled = api.post(f"/api/memory/{memory['id']}/disable")
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    assert api.get("/api/projects/ground-checker/memory/context").json() == {"memories": []}


def test_revalidation_marks_conflicting_ownership_memory(tmp_path):
    api = client(tmp_path)
    proposed = api.post("/api/projects/ground-checker/memory/proposals", json=proposal_payload()).json()
    memory = approve(api, proposed["id"])

    result = api.post(
        "/api/projects/ground-checker/memory/revalidate",
        json={"commit_sha": "abc123", "observations": [{"symbol": "ads1115_read", "caller": "calibration_task", "file": "main/calibration.c", "line": 17}]},
    )
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["checked"] == 1
    assert body["conflicts"][0]["memory"]["id"] == memory["id"]
    assert body["conflicts"][0]["observation"]["caller"] == "calibration_task"
    assert api.get(f"/api/memory/{memory['id']}").json()["status"] == "NEEDS_REVALIDATION"
    history = api.get(f"/api/memory/{memory['id']}/revalidation-events")
    assert history.status_code == 200
    assert history.json()[0]["commit_sha"] == "abc123"
    assert history.json()[0]["observation"]["file"] == "main/calibration.c"


def test_proposal_can_be_edited_or_ignored_without_creating_memory(tmp_path):
    api = client(tmp_path)
    proposal = api.post("/api/projects/ground-checker/memory/proposals", json=proposal_payload()).json()
    changed = api.patch(f"/api/memory/proposals/{proposal['id']}", json={"statement": "Updated after engineer review."})
    assert changed.status_code == 200
    assert changed.json()["statement"] == "Updated after engineer review."
    ignored = api.delete(f"/api/memory/proposals/{proposal['id']}")
    assert ignored.status_code == 204
    assert api.post(f"/api/memory/proposals/{proposal['id']}/approve", json={"approved_by": "adri"}).status_code == 404


def test_revalidation_does_not_invalidate_non_ownership_lessons(tmp_path):
    api = client(tmp_path)
    payload = proposal_payload()
    payload["type"] = "LESSON_LEARNED"
    payload["statement"] = "ADS1115 readings are sampled every 500 ms."
    proposal = api.post("/api/projects/ground-checker/memory/proposals", json=payload).json()
    memory = approve(api, proposal["id"])
    result = api.post("/api/projects/ground-checker/memory/revalidate", json={"observations": [{"symbol": "ads1115_read", "caller": "calibration_task"}]})
    assert result.json()["conflicts"] == []
    assert api.get(f"/api/memory/{memory['id']}").json()["status"] == "ACTIVE"
