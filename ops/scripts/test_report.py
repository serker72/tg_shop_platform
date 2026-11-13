#!/usr/bin/env python3
"""Render the milestone I test report (Markdown, Russian) for the customer.

Inputs (``--reports`` dir, produced by ``make test-report``):
  unit.json / integration.json - ``pytest --req-report`` output (see ``tests/conftest.py``);
  promtool.txt / promtool.rc   - output and exit code of ``make test-alerts``.
Registry of requirements: ``tests/traceability.py``. Standard library only.

Exit code: 0 - all suites ran and passed, every ``auto`` requirement is covered and green;
1 - otherwise (the report is written anyway).
"""

from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.traceability import PROMTOOL_REQS, REQUIREMENTS, Requirement  # noqa: E402

GOOD = {"passed", "xfailed"}
BAD = {"failed", "error", "xpassed"}
ICON = {
    "passed": "✅",
    "xfailed": "✅",
    "failed": "❌",
    "error": "❌",
    "xpassed": "❌",
    "skipped": "⏭️",
    "not-run": "—",
}
PROMTOOL_NODEID = "promtool test rules ops/observability/prometheus/tests/rules_test.yml"


@dataclass
class Suite:
    title: str
    present: bool = False
    exitstatus: int | None = None
    duration: float = 0.0
    tests: list[dict[str, Any]] = field(default_factory=list)

    def count(self, *outcomes: str) -> int:
        return sum(1 for t in self.tests if t["outcome"] in outcomes)

    @property
    def ok(self) -> bool:
        return self.present and self.exitstatus in (0, 5) and not self.count(*BAD)


def load_suite(path: Path, title: str) -> Suite:
    if not path.is_file():
        return Suite(title)
    data = json.loads(path.read_text("utf-8"))
    return Suite(title, True, data.get("exitstatus"), float(data.get("duration", 0)), data["tests"])


@dataclass
class Promtool:
    ran: bool
    ok: bool
    rules: int
    output: str


def load_promtool(reports: Path) -> Promtool:
    txt, rc = reports / "promtool.txt", reports / "promtool.rc"
    if not rc.is_file():
        return Promtool(False, False, 0, "")
    output = txt.read_text("utf-8", errors="replace") if txt.is_file() else ""
    rules = sum(int(n) for n in re.findall(r"SUCCESS: (\d+) rules found", output))
    return Promtool(True, rc.read_text().strip() == "0", rules, output)


def run(*cmd: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            cmd, cwd=ROOT, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return "—"
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else "—"


def git_state(output: Path) -> tuple[str, str, bool]:
    """``(describe, short commit, dirty)``; the report file itself does not make the tree dirty.

    Delivery flow: commit the code, run ``make test-report REPORT_MD=docs/reports/...`` on the
    clean tree, commit the report separately - the report then names the verified commit.
    """
    describe = run("git", "describe", "--tags", "--always")
    commit = run("git", "rev-parse", "--short", "HEAD")
    pathspec = ["--", "."]
    try:
        pathspec.append(f":(exclude){output.resolve().relative_to(ROOT)}")
    except ValueError:
        pass  # report outside the repository
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "status", "--porcelain", *pathspec],  # noqa: S607 - git from PATH
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        dirty = proc.returncode != 0 or bool(proc.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = True
    return describe, commit, dirty


def cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def req_result(req: Requirement, tests: list[dict[str, Any]]) -> str:
    if any(t["outcome"] in BAD for t in tests):
        return "❌"
    if not tests:
        return "—" if req.status != "auto" else "❌ нет тестов"
    if not any(t["outcome"] in GOOD for t in tests):
        return "⚠️ не выполнялись"
    return "✅"


def render(suites: list[Suite], prom: Promtool, now: datetime, output: Path) -> tuple[str, bool]:
    describe, commit, dirty = git_state(output)
    version = run("sh", "ops/scripts/version.sh")
    if not dirty:
        version = version.removesuffix("+dirty")  # only the report file itself changed
    dirty_mark = "-dirty" if dirty else ""
    by_req: dict[str, list[dict[str, Any]]] = {key: [] for key in REQUIREMENTS}
    for suite in suites:
        for t in suite.tests:
            for code in t["reqs"]:
                by_req.setdefault(code, []).append(t)
    prom_outcome = "not-run" if not prom.ran else ("passed" if prom.ok else "failed")
    for code in PROMTOOL_REQS:
        by_req[code].append({"nodeid": PROMTOOL_NODEID, "outcome": prom_outcome, "reqs": [code]})

    lines = [
        "# Веха I — отчёт о проверке автотестами",
        "",
        "> Сформирован командой `make test-report`. Не редактировать вручную.",
        "",
        f"- **Дата (UTC):** {now:%Y-%m-%d %H:%M}",
        f"- **Git:** `{describe}{dirty_mark}` (проверенный commit `{commit}`)",
        f"- **Версия приложения:** `{version}`",
        f"- **Окружение:** {platform.system()} {platform.release()} ({platform.machine()}),"
        f" Python {platform.python_version()},"
        f" Docker {run('docker', 'version', '--format', '{{.Server.Version}}')}",
        "",
        *(
            [
                "> ⚠️ **Предварительный отчёт:** рабочее дерево содержало незакоммиченные"
                " изменения; для сдачи перегенерируйте на чистом дереве (см. «Как воспроизвести»).",
                "",
            ]
            if dirty
            else []
        ),
        "## Итоги",
        "",
        "«Пройдено» включает ожидаемо упавшие (xfail); «Упало» — падения и ошибки"
        " подготовки/завершения; каждый тест считается один раз (в JUnit ошибка teardown"
        " прошедшего теста даёт две записи).",
        "",
        "| Набор | Пройдено | Упало | Пропущено | Время, с | Итог |",
        "|---|---:|---:|---:|---:|---|",
    ]
    for s in suites:
        if not s.present:
            lines.append(f"| {s.title} | — | — | — | — | ❌ не выполнялся |")
            continue
        lines.append(
            f"| {s.title} | {s.count(*GOOD)} | {s.count(*BAD)} | {s.count('skipped', 'not-run')}"
            f" | {s.duration:.1f} | {'✅' if s.ok else '❌'} |"
        )
    if prom.ran:
        verdict = "✅" if prom.ok else "❌"
        lines.append(
            f"| Правила алертов (promtool, R8) | {prom.rules} правил, unit-тесты алертов |"
            f" {'0' if prom.ok else 'см. вывод'} | — | — | {verdict} |"
        )
    else:
        lines.append("| Правила алертов (promtool, R8) | — | — | — | — | ❌ не выполнялся |")

    ok = all(s.ok for s in suites) and prom.ok
    lines += [
        "",
        "## Матрица требований",
        "",
        "Коды и формулировки — `tests/traceability.py` (по PLAN.md 3.1/3.3 и"
        " [этапам сдачи](../delivery-milestones.md)).",
        "",
        "| Код | Что обещано | Тесты | Результат | Примечание |",
        "|---|---|---|---|---|",
    ]
    for req in REQUIREMENTS.values():
        tests = by_req.get(req.id, [])
        result = req_result(req, tests)
        if req.status == "auto" and result != "✅":
            ok = False
        if tests:
            items = "<br>".join(
                f"{ICON.get(t['outcome'], '?')} `{cell(t['nodeid'])}`" for t in tests
            )
            tests_cell = f"<details><summary>{len(tests)}</summary>{items}</details>"
        else:
            tests_cell = "—"
        note = req.scope_note or ""
        if req.status != "auto":
            note = f"**{'вручную' if req.status == 'manual' else 'отложено'}.** {note}"
        lines.append(
            f"| **{req.id}** {cell(req.title)} | {cell(req.customer_text)} | {tests_cell}"
            f" | {result} | {cell(note)} |"
        )

    lines += ["", "## Проверено вручную / отложено", ""]
    for req in REQUIREMENTS.values():
        if req.status != "auto":
            kind = "Вручную" if req.status == "manual" else "Отложено"
            lines.append(f"- **{req.id} — {req.title}** ({kind}): {req.scope_note}")
    lines += ["", "Частично (суженная рамка вехи I):", ""]
    for req in REQUIREMENTS.values():
        if req.status == "auto" and req.scope_note:
            lines.append(f"- **{req.id}:** {req.scope_note}")

    lines += [
        "",
        "## Как воспроизвести",
        "",
        "```bash",
        "make env-test          # изолированное тестовое окружение (.env.test)",
        "make test-report       # unit + integration + promtool -> build/reports/",
        "# для сдачи: закоммитить код, затем на чистом дереве:",
        "make test-report REPORT_MD=docs/reports/milestone-I-tests.md",
        "# и закоммитить отчёт отдельным коммитом (поле Git = проверенный коммит)",
        "# по отдельности:",
        "make check             # линтеры + unit-тесты",
        "make test-integration  # тестовый стек + интеграционные тесты",
        "make test-alerts       # promtool check/test rules",
        "```",
        "",
        "Сырые результаты (JSON/JUnit, вывод promtool) — `build/reports/`.",
        "",
    ]
    if prom.ran and not prom.ok:
        lines += [
            "<details><summary>Вывод promtool</summary>",
            "",
            "```",
            prom.output,
            "```",
            "</details>",
            "",
        ]
    return "\n".join(lines), ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reports", type=Path, default=ROOT / "build" / "reports")
    parser.add_argument("--output", type=Path, default=ROOT / "build/reports/milestone-I-tests.md")
    args = parser.parse_args()
    suites = [
        load_suite(args.reports / "unit.json", "Unit-тесты"),
        load_suite(args.reports / "integration.json", "Интеграционные тесты"),
    ]
    text, ok = render(suites, load_promtool(args.reports), datetime.now(UTC), args.output)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(text, "utf-8")
    print(f"Report: {args.output} ({'OK' if ok else 'FAILED'})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
