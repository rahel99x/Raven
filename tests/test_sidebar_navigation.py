"""Navigation actions, persistence and gestures through published rail geometry."""

from types import SimpleNamespace

import pytest

from raven import layout as L, sidebar as S
from raven.ui.destinations import children, current, destinations, valid_favorites


def app(width=80, height=30, pins=()):
    result = SimpleNamespace(
        width=width,
        height=height,
        mode="main",
        tab="jobs",
        research_view="experiment",
        analytics_view="job",
        nodes_view="mine",
        toolbar_state={"favorites": list(pins)},
        commands=[],
        messages=[],
        saves=[],
        interaction_state={},
    )
    result.run_command = result.commands.append
    result.say = result.messages.append
    result.save = lambda: result.saves.append(
        (list(result.toolbar_state["favorites"]), S.save(result))
    )
    S.initialize(result)
    return result


def paint(target, ascii_=False):
    views = SimpleNamespace(g=L.Glyphs(ascii_))
    rows = S.render(views, target, target.width, target.height)
    overlays = S.overlay(views, {}, target, target.width, target.height)
    return rows, overlays


def hit(target, identity, kind=None):
    return next(
        item
        for item in target.sidebar_state["hits"]
        if item[4] == identity and (kind is None or item[3] == kind)
    )


def click(target, item, button="left"):
    return S.handle_mouse(target, item[0], item[1], button)


@pytest.mark.parametrize(
    "width,height",
    [(1, 1), (2, 3), (7, 4), (24, 8), (40, 16), (60, 24), (80, 30), (160, 48)],
)
@pytest.mark.parametrize("ascii_", [False, True])
def test_geometry_is_bounded_and_ascii_stays_ascii(width, height, ascii_):
    target = app(width, height, ("research:experiment", "analytics:timeline"))
    target.sidebar_state["expanded"] = ["research", "analytics", "nodes", "traces"]
    rows, _ = paint(target, ascii_)
    rail = S.width(target, width)
    assert rail < width
    assert all(
        1 <= y < height - 1 and x == 0 and L.vlen(L.row_text(row)) == rail
        for y, x, row in rows
    )
    assert all(0 <= item[1] < item[2] <= rail for item in target.sidebar_state["hits"])
    if ascii_:
        assert all(L.row_text(row).isascii() for _, _, row in rows)
    controls = S.controls(target)
    assert len({item.id for item in controls}) == len(controls)
    assert all(item.rect.right <= rail for item in controls)


def test_every_parent_remains_in_full_list_when_favorited():
    target = app(pins=("tab:jobs",))
    paint(target)
    assert hit(target, "tab:jobs", "favorite")
    assert hit(target, "tab:jobs", "destination")
    assert {
        identity
        for kind, identity, _, _ in target.sidebar_state["rows"]
        if kind == "destination"
    } == {key for key in destinations() if key.startswith("tab:")}


@pytest.mark.parametrize("parent", ["research", "analytics", "nodes", "traces"])
def test_arrow_discloses_child_views_without_navigation(parent):
    target = app(height=64)
    paint(target)
    click(target, hit(target, "tab:" + parent, "expand"))
    assert not target.commands
    assert parent in S.save(target)["expanded"]
    rows, _ = paint(target)
    assert all(hit(target, identity, "child") for identity, _ in children(parent))
    first = children(parent)[0][0]
    click(target, hit(target, first, "child"))
    assert target.commands == list(destinations()[first][1])
    assert target.mode == "main"
    assert len(target.saves) == 1
    assert any("▾" in L.row_text(row) for _, _, row in rows)


def test_right_click_pins_pointed_destination_not_current_view():
    target = app()
    paint(target)
    click(target, hit(target, "tab:history"), "right")
    assert target.mode == "sidebar_menu" and not target.commands
    _, overlays = paint(target)
    assert "Add favorite" in L.row_text(overlays[0][2])
    y, left, _, _ = target.sidebar_state["menu_hits"][0]
    assert S.handle_mouse(target, y, left, "press")
    assert not target.toolbar_state["favorites"]
    assert S.handle_mouse(target, y, left, "release")
    assert target.toolbar_state["favorites"] == ["tab:history"]
    assert target.mode == "main" and len(target.saves) == 1
    paint(target)
    # Some terminals emit an aggregate click after the press/release pair.
    S.handle_mouse(target, y, left, "left")
    assert not target.commands
    assert len(target.saves) == 1


def test_context_close_button_and_keyboard_close_do_not_toggle_favorite():
    target = app(pins=("tab:history",))
    paint(target)
    click(target, hit(target, "tab:history", "favorite"), "right")
    paint(target)
    y, left, _, _ = target.sidebar_state["menu_hits"][1]
    S.handle_mouse(target, y, left)
    assert target.toolbar_state["favorites"] == ["tab:history"] and not target.saves
    paint(target)
    click(target, hit(target, "tab:history", "favorite"), "right")
    S.handle_key(target, "down")
    S.handle_key(target, "enter")
    assert target.mode == "main" and not target.saves


def test_alpha_sort_is_presentation_only_and_custom_order_survives_roundtrip():
    pins = ["tab:research", "tab:jobs", "tab:analytics"]
    target = app(pins=pins)
    paint(target)
    click(target, hit(target, "", "order"))
    paint(target)
    shown = [row[1] for row in target.sidebar_state["rows"] if row[0] == "favorite"]
    assert shown == ["tab:analytics", "tab:jobs", "tab:research"]
    assert target.toolbar_state["favorites"] == pins
    restored = app(pins=target.toolbar_state["favorites"])
    S.restore(restored, S.save(target))
    paint(restored)
    assert S.save(restored)["favorite_order"] == "alpha"
    click(restored, hit(restored, "", "order"))
    paint(restored)
    assert [
        row[1] for row in restored.sidebar_state["rows"] if row[0] == "favorite"
    ] == pins


@pytest.mark.parametrize("alpha", [False, True])
def test_drag_reorders_after_release_only_and_automatically_persists(alpha):
    target = app(pins=("tab:research", "tab:jobs", "tab:analytics"))
    if alpha:
        target.sidebar_state["order"] = "alpha"
    paint(target)
    displayed = [row[1] for row in target.sidebar_state["rows"] if row[0] == "favorite"]
    start, end = (
        hit(target, displayed[0], "favorite"),
        hit(target, displayed[-1], "favorite"),
    )
    click(target, start, "press")
    click(target, end, "drag")
    assert not target.saves and not target.commands
    click(target, end, "release")
    assert target.toolbar_state["favorites"] == displayed[1:] + displayed[:1]
    assert S.save(target)["favorite_order"] == "custom"
    assert len(target.saves) == 1 and not target.commands


@pytest.mark.parametrize("release", ["later-favorite", "outside"])
def test_drag_release_coordinate_overrides_prior_hover_and_outside_cancels(release):
    target = app(pins=("tab:jobs", "tab:history", "tab:analytics"))
    paint(target)
    start = hit(target, "tab:jobs", "favorite")
    hovered = hit(target, "tab:history", "favorite")
    click(target, start, "press")
    click(target, hovered, "drag")
    if release == "later-favorite":
        click(target, hit(target, "tab:analytics", "favorite"), "release")
        assert target.toolbar_state["favorites"] == [
            "tab:history",
            "tab:analytics",
            "tab:jobs",
        ]
        assert len(target.saves) == 1
    else:
        S.handle_mouse(target, hovered[0], target.width - 1, "release")
        assert target.toolbar_state["favorites"] == [
            "tab:jobs",
            "tab:history",
            "tab:analytics",
        ]
        assert not target.saves
    assert not target.commands


def test_autoscroll_drop_uses_repainted_row_without_an_extra_motion_report():
    pins = list(destinations())[:12]
    target = app(height=8, pins=pins)
    paint(target)
    click(target, hit(target, pins[0], "favorite"), "press")
    S.handle_mouse(target, target.height - 2, 3, "drag")
    paint(target)
    final = next(
        item
        for item in target.sidebar_state["hits"]
        if item[0] == target.height - 2 and item[3] == "favorite"
    )
    assert final[4] == pins[5]
    click(target, final, "release")
    assert target.toolbar_state["favorites"] == pins[1:6] + pins[:1] + pins[6:]
    assert len(target.saves) == 1


@pytest.mark.parametrize("change", ["resize", "tab", "favorites", "toolbar", "mode"])
def test_stale_drag_or_press_never_navigates_or_commits(change):
    target = app(pins=("tab:jobs", "tab:history"))
    paint(target)
    start, end = (
        hit(target, "tab:jobs", "favorite"),
        hit(target, "tab:history", "favorite"),
    )
    click(target, start, "press")
    if change == "resize":
        target.width += 1
    elif change == "tab":
        target.tab = "history"
    elif change == "favorites":
        target.toolbar_state["favorites"].reverse()
    elif change == "toolbar":
        target.toolbar_state["menu"] = 0
    else:
        target.mode = "confirm"
    before = list(target.toolbar_state["favorites"])
    click(target, end, "drag")
    click(target, end, "release")
    assert target.toolbar_state["favorites"] == before
    assert not target.saves and not target.commands


def test_wheel_and_visible_scroll_arrows_reach_all_destinations_in_short_terminal():
    target = app(height=8)
    paint(target)
    assert hit(target, "", "scroll-down")
    click(target, hit(target, "", "scroll-down"))
    paint(target)
    assert target.sidebar_state["top"] > 0
    for _ in range(10):
        S.handle_mouse(target, 3, 3, "wheel-down")
        paint(target)
    assert hit(target, "tab:traces", "destination")
    assert hit(target, "", "scroll-up")
    assert not target.saves and not target.commands


def test_external_navigation_reveals_parent_without_overriding_later_wheel_scroll():
    target = app(width=40, height=12)
    paint(target)
    target.tab = "research"
    paint(target)
    assert hit(target, "tab:research", "destination")
    revealed_top = target.sidebar_state["top"]
    assert revealed_top > 0
    S.handle_mouse(target, 3, 3, "wheel-up")
    paint(target)
    assert target.sidebar_state["top"] < revealed_top
    scrolled_top = target.sidebar_state["top"]
    paint(target)
    assert target.sidebar_state["top"] == scrolled_top
    target.sidebar_state["expanded"] = ["research"]
    target.research_view = "operations"
    paint(target)
    assert hit(target, "research:operations", "child")


@pytest.mark.parametrize(
    "tab,leaf",
    [
        ("research", "research:experiment"),
        ("analytics", "analytics:job"),
        ("nodes", "nodes:mine"),
        ("traces", "trace:live"),
    ],
)
def test_active_parent_and_expanded_leaf_both_have_readable_selection(tab, leaf):
    target = app(height=64)
    target.tab = tab
    target.trace_panel_state = {"workspace_view": {"mode": "live"}}
    rows, _ = paint(target)
    parent_y = hit(target, "tab:" + tab, "destination")[0]
    parent = next(row for y, _, row in rows if y == parent_y)
    assert any("accent" in style and "bold" in style for _, style in parent)
    target.sidebar_state["expanded"] = [tab]
    rows, _ = paint(target)
    leaf_y = hit(target, leaf, "child")[0]
    selected = next(row for y, _, row in rows if y == leaf_y)
    assert any("sel" in style.split("+") for _, style in selected)


def test_sidebar_yields_to_modal_overlapping_rail_and_rejects_internal_activation():
    target = app()
    paint(target)
    target.mode = "confirm"
    paint(target)
    for button in ("press", "left", "right", "release", "motion"):
        assert not S.handle_mouse(target, 4, 3, button)
    assert not S.controls(target)
    revision = target.sidebar_state["revision"]
    S.run_command(target, ["sidebar", "choose", str(revision), "1"])
    assert not target.commands and target.mode == "confirm"


def test_drag_autoscroll_reaches_a_favorite_outside_initial_viewport():
    pins = list(destinations())[:12]
    target = app(height=8, pins=pins)
    paint(target)
    start = hit(target, pins[0], "favorite")
    click(target, start, "press")
    for _ in range(3):
        S.handle_mouse(target, target.height - 2, 3, "drag")
        paint(target)
    assert target.sidebar_state["top"] == 3
    destination = next(
        item
        for item in target.sidebar_state["hits"]
        if item[3] == "favorite" and item[0] == 4
    )
    target_identity = destination[4]
    S.handle_mouse(target, destination[0], destination[1], "drag")
    S.handle_mouse(target, destination[0], destination[1], "release")
    expected = list(pins[1:])
    expected.insert(pins.index(target_identity), pins[0])
    assert target.toolbar_state["favorites"] == expected
    assert len(target.saves) == 1 and not target.commands


def test_keyboard_path_expands_pins_orders_and_opens_without_mouse():
    target = app(height=12)
    S.run_command(target, ["sidebar", "focus"])
    assert target.mode == "sidebar"
    S.handle_key(target, "end")
    paint(target)
    assert hit(target, "tab:traces")
    S.handle_key(target, "right")
    S.handle_key(target, "down")
    S.handle_key(target, "f")
    assert target.toolbar_state["favorites"] == ["trace:live"]
    S.handle_key(target, "o")
    assert S.save(target)["favorite_order"] == "alpha"
    S.handle_key(target, "enter")
    assert target.commands == ["traces", "trace workspace live"]
    assert target.mode == "main"


def test_public_controls_use_exact_painted_identities_and_old_actions_are_inert():
    target = app()
    paint(target)
    control = next(
        item for item in S.controls(target) if item.id == "sidebar:destination:tab:jobs"
    )
    old_action = control.action[1].split()
    target.width = 40
    paint(target)
    S.run_command(target, old_action)
    assert not target.commands
    control = next(
        item
        for item in S.controls(target)
        if item.id == "sidebar:destination:tab:history"
    )
    S.run_command(target, control.action[1].split())
    assert target.commands == ["tab history"]


def test_render_hover_controls_and_browsing_never_save_or_collect():
    target = app()
    target.store = SimpleNamespace(
        snapshot=lambda: pytest.fail("sidebar read scheduler state")
    )
    for _ in range(3):
        paint(target)
        for item in target.sidebar_state["hits"]:
            click(target, item, "motion")
        assert S.controls(target)
    assert not target.saves and not target.commands
    target.toolbar_state["menu"] = 0
    assert not S.handle_mouse(target, 4, 2)
    assert not S.handle_key(target, "down")
    assert not S.controls(target)


@pytest.mark.parametrize(
    "value",
    [
        None,
        [],
        "research",
        {"expanded": "research"},
        {
            "expanded": [None, {}, "research", "research", "bad"],
            "favorite_order": "alpha",
        },
    ],
)
def test_restore_rejects_unbounded_or_unknown_preferences(value):
    target = app()
    S.restore(target, value)
    saved = S.save(target)
    assert saved["favorite_order"] in ("custom", "alpha")
    assert all(
        key in ("research", "analytics", "nodes", "traces") for key in saved["expanded"]
    )
    assert len(saved["expanded"]) == len(set(saved["expanded"]))


def test_shared_destinations_keep_legacy_favorites_and_full_navigation_identity():
    assert valid_favorites(
        ["research:experiment", "tab:jobs", "invalid", "tab:jobs"]
    ) == ["research:experiment", "tab:jobs"]
    target = app()
    target.tab, target.nodes_view = "nodes", "map"
    assert current(target) == "nodes:map"
    assert destinations()[current(target)][1] == ("tab nodes", "view map")
    target.tab = "traces"
    target.trace_panel_state = {"workspace_view": {"mode": "live"}}
    assert current(target) == "trace:live"
    target.trace_panel_state["workspace_view"]["mode"] = "explore"
    assert current(target) == "tab:traces"  # Panning is not necessarily Fit.
