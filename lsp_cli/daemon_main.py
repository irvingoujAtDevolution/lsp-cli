"""Entry point for the daemon process (spawned by client)."""

if __name__ == "__main__":
    from lsp_cli.daemon import run_daemon
    run_daemon(foreground=False)
