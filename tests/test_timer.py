"""Offline tests for timer.py: GitHub is replaced by a small simulation, nothing is called."""

from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

import timer
from timer import FORECAST_WORKFLOW, TIMER_WORKFLOW, GhError, Timer

REPO = "owner/bot"
START = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


def stamp(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeGitHub:
    """Stands in for the `gh` command, the clock and `sleep`."""

    def __init__(self, wait_timer_minutes: int | None = 40) -> None:
        self.clock = START
        self.wait_timer_minutes = wait_timer_minutes
        self.states = {TIMER_WORKFLOW: "active", FORECAST_WORKFLOW: "active"}
        self.runs: list[dict] = []
        self.started: list[tuple[str, dict[str, str]]] = []
        self.cancelled: list[int] = []
        self.enabled: list[str] = []
        self.slept: list[float] = []
        self.failing_starts = 0  # how many of the next `gh workflow run` calls fail
        self.listing_fails = False
        self.timer_listings_missing_new_runs = 0  # GitHub can take a moment to list a new run
        self.forecast_run_minutes: float | None = None  # when set, forecasting runs finish by themselves
        self.next_id = 1000

    # -- helpers for arranging a scenario

    def add_run(self, workflow: str, title: str, status: str, minutes_ago: float) -> dict:
        self.next_id += 1
        run = {
            "databaseId": self.next_id,
            "workflow": workflow,
            "displayTitle": title,
            "status": status,
            "createdAt": stamp(self.clock - timedelta(minutes=minutes_ago)),
        }
        self.runs.append(run)
        return run

    def started_workflows(self) -> list[str]:
        return [workflow for workflow, _ in self.started]

    # -- what the Timer is given

    def now(self) -> datetime:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.clock += timedelta(seconds=seconds)

    def gh(self, *args: str) -> str:
        if args[0] == "api":
            return self._api(args[1:])
        if args[:2] == ("run", "list"):
            if self.listing_fails:
                raise GhError("HTTP 502")
            workflow = args[args.index("--workflow") + 1]
            if self.forecast_run_minutes is not None:
                for run in self.runs:
                    finished_at = timer.parse_time(run["createdAt"]) + timedelta(minutes=self.forecast_run_minutes)
                    if run["workflow"] == FORECAST_WORKFLOW and finished_at <= self.clock:
                        run["status"] = "completed"
            hidden = workflow == TIMER_WORKFLOW and self.timer_listings_missing_new_runs > 0
            if hidden:
                self.timer_listings_missing_new_runs -= 1
            runs = [run for run in self.runs if run["workflow"] == workflow and not (hidden and run.get("new"))]
            return json.dumps(runs)
        if args[:2] == ("workflow", "run"):
            if self.failing_starts > 0:
                self.failing_starts -= 1
                raise GhError("HTTP 500")
            workflow = args[2]
            if self.states.get(workflow) != "active":
                raise GhError("HTTP 422: Workflow is disabled")
            fields = dict(pair.split("=", 1) for flag, pair in zip(args, args[1:]) if flag == "-f")
            self.started.append((workflow, fields))
            title = f"Timer {fields['lane']}" if workflow == TIMER_WORKFLOW else "Forecast"
            run = self.add_run(workflow, title, "queued", 0)
            run["new"] = True
            run["delay"] = int(fields.get("delay", "0"))
            return ""
        if args[:2] == ("run", "cancel"):
            self.cancelled.append(int(args[2]))
            for run in self.runs:
                if run["databaseId"] == int(args[2]):
                    run["status"] = "completed"
            return ""
        raise AssertionError(f"unexpected gh call: {args}")

    def _api(self, args: tuple[str, ...]) -> str:
        if args[0] == "--method":
            workflow = args[2].split("/")[-2]
            self.enabled.append(workflow)
            self.states[workflow] = "active"
            return ""
        path = args[0]
        if path.endswith("/environments/timer"):
            if self.wait_timer_minutes is None:
                raise GhError("HTTP 404")
            rules = [{"type": "wait_timer", "wait_timer": self.wait_timer_minutes}] if self.wait_timer_minutes else []
            return json.dumps({"name": "timer", "protection_rules": rules})
        if "/actions/runs/" in path:
            run_id = int(path.rsplit("/", 1)[1])
            return next(run["createdAt"] for run in self.runs if run["databaseId"] == run_id) + "\n"
        if "/actions/workflows/" in path:
            return self.states[path.rsplit("/", 1)[1]] + "\n"
        raise AssertionError(f"unexpected gh api call: {args}")


def make_timer(github: FakeGitHub) -> Timer:
    return Timer(REPO, gh=github.gh, sleep=github.sleep, now=github.now, log=lambda message: None)


class TickTests(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.timer = make_timer(self.github)

    def ticking_run(self, lane: str, minutes_ago: float = 40) -> str:
        """A run of this timer whose wait is over and whose job is now running."""
        return str(self.github.add_run(TIMER_WORKFLOW, f"Timer {lane}", "in_progress", minutes_ago)["databaseId"])

    def test_a_tick_starts_its_successor_and_a_forecasting_run(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)

        self.assertEqual(self.timer.tick("a", run_id), 0)

        self.assertEqual(self.github.started, [(TIMER_WORKFLOW, {"lane": "a"}), (FORECAST_WORKFLOW, {})])
        self.assertEqual(self.github.slept, [], "a run that waited its full period must not wait again")

    def test_the_period_comes_from_the_environment(self) -> None:
        self.assertEqual(make_timer(FakeGitHub(wait_timer_minutes=25)).period_seconds(), 25 * 60)
        self.assertEqual(make_timer(FakeGitHub(wait_timer_minutes=0)).period_seconds(), timer.DEFAULT_PERIOD_SECONDS)
        self.assertEqual(make_timer(FakeGitHub(wait_timer_minutes=None)).period_seconds(), timer.DEFAULT_PERIOD_SECONDS)

    def test_a_run_that_was_not_held_waits_on_the_runner(self) -> None:
        # The wait timer was removed: the job starts 5 seconds after the run was created.
        run_id = self.ticking_run("a", minutes_ago=5 / 60)
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)

        self.timer.tick("a", run_id)

        self.assertEqual(self.github.slept, [40 * 60 - 5], "the pace must hold even without the wait timer")
        self.assertIn(TIMER_WORKFLOW, self.github.started_workflows())

    def test_a_requested_delay_is_waited_before_ticking(self) -> None:
        run_id = self.ticking_run("b")
        self.github.add_run(TIMER_WORKFLOW, "Timer a", "waiting", 20)

        self.timer.tick("b", run_id, delay=1200)

        self.assertEqual(self.github.slept[0], 1200)
        self.assertEqual(self.github.started[0], (TIMER_WORKFLOW, {"lane": "b"}), "the delay is not passed on")

    def test_no_second_forecasting_run_while_one_is_under_way(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        self.github.add_run(FORECAST_WORKFLOW, "Forecast", "in_progress", 3)

        self.timer.tick("a", run_id)

        self.assertEqual(self.github.started_workflows(), [TIMER_WORKFLOW])

    def test_timers_ticking_together_start_one_forecasting_run(self) -> None:
        run_a, run_b = self.ticking_run("a"), self.ticking_run("b")

        self.timer.tick("a", run_a)
        self.timer.tick("b", run_b)

        self.assertEqual(self.github.started_workflows().count(FORECAST_WORKFLOW), 1)
        self.assertIn(timer.TIE_BREAK_SECONDS, self.github.slept, "timer b must look a little later than timer a")

    def test_a_stuck_forecasting_run_is_cancelled_and_replaced(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        stuck = self.github.add_run(FORECAST_WORKFLOW, "Forecast", "queued", 200)

        self.timer.tick("a", run_id)

        self.assertEqual(self.github.cancelled, [stuck["databaseId"]])
        self.assertIn(FORECAST_WORKFLOW, self.github.started_workflows())

    def test_forecasting_paused_by_hand_stays_paused(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        self.github.states[FORECAST_WORKFLOW] = "disabled_manually"

        self.assertEqual(self.timer.tick("a", run_id), 0)

        self.assertEqual(self.github.started_workflows(), [TIMER_WORKFLOW], "the timer keeps ticking, ready to resume")
        self.assertEqual(self.github.enabled, [])

    def test_forecasting_switched_off_for_inactivity_is_switched_back_on(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        self.github.states[FORECAST_WORKFLOW] = "disabled_inactivity"

        self.timer.tick("a", run_id)

        self.assertEqual(self.github.enabled, [FORECAST_WORKFLOW])
        self.assertIn(FORECAST_WORKFLOW, self.github.started_workflows())

    def test_a_timer_paused_by_hand_stops(self) -> None:
        run_id = self.ticking_run("a")
        self.github.states[TIMER_WORKFLOW] = "disabled_manually"

        self.assertEqual(self.timer.tick("a", run_id), 0)

        self.assertEqual(self.github.started, [])

    def test_a_lost_timer_is_restarted_half_a_period_later(self) -> None:
        run_id = self.ticking_run("a")  # there is no run of timer b at all

        self.timer.tick("a", run_id)

        self.assertIn((TIMER_WORKFLOW, {"lane": "b", "delay": "1200"}), self.github.started)
        self.assertIn(timer.CONFIRM_LOST_AFTER_SECONDS, self.github.slept, "a timer is declared lost only after a second look")

    def test_a_timer_missing_from_one_listing_is_not_restarted(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "queued", 0)["new"] = True
        self.github.timer_listings_missing_new_runs = 1

        self.timer.tick("a", run_id)

        self.assertNotIn("b", [fields.get("lane") for _, fields in self.github.started])

    def test_a_stuck_timer_run_is_cancelled_and_restarted(self) -> None:
        run_id = self.ticking_run("a")
        stuck = self.github.add_run(TIMER_WORKFLOW, "Timer b", "queued", 600)

        self.timer.tick("a", run_id)

        self.assertEqual(self.github.cancelled, [stuck["databaseId"]])
        self.assertIn((TIMER_WORKFLOW, {"lane": "b", "delay": "1200"}), self.github.started)

    def test_a_failed_listing_never_restarts_a_timer_blindly(self) -> None:
        run_id = self.ticking_run("a")
        self.github.listing_fails = True

        self.assertEqual(self.timer.tick("a", run_id), 0)

        self.assertEqual(
            self.github.started,
            [(TIMER_WORKFLOW, {"lane": "a"}), (FORECAST_WORKFLOW, {})],
            "own successor and a forecasting run still start; timer b is left alone",
        )

    def test_the_successor_is_retried_until_github_answers(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        self.github.add_run(FORECAST_WORKFLOW, "Forecast", "in_progress", 3)
        self.github.failing_starts = 6  # the three quick tries fail, then three of the long ones

        self.assertEqual(self.timer.tick("a", run_id), 0)

        self.assertEqual(self.github.started, [(TIMER_WORKFLOW, {"lane": "a"})])

    def test_a_tick_that_cannot_start_its_successor_fails_visibly(self) -> None:
        run_id = self.ticking_run("a")
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 20)
        self.github.failing_starts = 10_000

        self.assertEqual(self.timer.tick("a", run_id), 1)

        self.assertGreaterEqual(sum(self.github.slept), sum(timer.REARM_RETRY_DELAYS))


class EnsureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.github = FakeGitHub()
        self.timer = make_timer(self.github)

    def test_both_timers_are_started_half_a_period_apart(self) -> None:
        self.assertEqual(self.timer.ensure(), 0)

        self.assertEqual(
            self.github.started,
            [(TIMER_WORKFLOW, {"lane": "a", "delay": "0"}), (TIMER_WORKFLOW, {"lane": "b", "delay": "1200"})],
        )

    def test_running_timers_are_left_alone(self) -> None:
        self.github.add_run(TIMER_WORKFLOW, "Timer a", "waiting", 10)
        self.github.add_run(TIMER_WORKFLOW, "Timer b", "waiting", 30)

        self.timer.ensure()

        self.assertEqual(self.github.started, [])
        self.assertEqual(self.github.slept, [])

    def test_only_the_lost_timer_is_started(self) -> None:
        self.github.add_run(TIMER_WORKFLOW, "Timer a", "waiting", 10)

        self.timer.ensure()

        self.assertEqual(self.github.started, [(TIMER_WORKFLOW, {"lane": "b", "delay": "1200"})])

    def test_timers_paused_by_hand_stay_off(self) -> None:
        self.github.states[TIMER_WORKFLOW] = "disabled_manually"

        self.assertEqual(self.timer.ensure(), 0)

        self.assertEqual(self.github.started, [])

    def test_ensure_survives_github_errors(self) -> None:
        self.github.listing_fails = True

        self.assertEqual(self.timer.ensure(), 0)

        self.assertEqual(self.github.started, [])


class ChainTests(unittest.TestCase):
    """Runs the simulation forward: do the two timers keep themselves and the bot going?"""

    def test_the_timers_keep_going_and_forecast_twice_per_period(self) -> None:
        github = FakeGitHub()
        github.forecast_run_minutes = 2
        chain = make_timer(github)
        period = timedelta(minutes=40)
        end = START + timedelta(hours=8)
        chain.ensure()  # what the first forecasting run does

        ticks = 0
        while True:
            waiting = [run for run in github.runs if run["workflow"] == TIMER_WORKFLOW and run["status"] != "completed"]
            run = min(waiting, key=lambda run: run["createdAt"])
            due = timer.parse_time(run["createdAt"]) + period  # when the environment lets the job start
            if due > end:
                break
            github.clock = max(github.clock, due)
            self.assertEqual(chain.tick(run["displayTitle"][-1], str(run["databaseId"]), run.get("delay", 0)), 0)
            run["status"] = "completed"
            ticks += 1

        forecasts = [timer.parse_time(run["createdAt"]) for run in github.runs if run["workflow"] == FORECAST_WORKFLOW]
        gaps = [(later - earlier).total_seconds() / 60 for earlier, later in zip(forecasts, forecasts[1:])]
        unfinished = [run for run in github.runs if run["workflow"] == TIMER_WORKFLOW and run["status"] != "completed"]
        self.assertEqual(sorted(run["displayTitle"] for run in unfinished), ["Timer a", "Timer b"])
        self.assertGreaterEqual(len(forecasts), 20, "eight hours at one run per 20 minutes")
        self.assertLessEqual(max(gaps), 21, "the two timers must stay half a period apart")
        self.assertEqual(github.cancelled, [])
        self.assertEqual(ticks, len(forecasts))

    def test_one_timer_carries_on_alone_and_brings_the_other_back(self) -> None:
        github = FakeGitHub()
        chain = make_timer(github)
        lone = github.add_run(TIMER_WORKFLOW, "Timer a", "in_progress", 40)  # timer b was lost

        self.assertEqual(chain.tick("a", str(lone["databaseId"])), 0)

        lanes = sorted(fields["lane"] for workflow, fields in github.started if workflow == TIMER_WORKFLOW)
        self.assertEqual(lanes, ["a", "b"])


if __name__ == "__main__":
    unittest.main()
