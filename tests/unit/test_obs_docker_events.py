"""Unit tests of the docker-events sidecar and its compose service (no Docker needed)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import urllib.parse
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml

pytestmark = pytest.mark.req("R8")

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "ops/observability/docker-events/docker_events.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("docker_events", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


de = _load()

ATTRS = {
    "name": "tg_shop_platform-master_bot-1",
    "com.docker.compose.service": "master_bot",
    "com.docker.compose.project": "tg_shop_platform",
    "image": "tg_shop_platform-master_bot",
}


def _event(
    action: str, attrs: dict[str, str] | None = None, nano: int = 1_700_000_000_123_000_000
) -> dict[str, Any]:
    return {
        "Type": "container",
        "Action": action,
        "Actor": {"ID": "deadbeef" * 8, "Attributes": attrs if attrs is not None else dict(ATTRS)},
        "time": nano // 1_000_000_000,
        "timeNano": nano,
    }


def test_oom_event_format() -> None:
    record = de.format_event(_event("oom"))
    assert record == {
        "ts": "2023-11-14T22:13:20.123+00:00",
        "level": "warning",
        "logger": "docker_events",
        "event": "container",
        "action": "oom",
        "container": "tg_shop_platform-master_bot-1",
        "compose_service": "master_bot",
        "compose_project": "tg_shop_platform",
        "image": "tg_shop_platform-master_bot",
    }
    assert "deadbeef" not in json.dumps(record)  # no container id


@pytest.mark.parametrize(("code", "level"), [("137", "warning"), ("0", "info")])
def test_die_event_has_exit_code(code: str, level: str) -> None:
    record = de.format_event(_event("die", {**ATTRS, "exitCode": code}))
    assert record["exit_code"] == int(code)
    assert record["level"] == level


def test_restart_without_compose_labels() -> None:
    record = de.format_event(_event("restart", {"name": "restart-probe", "image": "alpine"}))
    assert record["action"] == "restart"
    assert record["container"] == "restart-probe"
    assert record["compose_service"] == ""
    assert record["compose_project"] == ""
    assert "exit_code" not in record


def test_kill_has_signal_and_other_events_ignored() -> None:
    assert de.format_event(_event("kill", {**ATTRS, "signal": "9"}))["signal"] == "9"
    assert de.format_event(_event("start")) is None
    assert de.format_event({**_event("oom"), "Type": "network"}) is None


def test_events_path_filters() -> None:
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(de.events_path(123)).query)
    assert json.loads(query["filters"][0]) == {
        "type": ["container"],
        "event": ["oom", "die", "restart", "kill"],
    }
    assert query["since"] == ["123"]
    assert "since" not in de.events_path()


class _Resp:
    def __init__(self, lines: list[bytes | BaseException], status: int = 200) -> None:
        self.status, self.lines = status, list(lines)

    def readline(self) -> bytes:
        item = self.lines.pop(0) if self.lines else b""
        if isinstance(item, BaseException):
            raise item
        return item


class _Conn:
    def __init__(self, resp: _Resp | None, paths: list[str]) -> None:
        self.resp, self.paths, self.closed = resp, paths, False

    def request(self, method: str, path: str) -> None:
        assert method == "GET"
        self.paths.append(path)
        if self.resp is None:
            raise ConnectionRefusedError("proxy down")

    def getresponse(self) -> _Resp:
        assert self.resp is not None
        return self.resp

    def close(self) -> None:
        self.closed = True


def _watcher(conns: list[_Conn]) -> tuple[Any, list[dict[str, Any]], list[float]]:
    emitted: list[dict[str, Any]] = []
    sleeps: list[float] = []
    it = iter(conns)
    watcher = de.Watcher(
        connect=lambda: next(it),
        emit=emitted.append,
        sleep=sleeps.append,
        beat=lambda: None,
        now=lambda: 1000.0,
    )
    return watcher, emitted, sleeps


def test_reconnect_with_backoff_and_since(capsys: pytest.CaptureFixture[str]) -> None:
    paths: list[str] = []
    line = json.dumps(_event("oom")).encode() + b"\n"
    conns = [
        _Conn(None, paths),  # proxy unreachable
        _Conn(_Resp([line, line]), paths),  # duplicate skipped, then the stream closes
        _Conn(None, paths),
    ]
    watcher, emitted, sleeps = _watcher(conns)
    watcher.run(max_cycles=3)
    assert [r["action"] for r in emitted] == ["oom"]
    assert sleeps == [1.0, 1.0, 2.0]  # delay reset after a successful connection
    assert "since" not in paths[0]
    assert "since=1000" in paths[2]
    assert all(c.closed for c in conns)
    errors = [json.loads(x) for x in capsys.readouterr().err.splitlines()]
    assert {e["msg"] for e in errors} == {"docker_events_stream_error"}


def test_backoff_capped_and_idle_timeout_is_not_an_error() -> None:
    paths: list[str] = []
    idle = _Conn(_Resp([TimeoutError()]), paths)
    watcher, _, sleeps = _watcher([idle] + [_Conn(None, paths) for _ in range(8)])
    watcher.run(max_cycles=9)
    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0]


def test_http_error_status_is_retried() -> None:
    watcher, _, sleeps = _watcher([_Conn(_Resp([], status=403), [])])
    watcher.run(max_cycles=1)
    assert sleeps == [1.0]


def test_compose_service_unprivileged() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    svc = compose["services"]["docker-events"]
    assert svc["profiles"] == ["observability"]
    assert svc["read_only"] is True
    assert svc["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in svc["security_opt"]
    assert svc["networks"] == ["docker_api"]
    assert not str(svc["user"]).startswith("0")
    assert "privileged" not in svc
    assert "docker.sock" not in json.dumps(svc)
    assert svc["mem_limit"] == "${DOCKER_EVENTS_MEM_LIMIT:-32m}"
    assert "docker-socket-proxy" in svc["depends_on"]


def test_make_agent_down_and_single_host_up() -> None:
    def dry(target: str) -> str:
        return subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["make", "-n", target, "ENV_FILE=.env.example"],  # noqa: S607
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    assert "docker-events" in dry("agent-down")
    # `make up` must keep the single-host overlay (bots stay in the obs network)
    assert "compose.single-host.yml" in dry("up")
