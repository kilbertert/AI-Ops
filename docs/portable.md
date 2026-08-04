# Portable Deployment

The first portable release is a console application, not a desktop GUI. It is
designed for Windows 11 first and also builds on Linux. Build on the target
operating system because the Codex SDK supplies a platform-specific native
runtime.

## Windows Requirements

- Windows 11 is the recommended baseline. Fully updated Windows 10 1809 or
  newer is best effort.
- Windows OpenSSH Client must be installed when production data is reached
  through the restricted SSH tunnel. `ssh.exe` may be selected with
  `AIOPS_SSH_BIN`.
- The portable runtime defaults to Codex's `unelevated` Windows sandbox. It
  uses a restricted token derived from the current user and remains bounded by
  the read-only filesystem profile and disabled model-command network. Set
  `AIOPS_WINDOWS_SANDBOX=elevated` only after validating the administrator
  setup and workspace ACL behavior in the target enterprise environment.
- No Python, Node.js, Codex CLI, or project checkout is required by the portable
  bundle.
- The initial zip is unsigned. Enterprise distribution should add organization
  code signing before treating the artifact as a broadly deployed Windows
  application.

## First Run

Open PowerShell in the extracted directory:

```powershell
.\aiops.exe init
.\aiops.exe paths
notepad "$env:LOCALAPPDATA\AI-Ops-Diagnostics\config\production.env"
```

Install a provider key without placing it on the command line:

```powershell
.\aiops.exe key-install primary
.\aiops.exe agent-doctor --key-slot primary
```

The default private locations are below
`%LOCALAPPDATA%\AI-Ops-Diagnostics`. Set `AIOPS_HOME` to move the complete
runtime boundary, or set `AIOPS_CONFIG_HOME` and `AIOPS_DATA_HOME` separately.

## Build

Build on Windows for a Windows artifact and on Linux for a Linux artifact:

```text
uv sync --locked --dev
uv run python packaging/build_portable.py
```

The build creates `dist/aiops/` and a versioned zip archive. It then extracts
the archive outside the source tree and launches it with a minimal PATH. The
acceptance run initializes an isolated private home, checks the provider key
slot and bundled Codex runtime, validates private path permissions, rejects
embedded secret files, and diagnoses the OCPP, YKC mismatch, and missing
transaction-data fixtures.

## Security Boundary

- Provider keys remain external key-slot files. They are never compiled into
  the executable or copied into the archive.
- On Windows, private config, key, Codex-home, and run paths receive a protected
  DACL for the current user. On POSIX systems they remain owner-only `0700` and
  `0600` paths.
- The packaged process launches the SDK-pinned native Codex runtime through the
  same environment-cleaning launcher used in source development.
- The portable bundle still exposes only bounded read-only diagnostic tools.
  Packaging does not add refund, recalculation, replay, order mutation, or
  service-control capabilities.
