import pytest
from tower.raven_qualification import cohort_report, qualify


def test_distinct_users_each_retain_separate_capped_entitlement(tmp_path):
    value = cohort_report(tmp_path, 4)
    assert value["ceilings_passed"]
    assert value["cross_user_coalescing"] is False
    assert value["maximum_emitted_per_user_per_test_window"]["controller"] <= 12
    rows = {(r["phase"], r["endpoint"]): r for r in value["windows"]}
    assert rows["startup-refresh", "controller"]["emitted"] > 12
    assert rows["startup-refresh", "controller"]["emitted"] <= 48
    assert rows["startup-refresh", "accounting"]["emitted"] <= 8
    assert rows["outage", "controller"]["emitted"] > 0
    assert rows["recovery", "controller"]["emitted"] > 0


@pytest.mark.parametrize("users,sessions", [(0, 1), (257, 1), (1, 0), (1, 5), (True, 1)])
def test_fixture_limits(users, sessions, tmp_path):
    with pytest.raises(ValueError):
        cohort_report(tmp_path, users, sessions=sessions)


def test_report_keeps_site_qualification_explicitly_untested():
    report = qualify(cohorts=(1,))
    assert report["production_qualified"] is False
    assert all(check["status"] == "passed" for check in report["checks"])
    assert "administrator-approved 200-user pilot" in report["untested"]
