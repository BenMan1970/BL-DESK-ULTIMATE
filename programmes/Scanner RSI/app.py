"""
RSI & Divergence Screener Pro — Production-grade Streamlit application.
Hardened for 24/7 institutional deployment.

Architecture:
    [DOMAIN]       pure logic (RSI, ATR, divergence) — no I/O, no framework
    [INFRA]        OANDA client (pooled), candle cache (singleton, thread-safe)
    [APPLICATION]  orchestration (scan engine, executor pool)
    [EXPORTS]      CSV / JSON / PDF builders (pure transforms, thread-isolated)
    [PRESENTATION] Streamlit UI (rerun-safe, thread-safe progress, isolated state)

Determinism guarantees:
    - All requests within a single scan share the same `snapshot_time`,
      anchored server-side via `to=<isoformat>` parameter.
    - Cache keys include the snapshot bucket; stale data cannot be served.
    - Pivots are only considered confirmed after `peak_distance` bars.
    - All caches return deep copies; mutation cannot poison shared state.

Concurrency model:
    - Pure threading (NOT asyncio). All locks are threading.Lock / RLock.
    - Bounded semaphore caps HTTP fan-out to OANDA.
    - Single shared ThreadPoolExecutor lives in st.cache_resource with
      health checks and lazy recreation.
    - Scan acquisition is fused with cooldown check via explicit state
      tracking (no fragile Lock.locked() polling).

Author: Principal Engineering
License: Proprietary
"""
from __future__ import annotations

# =============================================================================
# 1. IMPORTS
# =============================================================================
import hashlib
import heapq
import html as html_lib
import io
import json
import logging
import math
import os
import random
import re
import threading
import time
import uuid
from concurrent.futures import (
    Future,
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import (
    Any,
    Callable,
    Dict,
    Iterable,
    Iterator,
    List,
    Mapping,
    Optional,
    Tuple,
)

import numpy as np
import pandas as pd
import requests
import streamlit as st
from fpdf import FPDF
from oandapyV20 import API
from oandapyV20.endpoints.instruments import InstrumentsCandles
from oandapyV20.exceptions import V20Error
from requests.adapters import HTTPAdapter
from scipy.signal import find_peaks
from urllib3.util.retry import Retry


# =============================================================================
# 2. CONSTANTS (single source of truth)
# =============================================================================

# ---- RSI / Divergence parameters ----
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 30.0
RSI_OVERBOUGHT: float = 70.0
RSI_EXTREME_LOW: float = 20.0
RSI_EXTREME_HIGH: float = 80.0
RSI_BEAR_DIV_ZONE: float = 60.0
RSI_BULL_DIV_ZONE: float = 40.0
RSI_NEUTRAL_THRESHOLD: float = 50.0
MIN_RSI_DELTA: float = 3.0
MAX_NAN_RATIO_FOR_DIV: float = 0.30

# ---- ATR & divergence detection windows ----
ATR_PERIOD: int = 14
DIV_LOOKBACK_BARS: int = 50
PEAK_DISTANCE_BARS: int = 5
PIVOT_MAX_AGE_BARS: int = 5
# [QUANT-FIX] Pivot match tolerance widened from max(2, PEAK_DISTANCE_BARS - 2)
# to max(3, PEAK_DISTANCE_BARS). RSI is a smoothed momentum derivative whose
# extrema lag price extrema (EWM phase delay). The previous tight tolerance
# dropped legitimate divergences on lagging assets (JPY, GOLD). Robust because
# the loosening is bounded and paired with amplitude-aware scoring in
# `_match_pivot_pure` that resolves multi-candidate ties.
PIVOT_MATCH_TOLERANCE: int = max(3, PEAK_DISTANCE_BARS)
# [AUDIT-P07] Les quatre détecteurs ne considéraient que la DERNIÈRE paire de
# pivots (`price_pivots[-1]` et `[-2]`) : toute divergence portée par des pivots
# NON adjacents (par exemple -1 et -3) était invisible. L'élargissement est
# correctif MAIS il ÉTEND l'ensemble des signaux détectables, donc des gagnants
# possibles : il est livré DÉSACTIVÉ par défaut, derrière un drapeau explicite,
# pour garantir zéro régression décisionnelle. Le drapeau est exporté dans
# `meta.scoring.multi_pivot_scan_enabled`.
ENABLE_MULTI_PIVOT_SCAN: bool = False
PIVOT_MAX_PAIRS: int = 3

# ---- Float comparison epsilon for OHLC validation ----
_OHLC_EPSILON: float = 1e-9

# ---- Per-asset ATR multipliers (prominence factor for find_peaks) ----
_ASSET_CLASS_MULTIPLIERS: Mapping[str, float] = {
    "JPY":   0.80,
    "INDEX": 1.00,
    "GOLD":  0.30,
    "FX":    0.50,
}

# ---- Network & concurrency ----
MAX_WORKERS: int = 6
MAX_RETRIES: int = 3
BASE_BACKOFF: float = 1.0
MAX_BACKOFF: float = 30.0
REQUEST_TIMEOUT: float = 15.0
SEMAPHORE_LIMIT: int = 4
SEMAPHORE_ACQUIRE_TIMEOUT: float = 5.0
SCAN_GLOBAL_TIMEOUT: float = 300.0
SERVER_COOLDOWN: float = 30.0
SCAN_SUCCESS_THRESHOLD: float = 0.5

# ---- Cache ----
CACHE_TTL_BY_TF: Mapping[str, int] = {
    "M15": 60, "H1": 60, "H4": 300, "D": 900, "W": 3600,
}
CACHE_MAX_ENTRIES: int = 500

# ---- TF candle counts ----
TF_CANDLE_COUNTS: Mapping[str, int] = {
    "M15": 200, "H1": 200, "H4": 200, "D": 200, "W": 200,
}

# ---- Assets & timeframes ----
ASSETS: Tuple[str, ...] = (
    "EUR_USD", "GBP_USD", "USD_JPY", "USD_CHF", "AUD_USD", "USD_CAD", "NZD_USD",
    "EUR_GBP", "EUR_JPY", "GBP_JPY", "AUD_JPY", "CHF_JPY", "CAD_JPY", "NZD_JPY",
    "EUR_AUD", "EUR_CAD", "EUR_CHF", "EUR_NZD",
    "GBP_AUD", "GBP_CAD", "GBP_CHF", "GBP_NZD",
    "AUD_CAD", "AUD_CHF", "AUD_NZD",
    "CAD_CHF", "NZD_CAD", "NZD_CHF",
    "XAU_USD", "SPX500_USD", "NAS100_USD", "US30_USD", "DE30_EUR",
)

TIMEFRAMES: Tuple[Tuple[str, str], ...] = (
    ("15m", "M15"), ("1h", "H1"), ("4h", "H4"), ("D", "D"), ("W", "W"),
)
TIMEFRAMES_DISPLAY: Tuple[str, ...] = tuple(d for d, _ in TIMEFRAMES)
TIMEFRAMES_TO_KEY: Mapping[str, str] = dict(TIMEFRAMES)
TF_KEY_TO_DISPLAY: Mapping[str, str] = {k: d for d, k in TIMEFRAMES}

TF_BAR_SECONDS: Mapping[str, int] = {
    "M15": 15 * 60,
    "H1":  60 * 60,
    "H4":  4 * 3600,
    "D":   24 * 3600,
    "W":   7 * 24 * 3600,
}

# Hard caps for PDF rendering
PDF_MAX_ROWS: int = 200
PDF_MAX_PAIR_LEN: int = 14


# =============================================================================
# 3. ENUMS & DATA STRUCTURES  — [REFONTE: enrichissement signal, ZÉRO régression]
# =============================================================================

class ScanStatus(str, Enum):
    OK = "OK"
    PARTIAL = "PARTIAL"
    ERROR = "ERROR"
    TIMEOUT = "TIMEOUT"
    AUTH_ABORT = "AUTH_ABORT"
    CANCELLED = "CANCELLED"
    DATA_INVALID = "DATA_INVALID"
    NETWORK_ERROR = "NETWORK_ERROR"
    INTERNAL_BUG = "INTERNAL_BUG"
    NO_DATA = "NO_DATA"


class DivergenceLabel(str, Enum):
    AUCUNE = "Aucune"
    HAUSSIERE = "Haussiere"
    BAISSIERE = "Baissiere"
    STALE = "Stale"


_DIV_TO_JSON: Mapping[str, str] = {
    DivergenceLabel.AUCUNE.value: "NONE",
    DivergenceLabel.HAUSSIERE.value: "BULL",
    DivergenceLabel.BAISSIERE.value: "BEAR",
    DivergenceLabel.STALE.value: "STALE",
}

# [QUANT-FIX additive] quality_tier mapping from strength_score. Pure, bounded,
# universal (no per-asset calibration). Lets the fusion engine bucket signals
# without re-deriving thresholds. Tiers are descriptive, NOT filters.
_TIER_T1: float = 0.65   # institutional-grade
_TIER_T2: float = 0.45   # actionable
_TIER_T3: float = 0.25   # weak-but-valid (rejection floor, see _build_div_signal)


def _quality_tier(strength: float) -> str:
    if strength >= _TIER_T1:
        return "T1"
    if strength >= _TIER_T2:
        return "T2"
    if strength >= _TIER_T3:
        return "T3"
    return "T4"


def _clamp01_local(x: float) -> float:
    if not np.isfinite(x):
        return 0.0
    return float(max(0.0, min(1.0, x)))


# ── [AUDIT-P05] États de premier rang par cellule ────────────────────────────
# La midline 50 et les zones de surachat/survente sont des SIGNAUX exploités par
# le pipeline aval. Les laisser implicites dans un flottant oblige le
# consommateur (`merge.py:_rsi_status_from_value`, seuils 80/72/68/32/28/20) à
# REDÉRIVER l'état avec un second jeu de seuils, incompatible avec celui du
# scanner (20/30/70/80). Ces fonctions matérialisent l'état dans le JSON.
def _rsi_zone(rsi: float) -> str:
    """Zone RSI fermée et MUTUELLEMENT EXCLUSIVE. 'UNKNOWN' si RSI indisponible.

    Partition de l'axe RSI (extrêmes évalués en premier, puis bornes) :
      <=20 EXTREME_OVERSOLD | (20,30] OVERSOLD | [30,50) NEUTRAL_BEAR
      ==50 NEUTRAL | (50,70) NEUTRAL_BULL | [70,80) OVERBOUGHT | >=80 EXTREME_OVERBOUGHT
    """
    if rsi is None or not np.isfinite(rsi):
        return "UNKNOWN"
    if rsi <= RSI_EXTREME_LOW:
        return "EXTREME_OVERSOLD"
    if rsi <= RSI_OVERSOLD:
        return "OVERSOLD"
    if rsi >= RSI_EXTREME_HIGH:
        return "EXTREME_OVERBOUGHT"
    if rsi >= RSI_OVERBOUGHT:
        return "OVERBOUGHT"
    if rsi > RSI_NEUTRAL_THRESHOLD:
        return "NEUTRAL_BULL"
    if rsi < RSI_NEUTRAL_THRESHOLD:
        return "NEUTRAL_BEAR"
    return "NEUTRAL"


def _midline_block(rsi_series: Optional[pd.Series]) -> Dict[str, Any]:
    """État complet vis-à-vis de la ligne 50 : côté, distance, franchissement.

    Retourne TOUJOURS un objet complet (jamais None) : `available` indique si
    l'information existe, les feuilles scalaires valent None le cas échéant.
    """
    if rsi_series is None:
        return _empty_midline_block()
    s = rsi_series.dropna()
    if len(s) < 2:
        return _empty_midline_block()
    vals = s.to_numpy(np.float64)
    last, prev = float(vals[-1]), float(vals[-2])
    m = RSI_NEUTRAL_THRESHOLD
    side = "ABOVE" if last > m else ("BELOW" if last < m else "AT")
    if prev <= m < last:
        cross = "CROSS_UP"
    elif prev >= m > last:
        cross = "CROSS_DOWN"
    else:
        cross = "NONE"
    above = vals > m
    bars_since: Optional[int] = None
    for k in range(len(above) - 1, 0, -1):
        if above[k] != above[k - 1]:
            bars_since = (len(above) - 1) - k
            break
    return {
        "side": side,
        "distance": round(last - m, 2),
        "cross": cross,
        "bars_since_cross": bars_since,
        "available": True,
    }


# [REFONTE — exigence A/B/D/F] DivergenceSignal now carries the FULL audit
# context required by the fusion engine. Every field is OPTIONAL with a safe
# default, so all existing positional/keyword constructions remain valid and
# the frozen contract is preserved. NOTHING is removed.
@dataclass(frozen=True)
class DivergenceSignal:
    label: DivergenceLabel
    pivot_index: Optional[int] = None
    confirmed: bool = False
    strength_score: float = 0.0
    kind: str = "REGULAR"
    # ── enrichment (exigence A) ──
    confidence_score: float = 0.0          # [0,1] pivot-matching quality
    quality_tier: str = "T4"               # T1..T4 derived from strength
    divergence_age_bars: Optional[int] = None     # bars since last pivot
    pivot_age_bars: Optional[int] = None          # alias, explicit
    price_pivot_prev_idx: Optional[int] = None
    price_pivot_last_idx: Optional[int] = None
    rsi_pivot_prev_idx: Optional[int] = None
    rsi_pivot_last_idx: Optional[int] = None
    rsi_prev_value: Optional[float] = None
    rsi_last_value: Optional[float] = None
    price_prev_value: Optional[float] = None
    price_last_value: Optional[float] = None
    pivot_distance_bars: Optional[int] = None      # |last - prev| price pivots
    match_distance_bars: Optional[int] = None       # temporal price↔rsi match gap

    def to_export(self) -> Dict[str, Any]:
        """Serialise to a fusion-friendly dict. Pure, no side effects."""
        return {
            "label": self.label.value,
            "code": _DIV_TO_JSON.get(self.label.value, "NONE"),
            "kind": self.kind,
            "confirmed": self.confirmed,
            "strength_score": round(float(self.strength_score), 4),
            "confidence_score": round(float(self.confidence_score), 4),
            "quality_tier": self.quality_tier,
            "divergence_age_bars": self.divergence_age_bars,
            "pivot_distance_bars": self.pivot_distance_bars,
            "match_distance_bars": self.match_distance_bars,
            "pivots": {
                "price_prev_idx": self.price_pivot_prev_idx,
                "price_last_idx": self.price_pivot_last_idx,
                "rsi_prev_idx": self.rsi_pivot_prev_idx,
                "rsi_last_idx": self.rsi_pivot_last_idx,
                "rsi_prev_value": (round(self.rsi_prev_value, 2)
                                   if self.rsi_prev_value is not None else None),
                "rsi_last_value": (round(self.rsi_last_value, 2)
                                   if self.rsi_last_value is not None else None),
                "price_prev_value": (round(self.price_prev_value, 6)
                                     if self.price_prev_value is not None else None),
                "price_last_value": (round(self.price_last_value, 6)
                                     if self.price_last_value is not None else None),
            },
        }


# [REFONTE — exigence F] New aggregate result carrying the winner AND all
# candidates plus market context. Returned by detect_divergence_full; a
# backward-compatible accessor (detect_divergence) returns winner only.
@dataclass(frozen=True)
class DivergenceResult:
    winner: DivergenceSignal
    candidates: Tuple[DivergenceSignal, ...] = ()
    adx: float = float("nan")
    regime_factor: float = 1.0
    volatility_regime: str = "normal"      # quiet | normal | expansive
    allow_regular: bool = True
    lookback_used: int = DIV_LOOKBACK_BARS
    confirm_bars_used: int = PEAK_DISTANCE_BARS
    # [FIX-1 REVISED] Traceability for the adaptive RSI pivot prominence used
    # to build this result. Purely additive; defaults to the legacy floor.
    rsi_prominence: float = 2.0

    def context_export(self) -> Dict[str, Any]:
        return {
            "adx": (round(self.adx, 2) if np.isfinite(self.adx) else None),
            "regime_factor": round(self.regime_factor, 3),
            "volatility_regime": self.volatility_regime,
            "allow_regular": self.allow_regular,
            "lookback": self.lookback_used,
            "confirm_bars": self.confirm_bars_used,
            "rsi_prominence": round(self.rsi_prominence, 3),
        }


@dataclass(frozen=True)
class FetchResult:
    status: ScanStatus
    df: Optional[pd.DataFrame] = None
    message: str = ""
    abort_scan: bool = False


@dataclass
class TFCell:
    rsi: float = float("nan")
    divergence: str = DivergenceLabel.AUCUNE.value
    data_available: bool = False
    last_bar_age_seconds: Optional[float] = None
    divergence_strength: float = 0.0
    divergence_kind: str = "REGULAR"
    # [REFONTE — exigence A/F] full payload carried to the JSON layer.
    # Defaults keep to_dict() shape backward-compatible.
    divergence_payload: Optional[Dict[str, Any]] = None
    divergence_candidates: Tuple[Dict[str, Any], ...] = ()
    divergence_context: Optional[Dict[str, Any]] = None
    # ── [AUDIT-P05] signaux de premier rang et traçabilité PAR CELLULE ──
    status: str = ScanStatus.OK.value          # statut par timeframe
    last_bar_time: Optional[str] = None        # horodatage ISO 8601 de la barre
    zone: str = "UNKNOWN"                      # partition RSI fermée
    midline: Optional[Dict[str, Any]] = None   # bloc midline 50 (jamais null en sortie)
    div_actionable: bool = False               # actionnabilité explicite au niveau plat

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rsi": self.rsi,
            "divergence": self.divergence,
            "data_available": self.data_available,
            "last_bar_age_seconds": self.last_bar_age_seconds,
            "divergence_strength": self.divergence_strength,
            "divergence_kind": self.divergence_kind,
            "divergence_payload": self.divergence_payload,
            "divergence_candidates": list(self.divergence_candidates),
            "divergence_context": self.divergence_context,
            # ── [AUDIT-P05] ──
            "status": self.status,
            "last_bar_time": self.last_bar_time,
            "zone": self.zone,
            "midline": self.midline,
            "div_actionable": self.div_actionable,
        }


@dataclass
class AssetResult:
    pair: str
    status: ScanStatus = ScanStatus.OK
    timeframes: Dict[str, TFCell] = field(default_factory=dict)
    abort_scan: bool = False
    error_messages: List[str] = field(default_factory=list)


# =============================================================================
# 4. LOGGING (with redaction & scan correlation, thread-safe init)
# =============================================================================

_REDACT_PATTERNS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\b[a-f0-9]{32,}\b"), "<REDACTED_HEX>"),
    (re.compile(r"\b\d{3,}-\d{3,}-\d{6,}-\d{3,}\b"), "<REDACTED_ACCOUNT>"),
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE), "Bearer <REDACTED>"),
)


class _RedactingFilter(logging.Filter):  # pylint: disable=too-few-public-methods
    """Redacts sensitive tokens from log records. Never raises."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            for pat, repl in _REDACT_PATTERNS:
                message = pat.sub(repl, message)
            record.msg = message
            record.args = ()
        except (TypeError, ValueError, AttributeError):
            pass
        if not hasattr(record, "scan_id"):
            record.scan_id = "-"
        return True


class _LoggerState:  # pylint: disable=too-few-public-methods
    """Encapsulates the global logger init flag. Thread-safe."""
    __slots__ = ("_initialized", "_lock")

    def __init__(self) -> None:
        self._initialized = False
        self._lock = threading.Lock()

    def configure(self) -> logging.Logger:
        log = logging.getLogger("rsi_screener")
        if self._initialized:
            return log
        with self._lock:
            if self._initialized:
                return log
            log.setLevel(logging.INFO)
            for handler in list(log.handlers):
                log.removeHandler(handler)
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                "%(asctime)s [%(levelname)s] [scan=%(scan_id)s] "
                "%(name)s: %(message)s"
            ))
            handler.addFilter(_RedactingFilter())
            log.addHandler(handler)
            log.propagate = False
            self._initialized = True
        return log


_LOGGER_STATE = _LoggerState()
logger = _LOGGER_STATE.configure()


# =============================================================================
# 5. [DOMAIN] pure functions, no I/O, no Streamlit
# =============================================================================

def calculate_rsi(
    close: pd.Series,
    period: int = RSI_PERIOD,
) -> Tuple[float, Optional[pd.Series]]:
    """Wilder's RSI, fully vectorised via pandas EWM.

    Numerically stable: handles divide-by-zero via masked numpy ops without
    triggering RuntimeWarnings. Returns (last_value_or_nan, full_series_or_None).
    """
    if close is None or len(close) < period + 1:
        return float("nan"), None

    close_f = close.astype(np.float64)
    delta = close_f.diff()

    gains = delta.clip(lower=0.0)
    losses = (-delta).clip(lower=0.0)

    alpha = 1.0 / period
    avg_gain = gains.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
    avg_loss = losses.ewm(alpha=alpha, adjust=False, min_periods=period).mean()

    rsi_series = pd.Series(
        np.full(len(close_f), np.nan), index=close_f.index, name="RSI"
    )

    valid_mask = avg_gain.notna() & avg_loss.notna()
    gain_vals = avg_gain[valid_mask].to_numpy()
    loss_vals = avg_loss[valid_mask].to_numpy()

    rsi_vals = np.empty_like(gain_vals)
    flat = (gain_vals == 0.0) & (loss_vals == 0.0)
    pure_up = (loss_vals == 0.0) & ~flat
    pure_dn = (gain_vals == 0.0) & ~flat
    normal = ~(flat | pure_up | pure_dn)

    rsi_vals[flat] = 50.0
    rsi_vals[pure_up] = 100.0
    rsi_vals[pure_dn] = 0.0
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi_vals[normal] = (
            100.0 - 100.0 / (1.0 + gain_vals[normal] / loss_vals[normal])
        )

    rsi_series.loc[valid_mask] = rsi_vals

    last = rsi_series.iloc[-1]
    return (float(last) if pd.notna(last) else float("nan"), rsi_series)


def calculate_atr(df: pd.DataFrame, period: int = ATR_PERIOD) -> float:
    """True Range ATR, vectorised. Returns NaN if insufficient or invalid."""
    if df is None or len(df) < period + 1:
        return float("nan")
    try:
        high = df["High"].to_numpy(np.float64)
        low = df["Low"].to_numpy(np.float64)
        close_prev = df["Close"].shift(1).to_numpy(np.float64)
    except (KeyError, ValueError, TypeError):
        return float("nan")

    tr = np.maximum.reduce([
        high - low,
        np.abs(high - close_prev),
        np.abs(low - close_prev),
    ])
    tr_series = pd.Series(tr).dropna()
    if len(tr_series) < period:
        return float("nan")
    atr = tr_series.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    val = atr.iloc[-1]
    return float(val) if pd.notna(val) and val > 0 else float("nan")


def _classify_asset(pair: str) -> str:
    if pair == "XAU_USD":
        return "GOLD"
    if any(idx in pair for idx in ("SPX500", "NAS100", "US30", "DE30")):
        return "INDEX"
    if "JPY" in pair:
        return "JPY"
    return "FX"


def _get_price_delta_multiplier(pair: str) -> float:
    return _ASSET_CLASS_MULTIPLIERS[_classify_asset(pair)]


# [QUANT-FIX] Dynamic lookback per timeframe (exigence E). A fixed 50-bar window
# under-captures large-amplitude divergences on Daily/Weekly and is needlessly
# long on intraday. Returns a hard-coded integer with a safe fallback to the
# preserved DIV_LOOKBACK_BARS constant for unknown keys; the constant itself is
# intentionally NOT removed (still the default of detect_divergence).
_LOOKBACK_BY_TF: Mapping[str, int] = {
    "M15": 80, "H1": 80, "H4": 100, "D": 120, "W": 60,
}


def _get_lookback(tf_key: str) -> int:
    return _LOOKBACK_BY_TF.get(tf_key, DIV_LOOKBACK_BARS)


# [QUANT-FIX] Adaptive post-pivot confirmation per timeframe (exigence F). A
# single 5-bar confirmation is too slow on M15 and acceptable on D/W.
# PEAK_DISTANCE_BARS (the minimum inter-pivot distance fed to find_peaks) is
# left untouched; only the post-pivot maturity gate changes, with a fallback
# to PEAK_DISTANCE_BARS.
_CONFIRM_BARS_BY_TF: Mapping[str, int] = {
    "M15": 3, "H1": 3, "H4": 4, "D": 5, "W": 5,
}


def _get_confirmation_bars(tf_key: str) -> int:
    return _CONFIRM_BARS_BY_TF.get(tf_key, PEAK_DISTANCE_BARS)


# Expected RSI pivot amplitude (in RSI points) used by the matcher. Derived
# universally from the minimum-delta gate and the zone geometry; NOT calibrated
# per asset.
_EXPECTED_RSI_AMPLITUDE: float = RSI_NEUTRAL_THRESHOLD - RSI_BULL_DIV_ZONE + MIN_RSI_DELTA


# [REFONTE — exigence C: BUG-5 FIX] Amplitude-aware pivot matching with
# UNIT-CONSISTENT comparison. The previous version compared an RSI amplitude
# (0-100 scale) against `price_prominence` (price units) — incoherent across
# asset classes. We now derive `expected_rsi_amplitude` IN RSI UNITS so both
# terms of the score live on the same scale. When no amplitude context is
# supplied the function degrades EXACTLY to the original "minimise |idx delta|"
# behaviour (zero regression). PURE — no mutation of `used`.
def _match_pivot_pure(
    price_idx: int,
    rsi_peaks: np.ndarray,
    used: frozenset,
    tolerance: int = PIVOT_MATCH_TOLERANCE,
    rsi_values: Optional[np.ndarray] = None,
    expected_rsi_amplitude: Optional[float] = None,
) -> Tuple[Optional[int], float]:
    """Match a price pivot to a nearby unused RSI pivot. PURE — no mutation.

    Returns (matched_idx_or_None, match_confidence[0,1]).
    Scoring (all RSI-unit-consistent):
        cost = temporal_gap + beta * |rsi_amp - expected_rsi_amplitude| / 100
    Confidence is 1 - normalised_temporal_gap, clamped to [0,1].
    """
    if len(rsi_peaks) == 0:
        return None, 0.0
    candidates = [
        int(p) for p in rsi_peaks
        if int(p) not in used and abs(int(p) - price_idx) <= tolerance
    ]
    if not candidates:
        return None, 0.0

    tol = max(1, tolerance)

    if rsi_values is None or expected_rsi_amplitude is None:
        best = min(candidates, key=lambda p: abs(p - price_idx))
        conf = _clamp01_local(1.0 - abs(best - price_idx) / tol)
        return best, conf

    beta = 0.5
    n = len(rsi_values)

    def _cost(p: int) -> float:
        temporal = abs(p - price_idx) / tol
        if 0 <= p < n:
            amp = abs(float(rsi_values[p]) - RSI_NEUTRAL_THRESHOLD)
        else:
            amp = expected_rsi_amplitude
        # both terms dimensionless: temporal in [0,1], amp-gap scaled by /100
        return temporal + beta * abs(amp - expected_rsi_amplitude) / 100.0

    best = min(candidates, key=_cost)
    conf = _clamp01_local(1.0 - abs(best - price_idx) / tol)
    return best, conf


def _is_confirmed(pivot_idx: int, last_idx: int, confirm_bars: int) -> bool:
    return (last_idx - pivot_idx) >= confirm_bars


# ── [AUDIT-P07] Balayage de paires de pivots (opt-in) ────────────────────────
# Les détecteurs ne lisent QUE `price_pivots[-1]` et `[-2]` : ce sont les seules
# lectures de `price_pivots` dans les quatre détecteurs (vérifié par grep). On
# peut donc leur présenter une paire arbitraire en fabriquant un tableau de deux
# pivots. Quand `ENABLE_MULTI_PIVOT_SCAN` est faux, `_pivot_pair_contexts`
# retourne EXACTEMENT le contexte d'origine : le comportement reste alors
# identique à celui d'avant le correctif.
def _pivot_pair_contexts(
    ctx: _PivotContext, max_k: int = PIVOT_MAX_PAIRS,
) -> List[_PivotContext]:
    """Contextes des paires (-1,-2), (-1,-3), ... selon le drapeau."""
    n = len(ctx.price_pivots)
    if not ENABLE_MULTI_PIVOT_SCAN:
        return [ctx]
    if n < 2:
        return []
    kmax = min(max_k, n - 1)
    out: List[_PivotContext] = []
    for k in range(1, kmax + 1):
        if k == 1:
            out.append(ctx)
            continue
        synth = np.asarray(
            [ctx.price_pivots[-1 - k], ctx.price_pivots[-1]],
            dtype=ctx.price_pivots.dtype,
        )
        out.append(replace(ctx, price_pivots=synth))
    return out


def _scan_pairs(
    detector: Callable[
        [_PivotContext],
        Optional[Tuple[DivergenceSignal, Tuple[int, int]]],
    ],
    ctx: _PivotContext,
    used: frozenset,
) -> Tuple[List[DivergenceSignal], frozenset]:
    """Applique un détecteur à toutes les paires candidates en réservant les
    pivots RSI déjà consommés : aucun pivot ne peut être réutilisé."""
    signals: List[DivergenceSignal] = []
    used_cur = used
    for pair_ctx in _pivot_pair_contexts(ctx):
        found = detector(pair_ctx)
        if found is None:
            continue
        sig, matched = found
        if set(matched) & set(used_cur):
            continue
        signals.append(sig)
        used_cur = used_cur | set(matched)
    return signals, used_cur


# [QUANT-FIX — FAIBLESSE-7] Staleness now scales with the timeframe's own
# confirmation horizon rather than fixed bars. A pivot is stale once it is older
# than its confirmation window plus the standard max-age slack. Falls back to
# the legacy constant sum when confirm_bars is the default, so pre-existing
# behaviour on default TFs is preserved.
def _is_stale(
    pivot_idx: int, last_idx: int, confirm_bars: int = PEAK_DISTANCE_BARS,
) -> bool:
    horizon = PIVOT_MAX_AGE_BARS + max(confirm_bars, PEAK_DISTANCE_BARS)
    return (last_idx - pivot_idx) > horizon


# [QUANT-FIX] Local EMA helper for hidden-divergence regime confirmation
# (exigence A). Hidden divergences are only valid in a confirmed trend; without
# a trend filter they fire constantly in chop. The EMA is computed ONLY on the
# supplied lookback slice (no mutation of the global DataFrame) and returns NaN
# when data is insufficient, letting callers safely skip the signal.
def _local_ema(values: np.ndarray, span: int = 20) -> float:
    if values is None or len(values) < span:
        return float("nan")
    ema = pd.Series(values).ewm(span=span, adjust=False, min_periods=span).mean()
    last = ema.iloc[-1]
    return float(last) if pd.notna(last) else float("nan")


# [QUANT-FIX] Simplified ADX over the lookback (exigence G). Regular divergences
# are unreliable in low-directionality ranges; desks gate them behind a
# trend-strength floor. Adds NO new dependency (pure numpy/pandas), returns NaN
# on insufficient data so the caller can fail-open, and never raises.
def _simplified_adx(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14,
) -> float:
    n = len(close)
    if n < period * 2 + 1:
        return float("nan")
    try:
        up_move = high[1:] - high[:-1]
        down_move = low[:-1] - low[1:]
        plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
        minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

        prev_close = close[:-1]
        tr = np.maximum.reduce([
            high[1:] - low[1:],
            np.abs(high[1:] - prev_close),
            np.abs(low[1:] - prev_close),
        ])

        alpha = 1.0 / period
        tr_s = pd.Series(tr).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
        plus_s = pd.Series(plus_dm).ewm(alpha=alpha, adjust=False, min_periods=period).mean()
        minus_s = pd.Series(minus_dm).ewm(alpha=alpha, adjust=False, min_periods=period).mean()

        with np.errstate(divide="ignore", invalid="ignore"):
            plus_di = 100.0 * (plus_s / tr_s)
            minus_di = 100.0 * (minus_s / tr_s)
            di_sum = plus_di + minus_di
            dx = 100.0 * (plus_di - minus_di).abs() / di_sum.replace(0.0, np.nan)

        adx = dx.ewm(alpha=alpha, adjust=False, min_periods=period).mean()
        val = adx.iloc[-1]
        return float(val) if pd.notna(val) else float("nan")
    except (ValueError, TypeError, IndexError, ZeroDivisionError):
        return float("nan")


# [QUANT-FIX] Volatility-regime prominence factor (exigence C). A fixed per-asset
# multiplier ignores whether the market is currently quiet (noisy micro-pivots)
# or expansive (clean swings), producing range false-positives. Computes a
# rolling-ATR percentile purely on the lookback slice and returns (1.0, "normal")
# when data is insufficient, so the asset-class base multiplier is preserved.
def _volatility_regime_factor(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = ATR_PERIOD,
) -> Tuple[float, str]:
    """Returns (prominence_factor, regime_label).

    regime_label in {quiet, normal, expansive}. Pure; (1.0, 'normal') on
    insufficient data.
    """
    n = len(close)
    if n < period + 2:
        return 1.0, "normal"
    try:
        prev_close = close[:-1]
        tr = np.maximum.reduce([
            high[1:] - low[1:],
            np.abs(high[1:] - prev_close),
            np.abs(low[1:] - prev_close),
        ])
        atr_roll = pd.Series(tr).ewm(
            alpha=1.0 / period, adjust=False, min_periods=period,
        ).mean().dropna().to_numpy()
        if len(atr_roll) < period:
            return 1.0, "normal"
        current = atr_roll[-1]
        p25 = float(np.nanpercentile(atr_roll, 25))
        p75 = float(np.nanpercentile(atr_roll, 75))
        if current >= p75:
            return 0.7, "expansive"
        if current <= p25:
            return 1.3, "quiet"
        return 1.0, "normal"
    except (ValueError, TypeError, IndexError):
        return 1.0, "normal"


# [QUANT-FIX] Divergence quality score (exigence D). Every confirmed divergence
# was treated identically; desks need to discard weak noise-driven signals and
# rank institutional setups. Every input is guarded (ATR>0, finite checks),
# volume falls back to 0.5 when absent, and the score is clamped to [0,1].
# Both displacement terms are dimensionless (BUG-4 confirmed NON-bug):
# price_displacement is an ATR ratio, rsi_displacement an RSI fraction.
def _compute_strength_score(
    price_prev: float,
    price_last: float,
    rsi_prev: float,
    rsi_last: float,
    atr: float,
    volume_at_last: Optional[float],
    mean_volume: Optional[float],
) -> float:
    if not (np.isfinite(atr) and atr > 0):
        return 0.0
    price_displacement = abs(price_last - price_prev) / atr      # ATR units
    rsi_displacement = abs(rsi_last - rsi_prev) / 100.0          # RSI fraction

    if (volume_at_last is not None and mean_volume is not None
            and np.isfinite(volume_at_last) and np.isfinite(mean_volume)
            and mean_volume > 0):
        volume_factor = min(1.0, float(volume_at_last) / float(mean_volume))
    else:
        volume_factor = 0.5

    score = (
        0.4 * min(1.0, price_displacement / 2.0)
        + 0.4 * min(1.0, rsi_displacement / 0.3)
        + 0.2 * volume_factor
    )
    return float(max(0.0, min(1.0, score)))


# [QUANT-FIX] Context bundle: tf-aware confirmation, volume, the trend EMA, and
# the matching `used` set so that regular and hidden detectors cannot reuse the
# same RSI pivot within one lookback. Frozen, arg-bundling design preserved.
@dataclass(frozen=True)
class _PivotContext:
    price_close: np.ndarray
    rsi_filled: np.ndarray
    price_pivots: np.ndarray
    rsi_pivots: np.ndarray
    price_prominence: float
    last_idx: int
    atr: float
    confirm_bars: int
    ema20: float
    volume: Optional[np.ndarray] = None
    mean_volume: Optional[float] = None
    used_rsi: frozenset = frozenset()


# [REFONTE — exigence B/C] Pivot-pair resolution threads the amplitude-aware,
# unit-consistent matcher and respects an externally-supplied `used_rsi` set.
# Returns None whenever either match fails or collapses to the same RSI pivot;
# returns the matched RSI indices and aggregate match confidence so the caller
# can mark pivots used and export confidence.
def _resolve_pivot_pair(
    ctx: _PivotContext, last_p: int, prev_p: int,
) -> Optional[Tuple[float, float, int, int, float]]:
    """Match the two latest price pivots to RSI pivots. Pure.

    Returns (rsi_prev_val, rsi_last_val, matched_prev_idx, matched_last_idx,
             match_confidence).
    """
    matched_prev, conf_prev = _match_pivot_pure(
        prev_p, ctx.rsi_pivots, ctx.used_rsi,
        rsi_values=ctx.rsi_filled, expected_rsi_amplitude=_EXPECTED_RSI_AMPLITUDE,
    )
    if matched_prev is None:
        return None
    matched_last, conf_last = _match_pivot_pure(
        last_p, ctx.rsi_pivots, ctx.used_rsi | {matched_prev},
        rsi_values=ctx.rsi_filled, expected_rsi_amplitude=_EXPECTED_RSI_AMPLITUDE,
    )
    if matched_last is None or matched_prev == matched_last:
        return None
    match_conf = float((conf_prev + conf_last) / 2.0)
    return (
        float(ctx.rsi_filled[matched_prev]),
        float(ctx.rsi_filled[matched_last]),
        matched_prev,
        matched_last,
        match_conf,
    )


# [REFONTE — exigence A/B/D] Builds an enriched signal. The _TIER_T3 (0.25)
# floor still rejects noise but everything above is preserved WITH full
# metadata. STALE demotion semantics from the original are kept.
def _build_div_signal(
    label: DivergenceLabel,
    *,
    price_last_idx: int,
    price_prev_idx: int,
    rsi_last_idx: int,
    rsi_prev_idx: int,
    rsi_prev_val: float,
    rsi_last_val: float,
    price_prev_val: float,
    price_last_val: float,
    last_idx: int,
    confirm_bars: int,
    strength: float,
    match_confidence: float,
    kind: str,
) -> Optional[DivergenceSignal]:
    # [AUDIT-P06] NE JAMAIS DÉTRUIRE UN SIGNAL. Le plancher `_TIER_T3`
    # séparait autrefois « signal publiable » de « bruit » en SUPPRIMANT le
    # second : il devenait invisible ET non quantifiable pour l'aval. Le signal
    # est désormais CONSERVÉ, marqué T4 et non actionnable. La décision
    # actionnable reste identique (voir `_is_actionable` et `_select_divergence`).
    if not np.isfinite(strength):
        return None
    age = last_idx - price_last_idx
    common = dict(
        pivot_index=price_last_idx,
        strength_score=strength,
        kind=kind,
        confidence_score=_clamp01_local(match_confidence),
        quality_tier=_quality_tier(strength),
        divergence_age_bars=age,
        pivot_age_bars=age,
        price_pivot_prev_idx=price_prev_idx,
        price_pivot_last_idx=price_last_idx,
        rsi_pivot_prev_idx=rsi_prev_idx,
        rsi_pivot_last_idx=rsi_last_idx,
        rsi_prev_value=rsi_prev_val,
        rsi_last_value=rsi_last_val,
        price_prev_value=price_prev_val,
        price_last_value=price_last_val,
        pivot_distance_bars=abs(price_last_idx - price_prev_idx),
        match_distance_bars=abs(price_last_idx - rsi_last_idx),
    )
    if _is_stale(price_last_idx, last_idx, confirm_bars):
        return DivergenceSignal(DivergenceLabel.STALE, confirmed=False, **common)
    return DivergenceSignal(label, confirmed=True, **common)


def _vol_at(ctx: _PivotContext, idx: int) -> Optional[float]:
    if ctx.volume is None or not (0 <= idx < len(ctx.volume)):
        return None
    return float(ctx.volume[idx])


# [QUANT-FIX] Regular bearish divergence: price HH + RSI LH, both pivots in
# bear zone. Strength-scored and tf-aware. All original geometric/zone gates
# preserved verbatim; the returned RSI pivot indices are exposed so the
# orchestrator can reserve them against hidden-divergence reuse.
def _check_bearish_divergence(
    ctx: _PivotContext,
) -> Optional[Tuple[DivergenceSignal, Tuple[int, int]]]:
    """Bearish: price HH + RSI LH, both pivots in bear zone."""
    if len(ctx.price_pivots) < 2:
        return None
    last_p = int(ctx.price_pivots[-1])
    prev_p = int(ctx.price_pivots[-2])
    if not _is_confirmed(last_p, ctx.last_idx, ctx.confirm_bars):
        return None
    resolved = _resolve_pivot_pair(ctx, last_p, prev_p)
    if resolved is None:
        return None
    rsi_prev, rsi_last, m_prev, m_last, mconf = resolved

    if ctx.price_close[last_p] <= ctx.price_close[prev_p] + ctx.price_prominence * 0.5:
        return None
    if not (RSI_BEAR_DIV_ZONE <= rsi_prev
            and RSI_NEUTRAL_THRESHOLD <= rsi_last < rsi_prev - MIN_RSI_DELTA):
        return None

    strength = _compute_strength_score(
        ctx.price_close[prev_p], ctx.price_close[last_p],
        rsi_prev, rsi_last, ctx.atr, _vol_at(ctx, last_p), ctx.mean_volume,
    )
    signal = _build_div_signal(
        DivergenceLabel.BAISSIERE,
        price_last_idx=last_p, price_prev_idx=prev_p,
        rsi_last_idx=m_last, rsi_prev_idx=m_prev,
        rsi_prev_val=rsi_prev, rsi_last_val=rsi_last,
        price_prev_val=float(ctx.price_close[prev_p]),
        price_last_val=float(ctx.price_close[last_p]),
        last_idx=ctx.last_idx, confirm_bars=ctx.confirm_bars,
        strength=strength, match_confidence=mconf, kind="REGULAR",
    )
    if signal is None:
        return None
    return signal, (m_prev, m_last)


# [QUANT-FIX] Regular bullish divergence: price LL + RSI HL, both pivots in
# bull zone. Same upgrades as bearish. Original gates preserved verbatim.
def _check_bullish_divergence(
    ctx: _PivotContext,
) -> Optional[Tuple[DivergenceSignal, Tuple[int, int]]]:
    """Bullish: price LL + RSI HL, both pivots in bull zone."""
    if len(ctx.price_pivots) < 2:
        return None
    last_t = int(ctx.price_pivots[-1])
    prev_t = int(ctx.price_pivots[-2])
    if not _is_confirmed(last_t, ctx.last_idx, ctx.confirm_bars):
        return None
    resolved = _resolve_pivot_pair(ctx, last_t, prev_t)
    if resolved is None:
        return None
    rsi_prev, rsi_last, m_prev, m_last, mconf = resolved

    if ctx.price_close[last_t] >= ctx.price_close[prev_t] - ctx.price_prominence * 0.5:
        return None
    if not (rsi_prev <= RSI_BULL_DIV_ZONE
            and rsi_prev + MIN_RSI_DELTA < rsi_last <= RSI_NEUTRAL_THRESHOLD):
        return None

    strength = _compute_strength_score(
        ctx.price_close[prev_t], ctx.price_close[last_t],
        rsi_prev, rsi_last, ctx.atr, _vol_at(ctx, last_t), ctx.mean_volume,
    )
    signal = _build_div_signal(
        DivergenceLabel.HAUSSIERE,
        price_last_idx=last_t, price_prev_idx=prev_t,
        rsi_last_idx=m_last, rsi_prev_idx=m_prev,
        rsi_prev_val=rsi_prev, rsi_last_val=rsi_last,
        price_prev_val=float(ctx.price_close[prev_t]),
        price_last_val=float(ctx.price_close[last_t]),
        last_idx=ctx.last_idx, confirm_bars=ctx.confirm_bars,
        strength=strength, match_confidence=mconf, kind="REGULAR",
    )
    if signal is None:
        return None
    return signal, (m_prev, m_last)


# [QUANT-FIX] Hidden bullish divergence (exigence A): price HL + RSI LL, in an
# uptrend. Continuation setups were entirely invisible to the original engine.
# (1) EMA20 regime filter requires price above trend before firing; (2) honours
# externally-reserved RSI pivots; (3) strength-scored and confirmation-gated
# identically to regular divergences.
def _check_hidden_bullish_divergence(
    ctx: _PivotContext,
) -> Optional[Tuple[DivergenceSignal, Tuple[int, int]]]:
    """Hidden bullish: price Higher-Low + RSI Lower-Low, in an uptrend."""
    if len(ctx.price_pivots) < 2 or not np.isfinite(ctx.ema20):
        return None
    last_t = int(ctx.price_pivots[-1])
    prev_t = int(ctx.price_pivots[-2])
    if not _is_confirmed(last_t, ctx.last_idx, ctx.confirm_bars):
        return None
    if ctx.price_close[ctx.last_idx] <= ctx.ema20:
        return None
    resolved = _resolve_pivot_pair(ctx, last_t, prev_t)
    if resolved is None:
        return None
    rsi_prev, rsi_last, m_prev, m_last, mconf = resolved

    if ctx.price_close[last_t] <= ctx.price_close[prev_t] + ctx.price_prominence * 0.5:
        return None
    if not (rsi_last < rsi_prev - MIN_RSI_DELTA and rsi_last <= RSI_BULL_DIV_ZONE):
        return None

    strength = _compute_strength_score(
        ctx.price_close[prev_t], ctx.price_close[last_t],
        rsi_prev, rsi_last, ctx.atr, _vol_at(ctx, last_t), ctx.mean_volume,
    )
    signal = _build_div_signal(
        DivergenceLabel.HAUSSIERE,
        price_last_idx=last_t, price_prev_idx=prev_t,
        rsi_last_idx=m_last, rsi_prev_idx=m_prev,
        rsi_prev_val=rsi_prev, rsi_last_val=rsi_last,
        price_prev_val=float(ctx.price_close[prev_t]),
        price_last_val=float(ctx.price_close[last_t]),
        last_idx=ctx.last_idx, confirm_bars=ctx.confirm_bars,
        strength=strength, match_confidence=mconf, kind="HIDDEN",
    )
    if signal is None:
        return None
    return signal, (m_prev, m_last)


# [QUANT-FIX] Hidden bearish divergence (exigence A): price LH + RSI HH, in a
# downtrend. Mirror of hidden bullish: EMA20 requires price BELOW trend, RSI
# must make a Higher-High in the bear zone.
def _check_hidden_bearish_divergence(
    ctx: _PivotContext,
) -> Optional[Tuple[DivergenceSignal, Tuple[int, int]]]:
    """Hidden bearish: price Lower-High + RSI Higher-High, in a downtrend."""
    if len(ctx.price_pivots) < 2 or not np.isfinite(ctx.ema20):
        return None
    last_p = int(ctx.price_pivots[-1])
    prev_p = int(ctx.price_pivots[-2])
    if not _is_confirmed(last_p, ctx.last_idx, ctx.confirm_bars):
        return None
    if ctx.price_close[ctx.last_idx] >= ctx.ema20:
        return None
    resolved = _resolve_pivot_pair(ctx, last_p, prev_p)
    if resolved is None:
        return None
    rsi_prev, rsi_last, m_prev, m_last, mconf = resolved

    if ctx.price_close[last_p] >= ctx.price_close[prev_p] - ctx.price_prominence * 0.5:
        return None
    if not (rsi_last > rsi_prev + MIN_RSI_DELTA and rsi_last >= RSI_BEAR_DIV_ZONE):
        return None

    strength = _compute_strength_score(
        ctx.price_close[prev_p], ctx.price_close[last_p],
        rsi_prev, rsi_last, ctx.atr, _vol_at(ctx, last_p), ctx.mean_volume,
    )
    signal = _build_div_signal(
        DivergenceLabel.BAISSIERE,
        price_last_idx=last_p, price_prev_idx=prev_p,
        rsi_last_idx=m_last, rsi_prev_idx=m_prev,
        rsi_prev_val=rsi_prev, rsi_last_val=rsi_last,
        price_prev_val=float(ctx.price_close[prev_p]),
        price_last_val=float(ctx.price_close[last_p]),
        last_idx=ctx.last_idx, confirm_bars=ctx.confirm_bars,
        strength=strength, match_confidence=mconf, kind="HIDDEN",
    )
    if signal is None:
        return None
    return signal, (m_prev, m_last)


# [QUANT-FIX] Array prep also returns aligned volume for scoring. Volume
# extraction is best-effort (returns None on absence/failure); the NaN-ratio
# gate logic on RSI/close is preserved exactly.
def _prepare_divergence_arrays(
    price_data: pd.DataFrame,
    rsi_series: pd.Series,
    lookback: int,
) -> Optional[Tuple[np.ndarray, np.ndarray, Optional[np.ndarray]]]:
    recent_rsi = rsi_series.iloc[-lookback:].to_numpy(np.float64)
    recent_close = price_data["Close"].iloc[-lookback:].to_numpy(np.float64)

    nan_ratio = max(
        float(np.isnan(recent_rsi).mean()),
        float(np.isnan(recent_close).mean()),
    )
    if nan_ratio > MAX_NAN_RATIO_FOR_DIV:
        return None

    if np.isnan(recent_rsi).any():
        recent_rsi = pd.Series(recent_rsi).interpolate(
            method="linear", limit_area="inside", limit=2
        ).to_numpy()
        if np.isnan(recent_rsi).any():
            return None

    if np.isnan(recent_close).any():
        return None

    recent_volume: Optional[np.ndarray] = None
    try:
        if "Volume" in price_data.columns:
            vol = price_data["Volume"].iloc[-lookback:].to_numpy(np.float64)
            if not np.isnan(vol).all():
                recent_volume = np.nan_to_num(vol, nan=0.0)
    except (KeyError, ValueError, TypeError):
        recent_volume = None

    return recent_close, recent_rsi, recent_volume


# [REFONTE — exigence D/F] Selection RANKS but PRESERVES. Returns the winner AND
# the full ordered candidate list. Confirmed signals always outrank STALE;
# within a class, higher strength wins, REGULAR breaks exact ties over HIDDEN.
# Deterministic sort key: (confirmed_rank, -strength, kind_rank).
def _select_divergence(
    candidates: List[Optional[DivergenceSignal]],
) -> Tuple[DivergenceSignal, Tuple[DivergenceSignal, ...]]:
    present = [c for c in candidates if c is not None]
    if not present:
        none_sig = DivergenceSignal(DivergenceLabel.AUCUNE)
        return none_sig, ()

    def _sort_key(sig: DivergenceSignal) -> Tuple[int, float, int]:
        confirmed_rank = 0 if sig.label != DivergenceLabel.STALE else 1
        kind_rank = 0 if sig.kind == "REGULAR" else 1
        return (confirmed_rank, -sig.strength_score, kind_rank)

    ordered = sorted(present, key=_sort_key)
    # [AUDIT-P06] Le GAGNANT est choisi exactement comme avant le correctif :
    # parmi les seuls signaux qui franchissaient le plancher `_TIER_T3` (que
    # `_build_div_signal` supprimait auparavant). Les signaux faibles désormais
    # conservés apparaissent dans `candidates` mais ne peuvent PAS devenir
    # gagnants : la sortie gagnante est donc rigoureusement identique.
    eligible = [s for s in ordered if s.strength_score >= _TIER_T3]
    winner = eligible[0] if eligible else DivergenceSignal(DivergenceLabel.AUCUNE)
    return winner, tuple(ordered)


def _find_pivots(
    recent_close: np.ndarray,
    recent_rsi: np.ndarray,
    price_prom: float,
    rsi_prom: float,
) -> Optional[Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
    """Locate peaks/troughs in price & RSI. Returns None on numerical failure."""
    if len(recent_close) < PEAK_DISTANCE_BARS * 2:
        return None
    try:
        price_peaks, _ = find_peaks(
            recent_close, distance=PEAK_DISTANCE_BARS, prominence=price_prom,
        )
        price_troughs, _ = find_peaks(
            -recent_close, distance=PEAK_DISTANCE_BARS, prominence=price_prom,
        )
        rsi_peaks, _ = find_peaks(
            recent_rsi, distance=PEAK_DISTANCE_BARS, prominence=rsi_prom,
        )
        rsi_troughs, _ = find_peaks(
            -recent_rsi, distance=PEAK_DISTANCE_BARS, prominence=rsi_prom,
        )
    except (ValueError, RuntimeError):
        logger.exception("find_peaks failed")
        return None
    return price_peaks, price_troughs, rsi_peaks, rsi_troughs


# ── [FIX-1 REVISED] Prominence RSI dérivée de la dispersion réalisée. ──
# Motif : `price_prom` est déjà adaptatif (ATR x classe d'actif x régime de
# volatilité) alors que `rsi_prom` était une constante littérale (2.0). On
# rétablit la symétrie de conception par le MÊME principe (dispersion x
# coefficient) plutôt que par un ladder codé en dur par timeframe — un ladder
# fixe (a) mute silencieusement pour tout appelant qui laisse tf_key=None
# (wrapper legacy, backtests, tests unitaires) et (b) produit des falaises
# binaires non calibrées sur les timeframes seniors (W1/D1).
# Le plancher est fixé à la valeur legacy 2.0 : la baseline déjà validée en
# production devient la borne inférieure stricte, donc zéro régression par
# construction. Ne dépend PAS de tf_key.
_RSI_PROM_K: float = 0.35
_RSI_PROM_FLOOR: float = 2.0
_RSI_PROM_CEIL: float = 8.0


def _adaptive_rsi_prominence(recent_rsi: np.ndarray) -> float:
    """Prominence de pivot RSI proportionnelle à la dispersion locale. Pure.

    Dégrade vers _RSI_PROM_FLOOR (== ancienne constante) sur données
    insuffisantes ou dégénérées, ce qui neutralise toute mutation silencieuse
    du défaut legacy.
    """
    if recent_rsi is None or len(recent_rsi) < RSI_PERIOD:
        return _RSI_PROM_FLOOR
    try:
        sigma = float(np.nanstd(recent_rsi))
    except (ValueError, TypeError):
        return _RSI_PROM_FLOOR
    if not np.isfinite(sigma) or sigma <= 0.0:
        return _RSI_PROM_FLOOR
    return float(min(_RSI_PROM_CEIL, max(_RSI_PROM_FLOOR, _RSI_PROM_K * sigma)))


# [REFONTE — orchestrateur enrichi] detect_divergence_full returns the rich
# DivergenceResult for the fusion-aware path (used by _compute_cell). The public
# detect_divergence() STILL returns a DivergenceSignal (the winner) so every
# existing caller keeps working — backward-compatible surface.
def detect_divergence_full(
    price_data: pd.DataFrame,
    rsi_series: Optional[pd.Series],
    pair: str,
    lookback: Optional[int] = None,
    tf_key: Optional[str] = None,
) -> DivergenceResult:
    """Detect regular & hidden RSI divergence; return winner + all candidates."""
    if lookback is None:
        lookback = _get_lookback(tf_key) if tf_key else DIV_LOOKBACK_BARS
    confirm_bars = _get_confirmation_bars(tf_key) if tf_key else PEAK_DISTANCE_BARS

    none_result = DivergenceResult(
        winner=DivergenceSignal(DivergenceLabel.AUCUNE),
        lookback_used=lookback, confirm_bars_used=confirm_bars,
    )
    if (rsi_series is None or len(price_data) < lookback
            or len(rsi_series) < lookback):
        return none_result

    arrays = _prepare_divergence_arrays(price_data, rsi_series, lookback)
    if arrays is None:
        return none_result
    recent_close, recent_rsi, recent_volume = arrays

    recent_slice = price_data.iloc[-lookback:]
    atr = calculate_atr(recent_slice)
    if not (np.isfinite(atr) and atr > 0):
        return none_result

    try:
        high_arr = recent_slice["High"].to_numpy(np.float64)
        low_arr = recent_slice["Low"].to_numpy(np.float64)
    except (KeyError, ValueError, TypeError):
        return none_result

    regime_factor, regime_label = _volatility_regime_factor(
        high_arr, low_arr, recent_close,
    )
    price_prom = atr * _get_price_delta_multiplier(pair) * regime_factor

    # [FIX-1 REVISED] Prominence RSI adaptative (dispersion locale), plancherée
    # à la valeur legacy 2.0. Remplace le seuil constant / ladder codé en dur.
    rsi_prom = _adaptive_rsi_prominence(recent_rsi)
    pivots = _find_pivots(recent_close, recent_rsi, price_prom, rsi_prom=rsi_prom)
    if pivots is None:
        return DivergenceResult(
            winner=DivergenceSignal(DivergenceLabel.AUCUNE),
            adx=_simplified_adx(high_arr, low_arr, recent_close),
            regime_factor=regime_factor, volatility_regime=regime_label,
            lookback_used=lookback, confirm_bars_used=confirm_bars,
            rsi_prominence=rsi_prom,
        )
    price_peaks, price_troughs, rsi_peaks, rsi_troughs = pivots

    last_idx = lookback - 1
    ema20 = _local_ema(recent_close, span=20)
    mean_volume = (
        float(np.mean(recent_volume))
        if recent_volume is not None and len(recent_volume) > 0
        else None
    )

    # Range filter: weak trend (ADX < 20) suppresses REGULAR divergences only;
    # hidden divergences (continuation) remain allowed. Fail-open on NaN ADX.
    adx = _simplified_adx(high_arr, low_arr, recent_close)
    allow_regular = (not np.isfinite(adx)) or (adx >= 20.0)

    def _ctx(pivots_arr: np.ndarray, rsi_arr: np.ndarray, used: frozenset) -> _PivotContext:
        return _PivotContext(
            price_close=recent_close, rsi_filled=recent_rsi,
            price_pivots=pivots_arr, rsi_pivots=rsi_arr,
            price_prominence=price_prom, last_idx=last_idx, atr=atr,
            confirm_bars=confirm_bars, ema20=ema20,
            volume=recent_volume, mean_volume=mean_volume, used_rsi=used,
        )

    candidates: List[Optional[DivergenceSignal]] = []
    used_peaks: frozenset = frozenset()
    used_troughs: frozenset = frozenset()

    # --- Regular divergences (gated by trend strength) ---
    if allow_regular:
        _bear_sigs, used_peaks = _scan_pairs(
            _check_bearish_divergence,
            _ctx(price_peaks, rsi_peaks, used_peaks), used_peaks,
        )
        candidates.extend(_bear_sigs)
        _bull_sigs, used_troughs = _scan_pairs(
            _check_bullish_divergence,
            _ctx(price_troughs, rsi_troughs, used_troughs), used_troughs,
        )
        candidates.extend(_bull_sigs)

    # --- Hidden divergences (always allowed; cannot reuse reserved RSI pivots) ---
    _hbear_sigs, used_peaks = _scan_pairs(
        _check_hidden_bearish_divergence,
        _ctx(price_peaks, rsi_peaks, used_peaks), used_peaks,
    )
    candidates.extend(_hbear_sigs)
    _hbull_sigs, used_troughs = _scan_pairs(
        _check_hidden_bullish_divergence,
        _ctx(price_troughs, rsi_troughs, used_troughs), used_troughs,
    )
    candidates.extend(_hbull_sigs)

    winner, ordered = _select_divergence(candidates)
    return DivergenceResult(
        winner=winner, candidates=ordered, adx=adx,
        regime_factor=regime_factor, volatility_regime=regime_label,
        allow_regular=allow_regular,
        lookback_used=lookback, confirm_bars_used=confirm_bars,
        rsi_prominence=rsi_prom,
    )


def detect_divergence(
    price_data: pd.DataFrame,
    rsi_series: Optional[pd.Series],
    pair: str,
    lookback: Optional[int] = None,
    tf_key: Optional[str] = None,
) -> DivergenceSignal:
    """Backward-compatible wrapper — returns the winning signal only."""
    return detect_divergence_full(
        price_data, rsi_series, pair, lookback=lookback, tf_key=tf_key,
    ).winner


# =============================================================================
# 6. [INFRA] OANDA client, HTTP session & candle cache
# =============================================================================

def _validate_ohlc_row(open_: float, high: float, low: float, close: float) -> bool:
    """Validate a single OHLC row with epsilon-tolerant comparisons."""
    vals = (open_, high, low, close)
    if any(not np.isfinite(v) for v in vals):
        return False
    if any(v <= 0 for v in vals):
        return False
    eps = _OHLC_EPSILON * max(abs(high), abs(low), 1.0)
    if (high + eps) < max(open_, close):
        return False
    if (low - eps) > min(open_, close):
        return False
    if (high + eps) < low:
        return False
    return True


def _parse_candle(raw: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """Parse a single raw candle into a normalized row or None."""
    if not raw.get("complete"):
        return None
    mid = raw.get("mid")
    ts = raw.get("time")
    if not isinstance(mid, Mapping) or not isinstance(ts, str):
        return None
    try:
        open_ = float(mid["o"])
        high = float(mid["h"])
        low = float(mid["l"])
        close = float(mid["c"])
        volume_raw = raw.get("volume", 0) or 0
        volume = float(volume_raw)
        if volume < 0 or not math.isfinite(volume):
            volume = 0.0
        time_ = pd.to_datetime(ts, utc=True, errors="raise")
    except (KeyError, ValueError, TypeError):
        return None
    if not _validate_ohlc_row(open_, high, low, close):
        return None
    return {
        "time": time_, "Open": open_, "High": high,
        "Low": low, "Close": close, "Volume": volume,
    }


def _build_candle_dataframe(
    candles: List[Mapping[str, Any]],
    pair: str,
    tf_key: str,
) -> Tuple[Optional[pd.DataFrame], int, int]:
    """Convert raw OANDA candles into a strict OHLC DataFrame."""
    rows: List[Dict[str, Any]] = []
    invalid = 0
    incomplete = 0
    for raw in candles:
        if not isinstance(raw, Mapping):
            invalid += 1
            continue
        if not raw.get("complete"):
            incomplete += 1
            continue
        parsed = _parse_candle(raw)
        if parsed is None:
            invalid += 1
            continue
        rows.append(parsed)

    if not rows:
        logger.warning(
            "No valid candles for %s %s (invalid=%d, incomplete=%d)",
            pair, tf_key, invalid, incomplete,
        )
        return None, 0, incomplete

    df = pd.DataFrame(rows).set_index("time").sort_index()
    if not df.index.is_monotonic_increasing or df.index.has_duplicates:
        df = df[~df.index.duplicated(keep="last")].sort_index()

    return df, len(df), incomplete


def _classify_v20_error(exc: V20Error) -> Tuple[ScanStatus, bool, str]:
    """Classify OANDA error. Returns (status, abort_scan, redacted_message)."""
    code = getattr(exc, "code", None)
    if code in (401, 403):
        return ScanStatus.AUTH_ABORT, True, f"auth_error(code={code})"
    if code == 429:
        return ScanStatus.NETWORK_ERROR, False, "rate_limited"
    if code is not None:
        try:
            code_int = int(code)
            if 500 <= code_int < 600:
                return ScanStatus.NETWORK_ERROR, False, f"server_error(code={code_int})"
        except (TypeError, ValueError):
            pass
    return ScanStatus.NETWORK_ERROR, False, f"v20_error(code={code})"


_BACKOFF_RNG = random.SystemRandom()


def _compute_backoff(attempt: int) -> float:
    """Exponential backoff with full jitter."""
    base = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
    return base * (0.5 + _BACKOFF_RNG.random() * 0.5)


def _build_hardened_session() -> requests.Session:
    """Build a requests.Session with retry/connection-pool tuning."""
    sess = requests.Session()
    retry = Retry(
        total=0, connect=0, read=0, redirect=0, status=0,
        backoff_factor=0, raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=MAX_WORKERS * 2,
        pool_maxsize=MAX_WORKERS * 2,
        max_retries=retry,
    )
    sess.mount("https://", adapter)
    sess.mount("http://", adapter)
    return sess


class _AuthConfigError(RuntimeError):
    """Raised when OANDA credentials are missing or malformed."""


@dataclass(frozen=True)
class _ClientLoad:
    client: Optional[API]
    error: Optional[str]


def _load_oanda_client_impl() -> _ClientLoad:
    """Build a fresh OANDA client. Returns a _ClientLoad (never raises)."""
    try:
        token = st.secrets["OANDA_ACCESS_TOKEN"]
        env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
    except (KeyError, FileNotFoundError) as exc:
        return _ClientLoad(None, f"OANDA credentials missing: {exc!s}")
    except Exception as exc:  # pylint: disable=broad-except
        return _ClientLoad(None, f"OANDA secrets access failed: {type(exc).__name__}")

    if not isinstance(token, str) or not token.strip():
        return _ClientLoad(None, "OANDA_ACCESS_TOKEN is empty")
    if env not in ("practice", "live"):
        return _ClientLoad(None, f"Invalid OANDA_ENVIRONMENT: {env!r}")

    try:
        client = API(access_token=token, environment=env, request_params={
            "timeout": REQUEST_TIMEOUT,
        })
        try:
            old_session = getattr(client, "client", None)
            if isinstance(old_session, requests.Session):
                try:
                    old_session.close()
                except Exception:  # pylint: disable=broad-except
                    pass
            client.client = _build_hardened_session()
            client.client.headers.update({
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "Accept-Datetime-Format": "RFC3339",
            })
        except (AttributeError, TypeError):
            logger.warning("Could not replace OANDA underlying session")
    except (V20Error, ValueError, TypeError) as exc:
        return _ClientLoad(None, f"OANDA client init failed: {type(exc).__name__}")

    return _ClientLoad(client, None)


def _get_secrets_hash() -> Tuple[str, str]:
    """Generate a SHA256 hash of OANDA secrets to enable client re-allocation

    upon credential rotation without container restart.
    """
    try:
        token = st.secrets.get("OANDA_ACCESS_TOKEN", "")
        env = st.secrets.get("OANDA_ENVIRONMENT", "practice")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return token_hash, env
    except Exception:
        return "", "practice"


@st.cache_resource(show_spinner=False)
def _cached_oanda_client_holder(token_hash: str, env: str) -> List[_ClientLoad]:
    """Return a mutable single-element holder so we can refresh in-place

    without invalidating Streamlit's cached resource unless credentials rotate.
    """
    return [_load_oanda_client_impl()]


def get_oanda_client() -> API:
    """Return a usable OANDA API client.

    If the cached entry holds an error, attempt one reload in case secrets
    have been updated. If still failing, raise _AuthConfigError.
    """
    token_hash, env = _get_secrets_hash()
    holder = _cached_oanda_client_holder(token_hash, env)
    load = holder[0]
    if load.client is None:
        refreshed = _load_oanda_client_impl()
        holder[0] = refreshed
        if refreshed.client is None:
            raise _AuthConfigError(refreshed.error or "OANDA client unavailable")
        return refreshed.client
    return load.client


# ---- Candle cache : snapshot-bucketed, deep-copy safe, O(log n) eviction ----

class _CandleCache:
    """Thread-safe LRU cache of RAW candles.

    Always returns deep copies; mutation cannot corrupt cached state.
    """

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str, int], Tuple[float, pd.DataFrame]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _bucket(snapshot_ts: float, tf_key: str) -> int:
        bar_seconds = TF_BAR_SECONDS[tf_key]
        return int(snapshot_ts // bar_seconds)

    def get(
        self, pair: str, tf_key: str, snapshot_ts: float,
    ) -> Optional[pd.DataFrame]:
        key = (pair, tf_key, self._bucket(snapshot_ts, tf_key))
        ttl = CACHE_TTL_BY_TF.get(tf_key, 300)
        now = time.time()
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            stored_at, cached_df = entry
            if now - stored_at > ttl:
                self._store.pop(key, None)
                return None
            return cached_df.copy(deep=True)

    def set(
        self, pair: str, tf_key: str, snapshot_ts: float, df: pd.DataFrame,
    ) -> None:
        key = (pair, tf_key, self._bucket(snapshot_ts, tf_key))
        with self._lock:
            if len(self._store) >= CACHE_MAX_ENTRIES:
                evict_n = max(1, CACHE_MAX_ENTRIES // 10)
                oldest = heapq.nsmallest(
                    evict_n, self._store.items(), key=lambda kv: kv[1][0],
                )
                for evict_key, _ in oldest:
                    self._store.pop(evict_key, None)
            self._store[key] = (time.time(), df.copy(deep=True))

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


_CANDLE_CACHE = _CandleCache()
_OANDA_SEMAPHORE = threading.BoundedSemaphore(SEMAPHORE_LIMIT)


@contextmanager
def _oanda_slot(stop_event: threading.Event) -> Iterator[bool]:
    """Acquire a bounded slot. Single-shot timed acquire."""
    if stop_event.is_set():
        yield False
        return
    acquired = _OANDA_SEMAPHORE.acquire(timeout=SEMAPHORE_ACQUIRE_TIMEOUT)
    try:
        if acquired and stop_event.is_set():
            try:
                _OANDA_SEMAPHORE.release()
            except ValueError:
                logger.exception("Semaphore release fault (defensive)")
            acquired = False
            yield False
            return
        yield acquired
    finally:
        if acquired:
            try:
                _OANDA_SEMAPHORE.release()
            except ValueError:
                logger.exception("Semaphore release double-fault (defensive)")


def _build_candles_request(
    pair: str, tf_key: str, snapshot_time: datetime,
) -> InstrumentsCandles:
    params = {
        "granularity": tf_key,
        "count": TF_CANDLE_COUNTS[tf_key],
        "price": "M",
        "to": snapshot_time.strftime("%Y-%m-%dT%H:%M:%S.000000000Z"),
    }
    return InstrumentsCandles(instrument=pair, params=params)


def _wait_or_cancel(stop_event: threading.Event, delay: float) -> bool:
    """Returns True if the wait was cancelled by stop_event."""
    return stop_event.wait(delay)


def _validate_fetched_dataframe(
    df: Optional[pd.DataFrame],
    pair: str,
    tf_key: str,
    snapshot_time: datetime,
    n_kept: int,
) -> FetchResult:
    """Validate post-parse DataFrame. Returns OK or DATA_INVALID."""
    if df is None or len(df) < RSI_PERIOD + 2:
        return FetchResult(
            status=ScanStatus.DATA_INVALID,
            message=f"insufficient_data(kept={n_kept})",
        )
    if df.index[-1] > snapshot_time + timedelta(seconds=1):
        logger.error(
            "Lookahead detected for %s %s: last=%s snapshot=%s",
            pair, tf_key, df.index[-1], snapshot_time,
        )
        return FetchResult(
            status=ScanStatus.DATA_INVALID, message="lookahead_violation",
        )
    return FetchResult(status=ScanStatus.OK, df=df, message="fetched")


def _try_fetch_once(
    pair: str,
    tf_key: str,
    snapshot_time: datetime,
    stop_event: threading.Event,
    client: API,
) -> FetchResult:
    """Single attempt: returns the FetchResult of this attempt (no retry)."""
    with _oanda_slot(stop_event) as acquired:
        if not acquired:
            return FetchResult(
                status=(ScanStatus.CANCELLED if stop_event.is_set()
                        else ScanStatus.NETWORK_ERROR),
                message="semaphore_unavailable",
            )
        try:
            req = _build_candles_request(pair, tf_key, snapshot_time)
            resp = client.request(req)
        except V20Error as exc:
            status, abort, msg = _classify_v20_error(exc)
            logger.warning("V20Error %s/%s: %s", pair, tf_key, msg)
            return FetchResult(status=status, message=msg, abort_scan=abort)
        except (requests.exceptions.RequestException,
                TimeoutError, ConnectionError, OSError) as exc:
            err_msg = f"network_error({type(exc).__name__})"
            logger.warning("Network error %s/%s: %s",
                           pair, tf_key, type(exc).__name__)
            return FetchResult(status=ScanStatus.NETWORK_ERROR, message=err_msg)
        except (KeyError, ValueError, TypeError) as exc:
            logger.exception("Data parsing error %s/%s", pair, tf_key)
            return FetchResult(
                status=ScanStatus.DATA_INVALID,
                message=f"parse_error({type(exc).__name__})",
            )

    candles = resp.get("candles", []) if isinstance(resp, Mapping) else []
    df, n_kept, _ = _build_candle_dataframe(candles, pair, tf_key)
    return _validate_fetched_dataframe(df, pair, tf_key, snapshot_time, n_kept)


def _fetch_with_retry(
    pair: str,
    tf_key: str,
    snapshot_time: datetime,
    stop_event: threading.Event,
    client: API,
) -> FetchResult:
    """Retry wrapper around `_try_fetch_once`."""
    last_result: FetchResult = FetchResult(
        status=ScanStatus.NETWORK_ERROR, message="not_attempted",
    )
    for attempt in range(MAX_RETRIES):
        if stop_event.is_set():
            return FetchResult(status=ScanStatus.CANCELLED, message="cancelled")

        attempt_result = _try_fetch_once(
            pair, tf_key, snapshot_time, stop_event, client,
        )

        if attempt_result.status == ScanStatus.OK and attempt_result.df is not None:
            return attempt_result
        if attempt_result.abort_scan:
            return attempt_result

        last_result = attempt_result

        if (attempt_result.status == ScanStatus.DATA_INVALID
                and attempt_result.message.startswith("parse_error")):
            return attempt_result

        if attempt < MAX_RETRIES - 1:
            if _wait_or_cancel(stop_event, _compute_backoff(attempt)):
                return FetchResult(status=ScanStatus.CANCELLED, message="cancelled")

    return last_result


def fetch_candles(
    pair: str,
    tf_key: str,
    snapshot_time: datetime,
    stop_event: threading.Event,
) -> FetchResult:
    """Fetch OANDA candles for a given pair/TF, anchored at snapshot_time."""
    snapshot_ts = snapshot_time.timestamp()

    cached = _CANDLE_CACHE.get(pair, tf_key, snapshot_ts)
    if cached is not None and not cached.empty:
        return FetchResult(status=ScanStatus.OK, df=cached, message="cache_hit")

    try:
        client = get_oanda_client()
    except _AuthConfigError as exc:
        return FetchResult(
            status=ScanStatus.AUTH_ABORT, message=str(exc), abort_scan=True,
        )

    result = _fetch_with_retry(pair, tf_key, snapshot_time, stop_event, client)
    if result.status == ScanStatus.OK and result.df is not None:
        _CANDLE_CACHE.set(pair, tf_key, snapshot_ts, result.df)
    return result


# =============================================================================
# 7. [APPLICATION] orchestration (no Streamlit calls)
# =============================================================================

def _empty_midline_block() -> Dict[str, Any]:
    """Bloc midline complet à feuilles nulles (jamais null au niveau nœud)."""
    return {
        "side": "UNKNOWN", "distance": None, "cross": "NONE",
        "bars_since_cross": None, "available": False,
    }


def _empty_divergence_payload() -> Dict[str, Any]:
    """Objet `divergence` COMPLET à feuilles nulles (jamais null au niveau nœud).

    Le consommateur aval (`merge.py:_extract_nested_rsi`) fait
    `v.get("divergence")` puis `isinstance(..., dict)` et retombe sur `{}`.
    Toute grammaire de décodage contraint exige en plus un schéma FERMÉ sans
    branche nulle au niveau des NŒUDS : d'où cette forme vide canonique.
    """
    return _neutralize_export(
        {
            "label": DivergenceLabel.AUCUNE.value,
            "code": "NONE",
            "kind": "REGULAR",
            "confirmed": False,
            "strength_score": 0.0,
            "confidence_score": 0.0,
            "quality_tier": "T4",
            "divergence_age_bars": None,
            "pivot_distance_bars": None,
            "match_distance_bars": None,
            "pivots": {
                "price_prev_idx": None, "price_last_idx": None,
                "rsi_prev_idx": None, "rsi_last_idx": None,
                "rsi_prev_value": None, "rsi_last_value": None,
                "price_prev_value": None, "price_last_value": None,
            },
        },
        False,
    )


def _empty_context_payload() -> Dict[str, Any]:
    """Objet `context` COMPLET à feuilles nulles (jamais null au niveau nœud)."""
    return {
        "adx": None,
        "regime_factor": 1.0,
        "volatility_regime": "unknown",
        "allow_regular": False,
        "lookback": 0,
        "confirm_bars": 0,
        "rsi_prominence": None,
        "available": False,
    }


def _empty_cell(status: str = ScanStatus.NO_DATA.value) -> TFCell:
    """Cellule vide mais CONTRACTUELLE : aucun nœud objet nul, statut explicite."""
    return TFCell(
        status=status,
        divergence_payload=_empty_divergence_payload(),
        divergence_context=_empty_context_payload(),
        midline=_empty_midline_block(),
    )


# [REFONTE — exigence A/F] Cell computation forwards `tf_key` so divergence
# detection uses the dynamic lookback / confirmation windows (exigences E, F)
# and records the full enriched payload + candidate list + market context for
# export (exigences A, D, F). No fetch/cache/threadpool logic is touched.
# ── [FIX-2 REVISED] Source unique de vérité pour l'actionnabilité. ──
def _is_actionable(sig: DivergenceSignal) -> bool:
    """Un signal peut-il légitimement piloter une décision aval ?

    [AUDIT-P06] Le plancher `_TIER_T3` est désormais testé ICI et non plus dans
    `_build_div_signal`. Le prédicat d'actionnabilité est donc exactement celui
    d'avant le correctif (les signaux sous le plancher n'existaient pas), ce qui
    garantit une absence de régression décisionnelle.
    """
    return bool(
        sig.confirmed
        and sig.label not in (DivergenceLabel.AUCUNE, DivergenceLabel.STALE)
        and np.isfinite(sig.strength_score)
        and sig.strength_score >= _TIER_T3
    )


def _neutralize_export(payload: Dict[str, Any], actionable: bool) -> Dict[str, Any]:
    """Tague le payload et neutralise les scores décisionnels si non actionnable.

    Les valeurs d'origine sont préservées sous `*_raw` : le chemin chaud est
    assaini (strength_score/confidence_score forcés à 0.0), la piste d'audit
    reste intacte. Ferme la fuite A6 : sans cette étape, `divergence_payload`
    (le dictionnaire imbriqué lu par le moteur V10) continuait de transporter
    le score brut même quand le champ plat correspondant était déjà zéroé.
    """
    out = dict(payload)
    raw_strength = float(payload.get("strength_score", 0.0) or 0.0)
    raw_conf = float(payload.get("confidence_score", 0.0) or 0.0)
    # [AUDIT-P01] Jeu de clés STRICTEMENT invariant : `actionable`,
    # `strength_score_raw` et `confidence_score_raw` sont TOUJOURS présents.
    # Le constraint decoding aval exige un schéma fermé sans branche
    # conditionnelle : c'est la condition d'un `additionalProperties: false`
    # directement exploitable comme grammaire.
    out["actionable"] = bool(actionable)
    out["strength_score_raw"] = round(raw_strength, 4)
    out["confidence_score_raw"] = round(raw_conf, 4)
    if actionable:
        return out
    # Chemin chaud inchangé : un signal non actionnable reste neutralisé.
    out["strength_score"] = 0.0
    out["confidence_score"] = 0.0
    return out


def _compute_cell(
    df: pd.DataFrame,
    pair: str,
    snapshot_time: datetime,
    tf_key: str,
) -> TFCell:
    """Compute one (asset, tf) cell from a DataFrame."""
    last_rsi, rsi_series = calculate_rsi(df["Close"])
    result = detect_divergence_full(df, rsi_series, pair, tf_key=tf_key)
    div = result.winner
    bar_age = (snapshot_time - df.index[-1].to_pydatetime()).total_seconds()

    is_active = _is_actionable(div)

    # [AUDIT-P05] États de premier rang (midline, zone) + traçabilité de la barre.
    # [AUDIT-P08] `div.kind` n'est PLUS masqué : c'est une donnée DESCRIPTIVE.
    # Le forcer à "REGULAR" quand le signal n'était pas actionnable produisait une
    # contradiction INTERNE au document : le payload imbriqué, lui, continuait
    # d'indiquer "HIDDEN".
    return TFCell(
        rsi=last_rsi,
        divergence=div.label.value,          # "Stale" reste visible : pas de perte
        data_available=bool(np.isfinite(last_rsi)),
        last_bar_age_seconds=bar_age,
        divergence_strength=(
            round(float(div.strength_score), 4) if is_active else 0.0
        ),
        divergence_kind=div.kind,
        divergence_payload=_neutralize_export(div.to_export(), is_active),
        divergence_candidates=tuple(
            _neutralize_export(c.to_export(), _is_actionable(c))
            for c in result.candidates
        ),
        divergence_context=result.context_export(),
        status=ScanStatus.OK.value,
        last_bar_time=df.index[-1].isoformat(),
        zone=_rsi_zone(last_rsi),
        midline=_midline_block(rsi_series),
        div_actionable=is_active,
    )


def _aggregate_status(statuses: Iterable[ScanStatus]) -> ScanStatus:
    statuses_list = list(statuses)
    if not statuses_list:
        return ScanStatus.ERROR
    if all(s == ScanStatus.OK for s in statuses_list):
        return ScanStatus.OK
    if all(s != ScanStatus.OK for s in statuses_list):
        return ScanStatus.ERROR
    return ScanStatus.PARTIAL


def process_single_asset(
    pair: str,
    stop_event: threading.Event,
    snapshot_time: datetime,
) -> AssetResult:
    """Process all timeframes for one asset."""
    result = AssetResult(pair=pair)
    statuses: List[ScanStatus] = []

    for tf_display_iter, tf_key_iter in TIMEFRAMES:
        if stop_event.is_set():
            result.timeframes[tf_display_iter] = _empty_cell(ScanStatus.CANCELLED.value)
            statuses.append(ScanStatus.CANCELLED)
            continue

        fetch = fetch_candles(pair, tf_key_iter, snapshot_time, stop_event)

        if fetch.abort_scan:
            result.abort_scan = True
            result.status = ScanStatus.AUTH_ABORT
            result.error_messages.append(fetch.message)
            for tf_d_fill, _ in TIMEFRAMES:
                result.timeframes.setdefault(
                    tf_d_fill, _empty_cell(ScanStatus.AUTH_ABORT.value),
                )
            return result

        if fetch.status != ScanStatus.OK or fetch.df is None:
            # [AUDIT-P05] Le statut PAR TIMEFRAME est conservé jusqu'au JSON :
            # « pas de donnee » (NO_DATA / NETWORK_ERROR / TIMEOUT / DATA_INVALID)
            # devient distinguable de « pas de signal » (OK + div=NONE).
            result.timeframes[tf_display_iter] = _empty_cell(fetch.status.value)
            statuses.append(fetch.status)
            continue

        try:
            result.timeframes[tf_display_iter] = _compute_cell(
                fetch.df, pair, snapshot_time, tf_key_iter,
            )
            statuses.append(ScanStatus.OK)
        except (ValueError, TypeError, KeyError, IndexError) as exc:
            logger.exception("compute_cell failed %s/%s", pair, tf_key_iter)
            result.timeframes[tf_display_iter] = _empty_cell(
                ScanStatus.INTERNAL_BUG.value,
            )
            statuses.append(ScanStatus.INTERNAL_BUG)
            result.error_messages.append(f"compute_error({type(exc).__name__})")

    result.status = _aggregate_status(statuses)
    return result


def _asset_result_to_row(result: AssetResult) -> Dict[str, Any]:
    output_row: Dict[str, Any] = {
        "Devises": result.pair, "Status": result.status.value,
    }
    for tf_disp_col in TIMEFRAMES_DISPLAY:
        tf_cell = result.timeframes.get(tf_disp_col, _empty_cell(result.status.value))
        output_row[tf_disp_col] = tf_cell.to_dict()
    return output_row


def _timeout_row(asset: str) -> Dict[str, Any]:
    """Ligne d'actif en timeout : STRUCTURELLEMENT identique à une ligne valide.

    [AUDIT-P02] Auparavant construite à la main avec `divergence_payload: None`
    et `divergence_context: None` (nœuds objets nuls). Elle réutilise désormais
    le domaine (`_empty_cell`) : une seule source de vérité de forme, donc un
    jeu de clés rigoureusement identique à celui d'une cellule valide.
    """
    output_row: Dict[str, Any] = {
        "Devises": asset, "Status": ScanStatus.TIMEOUT.value,
    }
    for tf_disp_col in TIMEFRAMES_DISPLAY:
        output_row[tf_disp_col] = _empty_cell(ScanStatus.TIMEOUT.value).to_dict()
    return output_row


# ---- Shared executor pool with health-check & lazy recreation ----

class _ExecutorHolder:
    __slots__ = ("_lock", "_executor")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._executor: Optional[ThreadPoolExecutor] = None

    def get(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None or getattr(self._executor, "_shutdown", False):
                self._executor = ThreadPoolExecutor(
                    max_workers=MAX_WORKERS, thread_name_prefix="rsi-pool",
                )
            return self._executor


@st.cache_resource(show_spinner=False)
def _get_executor_holder() -> _ExecutorHolder:
    return _ExecutorHolder()


def _get_scan_executor() -> ThreadPoolExecutor:
    return _get_executor_holder().get()


# ---- Scan orchestration ----

class _ScanState:
    __slots__ = ("_state_lock", "_running", "_last_scan_at")

    def __init__(self) -> None:
        self._state_lock = threading.Lock()
        self._running: bool = False
        self._last_scan_at: float = 0.0

    def cooldown_remaining(self) -> float:
        with self._state_lock:
            last = self._last_scan_at
        if last <= 0:
            return 0.0
        elapsed = time.time() - last
        return max(0.0, SERVER_COOLDOWN - elapsed)

    def set_last_scan_time(self, ts: float) -> None:
        with self._state_lock:
            self._last_scan_at = ts

    @contextmanager
    def try_acquire_scan(self) -> Iterator[Tuple[bool, float]]:
        owned = False
        with self._state_lock:
            if self._running:
                yield False, 0.0
                return
            last = self._last_scan_at
            if last > 0:
                elapsed = time.time() - last
                remaining = SERVER_COOLDOWN - elapsed
                if remaining > 0:
                    yield False, remaining
                    return
            self._running = True
            owned = True
        try:
            yield True, 0.0
        finally:
            if owned:
                with self._state_lock:
                    self._running = False


_SCAN_STATE = _ScanState()


def _process_one_future(
    future: Future,
    asset: str,
    deadline: float,
    stop_event: threading.Event,
    scan_log: logging.LoggerAdapter,
) -> Tuple[Dict[str, Any], int, bool]:
    remaining = max(0.1, deadline - time.monotonic())
    try:
        asset_result = future.result(timeout=remaining)
    except FuturesTimeoutError:
        return _timeout_row(asset), 0, False
    except (ValueError, TypeError, KeyError, RuntimeError):
        scan_log.exception("Future extraction failed for %s", asset)
        timeout_row = _timeout_row(asset)
        timeout_row["Status"] = ScanStatus.INTERNAL_BUG.value
        return timeout_row, 0, False

    auth_abort = False
    if asset_result.abort_scan:
        auth_abort = True
        stop_event.set()
        scan_log.error("Auth abort triggered by %s", asset)

    output_row = _asset_result_to_row(asset_result)
    cells_ok = sum(
        1 for c in asset_result.timeframes.values() if c.data_available
    )
    return output_row, cells_ok, auth_abort


def _drain_futures(
    futures: Dict[Future, str],
    deadline: float,
    stop_event: threading.Event,
    scan_log: logging.LoggerAdapter,
    progress_emitter: Optional[Callable[[float, str], None]],
) -> Tuple[Dict[str, Dict[str, Any]], int, bool]:
    results_map: Dict[str, Dict[str, Any]] = {}
    successful_cells = 0
    auth_abort = False
    total_jobs = len(futures)
    done_count = 0

    try:
        ac_timeout = max(0.5, deadline - time.monotonic())
        for future in as_completed(futures, timeout=ac_timeout):
            asset = futures[future]
            output_row, cells_ok, abort = _process_one_future(
                future, asset, deadline, stop_event, scan_log,
            )
            results_map[asset] = output_row
            successful_cells += cells_ok
            if abort and not auth_abort:
                auth_abort = True

            done_count += 1
            if progress_emitter is not None and total_jobs > 0:
                try:
                    progress_emitter(done_count / total_jobs, asset)
                except (ValueError, RuntimeError, TypeError):
                    pass
    except FuturesTimeoutError:
        scan_log.warning("Global scan timeout reached")
        stop_event.set()

    return results_map, successful_cells, auth_abort


def _execute_scan(
    scan_log: logging.LoggerAdapter,
    snapshot_time: datetime,
    progress_emitter: Optional[Callable[[float, str], None]],
) -> Tuple[Dict[str, Dict[str, Any]], int, bool]:
    stop_event = threading.Event()
    executor = _get_scan_executor()

    results_map: Dict[str, Dict[str, Any]] = {}
    successful_cells = 0
    auth_abort = False

    submitted: Dict[Future, str] = {}
    try:
        for asset in ASSETS:
            try:
                fut = executor.submit(
                    process_single_asset, asset, stop_event, snapshot_time,
                )
                submitted[fut] = asset
            except RuntimeError:
                scan_log.exception("Submit failed for %s", asset)

        deadline = time.monotonic() + SCAN_GLOBAL_TIMEOUT

        results_map, successful_cells, auth_abort = _drain_futures(
            submitted, deadline, stop_event, scan_log, progress_emitter,
        )

        for asset in ASSETS:
            results_map.setdefault(asset, _timeout_row(asset))
    except (RuntimeError, OSError):
        scan_log.exception("Orchestration failure")
        for asset in ASSETS:
            results_map.setdefault(asset, _timeout_row(asset))
    finally:
        stop_event.set()
        for fut in submitted:
            if not fut.done():
                fut.cancel()

    return results_map, successful_cells, auth_abort


def run_scan(
    progress_emitter: Optional[Callable[[float, str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Run a full scan. Returns:

        None              -> another scan is already running.
        {"cooldown": s}   -> cooldown active; scan refused.
        full result dict  -> scan executed.
    """
    with _SCAN_STATE.try_acquire_scan() as (acquired, cooldown_remaining):
        if not acquired:
            if cooldown_remaining > 0:
                return {"cooldown": cooldown_remaining, "results": None}
            return None

        scan_id = uuid.uuid4().hex[:12]
        scan_log = logging.LoggerAdapter(logger, {"scan_id": scan_id})
        scan_log.info("Scan started")

        snapshot_time = datetime.now(timezone.utc).replace(microsecond=0)
        expected_cells = len(ASSETS) * len(TIMEFRAMES_DISPLAY)

        results_map, successful_cells, auth_abort = _execute_scan(
            scan_log, snapshot_time, progress_emitter,
        )

        completion_ratio = (
            successful_cells / expected_cells if expected_cells else 0.0
        )
        if completion_ratio >= SCAN_SUCCESS_THRESHOLD and not auth_abort:
            _SCAN_STATE.set_last_scan_time(time.time())

        scan_log.info(
            "Scan ended: cells=%d/%d, completion=%.1f%%, auth_abort=%s",
            successful_cells, expected_cells,
            completion_ratio * 100, auth_abort,
        )

        ordered_results = [results_map[a] for a in ASSETS]
        return {
            "scan_id": scan_id,
            "results": ordered_results,
            "scan_ts": snapshot_time,
            "successful_cells": successful_cells,
            "expected_cells": expected_cells,
            "auth_abort": auth_abort,
        }


# =============================================================================
# 8. [EXPORTS] statistics & exports — pure data transforms
# =============================================================================

def safe_get(obj: Any, key: str, default: Any = None) -> Any:
    """Tolerant getter for Mapping-like objects. Never raises."""
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return default


def safe_float(value: Any) -> float:
    """Convert anything to float, returning NaN on failure or non-finite."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float("nan")
    if not math.isfinite(result):
        return float("nan")
    return result


def safe_int(value: Any, default: int = 0) -> int:
    """Convert anything to int, returning `default` on failure."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _tf_stats(cells: List[Mapping[str, Any]]) -> Dict[str, int]:
    counts = {
        "extreme_oversold": 0, "oversold": 0,
        "extreme_overbought": 0, "overbought": 0,
        "bull_div": 0, "bear_div": 0, "valid_count": 0,
        # [AUDIT-P10] Ventilation manquante. Les buckets de ZONE sont
        # mutuellement exclusifs (partition) ; les compteurs de DIVERGENCE sont
        # exhaustifs (STALE inclus) et se reconcilient donc avec `instruments`.
        "stale_div": 0, "bull_div_regular": 0, "bull_div_hidden": 0,
        "bear_div_regular": 0, "bear_div_hidden": 0,
        "tier_t1": 0, "tier_t2": 0, "tier_t3": 0, "tier_t4": 0,
        "actionable_count": 0, "non_actionable_signal": 0,
        "above_midline": 0, "below_midline": 0, "at_midline": 0,
        "unknown_zone": 0, "failed_count": 0,
    }
    for tf_cell in cells:
        rsi_val = safe_float(safe_get(tf_cell, "rsi"))
        status_val = str(safe_get(tf_cell, "status", ScanStatus.OK.value) or "")
        if status_val and status_val != ScanStatus.OK.value:
            counts["failed_count"] += 1
        if np.isfinite(rsi_val):
            counts["valid_count"] += 1
            if rsi_val <= RSI_EXTREME_LOW:
                counts["extreme_oversold"] += 1
            elif rsi_val <= RSI_OVERSOLD:
                counts["oversold"] += 1
            elif rsi_val >= RSI_EXTREME_HIGH:
                counts["extreme_overbought"] += 1
            elif rsi_val >= RSI_OVERBOUGHT:
                counts["overbought"] += 1
        else:
            counts["unknown_zone"] += 1

        payload = safe_get(tf_cell, "divergence_payload")
        payload = payload if isinstance(payload, Mapping) else {}
        code = str(safe_get(payload, "code", "NONE") or "NONE")
        kind = str(safe_get(payload, "kind", "REGULAR") or "REGULAR")
        tier = str(safe_get(payload, "quality_tier", "") or "").lower()
        if code == "STALE":
            counts["stale_div"] += 1
        elif code == "BULL":
            counts["bull_div"] += 1
            counts["bull_div_regular" if kind == "REGULAR" else "bull_div_hidden"] += 1
        elif code == "BEAR":
            counts["bear_div"] += 1
            counts["bear_div_regular" if kind == "REGULAR" else "bear_div_hidden"] += 1
        if tier in ("t1", "t2", "t3", "t4"):
            counts["tier_" + tier] += 1
        if code in ("BULL", "BEAR", "STALE"):
            if bool(safe_get(payload, "actionable", False)):
                counts["actionable_count"] += 1
            else:
                counts["non_actionable_signal"] += 1

        mid = safe_get(tf_cell, "midline")
        side = (str(safe_get(mid, "side", "UNKNOWN") or "UNKNOWN")
                if isinstance(mid, Mapping) else "UNKNOWN")
        if side == "ABOVE":
            counts["above_midline"] += 1
        elif side == "BELOW":
            counts["below_midline"] += 1
        elif side == "AT":
            counts["at_midline"] += 1
    return counts


def _market_bias(
    avg_rsi_value: float,
    total_bull: int,
    total_bear: int,
    valid_count: int,
) -> Tuple[str, str]:
    if valid_count == 0 or not np.isfinite(avg_rsi_value):
        # [AUDIT-P11] Un seul vocabulaire pour le contrat machine : la valeur
        # francaise "INSUFFISANT" cohabitait avec BULLISH/BEARISH/NEUTRAL.
        return "INSUFFICIENT", "#888"
    if total_bull > total_bear * 1.5 and avg_rsi_value < RSI_NEUTRAL_THRESHOLD:
        return "BULLISH", "#00C853"
    if total_bear > total_bull * 1.5 and avg_rsi_value > RSI_NEUTRAL_THRESHOLD:
        return "BEARISH", "#D32F2F"
    return "NEUTRAL", "#FFA500"


def compute_statistics(results: Any) -> Dict[str, Any]:
    """Build summary statistics. Tolerant to malformed input."""
    if not isinstance(results, list):
        results = []

    stats_by_tf: Dict[str, Dict[str, int]] = {}
    global_vals: List[float] = []
    total_cells = 0

    for tf_disp_col in TIMEFRAMES_DISPLAY:
        cells = [safe_get(data_row, tf_disp_col, {}) for data_row in results]
        stats_by_tf[tf_disp_col] = _tf_stats(cells)
        for tf_cell in cells:
            rsi_val = safe_float(safe_get(tf_cell, "rsi"))
            if np.isfinite(rsi_val):
                global_vals.append(rsi_val)
            total_cells += 1

    avg = float(np.mean(global_vals)) if global_vals else float("nan")
    total_bull = sum(s["bull_div"] for s in stats_by_tf.values())
    total_bear = sum(s["bear_div"] for s in stats_by_tf.values())
    extreme_count = sum(
        s["extreme_oversold"] + s["extreme_overbought"]
        for s in stats_by_tf.values()
    )

    bias, color = _market_bias(avg, total_bull, total_bear, len(global_vals))

    return {
        "by_tf": stats_by_tf,
        "avg_rsi": avg,
        "total_bull_div": total_bull,
        "total_bear_div": total_bear,
        "extreme_count": extreme_count,
        "market_bias": bias,
        "bias_color": color,
        "total_cells": total_cells,
        "valid_cells": len(global_vals),
    }


def create_csv_export(results: Any) -> bytes:
    """Build CSV. Tolerant to missing fields."""
    if not isinstance(results, list):
        results = []
    try:
        records: List[Dict[str, Any]] = []
        for data_row in results:
            record: Dict[str, Any] = {
                "Devises": safe_get(data_row, "Devises", ""),
                "Status": safe_get(data_row, "Status", "OK"),
            }
            for tf_disp_col in TIMEFRAMES_DISPLAY:
                tf_cell = safe_get(data_row, tf_disp_col, {})
                rsi_val = safe_float(safe_get(tf_cell, "rsi"))
                record[f"RSI_{tf_disp_col}"] = (
                    round(rsi_val, 2) if np.isfinite(rsi_val) else None
                )
                record[f"DIV_{tf_disp_col}"] = safe_get(
                    tf_cell, "divergence", DivergenceLabel.AUCUNE.value,
                )
                record[f"STRENGTH_{tf_disp_col}"] = safe_float(
                    safe_get(tf_cell, "divergence_strength", 0.0)
                )
                record[f"KIND_{tf_disp_col}"] = safe_get(
                    tf_cell, "divergence_kind", "REGULAR",
                )
            records.append(record)
        return pd.DataFrame(records).to_csv(index=False).encode("utf-8-sig")
    except Exception:
        logger.exception("CSV export failed")
        return b""


def _json_build_meta(
    scan_ts: datetime,
    *,
    scan_id: str = "",
    completeness: Optional[Mapping[str, Any]] = None,
    export_status: str = "OK",
    error: str = "",
) -> Dict[str, Any]:
    """Métadonnées d'intégrité, de traçabilité et de sémantique.

    [AUDIT-P04] Trois lacunes comblées, toutes additivement :
      * `scan_id` : corrélation entre le fichier livré et les logs serveur ;
      * `universe` + `completeness` : le consommateur peut VÉRIFIER qu'il a reçu
        l'univers complet (33 actifs x 5 timeframes = 165 cellules) au lieu de
        l'inférer ;
      * `semantics` : lève les ambiguïtés de lecture relevées à l'audit (buckets
        de zone mutuellement exclusifs, `market_bias` contrariant, `label`
        purement présentationnel).
    """
    # `completeness is None` = métrique INCONNUE (appel direct hors flux de
    # scan). On le dit explicitement plutôt que d'émettre des zéros qui
    # impliqueraient faussement un échec total du scan.
    comp_known = completeness is not None
    comp = dict(completeness or {})
    expected_cells = int(comp.get("cells_expected", len(ASSETS) * len(TIMEFRAMES_DISPLAY)))
    cells_ok = int(comp.get("cells_ok", 0))
    ratio = (cells_ok / expected_cells) if expected_cells else 0.0
    return {
        "generated_at": scan_ts.isoformat(),
        "schema_version": "3.0.0",  # bump: clés invariantes, nœuds non nuls, zone/midline
        "scan_id": scan_id,
        "export_status": export_status,
        "error": error,
        "rsi_period": RSI_PERIOD,
        "atr_period": ATR_PERIOD,
        "universe": {
            "instruments": list(ASSETS),
            "timeframes": list(TIMEFRAMES_DISPLAY),
            "timeframes_keys": [k for _d, k in TIMEFRAMES],
            "expected_instruments": len(ASSETS),
            "expected_cells": expected_cells,
        },
        "completeness": {
            "available": comp_known,
            "instruments_expected": len(ASSETS),
            "instruments_received": (
                int(comp.get("instruments_received", 0)) if comp_known else None
            ),
            "cells_expected": expected_cells,
            "cells_ok": cells_ok if comp_known else None,
            "cells_failed": (
                int(comp.get("cells_failed", max(0, expected_cells - cells_ok)))
                if comp_known else None
            ),
            "completion_ratio": round(ratio, 4) if comp_known else None,
            "scan_success_threshold": SCAN_SUCCESS_THRESHOLD,
            "universe_complete": (
                bool(int(comp.get("instruments_received", 0)) == len(ASSETS))
                if comp_known else None
            ),
        },
        "thresholds": {
            "oversold": RSI_OVERSOLD, "overbought": RSI_OVERBOUGHT,
            "extreme_low": RSI_EXTREME_LOW, "extreme_high": RSI_EXTREME_HIGH,
            "bull_div_zone": RSI_BULL_DIV_ZONE, "bear_div_zone": RSI_BEAR_DIV_ZONE,
            "min_rsi_delta": MIN_RSI_DELTA, "neutral": RSI_NEUTRAL_THRESHOLD,
        },
        # exigence D — expose the scoring contract so fusion can interpret it
        "scoring": {
            "strength_weights": {"price_atr": 0.4, "rsi_delta": 0.4, "volume": 0.2},
            "strength_floor": _TIER_T3,
            "quality_tiers": {"T1": _TIER_T1, "T2": _TIER_T2, "T3": _TIER_T3},
            "pivot_match_tolerance": PIVOT_MATCH_TOLERANCE,
            "adx_regular_floor": 20.0,
            "expected_rsi_amplitude": round(_EXPECTED_RSI_AMPLITUDE, 2),
            "multi_pivot_scan_enabled": ENABLE_MULTI_PIVOT_SCAN,
        },
        "enums": {
            "cell_status": sorted(s.value for s in ScanStatus),
            "zone": ["EXTREME_OVERSOLD", "OVERSOLD", "NEUTRAL_BEAR", "NEUTRAL",
                     "NEUTRAL_BULL", "OVERBOUGHT", "EXTREME_OVERBOUGHT", "UNKNOWN"],
            "div": sorted(set(_DIV_TO_JSON.values())),
            "div_kind": ["HIDDEN", "REGULAR"],
            "quality_tier": ["T1", "T2", "T3", "T4"],
            "midline_side": ["ABOVE", "AT", "BELOW", "UNKNOWN"],
            "midline_cross": ["CROSS_DOWN", "CROSS_UP", "NONE"],
            "market_bias": ["BEARISH", "BULLISH", "INSUFFICIENT", "NEUTRAL"],
            "divergence_label_ui_only": [lbl.value for lbl in DivergenceLabel],
            "volatility_regime": ["expansive", "normal", "quiet", "unknown"],
        },
        "semantics": {
            "zone_buckets": "mutuellement exclusifs et exhaustifs (partition)",
            "market_bias": (
                "CONTRARIANT : BULLISH quand les divergences haussieres dominent "
                "ET que la moyenne RSI est SOUS la midline 50 ; BEARISH quand les "
                "divergences baissieres dominent ET que la moyenne RSI est "
                "AU-DESSUS de 50 ; INSUFFICIENT si aucune cellule valide. La "
                "moyenne porte sur toutes les cellules valides."
            ),
            "label_field": (
                "`divergence.label` est PRESENTATIONNEL (francais, UI/PDF). Le "
                "contrat machine est `divergence.code` et le champ plat `div`. "
                "`summary.market_bias` est machine et en anglais."
            ),
            "candidates": (
                "`candidates[0]` DUPLIQUE `divergence` (le gagnant). Ne pas "
                "compter deux fois : utiliser `divergence` pour le signal "
                "principal et `candidates[1:]` pour les alternatives."
            ),
            "actionable": (
                "Un signal non actionnable n'est JAMAIS supprime : il reste "
                "present avec `actionable: false`, sa force brute preservee dans "
                "`strength_score_raw` et `strength_score` neutralise a 0.0."
            ),
            "data_vs_signal": (
                "`cell_status` distingue l'absence de DONNEE (NO_DATA, "
                "NETWORK_ERROR, TIMEOUT, DATA_INVALID, AUTH_ABORT, CANCELLED) de "
                "l'absence de SIGNAL (cell_status=OK et div=NONE)."
            ),
            "midline": (
                "`midline.distance` = RSI - 50 (signe = cote). "
                "`midline.cross` porte sur la DERNIERE barre close ; "
                "`bars_since_cross` est le nombre de barres depuis le dernier "
                "changement de cote."
            ),
        },
    }


def _json_build_summary(statistics: Mapping[str, Any]) -> Dict[str, Any]:
    by_tf_input = safe_get(statistics, "by_tf", {})
    if not isinstance(by_tf_input, Mapping):
        by_tf_input = {}
    by_tf_out: Dict[str, Any] = {}
    for tf_disp_col in TIMEFRAMES_DISPLAY:
        tf_stats_in = by_tf_input.get(tf_disp_col, {})
        def _i(key: str) -> int:
            return safe_int(safe_get(tf_stats_in, key, 0))

        by_tf_out[TIMEFRAMES_TO_KEY[tf_disp_col]] = {
            "extreme_oversold": _i("extreme_oversold"),
            "oversold": _i("oversold"),
            "extreme_overbought": _i("extreme_overbought"),
            "overbought": _i("overbought"),
            "bull_div": _i("bull_div"),
            "bear_div": _i("bear_div"),
            "valid_count": _i("valid_count"),
            # -- ventilation additive (reconciliation exacte) --
            "stale_div": _i("stale_div"),
            "bull_div_regular": _i("bull_div_regular"),
            "bull_div_hidden": _i("bull_div_hidden"),
            "bear_div_regular": _i("bear_div_regular"),
            "bear_div_hidden": _i("bear_div_hidden"),
            "tier_t1": _i("tier_t1"), "tier_t2": _i("tier_t2"),
            "tier_t3": _i("tier_t3"), "tier_t4": _i("tier_t4"),
            "actionable_count": _i("actionable_count"),
            "non_actionable_signal": _i("non_actionable_signal"),
            "above_midline": _i("above_midline"),
            "below_midline": _i("below_midline"),
            "at_midline": _i("at_midline"),
            "unknown_zone": _i("unknown_zone"),
            "failed_count": _i("failed_count"),
        }
    bias_raw = str(safe_get(statistics, "market_bias", "NEUTRAL"))
    bias_key = next(
        (k for k in ("BEARISH", "BULLISH", "INSUFFICIENT") if k in bias_raw),
        "NEUTRAL",
    )
    # Retro-compatibilite de LECTURE : une ancienne valeur "INSUFFISANT" est
    # normalisee vers le vocabulaire anglais unique.
    if bias_key == "NEUTRAL" and "INSUFFISANT" in bias_raw:
        bias_key = "INSUFFICIENT"
    avg_rsi_out = safe_float(safe_get(statistics, "avg_rsi"))
    return {
        "market_bias": bias_key,
        "avg_rsi": round(avg_rsi_out, 2) if np.isfinite(avg_rsi_out) else None,
        "total_bull_div": safe_int(safe_get(statistics, "total_bull_div", 0)),
        "total_bear_div": safe_int(safe_get(statistics, "total_bear_div", 0)),
        "extreme_count": safe_int(safe_get(statistics, "extreme_count", 0)),
        # [AUDIT-P10] Compteurs globaux permettant la reconciliation EXACTE avec
        # `instruments` : le consommateur peut verifier, pas supposer.
        "total_cells": safe_int(safe_get(statistics, "total_cells", 0)),
        "valid_cells": safe_int(safe_get(statistics, "valid_cells", 0)),
        "total_stale_div": sum(
            safe_int(safe_get(v, "stale_div", 0)) for v in by_tf_out.values()
        ),
        "by_tf": by_tf_out,
    }


# [REFONTE — exigence A/D/F] Instrument block exposes the full divergence
# intelligence. CRITICAL: pre-existing keys (`rsi`, `div`, `strength_score`,
# `div_kind`) are PRESERVED verbatim so the downstream merge engine (which reads
# `rsi.value`, `rsi.div`, `rsi.strength_score`, `rsi.div_kind`) keeps working.
# Everything else is ADDITIVE under new keys (`divergence`, `candidates`,
# `context`).
def _json_build_instruments(results: List[Any]) -> List[Dict[str, Any]]:
    instruments: List[Dict[str, Any]] = []
    for data_row in results:
        tf_data: Dict[str, Any] = {}
        for tf_disp_col, tf_key_col in TIMEFRAMES:
            tf_cell = safe_get(data_row, tf_disp_col, {})
            rsi_val = safe_float(safe_get(tf_cell, "rsi"))
            div_val = safe_get(tf_cell, "divergence", DivergenceLabel.AUCUNE.value)
            strength_val = safe_float(safe_get(tf_cell, "divergence_strength", 0.0))
            payload = safe_get(tf_cell, "divergence_payload")
            candidates = safe_get(tf_cell, "divergence_candidates", []) or []
            context = safe_get(tf_cell, "divergence_context")
            # [AUDIT-P02/P05] Aucun nœud objet nul : `divergence` et `context`
            # retombent sur des objets COMPLETS (feuilles nulles) au lieu de
            # `null`, et les clés additives sont INCONDITIONNELLES.
            _midline = safe_get(tf_cell, "midline")
            tf_data[tf_key_col] = {
                # ── preserved (back-compat with fusion/merge engine) ──
                "rsi": round(rsi_val, 2) if np.isfinite(rsi_val) else None,
                "div": _DIV_TO_JSON.get(div_val, "NONE"),
                "strength_score": (
                    round(strength_val, 4) if np.isfinite(strength_val) else 0.0
                ),
                "div_kind": str(safe_get(tf_cell, "divergence_kind", "REGULAR")),
                # ── additive enrichment (exigence A/D/F) ──
                "divergence": payload if isinstance(payload, Mapping)
                              else _empty_divergence_payload(),
                "candidates": list(candidates),
                "context": context if isinstance(context, Mapping)
                           else _empty_context_payload(),
                # ── [AUDIT-P05] signaux de premier rang par cellule ──
                "cell_status": str(safe_get(tf_cell, "status", ScanStatus.OK.value)),
                "rsi_available": bool(safe_get(tf_cell, "data_available", False)),
                "zone": str(safe_get(tf_cell, "zone", "UNKNOWN")),
                "midline": (_midline if isinstance(_midline, Mapping)
                            else _empty_midline_block()),
                "last_bar_time": safe_get(tf_cell, "last_bar_time"),
                "last_bar_age_seconds": safe_get(tf_cell, "last_bar_age_seconds"),
                "div_actionable": bool(safe_get(tf_cell, "div_actionable", False)),
            }
        instruments.append({
            "pair": safe_get(data_row, "Devises", ""),
            "status": safe_get(data_row, "Status", "OK"),
            "timeframes": tf_data,
        })
    return instruments


def _error_export(
    scan_ts: datetime, scan_id: str, reason: str,
) -> bytes:
    """Document d'erreur VALIDE, identifiable et conforme au schéma.

    [AUDIT-P03] Un fichier de zéro octet ingéré silencieusement par le pipeline
    est le pire mode de défaillance possible : il est indistinguable d'une
    absence de fichier et ne porte aucune cause. Ici le consommateur reçoit un
    document parseable avec `meta.export_status == "ERROR"` et une raison.
    """
    try:
        payload = {
            "meta": _json_build_meta(
                scan_ts, scan_id=scan_id,
                completeness={"cells_expected": 0, "cells_ok": 0,
                              "cells_failed": 0, "completion_ratio": 0.0,
                              "instruments_expected": 0, "instruments_received": 0},
                export_status="ERROR", error=reason,
            ),
            "summary": _json_build_summary({}),
            "instruments": [],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2,
                          allow_nan=False).encode("utf-8")
    except Exception:  # pylint: disable=broad-except
        # Le constructeur de méta ou de résumé est LUI-MÊME en cause : dernier
        # recours = document minimal littéral, sans AUCUN helper défaillant.
        logger.exception("error-export failed")
        return (
            b'{"meta": {"generated_at": "", "schema_version": "3.0.0",'
            b' "export_status": "ERROR", "error": "export_failed"},'
            b' "summary": {}, "instruments": []}'
        )


def create_json_export(
    results: Any,
    statistics: Any,
    scan_ts: datetime,
    scan_id: str = "",
    completeness: Optional[Mapping[str, Any]] = None,
) -> bytes:
    """Build JSON export. Schema-stable, tolerant to malformed input.

    [AUDIT-P03/P04] Deux garanties nouvelles, toutes deux additives :
      * `allow_nan=False` : un document contenant `NaN`/`Infinity` n'est PAS du
        JSON (RFC 8259) et casse la plupart des parseurs non-Python. La
        pré-sanitisation existante ne suffisait pas : elle ne couvrait que les
        champs connus, sans barrière au moment de la sérialisation.
      * en cas d'échec, on n'émet JAMAIS `b""` : un document d'erreur valide est
        produit (voir `_error_export`).
    """
    if not isinstance(results, list):
        results = []
    if not isinstance(statistics, Mapping):
        statistics = {}
    try:
        payload = {
            "meta": _json_build_meta(scan_ts, scan_id=scan_id,
                                     completeness=completeness),
            "summary": _json_build_summary(statistics),
            "instruments": _json_build_instruments(results),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2,
                          allow_nan=False).encode("utf-8")
    except Exception:
        logger.exception("JSON export failed")
        return _error_export(scan_ts, scan_id, "export_failed")


# ---- PDF ----

_PDF_TRANSLIT_PAIRS: Tuple[Tuple[str, str], ...] = (
    ("\u00e9", "e"), ("\u00e8", "e"), ("\u00ea", "e"), ("\u00eb", "e"),
    ("\u00e0", "a"), ("\u00e2", "a"), ("\u00e4", "a"),
    ("\u00ee", "i"), ("\u00ef", "i"),
    ("\u00f4", "o"), ("\u00f6", "o"),
    ("\u00f9", "u"), ("\u00fb", "u"), ("\u00fc", "u"),
    ("\u00e7", "c"),
    ("\u00c9", "E"), ("\u00c8", "E"), ("\u00ca", "E"),
    ("\u00c0", "A"),
    ("\u2191", "^"), ("\u2193", "v"),
    ("\u2014", "-"), ("\u2013", "-"),
    ("\u2022", "*"),
)
_PDF_TRANSLIT = {ord(k): v for k, v in _PDF_TRANSLIT_PAIRS}


def _pdf_str(value: Any, max_len: Optional[int] = None) -> str:
    """Convert to a Latin-1 safe string for fpdf. Never raises. Truncates."""
    try:
        s = str(value).translate(_PDF_TRANSLIT).encode(
            "latin-1", "replace",
        ).decode("latin-1")
    except (UnicodeError, TypeError, ValueError):
        return "?"
    if max_len is not None and len(s) > max_len:
        return s[: max(1, max_len - 1)] + "."
    return s


class _ReportPDF(FPDF):  # pylint: disable=too-few-public-methods
    def __init__(self, scan_ts: str, orientation: str = "L",
                 unit: str = "mm", page_format: str = "A4") -> None:
        super().__init__(orientation=orientation, unit=unit, format=page_format)
        self.scan_ts = scan_ts

    def header(self) -> None:
        self.set_fill_color(15, 23, 42)
        self.rect(0, 0, self.w, 18, "F")
        self.set_text_color(255, 255, 255)
        self.set_font("Helvetica", "B", 14)
        self.cell(
            0, 12, _pdf_str("RSI & Divergence Screener Pro"), 0, 1, "C",
        )
        self.set_text_color(0, 0, 0)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font("Helvetica", "I", 8)
        self.set_text_color(120, 120, 120)
        self.cell(
            0, 10,
            _pdf_str(f"Scan: {self.scan_ts} | Page {self.page_no()}"),
            0, 0, "C",
        )


def _pdf_render_summary(
    pdf: _ReportPDF, statistics: Mapping[str, Any],
) -> None:
    avg = safe_float(safe_get(statistics, "avg_rsi"))
    bias = str(safe_get(statistics, "market_bias", "NEUTRAL"))
    bull = safe_int(safe_get(statistics, "total_bull_div", 0))
    bear = safe_int(safe_get(statistics, "total_bear_div", 0))
    extreme = safe_int(safe_get(statistics, "extreme_count", 0))

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, _pdf_str(f"Market Bias: {bias}"), 0, 1, "L")
    pdf.set_font("Helvetica", "", 10)
    avg_text = f"{avg:.2f}" if np.isfinite(avg) else "N/A"
    pdf.cell(0, 6, _pdf_str(f"Avg RSI: {avg_text} | Extremes: {extreme}"), 0, 1, "L")
    pdf.cell(
        0, 6, _pdf_str(f"Divergences: {bull} Bullish vs {bear} Bearish"),
        0, 1, "L",
    )
    pdf.ln(8)


def _pdf_render_header_row(pdf: _ReportPDF, widths: List[int]) -> None:
    pdf.set_font("Helvetica", "B", 9)
    pdf.set_fill_color(15, 23, 42)
    pdf.set_text_color(255, 255, 255)
    pdf.cell(widths[0], 8, "Devises", 1, 0, "C", True)
    for width, tf_disp_col in zip(widths[1:], TIMEFRAMES_DISPLAY):
        pdf.cell(width, 8, _pdf_str(tf_disp_col), 1, 0, "C", True)
    pdf.ln()


def _pdf_render_data_row(
    pdf: _ReportPDF, data_row: Mapping[str, Any], widths: List[int],
) -> None:
    pdf.cell(
        widths[0], 7,
        _pdf_str(safe_get(data_row, "Devises", ""), max_len=PDF_MAX_PAIR_LEN),
        1, 0, "L",
    )
    for col_index, tf_disp_col in enumerate(TIMEFRAMES_DISPLAY):
        tf_cell = safe_get(data_row, tf_disp_col, {})
        rsi_val = safe_float(safe_get(tf_cell, "rsi"))
        div_val = safe_get(tf_cell, "divergence", "")
        if np.isfinite(rsi_val):
            if rsi_val <= RSI_OVERSOLD:
                pdf.set_fill_color(220, 252, 231)
            elif rsi_val >= RSI_OVERBOUGHT:
                pdf.set_fill_color(254, 226, 226)
            else:
                pdf.set_fill_color(255, 255, 255)
            txt = f"{rsi_val:.1f}"
        else:
            pdf.set_fill_color(245, 245, 245)
            txt = "N/A"
        if div_val == DivergenceLabel.HAUSSIERE.value:
            txt += " ^"
        elif div_val == DivergenceLabel.BAISSIERE.value:
            txt += " v"
        pdf.cell(widths[col_index + 1], 7, _pdf_str(txt), 1, 0, "C", True)
    pdf.ln()


def create_pdf_report(
    results: Any,
    statistics: Any,
    scan_ts: str,
) -> bytes:
    if not isinstance(results, list):
        results = []
    if not isinstance(statistics, Mapping):
        statistics = {}
    try:
        pdf = _ReportPDF(scan_ts=scan_ts)
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        _pdf_render_summary(pdf, statistics)

        widths: List[int] = [40] + [44] * len(TIMEFRAMES_DISPLAY)
        _pdf_render_header_row(pdf, widths)

        pdf.set_text_color(0, 0, 0)
        pdf.set_font("Helvetica", "", 8)
        rendered = 0
        for data_row in results:
            if rendered >= PDF_MAX_ROWS:
                logger.warning(
                    "PDF row cap %d reached; truncating output", PDF_MAX_ROWS,
                )
                break
            if isinstance(data_row, Mapping):
                _pdf_render_data_row(pdf, data_row, widths)
                rendered += 1

        try:
            out = pdf.output()
        except TypeError:
            out = pdf.output(dest="S")

        if isinstance(out, (bytes, bytearray)):
            return bytes(out)
        if isinstance(out, str):
            return out.encode("latin-1", "replace")
        return b""
    except Exception:
        logger.exception("PDF export failed")
        return b""


# =============================================================================
# 9. [PRESENTATION] Streamlit UI
# =============================================================================

def _safe_rerun() -> None:
    """Rerun Streamlit script with robust fallback compatibility."""
    try:
        st.rerun()
    except AttributeError:
        st.experimental_rerun()


st.set_page_config(
    page_title="RSI & Divergence Screener Pro",
    page_icon="",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
.screener-header {
    color: #0F172A; font-weight: 700;
    border-bottom: 3px solid #0F172A; padding-bottom: 12px; margin-bottom: 18px;
}
.update-info { color: #64748B; font-size: 12px; margin-bottom: 8px; }
</style>
""", unsafe_allow_html=True)

st.markdown(
    '<h1 class="screener-header">Screener RSI &amp; Divergence Pro</h1>',
    unsafe_allow_html=True,
)


def _format_rsi(value: float) -> str:
    return f"{value:.2f}" if np.isfinite(value) else ""


# ---- Session state defaults ----
_SESSION_DEFAULTS: Mapping[str, Any] = {
    "scan_done": False,
    "scan_data": None,
    "scan_stats": None,
    "scan_ts": None,
    "scan_fingerprint": "",
    "auth_error_msg": "",
    "export_pdf": b"",
    "export_json": b"",
    "export_csv": b"",
}
for _state_key, _default_val in _SESSION_DEFAULTS.items():
    if _state_key not in st.session_state:
        st.session_state[_state_key] = _default_val


def _current_exports() -> Dict[str, bytes]:
    """Retrieve current export blobs directly from session state for complete

    session isolation, thread safety, and multi-node reliability.
    """
    return {
        "pdf": st.session_state.get("export_pdf", b""),
        "json": st.session_state.get("export_json", b""),
        "csv": st.session_state.get("export_csv", b""),
    }


def _publish_json_bytes(
    payload: bytes, scan_ts: datetime, scan_id: str,
) -> Dict[str, Any]:
    """[AUDIT-P09] Publie le JSON de façon ATOMIQUE (temporaire + os.replace).

    Chemins configurables par secrets (aucune écriture si non configurés) :
      * `RSI_JSON_OUTPUT_PATH` : fichier consommé par merge.py (ex. /data/rsi.json)
      * `RSI_JSON_ARCHIVE_DIR` : répertoire d'archivage horodaté (optionnel)

    Garanties :
      * `os.replace` est atomique sur un même système de fichiers : le
        consommateur ne peut JAMAIS lire un fichier partiellement écrit ;
      * un échec de publication est journalisé et retourné, jamais propagé : le
        scan reste valide et l'export navigateur reste disponible.
    """
    report: Dict[str, Any] = {"attempted": False, "published": [], "errors": []}
    try:
        out_path = str(st.secrets.get("RSI_JSON_OUTPUT_PATH", "") or "").strip()
        archive_dir = str(st.secrets.get("RSI_JSON_ARCHIVE_DIR", "") or "").strip()
    except Exception:  # pylint: disable=broad-except
        return report
    if not out_path and not archive_dir:
        return report

    report["attempted"] = True
    stamp = scan_ts.strftime("%Y%m%dT%H%M%SZ")
    targets = []
    if out_path:
        targets.append(out_path)
    if archive_dir:
        targets.append(os.path.join(
            archive_dir, f"rsi_{stamp}_{scan_id or 'noid'}.json",
        ))

    for target in targets:
        tmp = f"{target}.tmp"
        try:
            parent = os.path.dirname(target)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(tmp, "wb") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)   # atomique : jamais de lecture partielle
            report["published"].append(target)
        except (OSError, ValueError):
            # ValueError : chemin invalide (ex. octet NUL) — un chemin fourni
            # par configuration ne doit JAMAIS faire échouer le scan.
            logger.exception("RSI JSON publication failed for %s", target)
            report["errors"].append(f"{target}: OSError/ValueError")
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except OSError:
                pass
    return report


def _finalize_scan_outcome(outcome: Mapping[str, Any]) -> None:
    """Persist scan outcome to session_state. Exports go directly into session_state

    for robust, isolated, and multi-node compliant downloads.
    """
    raw_results = outcome.get("results") or []
    scan_ts_obj_local: datetime = (
        outcome.get("scan_ts") or datetime.now(timezone.utc)
    )

    statistics = compute_statistics(raw_results)

    # [AUDIT-P04/P09] Le `scan_id` et la complétude remontent jusqu'au document :
    # le consommateur peut corréler le fichier aux logs serveur et VÉRIFIER qu'il
    # a reçu l'univers complet au lieu de l'inférer.
    scan_id_out = str(outcome.get("scan_id", "") or "")
    expected_cells_out = int(
        outcome.get("expected_cells") or len(ASSETS) * len(TIMEFRAMES_DISPLAY),
    )
    ok_cells_out = int(outcome.get("successful_cells") or 0)
    completeness_out = {
        "instruments_received": len(raw_results) if isinstance(raw_results, list) else 0,
        "cells_expected": expected_cells_out,
        "cells_ok": ok_cells_out,
        "cells_failed": max(0, expected_cells_out - ok_cells_out),
    }

    # Pre-generate target formats upon completion
    csv_bytes = create_csv_export(raw_results)
    json_bytes = create_json_export(
        raw_results, statistics, scan_ts_obj_local,
        scan_id=scan_id_out, completeness=completeness_out,
    )
    pdf_bytes = create_pdf_report(
        raw_results,
        statistics,
        scan_ts_obj_local.strftime("%d/%m/%Y %H:%M:%S UTC")
    )

    st.session_state["scan_done"] = True
    st.session_state["scan_data"] = raw_results
    st.session_state["scan_stats"] = statistics
    st.session_state["scan_ts"] = scan_ts_obj_local
    st.session_state["export_csv"] = csv_bytes
    st.session_state["export_json"] = json_bytes
    st.session_state["export_pdf"] = pdf_bytes
    # [AUDIT-P09] Publication machine (atomique) si un chemin est configuré.
    st.session_state["publish_report"] = _publish_json_bytes(
        json_bytes, scan_ts_obj_local, scan_id_out,
    )
    st.session_state["scan_id"] = scan_id_out
    st.session_state["auth_error_msg"] = ""


# ---- UI controls (download bar always rendered) ----

_current_cooldown = _SCAN_STATE.cooldown_remaining()
_cooldown_active = _current_cooldown > 0

_ts_obj = st.session_state.get("scan_ts")
if isinstance(_ts_obj, datetime):
    st.markdown(
        f'<div class="update-info">Derniere mise a jour: '
        f'{html_lib.escape(_ts_obj.strftime("%Y-%m-%d %H:%M:%S %Z"))}</div>',
        unsafe_allow_html=True,
    )

if _cooldown_active:
    st.info(f"Prochain scan disponible dans {int(_current_cooldown)}s")

if st.session_state.get("auth_error_msg"):
    st.error(st.session_state["auth_error_msg"])

_dl_col1, _dl_col2, _dl_col3 = st.columns(3)
_exports = _current_exports()
_ts_for_name = (
    _ts_obj.strftime("%Y%m%d_%H%M") if isinstance(_ts_obj, datetime) else "noscan"
)

with _dl_col1:
    st.download_button(
        "PDF",
        data=_exports["pdf"],
        file_name=f"RSI_Report_{_ts_for_name}.pdf",
        mime="application/pdf",
        use_container_width=True,
        disabled=not _exports["pdf"],
        key="dl_pdf",
    )
with _dl_col2:
    st.download_button(
        "JSON",
        data=_exports["json"],
        file_name=f"RSI_Report_{_ts_for_name}.json",
        mime="application/json",
        use_container_width=True,
        disabled=not _exports["json"],
        key="dl_json",
    )
with _dl_col3:
    st.download_button(
        "CSV",
        data=_exports["csv"],
        file_name=f"RSI_Report_{_ts_for_name}.csv",
        mime="text/csv",
        use_container_width=True,
        disabled=not _exports["csv"],
        key="dl_csv",
    )


# ---- Scan trigger ----

if not st.session_state["scan_done"]:
    if st.button(
        "LANCER LE SCAN COMPLET",
        type="primary",
        use_container_width=True,
        disabled=_cooldown_active,
        key="btn_start_scan",
    ):
        prog_bar = st.progress(0.0)
        status_txt = st.empty()

        def on_progress_main_thread(pct: float, asset: str) -> None:
            try:
                bounded = min(max(float(pct), 0.0), 1.0)
                prog_bar.progress(bounded)
                status_txt.text(f"Analyse: {asset} ({int(bounded * 100)}%)")
            except (ValueError, TypeError, RuntimeError):
                pass

        scan_outcome: Optional[Dict[str, Any]] = None
        try:
            scan_outcome = run_scan(progress_emitter=on_progress_main_thread)
        except (RuntimeError, OSError):
            logger.exception("run_scan raised unexpectedly")
            scan_outcome = None

        if scan_outcome is None:
            st.error("Un scan est deja en cours.")
        elif scan_outcome.get("results") is None:
            st.info(
                f"Cooldown actif: {int(scan_outcome.get('cooldown', 0))}s"
            )
        else:
            if scan_outcome.get("auth_abort"):
                st.session_state["auth_error_msg"] = (
                    "Authentification OANDA refusee. "
                    "Verifier les secrets puis relancer."
                )
                st.error(st.session_state["auth_error_msg"])
            else:
                _finalize_scan_outcome(scan_outcome)
                _safe_rerun()
else:
    if st.button(
        "Nouveau scan",
        use_container_width=True,
        disabled=_cooldown_active,
        key="btn_reset_scan",
    ):
        st.session_state["scan_done"] = False
        st.session_state["export_pdf"] = b""
        st.session_state["export_json"] = b""
        st.session_state["export_csv"] = b""
        _safe_rerun()


# ---- Results rendering ----

def _row_status_badge(status: str) -> Tuple[str, str]:
    """Returns (badge_html, status_category)."""
    if status == ScanStatus.ERROR.value:
        return (
            ' <span style="color:#DC2626;font-size:10px;font-weight:600;">ERR</span>',
            "err",
        )
    if status == ScanStatus.PARTIAL.value:
        return (
            ' <span style="color:#F59E0B;font-size:10px;font-weight:600;">PART</span>',
            "partial",
        )
    if status == ScanStatus.TIMEOUT.value:
        return (
            ' <span style="color:#DC2626;font-size:10px;font-weight:600;">TIMEOUT</span>',
            "timeout",
        )
    if status == ScanStatus.AUTH_ABORT.value:
        return (
            ' <span style="color:#DC2626;font-size:10px;font-weight:600;">AUTH</span>',
            "auth",
        )
    return "", "ok"


# [FIX-UI] Cell rendering now uses INLINE styles instead of CSS classes. Inline
# styles survive Streamlit's HTML sanitization unconditionally, whereas
# class-based styles injected via a separate <style> block can be stripped or
# fail to bind to a table emitted by st.html / st.markdown. This is the root
# cause of the regression where the "Devises" column text and the friendly
# green/red cell colours disappeared. Logic is otherwise identical.
_CELL_BASE_STYLE: str = (
    "padding:6px;text-align:center;border:1px solid #E5E7EB;"
)
_DEVISES_STYLE: str = (
    "padding:6px;text-align:left;border:1px solid #E5E7EB;"
    "font-weight:600;background:#F8FAFC;color:#0F172A;"
)
_TH_STYLE: str = (
    "background:#0F172A;color:#ffffff;padding:8px;text-align:center;"
    "border:1px solid #0F172A;"
)
_TABLE_STYLE: str = (
    "width:100%;border-collapse:collapse;font-size:13px;"
    "font-family:inherit;"
)


def _render_cell_html(rsi_val: float, div_val: str) -> str:
    if np.isfinite(rsi_val) and rsi_val <= RSI_OVERSOLD:
        bg = "background:#DCFCE7;color:#166534;font-weight:600;"
    elif np.isfinite(rsi_val) and rsi_val >= RSI_OVERBOUGHT:
        bg = "background:#FEE2E2;color:#991B1B;font-weight:600;"
    else:
        bg = "background:#ffffff;color:#334155;"

    val_str = _format_rsi(rsi_val)
    if div_val == DivergenceLabel.HAUSSIERE.value:
        icon_html = (
            '<span style="font-weight:bold;margin-left:4px;color:#16A34A;">'
            "&#8593;</span>"
        )
    elif div_val == DivergenceLabel.BAISSIERE.value:
        icon_html = (
            '<span style="font-weight:bold;margin-left:4px;color:#DC2626;">'
            "&#8595;</span>"
        )
    elif div_val == DivergenceLabel.STALE.value:
        icon_html = (
            '<span style="color:#B45309;font-size:10px;margin-left:6px;">'
            "STALE</span>"
        )
    else:
        icon_html = ""
    return (
        f'<td style="{_CELL_BASE_STYLE}{bg}">'
        f'{html_lib.escape(val_str)} {icon_html}</td>'
    )


if st.session_state["scan_done"] and st.session_state["scan_data"]:
    _data = st.session_state["scan_data"]
    _stats = st.session_state["scan_stats"] or {}

    _header_cells = "".join(
        f'<th style="{_TH_STYLE}">{html_lib.escape(tf_disp)}</th>'
        for tf_disp in TIMEFRAMES_DISPLAY
    )
    _rows_html: List[str] = []
    _counters: Dict[str, int] = {
        "err": 0, "timeout": 0, "auth": 0, "partial": 0, "ok": 0,
    }

    for _data_row in _data:
        if not isinstance(_data_row, Mapping):
            continue
        _pair_name = html_lib.escape(str(safe_get(_data_row, "Devises", "")))
        _status_val = str(safe_get(_data_row, "Status", "OK"))
        _badge_html, _category = _row_status_badge(_status_val)
        _counters[_category] = _counters.get(_category, 0) + 1

        _cells_html: List[str] = []
        for _tf_disp in TIMEFRAMES_DISPLAY:
            _tf_cell = safe_get(_data_row, _tf_disp, {})
            _rsi_val = safe_float(safe_get(_tf_cell, "rsi"))
            _div_val = safe_get(
                _tf_cell, "divergence", DivergenceLabel.AUCUNE.value,
            )
            _cells_html.append(_render_cell_html(_rsi_val, str(_div_val)))

        _rows_html.append(
            f'<tr><td style="{_DEVISES_STYLE}">{_pair_name}{_badge_html}</td>'
            f'{"".join(_cells_html)}</tr>'
        )

    _table_html = (
        f'<table style="{_TABLE_STYLE}"><thead><tr>'
        f'<th style="{_TH_STYLE}">Devises</th>'
        + _header_cells
        + "</tr></thead><tbody>"
        + "".join(_rows_html)
        + "</tbody></table>"
    )
    # [FIX-UI] Use st.html (dedicated HTML renderer) rather than
    # st.markdown(unsafe_allow_html=True). st.markdown runs the content through
    # a Markdown parser that is whitespace-sensitive and can mangle/escape large
    # concatenated HTML tables (dropping the first-cell text — the "Devises"
    # column). st.html renders raw HTML verbatim. Fallback to st.markdown for
    # older Streamlit versions that lack st.html.
    try:
        st.html(_table_html)
    except AttributeError:
        st.markdown(_table_html, unsafe_allow_html=True)

    if _counters["err"]:
        st.warning(f"{_counters['err']} actif(s) en erreur.")
    if _counters["timeout"]:
        st.warning(f"{_counters['timeout']} actif(s) en timeout.")
    if _counters["auth"]:
        st.error(f"{_counters['auth']} actif(s) interrompus (auth).")
    if _counters["partial"]:
        st.info(f"{_counters['partial']} actif(s) partiels.")

    if _stats:
        st.markdown("### Signal Statistics")
        _stat_cols = st.columns(len(TIMEFRAMES_DISPLAY))
        _by_tf_stats = _stats.get("by_tf", {}) or {}
        for _col_idx, _tf_disp in enumerate(TIMEFRAMES_DISPLAY):
            _tf_stat = _by_tf_stats.get(_tf_disp, {}) or {}
            with _stat_cols[_col_idx]:
                _total = (
                    safe_int(_tf_stat.get("extreme_oversold", 0))
                    + safe_int(_tf_stat.get("oversold", 0))
                    + safe_int(_tf_stat.get("extreme_overbought", 0))
                    + safe_int(_tf_stat.get("overbought", 0))
                )
                st.metric(f"RSI States {_tf_disp}", str(_total))
                st.caption(
                    f"{int(RSI_EXTREME_LOW)}: "
                    f"{_tf_stat.get('extreme_oversold', 0)} | "
                    f"{int(RSI_OVERSOLD)}: {_tf_stat.get('oversold', 0)} | "
                    f"{int(RSI_OVERBOUGHT)}: {_tf_stat.get('overbought', 0)} | "
                    f"{int(RSI_EXTREME_HIGH)}: "
                    f"{_tf_stat.get('extreme_overbought', 0)}"
                )

        _avg_rsi = safe_float(_stats.get("avg_rsi"))
        _avg_text = f"{_avg_rsi:.2f}" if np.isfinite(_avg_rsi) else "N/A"
        st.markdown(
            f"**Market Bias:** "
            f'<span style="color:{_stats.get("bias_color", "#888")};'
            f'font-weight:600;">'
            f'{html_lib.escape(str(_stats.get("market_bias", "")))}'
            f'</span> | Avg RSI: {_avg_text} | '
            f"Bull div: {safe_int(_stats.get('total_bull_div', 0))} | "
            f"Bear div: {safe_int(_stats.get('total_bear_div', 0))}",
            unsafe_allow_html=True,
        )
