"""Model line-ups for the NorthSignal bot.

Two profiles:
  lean  low cost per question, used while the bot is proving itself (about $0.15 a question)
  full  competitive settings for the seasonal tournament (about $0.50 to $1 a question)

Pick one with the BOT_PROFILE environment variable (default: lean). All models are reached through
OpenRouter, which is also how the tournament hands out sponsored credits, so one key covers everything.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from forecasting_tools import GeneralLlm


@dataclass(frozen=True)
class ModelSpec:
    model: str
    timeout: int = 180  # seconds; the framework's default of 60 is too short for deep reasoning
    reasoning_effort: str | None = None
    allowed_tries: int = 2

    def build(self) -> GeneralLlm:
        kwargs: dict = {}
        if self.reasoning_effort:
            # Sent in the request body as OpenRouter's own field so it is never silently dropped.
            kwargs["extra_body"] = {"reasoning": {"effort": self.reasoning_effort}}
        return GeneralLlm(
            model=self.model, timeout=self.timeout, allowed_tries=self.allowed_tries, **kwargs
        )


@dataclass(frozen=True)
class Profile:
    name: str
    forecasters: tuple[ModelSpec, ...]  # forecasts rotate through these models
    predictions_per_question: int
    searchers: tuple[ModelSpec, ...]  # search-grounded models; each one adds a research pass
    helper: ModelSpec  # cheap model for reading pages, summarising and parsing answers
    max_source_pages: int
    max_run_cost_usd: float
    binary_clamp: tuple[float, float] = (0.02, 0.98)
    option_floor: float = 0.01

    def __str__(self) -> str:
        return self.name


HELPER = ModelSpec("openrouter/anthropic/claude-haiku-5.5", timeout=120)

LEAN = Profile(
    name="lean",
    forecasters=(
        ModelSpec("openrouter/anthropic/claude-sonnet-5.5", timeout=600, reasoning_effort="high"),
    ),
    predictions_per_question=3,
    searchers=(ModelSpec("openrouter/perplexity/sonar-pro", timeout=180),),
    helper=HELPER,
    max_source_pages=3,
    max_run_cost_usd=3.0,
)

FULL = Profile(
    name="full",
    forecasters=(
        ModelSpec("openrouter/anthropic/claude-opus-5.5", timeout=900, reasoning_effort="high"),
        ModelSpec("openrouter/openai/gpt-5.4", timeout=900, reasoning_effort="high"),
    ),
    predictions_per_question=6,
    searchers=(
        ModelSpec("openrouter/perplexity/sonar-pro", timeout=180),
        ModelSpec("openrouter/perplexity/sonar-reasoning-pro", timeout=300),
    ),
    helper=HELPER,
    max_source_pages=5,
    max_run_cost_usd=15.0,
)

PROFILES = {profile.name: profile for profile in (LEAN, FULL)}


def get_profile(name: str | None = None) -> Profile:
    chosen = (name or os.getenv("BOT_PROFILE") or "lean").strip().lower()
    if chosen not in PROFILES:
        raise ValueError(f"Unknown profile '{chosen}'. Choose one of: {', '.join(PROFILES)}")
    profile = PROFILES[chosen]
    cap = os.getenv("BOT_MAX_RUN_COST_USD")
    if cap:
        profile = Profile(**{**profile.__dict__, "max_run_cost_usd": float(cap)})
    return profile
