"""API endpoint tests using TestClient with mocked auth.

These tests verify routing, request validation, and response shapes
without requiring a real database.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.security import SecurityScopes
from fastapi.testclient import TestClient

from app.core.auth import check_scopes, get_web_user
from app.main import app
from app.models.database import get_db
from app.models.fa_case import FAUser


def _make_mock_user(scopes: list[str] | None = None) -> FAUser:
    """Create a mock FAUser with jwt_scopes attached."""
    user = MagicMock(spec=FAUser)
    user.id = 1
    user.employee_name = "test"
    user.org_id = "test"
    user.jwt_scopes = scopes or ["read", "write", "admin"]
    return user


def _override_auth(scopes: list[str] | None = None):
    """Return a dependency override for get_web_user with scope enforcement."""
    user_scopes = scopes or ["read", "write", "admin"]
    mock_user = _make_mock_user(user_scopes)

    async def override(security_scopes: SecurityScopes = SecurityScopes()):
        check_scopes(security_scopes.scopes, user_scopes)
        return mock_user

    return override


def _override_db():
    """Return a dependency override that yields a mock async session."""
    mock_session = AsyncMock()
    mock_result = MagicMock()
    mock_result.scalar_one_or_none.return_value = None
    mock_session.execute = AsyncMock(return_value=mock_result)

    async def override():
        yield mock_session

    return override


@pytest.fixture
def client():
    """TestClient with auth bypassed (full scopes)."""
    app.dependency_overrides[get_web_user] = _override_auth()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.pop(get_web_user, None)


class TestHealthEndpoint:
    """Health check requires no auth and no DB."""

    def test_health_returns_ok(self):
        with TestClient(app) as c:
            resp = c.get("/health")
            assert resp.status_code == 200
            assert resp.json() == {"status": "ok"}


class TestCasesEndpointValidation:
    """Test that query parameter validation works on /api/cases."""

    def test_page_must_be_positive(self, client):
        app.dependency_overrides[get_db] = _override_db()
        try:
            resp = client.get("/api/cases?page=0")
            assert resp.status_code == 422  # validation error
        finally:
            app.dependency_overrides.pop(get_db, None)

    def test_page_size_max_100(self, client):
        app.dependency_overrides[get_db] = _override_db()
        try:
            resp = client.get("/api/cases?page_size=200")
            assert resp.status_code == 422
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestDeleteCaseEndpoint:
    """Test DELETE /api/cases/{case_id} routing."""

    def test_delete_nonexistent_case(self, client):
        """Deleting a non-existent case should 404."""
        app.dependency_overrides[get_db] = _override_db()
        try:
            resp = client.delete("/api/cases/99999")
            assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_db, None)


class TestScopeEnforcement:
    """Test that scope checks actually block unauthorized access."""

    def test_write_endpoint_rejects_read_only_user(self):
        """A user with only 'read' scope should get 403 on write endpoints."""
        app.dependency_overrides[get_web_user] = _override_auth(["read"])
        app.dependency_overrides[get_db] = _override_db()
        try:
            with TestClient(app) as c:
                resp = c.delete("/api/cases/1")
                assert resp.status_code == 403
                assert "write" in resp.json()["detail"]
        finally:
            app.dependency_overrides.pop(get_web_user, None)
            app.dependency_overrides.pop(get_db, None)

    def test_read_endpoint_allows_read_only_user(self):
        """A user with 'read' scope should be able to access read endpoints."""
        app.dependency_overrides[get_web_user] = _override_auth(["read"])
        app.dependency_overrides[get_db] = _override_db()
        try:
            with TestClient(app) as c:
                resp = c.get("/api/cases/1")
                # Should get 404 (case not found), NOT 401/403
                assert resp.status_code == 404
        finally:
            app.dependency_overrides.pop(get_web_user, None)
            app.dependency_overrides.pop(get_db, None)
