"""LSP CLI — IDE-grade code intelligence for coding agents.

All output is JSON to stdout, human-readable messages to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Annotated, Any, Optional

import typer

from lsp_cli.client import DaemonClient
from lsp_cli.daemon_state import is_daemon_running
from lsp_cli.locate import Location
from lsp_cli.observability import read_events


def _loc_params(loc: Location, session: str | None) -> dict:
    """Build daemon call params from a parsed location.

    Converts 1-indexed CLI input to 0-indexed LSP coordinates
    and resolves relative paths to absolute.
    """
    line = loc.line - 1
    col = loc.col - 1
    if line < 0 or col < 0:
        raise ValueError("Line and column numbers must be >= 1 (1-indexed)")
    return {
        "file": os.path.abspath(loc.file),
        "line": line,
        "col": col,
        "session": session,
    }

app = typer.Typer(
    name="lsp",
    help="IDE-grade code intelligence for coding agents.",
    no_args_is_help=True,
    add_completion=False,
)

session_app = typer.Typer(help="Manage language server sessions.", no_args_is_help=True)
daemon_app = typer.Typer(help="Manage the LSP daemon.", no_args_is_help=True)
app.add_typer(session_app, name="session")
app.add_typer(daemon_app, name="daemon")


def _output(data: object) -> None:
    """Write JSON to stdout."""
    json.dump(data, sys.stdout, indent=2, default=str)
    sys.stdout.write("\n")
    sys.stdout.flush()


def _error(msg: str) -> None:
    """Write error to stderr and exit."""
    sys.stderr.write(f"error: {msg}\n")
    raise typer.Exit(1)


def _client() -> DaemonClient:
    return DaemonClient(auto_start=True)


def _format_wait_hint(session_info: dict[str, Any]) -> str | None:
    status = session_info.get("status")
    progress = session_info.get("progress")
    if not isinstance(progress, dict):
        return None

    details: list[str] = []
    phase = progress.get("phase")
    if isinstance(phase, str) and phase and phase not in {"unknown", status}:
        details.append(phase)

    message = progress.get("message") or progress.get("title")
    if isinstance(message, str) and message:
        details.append(message)

    percentage = progress.get("percentage")
    if isinstance(percentage, (int, float)):
        details.append(f"{percentage:g}%")

    current_file = progress.get("current_file")
    if isinstance(current_file, str) and current_file:
        details.append(current_file)

    observed_files = progress.get("observed_files")
    if isinstance(observed_files, int) and observed_files > 0:
        details.append(f"observed={observed_files}")

    completed = progress.get("completed")
    total = progress.get("total")
    if isinstance(completed, int) and isinstance(total, int) and total > 0:
        details.append(f"{completed}/{total}")

    if not details:
        return status if isinstance(status, str) else None
    prefix = status if isinstance(status, str) and status else "progress"
    return f"{prefix}: {' | '.join(details)}"


def _emit_wait_hint(client: DaemonClient, result: dict[str, Any], last_hint: str | None) -> str | None:
    session_name = result.get("name")
    session_info = result
    if isinstance(session_name, str) and session_name:
        try:
            session_info = client.call("session/info", {"name": session_name})
        except Exception:
            session_info = result

    hint = _format_wait_hint(session_info)
    if hint and hint != last_hint:
        sys.stderr.write(f"{hint}\n")
        sys.stderr.flush()
        return hint
    return last_hint


def _call_with_wait(method: str, params: dict[str, Any], wait: bool) -> Any:
    """Optionally poll until a session leaves starting/indexing state."""
    client = _client()
    result = client.call(method, params)
    if not wait:
        return result

    deadline = time.monotonic() + 30.0
    last_hint: str | None = None
    while (
        isinstance(result, dict)
        and result.get("status") in {"starting", "indexing"}
        and time.monotonic() < deadline
    ):
        last_hint = _emit_wait_hint(client, result, last_hint)
        retry_after = result.get("retry_after", 1)
        sleep_seconds = max(0.2, min(float(retry_after), deadline - time.monotonic()))
        if sleep_seconds <= 0:
            break
        time.sleep(sleep_seconds)
        result = client.call(method, params)

    return result


# === Session commands ===


@session_app.command("start")
def session_start(
    name: Annotated[str, typer.Argument(help="Session name")],
    root: Annotated[str, typer.Option("--root", "-r", help="Project root path")],
    lang: Annotated[str, typer.Option("--lang", "-l", help="Language (rust, csharp, typescript, python, ...)")],
    solution: Annotated[Optional[str], typer.Option("--solution", help="Path to .sln file (for C# monorepos with multiple solutions)")] = None,
) -> None:
    """Start a new language server session."""
    try:
        params: dict = {
            "name": name,
            "root": root,
            "language": lang,
        }
        if solution:
            params["solution"] = os.path.abspath(solution)
        result = _client().call("session/start", params)
        _output(result)
    except Exception as e:
        _error(str(e))


@session_app.command("stop")
def session_stop(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Stop a language server session."""
    try:
        result = _client().call("session/stop", {"name": name})
        _output(result)
    except Exception as e:
        _error(str(e))


@session_app.command("list")
def session_list() -> None:
    """List all active sessions."""
    try:
        result = _client().call("session/list")
        _output(result)
    except Exception as e:
        _error(str(e))


@session_app.command("info")
def session_info(
    name: Annotated[str, typer.Argument(help="Session name")],
) -> None:
    """Get info about a session."""
    try:
        result = _client().call("session/info", {"name": name})
        _output(result)
    except Exception as e:
        _error(str(e))


# === LSP query commands ===


@app.command("definition")
def definition(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Jump to definition of symbol at location."""
    try:
        loc = Location.parse(locate)
        result = _call_with_wait("lsp/definition", _loc_params(loc, session), wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("references")
def references(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Find all references to symbol at location."""
    try:
        loc = Location.parse(locate)
        result = _call_with_wait("lsp/references", _loc_params(loc, session), wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("hover")
def hover(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Get type signature and docs at location."""
    try:
        loc = Location.parse(locate)
        result = _call_with_wait("lsp/hover", _loc_params(loc, session), wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("symbols")
def symbols(
    query: Annotated[str, typer.Argument(help="Symbol name query")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    root: Annotated[Optional[str], typer.Option("--root", "-r", help="Project path to auto-detect a session from")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Search symbols by name across the project."""
    try:
        result = _call_with_wait("lsp/symbols", {
            "query": query,
            "session": session,
            "root": os.path.abspath(root) if root else (None if session else os.getcwd()),
        }, wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("diagnostics")
def diagnostics(
    file: Annotated[Optional[str], typer.Argument(help="File path (optional)")] = None,
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    fresh: Annotated[bool, typer.Option("--fresh", help="Wait for file watcher to process pending changes")] = False,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Get diagnostics (errors/warnings) for a file."""
    try:
        if fresh:
            import time
            time.sleep(0.5)  # Brief wait for file watcher debounce
        result = _call_with_wait("lsp/diagnostics", {
            "file": os.path.abspath(file) if file else None,
            "session": session,
        }, wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("outline")
def outline(
    file: Annotated[str, typer.Argument(help="File path")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    wait: Annotated[bool, typer.Option("--wait/--no-wait", help="Poll until the session is ready")] = True,
) -> None:
    """Get file structure (classes, functions, methods)."""
    try:
        result = _call_with_wait("lsp/outline", {
            "file": os.path.abspath(file),
            "session": session,
        }, wait)
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("rename")
def rename(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    new_name: Annotated[str, typer.Argument(help="New name for the symbol")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Preview changes without applying")] = True,
) -> None:
    """Rename symbol at location."""
    try:
        loc = Location.parse(locate)
        params = _loc_params(loc, session)
        params["new_name"] = new_name
        params["dry_run"] = dry_run
        result = _client().call("lsp/rename", params)
        _output(result)
    except Exception as e:
        _error(str(e))


# === Batch command ===


@app.command("batch")
def batch(
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
) -> None:
    """Execute multiple LSP queries from stdin (one per line).

    Each line format: command file:line:col
    Example:
      definition src/lib.rs:42:10
      hover src/lib.rs:42:10
      references src/lib.rs:42:10
    """
    import sys as _sys
    client = _client()
    results = []
    for line in _sys.stdin:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            results.append({"error": f"Invalid batch line: {line!r}"})
            continue

        cmd, arg = parts[0], parts[1]
        method_map = {
            "definition": "lsp/definition",
            "references": "lsp/references",
            "hover": "lsp/hover",
            "outline": "lsp/outline",
            "diagnostics": "lsp/diagnostics",
        }
        method = method_map.get(cmd)
        if method is None:
            results.append({"error": f"Unknown batch command: {cmd}"})
            continue

        try:
            if cmd in ("definition", "references", "hover"):
                loc = Location.parse(arg)
                result = client.call(method, _loc_params(loc, session))
            elif cmd in ("outline", "diagnostics"):
                result = client.call(method, {
                    "file": os.path.abspath(arg), "session": session,
                })
            else:
                result = None
            results.append({"command": line, "result": result})
        except Exception as e:
            results.append({"command": line, "error": str(e)})

    _output(results)


# === Skill command ===

_SKILL_TEXT = """\
# LSP CLI - Code Intelligence for Agents

Use `lsp` commands for IDE-grade code intelligence without an editor.

## Zero-Config Usage

Just query -- sessions are auto-created from your file path:

    lsp hover src/main.rs:42:10         # Auto-detects project root + language
    lsp definition src/lib.rs:10:5      # Works with relative paths
    lsp references D:\\project\\foo.rs:1:1 # Or absolute paths

On first use for a project, the language server starts indexing in the background.
If you pass --no-wait you'll get an immediate response like:

    {"status": "indexing", "retry_after": 15, "elapsed_seconds": 0}

Read-only queries wait for a usable session by default. On very large repos a
session may become "warm" before it is fully indexed; queries can still run
while background indexing continues. Pass --no-wait if you want the immediate
starting/indexing response instead.

## Query Commands

All positions are 1-indexed (line 1 = first line, col 1 = first char).

    lsp hover <file>:<line>:<col> [--no-wait]         # Type signature and docs
    lsp definition <file>:<line>:<col> [--no-wait]    # Jump to definition
    lsp references <file>:<line>:<col> [--no-wait]    # Find all usages
    lsp symbols "<query>" [--root <path>] [--no-wait] # Search symbols by name
    lsp diagnostics <file> [--fresh] [--no-wait]      # Errors/warnings (--fresh waits for pending changes)
    lsp outline <file> [--no-wait]                    # File structure (kinds: Function, Struct, etc.)
    lsp rename <file>:<line>:<col> <new>              # Currently not implemented in daemon mode

## Session Management (Optional)

Sessions are auto-created, but you can manage them explicitly:

    lsp session list                     # See all active sessions and their status
    lsp session info <name>              # Includes best-effort progress + raw payload
    lsp session start <name> --root <path> --lang <language> [--solution <file.sln>]
    lsp session stop <name>

For C# monorepos with multiple .sln files, use --solution to pick which one:

    lsp session start rdm-win --root D:\\RDM --lang csharp --solution D:\\RDM\\RemoteDesktopManagerWindows.sln

Supported languages: rust, csharp, python, typescript, java, go, cpp, and more.

## Batch Mode

Combine multiple queries in one call to reduce round trips:

    lsp batch --session <name> <<'EOF'
    definition src/lib.rs:42:10
    hover src/lib.rs:42:10
    references src/lib.rs:42:10
    EOF

## Tips

- The daemon auto-starts on first CLI call -- no manual setup needed
- Sessions auto-create from file paths — no need to specify language or project root
- After writing files, use --fresh on diagnostics to wait for LS to process changes
- Use `lsp daemon events --tail 50` to inspect recent structured timings and state transitions
- Use `lsp daemon events --tail 20 --event session.progress` to inspect best-effort progress history
- `warm` means queryable before full indexing/quiescence; `ready` means fully ready
- `session list` shows a compact progress snapshot when available
- `session info` exposes best-effort progress derived from LSP/server notifications
- Final results go to stdout as JSON; wait hints and errors go to stderr
- Use --session <name> to target a specific session when multiple projects are open
- lsp daemon status shows all active sessions and PID
"""


@app.command("skill")
def skill() -> None:
    """Print the LSP CLI usage guide for agent context injection."""
    sys.stdout.write(_SKILL_TEXT)
    sys.stdout.flush()


# === Daemon commands ===


@daemon_app.command("start")
def daemon_start(
    foreground: Annotated[bool, typer.Option("--foreground", "-f", help="Run in foreground")] = False,
) -> None:
    """Start the LSP daemon."""
    if is_daemon_running():
        try:
            result = DaemonClient(auto_start=False).call("daemon/status")
            _output({"status": "already_running", **result})
            return
        except Exception as e:
            _error(str(e))

    if foreground:
        from lsp_cli.daemon import run_daemon
        run_daemon(foreground=True)
    else:
        # Start via client (which auto-starts daemon)
        try:
            client = DaemonClient(auto_start=True)
            result = client.call("daemon/status")
            _output({"status": "started", **result})
        except Exception as e:
            _error(str(e))


@daemon_app.command("stop")
def daemon_stop() -> None:
    """Stop the LSP daemon."""
    try:
        result = _client().call("daemon/shutdown")
        for _ in range(50):
            if not is_daemon_running():
                _output({"status": "stopped"})
                return
            time.sleep(0.1)
        _output(result)
    except ConnectionError:
        _output({"status": "not_running"})
    except Exception as e:
        _error(str(e))


@daemon_app.command("status")
def daemon_status() -> None:
    """Check daemon status."""
    try:
        result = DaemonClient(auto_start=False).call("daemon/status")
        _output(result)
    except ConnectionError:
        _output({"status": "not_running"})
    except Exception as e:
        _error(str(e))


@daemon_app.command("events")
def daemon_events(
    tail: Annotated[int, typer.Option("--tail", help="Number of recent events to return")] = 50,
    event: Annotated[Optional[str], typer.Option("--event", help="Filter to a specific event name")] = None,
) -> None:
    """Show recent structured observability events."""
    try:
        _output(read_events(limit=tail, event=event))
    except Exception as e:
        _error(str(e))


if __name__ == "__main__":
    app()
