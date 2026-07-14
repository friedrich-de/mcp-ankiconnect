from __future__ import annotations

import hashlib
from io import BytesIO
import json
from pathlib import Path
import zipfile

import pytest

import mcp_ankiconnect.main as main_module
from mcp_ankiconnect.secure_tunnel import (
    SecureTunnelSettings,
    TunnelClientBinaryManager,
    _platform_asset_suffix,
    build_tunnel_command,
    build_tunnel_environment,
    run_secure_tunnel,
)


VALID_TUNNEL_ID = "tunnel_0123456789abcdef0123456789abcdef"


def test_tunnel_is_disabled_by_default():
    assert SecureTunnelSettings.from_environment({}) == SecureTunnelSettings()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_tunnel_enabled_values(value):
    settings = SecureTunnelSettings.from_environment(
        {
            "OPENAI_SECURE_TUNNEL_ENABLED": value,
            "CONTROL_PLANE_TUNNEL_ID": VALID_TUNNEL_ID,
            "CONTROL_PLANE_API_KEY": "runtime-secret",
        }
    )

    assert settings.enabled
    assert settings.tunnel_id == VALID_TUNNEL_ID
    assert settings.api_key == "runtime-secret"


def test_invalid_enabled_value_is_rejected():
    with pytest.raises(ValueError, match="OPENAI_SECURE_TUNNEL_ENABLED"):
        SecureTunnelSettings.from_environment(
            {"OPENAI_SECURE_TUNNEL_ENABLED": "sometimes"}
        )


@pytest.mark.parametrize(
    ("settings", "message"),
    [
        (SecureTunnelSettings(enabled=True, api_key="secret"), "TUNNEL_ID"),
        (
            SecureTunnelSettings(enabled=True, tunnel_id=VALID_TUNNEL_ID),
            "API_KEY",
        ),
    ],
)
def test_enabled_tunnel_requires_control_plane_values(settings, message):
    with pytest.raises(ValueError, match=message):
        settings.validate()


@pytest.mark.parametrize(
    ("system", "machine", "expected"),
    [
        ("Darwin", "x86_64", "darwin-amd64.zip"),
        ("Darwin", "arm64", "darwin-arm64.zip"),
        ("Linux", "AMD64", "linux-amd64.zip"),
        ("Linux", "aarch64", "linux-arm64.zip"),
        ("Windows", "AMD64", "windows-amd64.zip"),
        ("Windows", "ARM64", "windows-arm64.zip"),
    ],
)
def test_platform_asset_mapping(system, machine, expected):
    assert _platform_asset_suffix(system, machine) == expected


def test_tunnel_process_uses_official_environment_variables(tmp_path):
    settings = SecureTunnelSettings(
        enabled=True, tunnel_id=VALID_TUNNEL_ID, api_key="runtime-secret"
    )
    environment = build_tunnel_environment(
        settings,
        "http://127.0.0.1:45678/mcp",
        {"OPENAI_ADMIN_KEY": "must-not-leak"},
    )
    command = build_tunnel_command(Path("/opt/tunnel-client"), tmp_path / "health")

    assert environment == {
        "CONTROL_PLANE_TUNNEL_ID": VALID_TUNNEL_ID,
        "CONTROL_PLANE_API_KEY": "runtime-secret",
        "MCP_SERVER_URL": "http://127.0.0.1:45678/mcp",
    }
    assert "runtime-secret" not in command
    assert VALID_TUNNEL_ID not in command


def _zip_with_executable(contents: bytes = b"tunnel binary") -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tunnel-client", contents)
    return buffer.getvalue()


class FakeResponse(BytesIO):
    status = 200


def test_binary_manager_downloads_and_verifies_official_release(tmp_path, monkeypatch):
    archive = _zip_with_executable()
    digest = hashlib.sha256(archive).hexdigest()
    release = {
        "tag_name": "v1.2.3",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "tunnel-client-v1.2.3-linux-amd64.zip",
                "digest": f"sha256:{digest}",
                "browser_download_url": (
                    "https://github.com/openai/tunnel-client/releases/download/"
                    "v1.2.3/tunnel-client-v1.2.3-linux-amd64.zip"
                ),
            }
        ],
    }

    def opener(request, timeout):
        assert timeout == 30
        if request.full_url.endswith("/releases/latest"):
            return FakeResponse(json.dumps(release).encode())
        return FakeResponse(archive)

    monkeypatch.delenv("OPENAI_TUNNEL_CLIENT_PATH", raising=False)
    monkeypatch.setattr(
        "mcp_ankiconnect.secure_tunnel._platform_asset_suffix",
        lambda: "linux-amd64.zip",
    )
    manager = TunnelClientBinaryManager(tmp_path, opener=opener)

    executable = manager.ensure()

    assert executable.read_bytes() == b"tunnel binary"
    metadata = json.loads((executable.parent / "release.json").read_text())
    assert metadata["tag_name"] == "v1.2.3"


def test_binary_manager_rejects_failed_checksum(tmp_path, monkeypatch):
    archive = _zip_with_executable()
    release = {
        "tag_name": "v1.2.3",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": "tunnel-client-v1.2.3-linux-amd64.zip",
                "digest": f"sha256:{'0' * 64}",
                "browser_download_url": (
                    "https://github.com/openai/tunnel-client/releases/download/"
                    "v1.2.3/tunnel-client-v1.2.3-linux-amd64.zip"
                ),
            }
        ],
    }

    def opener(request, timeout):
        if request.full_url.endswith("/releases/latest"):
            return FakeResponse(json.dumps(release).encode())
        return FakeResponse(archive)

    monkeypatch.delenv("OPENAI_TUNNEL_CLIENT_PATH", raising=False)
    monkeypatch.setattr(
        "mcp_ankiconnect.secure_tunnel._platform_asset_suffix",
        lambda: "linux-amd64.zip",
    )
    manager = TunnelClientBinaryManager(tmp_path, opener=opener)

    with pytest.raises(RuntimeError, match="SHA-256 verification"):
        manager.ensure()
    assert not manager.executable_path.exists()


class FakeBinaryManager:
    def ensure(self) -> Path:
        return Path("/managed/tunnel-client")


class FakeServer:
    def __init__(self, fast_mcp):
        self.fast_mcp = fast_mcp
        self.url = "http://127.0.0.1:45678/mcp"
        self.started = False
        self.stopped = False

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class FakeProcess:
    def __init__(self, return_code=0):
        self.return_code = return_code
        self.finished = False
        self.terminated = False

    def wait(self, timeout=None):
        self.finished = True
        return self.return_code

    def poll(self):
        return self.return_code if self.finished or self.terminated else None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.return_code = -9
        self.terminated = True


def test_run_secure_tunnel_supervises_loopback_server_and_client(tmp_path):
    settings = SecureTunnelSettings(
        enabled=True, tunnel_id=VALID_TUNNEL_ID, api_key="runtime-secret"
    )
    servers = []
    spawned = []

    def server_factory(fast_mcp):
        server = FakeServer(fast_mcp)
        servers.append(server)
        return server

    def popen(command, **kwargs):
        process = FakeProcess()
        spawned.append((command, kwargs, process))
        return process

    marker = object()
    exit_code = run_secure_tunnel(
        marker,
        settings,
        cache_dir=tmp_path,
        binary_manager=FakeBinaryManager(),
        server_factory=server_factory,
        popen_factory=popen,
    )

    command, kwargs, _ = spawned[0]
    assert exit_code == 0
    assert servers[0].fast_mcp is marker
    assert servers[0].started and servers[0].stopped
    assert command[0] == "/managed/tunnel-client"
    assert kwargs["env"]["CONTROL_PLANE_API_KEY"] == "runtime-secret"
    assert kwargs["env"]["MCP_SERVER_URL"] == servers[0].url


def test_main_keeps_stdio_as_default(monkeypatch, mocker):
    monkeypatch.delenv("OPENAI_SECURE_TUNNEL_ENABLED", raising=False)
    run = mocker.patch.object(main_module.mcp, "run")
    tunnel = mocker.patch.object(main_module, "run_secure_tunnel")

    main_module.main()

    run.assert_called_once_with()
    tunnel.assert_not_called()


def test_main_uses_tunnel_when_enabled(monkeypatch, mocker):
    monkeypatch.setenv("OPENAI_SECURE_TUNNEL_ENABLED", "true")
    monkeypatch.setenv("CONTROL_PLANE_TUNNEL_ID", VALID_TUNNEL_ID)
    monkeypatch.setenv("CONTROL_PLANE_API_KEY", "runtime-secret")
    run = mocker.patch.object(main_module.mcp, "run")
    tunnel = mocker.patch.object(main_module, "run_secure_tunnel", return_value=0)

    main_module.main()

    run.assert_not_called()
    settings = tunnel.call_args.args[1]
    assert settings.enabled
    assert settings.tunnel_id == VALID_TUNNEL_ID
