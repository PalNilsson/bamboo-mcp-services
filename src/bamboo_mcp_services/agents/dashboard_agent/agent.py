"""HTTP dashboard agent for Bamboo MCP Services.

Starts a FastAPI/uvicorn HTTP server in a background thread that exposes
REST endpoints for job metrics, queue status, and error summaries, and
serves a single-page dark-themed monitoring dashboard.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

import duckdb
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from bamboo_mcp_services.agents.base import Agent

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


@dataclass
class DashboardConfig:
    """Configuration for DashboardAgent.

    Attributes:
        jobs_db: Path to jobs.duckdb.
        cric_db: Path to cric.db or cric.duckdb.
        host: HTTP server bind address.
        port: HTTP server port.
        refresh_interval_s: Suggested client auto-refresh interval in seconds.
    """

    jobs_db: str
    cric_db: str
    host: str = "0.0.0.0"
    port: int = 8080
    refresh_interval_s: int = 30


def _query(db_path: str, sql: str) -> list:
    """Execute a read-only DuckDB query and return all rows.

    Args:
        db_path: Path to the DuckDB database file.
        sql: SQL query to execute.

    Returns:
        List of result rows as tuples.

    Raises:
        duckdb.Error: If the connection or query fails.
    """
    conn = duckdb.connect(db_path, read_only=True)
    try:
        return conn.execute(sql).fetchall()
    finally:
        conn.close()


def _add_static_routes(app: FastAPI, config: DashboardConfig) -> None:
    """Register the HTML and config API routes.

    Args:
        app: FastAPI application instance.
        config: Dashboard configuration.
    """
    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (_STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return HTMLResponse(
            content=html.replace("__REFRESH_INTERVAL__", str(config.refresh_interval_s))
        )

    @app.get("/api/config")
    def api_config():
        return {
            "jobs_db": config.jobs_db,
            "cric_db": config.cric_db,
            "refresh_interval_s": config.refresh_interval_s,
        }


def _add_status_routes(app: FastAPI, config: DashboardConfig) -> None:
    """Register the /api/status route that checks both databases.

    Args:
        app: FastAPI application instance.
        config: Dashboard configuration.
    """
    @app.get("/api/status")
    def api_status():
        result: dict[str, Any] = {}
        try:
            rows = _query(config.jobs_db, "SELECT MAX(_fetched_utc) FROM jobs")
            result["jobs_db"] = {
                "ok": True,
                "last_fetched_utc": str(rows[0][0]) if rows and rows[0][0] else None,
            }
        except Exception as exc:
            result["jobs_db"] = {"ok": False, "error": str(exc)}
        try:
            rows = _query(config.cric_db, "SELECT COUNT(*) FROM queuedata")
            result["cric_db"] = {"ok": True, "queue_count": rows[0][0] if rows else 0}
        except Exception as exc:
            result["cric_db"] = {"ok": False, "error": str(exc)}
        return result


def _add_job_routes(app: FastAPI, config: DashboardConfig) -> None:
    """Register /api/jobs/* routes for job metrics.

    Args:
        app: FastAPI application instance.
        config: Dashboard configuration.
    """
    @app.get("/api/jobs/summary")
    def api_jobs_summary():
        try:
            status_rows = _query(
                config.jobs_db,
                "SELECT jobstatus, COUNT(*) AS n FROM jobs GROUP BY jobstatus ORDER BY n DESC",
            )
            fetch_rows = _query(config.jobs_db, "SELECT MAX(_fetched_utc) FROM jobs")
            total = sum(r[1] for r in status_rows)
            return {
                "total": total,
                "by_status": [{"status": r[0], "count": r[1]} for r in status_rows],
                "last_fetched_utc": (
                    str(fetch_rows[0][0]) if fetch_rows and fetch_rows[0][0] else None
                ),
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)

    @app.get("/api/jobs/by_queue")
    def api_jobs_by_queue():
        try:
            rows = _query(
                config.jobs_db,
                "SELECT _queue, COUNT(*) AS n FROM jobs WHERE _queue IS NOT NULL"
                " GROUP BY _queue ORDER BY n DESC LIMIT 20",
            )
            return {"queues": [{"queue": r[0], "count": r[1]} for r in rows]}
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)


def _add_error_routes(app: FastAPI, config: DashboardConfig) -> None:
    """Register the /api/errors route.

    Args:
        app: FastAPI application instance.
        config: Dashboard configuration.
    """
    @app.get("/api/errors")
    def api_errors():
        try:
            rows = _query(
                config.jobs_db,
                """
                SELECT error, codename, codeval, diag, SUM(count) AS total
                FROM errors_by_count
                GROUP BY error, codename, codeval, diag
                ORDER BY total DESC
                LIMIT 25
                """,
            )
            return {
                "errors": [
                    {
                        "error": r[0],
                        "codename": r[1],
                        "codeval": r[2],
                        "diag": r[3],
                        "count": int(r[4]) if r[4] is not None else 0,
                    }
                    for r in rows
                ]
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)


def _add_queue_routes(app: FastAPI, config: DashboardConfig) -> None:
    """Register the /api/queues route for CRIC queue metadata.

    Args:
        app: FastAPI application instance.
        config: Dashboard configuration.
    """
    @app.get("/api/queues")
    def api_queues():
        try:
            status_rows = _query(
                config.cric_db,
                "SELECT status, COUNT(*) AS n FROM queuedata GROUP BY status ORDER BY n DESC",
            )
            cloud_rows = _query(
                config.cric_db,
                "SELECT cloud, COUNT(*) AS n FROM queuedata"
                " WHERE cloud IS NOT NULL GROUP BY cloud ORDER BY n DESC LIMIT 15",
            )
            return {
                "by_status": [{"status": r[0], "count": r[1]} for r in status_rows],
                "by_cloud": [{"cloud": r[0], "count": r[1]} for r in cloud_rows],
            }
        except Exception as exc:
            return JSONResponse({"error": str(exc)}, status_code=503)


def build_app(config: DashboardConfig) -> FastAPI:
    """Build the FastAPI application for the dashboard.

    Args:
        config: Dashboard configuration.

    Returns:
        Configured FastAPI application with all routes registered.
    """
    app = FastAPI(title="Bamboo Dashboard", docs_url=None, redoc_url=None)
    _add_static_routes(app, config)
    _add_status_routes(app, config)
    _add_job_routes(app, config)
    _add_error_routes(app, config)
    _add_queue_routes(app, config)
    return app


class DashboardAgent(Agent):
    """Agent that serves a web dashboard for Bamboo MCP Services.

    Starts a FastAPI/uvicorn HTTP server in a background daemon thread.
    The dashboard auto-refreshes job metrics, queue status, and error
    summaries from the DuckDB databases written by the other agents.
    """

    def __init__(
        self,
        name: str = "dashboard-agent",
        config: Optional[DashboardConfig] = None,
    ) -> None:
        """Initialise the dashboard agent.

        Args:
            name: Agent name.
            config: Dashboard configuration. Required.

        Raises:
            ValueError: If config is None.
        """
        super().__init__(name=name)
        if config is None:
            raise ValueError("DashboardConfig must be provided")
        self.config = config
        self._server: Optional[uvicorn.Server] = None
        self._thread: Optional[threading.Thread] = None

    def _start_impl(self) -> None:
        """Start the uvicorn HTTP server in a background daemon thread."""
        app = build_app(self.config)
        uv_config = uvicorn.Config(
            app,
            host=self.config.host,
            port=self.config.port,
            log_level="warning",
            access_log=False,
        )
        self._server = uvicorn.Server(uv_config)
        self._thread = threading.Thread(
            target=self._server.run,
            daemon=True,
            name="dashboard-uvicorn",
        )
        self._thread.start()
        logger.info(
            "Dashboard server starting on http://%s:%d",
            self.config.host,
            self.config.port,
        )

    def _tick_impl(self) -> None:
        """Verify the HTTP server thread is still alive."""
        if self._thread is None or not self._thread.is_alive():
            raise RuntimeError("Dashboard HTTP server thread died unexpectedly")

    def _stop_impl(self) -> None:
        """Signal the uvicorn server to exit and wait for the thread."""
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        logger.info("Dashboard agent stopped")

    def _health_details(self) -> Mapping[str, Any]:
        """Return dashboard-specific health metrics.

        Returns:
            Dictionary with host, port, URL, and server thread liveness.
        """
        alive = self._thread is not None and self._thread.is_alive()
        return {
            "host": self.config.host,
            "port": self.config.port,
            "url": f"http://{self.config.host}:{self.config.port}",
            "server_alive": alive,
            "jobs_db": self.config.jobs_db,
            "cric_db": self.config.cric_db,
        }
