import pytest

from raven.expr import Expr, ExprError
from raven.cli import main


@pytest.mark.parametrize("expression", ["-None", "{[]: 1}", "{[]}", "[1][::0]", "[a for a, b in [(1,)]]", "[x for x in 1]"])
def test_invalid_operations_are_user_expression_errors(expression):
    with pytest.raises(ExprError):
        Expr(expression)({})


def test_unsupported_nested_unpacking_is_rejected_before_evaluation():
    with pytest.raises(ExprError, match="flat tuple"):
        Expr("[a for (a, (b, c)) in [(1, (2, 3))]]")


def test_slices_apply_the_requested_step():
    assert Expr("[1, 2, 3][::-1]")({}) == [3, 2, 1]
    assert Expr("[1, 2, 3, 4][1::2]")({}) == [2, 4]


def test_wait_for_invalid_operation_exits_cleanly(capsys):
    assert main(["--fake", "--no-state", "--no-plugins", "--wait-for=-None"]) == 1
    error = capsys.readouterr().err
    assert error.startswith("wait-for:") and "Traceback" not in error
