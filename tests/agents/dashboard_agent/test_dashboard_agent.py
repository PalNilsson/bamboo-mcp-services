"""Tests for the dashboard agent."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from starlette.testclient import TestClient

from bamboo_mcp_services.agents.base import AgentState
from bamboo_mcp_services.agents.dashboard_agent.agent import (
    DashboardAgent,
    DashboardConfig,
    build_app,
)


def _make_config(**kwargs) -> DashboardConfig:
    defaults = {"jobs_db": "jobs.duckdb", "cric_db": "cric.db"}
    defaults.update(kwargs)
    return DashboardConfig(**defaults)


# ── DashboardConfig ───────────────────────────────────────────────────────────

class TestDashboardConfig:
    def test_defaults(self):
        cfg = DashboardConfig(jobs_db="j.db", cric_db="c.db")
        assert cfg.host == "0.0.0.0"
        assert cfg.port == 8080
        assert cfg.refresh_interval_s == 30

    def test_custom_values(self):
        cfg = DashboardConfig(jobs_db="j.db", cric_db="c.db", host="127.0.0.1", port=9000, refresh_interval_s=60)
        assert cfg.host == "127.0.0.1"
        assert cfg.port == 9000
        assert cfg.refresh_interval_s == 60


# ── DashboardAgent lifecycle ──────────────────────────────────────────────────

class TestDashboardAgent:
    def test_requires_config(self):
        with pytest.raises(ValueError, match="DashboardConfig must be provided"):
            DashboardAgent(config=None)

    def test_initial_state(self):
        agent = DashboardAgent(config=_make_config())
        assert agent.state == AgentState.NEW

    def test_health_details_before_start(self):
        agent = DashboardAgent(config=_make_config())
        details = agent._health_details()
        assert details["server_alive"] is False
        assert details["port"] == 8080
        assert "url" in details
        assert "jobs_db" in details
        assert "cric_db" in details

    def test_start_transitions_to_running(self):
        agent = DashboardAgent(config=_make_config(port=18080))
        mock_server = MagicMock()
        mock_server.run = MagicMock()

        with patch("bamboo_mcp_services.agents.dashboard_agent.agent.uvicorn.Server", return_value=mock_server):
            with patch("bamboo_mcp_services.agents.dashboard_agent.agent.build_app", return_value=MagicMock()):
                agent.start()

        assert agent.state == AgentState.RUNNING
        agent._server = mock_server
        agent._server.should_exit = True

    def test_stop_sets_should_exit(self):
        agent = DashboardAgent(config=_make_config(port=18081))
        mock_server = MagicMock()
        mock_server.run = MagicMock()
        mock_server.should_exit = False

        with patch("bamboo_mcp_services.agents.dashboard_agent.agent.uvicorn.Server", return_value=mock_server):
            with patch("bamboo_mcp_services.agents.dashboard_agent.agent.build_app", return_value=MagicMock()):
                agent.start()
                agent.stop()

        assert agent.state == AgentState.STOPPED
        assert mock_server.should_exit is True

    def test_tick_raises_when_thread_none(self):
        agent = DashboardAgent(config=_make_config())
        agent._state = AgentState.RUNNING
        assert agent._thread is None
        with pytest.raises(RuntimeError, match="Dashboard HTTP server thread died"):
            agent._tick_impl()

    def test_tick_raises_when_thread_dead(self):
        agent = DashboardAgent(config=_make_config())
        agent._state = AgentState.RUNNING
        dead_thread = MagicMock()
        dead_thread.is_alive.return_value = False
        agent._thread = dead_thread
        with pytest.raises(RuntimeError, match="Dashboard HTTP server thread died"):
            agent._tick_impl()

    def test_tick_ok_when_thread_alive(self):
        agent = DashboardAgent(config=_make_config())
        agent._state = AgentState.RUNNING
        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        agent._thread = alive_thread
        agent._tick_impl()  # must not raise

    def test_health_url_format(self):
        agent = DashboardAgent(config=DashboardConfig(jobs_db="j.db", cric_db="c.db", host="127.0.0.1", port=9999))
        details = agent._health_details()
        assert details["url"] == "http://127.0.0.1:9999"


# ── build_app / API endpoints ─────────────────────────────────────────────────

class TestBuildApp:
    def _make_app(self, **cfg_kwargs):
        return build_app(_make_config(**cfg_kwargs))

    def test_index_injects_refresh_interval(self, tmp_path):
        static_dir = tmp_path / "static"
        static_dir.mkdir()
        (static_dir / "index.html").write_text("<html>__REFRESH_INTERVAL__</html>", encoding="utf-8")

        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._STATIC_DIR", static_dir):
            app = build_app(_make_config(refresh_interval_s=60))

        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "60" in resp.text
        assert "__REFRESH_INTERVAL__" not in resp.text

    def test_api_config_returns_paths(self):
        app = build_app(_make_config(jobs_db="my_jobs.db", cric_db="my_cric.db", refresh_interval_s=45))
        resp = TestClient(app).get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs_db"] == "my_jobs.db"
        assert data["cric_db"] == "my_cric.db"
        assert data["refresh_interval_s"] == 45

    def test_api_status_both_unavailable(self):
        app = self._make_app(jobs_db="nonexistent.db", cric_db="nonexistent2.db")
        resp = TestClient(app).get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs_db"]["ok"] is False
        assert data["cric_db"]["ok"] is False

    def test_api_status_success(self):
        def mock_query(db_path, sql):
            if "jobs" in db_path:
                return [("2026-05-09T12:00:00+00:00",)]
            return [(712,)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/status")

        assert resp.status_code == 200
        data = resp.json()
        assert data["jobs_db"]["ok"] is True
        assert data["cric_db"]["ok"] is True
        assert data["cric_db"]["queue_count"] == 712

    def test_api_jobs_summary_success(self):
        call_count = [0]

        def mock_query(db_path, sql):
            call_count[0] += 1
            if "jobstatus" in sql:
                return [("finished", 1000), ("running", 500), ("failed", 100)]
            return [("2026-05-09T12:00:00+00:00",)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/jobs/summary")

        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1600
        assert len(data["by_status"]) == 3
        assert data["by_status"][0]["status"] == "finished"
        assert data["last_fetched_utc"] is not None

    def test_api_jobs_summary_db_error(self):
        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=Exception("DB gone")):
            resp = TestClient(app).get("/api/jobs/summary")
        assert resp.status_code == 503
        assert "error" in resp.json()

    def test_api_jobs_by_queue(self):
        def mock_query(db_path, sql):
            return [("ANALY_BNL", 500), ("ANALY_CERN", 300)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/jobs/by_queue")

        assert resp.status_code == 200
        data = resp.json()
        assert data["queues"][0]["queue"] == "ANALY_BNL"
        assert data["queues"][0]["count"] == 500

    def test_api_jobs_by_queue_db_error(self):
        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=Exception("err")):
            resp = TestClient(app).get("/api/jobs/by_queue")
        assert resp.status_code == 503

    def test_api_errors_success(self):
        def mock_query(db_path, sql):
            return [("pilot", "ERR_TOEXPIRED", 1232, "exceeded time limit", 500)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/errors")

        assert resp.status_code == 200
        data = resp.json()
        assert data["errors"][0]["codename"] == "ERR_TOEXPIRED"
        assert data["errors"][0]["count"] == 500

    def test_api_errors_none_count(self):
        def mock_query(db_path, sql):
            return [("pilot", "ERR_UNKNOWN", 0, "unknown error", None)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/errors")

        assert resp.status_code == 200
        assert resp.json()["errors"][0]["count"] == 0

    def test_api_errors_db_error(self):
        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=Exception("err")):
            resp = TestClient(app).get("/api/errors")
        assert resp.status_code == 503

    def test_api_queues_success(self):
        call_count = [0]

        def mock_query(db_path, sql):
            call_count[0] += 1
            if call_count[0] == 1:
                return [("online", 600), ("offline", 100)]
            return [("US", 200), ("EU", 150), ("WORLD", 50)]

        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=mock_query):
            resp = TestClient(app).get("/api/queues")

        assert resp.status_code == 200
        data = resp.json()
        assert data["by_status"][0]["status"] == "online"
        assert data["by_status"][0]["count"] == 600
        assert data["by_cloud"][0]["cloud"] == "US"

    def test_api_queues_db_error(self):
        app = self._make_app()
        with patch("bamboo_mcp_services.agents.dashboard_agent.agent._query", side_effect=Exception("err")):
            resp = TestClient(app).get("/api/queues")
        assert resp.status_code == 503
