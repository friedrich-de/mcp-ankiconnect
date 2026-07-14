"""Environment-driven OpenAI Secure MCP Tunnel support."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen
import zipfile


LATEST_RELEASE_API = "https://api.github.com/repos/openai/tunnel-client/releases/latest"
TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")
TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_VALUES = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class SecureTunnelSettings:
    enabled: bool = False
    tunnel_id: str = ""
    api_key: str = field(default="", repr=False)

    @classmethod
    def from_environment(
        cls, environment: Mapping[str, str] | None = None
    ) -> "SecureTunnelSettings":
        environment = environment if environment is not None else os.environ
        raw_enabled = environment.get("OPENAI_SECURE_TUNNEL_ENABLED", "")
        normalized = raw_enabled.strip().lower()
        if normalized in TRUE_VALUES:
            enabled = True
        elif normalized in FALSE_VALUES:
            enabled = False
        else:
            raise ValueError(
                "OPENAI_SECURE_TUNNEL_ENABLED must be one of: "
                "true, false, 1, 0, yes, no, on, off."
            )

        return cls(
            enabled=enabled,
            tunnel_id=environment.get("CONTROL_PLANE_TUNNEL_ID", "").strip(),
            api_key=environment.get("CONTROL_PLANE_API_KEY", "").strip(),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        if not TUNNEL_ID_RE.fullmatch(self.tunnel_id):
            raise ValueError(
                "CONTROL_PLANE_TUNNEL_ID must use the form 'tunnel_' followed by "
                "32 lowercase hexadecimal characters."
            )
        if not self.api_key:
            raise ValueError(
                "CONTROL_PLANE_API_KEY is required when OpenAI Secure Tunnel is enabled."
            )


def default_cache_dir(environment: Mapping[str, str] | None = None) -> Path:
    environment = environment if environment is not None else os.environ
    if configured := environment.get("MCP_ANKICONNECT_CACHE_DIR"):
        return Path(configured).expanduser()
    if os.name == "nt" and (local_app_data := environment.get("LOCALAPPDATA")):
        return Path(local_app_data) / "mcp-ankiconnect"
    if platform.system() == "Darwin":
        return Path.home() / "Library" / "Caches" / "mcp-ankiconnect"
    if xdg_cache := environment.get("XDG_CACHE_HOME"):
        return Path(xdg_cache) / "mcp-ankiconnect"
    return Path.home() / ".cache" / "mcp-ankiconnect"


def _platform_asset_suffix(
    system: str | None = None, machine: str | None = None
) -> str:
    system = (system or platform.system()).lower()
    machine = (machine or platform.machine()).lower()
    os_name = {"darwin": "darwin", "linux": "linux", "windows": "windows"}.get(system)
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine)
    if not os_name or not architecture:
        raise RuntimeError(
            f"OpenAI tunnel-client does not publish a supported binary for {system}/{machine}."
        )
    return f"{os_name}-{architecture}.zip"


class TunnelClientBinaryManager:
    """Find or download the official tunnel-client binary for this host."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        opener: Callable[..., Any] = urlopen,
        release_api: str = LATEST_RELEASE_API,
    ) -> None:
        self.cache_dir = cache_dir
        self.opener = opener
        self.release_api = release_api

    @property
    def executable_path(self) -> Path:
        executable = "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
        return self.cache_dir / "tunnel-client" / executable

    def ensure(self) -> Path:
        if override := os.environ.get("OPENAI_TUNNEL_CLIENT_PATH"):
            path = Path(override).expanduser()
            if not path.is_file():
                raise RuntimeError(f"OPENAI_TUNNEL_CLIENT_PATH does not exist: {path}")
            return path
        if self.executable_path.is_file():
            return self.executable_path
        if executable := shutil.which("tunnel-client"):
            return Path(executable)
        return self._download_latest()

    def _open(self, url: str):
        request = Request(url, headers={"User-Agent": "mcp-ankiconnect"})
        return self.opener(request, timeout=30)

    def _download_latest(self) -> Path:
        try:
            with self._open(self.release_api) as response:
                release = json.load(response)
        except (OSError, URLError, ValueError) as exc:
            raise RuntimeError(
                f"Unable to inspect the latest tunnel-client release: {exc}"
            ) from exc

        if release.get("draft") or release.get("prerelease"):
            raise RuntimeError(
                "The latest tunnel-client release is not a stable public release."
            )

        suffix = _platform_asset_suffix()
        assets = [
            asset
            for asset in release.get("assets", [])
            if str(asset.get("name", "")).endswith(suffix)
        ]
        if len(assets) != 1:
            raise RuntimeError(
                f"Unable to locate exactly one tunnel-client asset for {suffix}."
            )

        asset = assets[0]
        digest = str(asset.get("digest") or "")
        if not digest.startswith("sha256:"):
            raise RuntimeError(
                "The tunnel-client release asset does not include a SHA-256 digest."
            )
        download_url = str(asset.get("browser_download_url") or "")
        if not download_url.startswith(
            "https://github.com/openai/tunnel-client/releases/"
        ):
            raise RuntimeError(
                "The tunnel-client asset URL is not an official OpenAI release URL."
            )

        self.executable_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path: Path | None = None
        temporary_executable: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                dir=self.executable_path.parent, suffix=".zip", delete=False
            ) as archive:
                archive_path = Path(archive.name)
                sha256 = hashlib.sha256()
                with self._open(download_url) as response:
                    while chunk := response.read(1024 * 1024):
                        archive.write(chunk)
                        sha256.update(chunk)

            if sha256.hexdigest() != digest.removeprefix("sha256:").lower():
                raise RuntimeError(
                    "Downloaded tunnel-client archive failed SHA-256 verification."
                )

            with zipfile.ZipFile(archive_path) as archive:
                executable_name = (
                    "tunnel-client.exe" if os.name == "nt" else "tunnel-client"
                )
                candidates = [
                    member
                    for member in archive.infolist()
                    if not member.is_dir()
                    and Path(member.filename).name == executable_name
                ]
                if len(candidates) != 1:
                    raise RuntimeError(
                        "The tunnel-client archive does not contain exactly one executable."
                    )
                with (
                    archive.open(candidates[0]) as source,
                    tempfile.NamedTemporaryFile(
                        dir=self.executable_path.parent, delete=False
                    ) as destination,
                ):
                    temporary_executable = Path(destination.name)
                    shutil.copyfileobj(source, destination)

            assert temporary_executable is not None
            temporary_executable.chmod(0o700)
            os.replace(temporary_executable, self.executable_path)
            temporary_executable = None
            self._write_release_metadata(release, asset)
            return self.executable_path
        finally:
            if archive_path is not None:
                archive_path.unlink(missing_ok=True)
            if temporary_executable is not None:
                temporary_executable.unlink(missing_ok=True)

    def _write_release_metadata(
        self, release: Mapping[str, Any], asset: Mapping[str, Any]
    ) -> None:
        metadata = {
            "tag_name": release.get("tag_name"),
            "asset": asset.get("name"),
            "digest": asset.get("digest"),
        }
        path = self.executable_path.parent / "release.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(metadata, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, path)


class ThreadedMCPServer:
    """Run FastMCP on an ephemeral loopback Streamable HTTP endpoint."""

    def __init__(self, fast_mcp: Any) -> None:
        self.fast_mcp = fast_mcp
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._server: Any | None = None
        self._url: str | None = None

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("The local MCP server has not started.")
        return self._url

    def start(self) -> None:
        import uvicorn

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(
                self.fast_mcp.streamable_http_app(),
                host="127.0.0.1",
                port=port,
                log_level="warning",
                access_log=False,
            )
        )
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            name="mcp-ankiconnect-http",
            daemon=True,
        )
        self._socket = listener
        self._server = server
        self._thread = thread
        self._url = f"http://127.0.0.1:{port}/mcp"
        thread.start()

        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(0.05)
        if not server.started:
            self.stop()
            raise RuntimeError(
                "The local MCP server did not become ready within 10 seconds."
            )

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=5)
        if self._socket is not None:
            self._socket.close()
        self._server = None
        self._thread = None
        self._socket = None
        self._url = None


def build_tunnel_environment(
    settings: SecureTunnelSettings,
    mcp_url: str,
    environment: Mapping[str, str] | None = None,
) -> dict[str, str]:
    child_environment = dict(environment if environment is not None else os.environ)
    child_environment["CONTROL_PLANE_TUNNEL_ID"] = settings.tunnel_id
    child_environment["CONTROL_PLANE_API_KEY"] = settings.api_key
    child_environment["MCP_SERVER_URL"] = mcp_url
    child_environment.pop("OPENAI_ADMIN_KEY", None)
    return child_environment


def build_tunnel_command(executable: Path, health_url_file: Path) -> list[str]:
    return [
        str(executable),
        "run",
        "--health.listen-addr",
        "127.0.0.1:0",
        "--health.url-file",
        str(health_url_file),
        "--log.level",
        "info",
        "--log.format",
        "struct-text",
    ]


def _terminate_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def run_secure_tunnel(
    fast_mcp: Any,
    settings: SecureTunnelSettings,
    *,
    cache_dir: Path | None = None,
    binary_manager: TunnelClientBinaryManager | None = None,
    server_factory: Callable[[Any], ThreadedMCPServer] = ThreadedMCPServer,
    popen_factory: Callable[..., subprocess.Popen[Any]] = subprocess.Popen,
) -> int:
    """Run tunnel-client in the foreground until it exits or is interrupted."""

    settings.validate()
    cache_dir = cache_dir or default_cache_dir()
    manager = binary_manager or TunnelClientBinaryManager(cache_dir)
    executable = manager.ensure()
    cache_dir.mkdir(parents=True, exist_ok=True)
    health_url_file = cache_dir / "tunnel-health.url"
    health_url_file.unlink(missing_ok=True)

    server = server_factory(fast_mcp)
    server.start()
    process: subprocess.Popen[Any] | None = None
    try:
        command = build_tunnel_command(executable, health_url_file)
        environment = build_tunnel_environment(settings, server.url)
        process = popen_factory(command, env=environment)
        return process.wait()
    except KeyboardInterrupt:
        if process is not None:
            _terminate_process(process)
        return 130
    finally:
        if process is not None and process.poll() is None:
            _terminate_process(process)
        server.stop()
