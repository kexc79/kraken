"""Deploy and operate the Ubuntu job using local .env.deploy credentials."""
import argparse
import hashlib
import posixpath
import shlex
import sys
import time
from pathlib import Path, PurePosixPath

import paramiko
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent
ARTIFACTS = (
    "as_3.py", "app_config.py", "check_config.py", "requirements.txt",
    "Dockerfile", "compose.yaml", ".dockerignore", "kraken.sh", "README.md",
    ".env.example", ".req.example",
)


def load_deploy_settings():
    settings = dotenv_values(ROOT / ".env.deploy", interpolate=False, encoding="utf-8-sig")
    for key, aliases in {
        "DEPLOY_HOST": ("vps_ip", "vps_ip6"),
        "DEPLOY_USER": ("vps_user",),
        "DEPLOY_PASSWORD": ("vps_password",),
        "DEPLOY_PORT": ("vps_port",),
    }.items():
        vps_value = next((settings[name] for name in aliases if settings.get(name)), None)
        if vps_value:
            settings[key] = vps_value
    return settings


class RememberHost(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        # Trust on first use, as with an initial interactive SSH connection.
        fingerprint = hashlib.sha256(key.asbytes()).hexdigest()
        print(f"Recording first SSH host key: {key.get_name()} SHA256(hex)={fingerprint}")
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(ROOT / ".deploy" / "known_hosts"))


def connect(settings):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    cache = ROOT / ".deploy"
    cache.mkdir(exist_ok=True)
    if (cache / "known_hosts").exists():
        client.load_host_keys(str(cache / "known_hosts"))
    client.set_missing_host_key_policy(RememberHost())
    client.connect(
        settings["DEPLOY_HOST"], port=int(settings.get("DEPLOY_PORT") or 22),
        username=settings["DEPLOY_USER"], password=settings["DEPLOY_PASSWORD"],
        timeout=20, banner_timeout=20, auth_timeout=20,
        allow_agent=False, look_for_keys=False,
    )
    client.get_transport().set_keepalive(30)
    return client


def execute(client, command, password=None, quiet=False, interactive=False):
    channel = client.get_transport().open_session(timeout=20)
    if interactive:
        # Closing the terminal sends SIGHUP to the remote foreground command.
        # This also cancels launches interrupted during the build/check stages.
        channel.get_pty(term="xterm", width=120, height=40)
    channel.set_combine_stderr(True)
    channel.exec_command(command)
    if password is not None:
        channel.sendall((password + "\n").encode())
    if not interactive:
        channel.shutdown_write()
    collected = bytearray()
    try:
        while True:
            if channel.recv_ready():
                data = channel.recv(32768)
                collected.extend(data)
                if not quiet:
                    sys.stdout.buffer.write(data)
                    sys.stdout.buffer.flush()
            elif channel.exit_status_ready():
                break
            elif channel.closed:
                break
            else:
                time.sleep(0.1)
        code = channel.recv_exit_status()
        return code, collected.decode("utf-8", errors="replace")
    except KeyboardInterrupt:
        if interactive and not channel.closed:
            # Deliver Ctrl+C to the remote terminal's entire foreground group
            # before closing SSH (including an in-progress build/check).
            channel.sendall(b"\x03")
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not channel.exit_status_ready():
                if channel.recv_ready():
                    sys.stdout.buffer.write(channel.recv(32768))
                    sys.stdout.buffer.flush()
                else:
                    time.sleep(0.05)
        raise
    finally:
        channel.close()


def upload(sftp, local, remote, mode):
    # Atomic replacement; restrictive mode is applied before data is uploaded.
    temporary = remote + ".upload"
    with sftp.file(temporary, "w") as target:
        sftp.chmod(temporary, mode)
        target.write(local.read_bytes())
    sftp.posix_rename(temporary, remote)


def exists(sftp, path):
    try:
        sftp.stat(path)
        return True
    except FileNotFoundError:
        return False


def deploy(client, settings, directory, update_config):
    quoted = shlex.quote(directory)
    code, _ = execute(client, f"mkdir -p -- {quoted}", quiet=True)
    if code:
        # Only the explicitly configured application directory needs sudo.
        command = (
            "sudo -S -p '' install -d -m 700 -o \"$(id -u)\" "
            f"-g \"$(id -g)\" -- {quoted}"
        )
        code, _ = execute(client, command, password=settings["DEPLOY_PASSWORD"])
        if code:
            raise RuntimeError("Cannot create DEPLOY_HOME; sudo failed")
    code, _ = execute(client, f"chmod 700 -- {quoted}; mkdir -p -- {quoted}/config {quoted}/logs; chmod 700 -- {quoted}/config {quoted}/logs")
    if code:
        raise RuntimeError("Cannot prepare application directory")
    with client.open_sftp() as sftp:
        for name in ARTIFACTS:
            upload(sftp, ROOT / name, posixpath.join(directory, name), 0o700 if name == "kraken.sh" else 0o600)
        for name in (".env", ".req"):
            destination = posixpath.join(directory, "config", name)
            if update_config or not exists(sftp, destination):
                upload(sftp, ROOT / name, destination, 0o600)
                print(f"Uploaded config/{name} (contents hidden)", flush=True)
            else:
                print(f"Preserved server config/{name}", flush=True)
        credentials = dotenv_values(ROOT / ".env", interpolate=False, encoding="utf-8-sig").get("GSERVICE_JSON")
        if credentials:
            relative = PurePosixPath(credentials)
            local = Path(credentials)
            if not local.is_absolute():
                local = ROOT / local
            if local.is_file() and not relative.is_absolute() and ".." not in relative.parts:
                destination = posixpath.join(directory, "config", credentials)
                parent = posixpath.dirname(destination)
                code, _ = execute(client, "mkdir -p -- " + shlex.quote(parent))
                if code:
                    raise RuntimeError("Cannot create credentials directory")
                if update_config or not exists(sftp, destination):
                    upload(sftp, local, destination, 0o600)
                    print("Uploaded Google credentials (contents hidden)", flush=True)
            elif not local.is_file():
                print("Google JSON is missing locally; place it in server config/ before starting.", flush=True)
    print(f"Deployed to {directory}. Server configuration is preserved on repeat deployments.", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("deploy", "init", "build", "check", "start", "run", "logs", "status", "stop"), nargs="?", default="start")
    parser.add_argument("--update-config", action="store_true", help="Explicitly replace server .env/.req/Google JSON during deploy")
    arguments = parser.parse_args()
    settings = load_deploy_settings()
    required = ("DEPLOY_HOST", "DEPLOY_USER", "DEPLOY_PASSWORD", "DEPLOY_HOME")
    if any(not settings.get(key) for key in required):
        raise RuntimeError("Incomplete .env.deploy")
    directory = posixpath.normpath(settings["DEPLOY_HOME"])
    if not directory.startswith("/") or directory in ("/", "/home", "/opt", "/srv", "/root", "/usr", "/etc", "/var"):
        raise RuntimeError("DEPLOY_HOME must be an absolute application subdirectory")
    client = connect(settings)
    try:
        if arguments.action == "deploy":
            deploy(client, settings, directory, arguments.update_config)
            return 0
        if arguments.update_config:
            parser.error("--update-config is only valid with deploy")
        # Action is constrained by argparse; directory is shell-quoted.
        command = f"cd -- {shlex.quote(directory)} && ./kraken.sh {arguments.action}"
        try:
            code, _ = execute(client, command, interactive=arguments.action in ("start", "run"))
            return code
        except KeyboardInterrupt:
            if arguments.action in ("start", "run"):
                print("\nStopping the VPS task...", flush=True)
                execute(client, f"cd -- {shlex.quote(directory)} && ./kraken.sh stop")
            return 130
    finally:
        client.close()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except (OSError, RuntimeError, ValueError, paramiko.SSHException) as error:
        # Connection exceptions sometimes embed connection parameters.
        print(f"Operation failed ({type(error).__name__}); check configuration, SSH access and server permissions.", file=sys.stderr)
        sys.exit(1)
