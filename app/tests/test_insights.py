from datetime import date

from conftest import add_tx

from finance import insights
from finance.db import get_db


def with_db(app, fn, *args):
    with app.app_context():
        return fn(get_db(), *args)


def test_typical_month_is_the_median_not_the_mean(app):
    # Three ordinary months and one with a car purchase.
    for ym, food in (("2026-01", 400_00), ("2026-02", 500_00), ("2026-03", 450_00), ("2026-04", 24_000_00)):
        add_tx(app, f"{ym}-10 12:00:00", food, "food")
    typical = with_db(app, insights.typical_month, "2026-05")
    assert typical["living"] == 475_00  # median of 400, 450, 500, 24000


def test_typical_month_ignores_months_before_the_data_starts(app):
    add_tx(app, "2026-04-10 12:00:00", 300_00, "food")
    assert with_db(app, insights.typical_month, "2026-05")["living"] == 300_00
    assert with_db(app, insights.typical_month, "2026-04") is None


def test_living_pace_projects_from_how_earlier_months_went_on(app):
    # Earlier months: 100 by day 10, 300 by the end.
    for ym in ("2026-01", "2026-02", "2026-03"):
        add_tx(app, f"{ym}-05 12:00:00", 100_00, "food")
        add_tx(app, f"{ym}-20 12:00:00", 200_00, "food")
    add_tx(app, "2026-04-03 12:00:00", 150_00, "food")
    add_tx(app, "2026-04-03 13:00:00", 999_00, "hisa")  # project group: not a living cost
    pace = with_db(app, insights.living_pace, "2026-04", 10)
    assert pace["current"][9] == 150_00
    assert pace["typical"][9] == 100_00 and pace["typical_total"] == 300_00
    assert pace["projected"] == 150_00 + 200_00
    assert len(pace["current"]) == 30


def test_year_windows_compare_the_same_stretch_of_last_year():
    assert insights.year_windows(2026, date(2026, 10, 22)) == (
        "2026-01-01 00:00:00", "2026-10-23 00:00:00", "2025-01-01 00:00:00", "2025-10-23 00:00:00")
    assert insights.year_windows(2025, date(2026, 10, 22)) == (
        "2025-01-01 00:00:00", "2026-01-01 00:00:00", "2024-01-01 00:00:00", "2025-01-01 00:00:00")
    assert insights.year_windows(2028, date(2028, 2, 28))[3] == "2027-03-01 00:00:00"


def test_group_by_month(app):
    add_tx(app, "2026-02-01 10:00:00", 50_00, "food")
    add_tx(app, "2026-02-02 10:00:00", 1000_00, "hisa")
    add_tx(app, "2026-03-02 10:00:00", 40_00, "varcevanje")
    add_tx(app, "2026-03-03 10:00:00", 2500_00, "placa", is_income=True)
    groups = with_db(app, insights.group_by_month, 2026, False)
    assert list(groups) == ["living", "project", "savings"]
    assert groups["living"][1] == 50_00 and groups["project"][1] == 1000_00 and groups["savings"][2] == 40_00
    assert with_db(app, insights.group_by_month, 2026, False, "core").keys() == {"living"}


def test_cumulative_net(app):
    add_tx(app, "2026-01-05 10:00:00", 1000_00, "placa", is_income=True)
    add_tx(app, "2026-01-06 10:00:00", 300_00, "food")
    add_tx(app, "2026-02-06 10:00:00", 200_00, "food")
    assert with_db(app, insights.cumulative_net, 2026, 3)[:4] == [700_00, 500_00, 500_00, None]


def test_category_changes_against_last_year(app):
    add_tx(app, "2025-03-01 10:00:00", 100_00, "food")
    add_tx(app, "2026-03-01 10:00:00", 250_00, "food")
    add_tx(app, "2025-04-01 10:00:00", 80_00, "gas")
    rows = with_db(app, insights.category_changes, "2026-01-01", "2027-01-01", "2025-01-01", "2026-01-01", False)
    by_key = {row["key"]: row for row in rows}
    assert (by_key["food"]["total"], by_key["food"]["prev"], by_key["food"]["change"]) == (250_00, 100_00, 150_00)
    assert (by_key["gas"]["total"], by_key["gas"]["change"]) == (0, -80_00)


def test_business_by_month(app):
    add_tx(app, "2026-07-01 10:00:00", 900_00, "sunny", is_income=True)
    add_tx(app, "2026-07-02 10:00:00", 150_00, "sunny")
    add_tx(app, "2025-08-01 10:00:00", 400_00, "sunny", is_income=True)
    [sunny] = with_db(app, insights.business_by_month, 2026)
    assert sunny["net"][6] == 750_00 and sunny["total"] == 750_00 and sunny["prev_net"] == 400_00


def test_recurring(app):
    with app.app_context():
        db = get_db()
        db.execute("""INSERT INTO repeats (amount_cents, category, is_income, day_of_month, active_from, created_at)
                      VALUES (1989, 'tel', 0, 18, '2026-01-01', '2026-01-01'), (250000, 'placa', 1, 15, '2026-01-01', '2026-01-01')""")
        db.commit()
    assert with_db(app, insights.recurring) == {"n": 2, "expense": 1989, "income": 250000}
