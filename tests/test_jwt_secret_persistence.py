"""开发态 JWT secret 持久化测试（skillos/api/main.py::_dev_jwt_secret）。

目标：`skillos serve` 不再要求每次设 SKILLOS_JWT_SECRET —— env 未设时
首启生成 `<home>/.skillos/jwt_secret` 并复用，重启后 token 不失效。
沙盒/测试一律经 SKILLOS_ROOT 指向 tmp_path，不污染真实 ~/.skillos。
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from skillos.api.main import create_app
from skillos.api.security import verify_jwt

SECRET_PATH_PARTS = (".skillos", "jwt_secret")


def _make_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLOS_ROOT", str(tmp_path))
    monkeypatch.delenv("SKILLOS_JWT_SECRET", raising=False)
    app = create_app(db_url=f"sqlite:///{tmp_path / 'cloud.db'}")
    return TestClient(app)


def _secret_path(tmp_path):
    return tmp_path.joinpath(*SECRET_PATH_PARTS)


class TestDevJwtSecret:
    def test_first_start_creates_file_and_reuses(self, tmp_path, monkeypatch):
        """两次启动（进程重启模拟）拿到同一 secret：文件生成一次，之后复用。"""
        with _make_client(tmp_path, monkeypatch) as c1:
            r = c1.post("/api/auth/register", json={
                "email": "a@example.com", "password": "password123",
            })
            assert r.status_code == 200
            token1 = c1.post("/api/auth/login", json={
                "email": "a@example.com", "password": "password123",
            }).json()["data"]["token"]

        # 第二个"进程"：只共享 home，不共享内存
        with _make_client(tmp_path, monkeypatch) as c2:
            token2 = c2.post("/api/auth/login", json={
                "email": "a@example.com", "password": "password123",
            }).json()["data"]["token"]
            # 第一次进程签发的 token 在重启后仍有效（旧实现必然 401）
            assert c2.get("/api/auth/me",
                          headers={"Authorization": f"Bearer {token1}"}).status_code == 200
            assert verify_jwt(token2, _secret_path(tmp_path).read_text(
                encoding="utf-8").strip()) is not None

    def test_existing_file_value_is_used(self, tmp_path, monkeypatch):
        """已有文件 → 原样采用，不覆盖。"""
        path = _secret_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixed-dev-secret\n", encoding="utf-8")
        with _make_client(tmp_path, monkeypatch) as c:
            r = c.post("/api/auth/register", json={
                "email": "b@example.com", "password": "password123",
            })
            assert r.status_code == 200
            token = c.post("/api/auth/login", json={
                "email": "b@example.com", "password": "password123",
            }).json()["data"]["token"]
            assert verify_jwt(token, "fixed-dev-secret") is not None
        assert path.read_text(encoding="utf-8") == "fixed-dev-secret\n"

    def test_blank_file_regenerates(self, tmp_path, monkeypatch):
        """文件存在但内容为空（坏文件）→ 重新生成，不把空串当 secret。"""
        path = _secret_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("   \n", encoding="utf-8")
        with _make_client(tmp_path, monkeypatch):
            pass
        assert path.read_text(encoding="utf-8").strip() != ""

    def test_unwritable_location_falls_back_to_random(self, tmp_path, monkeypatch):
        """持久化失败 → 退回进程随机值：服务照常可用，不阻塞启动。"""
        path = _secret_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 同名目录挡住 write_text（IsADirectoryError ⊂ OSError）
        path.mkdir()
        with _make_client(tmp_path, monkeypatch) as c:
            assert c.get("/api/health").status_code == 200
