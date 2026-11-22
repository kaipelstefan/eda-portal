import sys, pathlib
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "custom_components" / "eda_portal"))
import importer  # noqa: E402

TZ = ZoneInfo("Europe/Vienna")


def day_rows(day, f, q="L1"):
    return [[f"{day}T{h:02d}:{m:02d}:00", f(h, m), q] for h in range(24) for m in (0, 15, 30, 45)]


def test_basic_hour():
    g = day_rows("2026-11-15", lambda h, m: 0.264 if h == 13 else 0.1)
    p = day_rows("2026-11-15", lambda h, m: 0.237808 if (h == 13 and m == 15) else (0.264 if h == 13 else 0.1))
    hs = importer.build_hours(g, p, TZ, date(2026, 11, 22), 3)
    assert len(hs) == 24
    h13 = [h for h in hs if h["start"] == datetime(2026, 11, 15, 12, tzinfo=timezone.utc)][0]
    assert abs(h13["verbrauch"] - 1.056) < 1e-9
    assert abs(h13["eigendeckung"] - 0.026192) < 1e-9
    assert h13["vorlaeufig"] == 0


def test_missing_quarter_skips_hour():
    g = day_rows("2026-11-15", lambda h, m: 0.1)
    p = day_rows("2026-11-15", lambda h, m: 0.1)
    del g[5]
    hs = importer.build_hours(g, p, TZ, date(2026, 11, 22), 3)
    assert len(hs) == 23


def test_prelim_recent_and_quality():
    g = day_rows("2026-11-21", lambda h, m: 0.1)
    p = day_rows("2026-11-21", lambda h, m: 0.1)
    assert all(h["vorlaeufig"] == 1 for h in importer.build_hours(g, p, TZ, date(2026, 11, 22), 3))
    g = day_rows("2026-11-01", lambda h, m: 0.1, "L2")
    p = day_rows("2026-11-01", lambda h, m: 0.1)
    assert all(h["vorlaeufig"] == 1 for h in importer.build_hours(g, p, TZ, date(2026, 11, 22), 3))


def test_dst_fall_back_25_hours():
    rows = day_rows("2026-10-25", lambda h, m: 0.1)
    # doppelte Stunde 02:00-02:45 einfuegen
    extra = [[f"2026-10-25T02:{m:02d}:00", 0.2, "L1"] for m in (0, 15, 30, 45)]
    idx = next(i for i, r in enumerate(rows) if r[0].startswith("2026-10-25T03:00"))
    rows = rows[:idx] + extra + rows[idx:]
    hs = importer.build_hours(rows, rows, TZ, date(2026, 12, 1), 3)
    assert len(hs) == 25


def test_stat_id():
    assert importer.stat_id("AT009X49343", "verbrauch") == "sensor:eeg_at009x49343_verbrauch"


def test_split_proportional_and_fallback():
    g = day_rows("2026-11-15", lambda h, m: 0.4)
    p = day_rows("2026-11-15", lambda h, m: 0.1)  # eigendeckung 0.3 je Viertelstunde
    gm = day_rows("2026-11-15", lambda h, m: 0.3 if h == 12 else 0.0)
    go = day_rows("2026-11-15", lambda h, m: 0.1 if h == 12 else 0.0)
    hs = importer.build_hours(g, p, TZ, date(2026, 12, 1), 3, gm, go)
    h11 = [h for h in hs if h["start"] == datetime(2026, 11, 15, 11, tzinfo=timezone.utc)][0]  # 12 Uhr lokal
    assert abs(h11["eigendeckung"] - 1.2) < 1e-9
    assert abs(h11["eigendeckung_oliver"] - 0.3) < 1e-9
    assert abs(h11["eigendeckung_maritta"] - 0.9) < 1e-9
    h0 = hs[0]  # keine Erzeugung -> Maritta
    assert h0["eigendeckung_oliver"] == 0 and abs(h0["eigendeckung_maritta"] - 1.2) < 1e-9


def test_no_split_without_generation():
    g = day_rows("2026-11-15", lambda h, m: 0.4)
    hs = importer.build_hours(g, g, TZ, date(2026, 12, 1), 3, [], [])
    assert "eigendeckung_maritta" not in hs[0]
