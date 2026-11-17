"""Keeps time for the forecasting workflow, because GitHub's own schedule proved unreliable.

GitHub's `schedule` trigger did not fire for this repository, and it is widely reported as
hours late on new repositories, while tournament questions stay open for about three hours.
So the repository keeps time itself.

How it works
------------
Two timers, a and b, are runs of the Timer workflow (`.github/workflows/timer.yaml`). A run
first waits in the `timer` environment, whose wait timer holds the job without using a
runner. When the wait is over (`tick`) the run:

1. starts the next run of its own timer,
2. starts a forecasting run, unless one is already under way,
3. restarts the other timer if that one was lost.

The two timers tick half a period apart, so a forecasting run starts about twice per period
and losing one timer does not stop the bot. Every forecasting run makes the same check
(`ensure`), so any forecasting run brings both timers back, whoever started it.

The pace is the wait timer of the `timer` environment (Settings -> Environments -> timer).
To pause the bot, disable the Timer workflow and the forecasting workflow.

Standard library only: the Timer workflow runs this file without installing anything.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Callable, Sequence

TIMER_WORKFLOW = "timer.yaml"
FORECAST_WORKFLOW = "run_bot_on_tournament.yaml"
ENVIRONMENT = "timer"
LANES = ("a", "b")

# Used when the environment's wait timer cannot be read or has been removed.
DEFAULT_PERIOD_SECONDS = 40 * 60
# A run that waited within this margin of a full period is on pace.
PACE_TOLERANCE_SECONDS = 60
QUICK_RETRY_DELAYS = (10, 30)
# About an hour in total, which outlasts most GitHub incidents.
REARM_RETRY_DELAYS = (15, 30, 60, 120, 240, 480, 900, 900, 900)
# A timer is declared lost only after two looks this far apart: a run that is handing over
# to its successor can be missing from a single listing.
CONFIRM_LOST_AFTER_SECONDS = 20
# Timer b looks for a running forecast slightly later than timer a, so two timers that
# happen to tick together start one forecasting run instead of two.
TIE_BREAK_SECONDS = 15
# The forecasting job is capped at 50 minutes; a run still unfinished after this is stuck.
FORECAST_STUCK_SECONDS = 90 * 60


class GhError(RuntimeError):
    """The GitHub CLI reported an error."""


def run_gh(*args: str) -> str:
    """Run the GitHub CLI and return what it printed."""
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise GhError(f"gh {' '.join(args)}: {error}") from error
    if result.returncode != 0:
        raise GhError(f"gh {' '.join(args)}: {result.stderr.strip() or result.stdout.strip()}")
    return result.stdout


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def run_title(lane: str) -> str:
    """The title the Timer workflow gives a run of this timer (its `run-name`)."""
    return f"Timer {lane}"


def log(message: str) -> None:
    print(message, flush=True)


class Timer:
    def __init__(
        self,
        repo: str,
        gh: Callable[..., str] = run_gh,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        log: Callable[[str], None] = log,
    ) -> None:
        self.repo = repo
        self.gh = gh
        self.sleep = sleep
        self.now = now
        self.log = log

    # ------------------------------------------------------------- reading GitHub's state

    def period_seconds(self) -> int:
        """The wait timer of the `timer` environment, which sets the pace."""
        try:
            environment = json.loads(self.gh("api", f"repos/{self.repo}/environments/{ENVIRONMENT}"))
        except (GhError, ValueError):
            return DEFAULT_PERIOD_SECONDS
        minutes = max(
            (
                rule.get("wait_timer") or 0
                for rule in environment.get("protection_rules") or []
                if rule.get("type") == "wait_timer"
            ),
            default=0,
        )
        return minutes * 60 if minutes > 0 else DEFAULT_PERIOD_SECONDS

    def workflow_state(self, workflow: str) -> str:
        try:
            return self.gh("api", f"repos/{self.repo}/actions/workflows/{workflow}", "--jq", ".state").strip()
        except GhError:
            return "unknown"

    def unfinished_runs(self, workflow: str) -> list[dict[str, Any]]:
        listing = self.gh(
            "run", "list", "-R", self.repo, "--workflow", workflow, "--limit", "40",
            "--json", "databaseId,status,displayTitle,createdAt",
        )  # fmt: skip
        return [run for run in json.loads(listing) if run["status"] != "completed"]

    def age_seconds(self, run: dict[str, Any]) -> float:
        return (self.now() - parse_time(run["createdAt"])).total_seconds()

    def timer_runs(self, lane: str, period: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Unfinished runs of one timer, split into those still in time and those stuck."""
        # The longest a healthy run lives: its wait, a half-period shift and an hour of retries.
        stuck_after = 2 * period + sum(REARM_RETRY_DELAYS) + 15 * 60
        runs = [run for run in self.unfinished_runs(TIMER_WORKFLOW) if run["displayTitle"] == run_title(lane)]
        live = [run for run in runs if self.age_seconds(run) <= stuck_after]
        stuck = [run for run in runs if self.age_seconds(run) > stuck_after]
        return live, stuck

    # ---------------------------------------------------------------------------- acting

    def start(
        self,
        workflow: str,
        fields: dict[str, str] | None = None,
        retry_delays: Sequence[int] = QUICK_RETRY_DELAYS,
    ) -> bool:
        command = ["workflow", "run", workflow, "-R", self.repo]
        for name, value in (fields or {}).items():
            command += ["-f", f"{name}={value}"]
        for delay in (*retry_delays, None):
            try:
                self.gh(*command)
                return True
            except GhError as error:
                self.log(f"Could not start {workflow}: {error}")
                if delay is None:
                    break
                self.sleep(delay)
        return False

    def cancel(self, run: dict[str, Any]) -> None:
        try:
            self.gh("run", "cancel", str(run["databaseId"]), "-R", self.repo)
            self.log(f"Cancelled stuck run {run['databaseId']} ({run['displayTitle']}).")
        except GhError as error:
            self.log(f"Could not cancel run {run['databaseId']}: {error}")

    def safely(self, action: Callable[..., Any], *args: Any) -> Any:
        """Run one step without letting its failure stop the steps after it."""
        try:
            return action(*args)
        except Exception as error:  # the timer must keep going whatever one step does
            self.log(f"::warning::{getattr(action, '__name__', 'A step')} failed: {error}")
            return None

    def hold_pace(self, run_id: str, period: int) -> None:
        """Wait here if the environment did not hold this run for a full period.

        If the environment's wait timer were ever removed, runs would start at once and the
        timer would become a tight loop. Waiting on the runner instead keeps the pace.
        """
        started = None
        for attempt in range(3):
            try:
                started = parse_time(
                    self.gh("api", f"repos/{self.repo}/actions/runs/{run_id}", "--jq", ".run_started_at")
                )
                break
            except (GhError, ValueError) as error:
                self.log(f"Could not read when this run started: {error}")
                if attempt < 2:
                    self.sleep(10)
        waited = (self.now() - started).total_seconds() if started else 0
        remaining = period - waited
        if remaining > PACE_TOLERANCE_SECONDS:
            self.log(
                f"::warning::The '{ENVIRONMENT}' environment held this run for {int(waited)} s instead of "
                f"{period} s, so it waits the other {int(remaining)} s on the runner. "
                f"Check Settings > Environments > {ENVIRONMENT} > Wait timer."
            )
            self.sleep(remaining)

    def start_forecast(self) -> None:
        state = self.workflow_state(FORECAST_WORKFLOW)
        if state == "disabled_manually":
            self.log("Forecasting was switched off by hand; not starting a run.")
            return
        if state == "disabled_inactivity":
            self.log("GitHub switched forecasting off for inactivity; switching it back on.")
            try:
                self.gh("api", "--method", "PUT", f"repos/{self.repo}/actions/workflows/{FORECAST_WORKFLOW}/enable")
            except GhError as error:
                self.log(f"Could not switch forecasting back on: {error}")
        try:
            unfinished = self.unfinished_runs(FORECAST_WORKFLOW)
        except (GhError, ValueError) as error:
            self.log(f"Could not list forecasting runs: {error}")
            unfinished = []
        under_way = False
        for run in unfinished:
            if self.age_seconds(run) > FORECAST_STUCK_SECONDS:
                self.cancel(run)
            else:
                under_way = True
        if under_way:
            self.log("A forecasting run is already under way; not starting another.")
        elif self.start(FORECAST_WORKFLOW):
            self.log("Started a forecasting run.")

    def revive_if_lost(self, lane: str, period: int, delay: int) -> bool:
        """Start a timer again when it has no run in time. Returns True when it was started."""
        live, stuck = self.timer_runs(lane, period)
        if not live:
            self.sleep(CONFIRM_LOST_AFTER_SECONDS)
            live, stuck = self.timer_runs(lane, period)
        if live:
            self.log(f"Timer {lane} is running.")
            return False
        for run in stuck:
            self.cancel(run)
        self.log(f"Timer {lane} is not running; starting it.")
        started = self.start(TIMER_WORKFLOW, {"lane": lane, "delay": str(delay)})
        if not started:
            self.log(f"::warning::Timer {lane} could not be started.")
        return started

    # -------------------------------------------------------------------------- commands

    def tick(self, lane: str, run_id: str, delay: int = 0) -> int:
        """What a Timer run does once its wait is over."""
        period = self.period_seconds()
        self.hold_pace(run_id, period)
        delay = max(0, min(delay, period))
        if delay:
            self.log(f"Waiting {delay} s more, so the two timers tick half a period apart.")
            self.sleep(delay)
        if self.workflow_state(TIMER_WORKFLOW) == "disabled_manually":
            self.log("The timer was switched off by hand; stopping here.")
            return 0

        # The successor comes first: everything after it gives GitHub time to list the new run.
        rearmed = self.start(TIMER_WORKFLOW, {"lane": lane})
        if lane != LANES[0]:
            self.sleep(TIE_BREAK_SECONDS)
        self.safely(self.start_forecast)
        for other in LANES:
            if other != lane:
                self.safely(self.revive_if_lost, other, period, period // 2)

        if not rearmed:
            rearmed = self.start(TIMER_WORKFLOW, {"lane": lane}, REARM_RETRY_DELAYS)
        if not rearmed:
            self.log(
                f"::error::Timer {lane} could not start its next run. "
                "The other timer or the next forecasting run will restart it."
            )
            return 1
        self.log(f"Timer {lane} ticks again in about {period // 60} minutes.")
        return 0

    def ensure(self) -> int:
        """Restart any timer that is not running. Called at the end of every forecasting run."""
        if self.workflow_state(TIMER_WORKFLOW) == "disabled_manually":
            self.log("The timer was switched off by hand; leaving it off.")
            return 0
        period = self.period_seconds()
        for lane in LANES:
            # Timer b starts half a period later than timer a when both are started here.
            delay = 0 if lane == LANES[0] else period // 2
            self.safely(self.revive_if_lost, lane, period, delay)
        return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Keeps time for the forecasting workflow.")
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser("tick", help="what a Timer run does once its wait is over")
    tick.add_argument("--lane", choices=LANES, required=True)
    tick.add_argument("--run-id", required=True)
    tick.add_argument("--delay", type=int, default=0, help="extra seconds to wait before ticking")
    commands.add_parser("ensure", help="restart any timer that is not running")
    args = parser.parse_args(argv)

    repo = os.environ.get("GH_REPO") or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        log("Set GH_REPO to the repository, as owner/name.")
        return 2
    timer = Timer(repo)
    if args.command == "tick":
        return timer.tick(args.lane, args.run_id, args.delay)
    return timer.ensure()


if __name__ == "__main__":
    raise SystemExit(main())
