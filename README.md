# mcp-ankiconnect MCP server

Connect ChatGPT or Claude conversations with AnkiConnect via MCP to make spaced
repetition as easy as "Let's go through today's flashcards" or "Make flashcards
for this."

## Components

### Tools

The server implements the following tools:

- `num_cards_due_today`: Get the number of cards due today
  - Optional `deck` argument to filter by specific deck
  - Returns count of due cards across all decks or specified deck

- `get_due_cards`: Get cards that are due for review
  - Optional `limit` argument (default: 5) to control number of cards
  - Optional `deck` argument to filter by specific deck
  - Optional `today_only` argument (default: true) to show only today's cards
  - Returns cards in XML format with questions and answers

- `submit_reviews`: Submit answers for reviewed cards
  - Takes list of `reviews` with `card_id` and `rating`
  - Ratings: "wrong", "hard", "good", "easy"
  - Returns confirmation of submitted reviews

- `search_notes`: Find notes by AnkiConnect query. Returns IDs + a short Front preview by default (cheap); pass `return_card_content=true` to receive cleaned field content inline.
- `inspect_cards`: View per-card state for given card IDs or note IDs. Sparse-fieldset selection via the `properties` list: any of `identity`, `state`, `scheduling`, `timestamps`, `history`, `fields`, or `all` (default: `["identity", "state", "scheduling"]`). The legacy `include_history=true` flag is still accepted as an alias.
- `update_note_fields`: Modify the text content of one note's fields. Uses the same MathJax/code conversions as `add_note`.
- `update_note_tags`: Add and/or remove tags on one or more notes.
- `set_suspended`: Suspend or unsuspend one or more cards.
- `change_deck`: Move cards (by card ID) into a different deck.
- `reschedule_cards`: Set due date, forget, or relearn one or more cards.

## Prerequisites

- [Anki 2.1.45 or newer](https://apps.ankiweb.net/) with the
  [AnkiConnect add-on](https://ankiweb.net/shared/info/2055492159) installed
  (add-on code `2055492159`).

The ChatGPT Anki add-on does not require a separate Python or `uv` installation.
It installs and keeps its isolated runtime automatically.

AnkiConnect can be slow on macOS because of App Nap. To disable it for Anki,
run:

```bash
defaults write net.ankiweb.dtop NSAppSleepDisabled -bool true
defaults write net.ichi2.anki NSAppSleepDisabled -bool true
defaults write org.qt-project.Qt.QtWebEngineCore NSAppSleepDisabled -bool true
```

## Claude Desktop quickstart

This standalone Claude Desktop setup uses
[`uv`](https://docs.astral.sh/uv/getting-started/installation/) as its command
runner. Install `uv` before continuing with this section.

1. Install the AnkiConnect plugin in Anki:
   - Tools > Add-ons > Get Add-ons...
   - Enter code: `2055492159`
   - Restart Anki

2. Configure Claude Desktop:

   On MacOS: `~/Library/Application\ Support/Claude/claude_desktop_config.json`
   On Windows: `%APPDATA%/Claude/claude_desktop_config.json`

   Add this configuration:
   ```json
   {
     "mcpServers": {
       "mcp-ankiconnect": {
         "command": "uv",
         "args": ["run", "--with", "mcp-ankiconnect", "mcp-ankiconnect"]
       }
     }
   }
   ```

3. Restart Anki and Claude Desktop.

## ChatGPT with the Anki add-on

The Anki add-on starts this server automatically when Anki starts and connects
it to ChatGPT through an outbound-only
[OpenAI Secure MCP Tunnel](https://developers.openai.com/api/docs/guides/secure-mcp-tunnels).
It does not expose a public port or require an inbound firewall port.

### 1. Create and associate a tunnel

1. Open [OpenAI Platform tunnel settings](https://platform.openai.com/settings/organization/tunnels),
   create a tunnel, and create its runtime API key. Record the `tunnel_...` ID
   and key when they are shown.
2. Associate the tunnel with both the Platform organization that owns it and
   the ChatGPT workspace where you will use Anki. The relevant organization
   roles need Tunnels Read + Manage to create the tunnel and Tunnels Read + Use
   to run or select it.
3. Enable ChatGPT developer mode if necessary. In ChatGPT, open
   **Settings → Plugins**, use the plus button to create a developer-mode app,
   choose **Tunnel** as the connection, and select the tunnel (or paste its ID).

ChatGPT developer-mode access and Platform tunnel permissions are separate.
For managed workspaces, an administrator may need to grant both.

### 2. Install and configure the add-on

1. Download the `.ankiaddon` from the
   [latest GitHub release](https://github.com/friedrich-de/mcp-ankiconnect/releases/latest).
   The add-on is not published on AnkiWeb.
2. In Anki, open **Tools → Add-ons → Install from file**, select the downloaded
   `.ankiaddon`, and confirm the installation.
3. In **Tools → Add-ons**, select **MCP AnkiConnect**, click **Config**, and use
   this configuration:

```json
{
  "CONTROL_PLANE_TUNNEL_ID": "tunnel_0123456789abcdef0123456789abcdef",
  "CONTROL_PLANE_API_KEY": "replace-with-the-runtime-api-key"
}
```

Leaving both values empty disables the add-on. A valid tunnel ID and API key
enable it automatically.

4. Restart Anki. Configuration changes take effect only after a restart.

On first launch, the add-on downloads and verifies its pinned `uv`, then installs
an isolated Python runtime and the server. No system Python or `uv` is used.
Windows, macOS, and glibc Linux are supported on x86-64 and ARM64, including the
official Anki Flatpak.

Runtime files are preserved under `user_files/runtime/`; diagnostics without
tunnel credentials go to `user_files/server.log`. Failed setup is retried after
the next Anki restart, and the server process tree is stopped when Anki exits.

> **Security:** Anki stores add-on configuration, including
> `CONTROL_PLANE_API_KEY`, unencrypted in the add-on's local `meta.json`. Protect
> access to your Anki profile and rotate the runtime key if that file is exposed.

### Standalone secure-tunnel mode

The standalone server can connect to ChatGPT without exposing a public port.
This non-add-on workflow requires `uv`. Set the tunnel environment variables and
start the normal entry point:

```bash
export OPENAI_SECURE_TUNNEL_ENABLED=true
export CONTROL_PLANE_TUNNEL_ID=tunnel_0123456789abcdef0123456789abcdef
export CONTROL_PLANE_API_KEY=sk-...
uv run mcp-ankiconnect
```

When tunnel mode is disabled or unset, `mcp-ankiconnect` continues to use stdio
as before.

## Debugging

Since MCP servers run over stdio, debugging can be challenging. For the best debugging
experience, we strongly recommend using the [MCP Inspector](https://github.com/modelcontextprotocol/inspector).
First, clone the repository and install the dependencies:

```bash
git clone https://github.com/samefarrar/mcp-ankiconnect.git
cd mcp-ankiconnect
uv sync
```
You can launch the MCP Inspector via the mcp CLI:

```bash
uv run mcp dev mcp_ankiconnect/main.py
```

Upon launching, the Inspector will display a URL you can access in your browser to begin debugging.
