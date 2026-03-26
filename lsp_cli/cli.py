"""LSP CLI — IDE-grade code intelligence for coding agents.

All output is JSON to stdout, human-readable messages to stderr.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Annotated, Optional

import typer

from lsp_cli.client import DaemonClient
from lsp_cli.locate import Location


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
) -> None:
    """Jump to definition of symbol at location."""
    try:
        loc = Location.parse(locate)
        result = _client().call("lsp/definition", _loc_params(loc, session))
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("references")
def references(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
) -> None:
    """Find all references to symbol at location."""
    try:
        loc = Location.parse(locate)
        result = _client().call("lsp/references", _loc_params(loc, session))
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("hover")
def hover(
    locate: Annotated[str, typer.Argument(help="file:line:col location")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
) -> None:
    """Get type signature and docs at location."""
    try:
        loc = Location.parse(locate)
        result = _client().call("lsp/hover", _loc_params(loc, session))
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("symbols")
def symbols(
    query: Annotated[str, typer.Argument(help="Symbol name query")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
) -> None:
    """Search symbols by name across the project."""
    try:
        result = _client().call("lsp/symbols", {
            "query": query,
            "session": session,
        })
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("diagnostics")
def diagnostics(
    file: Annotated[Optional[str], typer.Argument(help="File path (optional)")] = None,
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
    fresh: Annotated[bool, typer.Option("--fresh", help="Wait for file watcher to process pending changes")] = False,
) -> None:
    """Get diagnostics (errors/warnings) for a file."""
    try:
        if fresh:
            import time
            time.sleep(0.5)  # Brief wait for file watcher debounce
        result = _client().call("lsp/diagnostics", {
            "file": os.path.abspath(file) if file else None,
            "session": session,
        })
        _output(result)
    except Exception as e:
        _error(str(e))


@app.command("outline")
def outline(
    file: Annotated[str, typer.Argument(help="File path")],
    session: Annotated[Optional[str], typer.Option("--session", "-s", help="Session name")] = None,
) -> None:
    """Get file structure (classes, functions, methods)."""
    try:
        result = _client().call("lsp/outline", {
            "file": os.path.abspath(file),
            "session": session,
        })
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
You'll get an immediate response like:

    {"status": "indexing", "retry_after": 15, "elapsed_seconds": 0}

Just retry after the suggested seconds. Never blocks, never times out.

## Query Commands

All positions are 1-indexed (line 1 = first line, col 1 = first char).

    lsp hover <file>:<line>:<col>        # Type signature and docs
    lsp definition <file>:<line>:<col>   # Jump to definition
    lsp references <file>:<line>:<col>   # Find all usages
    lsp symbols "<query>"                # Search symbols by name
    lsp diagnostics <file> [--fresh]     # Errors/warnings (--fresh waits for pending changes)
    lsp outline <file>                   # File structure (kinds: Function, Struct, etc.)
    lsp rename <file>:<line>:<col> <new> # Preview rename (--dry-run default)

## Session Management (Optional)

Sessions are auto-created, but you can manage them explicitly:

    lsp session list                     # See all active sessions
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
- All output is JSON to stdout; errors go to stderr
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
    from lsp_cli.daemon import is_daemon_running

    if is_daemon_running():
        _error("Daemon is already running")

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
        _output(result)
    except ConnectionError:
        sys.stderr.write("Daemon is not running.\n")
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


if __name__ == "__main__":
    app()
