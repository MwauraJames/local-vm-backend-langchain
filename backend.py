import paramiko
from uuid import uuid4
import logging
import os
import shlex
import subprocess
import time

logging.getLogger("paramiko.transport").setLevel(logging.CRITICAL)

class VirtualBoxSandbox:
    # 1. Added password parameter, changed default user to 'james'
    def __init__(self, vm_name: str = "LUBUNTU2", host: str = "127.0.0.1", port: int = 2222, username: str = "james", password: str = None, key_path: str = "~/.ssh/id_rsa"):
        self.vm_name = vm_name
        self.sandbox_id = uuid4().hex[:8]
        self.workspace_dir = f"/tmp/vm-sandbox-{self.sandbox_id}"
        
        print(f"Ensuring VM '{self.vm_name}' is stopped...")
        subprocess.run(["VBoxManage", "controlvm", self.vm_name, "poweroff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1)

        print(f"Restoring VM '{self.vm_name}' to Golden Image...")
        subprocess.run(["VBoxManage", "snapshot", self.vm_name, "restore", "Agent-Golden-Image"], check=True)

        print("Booting VM...")
        subprocess.run(["VBoxManage", "startvm", self.vm_name, "--type", "headless"], check=True)

        print("Waiting for VM OS to boot and SSH to come online...")
        key_path = os.path.expanduser(key_path)
        self.ssh = paramiko.SSHClient()
        self.ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        time.sleep(15)
        connected = False
        
        for attempt in range(30):
            try:
                # 2. Pass the password to Paramiko
                self.ssh.connect(
                    hostname=host, 
                    port=port, 
                    username=username, 
                    password=password,                  # <--- Added this
                    key_filename=key_path if not password else None, 
                    timeout=5,
                    banner_timeout=10,
                    auth_timeout=10
                )
                connected = True
                print("SSH Connection Established!")
                break
            except paramiko.AuthenticationException:
                # 3. Fail immediately if credentials are wrong
                raise PermissionError(f"Authentication failed for user '{username}'. Check your password or SSH key.")
            except Exception as e:
                print(f"SSH not ready yet (Attempt {attempt + 1}/30). Waiting 3 seconds...")
                time.sleep(3)
                
        if not connected:
            raise ConnectionError("VM failed to boot, or SSH is unreachable.")

        self.sftp = self.ssh.open_sftp()
        self.ssh.exec_command(f"mkdir -p {self.workspace_dir}")
        self.sftp = self.ssh.open_sftp()
        
        # Add stdout.channel.recv_exit_status() to block until the folder is created
        _, stdout, _ = self.ssh.exec_command(f"mkdir -p {self.workspace_dir}")
        stdout.channel.recv_exit_status()  
        
        print(f"Sandbox Ready: {self.workspace_dir}")
        

    def _clean_and_truncate_output(self, text: str, max_chars: int = 2500) -> str:
        """Strips progress bar carriage returns and keeps only the head and tail."""
        if not text:
            return ""

        # Normalize carriage returns from progress bars (keep only the last state per update)
        lines = []
        for line in text.splitlines():
            # If the line contains progress bar redraws (\r), take the last segment
            clean_line = line.split("\r")[-1].strip()
            if clean_line:
                lines.append(clean_line)

        cleaned_text = "\n".join(lines)

        # If it fits within limits, return as is
        if len(cleaned_text) <= max_chars:
            return cleaned_text

        # Head + Tail Truncation
        head = "\n".join(lines[:10])
        tail = "\n".join(lines[-25:])
        omitted_count = max(0, len(lines) - 35)

        return (
            f"{head}\n\n"
            f"[... {omitted_count} lines / {len(cleaned_text) - max_chars} characters truncated to save context ...]\n\n"
            f"{tail}"
        )

    def execute_command(self, command: str) -> dict:
        """Executes a command with secret injection and output truncation."""
        
        # 1. Properly separate the logging step from the execution step using semicolons
        full_command = (
            f"cd {self.workspace_dir} && "
            f"[ -f .env ] && source .env; "
            f"echo '--- Executing: {command} ---' >> /tmp/agent_audit.log; "
            f"{command} 2>&1 | tee -a /tmp/agent_audit.log"
        )
        
        stdin, stdout, stderr = self.ssh.exec_command(full_command)
        exit_code = stdout.channel.recv_exit_status()

        raw_out = stdout.read().decode("utf-8", errors="replace")
        raw_err = stderr.read().decode("utf-8", errors="replace")

        return {
            "exit_code": exit_code,
            "stdout": self._clean_and_truncate_output(raw_out),
            "stderr": self._clean_and_truncate_output(raw_err)
        }

    def write_file(self, filename: str, content: str) -> dict: # or -> str depending on your return type
        """Writes content to a file in the workspace."""
        file_path = f"{self.workspace_dir}/{filename}"
        
        # Safely extract the parent directory and guarantee it exists
        dir_name = os.path.dirname(file_path)
        _, stdout, _ = self.ssh.exec_command(f"mkdir -p '{dir_name}'")
        stdout.channel.recv_exit_status()
        
        with self.sftp.file(file_path, 'w') as f:
            f.write(content)
            
        # Tell the agent exactly where it is so it doesn't need to run `pwd` or `ls`
        return f"Success: Wrote {len(content)} bytes to {os.path.abspath(file_path)}"
        
    def read_file(self, filename: str) -> str:
        """Reads a file from the workspace."""
        file_path = f"{self.workspace_dir}/{filename}"
        try:
            with self.sftp.file(file_path, 'r') as f:
                return f.read().decode("utf-8")
        except IOError:
            return f"Error: File {filename} not found."

    def list_directory(self, path: str = ".") -> dict:
        """Lists files in the workspace with human-readable permissions and sizes."""
        # -l for list, -a for all (hidden), -h for human-readable sizes
        safe_path = shlex.quote(path)
        return self.execute_command(f"ls -lah {safe_path}")

    def grep_search(self, pattern: str, target: str = ".") -> dict:
        """Searches for a regex pattern recursively across files."""
        # -r for recursive, -n for line numbers, -I to ignore binaries
        safe_pattern = shlex.quote(pattern)
        safe_target = shlex.quote(target)
        return self.execute_command(f"grep -rnI {safe_pattern} {safe_target}")

    def delete_item(self, path: str) -> dict:
        """Permanently deletes a file or directory."""
        safe_path = shlex.quote(path)
        return self.execute_command(f"rm -rf {safe_path}")

    # Add this at the very bottom of the VirtualBoxSandbox class
    def cleanup(self):
        """Tears down the VM immediately when the session ends."""
        try:
            self.sftp.close()
            self.ssh.close()
        except Exception:
            pass
            
        print("Powering off VM to discard changes...")
        # Use check=False so it doesn't crash if the VM is already off
        subprocess.run(["VBoxManage", "controlvm", self.vm_name, "poweroff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
        print("Sandbox Cleanup Complete.")