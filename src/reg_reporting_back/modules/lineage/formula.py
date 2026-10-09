"""Safe evaluation of the simple arithmetic formulas published in metadata.

A ``transformation`` string such as ``"adjusted_local * fx_rate"`` declares how
a reported figure is derived from the columns of its source row. To reconcile a
figure against its source we evaluate exactly that expression over the values
of the row.

Only a tiny arithmetic grammar is accepted: numbers, column names and the
operators ``+ - * / **`` (with parentheses and unary sign). Anything else -- a
function call, an attribute access, a comparison, a JOIN's worth of prose --
returns ``None`` rather than being executed, so a malicious or merely verbose
metadata row can never run code or raise out of the lineage service.
"""

import ast
import operator
from typing import Any

_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Pow: operator.pow,
}

_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def _as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def evaluate_arithmetic(expression: str | None, variables: dict[str, Any]) -> float | None:
    """Evaluate ``expression`` over ``variables``, or return ``None``.

    ``None`` is returned when the expression is missing, is not a pure
    arithmetic formula, references an unknown or non-numeric column, or does not
    divide cleanly. Callers treat that as "no check available" rather than an
    error.
    """

    if not expression:
        return None

    try:
        tree = ast.parse(expression, mode="eval")
    except (SyntaxError, ValueError, TypeError):
        return None

    def _eval(node: ast.AST) -> float | None:
        if isinstance(node, ast.Expression):
            return _eval(node.body)

        if isinstance(node, ast.Constant):
            return _as_number(node.value)

        if isinstance(node, ast.Name):
            return _as_number(variables.get(node.id))

        if isinstance(node, ast.BinOp):
            operation = _BINARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError("unsupported operator")
            left = _eval(node.left)
            right = _eval(node.right)
            if left is None or right is None:
                return None
            return float(operation(left, right))

        if isinstance(node, ast.UnaryOp):
            operation = _UNARY_OPERATORS.get(type(node.op))
            if operation is None:
                raise ValueError("unsupported operator")
            operand = _eval(node.operand)
            if operand is None:
                return None
            return float(operation(operand))

        raise ValueError("unsupported expression")

    try:
        result = _eval(tree)
    except (ValueError, ZeroDivisionError, OverflowError, TypeError):
        return None

    if result is None:
        return None
    return float(result)
