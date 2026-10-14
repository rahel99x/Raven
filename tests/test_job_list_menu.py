"""List menus keep presentation independent from shared membership."""
import pytest

from raven import job_groups, job_list_controls, job_list_menu as menu, manual_job_groups
from raven.config import Config
from raven.controller import App
from raven.layout import Glyphs, row_text, vlen
from raven.model import Job, Store
from raven.views import Views


def choose(app, size, action, *, send=None):
    """Choose a visibly rendered menu row through a full pointer gesture."""
    rendered = menu.overlay(app.views_ref, app.store.snapshot(), app, *size)
    from raven.interaction import publish
    hits = getattr(app, "last_hits", [])
    publish(app, getattr(app, "last_rows", []), hits, *size, overlays=rendered)
    state = menu.initialize(app)
    index = next(i for i, item in enumerate(state["items"]) if item[0] == action)
    y, left, _, _ = next(hit for hit in state["hits"] if hit[3] == index)
    assert any(ry == y and row_text(row).strip() for ry, _, row in rendered)
    if send is None:
        app.click(y, left, hits, button="press")
        app.click(y, left, hits, button="release")
    else:
        send((y, left), "press")
        send((y, left), "release")


@pytest.fixture
def app(monkeypatch):
    cfg = Config({"animations": False, "startup_animation": False, "gpu_sampling": False,
                  "smooth_scrolling": False, "log_lines": 0})
    store = Store(persist=False)
    store.apply_jobs([Job(f"10_{i}", "train", "local", "RUNNING" if i == 0 else "PENDING") for i in range(3)])
    result = App(store, None, None, cfg, "alex", interactive=False)
    result.views_ref = Views(Glyphs(False), cfg)
    result.width, result.height = 80, 24
    monkeypatch.setattr(result, "save", lambda: None)
    yield result
    if result.research:
        result.research.close()


@pytest.mark.parametrize("context", job_list_controls.CONTEXTS)
@pytest.mark.parametrize("width", [24, 40, 80, 160])
def test_three_readable_menu_controls_are_bounded_without_fetching(app, context, width, monkeypatch):
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Controls copied jobs"))
    rows, hits = job_list_controls.controls(app, Glyphs(False), width, context)
    assert len(rows) == 1
    assert [value["id"] for _, _, value in hits] == [f"joblist:{context}:{section}" for section in ("view", "order", "groups")]
    assert all(vlen(row_text(row)) <= width for row in rows)
    assert "assignments" in hits[0][2]["label"]


@pytest.mark.parametrize("context", job_list_controls.CONTEXTS)
def test_view_changes_keep_batch_order_and_shared_assignments(app, context):
    made = manual_job_groups.create(app, app.store.snapshot(), ["10_0", "10_1"])
    job_groups.fold(app, made.group_id, True)
    app.run_command(f"joblist {context} sort-max")
    before = list(app.table_state["collapsed"])
    app.run_command(f"joblistmenu {context} view")
    choose(app, (80, 24), "sequential")
    assert job_list_controls.fingerprint(app, context) == ("sequential", "max")
    assert job_groups.registry(app).ensure(app.store.snapshot()).for_job("10_0").id == made.group_id
    assert app.table_state["collapsed"] == before
    app.run_command(f"joblistmenu {context} view")
    choose(app, (80, 24), "grouped")
    assert job_list_controls.fingerprint(app, context) == ("grouped", "max")


@pytest.mark.parametrize("context", job_list_controls.CONTEXTS)
def test_order_selection_remembers_preference_without_switching_view(app, context):
    app.run_command(f"joblist {context} sequential")
    app.run_command(f"joblistmenu {context} order")
    choose(app, (80, 24), "sort-max")
    assert job_list_controls.fingerprint(app, context) == ("sequential", "max")


def test_secondary_column_order_can_be_restored(app):
    app.run_command("joblist recent sort-max")
    app.run_command("joblistmenu recent order")
    choose(app, (80, 24), "column-order")
    assert job_list_controls.batch_mode(app, "recent") is None


def test_remove_is_separate_review_and_can_be_cancelled_or_restored(app):
    made = manual_job_groups.create(app, app.store.snapshot(), ["10_0", "10_1"])
    app.run_command("joblistmenu jobs groups")
    choose(app, (80, 24), "manage")
    choose(app, (80, 24), "remove")
    assert job_groups.registry(app).ensure(app.store.snapshot()).for_job("10_0").id == made.group_id
    assert menu.initialize(app)["items"][0][0] == "manage"
    menu.handle_key(app, "esc")
    assert job_groups.registry(app).ensure(app.store.snapshot()).for_job("10_0").id == made.group_id
    app.run_command("joblistmenu jobs groups")
    choose(app, (80, 24), "manage")
    choose(app, (80, 24), "remove")
    choose(app, (80, 24), "clear")
    assert not job_groups.registry(app).ensure(app.store.snapshot()).groups
    app.run_command("joblistmenu jobs groups")
    choose(app, (80, 24), "manage")
    choose(app, (80, 24), "restore")
    assert job_groups.registry(app).ensure(app.store.snapshot()).for_job("10_0").id == made.group_id


def test_removal_rechecks_assignment_revision_before_mutation(app):
    app.run_command("joblistmenu jobs remove")
    previous = menu.initialize(app)["token"]
    made = manual_job_groups.create(app, app.store.snapshot(), ["10_0", "10_1"])
    choose(app, (80, 24), "clear")
    assert menu.active(app) and menu.initialize(app)["token"] > previous
    assert job_groups.registry(app).ensure(app.store.snapshot()).for_job("10_0").id == made.group_id


def test_stale_painted_coordinates_cannot_act_after_resize_or_reopen(app):
    app.run_command("joblistmenu jobs view")
    menu.overlay(app.views_ref, {}, app, 80, 24)
    state = menu.initialize(app)
    y, left, _, _ = state["hits"][1]
    token = state["token"]
    menu.handle_mouse(app, y, left, "press")
    app.width = 100
    menu.handle_mouse(app, y, left, "release")
    assert menu.active(app) and job_list_controls.mode(app, "jobs") == "grouped"
    app.width = 80
    app.run_command("joblistmenu jobs groups")
    menu.overlay(app.views_ref, {}, app, 80, 24)
    app.run_command(f"joblistmenu choose {token} 1")
    assert menu.active(app) and menu.initialize(app)["section"] == "groups"


@pytest.mark.parametrize("ascii_", [False, True])
@pytest.mark.parametrize("size", [(24, 8), (80, 24), (120, 30)])
def test_keyboard_navigation_and_menu_bounds(app, ascii_, size):
    app.views_ref.g = Glyphs(ascii_)
    app.width, app.height = size
    app.run_command("joblistmenu jobs view")
    rows = menu.overlay(app.views_ref, {}, app, *size)
    assert all(0 <= y < size[1] and 0 <= x < size[0] and x + vlen(row_text(row)) <= size[0]
               for y, x, row in rows)
    menu.handle_key(app, "down")
    menu.handle_key(app, "enter")
    assert app.mode == "main" and job_list_controls.mode(app, "jobs") == "sequential"


def test_opening_and_painting_menus_never_fetches_jobs(app, monkeypatch):
    monkeypatch.setattr(app.store, "snapshot", lambda: pytest.fail("Menu fetched jobs"))
    for section in ("view", "order", "groups", "manage", "remove"):
        app.run_command("joblistmenu jobs " + section)
        for _ in range(3):
            menu.overlay(app.views_ref, {}, app, 80, 24)
            assert menu.controls(app)
        menu.close(app)


def test_f8_focus_activates_semantic_menu_choice(app):
    from raven import interaction
    app.run_command("joblistmenu jobs view")
    rendered = menu.overlay(app.views_ref, {}, app, 80, 24)
    interaction.publish(app, [], [], 80, 24, overlays=rendered)
    assert menu.handle_key(app, "f8")
    assert app.interaction_state["active"]
    assert menu.handle_key(app, "tab")
    assert menu.handle_key(app, "enter")
    assert app.mode == "main"
    assert job_list_controls.mode(app, "jobs") == "sequential"


def test_aggregate_click_after_submenu_transition_cannot_choose_new_action(app):
    app.run_command("joblistmenu jobs groups")
    menu.overlay(app.views_ref, {}, app, 80, 24)
    state = menu.initialize(app)
    y, x, _, _ = state["hits"][2]
    menu.handle_mouse(app, y, x, "press")
    menu.handle_mouse(app, y, x, "release")
    assert state["section"] == "manage"
    menu.overlay(app.views_ref, {}, app, 80, 24)
    token = state["token"]
    menu.handle_mouse(app, y, x, "left")
    menu.handle_mouse(app, y, x, "double")
    assert state["section"] == "manage" and state["token"] == token
    choose(app, (80, 24), "remove")
    assert state["section"] == "remove"
