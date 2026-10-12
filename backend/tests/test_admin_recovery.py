from app.config import settings


def test_admin_recovery_is_disabled_by_default(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_recovery_token", "")
    response = client.post("/auth/admin-recovery", json={"token": "t" * 40, "new_password": "New-Admin-Password-2026!"})
    assert response.status_code == 404


def test_admin_recovery_changes_only_admin_and_can_run_once(client, monkeypatch):
    recovery_token = "recovery-token-for-test-only-1234567890"
    new_password = "New-Admin-Password-2026!"
    monkeypatch.setattr(settings, "admin_recovery_token", recovery_token)

    response = client.post("/auth/admin-recovery", json={"token": recovery_token, "new_password": new_password})
    assert response.status_code == 200
    admin_login = client.post("/auth/login", json={"username": "admin", "password": new_password})
    assert admin_login.status_code == 200
    manager_login = client.post("/auth/login", json={"username": "manager", "password": "ChangeMe-2026!"})
    assert manager_login.status_code == 200

    repeated = client.post("/auth/admin-recovery", json={
        "token": recovery_token, "new_password": "Another-Admin-Password-2026!",
    })
    assert repeated.status_code == 410
    audit = client.get("/audit-logs/verify", headers={"Authorization": f"Bearer {admin_login.json()['access_token']}"})
    assert audit.status_code == 200
    assert audit.json()["valid"] is True


def test_admin_recovery_rejects_wrong_token_without_changing_password(client, monkeypatch):
    monkeypatch.setattr(settings, "admin_recovery_token", "recovery-token-for-test-only-1234567890")
    response = client.post("/auth/admin-recovery", json={
        "token": "wrong-token-123456789012345678901234", "new_password": "New-Admin-Password-2026!",
    })
    assert response.status_code == 401
    old_login = client.post("/auth/login", json={"username": "admin", "password": "ChangeMe-2026!"})
    assert old_login.status_code == 200
