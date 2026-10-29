"""Drives kotlin-lsp over stdio and prints what it answers: its workspace capabilities, the progress it reports,
and hover and go-to-definition at the positions named, optionally after adding folders to the running server.

    python lsp_probe.py --server <intellij-server> --folder <dir> [--folder <dir>]... [--add <dir>]...
        [--definition <file>:<line>:<character>]... [--hover <file>:<line>:<character>]...
        [--import-deadline <seconds>] [--grace <seconds>] [--record <file>]

Lines and characters are zero-based, as the protocol counts them. The import's end is the server's own progress
report: every work-done progress it began has ended. A folder added with workspace/didChangeWorkspaceFolders is
given --grace seconds to begin an import of its own before the queries run. The server's log is copied beside
the record before the server stops, since it removes its temporary system folder on exit.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HANDLER_NOT_FOUND = "Notification handler for"


class Server:
    def __init__(self, executable: str, stderr_path: Path) -> None:
        self.started = time.monotonic()
        self.started_wall = time.time()
        self.events: list[dict] = []
        self.pending: dict[int, dict] = {}
        self.open_progress: dict[str, str] = {}
        self.progress_begun = 0
        self.folders: list[Path] = []
        self.condition = threading.Condition()
        self.next_id = 0
        creation_flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.stderr = stderr_path.open("wb")
        self.process = subprocess.Popen(
            [executable, "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self.stderr,
            creationflags=creation_flags,
        )
        threading.Thread(target=self._read, daemon=True).start()

    def elapsed(self) -> float:
        return round(time.monotonic() - self.started, 1)

    def record(self, kind: str, detail: object) -> None:
        with self.condition:
            self.events.append({"at": self.elapsed(), "kind": kind, "detail": detail})
            self.condition.notify_all()

    def _send(self, message: dict) -> None:
        body = json.dumps(message).encode("utf-8")
        self.process.stdin.write(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
        self.process.stdin.flush()

    def notify(self, method: str, params: dict) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def request(self, method: str, params: dict, deadline: float) -> dict:
        with self.condition:
            self.next_id += 1
            request_id = self.next_id
            self.pending[request_id] = {}
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        with self.condition:
            answered = self.condition.wait_for(lambda: "answer" in self.pending[request_id], timeout=deadline)
            if not answered:
                return {"outcome": "timed-out", "seconds": deadline}
            return {"outcome": "answered", "message": self.pending.pop(request_id)["answer"]}

    def _read(self) -> None:
        stream = self.process.stdout
        while True:
            length = None
            while True:
                header = stream.readline()
                if not header:
                    self.record("server-exited", self.process.poll())
                    return
                header = header.strip()
                if not header:
                    break
                name, _, value = header.decode("ascii").partition(":")
                if name.lower() == "content-length":
                    length = int(value.strip())
            if length is not None:
                self._handle(json.loads(stream.read(length)))

    def _handle(self, message: dict) -> None:
        if "id" in message and "method" not in message:
            with self.condition:
                if message["id"] in self.pending:
                    self.pending[message["id"]]["answer"] = message
                    self.condition.notify_all()
            return
        method = message.get("method")
        params = message.get("params") or {}
        if "id" in message:
            if method == "workspace/configuration":
                result = [None] * len(params.get("items", []))
            elif method == "workspace/workspaceFolders":
                result = [{"uri": folder.as_uri(), "name": folder.name} for folder in self.folders]
            else:
                result = None
            self._send({"jsonrpc": "2.0", "id": message["id"], "result": result})
            self.record("server-request", method)
            return
        if method == "$/progress":
            token = str(params.get("token"))
            value = params.get("value") or {}
            with self.condition:
                if value.get("kind") == "begin":
                    self.open_progress[token] = value.get("title", "")
                    self.progress_begun += 1
                elif value.get("kind") == "end":
                    self.open_progress.pop(token, None)
            self.record("progress", {"token": token, "kind": value.get("kind"), "title": value.get("title"),
                                     "message": value.get("message")})
        elif method in ("window/logMessage", "window/showMessage"):
            self.record(method, str(params.get("message", ""))[:300])
        elif method == "textDocument/publishDiagnostics":
            self.record("diagnostics", {"uri": params.get("uri"),
                                        "messages": [item.get("message") for item in params.get("diagnostics", [])]})

    def wait_for_progress_to_settle(self, begun_before: int, deadline: float) -> str:
        """`settled` once a progress begun after `begun_before` exists and none is open; `timed-out` otherwise."""
        with self.condition:
            settled = self.condition.wait_for(
                lambda: self.progress_begun > begun_before and not self.open_progress, timeout=deadline)
        return "settled" if settled else "timed-out"

    def wait_for_progress_to_begin(self, begun_before: int, grace: float) -> str:
        with self.condition:
            begun = self.condition.wait_for(lambda: self.progress_begun > begun_before, timeout=grace)
        return "begun" if begun else "none-within-grace"

    def stop(self) -> None:
        self.request("shutdown", {}, 60)
        self.notify("exit", {})
        try:
            self.process.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self.process.kill()
        self.stderr.close()


def parse_position(text: str) -> tuple[Path, int, int]:
    file, line, character = text.rsplit(":", 2)
    return Path(file).resolve(), int(line), int(character)


def summarize_answer(kind: str, answer: dict) -> object:
    if answer["outcome"] != "answered":
        return answer
    message = answer["message"]
    if "error" in message:
        return {"error": message["error"]}
    result = message.get("result")
    if kind == "definition":
        locations = result if isinstance(result, list) else ([] if result is None else [result])
        return [{"uri": location.get("uri") or location.get("targetUri"),
                 "line": (location.get("range") or location.get("targetSelectionRange") or {}).get("start", {}).get("line")}
                for location in locations]
    if result is None:
        return None
    contents = result.get("contents")
    text = contents.get("value") if isinstance(contents, dict) else json.dumps(contents)
    return text[:240]


def read_server_log(started_wall: float, copy_to: Path) -> dict:
    """The log of the running server this run started, copied out: the server removes its temporary system
    folder when it exits. It is the newest such folder made since the start."""
    def created(folder: Path) -> float:
        status = folder.stat()
        return getattr(status, "st_birthtime", status.st_ctime)

    candidates = [folder for folder in Path(tempfile.gettempdir()).glob("idea-system*")
                  if created(folder) >= started_wall - 1]
    for folder in sorted(candidates, key=created, reverse=True):
        log = folder / "system" / "log" / "intellij-server.log"
        if log.is_file():
            text = log.read_text(encoding="utf-8", errors="replace")
            copy_to.write_text(text, encoding="utf-8")
            lines = text.splitlines()
            return {"path": str(log), "copy": str(copy_to),
                    "build": next((line for line in lines if "ILS-" in line), None),
                    "handler_not_found": [line for line in lines if HANDLER_NOT_FOUND in line]}
    return {"path": None}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--server", required=True)
    parser.add_argument("--folder", action="append", required=True)
    parser.add_argument("--add", action="append", default=[])
    parser.add_argument("--definition", action="append", default=[])
    parser.add_argument("--hover", action="append", default=[])
    parser.add_argument("--import-deadline", type=float, default=900)
    parser.add_argument("--grace", type=float, default=90)
    parser.add_argument("--record")
    arguments = parser.parse_args()

    record_path = Path(arguments.record) if arguments.record else Path(tempfile.gettempdir()) / "lsp-probe.json"
    server = Server(arguments.server, record_path.with_suffix(".stderr.log"))
    server.folders = [Path(folder).resolve() for folder in arguments.folder]
    initialize = server.request("initialize", {
        "processId": os.getpid(),
        "rootUri": server.folders[0].as_uri(),
        "workspaceFolders": [{"uri": folder.as_uri(), "name": folder.name} for folder in server.folders],
        "capabilities": {
            "workspace": {"workspaceFolders": True, "configuration": True},
            "window": {"workDoneProgress": True},
            "textDocument": {"hover": {"contentFormat": ["markdown", "plaintext"]}, "definition": {},
                             "publishDiagnostics": {}},
        },
    }, 300)
    capabilities = (initialize.get("message") or {}).get("result", {}).get("capabilities", {})
    server.notify("initialized", {})
    report: dict = {"workspace_capability": capabilities.get("workspace"),
                    "initial_folders": [str(folder) for folder in server.folders]}
    report["initial_import"] = server.wait_for_progress_to_settle(0, arguments.import_deadline)

    if arguments.add:
        added = [Path(folder).resolve() for folder in arguments.add]
        begun_before = server.progress_begun
        server.folders.extend(added)
        server.notify("workspace/didChangeWorkspaceFolders", {"event": {
            "added": [{"uri": folder.as_uri(), "name": folder.name} for folder in added], "removed": []}})
        report["added_folders"] = [str(folder) for folder in added]
        report["added_import"] = server.wait_for_progress_to_begin(begun_before, arguments.grace)
        if report["added_import"] == "begun":
            report["added_import"] = server.wait_for_progress_to_settle(begun_before, arguments.import_deadline)

    opened: set[Path] = set()
    report["queries"] = []
    for kind, positions in (("definition", arguments.definition), ("hover", arguments.hover)):
        for position in positions:
            file, line, character = parse_position(position)
            if file not in opened:
                server.notify("textDocument/didOpen", {"textDocument": {
                    "uri": file.as_uri(), "languageId": "kotlin", "version": 1,
                    "text": file.read_text(encoding="utf-8")}})
                opened.add(file)
            answer = server.request(f"textDocument/{kind}", {
                "textDocument": {"uri": file.as_uri()}, "position": {"line": line, "character": character}}, 120)
            report["queries"].append({"kind": kind, "at": f"{file.name}:{line}:{character}",
                                      "answer": summarize_answer(kind, answer)})

    report["server_log"] = read_server_log(server.started_wall, record_path.with_suffix(".server.log"))
    server.stop()
    report["progress"] = [event for event in server.events if event["kind"] == "progress"]
    record_path.write_text(json.dumps({"report": report, "events": server.events}, indent=1), encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "progress"}, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
