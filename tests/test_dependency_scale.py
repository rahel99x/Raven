"""Dependency traversals preserve action order without recursion or rescans."""
from collections import deque

from raven.deps import DepGraph
from raven.model import Job


def job(jid, dependency=""):
    return Job(str(jid), "job-" + str(jid), "main", "PENDING", dependency=dependency)


def test_ten_thousand_deep_chain_has_exact_dfs_and_action_closures():
    records = [job(1)] + [job(index, f"afterok:{index - 1}") for index in range(2, 10001)]
    graph = DepGraph(records)
    tree, = graph.trees()
    assert tree == [(0, "", "1")] + [(index - 1, "afterok", str(index)) for index in range(2, 10001)]
    assert graph.downstream("1") == [str(index) for index in range(2, 10001)]
    assert graph.upstream("10000") == [str(index) for index in range(9999, 0, -1)]


def test_wide_bfs_has_constant_time_queue_removal_and_original_neighbor_order(monkeypatch):
    from raven import deps
    records = [job(1)] + [job(index, "afterok:1") for index in range(2, 10002)]
    queues = []

    class CountedQueue(deque):
        def __init__(self, values):
            super().__init__(values)
            self.removed = 0
            queues.append(self)

        def popleft(self):
            self.removed += 1
            return super().popleft()

    monkeypatch.setattr(deps, "deque", CountedQueue)
    graph = DepGraph(records)
    assert graph.downstream("1") == [str(index) for index in range(2, 10002)]
    assert queues[-1].removed == 10001
    assert graph.upstream("10001") == ["1"] and queues[-1].removed == 2


def test_diamond_dfs_repeated_edge_and_nearest_first_closure_match_original():
    graph = DepGraph([job(1), job(2, "afterok:1"), job(3, "afterany:1"),
                      job(4, "afterok:2:3")])
    assert graph.trees() == [[(0, "", "1"), (1, "afterok", "2"), (2, "afterok", "4"),
                             (1, "afterany", "3"), (2, "afterok*", "4")]]
    assert graph.downstream("1") == ["2", "3", "4"]
    assert graph.upstream("4") == ["2", "3", "1"]


def test_multiple_roots_keep_shared_child_repeat_and_each_real_id():
    graph = DepGraph([job(1), job(2), job(3, "afterok:1:2"), job(4, "afterok:3")])
    assert graph.trees() == [[(0, "", "1"), (1, "afterok", "3"), (2, "afterok", "4")],
                             [(0, "", "2"), (1, "afterok*", "3")]]
    assert graph.downstream("2") == ["3", "4"]


def test_rootless_cycles_are_visible_once_per_node_with_bounded_repeated_edges():
    graph = DepGraph([job(1, "afterok:3"), job(2, "afterok:1"), job(3, "afterok:2")])
    assert graph.roots() == []
    assert graph.trees() == [[(0, "", "1"), (1, "afterok", "2"),
                             (2, "afterok", "3"), (3, "afterok*", "1")]]
    assert graph.downstream("1") == ["2", "3"]
    assert graph.upstream("1") == ["3", "2"]


def test_self_loop_and_disconnected_cycle_do_not_hide_valid_tree_or_other_jobs():
    graph = DepGraph([job(1), job(2, "afterok:1"), job(3, "afterok:3"),
                      job(4, "afterok:5"), job(5, "afterok:4"), job(6)])
    trees = graph.trees()
    assert trees[0] == [(0, "", "1"), (1, "afterok", "2")]
    assert trees[1] == [(0, "", "3"), (1, "afterok*", "3")]
    assert trees[2] == [(0, "", "4"), (1, "afterok", "5"), (2, "afterok*", "4")]
    assert sum(len(tree) for tree in trees) <= len(graph.related()) + len(graph.edges)
    assert graph.downstream("3") == graph.upstream("3") == []
    assert "6" not in {jid for tree in trees for _, _, jid in tree}


def test_original_small_graph_matches_known_dfs_and_singleton_action_order():
    first, second, third, fourth = job(1), job(2, "afterok:1"), job(3, "afterany:2"), job(4, "afterok:1:9")
    singleton = job(5, "singleton")
    first.name = singleton.name = "same"
    graph = DepGraph([first, second, third, fourth, singleton], {"9": "old completed"})
    assert graph.roots() == ["1", "9"]
    assert graph.downstream("1") == ["2", "4", "5", "3"]
    assert graph.upstream("3") == ["2", "1"]
    assert graph.trees()[0][:3] == [(0, "", "1"), (1, "afterok", "2"), (2, "afterany", "3")]
    assert graph.trees()[1] == [(0, "", "9"), (1, "afterok*", "4")]
