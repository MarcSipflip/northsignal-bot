"""Entry point for the NorthSignal bot.

    uv run python run_bot.py --mode test_questions     smoke test on the unscored bot-testing-area
    uv run python run_bot.py --mode tournament         seasonal tournament + MiniBench (what the schedule runs)
    uv run python run_bot.py --mode minibench --dry-run

Profile: --profile lean|full, or the BOT_PROFILE environment variable (default lean).
Each run stops spending once it reaches the profile's cap (override with BOT_MAX_RUN_COST_USD).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from bot_helpers import (
    check_environment,
    print_run_summary_banner,
    print_startup_banner,
    silence_noisy_dependencies,
)

silence_noisy_dependencies()

from forecasting_tools import ForecastReport, MetaculusQuestion  # noqa: E402
from forecasting_tools.ai_models.resource_managers.monetary_cost_manager import (  # noqa: E402
    MonetaryCostManager,
)

from northsignal_bot import NorthSignalBot  # noqa: E402
from profiles import get_profile  # noqa: E402

logger = logging.getLogger(__name__)

TOURNAMENT_URLS = {
    "tournament": "https://www.metaculus.com/futureeval/",
    "seasonal": "https://www.metaculus.com/futureeval/",
    "minibench": "https://www.metaculus.com/tournament/minibench/",
    "test_questions": "https://www.metaculus.com/tournament/bot-testing-area/",
}


def _closing_soonest_first(question: MetaculusQuestion) -> tuple[bool, float]:
    close_time = question.close_time
    return (close_time is None, close_time.timestamp() if close_time else 0.0)


async def forecast(bot: NorthSignalBot, mode: str, max_questions: int) -> list:
    client = bot.metaculus_client
    targets = {
        "tournament": [client.CURRENT_AI_COMPETITION_ID, client.CURRENT_MINIBENCH_ID],
        "seasonal": [client.CURRENT_AI_COMPETITION_ID],
        "minibench": [client.CURRENT_MINIBENCH_ID],
        "test_questions": ["bot-testing-area"],
    }[mode]

    reports: list = []
    remaining = max_questions
    for tournament_id in targets:
        if remaining <= 0:
            break
        questions = client.get_all_open_questions_from_tournament(tournament_id)
        if bot.skip_previously_forecasted_questions:
            questions = [question for question in questions if not question.already_forecasted]
        # Questions stay open for about three hours: if a run is capped, keep the ones closing first.
        questions = sorted(questions, key=_closing_soonest_first)[:remaining]
        remaining -= len(questions)
        logger.info(f"{tournament_id}: {len(questions)} question(s) to forecast")
        if questions:
            reports += await bot.forecast_questions(questions, return_exceptions=True)
    return reports


def main() -> int:
    # The banners use emoji; a Windows console with a legacy code page would crash on them.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    parser = argparse.ArgumentParser(description="Run the NorthSignal forecasting bot")
    parser.add_argument("--mode", choices=list(TOURNAMENT_URLS), default="tournament")
    parser.add_argument("--profile", choices=["lean", "full"], default=None, help="default: BOT_PROFILE or lean")
    parser.add_argument("--dry-run", action="store_true", help="forecast without posting to Metaculus")
    parser.add_argument("--max-questions", type=int, default=15, help="most questions to forecast in one run")
    args = parser.parse_args()

    check_environment(strict=True)
    profile = get_profile(args.profile)
    publish = not args.dry_run
    print_startup_banner(f"{args.mode} (profile={profile.name}, cap=${profile.max_run_cost_usd:.2f})", will_publish=publish)

    bot = NorthSignalBot(
        profile=profile,
        publish_reports_to_metaculus=publish,
        folder_to_save_reports_to="reports",
        # The test area is re-forecast on every smoke test; live questions are forecast once.
        skip_previously_forecasted_questions=args.mode != "test_questions",
        extra_metadata_in_explanation=True,
    )

    with MonetaryCostManager(profile.max_run_cost_usd) as cost_manager:
        reports = asyncio.run(forecast(bot, args.mode, args.max_questions))
        spent = cost_manager.current_usage

    bot.log_report_summary(reports, raise_errors=False)
    print_run_summary_banner(reports, will_publish=publish, tournament_url=TOURNAMENT_URLS[args.mode])
    print(f"Spent this run (tracked): ${spent:.3f} of a ${profile.max_run_cost_usd:.2f} cap")

    succeeded = [report for report in reports if isinstance(report, ForecastReport)]
    # Fail the run only when nothing worked, so one awkward question never raises an alarm
    # and a broken key or exhausted balance always does.
    return 1 if reports and not succeeded else 0


if __name__ == "__main__":
    sys.exit(main())
