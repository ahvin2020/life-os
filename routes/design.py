"""/design — the living style guide (kitchen sink).

Renders every shared component in every state from the SAME macros + CSS the
product ships (_macros.html + app.css), so it can't rot the way a frozen mockup
does. Judge new pages against this page, not design/mockup.html.

No DB, no vault, no mutation — pure sample data assembled here. Not in the nav;
it's a build-time reference tool, reachable at /design."""

from __future__ import annotations

from flask import Blueprint, render_template

from core.web_core import today_iso

bp = Blueprint("design", __name__)


def _task(**over):
    """A task dict shaped like domain.tasks_core.task_dict — every field a macro
    touches, defaulted, so a sample only overrides what the state needs."""
    base = dict(
        id=0, title="Sample task", due_date=None, priority=None, category=None,
        col="week", recur_rule=None, goal_id=None, planned_on=None, media=None,
        subtasks=[], sub_total=0, sub_done=0, done=False, pinned=False,
        week_since=None, reschedule_count=0,
    )
    base.update(over)
    return base


def _note(**over):
    base = dict(
        slug="sample", kind="text", title="Sample note", snippet="", body="",
        tags=[], spaces=[], created="2026-07-10", url=None, domain=None,
    )
    base.update(over)
    return base


def _goal(progress, **over):
    """A goal dict + its domain.goals_core.goal_progress() result, shaped so goal_card
    can switch on `progress.shape` without a DB. Only `progress` and the fields the
    macro reads (id/title/end_date) matter here."""
    base = dict(id=0, title="Sample goal", end_date=None)
    base.update(over)
    prog = dict(shape="milestone", linked=[], done=0, total=0, current=0,
                target=0, unit="", achieved=False, pct=0)
    prog.update(progress)
    base["progress"] = prog
    return base


@bp.route("/design")
def design():
    today = today_iso()
    over = "2026-07-11"   # a past date → renders as overdue

    tasks = {
        "plain": _task(id=1, title="Reply to the accountant"),
        "priority": _task(id=2, title="File Q2 GST return", priority="high", category="business"),
        "due": _task(id=3, title="Renew domain", due_date=over, category="personal"),
        "done": _task(id=4, title="Book dentist", done=True),
        "parent": _task(id=5, title="Ship the design page", sub_total=3, sub_done=1,
                        category="content",
                        subtasks=[{"id": 51, "title": "route", "done": True},
                                  {"id": 52, "title": "template", "done": False},
                                  {"id": 53, "title": "verify", "done": False}]),
    }
    cards = {
        "plain": _task(id=6, title="Draft the newsletter", category="content"),
        "pinned": _task(id=7, title="Call the bank", pinned=True, planned_on=today,
                        priority="high", category="business"),
        "stale": _task(id=8, title="Sort the garage", col="week",
                       week_since="2026-06-20", reschedule_count=3, category="personal"),
        "recur": _task(id=9, title="Weekly review", recur_rule="weekly:sun", category="personal"),
        # distinct id + subtask ids from tasks.parent — same-id twins share a
        # data-ring group and core.js would double-count the ring (2/6, not 1/3)
        "parent": _task(id=10, title="Ship the design page", sub_total=3, sub_done=1,
                        category="content",
                        subtasks=[{"id": 101, "title": "route", "done": True},
                                  {"id": 102, "title": "template", "done": False},
                                  {"id": 103, "title": "verify", "done": False}]),
    }
    notes = [
        _note(slug="a", title="Idea: batch the morning brief", tags=["idea"],
              snippet="What if the 7am brief folded in renewals due this week…"),
        _note(slug="b", title="Options trading notes", tags=["trading", "options"],
              snippet="Wheel strategy only on tickers I'd hold anyway."),
    ]

    # One goal per shape goal_progress() can derive (measure/rollup/milestone/both),
    # plus the two "finished" looks (.goal.achieved). A rollup/both goal ALWAYS has
    # linked tasks (the shape is derived from having them), so the macro's empty-linked
    # branch is unreachable in the product and isn't faked here.
    goals = [
        _goal(dict(shape="measure", current=8, target=12, unit="videos", pct=66.7),
              id=11, title="Publish 12 videos this quarter"),
        _goal(dict(shape="measure", current=5, target=5, unit="kg", pct=100),
              id=12, title="Lose 5kg"),
        _goal(dict(shape="rollup", done=1, total=3, pct=33.3,
                   linked=[{"title": "Draft the outline", "done": True},
                           {"title": "Record the walkthrough", "done": False},
                           {"title": "Cut the trailer", "done": False}]),
              id=13, title="Launch the course"),
        _goal(dict(shape="milestone"), id=14, title="Get the new passport"),
        _goal(dict(shape="milestone", achieved=True, pct=100),
              id=15, title="Hit 10k subscribers"),
        _goal(dict(shape="both", current=3, target=10, unit="clients", pct=30,
                   done=1, total=2,
                   linked=[{"title": "Write the pitch deck", "done": True},
                           {"title": "Email the shortlist", "done": False}]),
              id=16, title="Sign 10 retainer clients", end_date="2026-12-31"),
    ]

    swatches = [
        ("--bg", "page"), ("--surface", "surface"), ("--surface2", "surface2"),
        ("--surface3", "surface3"), ("--border", "border"), ("--border2", "border2"),
        ("--text", "text"), ("--muted", "muted"), ("--accent", "accent · attention"),
        ("--good", "good · done"), ("--bad", "bad · overdue"), ("--link", "link"),
        ("--cat-content", "cat content"), ("--cat-business", "cat business"),
        ("--cat-personal", "cat personal"),
    ]

    return render_template("design.html", active="", today=today,
                           tasks=tasks, cards=cards, notes=notes, goals=goals,
                           swatches=swatches)
