import sys

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "__codex-launcher":
        from aiops_diagnostics.codex_launcher import main

        del sys.argv[1]
        main()
    else:
        from aiops_diagnostics.cli import app

        app()
