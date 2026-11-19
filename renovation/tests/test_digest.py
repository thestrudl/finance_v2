"""The Monday/Friday "ready to start" email."""
from datetime import datetime

import pytest
from conftest import ANA, BEN, add_goal, add_task, make_app

from renovation import digest, mail, maintenance
from renovation.db import get_db

SCHEDULE = digest.parse_schedule("mon 08:00, fri 18:00")


def test_parse_and_describe_schedule():
    assert SCHEDULE == [(0, 8, 0), (4, 18, 0)]
    assert digest.parse_schedule("Monday 8:30,  sat 09:05") == [(0, 8, 30), (5, 9, 5)]
    assert digest.parse_schedule("") == []
    assert digest.describe(SCHEDULE) == "Monday at 08:00 and Friday at 18:00"
    assert digest.describe([(2, 7, 0)]) == "Wednesday at 07:00"
    for bad in ("mon", "xyz 08:00", "mon 25:00", "fri 18:60"):
        with pytest.raises(ValueError):
            digest.parse_schedule(bad)


@pytest.mark.parametrize("moment, slot", [
    (datetime(2026, 11, 23, 7, 59), None),                                # Monday, a minute early
    (datetime(2026, 11, 23, 8, 0), datetime(2026, 11, 23, 8, 0)),         # on time
    (datetime(2026, 11, 23, 13, 30), datetime(2026, 11, 23, 8, 0)),       # caught up after a restart
    (datetime(2026, 11, 23, 14, 1), None),                                # too late: skipped
    (datetime(2026, 11, 24, 8, 0), None),                                 # Tuesday
    (datetime(2026, 11, 27, 18, 4), datetime(2026, 11, 27, 18, 0)),       # Friday evening
    (datetime(2026, 11, 28, 0, 0), datetime(2026, 11, 27, 18, 0)),        # past midnight, within 6 hours
    (datetime(2026, 11, 28, 0, 1), None),
])
def test_due_slot(moment, slot):
    assert digest.due_slot(moment, SCHEDULE) == slot


@pytest.fixture
def house(app):
    walls = add_goal(app, "Walls", "2026-12-20")
    doors = add_goal(app, "Doors", "2027-05-01")
    plaster = add_task(app, walls, "Plaster", days=5, cost=1_200_00)
    add_task(app, walls, "Sanding", days=3, needs=(plaster,))                 # blocked
    add_task(app, doors, "Measure doors", days=1)                             # ready
    add_task(app, doors, "Order doors", days=1, not_before="2026-11-25")      # booked this week
    add_task(app, doors, "Fit doors", days=2, not_before="2027-01-15")        # far off
    add_task(app, doors, "Old frames", status="doing", started_on="2026-11-16", days=4)
    add_task(app, walls, "Done already", status="done", done_on="2026-11-10")
    rush = add_goal(app, "Rush", "2026-11-19")
    add_task(app, rush, "Too long", days=5)                                   # ready, but late
    return app


def test_content(house):
    from conftest import build_plan

    lists = digest.content(build_plan(house))
    assert [t["title"] for t in lists["ready"]] == ["Too long", "Plaster", "Measure doors"]  # most urgent first
    assert [t["title"] for t in lists["doing"]] == ["Old frames"]
    assert [t["title"] for t in lists["booked"]] == ["Order doors"]
    assert lists["late"] == 1


def test_sent_to_every_user_once_per_slot(house, outbox):
    monday = datetime(2026, 11, 23, 8, 2)
    with house.app_context():
        assert digest.run_due(monday) == datetime(2026, 11, 23, 8, 0)
        assert digest.run_due(datetime(2026, 11, 23, 8, 7)) is None   # the next check: already sent
    assert sorted(m["To"] for m in outbox) == ["Ana Novak <ana@example.com>", "Ben Novak <ben@example.com>"]
    message = outbox[0]
    assert message["Subject"] == "Renovation: 3 tasks ready to start this week"
    text = message.get_body(("plain",)).get_content()
    assert "Hi Ben," in text or "Hi Ana," in text
    assert "- Plaster (Walls), 5 days, €1,200" in text
    assert "Sanding" not in text and "Fit doors" not in text and "Done already" not in text
    assert "LATE: should have started by" in text
    assert "Booked this week" in text and "Order doors (Doors), from 25 Nov" in text
    assert "1 task is behind schedule: https://renovation.example/upcoming" in text
    assert "https://renovation.example/tasks/" in text
    assert "every Monday at 08:00 and Friday at 18:00" in text
    html = message.get_body(("html",)).get_content()
    assert "Ready to start" in html and "Open the plan" in html


def test_friday_subject_and_nothing_ready(app, outbox):
    goal = add_goal(app, "Walls", "2027-05-01")
    add_task(app, goal, "Later", not_before="2027-02-01")
    with app.app_context():
        digest.run_due(datetime(2026, 11, 27, 18, 0))
    assert outbox[0]["Subject"] == "Renovation: nothing ready to start for the weekend"
    assert "Nothing is ready to start" in outbox[0].get_body(("plain",)).get_content()


def test_failed_send_is_tried_again(house, monkeypatch):
    calls = []

    def flaky(message):
        calls.append(message)
        if len(calls) == 1:
            raise OSError("SMTP down")

    monkeypatch.setattr(mail, "deliver", flaky)
    with house.app_context():
        with pytest.raises(OSError):
            digest.run_due(datetime(2026, 11, 23, 8, 0))
        assert get_db().execute("SELECT COUNT(*) FROM digest_runs").fetchone()[0] == 0
        assert digest.run_due(datetime(2026, 11, 23, 8, 5)) is not None


def test_maintenance_tick_sends_and_survives_failures(house, monkeypatch, outbox):
    from renovation import util

    monkeypatch.setattr(util, "now", lambda: datetime(2026, 11, 23, 8, 1))
    monkeypatch.setattr(maintenance, "now", lambda: datetime(2026, 11, 23, 8, 1))
    maintenance.tick(house)
    assert len(outbox) == 2
    monkeypatch.setattr(mail, "deliver", lambda m: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(maintenance, "now", lambda: datetime(2026, 11, 27, 18, 0))
    maintenance.tick(house)  # logs the failure, still makes the backup
    assert list((house.config["BACKUP_DIR"] and __import__("pathlib").Path(house.config["BACKUP_DIR"])).glob("*.db"))


def test_turned_off(tmp_path, outbox):
    app = make_app(tmp_path, DIGEST="")
    with app.app_context():
        assert digest.run_due(datetime(2026, 11, 23, 8, 0)) is None
    assert outbox == []


def test_cli_preview_and_send_to_one(house, outbox):
    runner = house.test_cli_runner()
    result = runner.invoke(args=["send-digest", "--preview"])
    assert result.exit_code == 0, result.output
    assert "Subject: Renovation: 3 tasks ready to start" in result.output
    assert outbox == []
    result = runner.invoke(args=["send-digest", "--to", ANA])
    assert "Sent to 1 person." in result.output
    assert [m["To"] for m in outbox] == ["Ana Novak <ana@example.com>"]
