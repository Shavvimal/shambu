"""Strict JSON in for `shambu cad`: every spec, record, fits, expectation and plate file is read through `loads`.

JSON has no NaN or Infinity, but Python's reader takes `NaN`, `Infinity` and `-Infinity`, and reads a number such as
`1e400` as infinity. A non-finite number in an input would slip past every range check (`nan < x` is always False)
and come back out as invalid JSON, so `loads` refuses it with a ValueError that names the token.

What it does not do: no schema checks (spec.py, fits.py and the verbs do those), no file reading.
"""

from __future__ import annotations

import json
import math


def _constant(token: str) -> float:
    raise ValueError(f"{token} is not a JSON number; give a finite number")


def _number(token: str) -> float:
    v = float(token)
    if not math.isfinite(v):
        raise ValueError(f"{token} is out of range; give a finite number")
    return v


def loads(text: str | bytes) -> object:
    """`json.loads` that refuses NaN, Infinity and numbers that overflow to infinity (ValueError)."""
    return json.loads(text, parse_constant=_constant, parse_float=_number)
