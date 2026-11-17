"""Installs server_watchdog.py on a Linux server over SSH, where cron runs it every 15 minutes.

    python watchdog/deploy.py --host root@example.com install             # shows what would be done
    python watchdog/deploy.py --host root@example.com install --apply     # does it
    python watchdog/deploy.py --host root@example.com token FILE --apply  # hands over the GitHub token, then deletes FILE
    python watchdog/deploy.py --host root@example.com remove --apply      # takes everything off the server

SSH to the host must already work with a key, as root. Nothing is changed without --apply.
On the server this creates a user without login (northsignal) that the check runs as, the folder
/opt/northsignal-watchdog (script, token, logs) and the file /etc/cron.d/northsignal-watchdog.
The token is never shown: this program reads FILE and sends its content straight to the server.
"""

from __future__ import annotations

import argparse
import base64
import subprocess
import sys
import textwrap
from pathlib import Path

USER = "northsignal"
FOLDER = "/opt/northsignal-watchdog"
CRON_FILE = "/etc/cron.d/northsignal-watchdog"
SCHEDULE = "4-59/15 * * * *"  # every 15 minutes, off the busy top of the hour
SCRIPT = Path(__file__).resolve().parent / "server_watchdog.py"
CHECK = f"runuser -u {USER} -- /usr/bin/python3 {FOLDER}/server_watchdog.py"
STORE_TOKEN = (
    f"umask 027; cat > {FOLDER}/github-token"
    f" && chown root:{USER} {FOLDER}/github-token && chmod 640 {FOLDER}/github-token"
)
REMOVE = f"rm -f {CRON_FILE}; rm -rf {FOLDER}; userdel {USER} 2>/dev/null || true; echo 'Removed the watchdog.'"


def install_script(source: bytes) -> str:
    """The bash script that installs or updates the watchdog. It can be run again at any time."""
    encoded = "\n".join(textwrap.wrap(base64.b64encode(source.replace(b"\r\n", b"\n")).decode(), 76))
    return f"""set -eu
# The user id comes from a quiet range. useradd alone would pick 999, which many Docker images run as,
# and the files those containers write on the server would then belong to this user.
id -u {USER} >/dev/null 2>&1 || useradd --system -K SYS_UID_MIN=700 -K SYS_UID_MAX=799 \\
  -K SYS_GID_MIN=700 -K SYS_GID_MAX=799 --no-create-home --shell /usr/sbin/nologin {USER}
# The check may read this folder but cannot change the script or the token. It only writes its two logs.
install -d -o root -g {USER} -m 750 {FOLDER}
base64 -d > {FOLDER}/server_watchdog.py <<'END_OF_SCRIPT'
{encoded}
END_OF_SCRIPT
chown root:{USER} {FOLDER}/server_watchdog.py
chmod 640 {FOLDER}/server_watchdog.py
for log in watchdog.log errors.log; do
  touch {FOLDER}/$log
  chown {USER}:{USER} {FOLDER}/$log
  chmod 640 {FOLDER}/$log
done
cat > {CRON_FILE} <<'END_OF_CRON'
# Installed from the northsignal-bot repository (watchdog/deploy.py).
# Checks that the forecasting bot on GitHub still starts runs, and starts one if it has gone quiet.
{SCHEDULE} {USER} /usr/bin/python3 {FOLDER}/server_watchdog.py >/dev/null 2>>{FOLDER}/errors.log
END_OF_CRON
chmod 644 {CRON_FILE}
echo "Installed {FOLDER} and {CRON_FILE}."
"""


def shown(script: str) -> str:
    """The script as printed for the reader: the encoded file is replaced by one line."""
    before, rest = script.split("<<'END_OF_SCRIPT'\n", 1)
    encoded, after = rest.split("\nEND_OF_SCRIPT\n", 1)
    return f"{before}<<'END_OF_SCRIPT'\n(server_watchdog.py, {len(encoded.splitlines())} lines of base64)\nEND_OF_SCRIPT\n{after}"


def read_token(path: Path) -> str:
    raw = path.read_bytes()
    text = raw.decode("utf-16") if b"\x00" in raw else raw.decode("utf-8-sig")  # Notepad saves either
    token = text.strip()
    if not token or not token.isascii() or any(character.isspace() for character in token):
        raise SystemExit(f"{path} should hold the token alone, on one line.")
    return token


def ssh(host: str, command: str, data: bytes = b"", key: str | None = None) -> tuple[int, str]:
    """Runs a command on the server. Returns its exit code and everything it printed."""
    options = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"] + (["-i", key] if key else [])
    result = subprocess.run(["ssh", *options, host, command], input=data, capture_output=True, timeout=180)
    return result.returncode, (result.stdout + result.stderr).decode("utf-8", errors="replace").strip()


def main(argv: list[str] | None = None, run=ssh) -> int:
    parser = argparse.ArgumentParser(description="Install the forecasting bot's watchdog on a server over SSH.")
    parser.add_argument("--host", required=True, help="SSH destination, for example root@example.com")
    parser.add_argument("--key", help="private key file, when it is not one SSH picks by itself")
    parser.add_argument("action", choices=["install", "token", "remove"])
    parser.add_argument("file", nargs="?", type=Path, help="for token: the file that holds the GitHub token")
    parser.add_argument("--apply", action="store_true", help="really do it; without this the steps are only shown")
    args = parser.parse_args(argv)

    def remote(command: str, data: bytes = b"") -> int:
        code, output = run(args.host, command, data, args.key)
        if output:
            print(output)
        return code

    if args.action == "install":
        script = install_script(SCRIPT.read_bytes())
        print(f"On {args.host}, this script is given to bash:\n\n{shown(script)}")
        if not args.apply:
            print("Nothing was changed. Add --apply to do it.")
            return 0
        return remote("bash -s", script.encode())

    if args.action == "remove":
        print(f"On {args.host}: {REMOVE}")
        if not args.apply:
            print("Nothing was changed. Add --apply to do it.")
            return 0
        return remote(REMOVE)

    if args.file is None:
        parser.error("token needs the file that holds the GitHub token")
    token = read_token(args.file)
    looks_right = token.startswith(("github_pat_", "ghp_"))
    print(f"Read a token of {len(token)} characters from {args.file}." + ("" if looks_right else " It does not look like a GitHub token."))
    print(f"On {args.host}: {STORE_TOKEN}   (the token arrives on standard input)")
    print(f"On {args.host}: {CHECK} --dry-run")
    print(f"Then {args.file} is deleted from this computer.")
    if not args.apply:
        print("Nothing was changed. Add --apply to do it.")
        return 0
    if remote(STORE_TOKEN, (token + "\n").encode()) != 0:
        print(f"The token could not be stored on the server. {args.file} was kept.")
        return 1
    if remote(f"{CHECK} --dry-run") != 0:
        print(f"The server could not use the token. {args.file} was kept.")
        return 1
    args.file.unlink()
    print(f"The server can use the token. Deleted {args.file}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
