from hypothesis import given, strategies as st
import pytest

from raven.expr import Expr, ExprError, MAX_ITEMS, MAX_STRING, MAX_TEXT


@given(st.lists(st.integers(-100, 100), max_size=50), st.integers(-10, 10))
def test_bounded_comprehensions_preserve_arithmetic(values, increment):
    assert Expr("[x + increment for x in values if x >= 0]")({"values": values, "increment": increment}) == [x + increment for x in values if x >= 0]


@given(st.integers(MAX_STRING + 1, MAX_STRING * 100))
def test_repetition_refused_before_allocating_result(length):
    with pytest.raises(ExprError, match="budget"):
        Expr("'x' * count")({"count": length})


@pytest.mark.parametrize("expression,namespace", [
    ("[x for x in values for y in values if False]", {"values": list(range(400))}),
    ("sum(values)", {"values": list(range(MAX_ITEMS + 1))}),
    ("'x'.replace('', replacement)", {"replacement": "x" * MAX_STRING}),
    ("'%1000000000s' % 'x'", {}),
    ("large * large", {"large": 2 ** 4095}),
    ("str(values)", {"values": ["x" * 10000] * 1000}),
])
def test_expensive_operations_fail_as_user_errors(expression, namespace):
    with pytest.raises(ExprError):
        Expr(expression)(namespace)


def test_compile_budget_is_bounded_and_next_evaluation_recovers():
    with pytest.raises(ExprError, match="budget"):
        Expr(" " * MAX_TEXT + "1")
    expression = Expr("[x for x in values]")
    with pytest.raises(ExprError, match="budget"):
        expression({"values": range(1000000)})
    assert expression({"values": [1, 2]}) == [1, 2]


def test_unknown_unbounded_native_iterator_is_refused():
    import itertools
    with pytest.raises(ExprError, match="bounded"):
        Expr("sum(values)")({"values": itertools.repeat(1)})
