import os
import sys
from pathlib import Path

from fastapi.testclient import TestClient
import pytest


BACKEND_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIR))
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["JWT_SECRET_KEY"] = "inkwell-test-secret"
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["CORS_ORIGINS"] = "http://localhost:5173"

from app import main
from app.db import Base, engine
from app.rate_limit import auth_rate_limiter, user_rate_limiter


@pytest.fixture
def client():
    Base.metadata.drop_all(engine)
    auth_rate_limiter._requests.clear()
    user_rate_limiter._requests.clear()
    with TestClient(main.app) as test_client:
        yield test_client
    Base.metadata.drop_all(engine)


def register(client: TestClient, email: str = "writer@example.com") -> str:
    response = client.post(
        "/auth/register",
        json={"email": email, "password": "secure-password"},
    )
    assert response.status_code == 201
    return response.json()["access_token"]


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
