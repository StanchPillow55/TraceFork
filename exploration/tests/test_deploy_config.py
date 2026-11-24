"""M11 deployment settings: database location, health probe and CORS origins. Offline."""

from fastapi.testclient import TestClient

from exploration.backend import config
from exploration.backend.main import LOCAL_ORIGINS, allowed_origins, create_app


def test_db_path_comes_from_the_environment_when_set(tmp_path, monkeypatch):
    target = tmp_path / "volume" / "exploration.sqlite"
    monkeypatch.setenv("SEARCHHAX_DB_PATH", str(target))
    assert config.default_db_path() == target
    monkeypatch.delenv("SEARCHHAX_DB_PATH")
    assert config.default_db_path() == config.EXPLORATION_ROOT / "data" / "exploration.sqlite"


def test_health_reports_presence_never_values(tmp_path, monkeypatch):
    monkeypatch.setenv("TELEM_API_KEY", "offline-test-placeholder-not-a-key")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    client = TestClient(create_app(tmp_path / "health.sqlite"))
    body = client.get("/api/health").json()
    assert body["status"] == "ok" and body["database"] == "ok"
    assert body["search_configured"] is True and body["llm_configured"] is False
    assert "offline-test-placeholder" not in str(body)


def test_extra_origins_are_added_but_a_wildcard_is_refused(monkeypatch):
    monkeypatch.setenv("SEARCHHAX_ALLOWED_ORIGINS", "https://searchhax.internal.test, *, ")
    origins = allowed_origins()
    assert origins[: len(LOCAL_ORIGINS)] == LOCAL_ORIGINS
    assert "https://searchhax.internal.test" in origins
    assert "*" not in origins and "" not in origins


def test_billable_routes_are_refused_when_the_server_switch_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("SEARCHHAX_ALLOW_BILLABLE", "false")
    monkeypatch.setenv("TELEM_API_KEY", "offline-test-placeholder-not-a-key")
    client = TestClient(create_app(tmp_path / "billable.sqlite"))
    created = client.post("/api/exploration/investigations", json={"question": "Is the billable switch enforced on the server?"})
    assert created.status_code == 200  # saving a question is local and still allowed
    inv_id = created.json()["id"]
    for path, body in [
        (f"/api/exploration/investigations/{inv_id}/step", {}),
        (f"/api/exploration/investigations/{inv_id}/answer", {}),
        ("/api/exploration/branches/branch_any/resume", {}),
        ("/api/exploration/baseline", {"story": "A", "configuration": "B0"}),
    ]:
        response = client.post(path, json=body)
        assert response.status_code == 403, (path, response.status_code)
        assert "SEARCHHAX_ALLOW_BILLABLE" in response.json()["detail"]
    after = client.get(f"/api/exploration/investigations/{inv_id}").json()["investigation"]
    assert after["status"] == "created" and after["usage"]["search_calls"] == 0
    assert client.get("/api/health").json()["billable_allowed"] is False
    # Offline fixtures stay available.
    assert client.post("/api/exploration/baseline", json={"story": "C", "configuration": "B0"}).status_code == 200


def test_billable_switch_defaults_on_for_local_development(monkeypatch):
    from exploration.backend.config import billable_allowed

    monkeypatch.delenv("SEARCHHAX_ALLOW_BILLABLE", raising=False)
    assert billable_allowed() is True
    for off in ("false", "0", "No", "OFF"):
        monkeypatch.setenv("SEARCHHAX_ALLOW_BILLABLE", off)
        assert billable_allowed() is False
