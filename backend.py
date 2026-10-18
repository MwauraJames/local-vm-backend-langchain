"""Deep Agents sandbox backend that runs inside a local VirtualBox VM over SSH.

Two classes:

* SSHSandbox        - the actual Deep Agents backend. It subclasses BaseSandbox and
                      only implements the four things BaseSandbox needs:
                      execute(), upload_files(), download_files() and id.
                      It knows nothing about VirtualBox, so it works with any
                      machine you can SSH into.
* VirtualBoxSandbox - SSHSandbox plus VM lifecycle: restore the golden snapshot,
                      boot headless, wait for SSH, and power off on close().
"""

from __future__ import annotations

import errno
import logging
import posixpath
import shlex
import socket
import stat
import subprocess
import time
from datetime import datetime, timezone
from uuid import uuid4

import paramiko
from deepagents.backends.protocol import (
    FILE_NOT_FOUND,
    INVALID_PATH,
    IS_DIRECTORY,
    PERMISSION_DENIED,
    ExecuteResponse,
    FileDownloadResponse,
    FileUploadResponse,
)
from deepagents.backends.sandbox import BaseSandbox

logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

def _collapse_progress(text: str) -> str:
    """Keep only the final state of lines a progress bar redrew with a lone "\\r".

    A "\\r" right before "\\n" (CRLF line endings) is real content and is kept.
    """
    if "\r" not in text:
        return text
    lines = []
    for line in text.split("\n"):
        if "\r" in line:
            crlf = line.endswith("\r")
            body = line[:-1] if crlf else line
            line = body.rsplit("\r", 1)[-1] + ("\r" if crlf else "")
        lines.append(line)
    return "\n".join(lines)


class SSHSandbox(BaseSandbox):
    """BaseSandbox implementation that talks to a machine over SSH/SFTP."""

    MAX_OUTPUT_BYTES = 1_000_000  # hard cap per command; Deep Agents trims what the LLM sees

    def __init__(
        self,
        host: str,
        port: int,
        username: str,
        password: str | None = None,
        key_path: str | None = None,
        workspace_dir: str | None = None,
        default_timeout: int = 300,
        connect_timeout: int = 180,
        audit_log_path: str | None = "agent_audit.log",
    ) -> None:
        super().__init__()
        self._id = uuid4().hex[:8]
        self.workspace_dir = workspace_dir or f"/home/{username}/workspace"
        self.default_timeout = default_timeout
        self.audit_log_path = audit_log_path
        self.ssh: paramiko.SSHClient | None = None

        self._connect(host, port, username, password, key_path, connect_timeout)

        # Make sure the workspace exists. execute() cd's into it, so it must exist first.
        _, code, _, _ = self._run_remote(f"mkdir -p {shlex.quote(self.workspace_dir)}", timeout=30)
        if code != 0:
            self.close()
            raise RuntimeError(f"Could not create workspace {self.workspace_dir}")
        print(f"Sandbox ready: {self.workspace_dir}")

    # ------------------------------------------------------------------ connection

    def _connect(self, host, port, username, password, key_path, connect_timeout) -> None:
        """Retry until sshd answers (the VM may still be booting); fail fast on bad credentials."""
        deadline = time.monotonic() + connect_timeout
        attempt = 0
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            attempt += 1
            client = paramiko.SSHClient()
            # Fine for a throwaway local VM. For anything remote, load known_hosts instead.
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                client.connect(
                    hostname=host,
                    port=port,
                    username=username,
                    password=password,
                    key_filename=key_path,
                    look_for_keys=password is None,
                    allow_agent=password is None,
                    timeout=5,
                    banner_timeout=10,
                    auth_timeout=10,
                )
            except paramiko.AuthenticationException as exc:
                client.close()
                raise PermissionError(f"SSH authentication failed for '{username}'. Check the password or key.") from exc
            except Exception as exc:  # sshd not up yet, connection refused, banner timeout...
                client.close()
                last_error = exc
                print(f"SSH not ready yet (attempt {attempt}). Retrying in 3s...")
                time.sleep(3)
                continue
            client.get_transport().set_keepalive(30)
            self.ssh = client
            print("SSH connection established.")
            return
        raise ConnectionError(f"SSH to {host}:{port} did not come up within {connect_timeout}s") from last_error

    def close(self) -> None:
        """Close the SSH connection. Safe to call more than once."""
        ssh, self.ssh = getattr(self, "ssh", None), None
        if ssh is not None:
            try:
                ssh.close()
            except Exception:
                pass

    def __enter__(self) -> "SSHSandbox":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ------------------------------------------------------------------ BaseSandbox: id

    @property
    def id(self) -> str:
        return self._id

    # ------------------------------------------------------------------ BaseSandbox: execute

    def _run_remote(self, remote_command: str, timeout: int) -> tuple[bytes, int | None, bool, bool]:
        """Run one command on its own SSH channel.

        Returns (output, exit_code, truncated, timed_out). stderr is merged into stdout on
        the channel itself, so there is no pipe whose exit status could hide the real one.
        """
        transport = self.ssh.get_transport()
        chan = transport.open_session()
        chan.set_combine_stderr(True)
        chan.settimeout(1.0)
        chan.exec_command(remote_command)
        chan.shutdown_write()  # stdin = EOF, so anything that waits for input fails instead of hanging

        # Safety net in case the remote `timeout` wrapper itself never fires.
        deadline = time.monotonic() + timeout + 15
        buf = bytearray()
        truncated = False
        timed_out = False
        while True:
            try:
                data = chan.recv(32768)
            except socket.timeout:
                if time.monotonic() > deadline:
                    timed_out = True
                    break
                continue
            if not data:  # EOF: the command finished and closed its output
                break
            room = self.MAX_OUTPUT_BYTES - len(buf)
            if room > 0:
                buf.extend(data[:room])
            if len(data) > room:
                truncated = True
            if time.monotonic() > deadline:
                timed_out = True
                break

        exit_code: int | None = None
        if not timed_out:
            chan.status_event.wait(10)
            if chan.exit_status_ready():
                exit_code = chan.recv_exit_status()
        chan.close()
        return bytes(buf), exit_code, truncated, timed_out

    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse:
        limit = timeout if timeout and timeout > 0 else self.default_timeout

        script = (
            f"cd {shlex.quote(self.workspace_dir)} || exit 1\n"
            "set -a; [ -f .env ] && . ./.env; set +a\n"  # secrets live in the VM, not in the agent's prompt
            f"{command}\n"
        )
        remote = f"timeout --kill-after=5 {int(limit)} bash -c {shlex.quote(script)}"

        started = time.monotonic()
        raw, exit_code, truncated, timed_out = self._run_remote(remote, limit)
        text = _collapse_progress(raw.decode("utf-8", errors="replace"))

        if timed_out or exit_code == 124:
            text += f"\n[Command timed out after {limit}s]"
            exit_code = 124
        if truncated:
            text += f"\n[Output cut off after {self.MAX_OUTPUT_BYTES} bytes]"

        self._audit(command, exit_code, time.monotonic() - started)
        return ExecuteResponse(output=text, exit_code=exit_code, truncated=truncated)

    def _audit(self, command: str, exit_code: int | None, seconds: float) -> None:
        """Log on the host. A log kept inside the VM could be edited by the agent."""
        if not self.audit_log_path:
            return
        stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        short = command if len(command) <= 300 else command[:300] + "..."
        with open(self.audit_log_path, "a", encoding="utf-8") as log:
            log.write(f"{stamp}\texit={exit_code}\t{seconds:.1f}s\t{short!r}\n")

    # ------------------------------------------------------------------ BaseSandbox: files

    @staticmethod
    def _resolve(path: str) -> str:
        """The protocol requires absolute paths: anything else is reported as invalid_path."""
        if not path or "\x00" in path or not path.startswith("/"):
            raise ValueError("invalid path")
        return posixpath.normpath(path)

    @staticmethod
    def _error_code(exc: OSError) -> str:
        if exc.errno == errno.ENOENT:
            return FILE_NOT_FOUND
        if exc.errno in (errno.EACCES, errno.EPERM):
            return PERMISSION_DENIED
        if exc.errno == errno.EISDIR:
            return IS_DIRECTORY
        return str(exc) or exc.__class__.__name__

    @staticmethod
    def _mkdir_p(sftp: paramiko.SFTPClient, directory: str) -> None:
        if directory in ("", "/"):
            return
        try:
            sftp.stat(directory)
            return
        except FileNotFoundError:
            pass
        SSHSandbox._mkdir_p(sftp, posixpath.dirname(directory))
        try:
            sftp.mkdir(directory)
        except OSError:
            sftp.stat(directory)  # lost a race with another writer? then it exists and this passes

    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]:
        # One SFTP session per call: no shared client state if tool calls run in parallel.
        try:
            sftp = self.ssh.open_sftp()
        except Exception as exc:
            return [FileUploadResponse(path=path, error=str(exc)) for path, _ in files]

        results: list[FileUploadResponse] = []
        try:
            for path, content in files:
                try:
                    remote = self._resolve(path)
                    self._mkdir_p(sftp, posixpath.dirname(remote))
                    with sftp.file(remote, "wb") as handle:
                        handle.write(content)
                    results.append(FileUploadResponse(path=path, error=None))
                except ValueError:
                    results.append(FileUploadResponse(path=path, error=INVALID_PATH))
                except OSError as exc:
                    results.append(FileUploadResponse(path=path, error=self._error_code(exc)))
                except Exception as exc:
                    results.append(FileUploadResponse(path=path, error=str(exc)))
        finally:
            sftp.close()
        return results

    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]:
        try:
            sftp = self.ssh.open_sftp()
        except Exception as exc:
            return [FileDownloadResponse(path=path, content=None, error=str(exc)) for path in paths]

        results: list[FileDownloadResponse] = []
        try:
            for path in paths:
                try:
                    remote = self._resolve(path)
                    if stat.S_ISDIR(sftp.stat(remote).st_mode or 0):
                        results.append(FileDownloadResponse(path=path, content=None, error=IS_DIRECTORY))
                        continue
                    with sftp.file(remote, "rb") as handle:
                        results.append(FileDownloadResponse(path=path, content=handle.read(), error=None))
                except ValueError:
                    results.append(FileDownloadResponse(path=path, content=None, error=INVALID_PATH))
                except OSError as exc:
                    results.append(FileDownloadResponse(path=path, content=None, error=self._error_code(exc)))
                except Exception as exc:
                    results.append(FileDownloadResponse(path=path, content=None, error=str(exc)))
        finally:
            sftp.close()
        return results


class VirtualBoxSandbox(SSHSandbox):
    """SSHSandbox that also manages the VirtualBox VM around it."""

    def __init__(
        self,
        vm_name: str,
        host: str = "127.0.0.1",
        port: int = 2222,
        username: str = "james",
        password: str | None = None,
        key_path: str | None = None,
        snapshot: str = "Agent-Golden-Image",
        **kwargs,
    ) -> None:
        self.vm_name = vm_name
        self.snapshot = snapshot
        self._vm_up = False

        print(f"Resetting VM '{vm_name}' to snapshot '{snapshot}'...")
        self._poweroff()
        time.sleep(1)  # let VirtualBox release the VM's lock before restoring
        self._vbox("snapshot", vm_name, "restore", snapshot)
        print("Booting VM...")
        self._vbox("startvm", vm_name, "--type", "headless")
        self._vm_up = True

        try:
            super().__init__(host=host, port=port, username=username, password=password, key_path=key_path, **kwargs)
        except BaseException:
            self._poweroff()  # never leave a half-started VM running if setup fails
            raise

    @staticmethod
    def _vbox(*args: str) -> None:
        result = subprocess.run(["VBoxManage", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"VBoxManage {' '.join(args)} failed: {result.stderr.strip() or result.stdout.strip()}")

    def _poweroff(self) -> None:
        subprocess.run(
            ["VBoxManage", "controlvm", self.vm_name, "poweroff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,  # already off is fine
        )
        self._vm_up = False

    def close(self) -> None:
        """Close SSH, then power the VM off so the next run restores a clean snapshot."""
        super().close()
        if self._vm_up:
            print("Powering off VM (changes are discarded on the next restore)...")
            self._poweroff()
            print("Sandbox cleanup complete.")

    cleanup = close  # keeps the old method name working