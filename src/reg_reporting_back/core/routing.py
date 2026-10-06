"""Shared HTTP plumbing for the routers."""

from collections.abc import Callable
from typing import TypeVar

from fastapi import HTTPException

from .exceptions import AppError

T = TypeVar("T")


def run_operation(operation: Callable[[], T]) -> T:
    """Run a service call and translate any failure into an HTTP 400."""

    try:
        return operation()
    except AppError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc