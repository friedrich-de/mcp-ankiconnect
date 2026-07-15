"""Managed runtime bootstrap and process supervision for the Anki add-on.

This module uses only Python's standard library so it can run inside Anki while
the MCP server and its dependencies remain in a separate Python process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import http.client
import os
from pathlib import Path
import platform
import re
import signal
import subprocess
import tarfile
import threading
import time
from typing import Any, IO, Mapping
import urllib.error
import urllib.parse
import urllib.request
import zipfile


TUNNEL_ID_RE = re.compile(r"^tunnel_[0-9a-f]{32}$")
ENABLED_KEY = "OPENAI_SECURE_TUNNEL_ENABLED"
TUNNEL_ID_KEY = "CONTROL_PLANE_TUNNEL_ID"
API_KEY = "CONTROL_PLANE_API_KEY"

UV_VERSION = "0.11.28"
UV_RELEASE_BASE_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}"
_DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class LauncherError(RuntimeError):
    """An error that should be shown to the user."""


class ConfigurationError(LauncherError):
    """Raised when the add-on configuration is incomplete or invalid."""


class SetupCancelled(LauncherError):
    """Raised internally when Anki starts shutting down during setup."""


@dataclass(frozen=True)
class LaunchSettings:
    tunnel_id: str
    api_key: str = field(repr=False)

    @classmethod
    def from_config(
        cls, config: Mapping[str, object] | None
    ) -> "LaunchSettings | None":
        config = config or {}
        tunnel_id = config.get(TUNNEL_ID_KEY, "")
        api_key = config.get(API_KEY, "")

        if (
            isinstance(tunnel_id, str)
            and isinstance(api_key, str)
            and not tunnel_id.strip()
            and not api_key.strip()
        ):
            return None
        if not isinstance(tunnel_id, str) or not TUNNEL_ID_RE.fullmatch(
            tunnel_id.strip()
        ):
            raise ConfigurationError(
                f"{TUNNEL_ID_KEY} must be 'tunnel_' followed by 32 lowercase "
                "hexadecimal characters."
            )
        if not isinstance(api_key, str) or not api_key.strip():
            raise ConfigurationError(f"{API_KEY} must be a nonempty runtime API key.")
        return cls(tunnel_id=tunnel_id.strip(), api_key=api_key.strip())


@dataclass(frozen=True)
class UvArtifact:
    system: str
    target: str
    archive_name: str
    archive_member: str
    archive_sha256: str

    @property
    def url(self) -> str:
        return f"{UV_RELEASE_BASE_URL}/{self.archive_name}"

    @property
    def executable_name(self) -> str:
        return "uv.exe" if self.system == "windows" else "uv"


def _artifact(
    system: str,
    target: str,
    archive_sha256: str,
    archive_member: str,
) -> UvArtifact:
    extension = ".zip" if system == "windows" else ".tar.gz"
    return UvArtifact(
        system=system,
        target=target,
        archive_name=f"uv-{target}{extension}",
        archive_member=archive_member,
        archive_sha256=archive_sha256,
    )


UV_ARTIFACTS: Mapping[tuple[str, str], UvArtifact] = {
    ("windows", "x86_64"): _artifact(
        "windows",
        "x86_64-pc-windows-msvc",
        "0a23463216d09c6a72ff80ef5dc5a795f07dc1575cb84d24596c2f124a441b7b",
        "uv.exe",
    ),
    ("windows", "aarch64"): _artifact(
        "windows",
        "aarch64-pc-windows-msvc",
        "3248109afad3ec59baad299d324ff53de17e2d9a3b3e21580ffd26744b11e036",
        "uv.exe",
    ),
    ("macos", "x86_64"): _artifact(
        "macos",
        "x86_64-apple-darwin",
        "2ad79983127ffca7d77b77ce6a24278d7e4f7b817a1acf72fea5f8124b4aac5e",
        "uv-x86_64-apple-darwin/uv",
    ),
    ("macos", "aarch64"): _artifact(
        "macos",
        "aarch64-apple-darwin",
        "33540eb7c883ab857eff79bd5ac2aa31fe27b595abecb4a9c003a2c998447232",
        "uv-aarch64-apple-darwin/uv",
    ),
    ("linux", "x86_64"): _artifact(
        "linux",
        "x86_64-unknown-linux-gnu",
        "e490a6464492183c5d4534a5527fb4440f7f2bb2f228162ad7e4afe076dc0224",
        "uv-x86_64-unknown-linux-gnu/uv",
    ),
    ("linux", "aarch64"): _artifact(
        "linux",
        "aarch64-unknown-linux-gnu",
        "03e9fe0a81b0718d0bc84625de3885df6cc3f89a8b6af6121d6b9f6113fb6533",
        "uv-aarch64-unknown-linux-gnu/uv",
    ),
}


def _linux_libc() -> str:
    libc_name, libc_version = platform.libc_ver()
    if libc_name:
        return f"{libc_name} {libc_version}".strip()
    try:
        return os.confstr("CS_GNU_LIBC_VERSION") or ""
    except (AttributeError, OSError, ValueError):
        return ""


def select_uv_artifact() -> UvArtifact:
    systems = {"windows": "windows", "darwin": "macos", "linux": "linux"}
    architectures = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "x64": "x86_64",
        "aarch64": "aarch64",
        "arm64": "aarch64",
    }
    system_name = platform.system()
    machine_name = platform.machine()
    system = systems.get(system_name.lower())
    architecture = architectures.get(machine_name.lower().replace("-", "_"))
    if system is None:
        raise LauncherError(
            f"Managed uv {UV_VERSION} does not support operating system "
            f"{system_name!r}."
        )
    if architecture is None:
        raise LauncherError(
            f"Managed uv {UV_VERSION} does not support architecture {machine_name!r}."
        )
    if system == "linux":
        libc = _linux_libc().lower()
        if not (
            libc.startswith("glibc")
            or libc.startswith("gnu libc")
            or libc.startswith("gnu c library")
        ):
            description = repr(libc) if libc else "an unknown libc"
            raise LauncherError(
                f"Managed uv {UV_VERSION} supports glibc Linux, not {description}."
            )
    return UV_ARTIFACTS[(system, architecture)]


def probe_uv_executable(path: Path) -> bool:
    options: Any = {}
    if platform.system() == "Windows":
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        completed = subprocess.run(
            [str(path), "--version"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10,
            text=True,
            **options,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and completed.stdout.startswith(f"uv {UV_VERSION}")


class ManagedUvRuntime:
    """Reuse or install the add-on's pinned uv executable."""

    def __init__(
        self,
        runtime_root: Path,
        *,
        cancel_event: threading.Event,
        diagnostic,
        download_timeout: float = 30.0,
    ) -> None:
        self.runtime_root = runtime_root
        self.cancel_event = cancel_event
        self.diagnostic = diagnostic
        self.download_timeout = download_timeout

    def _check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise SetupCancelled(
                "Runtime setup was cancelled because Anki is quitting."
            )

    def managed_executable_path(self, artifact: UvArtifact) -> Path:
        return (
            self.runtime_root
            / "uv"
            / UV_VERSION
            / artifact.target
            / artifact.executable_name
        )

    def resolve(self) -> Path:
        self._check_cancelled()
        artifact = select_uv_artifact()
        destination = self.managed_executable_path(artifact)
        if probe_uv_executable(destination):
            self.diagnostic(f"Using managed uv {UV_VERSION} for {artifact.target}.")
            return destination

        if destination.exists():
            self.diagnostic("The cached managed uv is unusable; replacing it.")
        destination.parent.mkdir(parents=True, exist_ok=True)
        archive_path = destination.with_name(f".{destination.name}.download")
        executable_path = destination.with_name(f".{destination.name}.tmp")
        for temporary_path in (archive_path, executable_path):
            temporary_path.unlink(missing_ok=True)

        self.diagnostic(
            f"Downloading pinned uv {UV_VERSION} for {artifact.target} from "
            f"{artifact.url}."
        )
        try:
            self._download(artifact, archive_path)
            self._check_cancelled()
            self._extract(artifact, archive_path, executable_path)
            if artifact.system != "windows":
                executable_path.chmod(0o755)
            self._check_cancelled()
            os.replace(executable_path, destination)
        except SetupCancelled:
            raise
        except LauncherError:
            raise
        except OSError as error:
            raise LauncherError(
                f"Could not install managed uv {UV_VERSION}: {error}. Anki will "
                "retry on its next restart."
            ) from error
        finally:
            for temporary_path in (archive_path, executable_path):
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass

        if not probe_uv_executable(destination):
            destination.unlink(missing_ok=True)
            raise LauncherError(
                f"Managed uv {UV_VERSION} was installed but could not execute. "
                "Check that user_files is on an executable filesystem."
            )
        self.diagnostic(f"Installed managed uv {UV_VERSION} at {destination}.")
        return destination

    def _download(self, artifact: UvArtifact, destination: Path) -> None:
        if urllib.parse.urlsplit(artifact.url).scheme != "https":
            raise LauncherError("Managed uv downloads must use HTTPS.")
        request = urllib.request.Request(
            artifact.url,
            headers={"User-Agent": "mcp-ankiconnect-addon/0.8.0"},
        )
        digest = hashlib.sha256()
        try:
            response_context = urllib.request.urlopen(
                request, timeout=self.download_timeout
            )
            with response_context as response:
                with destination.open("wb") as output:
                    if urllib.parse.urlsplit(response.geturl()).scheme != "https":
                        raise LauncherError(
                            "Managed uv download redirected away from HTTPS."
                        )
                    while chunk := response.read(_DOWNLOAD_CHUNK_SIZE):
                        self._check_cancelled()
                        output.write(chunk)
                        digest.update(chunk)
        except SetupCancelled:
            raise
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise LauncherError(
                f"Could not download managed uv {UV_VERSION}: {error}. Check "
                "outbound HTTPS access to github.com; Anki will retry on its next "
                "restart."
            ) from error

        received = digest.hexdigest()
        if received != artifact.archive_sha256:
            raise LauncherError(
                "Downloaded uv archive failed SHA-256 verification. Nothing was "
                "installed; Anki will retry on its next restart."
            )
        self.diagnostic("The uv archive checksum is valid; extracting uv.")

    def _copy_member(self, source: IO[bytes], destination: Path) -> None:
        with destination.open("wb") as output:
            while chunk := source.read(_DOWNLOAD_CHUNK_SIZE):
                self._check_cancelled()
                output.write(chunk)

    def _extract(
        self, artifact: UvArtifact, archive_path: Path, destination: Path
    ) -> None:
        try:
            if artifact.system == "windows":
                with zipfile.ZipFile(archive_path) as archive:
                    member = archive.getinfo(artifact.archive_member)
                    if member.is_dir():
                        raise LauncherError(
                            "The expected uv archive member is not a file."
                        )
                    with archive.open(member) as source:
                        self._copy_member(source, destination)
            else:
                with tarfile.open(archive_path, mode="r:gz") as archive:
                    member = archive.getmember(artifact.archive_member)
                    if not member.isfile():
                        raise LauncherError(
                            "The expected uv archive member is not a file."
                        )
                    source = archive.extractfile(member)
                    if source is None:
                        raise LauncherError("Could not read the uv archive member.")
                    with source:
                        self._copy_member(source, destination)
        except (KeyError, zipfile.BadZipFile, tarfile.TarError, EOFError) as error:
            raise LauncherError(
                f"Downloaded uv archive could not be read: {error}"
            ) from error


def find_bundled_project(addon_directory: Path) -> Path:
    required = (
        addon_directory / "pyproject.toml",
        addon_directory / "README.md",
        addon_directory / "mcp_ankiconnect" / "__init__.py",
    )
    if any(not path.is_file() for path in required):
        raise LauncherError(
            "The add-on does not contain its bundled server source. Reinstall it "
            "from the GitHub release."
        )
    return addon_directory.resolve()


def build_command(uv_executable: Path, project_directory: Path) -> list[str]:
    return [
        str(uv_executable),
        "tool",
        "run",
        "--isolated",
        "--python",
        "3.11",
        "--managed-python",
        "--from",
        str(project_directory),
        "--no-progress",
        "--color",
        "never",
        "mcp-ankiconnect",
    ]


def build_environment(settings: LaunchSettings, runtime_root: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment[ENABLED_KEY] = "true"
    environment[TUNNEL_ID_KEY] = settings.tunnel_id
    environment[API_KEY] = settings.api_key
    environment["UV_CACHE_DIR"] = str(runtime_root / "cache")
    environment["UV_PYTHON_INSTALL_DIR"] = str(runtime_root / "python")
    return environment


class ServerLauncher:
    """Bootstrap the runtime once and own the server's process tree."""

    def __init__(self, addon_directory: Path) -> None:
        self.addon_directory = addon_directory.resolve()
        self.system = platform.system()
        self.process: Any | None = None
        self.process_group_id: int | None = None
        self.started = False
        self._start_in_progress = False
        self._stopping = False
        self._shutdown = threading.Event()
        self._state_lock = threading.RLock()
        self._log_lock = threading.Lock()

    @property
    def log_path(self) -> Path:
        return self.addon_directory / "user_files" / "server.log"

    @property
    def runtime_root(self) -> Path:
        return self.addon_directory / "user_files" / "runtime"

    def _prepare_log(self) -> None:
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self.log_path.write_bytes(b"")
        except OSError as error:
            raise LauncherError(
                f"Could not create the diagnostic log at {self.log_path}: {error}"
            ) from error

    def _diagnostic(self, message: str) -> None:
        line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        try:
            with self._log_lock, self.log_path.open("a", encoding="utf-8") as log_file:
                log_file.write(line)
        except OSError:
            pass

    def _check_cancelled(self) -> None:
        if self._shutdown.is_set():
            raise SetupCancelled("Server setup was cancelled because Anki is quitting.")

    def start(self, config: Mapping[str, object] | None) -> bool:
        """Bootstrap and launch once from Anki's background task."""

        with self._state_lock:
            if self.started or self._start_in_progress or self._shutdown.is_set():
                return False
            self._start_in_progress = True

        try:
            settings = LaunchSettings.from_config(config)
            if settings is None:
                return False
            self._prepare_log()
            self._diagnostic("Starting MCP AnkiConnect runtime setup.")
            project_directory = find_bundled_project(self.addon_directory)
            uv_executable = ManagedUvRuntime(
                self.runtime_root,
                cancel_event=self._shutdown,
                diagnostic=self._diagnostic,
            ).resolve()
            self._check_cancelled()
            command = build_command(uv_executable, project_directory)
            environment = build_environment(settings, self.runtime_root)

            process_options: Any = {}
            if self.system == "Windows":
                process_options["creationflags"] = getattr(
                    subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200
                ) | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            else:
                process_options["start_new_session"] = True

            with self._state_lock:
                self._check_cancelled()
                try:
                    with self.log_path.open("ab") as log_file:
                        process = subprocess.Popen(
                            command,
                            cwd=self.addon_directory,
                            env=environment,
                            stdin=subprocess.DEVNULL,
                            stdout=log_file,
                            stderr=subprocess.STDOUT,
                            **process_options,
                        )
                except (OSError, subprocess.SubprocessError) as error:
                    raise LauncherError(
                        f"Unable to launch the MCP AnkiConnect server: {error}"
                    ) from error
                self.process = process
                if self.system != "Windows":
                    self.process_group_id = process.pid
                self.started = True
            self._diagnostic("MCP AnkiConnect server process started.")
            return True
        except SetupCancelled:
            self._diagnostic("Runtime setup cancelled during Anki shutdown.")
            return False
        except LauncherError as error:
            self._diagnostic(f"Runtime setup failed: {error}")
            raise
        except Exception as error:
            self._diagnostic(f"Unexpected runtime setup failure: {error}")
            raise LauncherError(f"Unexpected runtime setup failure: {error}") from error
        finally:
            with self._state_lock:
                self._start_in_progress = False

    def startup_exit_code(self) -> int | None:
        with self._state_lock:
            process = self.process
        return None if process is None else process.poll()

    def cancel_setup(self) -> None:
        with self._state_lock:
            self._shutdown.set()

    def stop(self, timeout: float = 5.0) -> None:
        self.cancel_setup()
        with self._state_lock:
            if self._stopping:
                return
            process = self.process
            if process is None:
                return
            self._stopping = True

        try:
            if self.system == "Windows":
                self._stop_windows_process_tree(process, timeout)
            else:
                self._stop_posix_process_tree(process, timeout)
        finally:
            with self._state_lock:
                if self.process is process:
                    self.process = None
                    self.process_group_id = None
                self._stopping = False

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    def _stop_posix_process_tree(self, process: Any, timeout: float) -> None:
        process_group_id = self.process_group_id or process.pid
        deadline = time.monotonic() + max(timeout, 0)
        try:
            os.killpg(process_group_id, signal.SIGTERM)
        except ProcessLookupError:
            return

        if process.poll() is None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                pass
        while self._process_group_exists(process_group_id):
            if time.monotonic() >= deadline:
                try:
                    os.killpg(process_group_id, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                break
            time.sleep(0.05)
        if process.poll() is None:
            process.wait(timeout=timeout)

    @staticmethod
    def _taskkill(process: Any, force: bool) -> None:
        command = ["taskkill", "/PID", str(process.pid), "/T"]
        if force:
            command.append("/F")
        subprocess.run(
            command,
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        )

    def _stop_windows_process_tree(self, process: Any, timeout: float) -> None:
        self._taskkill(process, False)
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self._taskkill(process, True)
            process.wait(timeout=timeout)
