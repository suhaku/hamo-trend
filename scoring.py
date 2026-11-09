"""Composite score /100 with renormalization over present signals. No fabricated zeros."""
from __future__ import annotations
import math

WEIGHTS = {
    "youtube_pv": 0.35,
    "google_trends": 0.25,
    "x": 0.20,
    "community": 0.20,
}

PROVISIONAL_BELOW = 0.60
HOLD_BELOW = 0.50
MIN_PRESENT = 1


def log_norm(value: float | None, peer_values: list[float]) -> float | None:
    if value is None or value < 0:
        return None
    peers = [v for v in peer_values if v is not None and v >= 0]
    if not peers:
        return None
    m = max(math.log1p(v) for v in peers) or 1.0
    return round(100.0 * math.log1p(value) / m, 2)


def composite_score(
    signals: dict[str, float | None],
    peer_pools: dict[str, list[float]] | None = None,
    available: list[str] | None = None,
):
    """Missing ≠ 0. Renormalize over present. Coverage vs available (excludes 미연동)."""
    peer_pools = peer_pools or {}
    avail = [k for k in (available or list(WEIGHTS.keys())) if k in WEIGHTS] or list(WEIGHTS.keys())

    present = {}
    for key in avail:
        raw = signals.get(key)
        if raw is None:
            continue
        pool = peer_pools.get(key)
        if pool is not None:
            normed = log_norm(float(raw), pool)
            if normed is None:
                continue
            present[key] = normed
        else:
            present[key] = float(raw)

    n_possible = len(avail)
    n_present = len(present)
    coverage = round(n_present / n_possible, 4) if n_possible else 0.0
    reliability = coverage

    if n_present == 0:
        return dict(
            score=None, coverage=0.0, reliability=0.0, provisional=False,
            hold=True, hold_reason="실측 신호 없음", used_weights=None,
            present_signals=[], available_signals=avail, signal_scores_0_100={},
        )

    wsum = sum(WEIGHTS[k] for k in present)
    used = {k: round(WEIGHTS[k] / wsum, 4) for k in present}
    score = round(sum(present[k] * used[k] for k in present), 2)

    hold = False
    provisional = False
    hold_reason = None
    if n_present < MIN_PRESENT:
        hold = True
        hold_reason = "실측 신호 없음"
    elif n_possible >= 2 and coverage < HOLD_BELOW:
        provisional = True
    elif coverage < PROVISIONAL_BELOW:
        provisional = True

    return dict(
        score=None if hold else score,
        score_raw=score,
        coverage=coverage,
        reliability=reliability,
        provisional=provisional,
        hold=hold,
        hold_reason=hold_reason,
        used_weights=used,
        present_signals=sorted(present.keys()),
        available_signals=avail,
        signal_scores_0_100=present,
    )
