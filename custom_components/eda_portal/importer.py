"""Reine Rechenlogik (ohne Home-Assistant-Abhaengigkeit, testbar)."""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

QUALITY_OK = "L1"


def stat_id(zp: str, key: str) -> str:
    """Gleiche IDs wie der bisherige Upload im Dashboard."""
    return f"sensor:eeg_{zp.lower()}_{key}"


def _to_utc(rows: list[list], tz: ZoneInfo) -> dict[datetime, tuple[float, str]]:
    """Lokale (naive) Zeitstempel -> UTC. Doppelte lokale Zeiten (Zeitumstellung Oktober)
    werden der Reihe nach als fold=0 / fold=1 gelesen."""
    out: dict[datetime, tuple[float, str]] = {}
    seen: set[str] = set()
    for row in rows:
        try:
            ts, val = row[0], row[1]
            q = row[2] if len(row) > 2 else QUALITY_OK
            if val is None:
                continue
            naive = datetime.fromisoformat(ts)
            fold = 1 if ts in seen else 0
            seen.add(ts)
            local = naive.replace(tzinfo=tz, fold=fold)
            out[local.astimezone(timezone.utc)] = (float(val), str(q))
        except (ValueError, TypeError, IndexError):
            continue
    return out


def build_hours(
    g_rows: list[list],
    p_rows: list[list],
    tz: ZoneInfo,
    today: date,
    prelim_days: int,
) -> list[dict]:
    """Stundenwerte: verbrauch (g), eigendeckung (g - p, >= 0), vorlaeufig (1/0).

    Eine Stunde wird nur geschrieben, wenn alle vier Viertelstunden in g und p vorliegen
    (fehlende Werte werden nie als 0 importiert). Vorlaeufig = Qualitaet != L1 oder der
    Tag liegt weniger als `prelim_days` Tage zurueck."""
    g = _to_utc(g_rows, tz)
    p = _to_utc(p_rows, tz)
    groups: dict[datetime, list[datetime]] = defaultdict(list)
    for q in g:
        if q in p:
            groups[q.replace(minute=0, second=0, microsecond=0)].append(q)
    hours = []
    for start in sorted(groups):
        qs = groups[start]
        if len(qs) != 4:
            continue
        v = sum(g[q][0] for q in qs)
        rest = sum(p[q][0] for q in qs)
        e = max(0.0, v - rest)
        e = min(e, v)
        quality_ok = all(g[q][1] == QUALITY_OK and p[q][1] == QUALITY_OK for q in qs)
        local_day = start.astimezone(tz).date()
        recent = (today - local_day).days < prelim_days
        hours.append(
            {
                "start": start,
                "verbrauch": round(v, 6),
                "eigendeckung": round(e, 6),
                "vorlaeufig": 0 if (quality_ok and not recent) else 1,
            }
        )
    return hours


def days_range(start: date, end: date) -> list[date]:
    n = (end - start).days
    return [start + timedelta(days=i) for i in range(n + 1)]
