from __future__ import annotations

import threading
import time

from worker.supervisor import WORKER_MODULES, WorkerSupervisor


class FakeProcess:
    def __init__(self, command):
        self.command = command
        self.returncode = None
        self.pid = len(command)
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode or 0


def test_supervisor_starts_each_logical_worker_and_stops_them_on_signal():
    spawned = []

    def fake_popen(command, *, cwd, env):
        process = FakeProcess(command)
        spawned.append((command, cwd, env, process))
        return process

    supervisor = WorkerSupervisor(
        popen_factory=fake_popen, poll_seconds=0.01,
        restart_base_seconds=0.5, restart_max_seconds=1.0, shutdown_grace_seconds=1.0,
    )
    stop = threading.Event()
    thread = threading.Thread(target=supervisor.run, kwargs={"stop_event": stop})
    thread.start()
    deadline = time.monotonic() + 2
    while len(spawned) < len(WORKER_MODULES) and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(timeout=2)

    assert not thread.is_alive()
    assert len(spawned) == len(WORKER_MODULES)
    assert {item[0][-1] for item in spawned} == set(WORKER_MODULES)
    assert all(item[3].terminated for item in spawned)
    assert all(item[1] == str(supervisor.backend_root) for item in spawned)


def test_supervisor_restart_delay_grows_after_repeated_exits():
    supervisor = WorkerSupervisor(restart_base_seconds=2, restart_max_seconds=10)
    assert min(supervisor.restart_max_seconds, supervisor.restart_base_seconds * (2 ** 0)) == 2
    assert min(supervisor.restart_max_seconds, supervisor.restart_base_seconds * (2 ** 1)) == 4
    assert min(supervisor.restart_max_seconds, supervisor.restart_base_seconds * (2 ** 6)) == 10
