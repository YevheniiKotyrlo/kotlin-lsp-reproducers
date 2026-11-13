"""Checks Kotlin/kotlin-lsp#297: an Android library from an included build resolves nothing in the app that uses it.

    python check_297.py --server <intellij-server> [--gradle <gradle>]

Imports fixtures/android-composite/app with nothing built and asks go-to-definition and hover on Library, from the
included Android build ../library, and on Local, from the app's own Android library :local; asks the same of Library in
fixtures/composite/app, whose included build is Kotlin/JVM; and with --gradle runs compileDebugKotlin in the Android app
and asks its two again. Lines and characters are zero-based, as the protocol counts them.

It prints the server's build and platform, each answer and a summary, and exits 1 while the Android app's Library does
not resolve and its Local does, 0 once Library resolves too, and 2 when a step fails: an Android import that does not
settle, a Local that does not resolve, a request with no answer, a build that fails, or anything else that stops it.
"""

import argparse
import platform
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, assert_never
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from lsp_probe import run_probe

ROOT = Path(__file__).resolve().parent
APP_KT = Path("src/main/kotlin/com/example/app/App.kt")
ANDROID_APP = Path("fixtures/android-composite/app")
JVM_APP = Path("fixtures/composite/app")
IMPORT_DEADLINE_SECONDS = 600
GRADLE_DEADLINE_SECONDS = 900


@dataclass(frozen=True)
class Symbol:
    name: str
    origin: str
    line: int
    character: int


ANDROID_LIBRARY = Symbol("Library", "the included Android build", 5, 25)
ANDROID_LOCAL = Symbol("Local", "the app's own Android library", 5, 43)
JVM_LIBRARY = Symbol("Library", "the included Kotlin/JVM build", 5, 12)


@dataclass(frozen=True)
class ImportCase:
    app: Path
    label: str
    symbols: tuple[Symbol, ...]


@dataclass(frozen=True)
class Answer:
    state: Literal["resolved", "empty", "failed"]
    text: str


@dataclass(frozen=True)
class Lookup:
    symbol: Symbol
    definition: Answer
    hover: Answer

    @property
    def resolved(self) -> bool:
        return self.definition.state == "resolved" and self.hover.state == "resolved"

    @property
    def failed(self) -> bool:
        return "failed" in (self.definition.state, self.hover.state)


@dataclass(frozen=True)
class Imported:
    case: ImportCase
    settled: bool
    lookups: tuple[Lookup, ...]

    def lookup(self, symbol: Symbol) -> Lookup:
        return next(lookup for lookup in self.lookups if lookup.symbol == symbol)


@dataclass(frozen=True)
class Built:
    app: Path
    succeeded: bool
    detail: str


Step = Imported | Built


@dataclass(frozen=True)
class Observation:
    server_build: str
    steps: tuple[Step, ...]
    stopped: str | None


def shown_location(uri: str, line: object) -> str:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return f"{uri}:{line}"
    try:
        return f"{Path(url2pathname(unquote(parsed.path))).relative_to(ROOT).as_posix()}:{line}"
    except ValueError:
        return f"{uri}:{line}"


def definition_answer(answer: object) -> Answer:
    if isinstance(answer, dict):
        return Answer("failed", str(answer))
    if not answer:
        return Answer("empty", "nothing")
    return Answer("resolved", ", ".join(shown_location(str(location.get("uri")), location.get("line"))
                                        for location in answer))


def hover_answer(answer: object) -> Answer:
    if isinstance(answer, dict):
        return Answer("failed", str(answer))
    lines = [line.strip() for line in str(answer or "").splitlines()
             if line.strip() and not line.strip().startswith("```")]
    if not lines:
        return Answer("empty", "nothing")
    return Answer("resolved", " ".join(lines))


def server_build_of(report: dict) -> str | None:
    found = re.search(r"ILS-[0-9.]+", (report.get("server_log") or {}).get("build") or "")
    return found.group(0) if found else None


def import_case(server: str, case: ImportCase, runs: Path) -> tuple[Imported, str | None]:
    app_kt = ROOT / case.app / APP_KT
    positions = [f"{app_kt}:{symbol.line}:{symbol.character}" for symbol in case.symbols]
    record = runs / f"297-{case.app.parent.name}-{re.sub(r'[^a-z0-9]+', '-', case.label.lower())}.json"
    report = run_probe(server, [str(ROOT / case.app)], [], positions, positions, IMPORT_DEADLINE_SECONDS, 0, record)
    answers = [query["answer"] for query in report["queries"]]
    count = len(case.symbols)
    lookups = tuple(Lookup(symbol, definition_answer(answers[index]), hover_answer(answers[count + index]))
                    for index, symbol in enumerate(case.symbols))
    return Imported(case, report["initial_import"] == "settled", lookups), server_build_of(report)


def build_app(gradle: str, app: Path) -> Built:
    completed = subprocess.run(
        [shutil.which(gradle) or gradle, "--no-daemon", "--quiet", "-p", str(ROOT / app), "compileDebugKotlin"],
        capture_output=True, encoding="utf-8", errors="replace", timeout=GRADLE_DEADLINE_SECONDS)
    if completed.returncode == 0:
        return Built(app, True, "succeeded")
    output = (completed.stderr or completed.stdout).splitlines()
    last = next((line.strip() for line in reversed(output) if line.strip()), "no output")
    return Built(app, False, f"exited {completed.returncode}: {last}")


def observe(server: str, gradle: str | None, runs: Path) -> Observation:
    steps: list[Step] = []
    server_build = "build unknown"
    cases = [ImportCase(ANDROID_APP, "nothing built", (ANDROID_LIBRARY, ANDROID_LOCAL)),
             ImportCase(JVM_APP, "Kotlin/JVM", (JVM_LIBRARY,))]
    try:
        for case in cases:
            imported, build = import_case(server, case, runs)
            steps.append(imported)
            server_build = build or server_build
        if gradle is not None:
            built = build_app(gradle, ANDROID_APP)
            steps.append(built)
            if built.succeeded:
                imported, build = import_case(
                    server, ImportCase(ANDROID_APP, "after compileDebugKotlin", (ANDROID_LIBRARY, ANDROID_LOCAL)), runs)
                steps.append(imported)
                server_build = build or server_build
    except Exception as error:
        return Observation(server_build, tuple(steps), f"the run stopped: {type(error).__name__}: {error}")
    return Observation(server_build, tuple(steps), None)


def android_imports(observation: Observation) -> list[Imported]:
    return [step for step in observation.steps if isinstance(step, Imported) and step.case.app == ANDROID_APP]


def jvm_clause(observation: Observation) -> str:
    jvm = next((step for step in observation.steps if isinstance(step, Imported) and step.case.app == JVM_APP), None)
    if jvm is None:
        return ""
    if not jvm.settled:
        return "; the Kotlin/JVM app's import did not settle"
    if jvm.lookup(JVM_LIBRARY).resolved:
        return "; Library from an included Kotlin/JVM build resolves"
    return "; Library from an included Kotlin/JVM build does not resolve either"


def verdict(observation: Observation) -> tuple[int, str]:
    if observation.stopped is not None:
        return 2, f"UNVERIFIED: {observation.stopped}"
    failed_build = next((step for step in observation.steps if isinstance(step, Built) and not step.succeeded), None)
    if failed_build is not None:
        return 2, f"UNVERIFIED: gradle compileDebugKotlin in {failed_build.app.as_posix()} {failed_build.detail}"
    android = android_imports(observation)
    if not android:
        return 2, "UNVERIFIED: the Android app was not imported"
    for imported in android:
        if not imported.settled:
            return 2, f"UNVERIFIED: the Android app's import ({imported.case.label}) did not settle"
        unanswered = next((lookup for lookup in imported.lookups if lookup.failed), None)
        if unanswered is not None:
            return 2, f"UNVERIFIED: the server did not answer for {unanswered.symbol.name} ({imported.case.label})"
        if not imported.lookup(ANDROID_LOCAL).resolved:
            return 2, (f"UNVERIFIED: Local from {ANDROID_LOCAL.origin} does not resolve ({imported.case.label}), "
                       "so the run cannot tell")
    states = ", ".join(imported.case.label for imported in android)
    unresolved = [imported.case.label for imported in android if not imported.lookup(ANDROID_LIBRARY).resolved]
    if not unresolved:
        return 0, f"FIXED: Library from {ANDROID_LIBRARY.origin} resolves ({states}){jvm_clause(observation)}"
    return 1, (f"DEFECT: Library from {ANDROID_LIBRARY.origin} does not resolve ({', '.join(unresolved)}) while Local "
               f"from {ANDROID_LOCAL.origin} does{jvm_clause(observation)}")


def report_lines(observation: Observation, system_info: str) -> list[str]:
    lines = [system_info]
    for step in observation.steps:
        match step:
            case Imported():
                settled = "the import settled" if step.settled else "the import did not settle"
                lines.append(f"{step.case.app.as_posix()}, {step.case.label}: {settled}")
                for lookup in step.lookups:
                    symbol = lookup.symbol
                    lines.append(f"  {symbol.name} from {symbol.origin}, App.kt:{symbol.line}:{symbol.character}: "
                                 f"definition {lookup.definition.text}; hover {lookup.hover.text}")
            case Built():
                lines.append(f"{step.app.as_posix()}: gradle compileDebugKotlin {step.detail}")
            case _:
                assert_never(step)
    return lines


def system_info(server_build: str) -> str:
    return (f"kotlin-lsp {server_build}, {platform.system()} {platform.release()} ({platform.version()}), "
            f"Python {platform.python_version()}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Checks Kotlin/kotlin-lsp#297.")
    parser.add_argument("--server", required=True, help="the release's bin/intellij-server")
    parser.add_argument("--gradle", help="a Gradle 8.14 command, to ask again after compileDebugKotlin")
    parser.add_argument("--runs", type=Path, default=ROOT / "runs", help="where each server run's record goes")
    arguments = parser.parse_args(argv)
    # An uncaught exception would exit 1, the code for the defect, so every failure ends as 2.
    try:
        observation = observe(arguments.server, arguments.gradle, arguments.runs)
        code, summary = verdict(observation)
        lines = report_lines(observation, system_info(observation.server_build))
    except Exception as error:
        code, summary, lines = 2, f"UNVERIFIED: the check failed: {type(error).__name__}: {error}", []
    for line in [*lines, summary]:
        print(line)
    return code


if __name__ == "__main__":
    sys.exit(main())
