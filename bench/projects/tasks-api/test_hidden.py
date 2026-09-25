import pytest
from app import create_app


@pytest.fixture
def client(tmp_path):
    return create_app(str(tmp_path / "t.db")).test_client()

def test_crud(client):
    r = client.post("/tasks", json={"title": "Купи хляб"})
    assert r.status_code == 201
    t = r.get_json()
    assert t["title"] == "Купи хляб" and t["done"] is False and "id" in t
    assert client.get("/tasks").get_json() == [t]
    r = client.patch(f"/tasks/{t['id']}", json={"done": True})
    assert r.status_code == 200 and r.get_json()["done"] is True
    assert client.delete(f"/tasks/{t['id']}").status_code == 204
    assert client.get("/tasks").get_json() == []

@pytest.mark.parametrize("body", [{}, {"title": ""}, None])
def test_bad_create(client, body):
    r = client.post("/tasks", json=body) if body is not None else client.post("/tasks", data="x", content_type="text/plain")
    assert r.status_code == 400

def test_missing(client):
    assert client.patch("/tasks/999", json={"done": True}).status_code == 404
    assert client.delete("/tasks/999").status_code == 404

def test_persists(tmp_path):
    db = str(tmp_path / "p.db")
    c1 = create_app(db).test_client()
    c1.post("/tasks", json={"title": "A"})
    c1.post("/tasks", json={"title": "Б"})
    titles = [t["title"] for t in create_app(db).test_client().get("/tasks").get_json()]
    assert titles == ["A", "Б"]
