import logging

from mcp_ankiconnect.server import mcp
from mcp_ankiconnect.secure_tunnel import (
    SecureTunnelSettings,
    run_secure_tunnel,
)

logger = logging.getLogger(__name__)


def main():
    """Main entry point for the package."""
    try:
        tunnel_settings = SecureTunnelSettings.from_environment()
        if tunnel_settings.enabled:
            logger.info("Starting MCP-AnkiConnect through OpenAI Secure MCP Tunnel")
            exit_code = run_secure_tunnel(mcp, tunnel_settings)
            if exit_code:
                raise SystemExit(exit_code)
            return
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Unable to start OpenAI Secure MCP Tunnel: {exc}") from exc

    logger.info("Starting MCP-AnkiConnect server over stdio")
    mcp.run()


if __name__ == "__main__":
    main()
