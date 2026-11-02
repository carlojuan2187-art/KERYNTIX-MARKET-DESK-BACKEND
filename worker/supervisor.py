"""Run the eight KERYNTIX MARKET DESK background workers in one deployable worker service.

Each child remains an independent process and owns its own Redis lease. This keeps
failure isolation while avoiding the need to create one Railway service per worker.
The Telegram authentication polling bot runs separately via worker.telegram_auth_bot.
"""
from __future__ import annotations

from api.logging_config import configure_logging

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:  # pragma: no cover - process managers can inject environment
    pass

logger = logging.getLogger("ftrade.worker_supervisor")
WORKER_MODULES = (
    "worker.market_feed",
    "worker.signal_scanner",
    "worker.signal_lifecycle",
    "worker.plan_scheduler",
    "worker.user_alerts",
    "worker.business_reconciler",
    "worker.telegram_notification_delivery",
    "worker.web_push_delivery",
)


def _bounded_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


@dataclass
class ChildProcess:
    module: str
    process: Any | None = None
    failures: int = 0
    restart_at: float = 0.0


class WorkerSupervisor:
    def __init__(self, *, popen_factory: Callable[..., Any] | None = None,
                 poll_seconds: float | None = None, restart_base_seconds: float | None = None,
                 restart_max_seconds: float | None = None, shutdown_grace_seconds: float | None = None) -> None:
        self.popen_factory = popen_factory or subprocess.Popen
        self.poll_seconds = poll_seconds if poll_seconds is not None else _bounded_float(
            "WORKER_SUPERVISOR_POLL_SECONDS", 1.0, minimum=0.2, maximum=10.0
        )
        self.restart_base_seconds = restart_base_seconds if restart_base_seconds is not None else _bounded_float(
            "WORKER_RESTART_BASE_SECONDS", 2.0, minimum=0.5, maximum=30.0
        )
        self.restart_max_seconds = restart_max_seconds if restart_max_seconds is not None else _bounded_float(
            "WORKER_RESTART_MAX_SECONDS", 60.0, minimum=2.0, maximum=600.0
        )
        self.shutdown_grace_seconds = shutdown_grace_seconds if shutdown_grace_seconds is not None else _bounded_float(
            "WORKER_SHUTDOWN_GRACE_SECONDS", 40.0, minimum=5.0, maximum=120.0
        )
        self.backend_root = Path(__file__).resolve().parents[1]
        self.children = {module: ChildProcess(module=module) for module in WORKER_MODULES}

    def _spawn(self, child: ChildProcess) -> bool:
        env = os.environ.copy()
        existing = env.get("PYTHONPATH", "")
        paths = [part for part in existing.split(os.pathsep) if part]
        root = str(self.backend_root)
        if root not in paths:
            paths.insert(0, root)
        env["PYTHONPATH"] = os.pathsep.join(paths)
        command = [sys.executable, "-m", child.module]
        try:
            child.process = self.popen_factory(command, cwd=root, env=env)
            child.restart_at = 0.0
            logger.info("Worker iniciado: %s (pid=%s)", child.module, getattr(child.process, "pid", "unknown"))
            return True
        except Exception:
            child.process = None
            child.failures += 1
            delay = min(self.restart_max_seconds, self.restart_base_seconds * (2 ** min(child.failures - 1, 8)))
            child.restart_at = time.monotonic() + delay
            logger.exception("No se pudo iniciar %s; reintento en %.1fs", child.module, delay)
            return False

    def run(self, *, stop_event: threading.Event | None = None) -> None:
        stop = stop_event or threading.Event()
        logger.info("Supervisor iniciando %d workers", len(self.children))
        try:
            for child in self.children.values():
                self._spawn(child)
            while not stop.wait(self.poll_seconds):
                now = time.monotonic()
                for child in self.children.values():
                    if child.process is not None:
                        exit_code = child.process.poll()
                        if exit_code is None:
                            continue
                        child.process = None
                        child.failures += 1
                        delay = min(self.restart_max_seconds, self.restart_base_seconds * (2 ** min(child.failures - 1, 8)))
                        child.restart_at = now + delay
                        logger.error("Worker %s terminó (exit=%s); reintento en %.1fs", child.module, exit_code, delay)
                    elif now >= child.restart_at:
                        self._spawn(child)
        finally:
            self._stop_children()
            logger.info("Supervisor detenido")

    def _stop_children(self) -> None:
        running = [child for child in self.children.values() if child.process is not None]
        for child in running:
            try:
                if child.process.poll() is None:
                    child.process.terminate()
            except Exception:
                logger.exception("No se pudo solicitar el cierre de %s", child.module)
        deadline = time.monotonic() + self.shutdown_grace_seconds
        for child in running:
            process = child.process
            if process is None:
                continue
            remaining = max(0.1, deadline - time.monotonic())
            try:
                process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.error("Worker %s no terminó a tiempo; se fuerza el cierre", child.module)
                try:
                    process.kill()
                    process.wait(timeout=2.0)
                except Exception:
                    logger.exception("No se pudo forzar el cierre de %s", child.module)
            except Exception:
                logger.exception("Error esperando el cierre de %s", child.module)
            child.process = None


def main() -> None:
    configure_logging()
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda _signum, _frame: stop.set())
        except (ValueError, OSError):  # pragma: no cover - non-main thread/platform
            pass
    WorkerSupervisor().run(stop_event=stop)


if __name__ == "__main__":
    main()
