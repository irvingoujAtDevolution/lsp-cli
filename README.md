# lsp-cli

IDE-grade code intelligence for coding agents. Wraps Language Server Protocol servers behind a persistent daemon, exposing them as simple CLI commands with JSON output.

## Features

- **Zero-config** -- auto-detects project root, language, and creates sessions on first query
- **Non-blocking** -- language server indexing happens in the background; never blocks, never times out
- **Daemon architecture** -- persistent process survives CLI restarts, keeps indexes warm
- **1-indexed** -- line/col numbers match editors and compilers (not raw LSP 0-indexed)
- **Agent-friendly** -- all output is JSON to stdout, errors to stderr

## Installation

Requires Python 3.11+.

```bash
# Install globally with pipx (recommended)
pipx install "lsp-cli @ git+https://github.com/yourusername/lsp-cli.git"

# Or with pip
pip install "lsp-cli @ git+https://github.com/yourusername/lsp-cli.git"

# Or from local clone
git clone https://github.com/yourusername/lsp-cli.git
cd lsp-cli
pip install .
```

### Dependency

lsp-cli uses [SolidLSP](https://github.com/oraios/serena) from the Serena project as the LSP client backend. It's installed automatically as a dependency.

## Quick Start

```bash
# Just query -- everything is auto-detected
lsp hover src/main.rs:42:10

# Read-only queries wait for a usable session by default
lsp symbols "_resolve_session"

# Opt out if you want the immediate starting/indexing response
lsp symbols "_resolve_session" --no-wait

# Large monorepos may first become "warm" before they are fully indexed.
# In that state, queries are allowed to run while background indexing continues.
```

## Commands

All positions are **1-indexed** (line 1 = first line, col 1 = first character).

```bash
lsp hover <file>:<line>:<col> [--no-wait]         # Type signature and docs
lsp definition <file>:<line>:<col> [--no-wait]    # Jump to definition
lsp references <file>:<line>:<col> [--no-wait]    # Find all usages
lsp symbols "<query>" [--root <path>] [--no-wait] # Search symbols by name
lsp diagnostics <file> [--fresh] [--no-wait]      # Errors/warnings
lsp outline <file> [--no-wait]                    # File structure (Function, Struct, etc.)
lsp rename <file>:<line>:<col> <new>          # Preview rename (--dry-run default)
lsp skill                                      # Print usage guide (for agent context injection)
```

### Session Management (optional)

Sessions are auto-created, but you can manage them explicitly:

```bash
lsp session list   # statuses include starting, warm, ready, error, stopped
lsp session start <name> --root <path> --lang <language>
lsp session stop <name>
```

### Batch Mode

```bash
lsp batch <<'EOF'
definition src/lib.rs:42:10
hover src/lib.rs:42:10
references src/lib.rs:42:10
EOF
```

### Daemon

```bash
lsp daemon status    # Check if daemon is running
lsp daemon start     # Start daemon (auto-starts on first query anyway)
lsp daemon stop      # Stop daemon and all sessions
lsp daemon events --tail 50  # Inspect recent structured timings/state
```

`warm` means the session crossed the startup timeout and is already queryable,
but the language server may still be indexing a large workspace in the
background. `ready` means the server reported full readiness.

## Supported Languages

rust, csharp, python, typescript, javascript, java, go, cpp, c, ruby, swift, kotlin, and [40+ more via SolidLSP](https://github.com/oraios/serena).

## How It Works

```
Agent/CLI  --TCP-->  Daemon (persistent)  --LSP-->  Language Server (rust-analyzer, etc.)
                         |
                    Session Manager
                    File Watcher
```

1. The CLI sends JSON-RPC requests over TCP to a localhost daemon
2. The daemon manages language server sessions (one per project)
3. File watchers notify the language server of filesystem changes
4. The daemon auto-starts on first CLI call and persists across CLI invocations

## License

MIT
