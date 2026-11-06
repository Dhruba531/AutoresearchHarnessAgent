"""End-to-end: every gate of the console, through the real HTTP API.

Only the model provider is faked (tests/fake_llm.py); the database, sandbox
execution, WebSocket stream, budget logic and groundedness gate are real.
"""

import base64
import time

import pytest
from fastapi.testclient import TestClient

from backend import llm
from backend.app import app
from backend.tests.fake_llm import BASELINE, FakeClient

FAKE = FakeClient()
llm.client_factory = lambda provider, key, model: FAKE

CSV = "id,v\n1,1\n2,2\n3,3\n4,4\n"


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        r = c.post("/api/auth/register", json={"email": "op@example.com", "password": "correct-horse", "name": "Op"})
        assert r.status_code == 200, r.text
        c.headers["Authorization"] = f"Bearer {r.json()['token']}"
        yield c


def wait(c, url, done, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        body = c.get(url).json()
        if done(body):
            return body
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting on {url}: {body}")


def setup_project(c, title):
    p = c.post("/api/projects", json={"title": title}).json()
    c.post(f"/api/projects/{p['id']}/idea", json={"idea_text": "Estimate the mean of column v."})
    up = c.post(f"/api/projects/{p['id']}/uploads",
                json={"filename": "data.csv", "content_base64": base64.b64encode(CSV.encode()).decode()})
    assert up.status_code == 200, up.text
    return p["id"]


def test_auth_and_gates_before_keys(client):
    assert client.get("/health").json()["ok"]
    assert client.get("/api/me").json()["email"] == "op@example.com"
    caps = client.get("/api/capabilities").json()
    assert caps["real_run_ready"] is False and caps["execution_backend"] == "local"
    pid = setup_project(client, "No key yet")
    r = client.post(f"/api/projects/{pid}/briefs/generate", json={})
    assert r.status_code == 400 and "key" in r.json()["detail"].lower()
    # unauthenticated access is refused
    assert TestClient(app).get("/api/projects").status_code == 401


def test_provider_keys_are_write_only(client):
    r = client.post("/api/provider-keys", json={"provider": "deepseek", "api_key": "sk-deepseek-1234abcd",
                                                "model_name": "deepseek-reasoner", "is_enabled": True})
    assert r.status_code == 200, r.text
    assert "1234abcd" not in r.json()["masked_key"] and r.json()["key_last4"] == "abcd"
    listed = client.get("/api/provider-keys").json()
    assert [k["provider"] for k in listed] == ["deepseek"]
    assert "sk-deepseek" not in str(listed)
    caps = client.get("/api/capabilities").json()
    assert caps["real_run_ready"] and caps["text_model"] == "deepseek-reasoner"


def run_to_completion(client, pid, config, budget=5):
    brief = client.post(f"/api/projects/{pid}/briefs/generate", json={}).json()
    assert brief["version"] == 1 and "## Hypothesis" in brief["content_markdown"]
    # G2 is locked until G1 clears
    assert client.post(f"/api/projects/{pid}/runs/estimate", json={"budget_threshold": budget}).status_code == 400
    assert client.post(f"/api/briefs/{brief['id']}/approve", json={"notes": "scope reviewed"}).json()["is_approved"]
    assert client.patch(f"/api/briefs/{brief['id']}", json={"content_markdown": "x"}).status_code == 409
    est = client.post(f"/api/projects/{pid}/runs/estimate", json={"budget_threshold": budget, "config_json": config}).json()
    assert est["cost_estimate"] > 0
    # the spend gate needs explicit approval
    assert client.post(f"/api/projects/{pid}/runs", json={"budget_threshold": budget, "config_json": config}).status_code == 400
    run = client.post(f"/api/projects/{pid}/runs", json={"budget_threshold": budget, "approved_cost": True,
                                                         "mode": "real", "config_json": config}).json()
    assert run["status"] == "queued"
    return wait(client, f"/api/runs/{run['id']}", lambda r: r["status"] in {"completed", "failed", "cancelled"})


def test_pipeline_run_with_execution_review_and_export(client):
    pid = setup_project(client, "Pipeline")
    run = run_to_completion(client, pid, {"orchestration": "pipeline", "execute": True, "figures": False})
    assert run["status"] == "completed", run.get("error")
    ex = run["analysis_json"]["execution"]
    assert ex["status"] == "ok" and "mean=2.5000" in ex["stdout"] and ex["results"]["mean"] == 2.5
    names = {a["name"] for a in run["artifacts"]}
    assert {"plan.md", "experiment.py", "paper.md", "stdout.txt", "results.json"} <= names
    assert run["actual_cost"] > 0 and run["logs"]
    findings = client.get(f"/api/projects/{pid}/reviews").json()
    assert {f["category"] for f in findings} == {"methods", "statistics", "novelty", "safety"}
    assert all("Score: 7/10" in f["finding"] for f in findings)

    # live-log socket replays a terminal status for a finished run
    token = client.headers["Authorization"].split()[1]
    with client.websocket_connect(f"/api/runs/{run['id']}/logs/ws?token={token}") as ws:
        assert ws.receive_json() == {"type": "status", "status": "completed"}

    # G3: targeted revision — estimate, preview, apply
    excerpt = "Small sample."
    est = client.post(f"/api/runs/{run['id']}/revisions/estimate", json={"instruction": "quantify", "target_excerpt": excerpt})
    assert est.status_code == 200 and est.json()["cost_estimate"] > 0
    rev = client.post(f"/api/runs/{run['id']}/revisions/preview",
                      json={"instruction": "quantify", "target_excerpt": excerpt, "approved_cost": True}).json()
    assert rev["status"] == "proposed"
    applied = client.post(f"/api/runs/{run['id']}/revisions/{rev['id']}/apply", json={"notes": "reviewer asked"}).json()
    assert "preliminary" in applied["draft_markdown"] and excerpt not in applied["draft_markdown"]

    # G4: groundedness passes (every number traces to stdout), package is signed
    final = client.post(f"/api/projects/{pid}/final/approve", json={"notes": "verified numbers"})
    assert final.status_code == 200, final.text
    assert final.json()["groundedness"]["fabrications"] == []
    assert client.get(f"/api/projects/{pid}").json()["status"] == "exported"

    # artifact download via signed link, no bearer token
    paper = next(a for a in run["artifacts"] if a["name"] == "paper.md")
    assert TestClient(app).get(paper["path"]).status_code == 200


def test_groundedness_blocks_placeholders_and_unbacked_numbers(client):
    FAKE.draft_extra = " TODO add citation [3]."
    try:
        pid = setup_project(client, "Ungrounded")
        run = run_to_completion(client, pid, {"orchestration": "pipeline", "execute": False})
        assert run["status"] == "completed"
        r = client.post(f"/api/projects/{pid}/final/approve", json={"notes": "try"})
        assert r.status_code == 409
        rep = r.json()["detail"]
        assert "TODO" in rep["placeholders"]
        assert any("[3]" in f for f in rep["fabrications"])
        assert any("no code was executed" in f for f in rep["fabrications"])
        forced = client.post(f"/api/projects/{pid}/final/approve", json={"notes": "accepted", "force": True})
        assert forced.status_code == 200 and forced.json()["forced"]
    finally:
        FAKE.draft_extra = ""


def test_super_agent_delegates_in_parallel_and_remembers(client):
    pid = setup_project(client, "Super agent")
    run = run_to_completion(client, pid, {"orchestration": "agentic", "execute": True})
    assert run["status"] == "completed", run.get("error")
    tools = [a["tool"] for a in run["analysis_json"]["actions"]]
    assert "lead:delegate" in tools and "lead:load_skill" in tools
    assert any(t.startswith("coder-1.1:run_python") for t in tools)
    assert any(t.startswith("researcher-1.2:") for t in tools)
    assert run["analysis_json"]["execution"]["results"]["mean"] == 2.5
    # the fake lead uses the original {role, goal} task form; each sub-agent's report lands in its folder
    from backend.pipeline import run_dir

    agents = run_dir(run["id"]) / "work" / "agents"
    assert "mean=2.5000" in (agents / "coder-1.1" / "report.md").read_text()
    assert "Main threat: n=4." in (agents / "researcher-1.2" / "report.md").read_text()
    # the memory written by this run is offered to the next one
    from backend.superagent import memories_for
    from backend.db import SessionLocal, Project

    with SessionLocal() as db:
        owner = db.get(Project, pid).owner_id
    assert any("mean of v is 2.5" in m for m in memories_for(owner, pid))


def test_campaign_hill_climbs_and_catches_cheating(client):
    pid = setup_project(client, "Campaign")
    client.post(f"/api/projects/{pid}/uploads",
                json={"filename": "train.py", "content_base64": base64.b64encode(BASELINE.encode()).decode()})
    c = client.post(f"/api/projects/{pid}/campaigns", json={"budget": {"usd": 5, "model_calls": 50},
                                                            "max_iters": 6, "explore": 0.0,
                                                            "edit_families": {"optimizer": 1}}).json()
    camp = wait(client, f"/api/campaigns/{c['id']}", lambda x: x["status"] not in {"queued", "running"}, timeout=120)
    assert camp["status"] == "completed", camp
    cands = client.get(f"/api/campaigns/{c['id']}/candidates").json()
    assert cands[0]["edit_family"] == "baseline" and cands[0]["score"] == -4.0
    kinds = {x["failure_kind"] for x in cands if x["status"] == "invalid"}
    assert {"leakage", "syntax"} <= kinds
    assert camp["best_score"] > -4.0 and camp["failure_counts"]["leakage"] == 1
    board = client.get(f"/api/campaigns/{c['id']}/leaderboard").json()
    assert board[0]["score"] == camp["best_score"]
    front = client.get(f"/api/campaigns/{c['id']}/frontier").json()
    assert front and any(not p["dominated"] for p in front)
    best = camp["best_candidate_id"]
    trace = client.get(f"/api/candidates/{best}/trace").json()
    assert [s["phase"] for s in trace["steps"]] == ["inspect", "hypothesize", "edit", "evaluate"]
    assert client.get(f"/api/candidates/{best}/trace/file", params={"path": "train.py"}).json()["content"].startswith("x =")
    assert client.get(f"/api/candidates/{best}/trace/file", params={"path": "../../../.secret_key"}).status_code == 404
    audit = client.get(f"/api/candidates/{best}/audit").json()
    assert all(ch["ok"] for ch in audit["checks"])
    promoted = client.post(f"/api/candidates/{best}/promote", json={"notes": "verified"}).json()
    assert promoted["audit"]["decision"] == "promoted"
    cheat = next(x for x in cands if x["failure_kind"] == "leakage")
    assert cheat["score"] is None and cheat["metrics"]["reported_score"] == 0.0
    assert all(row["status"] != "invalid" for row in board if row["score"] is not None)
    assert client.post(f"/api/candidates/{cheat['id']}/promote", json={}).status_code == 409
    assert client.get("/api/me/usage").json()["monthly_spend_usd"] > 0
