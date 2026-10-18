"""Stateful checks drive production owners; scheduler evidence is simulated.

Allocation messages model one authenticated helper channel. They do not model
arbitrary unauthenticated scheduler replies or prove distributed liveness.
"""
from types import SimpleNamespace

from hypothesis import settings, strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule
import pytest

from raven import pointer_routing, scrollbars
from raven.config import Config
from raven.raven_allocation import ACTIVE, AllocationManager, WorkerProxy


def desktop():
    return {"site": {"kind": "desktop"}, "execution": {"desktop_execution": "explicit", "heavy_requires_allocation": False,
            "heavy_target": "local"}, "connection": {"transport": "none"},
            "resources": {"heavy": {"memory_mib": 512, "max_workers": 2, "max_tasks": 1, "wall_seconds": 30}}}


class AllocationMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.manager = AllocationManager(desktop(), state_dir="/unused-model-state")
        # Fixture begins after a reviewed request, before any grant. No external
        # process or scheduler is substituted with a claimed live qualification.
        self.manager.generation = 2
        self.manager.request_id = "a" * 32
        self.manager.worker = WorkerProxy(self.manager)
        self.manager._state.update(state="pending", generation=2, request_id=self.manager.request_id)
        self.sent = []
        self.manager._send = lambda message: self.sent.append(message) is None
        self.sequence = 0
        self.cancelled = False

    @rule(state=st.sampled_from(["pending", "starting", "heavy", "draining", "light", "reconciling"]))
    def helper_publication(self, state):
        self.sequence += 1
        message = {"op": "state", "generation": 2, "request_id": "a" * 32, "sequence": self.sequence,
                   "state": {"state": state, "mode": "heavy" if state == "heavy" else "light"}}
        assert self.manager._apply(message)
        if self.cancelled:
            assert self.manager.snapshot()["mode"] != "heavy"
        else:
            assert self.manager.worker.ready == (state == "heavy")

    @rule(stale_generation=st.sampled_from([0, 1, 3]), state=st.sampled_from(["heavy", "light"]))
    def stale_publication(self, stale_generation, state):
        before = self.manager.snapshot()
        assert not self.manager._apply({"op": "state", "generation": stale_generation, "request_id": "a" * 32,
                                       "sequence": self.sequence + 100, "state": {"state": state, "mode": "heavy"}})
        assert self.manager.snapshot() == before

    @rule()
    def stale_owner_cannot_cancel(self):
        before = list(self.sent)
        assert not self.manager.cancel(generation=1)
        assert self.sent == before

    @rule()
    def cancel(self):
        active = self.manager.snapshot()["state"] in ACTIVE
        self.manager.cancel(generation=2)
        if active:
            self.cancelled = True

    @invariant()
    def no_implicit_external_resource_action(self):
        assert all(message["op"] == "cancel" for message in self.sent)
        assert not any("job_id" in message for message in self.sent)
        if self.cancelled:
            assert not self.manager.worker.ready


TestAllocationModel = AllocationMachine.TestCase
TestAllocationModel.settings = settings(max_examples=40, stateful_step_count=35, deadline=None)


class PointerMachine(RuleBasedStateMachine):
    def __init__(self):
        super().__init__()
        self.value = 0
        self.context = 0
        self.app = SimpleNamespace(mode="main", tab="jobs", width=80, height=24, cfg=Config(),
                                   marks=set(), toolbar_state={})
        self.publish()

    def publish(self):
        scrollbars.begin_frame(self.app)
        scrollbars.register(self.app, "model", (2, 0, 22, 40), 200, 20, self.value, self.value,
                            lambda value: setattr(self, "value", value), context=self.context)
        scrollbars.publish(self.app, 80, 24)

    def gesture(self, y, x, action):
        event = pointer_routing.PointerEvent(y, x, action)
        if event.fresh:
            pointer_routing.begin_gesture(self.app)
        if not pointer_routing.route_capture(self.app, event):
            scrollbars.handle_mouse(self.app, y, x, button=action)

    @rule(y=st.integers(2, 21))
    def press(self, y):
        self.gesture(y, 39, "press")
        self.publish()

    @rule(y=st.integers(2, 21), action=st.sampled_from(["drag", "release", "press"]))
    def details_pane_never_seeks_scrollbar(self, y, action):
        before = self.value
        self.gesture(y, 50, action)
        assert self.value == before

    @rule()
    def change_context_then_late_release(self):
        self.context += 1
        self.publish()
        before = self.value
        self.gesture(21, 39, "release")
        assert self.value == before

    @rule()
    def cancel_then_late_release(self):
        pointer_routing.cancel_capture(self.app, reason="model cancellation")
        before = self.value
        self.gesture(21, 39, "release")
        assert self.value == before

    @invariant()
    def bounded_single_capture(self):
        assert 0 <= self.value <= 180
        assert sum(pointer_routing._payload(self.app, owner) is not None for owner in pointer_routing.OWNERS) <= 1


TestPointerModel = PointerMachine.TestCase
TestPointerModel.settings = settings(max_examples=30, stateful_step_count=30, deadline=None)


@pytest.mark.parametrize("generation,request_id", [(1, "a" * 32), (2, "b" * 32)])
def test_reused_number_or_old_request_never_grants_current_worker(generation, request_id):
    machine = AllocationMachine()
    assert not machine.manager._apply({"op": "state", "generation": generation, "request_id": request_id, "sequence": 1,
                                       "state": {"state": "heavy", "job_id": "42", "identity": {"job_id": "42"}}})
    assert not machine.manager.worker.ready
