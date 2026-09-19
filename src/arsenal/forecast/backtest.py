"""Backtesting the forecast against completed gameweeks.

The discipline that matters here is **avoiding leakage**. Two things would make
the numbers look good and mean nothing:

1. **Current injury status.** ``status`` and ``chance_of_playing_next_round``
   describe today. Using them to predict a gameweek that has already happened
   tells the model who got injured, which it could not have known at the time.
   Backtests therefore run with availability disabled, predicting from
   historical minutes alone. Live forecasts keep it — that information is
   genuinely available before a deadline, and it is the single biggest lever the
   research agents have.

2. **Season-total stats.** Everything in ``bootstrap.elements`` is cumulative to
   *now*, so it silently includes the gameweek being predicted. The backtest
   rebuilds rates from per-gameweek history truncated before the target instead.

A caveat worth stating plainly: with only a handful of completed gameweeks, these
results are indicative at best. Rank correlation is the number to watch — squad
selection only needs players ordered correctly, not their totals predicted
exactly.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from ..fpl.client import FPLClient
from ..fpl.schemas import Bootstrap
from .history import History, load_history
from .model import forecast_players
from .teams import build_league_model, upcoming_fixtures

log = logging.getLogger(__name__)


@dataclass
class GameweekResult:
    gameweek: int
    n: int
    mae: float
    rmse: float
    spearman: float
    baseline_mae: float
    baseline_spearman: float
    top10_actual: float
    """Mean actual points of the ten players the model rated highest — the
    number that most directly reflects whether its picks were any good."""

    field_actual: float

    @property
    def beats_baseline(self) -> bool:
        return self.mae < self.baseline_mae


def _rank(values: list[float]) -> list[float]:
    """Average ranks, so ties do not distort the correlation."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        shared = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = shared
        i = j + 1
    return ranks


def spearman(predicted: list[float], actual: list[float]) -> float:
    """Rank correlation. The metric that matters: selection needs order, not totals."""
    if len(predicted) < 2:
        return 0.0
    x, y = _rank(predicted), _rank(actual)
    n = len(x)
    mean_x, mean_y = sum(x) / n, sum(y) / n
    cov = sum((a - mean_x) * (b - mean_y) for a, b in zip(x, y, strict=True))
    var_x = sum((a - mean_x) ** 2 for a in x)
    var_y = sum((b - mean_y) ** 2 for b in y)
    if var_x <= 0 or var_y <= 0:
        return 0.0
    return cov / math.sqrt(var_x * var_y)


def backtest_gameweek(
    bootstrap: Bootstrap,
    full_history: History,
    target: int,
    fixtures: list,
) -> GameweekResult | None:
    """Predict ``target`` from gameweeks strictly before it, and score the result."""
    prior_gameweeks = [g for g in full_history.gameweeks if g < target]
    if not prior_gameweeks or target not in full_history.gameweeks:
        return None

    history = History(
        records=[r for r in full_history.records if r.gameweek < target],
        gameweeks=prior_gameweeks,
    )
    league = build_league_model(history, bootstrap)
    schedule = upcoming_fixtures(fixtures, start_gameweek=target, horizon=1)

    forecasts = forecast_players(
        bootstrap,
        history,
        league,
        schedule,
        horizon=1,
        use_availability=False,
    )

    actuals = {
        r.element_id: r.total_points for r in full_history.records if r.gameweek == target
    }

    # Score only players with a real prior role. Including the 400 players who
    # never feature would flatter every model equally — they score zero and are
    # trivially predicted — and drown out the discrimination that matters.
    by_player = history.by_player()
    population = [
        element_id
        for element_id, records in by_player.items()
        if sum(r.minutes for r in records) >= 90
    ]
    if len(population) < 10:
        return None

    predicted: list[float] = []
    actual: list[float] = []
    baseline: list[float] = []
    for element_id in population:
        forecast = forecasts.get(element_id)
        if forecast is None:
            continue
        predicted.append(forecast.xp[0])
        actual.append(float(actuals.get(element_id, 0)))
        # Baseline: mean points per completed gameweek so far. This is what
        # "just pick whoever has been scoring" amounts to, and beating it is the
        # minimum bar for the model to be worth its complexity.
        records = by_player[element_id]
        baseline.append(sum(r.total_points for r in records) / len(prior_gameweeks))

    errors = [p - a for p, a in zip(predicted, actual, strict=True)]
    base_errors = [b - a for b, a in zip(baseline, actual, strict=True)]
    n = len(errors)

    ranked = sorted(zip(predicted, actual, strict=True), key=lambda pair: pair[0], reverse=True)
    top10 = [a for _, a in ranked[:10]]

    return GameweekResult(
        gameweek=target,
        n=n,
        mae=sum(abs(e) for e in errors) / n,
        rmse=math.sqrt(sum(e * e for e in errors) / n),
        spearman=spearman(predicted, actual),
        baseline_mae=sum(abs(e) for e in base_errors) / n,
        baseline_spearman=spearman(baseline, actual),
        top10_actual=sum(top10) / len(top10),
        field_actual=sum(actual) / n,
    )


def backtest(
    client: FPLClient,
    bootstrap: Bootstrap,
    *,
    first: int | None = None,
    last: int | None = None,
) -> list[GameweekResult]:
    """Backtest every gameweek that has at least one completed gameweek before it."""
    history = load_history(client, bootstrap)
    fixtures = client.fixtures()

    candidates = [g for g in history.gameweeks if g > min(history.gameweeks, default=0)]
    if first is not None:
        candidates = [g for g in candidates if g >= first]
    if last is not None:
        candidates = [g for g in candidates if g <= last]

    results = []
    for gameweek in candidates:
        result = backtest_gameweek(bootstrap, history, gameweek, fixtures)
        if result is not None:
            results.append(result)
    return results
