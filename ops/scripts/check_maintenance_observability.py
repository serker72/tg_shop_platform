"""Run panel PromQL regressions and a disposable PostgreSQL → Alloy → Prometheus smoke."""

# ruff: noqa: S101 -- test assertions; the database safety check is unconditional

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType
from typing import Literal, cast

__all__ = ("time",)
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import OpenerDirector, ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
OBS = ROOT / "ops/observability"
POSTGRES = "postgres:17-alpine"
ALLOY = "grafana/alloy:v1.11.3"
PROMETHEUS = "prom/prometheus:v3.6.0"
DB = "maintenance_smoke_test"
OWNER_PASSWORD = uuid.uuid4().hex
MONITOR_PASSWORD = uuid.uuid4().hex
LABELS = {"env": "test", "host": "maintenance-smoke", "job": "postgres"}


RESOURCE_LABEL = "tgsp.maintenance-smoke"
CLEANUP_TIMEOUT = 5
LATE_CREATE_GRACE = 5
ResourceKind = Literal["network", "container"]


def docker(*args: str, timeout: float = 90) -> str:
    return subprocess.check_output(  # noqa: S603
        ["docker", *args],  # noqa: S607
        text=True,
        timeout=timeout,
        start_new_session=True,
    ).strip()


def panel_query() -> str:
    dashboard = json.loads((OBS / "grafana/dashboards/platform_overview.json").read_text())
    (panel,) = [p for p in dashboard["panels"] if p["id"] == 14]
    return cast(
        str,
        panel["targets"][0]["expr"].replace("$env", LABELS["env"]).replace("$host", LABELS["host"]),
    )


def check_promql(tmp: Path, expr: str, image: str, cancellation: Cancellation) -> None:
    common = 'env="test",host="maintenance-smoke",job="postgres",instance="pg"'
    flag = f'system_maintenance_mode{{{common},server="pg:5432"}}'
    cases = [
        # Evaluate at 60s with 15s spacing; t=0 is stale, t=15 is the inclusive boundary.
        ("recent OFF", "_ _ _ 0", "_ _ _ 1", "_ _ _ 1", 0),
        ("ON at 45s boundary", "_ 1", "_ 1", "_ 1", 1),
        ("OFF at 45s boundary", "_ 0", "_ 1", "_ 1", 0),
        ("missing flag", "_", "_ _ _ 1", "_ _ _ 1", None),
        ("stale flag", "0", "_ _ _ 1", "_ _ _ 1", None),
        ("stale pg_up", "_ _ _ 0", "1", "_ _ _ 1", None),
        ("stale up", "_ _ _ 0", "_ _ _ 1", "1", None),
        ("remote write interrupted", "0", "1", "1", None),
        ("database down", "_ _ _ 0", "_ _ _ 0", "_ _ _ 1", None),
        ("scrape failed", "_ _ _ 0", "_ _ _ 1", "_ _ _ 0", None),
        ("different instance", "_ _ _ 0", "_ _ _ 1", "_ _ _ 1", None),
    ]
    tests = []
    for name, flag_values, pg_values, up_values, result in cases:
        evaluations = [("1m", result)]
        if name == "OFF at 45s boundary":
            # Identical samples cease to be a valid OFF one second past the threshold.
            evaluations.append(("1m1s", None))
        up_labels = (
            common.replace('instance="pg"', 'instance="other"')
            if name == ("different instance")
            else common
        )
        tests.append(
            {
                "name": name,
                "input_series": [
                    {"series": flag, "values": flag_values},
                    {"series": f"pg_up{{{common}}}", "values": pg_values},
                    {"series": f"up{{{up_labels}}}", "values": up_values},
                ],
                "promql_expr_test": [
                    {
                        "expr": expr,
                        "eval_time": eval_time,
                        "exp_samples": []
                        if expected is None
                        else [{"labels": flag, "value": expected}],
                    }
                    for eval_time, expected in evaluations
                ],
            }
        )
    # JSON is valid YAML and avoids requiring project dependencies for make test-alerts.
    (tmp / "promql.yml").write_text(json.dumps({"evaluation_interval": "15s", "tests": tests}))
    run_id = uuid.uuid4().hex
    with DockerCleanup() as cleanup:
        output = cancellation.acquire(
            cleanup,
            "container",
            run_id,
            f"tgsp-maintenance-{run_id[:10]}-promtool",
            "--rm",
            "--network=none",
            "--pull=never",
            "-v",
            f"{tmp}:/test:ro",
            "--entrypoint=promtool",
            image,
            "test",
            "rules",
            "/test/promql.yml",
            foreground=True,
        )
        print(output, flush=True)


class Cancellation:
    def __init__(self) -> None:
        self.requested = False
        self.signum: int | None = None

    @property
    def exit_code(self) -> int:
        return 128 + (self.signum or signal.SIGTERM)

    def request(self, signum: int, _frame: object) -> None:
        # Record only the first signal; never raise from the handler.
        if self.signum is None:
            self.signum = signum
            self.requested = True

    def check(self) -> None:
        if self.requested:
            raise SystemExit(self.exit_code)

    def acquire(
        self,
        cleanup: ExitStack,
        kind: ResourceKind,
        run_id: str,
        name: str,
        *options: str,
        foreground: bool = False,
    ) -> str:
        self.check()
        label = f"{RESOURCE_LABEL}={run_id}:{name}"
        resource = cleanup.enter_context(owned_docker_resource(kind, label))
        if kind == "network":
            command = ("network", "create", "--label", label, name)
        else:
            mode = () if foreground else ("-d",)
            command = ("run", *mode, "--name", name, "--label", label, *options)
        try:
            output = docker(*command)
        except (
            subprocess.TimeoutExpired,
            KeyboardInterrupt,
            subprocess.CalledProcessError,
        ) as error:
            resource.may_appear_late = (
                not isinstance(error, subprocess.CalledProcessError)
                or error.returncode == 125
                or (kind == "network" and error.returncode == 1)
            )
            raise
        self.check()
        return output

    def wait_for(self, check: Callable[[], bool], label: str, timeout: int = 60) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.check()
            try:
                ready = check()
            except (subprocess.CalledProcessError, URLError):
                ready = False
            self.check()
            if ready:
                return
            time.sleep(1)
        self.check()
        raise TimeoutError(label)


def owned_ids(kind: ResourceKind, label: str, timeout: float = CLEANUP_TIMEOUT) -> list[str]:
    all_states = ("--all",) if kind == "container" else ()
    return docker(
        kind,
        "ls",
        *all_states,
        "--quiet",
        "--no-trunc",
        "--filter",
        f"label={label}",
        timeout=timeout,
    ).splitlines()


def remove_owned_resource(kind: ResourceKind, label: str, *, late: bool) -> None:
    # The daemon may still finish creating the resource after an uncertain CLI failure.
    deadline = time.monotonic() + LATE_CREATE_GRACE
    while True:
        remaining = deadline - time.monotonic()
        timeout = min(CLEANUP_TIMEOUT, max(0.1, remaining)) if late else CLEANUP_TIMEOUT
        ids = owned_ids(kind, label, timeout)
        if ids or not late or time.monotonic() >= deadline:
            break
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    remove = ("container", "rm", "-fv") if kind == "container" else ("network", "rm")
    for resource_id in ids:
        try:
            docker(*remove, resource_id, timeout=CLEANUP_TIMEOUT)
        except subprocess.CalledProcessError:
            # An already removed resource is not a failure; do not suppress daemon errors.
            if resource_id in owned_ids(kind, label):
                raise


@dataclass
class CreationState:
    may_appear_late: bool = False


@contextmanager
def owned_docker_resource(kind: ResourceKind, label: str) -> Iterator[CreationState]:
    state = CreationState()
    primary = None
    try:
        yield state
    except BaseException as error:
        primary = error
        raise
    finally:
        try:
            remove_owned_resource(kind, label, late=state.may_appear_late)
        except Exception as error:
            # Diagnostics are strictly best-effort: a closed stderr must not mask
            # either the body exception or the cleanup exception itself.
            try:
                print(f"Docker cleanup failed for {kind} ({label}): {error}", file=sys.stderr)
            except (BrokenPipeError, OSError):
                pass
            if primary is None:
                raise


@contextmanager
def signal_handlers(cancellation: Cancellation) -> Iterator[None]:
    """Keep one non-throwing handler installed for the complete CLI lifetime."""
    previous = {
        signum: signal.signal(signum, cancellation.request)
        for signum in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


class DockerCleanup(ExitStack):
    """Run callbacks without changing signal handlers."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        return bool(super().__exit__(exc_type, exc, traceback))


def local_http_client() -> OpenerDirector:
    return build_opener(ProxyHandler({}))


def smoke(tmp: Path, expr: str, cancellation: Cancellation) -> None:
    run_id = uuid.uuid4().hex
    prefix = f"tgsp-maintenance-{run_id[:10]}"
    with DockerCleanup() as cleanup:
        cancellation.acquire(cleanup, "network", run_id, prefix)

        def start(alias: str, image: str, *options: str, args: tuple[str, ...] = ()) -> str:
            name = f"{prefix}-{alias}"
            cancellation.acquire(
                cleanup,
                "container",
                run_id,
                name,
                "--pull=never",
                "--network",
                prefix,
                "--network-alias",
                alias,
                *options,
                image,
                *args,
            )
            return name

        pg = start(
            "postgres",
            POSTGRES,
            "-e",
            f"POSTGRES_DB={DB}",
            "-e",
            f"POSTGRES_PASSWORD={OWNER_PASSWORD}",
            "-p",
            "127.0.0.1::5432",
        )
        cancellation.wait_for(
            lambda: docker("exec", pg, "pg_isready", "-h", "127.0.0.1", "-d", DB) != "",
            "temporary PostgreSQL not ready",
        )

        def sql(statement: str, *, monitor: bool = False) -> str:
            cancellation.check()
            user, password = (
                ("monitoring", MONITOR_PASSWORD) if monitor else ("postgres", OWNER_PASSWORD)
            )
            return docker(
                "exec",
                "-e",
                f"PGPASSWORD={password}",
                pg,
                "psql",
                "-XAtq",
                "-h",
                "127.0.0.1",
                "-v",
                "ON_ERROR_STOP=1",
                "-U",
                user,
                "-d",
                DB,
                "-c",
                statement,
            )

        if not DB.endswith("_test") or sql("SELECT current_database()") != DB:
            raise RuntimeError("refusing to modify a non-test database")
        # Only the schema surface involved in the custom query; no shared volumes or credentials.
        sql(
            "CREATE TABLE public.system_settings (id int PRIMARY KEY CHECK(id=1), "
            "is_maintenance_mode bool NOT NULL, extra_config jsonb NOT NULL); "
            "INSERT INTO public.system_settings VALUES (1, false, '{}'); "
            "CREATE ROLE monitoring LOGIN; REVOKE USAGE ON SCHEMA public FROM PUBLIC"
        )
        assert sql("SELECT has_schema_privilege('monitoring','public','USAGE')") == "f"
        port = docker("port", pg, "5432/tcp").rsplit(":", 1)[1]
        env = {
            "PATH": os.environ["PATH"],
            "PROJECT_ENVIRONMENT": "test",
            "POSTGRES_HOST": "127.0.0.1",
            "POSTGRES_PORT": port,
            "POSTGRES_DB": DB,
            "POSTGRES_USER": "postgres",
            "POSTGRES_PASSWORD": OWNER_PASSWORD,
            "POSTGRES_MONITORING_USER": "monitoring",
            "POSTGRES_MONITORING_PASSWORD": MONITOR_PASSWORD,
        }
        for _ in range(2):
            subprocess.run(  # noqa: S603
                [sys.executable, str(ROOT / "ops/scripts/ensure_monitoring_role.py")],
                cwd=tmp,
                env=env,
                check=True,
                timeout=30,
                start_new_session=True,
            )
        assert (
            sql("SELECT is_maintenance_mode::int FROM public.system_settings", monitor=True) == "0"
        )
        assert (
            sql(
                "SELECT has_column_privilege(current_user,'public.system_settings',"
                "'extra_config','SELECT'), "
                "has_column_privilege(current_user,'public.system_settings','id','SELECT'), "
                "has_table_privilege(current_user,'public.system_settings','UPDATE')",
                monitor=True,
            )
            == "f|f|f"
        )
        print("Existing monitoring role: grant, scope and repeat run OK", flush=True)

        # Use the actual production components, removing only unrelated scrape targets.
        alloy_config = (OBS / "alloy/config.alloy").read_text()
        blocks = [
            re.search(rf"(?m)^{re.escape(component)} \{{\n[\s\S]*?^\}}", alloy_config)
            for component in (
                'prometheus.exporter.postgres "main"',
                'discovery.relabel "postgres"',
                'prometheus.scrape "exporters"',
                'prometheus.remote_write "obs"',
            )
        ]
        assert all(blocks)
        (tmp / "smoke.alloy").write_text(
            "\n\n".join(
                block.group()
                .replace("\t\tdiscovery.relabel.node.output,\n", "")
                .replace("\t\tdiscovery.relabel.redis.output,\n", "")
                for block in blocks
                if block
            )
            + "\n"
        )
        (tmp / "prometheus.yml").write_text("scrape_configs: []\n")
        prom = start(
            "prometheus",
            PROMETHEUS,
            "-p",
            "127.0.0.1::9090",
            "-v",
            f"{tmp}:/test:ro",
            args=("--web.enable-remote-write-receiver", "--config.file=/test/prometheus.yml"),
        )
        endpoint = docker("port", prom, "9090/tcp")
        start(
            "alloy",
            ALLOY,
            "-e",
            f"POSTGRES_DB={DB}",
            "-e",
            "POSTGRES_MONITORING_USER=monitoring",
            "-e",
            f"POSTGRES_MONITORING_PASSWORD={MONITOR_PASSWORD}",
            "-e",
            "PROJECT_ENVIRONMENT=test",
            "-e",
            f"ALLOY_HOST_LABEL={LABELS['host']}",
            "-e",
            "OBS_PROMETHEUS_REMOTE_WRITE_URL=http://prometheus:9090/api/v1/write",
            "-v",
            f"{tmp}:/test:ro",
            "-v",
            f"{OBS}/alloy/postgres_queries.yml:/etc/alloy/postgres_queries.yml:ro",
            args=("run", "--disable-reporting", "/test/smoke.alloy"),
        )

        client = local_http_client()

        def observed(value: int) -> bool:
            url = f"http://{endpoint}/api/v1/query?{urlencode({'query': expr})}"
            with client.open(url, timeout=5) as response:
                result = json.load(response)["data"]["result"]
            if not result:
                return False
            (sample,) = result
            assert sample["metric"]["__name__"] == "system_maintenance_mode"
            assert {key: sample["metric"][key] for key in LABELS} == LABELS
            assert sample["metric"]["instance"] and sample["metric"]["server"] == "postgres:5432"
            return float(sample["value"][1]) == value

        cancellation.wait_for(lambda: observed(0), "OFF not received by Prometheus")
        sql("UPDATE public.system_settings SET is_maintenance_mode = true WHERE id = 1")
        cancellation.wait_for(lambda: observed(1), "ON not received by Prometheus")
        print("PostgreSQL → Alloy → Prometheus: OFF → ON and panel labels OK", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--promql-only", action="store_true", help="run only Docker promtool")
    parser.add_argument("--promtool-image", default=PROMETHEUS)
    args = parser.parse_args()
    cancellation = Cancellation()
    primary: BaseException | None = None
    with signal_handlers(cancellation):
        try:
            with TemporaryDirectory(prefix="tgsp-maintenance-") as directory:
                tmp = Path(directory)
                tmp.chmod(0o755)  # Prometheus runs as a non-root user in its container.
                expr = panel_query()
                check_promql(tmp, expr, args.promtool_image, cancellation)
                cancellation.check()
                if not args.promql_only:
                    smoke(tmp, expr, cancellation)
            cancellation.check()
        except BaseException as error:
            primary = error

        # This is deliberately after every ExitStack callback and while the
        # non-throwing handlers are still installed.
        if primary is not None:
            raise primary
        if cancellation.requested:
            raise SystemExit(cancellation.exit_code)


if __name__ == "__main__":
    main()
