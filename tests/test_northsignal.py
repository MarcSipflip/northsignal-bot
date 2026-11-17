"""Offline tests for the NorthSignal bot. No network and no paid model calls:
every model is replaced by a canned fake.

    uv run python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Dummy credentials, set before anything reads the environment, so tests can never spend money.
os.environ["METACULUS_TOKEN"] = "test-token"
os.environ["OPENROUTER_API_KEY"] = "test-key"
for name in ("ASKNEWS_CLIENT_ID", "ASKNEWS_SECRET", "ASKNEWS_API_KEY"):
    os.environ.pop(name, None)

from forecasting_tools import (  # noqa: E402
    BinaryPrediction,
    BinaryQuestion,
    ForecastBot,
    GeneralLlm,
    NumericDistribution,
    NumericQuestion,
    PredictedOptionList,
)
from forecasting_tools.data_models.multiple_choice_report import PredictedOption  # noqa: E402
from forecasting_tools.data_models.numeric_report import Percentile  # noqa: E402
from forecasting_tools.data_models.questions import DiscreteQuestion  # noqa: E402

import northsignal_bot  # noqa: E402
from northsignal_bot import NorthSignalBot, TemplateBot  # noqa: E402
from profiles import FULL, LEAN, ModelSpec, Profile, get_profile  # noqa: E402
from research_tools import SourcePage, clip, extract_urls, html_to_text  # noqa: E402

PROMPT_PERCENTILES = (5, 10, 20, 40, 50, 60, 80, 90, 95)  # the ones the numeric prompt asks for


class FakeLlm(GeneralLlm):
    def __init__(self, name: str, reply: str) -> None:
        super().__init__(model=name)
        self.reply = reply
        self.prompts: list[str] = []

    async def invoke(self, prompt, system_prompt=None) -> str:  # type: ignore[override]
        self.prompts.append(str(prompt))
        return self.reply


class UrlExtractionTests(unittest.TestCase):
    def test_finds_markdown_and_bare_links_and_skips_unreadable_ones(self) -> None:
        text = (
            "See [the release](https://www.bls.gov/news.release/cpi.nr0.htm). "
            "Also https://en.wikipedia.org/wiki/Foo_(bar), a post https://twitter.com/x/status/1 "
            "and a file https://example.com/report.pdf."
        )
        self.assertEqual(
            extract_urls([text]),
            ["https://www.bls.gov/news.release/cpi.nr0.htm", "https://en.wikipedia.org/wiki/Foo_(bar)"],
        )

    def test_deduplicates_and_respects_priority_and_limit(self) -> None:
        resolution = "Resolves per https://data.example.org/series/ and https://data.example.org/series#latest"
        background = "Context: https://news.example.com/a https://news.example.com/b https://news.example.com/c"
        self.assertEqual(
            extract_urls([resolution, None, background], limit=3),
            ["https://data.example.org/series/", "https://news.example.com/a", "https://news.example.com/b"],
        )

    def test_never_fetches_private_or_local_addresses(self) -> None:
        text = "http://localhost:8000/x http://192.168.1.5/a http://10.0.0.1/ http://printer.local/status"
        self.assertEqual(extract_urls([text]), [])


class HtmlTextTests(unittest.TestCase):
    def test_keeps_content_and_table_rows_and_drops_scripts(self) -> None:
        html = (
            "<html><head><title>CPI release</title><script>var hidden = 1;</script>"
            "<style>.x{color:red}</style></head><body><h1>Headline</h1><p>Para&nbsp;one.</p>"
            "<table><tr><th>Month</th><th>Value</th></tr><tr><td>Oct</td><td>3.1%</td></tr></table>"
            "</body></html>"
        )
        text = html_to_text(html)
        for expected in ("CPI release", "Headline", "Para one.", "Month | Value", "Oct | 3.1%"):
            self.assertIn(expected, text)
        self.assertNotIn("hidden", text)
        self.assertNotIn("color:red", text)

    def test_clip_keeps_both_ends(self) -> None:
        text = "START" + "x" * 5000 + "END"
        clipped = clip(text, 1000)
        self.assertLessEqual(len(clipped), 1010)
        self.assertTrue(clipped.startswith("START"))
        self.assertTrue(clipped.endswith("END"))
        self.assertEqual(clip("short", 1000), "short")


class ProfileTests(unittest.TestCase):
    def test_profiles_are_consistent(self) -> None:
        for profile in (LEAN, FULL):
            self.assertGreaterEqual(profile.predictions_per_question, len(profile.forecasters))
            for spec in (*profile.forecasters, *profile.searchers, profile.helper):
                self.assertTrue(spec.model.startswith("openrouter/"), spec.model)
            low, high = profile.binary_clamp
            self.assertTrue(0 < low < high < 1)
        self.assertLess(LEAN.max_run_cost_usd, FULL.max_run_cost_usd)

    def test_profile_selection(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BOT_PROFILE", None)
            os.environ.pop("BOT_MAX_RUN_COST_USD", None)
            self.assertEqual(get_profile().name, "lean")
            self.assertEqual(get_profile("full").name, "full")
            os.environ["BOT_PROFILE"] = "full"
            self.assertEqual(get_profile().name, "full")
            os.environ["BOT_MAX_RUN_COST_USD"] = "1.25"
            self.assertEqual(get_profile("lean").max_run_cost_usd, 1.25)
            with self.assertRaises(ValueError):
                get_profile("turbo")

    def test_reasoning_effort_travels_in_the_request_body(self) -> None:
        llm = ModelSpec("openrouter/test/model", timeout=321, reasoning_effort="high").build()
        self.assertEqual(llm.litellm_kwargs["extra_body"], {"reasoning": {"effort": "high"}})
        self.assertEqual(llm.litellm_kwargs["timeout"], 321)
        self.assertIsNone(llm.litellm_kwargs["temperature"])


class DistributionTests(unittest.TestCase):
    def test_option_floor_keeps_every_option_alive(self) -> None:
        options = PredictedOptionList(
            predicted_options=[
                PredictedOption(option_name="A", probability=0.999),
                PredictedOption(option_name="B", probability=0.001),
                PredictedOption(option_name="C", probability=0.0),
            ]
        )
        floored = NorthSignalBot.apply_option_floor(options, 0.01)
        probabilities = [option.probability for option in floored.predicted_options]
        self.assertAlmostEqual(sum(probabilities), 1.0, places=6)
        self.assertTrue(all(probability >= 0.009 for probability in probabilities))
        self.assertGreater(probabilities[0], 0.97)

    def test_prompt_percentiles_build_a_valid_numeric_forecast(self) -> None:
        question = NumericQuestion(
            question_text="How many?", upper_bound=100, lower_bound=0,
            open_upper_bound=True, open_lower_bound=False,
        )
        values = (10, 15, 22, 35, 40, 46, 60, 72, 80)
        percentiles = [Percentile(percentile=p / 100, value=v) for p, v in zip(PROMPT_PERCENTILES, values)]
        cdf = [point.percentile for point in NumericDistribution.from_question(percentiles, question).get_cdf()]
        self.assertEqual(len(cdf), 201)
        self.assertTrue(all(later >= earlier for earlier, later in zip(cdf, cdf[1:])))
        self.assertEqual(cdf[0], 0.0)
        self.assertLess(cdf[-1], 1.0)

    def test_prompt_percentiles_build_a_valid_discrete_forecast(self) -> None:
        question = DiscreteQuestion(
            question_text="How many launches?", upper_bound=10.5, lower_bound=-0.5,
            open_upper_bound=False, open_lower_bound=False, cdf_size=12,
        )
        values = (2, 2, 3, 4, 4, 5, 6, 7, 8)  # repeats, as a model would give for a small count
        percentiles = [Percentile(percentile=p / 100, value=v) for p, v in zip(PROMPT_PERCENTILES, values)]
        cdf = [point.percentile for point in NumericDistribution.from_question(percentiles, question).get_cdf()]
        self.assertEqual(len(cdf), 12)
        self.assertTrue(all(later >= earlier for earlier, later in zip(cdf, cdf[1:])))


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    def test_builds_on_the_upstream_template(self) -> None:
        self.assertTrue(issubclass(TemplateBot, ForecastBot))
        self.assertTrue(issubclass(NorthSignalBot, TemplateBot))
        self.assertIsNot(TemplateBot, NorthSignalBot)

    async def test_binary_question_end_to_end_with_fakes(self) -> None:
        profile = Profile(
            name="test",
            forecasters=(ModelSpec("openrouter/test/a"), ModelSpec("openrouter/test/b")),
            predictions_per_question=4,
            searchers=(ModelSpec("openrouter/test/search"),),
            helper=ModelSpec("openrouter/test/helper"),
            max_source_pages=2,
            max_run_cost_usd=1.0,
        )
        bot = NorthSignalBot(profile=profile, publish_reports_to_metaculus=False)
        forecaster_a = FakeLlm("fake/a", "Reasoning A.\n\nProbability: 99.5%")
        forecaster_b = FakeLlm("fake/b", "Reasoning B.\n\nProbability: 99.5%")
        searcher = FakeLlm("fake/search", "SEARCH FINDINGS: nothing scheduled.")
        helper = FakeLlm("fake/helper", "PAGE DIGEST: latest value 42 on 2026-11-10.")
        bot._forecasters = [forecaster_a, forecaster_b]
        bot._searchers = [searcher]
        bot.set_llm(helper, "summarizer")
        bot.set_llm(helper, "parser")

        question = BinaryQuestion(
            question_text="Will the index close above 50 on 2026-12-31?",
            id_of_post=1,
            id_of_question=1,
            page_url="https://www.metaculus.com/questions/1/",
            resolution_criteria="Resolves Yes per the figure at https://data.example.org/index.",
            fine_print="",
            background_info="See [history](https://data.example.org/history).",
        )

        async def fake_read_sources(urls, *args, **kwargs):
            return [SourcePage(url=url, text="Index level: 42 as of 2026-11-10.") for url in urls]

        async def fake_structure_output(text, output_type, **kwargs):
            self.assertIs(output_type, BinaryPrediction)
            return BinaryPrediction(prediction_in_decimal=0.995)

        with mock.patch.object(northsignal_bot, "read_sources", fake_read_sources), mock.patch.object(
            northsignal_bot.template, "structure_output", fake_structure_output
        ):
            reports = await bot.forecast_questions([question])

        report = reports[0]
        self.assertAlmostEqual(report.prediction, 0.98)  # 99.5% pulled back to the clamp
        # Four forecasts shared evenly between the two models of the pool.
        self.assertEqual((len(forecaster_a.prompts), len(forecaster_b.prompts)), (2, 2))
        self.assertEqual(len(searcher.prompts), 1)
        forecast_prompt = forecaster_a.prompts[0]
        for expected in ("Resolution sources", "PAGE DIGEST", "Web research", "SEARCH FINDINGS", "Probability: ZZ%"):
            self.assertIn(expected, forecast_prompt)
        self.assertNotIn("Recent news", forecast_prompt)  # no AskNews credentials in tests
        self.assertIn("https://data.example.org/index", forecast_prompt)

    async def test_research_survives_a_failed_pass(self) -> None:
        profile = Profile(
            name="test", forecasters=(ModelSpec("openrouter/test/a"),), predictions_per_question=1,
            searchers=(ModelSpec("openrouter/test/search"),), helper=ModelSpec("openrouter/test/helper"),
            max_source_pages=2, max_run_cost_usd=1.0,
        )
        bot = NorthSignalBot(profile=profile, publish_reports_to_metaculus=False)

        class BrokenLlm(FakeLlm):
            async def invoke(self, prompt, system_prompt=None) -> str:  # type: ignore[override]
                raise RuntimeError("search provider is down")

        bot._searchers = [BrokenLlm("fake/search", "")]
        question = BinaryQuestion(question_text="Will it rain?", resolution_criteria="No links here.")
        research = await bot.run_research(question)
        self.assertIn("No research could be gathered", research)


if __name__ == "__main__":
    unittest.main()
