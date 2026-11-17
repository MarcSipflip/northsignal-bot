"""Safety net for the forecasting bot, run by cron on a server that is always on.

The bot keeps time inside its GitHub repository (see timer.py). If both of its timers were lost
at the same moment, for example in a long GitHub outage, nothing on GitHub would restart them.
This script asks GitHub when the last forecasting run started. If none started for a while and
none is under way, it starts one. Every forecasting run restarts the timers as its last step.

It is one file and needs only Python 3 and a GitHub token for the repository: a fine-grained
token limited to this repository, with the permission "Actions: Read and write".
deploy.py installs it on a server over SSH.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

REPO = "MarcSipflip/northsignal-bot"
FORECAST_WORKFLOW = "run_bot_on_tournament.yaml"
TIMER_WORKFLOW = "timer.yaml"
API = "https://api.github.com"
QUIET_MINUTES = 35  # the timers start a forecasting run about every 15 minutes: two missed runs plus a margin
STUCK_MINUTES = 90  # same limit as FORECAST_STUCK_SECONDS in timer.py
LOG_LINES_KEPT = 2000  # about three weeks at one check every 15 minutes
HERE = Path(__file__).resolve().parent


class GitHubError(RuntimeError):
    """GitHub could not be reached, refused a request, or is not set up as expected."""


class GitHub:
    """The few GitHub REST calls the watchdog needs."""

    def __init__(self, repo: str, token: str, urlopen=urllib.request.urlopen) -> None:
        self.repo = repo
        self.token = token
        self.urlopen = urlopen

    def request(self, method: str, path: str, body: dict | None = None) -> dict:
        if method == "GET":
            data = None
        else:
            # GitHub wants a length on every POST and PUT, so one without content sends an empty body.
            data = json.dumps(body).encode() if body is not None else b""
        request = urllib.request.Request(
            f"{API}/repos/{self.repo}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "northsignal-watchdog",
            },
        )
        if body is not None:
            request.add_header("Content-Type", "application/json")
        try:
            with self.urlopen(request, timeout=30) as response:
                text = response.read().decode("utf-8")
        except urllib.error.HTTPError as error:
            raise GitHubError(
                f"GitHub answered {error.code} to {method} {path or '/'}: {_message(error)}{_hint(error.code)}"
            ) from None
        except OSError as error:  # no network, no answer in time, name not found
            raise GitHubError(f"could not reach GitHub: {getattr(error, 'reason', error)}") from None
        return json.loads(text) if text.strip() else {}

    def workflows(self) -> list[dict]:
        return self.request("GET", "/actions/workflows?per_page=100")["workflows"]

    def forecast_runs(self) -> list[dict]:
        return self.request("GET", f"/actions/workflows/{FORECAST_WORKFLOW}/runs?per_page=20")["workflow_runs"]

    def enable(self, workflow: str) -> None:
        self.request("PUT", f"/actions/workflows/{workflow}/enable")

    def cancel(self, run_id: int) -> None:
        self.request("POST", f"/actions/runs/{run_id}/cancel")

    def start(self, workflow: str) -> None:
        branch = self.request("GET", "")["default_branch"]
        self.request("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": branch})


def _message(error: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(error.read().decode("utf-8"))["message"])
    except (ValueError, KeyError, TypeError, OSError):
        return str(error.reason)


def _hint(code: int) -> str:
    if code == 401:
        return " (the token was refused: it may have expired or been revoked)"
    if code in (403, 404):
        return " (the token may lack the permission Actions: Read and write on this repository)"
    return ""


@dataclass
class Decision:
    start: bool = False
    enable: bool = False  # GitHub switched forecasting off for inactivity: switch it back on first
    cancel: list[int] = field(default_factory=list)  # runs that never finished and would keep the new one waiting
    reason: str = ""


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def decide(
    workflows: list[dict],
    runs: list[dict],
    now: datetime,
    quiet_minutes: float = QUIET_MINUTES,
    stuck_minutes: float = STUCK_MINUTES,
) -> Decision:
    """Decides what to do from GitHub's answers. It sends nothing itself."""
    states = {workflow["path"].rsplit("/", 1)[-1]: workflow["state"] for workflow in workflows}
    if FORECAST_WORKFLOW not in states:
        raise GitHubError(f"the repository has no workflow file named {FORECAST_WORKFLOW}")
    for name in (FORECAST_WORKFLOW, TIMER_WORKFLOW):
        if states.get(name) == "disabled_manually":
            return Decision(reason=f"paused: {name} was switched off by hand, so nothing is started")

    newest = None  # minutes since the newest run was created
    under_way = None  # the same for the newest run that has not finished and is not stuck
    stuck = []
    for run in runs:
        age = (now - parse_time(run["created_at"])).total_seconds() / 60
        newest = age if newest is None else min(newest, age)
        if run["status"] != "completed":
            if age > stuck_minutes:
                stuck.append(run["id"])
            else:
                under_way = age if under_way is None else min(under_way, age)

    if under_way is not None:
        return Decision(reason=f"healthy: a forecasting run is under way, started {int(under_way)} min ago")
    if newest is not None and newest < quiet_minutes:
        return Decision(reason=f"healthy: the last forecasting run started {int(newest)} min ago")
    if newest is None:
        reason = "no forecasting run was found"
    else:
        reason = f"no forecasting run has started for {int(newest)} min"
    return Decision(start=True, enable=states[FORECAST_WORKFLOW] == "disabled_inactivity", cancel=stuck, reason=reason)


def check(
    github: GitHub,
    now: datetime,
    quiet_minutes: float = QUIET_MINUTES,
    stuck_minutes: float = STUCK_MINUTES,
    dry_run: bool = False,
) -> tuple[int, str]:
    """One check. Returns the exit code and the text for the log."""
    try:
        decision = decide(github.workflows(), github.forecast_runs(), now, quiet_minutes, stuck_minutes)
        if not decision.start:
            return 0, decision.reason
        if dry_run:
            return 0, f"would repair: {decision.reason} (dry run, nothing was started)"
        if decision.enable:
            github.enable(FORECAST_WORKFLOW)
        notes = ""
        for run_id in decision.cancel:
            try:
                github.cancel(run_id)
            except GitHubError as error:
                notes += f"; could not cancel the stuck run {run_id}: {error}"
        github.start(FORECAST_WORKFLOW)
        return 0, f"repaired: {decision.reason}, so one was started{notes}"
    except GitHubError as error:
        return 1, f"error: {error}"
    except (KeyError, TypeError, ValueError) as error:
        return 1, f"error: GitHub's answer was not in the expected form ({type(error).__name__}: {error})"


def write_log(path: Path, line: str, kept: int = LOG_LINES_KEPT) -> None:
    """Adds the line and keeps only the newest ones. The file is rewritten in place, so its folder need not be writable."""
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    path.write_text("\n".join((lines + [line])[-kept:]) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None, now: datetime | None = None, urlopen=urllib.request.urlopen) -> int:
    parser = argparse.ArgumentParser(description="Start a forecasting run when the bot on GitHub has gone quiet.")
    parser.add_argument("--repo", default=REPO, help="owner/name of the bot's repository")
    parser.add_argument("--token-file", type=Path, default=HERE / "github-token", help="file that holds the GitHub token")
    parser.add_argument("--log", type=Path, default=HERE / "watchdog.log", help="file each check adds one line to")
    parser.add_argument("--quiet-minutes", type=float, default=QUIET_MINUTES, help="how long without a new run counts as quiet")
    parser.add_argument("--dry-run", action="store_true", help="say what would be done without starting anything")
    args = parser.parse_args(argv)
    now = now or datetime.now(timezone.utc)

    try:
        token = args.token_file.read_text(encoding="utf-8").strip()
    except OSError:
        token = ""
    if token:
        code, message = check(GitHub(args.repo, token, urlopen), now, args.quiet_minutes, dry_run=args.dry_run)
    else:
        code, message = 1, f"error: no GitHub token could be read from {args.token_file}"

    line = f"{now:%Y-%m-%d %H:%M:%S}Z  {message}"
    print(line)
    try:
        write_log(args.log, line)
    except OSError as error:
        print(f"Could not write {args.log}: {error}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
