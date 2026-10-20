"""Immutable controls, sparse hit geometry and geometric navigation.

No handler, feature renderer or application composition root is imported here.
"""

from __future__ import annotations
from dataclasses import dataclass, field
from functools import lru_cache
from types import MappingProxyType
from itertools import islice
from .geometry import Rect as Rect
from .. import layout as L

MAX_CONTROLS = 4096

MAX_OVERLAY_ROWS = 4096

MAX_HIT_ITEMS = MAX_CONTROLS * 64

POINTER_STYLE = "bg:surface-raised+under+bold"

FOCUS_STYLE = "bg:track+under+bold"

ROW_KINDS = frozenset(
    (
        "job",
        "recent",
        "fin",
        "source",
        "group",
        "dep",
        "log_file",
        "log_group",
        "research_array",
        "research_evidence",
        "research_metric",
        "partition_row",
        "node_row",
        "user_drill",
    )
)

BOUNDED_KINDS = frozenset(
    (
        "node_cell",
        "job_panel_tab",
        "job_panel_file",
        "job_panel_view",
        "job_panel_action",
    )
)


@dataclass(frozen=True)
class Control:
    """Immutable semantic control; action arguments contain only scalar data."""

    id: str
    label: str
    rect: Rect
    action: tuple
    group: str = "buttons"
    enabled: bool = True
    reason: str = ""
    button: bool = True
    layer: int = 0


@dataclass(frozen=True)
class _SpanIndex:
    """Sparse vertical interval index for rails and other tall controls.

    Each control is stored once. Looking up a terminal row visits one branch,
    rather than testing the tall controls from every other visible pane.
    """

    center: int
    entries: tuple
    before: object = None
    after: object = None

    @classmethod
    def build(cls, entries):
        if not entries:
            return None
        centers = sorted(
            (entry[1].rect.top + entry[1].rect.bottom - 1) // 2 for entry in entries
        )
        center = centers[len(centers) // 2]
        before, crossing, after = [], [], []
        for entry in entries:
            rect = entry[1].rect
            (
                before
                if rect.bottom <= center
                else after
                if rect.top > center
                else crossing
            ).append(entry)
        return cls(
            center,
            tuple(sorted(crossing, key=lambda entry: entry[0], reverse=True)),
            cls.build(before),
            cls.build(after),
        )

    def at(self, y, x):
        best = next(
            (entry for entry in self.entries if entry[1].rect.contains(y, x)), None
        )
        branch = (
            self.before if y < self.center else self.after if y > self.center else None
        )
        candidate = branch.at(y, x) if branch is not None else None
        return (
            candidate
            if candidate and (best is None or candidate[0] > best[0])
            else best
        )


@dataclass(frozen=True)
class Graph:
    controls: tuple[Control, ...]
    token: tuple
    width: int
    height: int
    generation: int
    observed_geometry: tuple = ()
    regions: tuple = ()
    scope: tuple = ()
    _identities: object = field(init=False, repr=False, compare=False)
    _rows: object = field(init=False, repr=False, compare=False)
    _spanning: tuple = field(init=False, repr=False, compare=False)
    _span_index: object = field(init=False, repr=False, compare=False)
    _regions: object = field(init=False, repr=False, compare=False)
    _ordered: tuple = field(init=False, repr=False, compare=False)
    _order_indices: object = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        identities, rows, spanning, regions = {}, {}, [], {}
        for index, control in enumerate(self.controls):
            identities.setdefault(control.id, control)
            rect = control.rect
            priority = (
                control.layer,
                control.button,
                -((rect.right - rect.left) * (rect.bottom - rect.top)),
                -index,
            )
            entry = (priority, control)
            if rect.bottom == rect.top + 1:
                rows.setdefault(rect.top, []).append(entry)
            else:
                # Multi-row controls do not multiply storage by screen height.
                # Native buttons and table rows take the constant-time row path.
                spanning.append(entry)
            if control.group == "pane-dividers":
                region = "divider"
            elif control.group.startswith("toolbar") or control.group == "tabs":
                region = "global:" + control.group
            else:
                region = next(
                    (
                        name
                        for name, bounds in self.regions
                        if bounds.top <= rect.top
                        and rect.bottom <= bounds.bottom
                        and bounds.left <= rect.left
                        and rect.right <= bounds.right
                    ),
                    "",
                )
            regions[control.id] = region
        object.__setattr__(self, "_identities", MappingProxyType(identities))
        object.__setattr__(
            self,
            "_rows",
            MappingProxyType(
                {
                    y: tuple(sorted(entries, key=lambda entry: entry[0], reverse=True))
                    for y, entries in rows.items()
                }
            ),
        )
        object.__setattr__(
            self,
            "_spanning",
            tuple(sorted(spanning, key=lambda entry: entry[0], reverse=True)),
        )
        object.__setattr__(
            self,
            "_span_index",
            _SpanIndex.build(
                [
                    entry
                    for entry in spanning
                    if entry[1].rect.top < entry[1].rect.bottom
                ]
            ),
        )
        object.__setattr__(self, "_regions", MappingProxyType(regions))
        ordered = tuple(
            sorted(
                (control for control in self.controls if control.enabled),
                key=lambda control: (control.rect.top, control.rect.left, control.id),
            )
        )
        object.__setattr__(self, "_ordered", ordered)
        object.__setattr__(
            self,
            "_order_indices",
            MappingProxyType(
                {control.id: index for index, control in enumerate(ordered)}
            ),
        )

    def get(self, identity):
        try:
            return self._identities.get(identity)
        except TypeError:
            return None

    def at(self, y, x):
        # Later / smaller controls win when a selectable row also contains a button.
        local = next(
            (entry for entry in self._rows.get(y, ()) if entry[1].rect.contains(y, x)),
            None,
        )
        spanning = self._span_index.at(y, x) if self._span_index is not None else None
        if local is None:
            return spanning[1] if spanning else None
        return spanning[1] if spanning and spanning[0] > local[0] else local[1]

    def region(self, control):
        return self._regions.get(control.id, "")


def _integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _rect(value):
    if isinstance(value, Rect):
        return (
            value
            if all(
                _integer(item)
                for item in (value.top, value.left, value.bottom, value.right)
            )
            else None
        )
    if (
        isinstance(value, (tuple, list))
        and len(value) == 4
        and all(_integer(item) for item in value)
    ):
        return Rect(*value)
    return None


def _action(value):
    if not isinstance(value, (tuple, list)) or not value:
        return None
    if value[0] not in ("click", "command", "key", "set", "row", "scrollbar"):
        return None
    if any(not isinstance(item, (str, int, float, bool, type(None))) for item in value):
        return None
    return tuple(value)


def _descriptor(value, *, layer=0):
    if isinstance(value, Control):
        value = {
            "id": value.id,
            "label": value.label,
            "rect": value.rect,
            "action": value.action,
            "group": value.group,
            "enabled": value.enabled,
            "reason": value.reason,
            "button": value.button,
        }
    if not isinstance(value, dict):
        return None
    identity = value.get("id")
    rect = _rect(value.get("rect"))
    action = _action(value.get("action"))
    if (
        not isinstance(identity, str)
        or not identity
        or len(identity) > 512
        or not rect
        or not action
    ):
        return None
    return Control(
        identity,
        str(value.get("label", identity))[:512],
        rect,
        action,
        str(value.get("group", "buttons"))[:128],
        bool(value.get("enabled", not value.get("disabled", False))),
        str(value.get("reason", ""))[:512],
        bool(value.get("button", True)),
        layer,
    )


def _overlay_spans(overlays, width, height):
    spans = {}
    for item in islice(overlays or (), MAX_OVERLAY_ROWS):
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            continue
        y, x, row = item
        if not _integer(y) or not _integer(x) or not 0 <= y < height:
            continue
        try:
            rect = Rect(y, x, y + 1, x + L.vlen(L.row_text(row))).clip(width, height)
        except (TypeError, ValueError):
            continue
        if rect:
            spans.setdefault(y, []).append((rect, row))
    return spans


def _row_rect(rows, y, width, height, *, overlays=None, main_right=None):
    if not _integer(y) or not 0 <= y < height:
        return None
    if overlays and y in overlays:
        rect, row = overlays[y][-1]
        # The overlay border and its margins do not activate list entries.
        return Rect(y, rect.left + 1, y + 1, rect.right - 1).clip(width, height)
    if not 0 <= y < len(rows):
        return None
    text = L.row_text(rows[y])
    length = L.vlen(text)
    right = min(width, length, width if main_right is None else main_right)
    left = min(right, length - L.vlen(text.lstrip()))
    return Rect(y, left, y + 1, right).clip(width, height)


def nearest(graph: Graph, current: Control, direction: str):
    """Find a directional neighbor in O(n), without a quadratic edge matrix.

    Same-row/column controls win. Aligned controls in the same semantic group
    then win over diagonal shortcuts into a different pane. Stable screen
    order breaks ties, including controls packed into wrapped button rows.
    """
    if direction not in ("left", "right", "up", "down"):
        return None
    vertical = direction in ("up", "down")
    sign = -1 if direction in ("left", "up") else 1
    cy, cx = current.rect.center
    region = graph.region(current)
    best = None
    # Table rows span every column. A header to the right of their center is
    # still in the same pane; it must not steal a requested pane crossing.
    row_crossing = (
        not current.button or current.group == "job-history" and ":job:" in current.id
    ) and direction in ("left", "right")
    pane_candidates = []
    # At the left edge of Details, return to the exact selected table row.
    # Otherwise a short header fragment near the top could win over Recents.
    if direction == "left" and region == "page:details":
        siblings = [
            control
            for control in graph.controls
            if control.enabled
            and graph.region(control) == region
            and control.rect.top < current.rect.bottom
            and current.rect.top < control.rect.bottom
            and control.rect.center[1] < cx
        ]
        if not siblings:
            pane_candidates = [
                control
                for control in graph.controls
                if control.enabled
                and not control.button
                and graph.region(control) == "page:main"
            ]
            if pane_candidates:
                selected = graph.token[2] if len(graph.token) > 2 else None
                return next(
                    (
                        control
                        for control in pane_candidates
                        if control.label == selected
                    ),
                    min(
                        pane_candidates,
                        key=lambda control: (
                            abs(control.rect.center[0] - cy),
                            control.rect.top,
                            control.rect.left,
                            control.id,
                        ),
                    ),
                )
    if row_crossing and region:
        bounds = dict(reversed(graph.regions))
        wanted = (
            "page:details"
            if region == "page:main"
            and direction == "right"
            and "page:details" in bounds
            else "page:main"
            if region == "page:details" and direction == "left"
            else None
        )
        if (
            wanted is None
            and region in ("page:main", "page:history")
            and "page:history" in bounds
        ):
            other = "page:history" if region == "page:main" else "page:main"
            delta = bounds[other].center[1] - bounds[region].center[1]
            if (
                delta * sign > 0
                or delta == 0
                and region == "page:history"
                and direction == "left"
            ):
                wanted = other
        if wanted:
            pane_candidates = [
                control
                for control in graph.controls
                if control.enabled and graph.region(control) == wanted
            ]
    if pane_candidates:
        if region == "page:main" and direction == "right" and len(graph.token) > 10:
            active_tab = graph.get("job_panel_tab:" + str(graph.token[10]))
            if active_tab in pane_candidates:
                return active_tab
        return min(
            pane_candidates,
            key=lambda control: (
                abs(control.rect.center[0] - cy),
                abs(control.rect.center[1] - cx),
                control.rect.top,
                control.rect.left,
                control.id,
            ),
        )
    for candidate in graph.controls:
        if candidate.id == current.id or not candidate.enabled:
            continue
        ny, nx = candidate.rect.center
        forward = (ny - cy if vertical else nx - cx) * sign
        if forward <= 0:
            continue
        orthogonal = abs(nx - cx if vertical else ny - cy)
        overlap = (
            current.rect.left < candidate.rect.right
            and candidate.rect.left < current.rect.right
            if vertical
            else current.rect.top < candidate.rect.bottom
            and candidate.rect.top < current.rect.bottom
        )
        # A row's own cells are a single semantic target, not independent
        # column controls. Only inline controls on the same painted row apply.
        if row_crossing and graph.region(candidate) == region and not overlap:
            continue
        same_group = candidate.group == current.group
        same_region = bool(region and graph.region(candidate) == region)
        rank = (
            0 if same_region else 1 if region else 0,
            0 if overlap else 1,
            0 if same_group else 1,
            forward + 2 * orthogonal,
            orthogonal,
            forward,
            candidate.rect.top,
            candidate.rect.left,
            candidate.id,
        )
        if best is None or rank < best[0]:
            best = rank, candidate
    return best[1] if best else None


@lru_cache(maxsize=512)
def _feedback_style(style, feedback):
    # Native cursor selection retains its semantic text and background. Hover
    # and keyboard focus add accents without making the selected job resemble
    # an ordinary hovered row. Explicit yank ranges are protected by decorate.
    source = style.split("+")
    if "sel" in source:
        accents = [token for token in feedback.split("+") if token in ("under", "bold")]
        return "+".join(dict.fromkeys(source + accents))
    # Other controls keep their semantic foreground while replacing their
    # ordinary background and removing dim for readable feedback in each theme.
    tokens = [
        token
        for token in style.split("+")
        if token not in ("dim", "sel", "rev", "under", "bold")
        and not token.startswith("bg:")
    ]
    return "+".join(tokens + feedback.split("+"))


def _decorate_row(row, ranges):
    if all(text.isascii() for text, _ in row):
        output, position = [], 0
        for text, style in row:
            end = position + len(text)
            edges = sorted(
                {position, end}
                | {
                    max(position, min(end, edge))
                    for left, right, _ in ranges
                    for edge in (left, right)
                }
            )
            for left, right in zip(edges, edges[1:]):
                feedback = next(
                    (
                        feedback
                        for start, stop, feedback in reversed(ranges)
                        if left < stop and right > start
                    ),
                    None,
                )
                painted_style = _feedback_style(style, feedback) if feedback else style
                fragment = text[left - position : right - position]
                if output and output[-1][1] == painted_style:
                    output[-1] = (output[-1][0] + fragment, painted_style)
                else:
                    output.append((fragment, painted_style))
            position = end
        return output
    output, position, previous_style, pieces = [], 0, "", []
    for text, style in row:
        for char in text:
            size = L.vlen(char)
            # A wide character intersecting a region receives one whole style;
            # combining marks inherit its base. Never split a grapheme's cells.
            feedback = (
                next(
                    (
                        feedback
                        for left, right, feedback in reversed(ranges)
                        if position < right and position + size > left
                    ),
                    None,
                )
                if size
                else None
            )
            new_style = (
                _feedback_style(style, feedback)
                if feedback
                else previous_style
                if not size and (pieces or output)
                else style
            )
            if pieces and previous_style != new_style:
                output.append(("".join(pieces), previous_style))
                pieces = []
            pieces.append(char)
            previous_style = new_style
            position += size
    if pieces:
        output.append(("".join(pieces), previous_style))
    return output
