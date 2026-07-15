# MCP AnkiConnect configuration

Provide the OpenAI tunnel ID and its runtime API key. The tunnel ID must be
`tunnel_` followed by 32 lowercase hexadecimal characters. Leaving both fields
empty disables the add-on; complete credentials enable it automatically.

The add-on downloads and verifies its pinned `uv` runtime automatically.
Subsequent launches reuse the managed copy. No separate Python or `uv`
installation is needed.

First-run setup requires outbound HTTPS access to GitHub releases, Python
package indexes, and OpenAI. A failed download produces a warning, leaves no
partial executable, and is retried the next time Anki starts.

Changes take effect after Anki is restarted. Managed `uv`, Python, and package
caches are preserved under `user_files/runtime/`. Bootstrap and server
diagnostics are written without tunnel credentials to `user_files/server.log`.

Supported systems are Windows x86-64/ARM64, macOS Intel/Apple Silicon, and
glibc-based Linux x86-64/ARM64. On the official Anki Flatpak, the managed runtime
runs inside the sandbox and does not access host executables.

**Security:** Anki stores this configuration, including
`CONTROL_PLANE_API_KEY`, unencrypted in the add-on's local `meta.json` file.
