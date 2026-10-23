import pytest
from conftest import add_tx, login

from finance import insights
from finance.db import get_db
from finance.util import month_key, now, shift_month


@pytest.fixture
def logged_in(client, outbox):
    login(client, outbox)
    return client


@pytest.fixture
def ym(app):
    with app.app_context():
        return month_key(now())


def test_new_pages_need_login(client):
    for path in ("/living", "/business", "/business/sunny"):
        assert client.get(path).headers["Location"].startswith("/login"), path


def test_living_categories(app, ym):
    add_tx(app, f"{shift_month(ym, -1)}-05 10:00:00", 300_00, "food")
    add_tx(app, f"{shift_month(ym, -2)}-05 10:00:00", 100_00, "food")
    add_tx(app, f"{shift_month(ym, -1)}-06 10:00:00", 60_00, "gas")
    add_tx(app, f"{shift_month(ym, -1)}-07 10:00:00", 5000_00, "hisa")     # project: not living
    add_tx(app, f"{shift_month(ym, -1)}-08 10:00:00", 900_00, "sunny")     # business: not living
    with app.app_context():
        rows = insights.living_categories(get_db(), shift_month(ym, -1), 12)
    assert [r["key"] for r in rows] == ["food", "gas"]
    food = rows[0]
    assert food["total"] == 400_00 and food["months"][-1] == 300_00 and food["months"][-2] == 100_00
    assert food["median"] == 0  # most months had no food entries in this test


def test_living_page(app, logged_in, ym):
    add_tx(app, f"{shift_month(ym, -1)}-05 10:00:00", 300_00, "food")
    add_tx(app, f"{shift_month(ym, -3)}-05 10:00:00", 200_00, "car")
    add_tx(app, f"{ym}-01 10:00:00", 45_50, "food", comment="spar")
    add_tx(app, f"{shift_month(ym, -1)}-07 10:00:00", 5000_00, "hisa")
    html = logged_in.get("/living").get_data(as_text=True)
    assert '<span class="dot"></span>Living costs' in html and "Biggest categories, month by month" in html
    assert "€500.00" in html          # previous 12 months: food 300 + car 200, no house
    assert "€45.50" in html           # this month so far
    assert "Hisa" not in html.split("All living categories")[1]
    assert logged_in.get(f"/living?month={shift_month(ym, -1)}").status_code == 200


def test_business_totals_and_payback(app, ym):
    start = shift_month(ym, -14)
    add_tx(app, f"{start}-10 10:00:00", 3000_00, "sunny", comment="bikes")
    add_tx(app, f"{shift_month(start, 3)}-10 10:00:00", 1000_00, "sunny", is_income=True)
    add_tx(app, f"{shift_month(start, 8)}-10 10:00:00", 2500_00, "sunny", is_income=True)
    with app.app_context():
        db = get_db()
        labels, running = insights.profit_since_start(db, "sunny", ym)
        assert labels[0] == start and len(labels) == 15
        assert running[0] == -3000_00 and running[3] == -2000_00 and running[8] == 500_00 and running[-1] == 500_00
        totals = insights.business_totals(db, "sunny", "0000", "9999")
        assert (totals["income"], totals["expense"], totals["net"]) == (3500_00, 3000_00, 500_00)
        years = insights.business_years(db, "sunny")
        assert sum(y["net"] for y in years) == 500_00


def test_business_pages(app, logged_in, ym):
    start = shift_month(ym, -14)
    add_tx(app, f"{start}-10 10:00:00", 3000_00, "sunny", comment="bikes")
    add_tx(app, f"{shift_month(start, 8)}-10 10:00:00", 3500_00, "sunny", is_income=True, comment="july rentals")
    add_tx(app, f"{ym}-01 10:00:00", 120_00, "app", is_income=True, comment="prvi gosti")
    overview = logged_in.get("/business").get_data(as_text=True)
    assert "Sunny" in overview and "App" in overview and "apartment rental" in overview

    page = logged_in.get(f"/business/sunny?year={start[:4]}").get_data(as_text=True)
    assert "Profit since the start" in page and "+€500.00" in page
    assert f"back above zero in" in page
    assert "bikes" in page or "july rentals" in page
    assert logged_in.get("/business/app").status_code == 200
    assert logged_in.get("/business/nope").status_code == 404


def test_dark_only(client):
    html = client.get("/login").get_data(as_text=True)
    assert '<meta name="color-scheme" content="dark">' in html
    assert "theme-toggle" not in html and "data-theme" not in html


def test_fonts_are_served_locally(client):
    css = client.get("/static/fonts/fonts.css").get_data(as_text=True)
    assert "fonts.googleapis" not in css and "inter-var-latin.woff2" in css
    assert client.get("/static/fonts/inter-var-latin.woff2").status_code == 200


def test_months_between():
    assert insights.months_between("2025-11", "2026-02") == 4
    assert insights.months_between("2026-02", "2026-02") == 1
