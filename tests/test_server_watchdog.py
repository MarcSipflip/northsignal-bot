"""Offline tests for the watchdog (watchdog/server_watchdog.py and watchdog/deploy.py). Nothing is called."""

from __future__ import annotations

import base64
import contextlib
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "watchdog"))

import deploy  # noqa: E402
import server_watchdog  # noqa: E402
from server_watchdog import FORECAST_WORKFLOW, GitHub, GitHubError, check, decide  # noqa: E402

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)
TOKEN = "github_pat_made_up_for_tests"


def run(run_id: int, minutes_ago: float, status: str = "completed") -> dict:
    created = NOW - timedelta(minutes=minutes_ago)
    return {"id": run_id, "status": status, "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ")}


def workflows(forecast: str = "active", timer: str = "active") -> list[dict]:
    return [
        {"path": ".github/workflows/run_bot_on_tournament.yaml", "state": forecast},
        {"path": ".github/workflows/timer.yaml", "state": timer},
        {"path": ".github/workflows/tests.yaml", "state": "active"},
    ]


class DecideTests(unittest.TestCase):
    def test_a_recent_run_means_nothing_to_do(self) -> None:
        decision = decide(workflows(), [run(3, 10), run(2, 25), run(1, 40)], NOW)
        self.assertFalse(decision.start)
        self.assertEqual(decision.reason, "healthy: the last forecasting run started 10 min ago")

    def test_the_quiet_limit(self) -> None:
        self.assertFalse(decide(workflows(), [run(1, 34)], NOW).start)
        decision = decide(workflows(), [run(2, 36), run(1, 51)], NOW)
        self.assertTrue(decision.start)
        self.assertEqual(decision.reason, "no forecasting run has started for 36 min")
        self.assertEqual((decision.enable, decision.cancel), (False, []))

    def test_the_newest_run_counts_wherever_it_is_listed(self) -> None:
        self.assertFalse(decide(workflows(), [run(1, 300), run(2, 200), run(3, 5)], NOW).start)

    def test_a_run_under_way_is_left_alone(self) -> None:
        decision = decide(workflows(), [run(2, 60, "in_progress"), run(1, 75)], NOW)
        self.assertFalse(decision.start)
        self.assertEqual(decision.reason, "healthy: a forecasting run is under way, started 60 min ago")
        self.assertFalse(decide(workflows(), [run(2, 50, "queued"), run(1, 75)], NOW).start)

    def test_a_run_that_never_finished_is_cancelled_and_replaced(self) -> None:
        decision = decide(workflows(), [run(7, 120, "in_progress"), run(6, 135)], NOW)
        self.assertTrue(decision.start)
        self.assertEqual(decision.cancel, [7])

    def test_a_stuck_run_is_left_to_the_timers_while_fresh_runs_arrive(self) -> None:
        decision = decide(workflows(), [run(8, 10), run(7, 120, "in_progress")], NOW)
        self.assertEqual((decision.start, decision.cancel), (False, []))

    def test_no_run_at_all_starts_one(self) -> None:
        decision = decide(workflows(), [], NOW)
        self.assertTrue(decision.start)
        self.assertEqual(decision.reason, "no forecasting run was found")

    def test_a_workflow_switched_off_by_hand_pauses_the_watchdog(self) -> None:
        for states in (workflows(forecast="disabled_manually"), workflows(timer="disabled_manually")):
            decision = decide(states, [run(1, 600)], NOW)
            self.assertFalse(decision.start)
            self.assertTrue(decision.reason.startswith("paused: "))

    def test_forecasting_switched_off_for_inactivity_is_switched_back_on(self) -> None:
        decision = decide(workflows(forecast="disabled_inactivity"), [run(1, 600)], NOW)
        self.assertEqual((decision.start, decision.enable), (True, True))
        self.assertFalse(decide(workflows(forecast="disabled_inactivity"), [run(1, 5)], NOW).start)

    def test_a_repository_without_the_forecasting_workflow_is_an_error(self) -> None:
        with self.assertRaises(GitHubError):
            decide([{"path": ".github/workflows/tests.yaml", "state": "active"}], [run(1, 5)], NOW)


class FakeGitHub:
    """Stands in for the GitHub class and records what the watchdog asked for."""

    def __init__(self, runs: list[dict], states: list[dict] | None = None) -> None:
        self.runs = runs
        self.states = states or workflows()
        self.calls: list[tuple] = []
        self.failing: set[str] = set()

    def workflows(self) -> list[dict]:
        return self.states

    def forecast_runs(self) -> list[dict]:
        if "list" in self.failing:
            raise GitHubError("could not reach GitHub: no route")
        return self.runs

    def _act(self, name: str, target) -> None:
        if name in self.failing:
            raise GitHubError(f"GitHub answered 500 to {name}")
        self.calls.append((name, target))

    def enable(self, workflow: str) -> None:
        self._act("enable", workflow)

    def cancel(self, run_id: int) -> None:
        self._act("cancel", run_id)

    def start(self, workflow: str) -> None:
        self._act("start", workflow)


class CheckTests(unittest.TestCase):
    def test_healthy_bot_is_left_alone(self) -> None:
        github = FakeGitHub([run(1, 10)])
        self.assertEqual(check(github, NOW), (0, "healthy: the last forecasting run started 10 min ago"))
        self.assertEqual(github.calls, [])

    def test_quiet_bot_gets_one_run(self) -> None:
        github = FakeGitHub([run(1, 50)])
        code, message = check(github, NOW)
        self.assertEqual((code, message), (0, "repaired: no forecasting run has started for 50 min, so one was started"))
        self.assertEqual(github.calls, [("start", FORECAST_WORKFLOW)])

    def test_stuck_run_is_cancelled_before_the_new_one(self) -> None:
        github = FakeGitHub([run(7, 120, "in_progress")], workflows(forecast="disabled_inactivity"))
        self.assertEqual(check(github, NOW)[0], 0)
        self.assertEqual(github.calls, [("enable", FORECAST_WORKFLOW), ("cancel", 7), ("start", FORECAST_WORKFLOW)])

    def test_a_failed_cancel_does_not_stop_the_repair(self) -> None:
        github = FakeGitHub([run(7, 120, "in_progress")])
        github.failing.add("cancel")
        code, message = check(github, NOW)
        self.assertEqual(code, 0)
        self.assertIn("could not cancel the stuck run 7", message)
        self.assertEqual(github.calls, [("start", FORECAST_WORKFLOW)])

    def test_dry_run_starts_nothing(self) -> None:
        github = FakeGitHub([run(1, 50)])
        code, message = check(github, NOW, dry_run=True)
        self.assertEqual(code, 0)
        self.assertTrue(message.startswith("would repair: "))
        self.assertEqual(github.calls, [])

    def test_paused_bot_gets_nothing(self) -> None:
        github = FakeGitHub([run(1, 600)], workflows(timer="disabled_manually"))
        self.assertEqual(check(github, NOW)[0], 0)
        self.assertEqual(github.calls, [])

    def test_problems_are_reported_as_errors(self) -> None:
        unreachable = FakeGitHub([run(1, 50)])
        unreachable.failing.add("list")
        self.assertEqual(check(unreachable, NOW), (1, "error: could not reach GitHub: no route"))

        refused = FakeGitHub([run(1, 50)])
        refused.failing.add("start")
        self.assertEqual(check(refused, NOW), (1, "error: GitHub answered 500 to start"))

        odd = FakeGitHub([{"id": 1, "status": "completed"}])  # no created_at
        code, message = check(odd, NOW)
        self.assertEqual(code, 1)
        self.assertIn("not in the expected form", message)


class FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self) -> bytes:
        return self.payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *details) -> bool:
        return False


class FakeWeb:
    """Stands in for urllib's urlopen: answers from a table and records the requests."""

    def __init__(self, answers: dict[tuple[str, str], object]) -> None:
        self.answers = answers
        self.requests: list = []

    def __call__(self, request, timeout: float = 0):
        self.requests.append(request)
        path = request.full_url.removeprefix("https://api.github.com/repos/owner/bot")
        answer = self.answers[(request.get_method(), path)]
        if isinstance(answer, Exception):
            raise answer
        return FakeResponse(json.dumps(answer).encode() if answer is not None else b"")


def refusal(code: int, message: str) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://api.github.com", code, "refused", None, io.BytesIO(json.dumps({"message": message}).encode()))


class GitHubTests(unittest.TestCase):
    def test_reads_carry_the_token_and_no_body(self) -> None:
        web = FakeWeb({("GET", "/actions/workflows?per_page=100"): {"workflows": workflows()}})
        self.assertEqual(len(GitHub("owner/bot", TOKEN, web).workflows()), 3)
        request = web.requests[0]
        self.assertEqual(request.get_header("Authorization"), f"Bearer {TOKEN}")
        self.assertEqual(request.get_header("User-agent"), "northsignal-watchdog")
        self.assertIsNone(request.data)

    def test_start_uses_the_default_branch(self) -> None:
        dispatch = ("POST", f"/actions/workflows/{FORECAST_WORKFLOW}/dispatches")
        web = FakeWeb({("GET", ""): {"default_branch": "trunk"}, dispatch: None})
        GitHub("owner/bot", TOKEN, web).start(FORECAST_WORKFLOW)
        self.assertEqual(json.loads(web.requests[1].data), {"ref": "trunk"})
        self.assertEqual(web.requests[1].get_header("Content-type"), "application/json")

    def test_calls_without_content_send_an_empty_body(self) -> None:
        web = FakeWeb({("POST", "/actions/runs/7/cancel"): None, ("PUT", f"/actions/workflows/{FORECAST_WORKFLOW}/enable"): None})
        github = GitHub("owner/bot", TOKEN, web)
        github.cancel(7)
        github.enable(FORECAST_WORKFLOW)
        self.assertEqual([request.data for request in web.requests], [b"", b""])

    def test_refusals_give_githubs_message_and_a_hint(self) -> None:
        runs = ("GET", f"/actions/workflows/{FORECAST_WORKFLOW}/runs?per_page=20")
        expired = GitHub("owner/bot", TOKEN, FakeWeb({runs: refusal(401, "Bad credentials")}))
        with self.assertRaisesRegex(GitHubError, r"answered 401 .*Bad credentials \(the token was refused"):
            expired.forecast_runs()
        dispatch = ("POST", f"/actions/workflows/{FORECAST_WORKFLOW}/dispatches")
        weak = GitHub("owner/bot", TOKEN, FakeWeb({("GET", ""): {"default_branch": "main"}, dispatch: refusal(403, "Resource not accessible")}))
        with self.assertRaisesRegex(GitHubError, r"answered 403 .*Actions: Read and write"):
            weak.start(FORECAST_WORKFLOW)

    def test_no_network_is_reported_plainly_and_without_the_token(self) -> None:
        web = FakeWeb({("GET", "/actions/workflows?per_page=100"): urllib.error.URLError("name not found")})
        with self.assertRaises(GitHubError) as caught:
            GitHub("owner/bot", TOKEN, web).workflows()
        self.assertEqual(str(caught.exception), "could not reach GitHub: name not found")


class MainTests(unittest.TestCase):
    def setUp(self) -> None:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)
        self.log = self.folder / "watchdog.log"
        self.token_file = self.folder / "github-token"

    def run_main(self, web: FakeWeb, *extra: str) -> tuple[int, str]:
        printed = io.StringIO()
        arguments = ["--repo", "owner/bot", "--token-file", str(self.token_file), "--log", str(self.log), *extra]
        with contextlib.redirect_stdout(printed), contextlib.redirect_stderr(printed):
            code = server_watchdog.main(arguments, now=NOW, urlopen=web)
        return code, printed.getvalue()

    def test_a_check_adds_one_dated_line_to_the_log(self) -> None:
        self.token_file.write_text(TOKEN + "\n", encoding="utf-8")
        web = FakeWeb({
            ("GET", "/actions/workflows?per_page=100"): {"workflows": workflows()},
            ("GET", f"/actions/workflows/{FORECAST_WORKFLOW}/runs?per_page=20"): {"workflow_runs": [run(1, 3)]},
        })
        code, printed = self.run_main(web)
        expected = "2026-11-17 12:00:00Z  healthy: the last forecasting run started 3 min ago\n"
        self.assertEqual((code, printed), (0, expected))
        self.assertEqual(self.log.read_text(encoding="utf-8"), expected)
        self.assertNotIn(TOKEN, printed)

    def test_a_missing_token_is_an_error_and_nothing_is_asked(self) -> None:
        web = FakeWeb({})
        code, printed = self.run_main(web)
        self.assertEqual(code, 1)
        self.assertIn("error: no GitHub token could be read from", printed)
        self.assertEqual(web.requests, [])
        self.assertIn("error: no GitHub token", self.log.read_text(encoding="utf-8"))

    def test_only_the_newest_log_lines_are_kept(self) -> None:
        for number in range(5):
            server_watchdog.write_log(self.log, f"line {number}", kept=3)
        self.assertEqual(self.log.read_text(encoding="utf-8"), "line 2\nline 3\nline 4\n")


class FakeServer:
    """Stands in for ssh: records what would run on the server."""

    def __init__(self, failing: str = "") -> None:
        self.calls: list[tuple[str, bytes]] = []
        self.failing = failing

    def __call__(self, host: str, command: str, data: bytes = b"", key: str | None = None) -> tuple[int, str]:
        self.calls.append((command, data))
        return (1, "it failed") if self.failing and self.failing in command else (0, "done")


class DeployTests(unittest.TestCase):
    def deploy(self, server: FakeServer, *arguments: str) -> tuple[int, str]:
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            code = deploy.main(["--host", "root@server.example", *arguments], run=server)
        return code, printed.getvalue()

    def token_file(self, content: bytes) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name) / "token.txt"
        path.write_bytes(content)
        return path

    def test_the_install_script_carries_the_watchdog_and_the_cron_line(self) -> None:
        script = deploy.install_script(b"print('hello')\r\n")
        encoded = script.split("<<'END_OF_SCRIPT'\n")[1].split("\nEND_OF_SCRIPT")[0]
        self.assertEqual(base64.b64decode(encoded), b"print('hello')\n")
        self.assertIn("4-59/15 * * * * northsignal /usr/bin/python3 /opt/northsignal-watchdog/server_watchdog.py", script)
        self.assertNotIn("\r", script)

    def test_install_changes_nothing_without_apply(self) -> None:
        server = FakeServer()
        code, printed = self.deploy(server, "install")
        self.assertEqual((code, server.calls), (0, []))
        self.assertIn("lines of base64", printed)
        self.assertIn("Nothing was changed", printed)

    def test_install_gives_the_script_to_bash_on_the_server(self) -> None:
        server = FakeServer()
        self.assertEqual(self.deploy(server, "install", "--apply")[0], 0)
        command, data = server.calls[0]
        self.assertEqual(command, "bash -s")
        self.assertIn(b"/etc/cron.d/northsignal-watchdog", data)

    def test_tokens_saved_by_notepad_are_read(self) -> None:
        for content in (b"github_pat_abc\r\n", b"\xef\xbb\xbfgithub_pat_abc\n", "github_pat_abc\r\n".encode("utf-16")):
            self.assertEqual(deploy.read_token(self.token_file(content)), "github_pat_abc")
        for content in (b"", b"  \n", b"two words\n", b"line one\nline two\n"):
            with self.assertRaises(SystemExit):
                deploy.read_token(self.token_file(content))

    def test_the_token_is_sent_checked_and_then_deleted_here(self) -> None:
        path = self.token_file(b"github_pat_secret\n")
        server = FakeServer()
        code, printed = self.deploy(server, "token", str(path), "--apply")
        self.assertEqual(code, 0)
        self.assertEqual(server.calls[0], (deploy.STORE_TOKEN, b"github_pat_secret\n"))
        self.assertIn("--dry-run", server.calls[1][0])
        self.assertFalse(path.exists())
        self.assertNotIn("github_pat_secret", printed)

    def test_the_token_file_is_kept_when_the_server_cannot_use_it(self) -> None:
        path = self.token_file(b"github_pat_secret\n")
        code, printed = self.deploy(FakeServer(failing="--dry-run"), "token", str(path), "--apply")
        self.assertEqual(code, 1)
        self.assertTrue(path.exists())
        self.assertIn("was kept", printed)

    def test_token_changes_nothing_without_apply(self) -> None:
        path = self.token_file(b"github_pat_secret\n")
        server = FakeServer()
        code, printed = self.deploy(server, "token", str(path))
        self.assertEqual((code, server.calls), (0, []))
        self.assertTrue(path.exists())
        self.assertNotIn("github_pat_secret", printed)

    def test_remove_takes_the_cron_file_the_folder_and_the_user_away(self) -> None:
        server = FakeServer()
        self.assertEqual(self.deploy(server, "remove", "--apply")[0], 0)
        command = server.calls[0][0]
        for part in ("rm -f /etc/cron.d/northsignal-watchdog", "rm -rf /opt/northsignal-watchdog", "userdel northsignal"):
            self.assertIn(part, command)


if __name__ == "__main__":
    unittest.main()
