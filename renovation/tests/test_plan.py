from datetime import date

from conftest import TODAY, add_goal, add_task, build_plan

from renovation import plan
from renovation.db import get_db


def d(s):
    return date.fromisoformat(s)


def doors_example(app, deadline="2027-05-01"):
    """Doors on the 2nd floor need parquet, which needs drywall, which needs
    electricity and finished walls."""
    goal = add_goal(app, "Doors, 2nd floor", deadline)
    walls = add_task(app, goal, "Walls", days=10)
    electricity = add_task(app, goal, "Electricity", days=5)
    drywall = add_task(app, goal, "Drywall", days=7, needs=(walls, electricity))
    parquet = add_task(app, goal, "Parquet", days=4, needs=(drywall,))
    doors = add_task(app, goal, "Doors", days=2, needs=(parquet,))
    return goal, {"walls": walls, "electricity": electricity, "drywall": drywall, "parquet": parquet, "doors": doors}


def test_tasks_follow_what_they_need(app):
    _, ids = doors_example(app)
    tasks = build_plan(app)["tasks"]
    assert tasks[ids["walls"]]["start"] == TODAY
    assert tasks[ids["walls"]]["end"] == d("2026-11-27")
    assert tasks[ids["electricity"]]["end"] == d("2026-11-22")
    # Drywall waits for the later of the two.
    assert tasks[ids["drywall"]]["start"] == d("2026-11-28")
    assert tasks[ids["drywall"]]["end"] == d("2026-12-04")
    assert tasks[ids["parquet"]]["start"] == d("2026-12-05")
    assert tasks[ids["doors"]]["end"] == d("2026-12-10")
    assert tasks[ids["drywall"]]["blocked_by"] == sorted([ids["walls"], ids["electricity"]])


def test_slack_comes_back_from_the_deadline(app):
    goal, ids = doors_example(app, deadline="2026-12-12")
    current = build_plan(app)
    tasks = current["tasks"]
    assert tasks[ids["doors"]]["latest_end"] == d("2026-12-12")
    assert tasks[ids["doors"]]["slack"] == 2
    assert tasks[ids["doors"]]["flag"] == "tight"
    # Electricity is short and has more room than the walls.
    assert tasks[ids["electricity"]]["slack"] == 7
    assert tasks[ids["electricity"]]["flag"] == ""
    assert tasks[ids["walls"]]["latest_start"] == d("2026-11-20")
    g = current["goals"][0]
    assert g["end"] == d("2026-12-10") and g["slack"] == 2 and not g["late"] and g["tight"]


def test_late_when_the_deadline_cant_be_made(app):
    _, ids = doors_example(app, deadline="2026-12-01")
    current = build_plan(app)
    assert current["tasks"][ids["doors"]]["flag"] == "late"
    assert current["tasks"][ids["doors"]]["slack"] == -9
    assert current["goals"][0]["late"]


def test_a_later_goal_constrains_work_it_needs_from_another(app):
    early = add_goal(app, "Kitchen", "2027-06-01")
    later = add_goal(app, "Move in", "2026-12-01")
    pipes = add_task(app, early, "Pipes", days=5)
    add_task(app, later, "Clean", days=3, needs=(pipes,))
    tasks = build_plan(app)["tasks"]
    # Pipes belong to the kitchen (June) but cleaning (Dec 1) waits for them.
    assert tasks[pipes]["latest_end"] == d("2026-11-28")


def test_not_before_and_status_dates(app):
    goal = add_goal(app, "Bathroom", "2027-03-01")
    booked = add_task(app, goal, "Tiler", days=3, not_before="2027-01-10")
    overrun = add_task(app, goal, "Plumbing", days=2, status="doing", started_on="2026-11-10")
    done = add_task(app, goal, "Demolition", days=4, status="done", done_on="2026-11-05")
    slipped = add_task(app, goal, "Old plan", days=2, not_before="2026-10-01")
    tasks = build_plan(app)["tasks"]
    assert (tasks[booked]["start"], tasks[booked]["end"]) == (d("2027-01-10"), d("2027-01-12"))
    # Two days from 10 Nov, still not done: it runs until today.
    assert (tasks[overrun]["start"], tasks[overrun]["end"]) == (d("2026-11-10"), TODAY)
    assert (tasks[done]["start"], tasks[done]["end"]) == (d("2026-11-02"), d("2026-11-05"))
    assert tasks[done]["flag"] == ""
    # A to-do whose date passed starts today.
    assert tasks[slipped]["start"] == TODAY


def test_done_dependency_unblocks(app):
    goal = add_goal(app, "Roof", "2027-03-01")
    first = add_task(app, goal, "Scaffold", days=2, status="done", started_on="2026-11-01", done_on="2026-11-02")
    second = add_task(app, goal, "Tiles", days=3, needs=(first,))
    tasks = build_plan(app)["tasks"]
    assert tasks[second]["start"] == TODAY
    assert tasks[second]["blocked_by"] == []


def test_goal_without_tasks(app):
    add_goal(app, "Garden", "2027-04-01", budget=5_000_00)
    g = build_plan(app)["goals"][0]
    assert g["start"] is None and g["total"] == 0
    assert g["estimate"] == 5_000_00
    assert g["days_left"] == 134


def test_goal_estimate_prefers_task_costs(app):
    goal = add_goal(app, "Garden", "2027-04-01", budget=5_000_00)
    add_task(app, goal, "Fence", cost=1_200_00, paid=200_00)
    g = build_plan(app)["goals"][0]
    assert (g["estimate"], g["cost"], g["paid"]) == (1_200_00, 1_200_00, 200_00)


def test_goals_by_deadline_finished_last(app):
    a = add_goal(app, "A", "2027-05-01")
    b = add_goal(app, "B", "2027-02-01")
    c = add_goal(app, "C", "2027-01-01")
    add_task(app, c, "x", status="done", done_on="2026-11-01")
    assert [g["id"] for g in build_plan(app)["goals"]] == [b, a, c]


def test_would_cycle(app):
    _, ids = doors_example(app)
    with app.app_context():
        db = get_db()
        assert plan.would_cycle(db, ids["walls"], ids["doors"])  # walls waiting for doors: loop
        assert plan.would_cycle(db, ids["walls"], ids["walls"])
        assert not plan.would_cycle(db, ids["doors"], ids["walls"])  # already implied, no loop


def test_upcoming(app):
    goal = add_goal(app, "Hall", "2027-06-01")
    rush = add_goal(app, "Rush", "2026-11-19")
    late = add_task(app, rush, "Too long", days=5)
    doing = add_task(app, goal, "Painting", days=5, status="doing", started_on="2026-11-17")
    ready = add_task(app, goal, "Lights", days=1)
    waiting = add_task(app, goal, "Lamps", days=1, needs=(doing,))
    later = add_task(app, goal, "Floor", days=1, not_before="2027-03-01")
    lists = plan.upcoming(build_plan(app))
    assert [t["id"] for t in lists["late"]] == [late]
    assert [t["id"] for t in lists["doing"]] == [doing]
    assert [t["id"] for t in lists["ready"]] == [ready]
    assert [t["id"] for t in lists["soon"]] == [waiting]
    assert later not in [t["id"] for section in ("late", "doing", "ready", "soon") for t in lists[section]]


def test_to_json(app):
    _, ids = doors_example(app)
    body = plan.to_json(build_plan(app))
    assert body["today"] == "2026-11-18"
    assert body["goals"][0]["deadline"] == "2027-05-01"
    drywall = next(t for t in body["tasks"] if t["id"] == ids["drywall"])
    assert drywall["start"] == "2026-11-28" and drywall["needs"] == sorted([ids["walls"], ids["electricity"]])
