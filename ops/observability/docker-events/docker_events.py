#!/usr/bin/env python3
"""Docker events sidecar: container oom/die/restart/kill -> one JSON line per event in stdout.

Reads the Docker Engine API stream ``GET /events`` through docker-socket-proxy (read-only,
``EVENTS=1``; no docker.sock here). Alloy collects the stdout like any other container log,
so the events land in Loki as ``{service="docker-events"}`` with the label ``action``.

Resilience: a broken stream or an unreachable proxy is logged (JSON, stderr) and retried with
an exponential pause up to 30 s; the process never exits on its own. An idle stream is
re-opened every ``READ_TIMEOUT`` seconds with ``since`` (duplicates are skipped by
``timeNano``), and each successful connection touches the heartbeat file used by the
docker healthcheck. Standard library only.
"""

from __future__ import annotations

import http.client
import json
import os
import signal
import sys
import time
import urllib.parse
from collections import OrderedDict
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ACTIONS = ("oom", "die", "restart", "kill")
MAX_DELAY = 30.0
READ_TIMEOUT = float(os.environ.get("DOCKER_EVENTS_READ_TIMEOUT", "60"))
HEARTBEAT = os.environ.get("DOCKER_EVENTS_HEARTBEAT", "/tmp/docker-events.alive")  # noqa: S108
DOCKER_URL = os.environ.get("DOCKER_EVENTS_URL", "http://docker-socket-proxy:2375")
LOGGER = "docker_events"
# die 137 classification: recent kill/oom per container id (event time, not wall clock).
CAUSE_WINDOW_NS = 60 * 1_000_000_000
CAUSE_MAX_CONTAINERS = 512


def events_path(since: int | None = None) -> str:
    """``/events`` query: containers only, the four actions, optional ``since`` (unix s)."""
    query = {"filters": json.dumps({"type": ["container"], "event": list(ACTIONS)})}
    if since is not None:
        query["since"] = str(since)
    return "/events?" + urllib.parse.urlencode(query)


def format_event(event: dict[str, Any]) -> dict[str, Any] | None:
    """Docker event -> output record; ``None`` for anything that is not a watched action."""
    if event.get("Type") != "container":
        return None
    action = str(event.get("Action") or event.get("status") or "")
    if action not in ACTIONS:
        return None
    attrs = (event.get("Actor") or {}).get("Attributes") or {}
    time_nano = int(event.get("timeNano") or int(event.get("time") or 0) * 1_000_000_000)
    record: dict[str, Any] = {
        "ts": datetime.fromtimestamp(time_nano / 1e9, tz=UTC).isoformat(timespec="milliseconds"),
        "level": "info",
        "logger": LOGGER,
        "event": "container",
        "action": action,
        "container": attrs.get("name", ""),
        "compose_service": attrs.get("com.docker.compose.service", ""),
        "compose_project": attrs.get("com.docker.compose.project", ""),
        "image": attrs.get("image", ""),
    }
    if action == "die":
        try:
            record["exit_code"] = int(attrs.get("exitCode", ""))
        except ValueError:
            record["exit_code"] = None
        if record["exit_code"] != 0:
            record["level"] = "warning"
    elif action == "kill":
        record["signal"] = attrs.get("signal", "")
    else:
        if action == "oom":
            record["level"] = "warning"
    return record


class CauseTracker:
    """Explains ``die`` with exit code 137 (SIGKILL) by the preceding events of the container.

    Docker Engine sends ``kill`` (attribute ``signal``) before ``die`` for ``docker stop`` /
    ``restart`` / ``kill`` / ``rm -f``, and ``oom`` (no ``kill``) before ``die`` when the kernel
    OOM killer hit the container. ``cause``: ``oom`` | ``docker_kill`` (+ ``signal``) |
    ``unknown``. Memory is bounded: at most ``max_containers`` ids, oldest dropped first.
    """

    def __init__(
        self, window_ns: int = CAUSE_WINDOW_NS, max_containers: int = CAUSE_MAX_CONTAINERS
    ) -> None:
        self.window_ns, self.max_containers = window_ns, max_containers
        self.recent: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def observe(self, event: dict[str, Any], record: dict[str, Any]) -> None:
        cid = str((event.get("Actor") or {}).get("ID") or event.get("id") or "")
        if not cid:
            return
        time_nano = int(event.get("timeNano") or int(event.get("time") or 0) * 1_000_000_000)
        action = record["action"]
        if action in ("oom", "kill"):
            entry = self.recent.pop(cid, {})
            entry[action] = (time_nano, record.get("signal", ""))
            self.recent[cid] = entry
            while len(self.recent) > self.max_containers:
                self.recent.popitem(last=False)
        elif action == "die":
            entry = self.recent.pop(cid, {})
            if record.get("exit_code") != 137:
                return

            def fresh(key: str) -> tuple[int, str] | None:
                seen = entry.get(key)
                return seen if seen and 0 <= time_nano - seen[0] <= self.window_ns else None

            if fresh("oom"):
                record["cause"] = "oom"
            elif kill := fresh("kill"):
                record["cause"] = "docker_kill"
                record["signal"] = kill[1]
            else:
                record["cause"] = "unknown"


def log_error(msg: str, **fields: Any) -> None:
    line = {"ts": datetime.now(tz=UTC).isoformat(timespec="milliseconds"), "level": "error"}
    line.update(logger=LOGGER, msg=msg, **fields)
    print(json.dumps(line, ensure_ascii=False), file=sys.stderr, flush=True)


def emit_stdout(record: dict[str, Any]) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


def touch_heartbeat() -> None:
    try:
        Path(HEARTBEAT).touch()
    except OSError as exc:  # the healthcheck will report it; keep streaming
        log_error("heartbeat_write_failed", error=str(exc))


def default_connection() -> http.client.HTTPConnection:
    url = urllib.parse.urlsplit(DOCKER_URL)
    return http.client.HTTPConnection(url.hostname or "", url.port or 2375, timeout=READ_TIMEOUT)


class Watcher:
    """Reconnecting reader of the Docker events stream (dependencies injectable for tests)."""

    def __init__(
        self,
        connect: Callable[[], Any] = default_connection,
        emit: Callable[[dict[str, Any]], None] = emit_stdout,
        sleep: Callable[[float], None] = time.sleep,
        beat: Callable[[], None] = touch_heartbeat,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.connect, self.emit, self.sleep, self.beat, self.now = connect, emit, sleep, beat, now
        self.delay = 1.0
        self.last_nano = 0
        self.since: int | None = None  # start of the previous successful connection
        self.causes = CauseTracker()

    def stream_once(self) -> None:
        """One connection: returns on idle timeout, raises on errors / closed stream."""
        conn = self.connect()
        try:
            started = int(self.now())
            conn.request("GET", events_path(self.since))
            resp = conn.getresponse()
            if resp.status != 200:
                raise ConnectionError(f"HTTP {resp.status} from {DOCKER_URL}/events")
            self.since = started
            self.delay = 1.0
            self.beat()
            while True:
                try:
                    line = resp.readline()
                except TimeoutError:
                    return  # idle stream: reconnect with ``since``
                if not line:
                    raise ConnectionError("events stream closed")
                self.handle_line(line)
        finally:
            conn.close()

    def handle_line(self, line: bytes) -> None:
        line = line.strip()
        if not line:
            return
        event = json.loads(line)
        time_nano = int(event.get("timeNano") or 0)
        if time_nano and time_nano <= self.last_nano:
            return  # replayed by ``since`` after a reconnect
        self.last_nano = max(self.last_nano, time_nano)
        record = format_event(event)
        if record is not None:
            self.causes.observe(event, record)
            self.emit(record)
        self.beat()

    def run(self, max_cycles: int | None = None) -> None:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            cycles += 1
            try:
                self.stream_once()
            except (OSError, http.client.HTTPException, ValueError) as exc:
                log_error("docker_events_stream_error", error=repr(exc), retry_in=self.delay)
                self.sleep(self.delay)
                self.delay = min(self.delay * 2, MAX_DELAY)


def main() -> None:
    # PID 1 ignores SIGTERM without a handler: exit promptly on docker stop.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    Watcher().run()


if __name__ == "__main__":
    main()
