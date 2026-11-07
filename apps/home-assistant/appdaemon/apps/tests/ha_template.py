"""A minimal Home Assistant template renderer for the HA configuration tests and the simulator.

Renders the real `value_template` conditions of the HA packages with Jinja2 and stand-ins for the
HA template functions they use (states, state_attr, is_number, float, now, timedelta, as_timestamp,
this). It is not HA: only what the comfort-controller packages need, with HA's semantics.
"""

from datetime import datetime, timedelta
from types import SimpleNamespace

import jinja2

_MISSING = object()


class TemplateError(Exception):
    pass


def _is_number(value):
    if isinstance(value, bool):
        return False
    try:
        number = float(value)
    except (TypeError, ValueError):
        return False
    return number == number and number not in (float("inf"), float("-inf"))


def _float(value, default=_MISSING):
    # HA's float filter: raises (a template error) on a non-number without a default.
    try:
        return float(value)
    except (TypeError, ValueError):
        if default is _MISSING:
            raise TemplateError(f"float got invalid input {value!r}")
        return default


def _as_timestamp(value, default=_MISSING):
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            pass
    if default is _MISSING:
        raise TemplateError(f"as_timestamp got invalid input {value!r}")
    return default


class _Domain:
    def __init__(self, domain, store):
        self._domain, self._store = domain, store

    def __getattr__(self, name):
        item = self._store.get(f"{self._domain}.{name}")
        if item is None:
            return None
        return SimpleNamespace(**item)


class _States:
    def __init__(self, store):
        self._store = store

    def __call__(self, entity_id):
        item = self._store.get(entity_id)
        return "unknown" if item is None else item["state"]

    def __getattr__(self, domain):
        return _Domain(domain, self._store)


_ENV = jinja2.Environment(undefined=jinja2.StrictUndefined)
_ENV.filters["float"] = _float
_ENV.filters["int"] = lambda value, default=_MISSING: int(_float(value, default))
_COMPILED = {}


def render(template, store, now, this=None, variables=None):
    """Render `template` against `store` ({entity_id: {state, attributes, last_updated, ...}}).

    `last_updated` / `last_changed` must be aware datetimes. Returns the rendered string.
    """
    if template not in _COMPILED:
        _COMPILED[template] = _ENV.from_string(template)
    states = _States(store)

    def state_attr(entity_id, name):
        item = store.get(entity_id)
        return None if item is None else item.get("attributes", {}).get(name)

    context = {
        "states": states,
        "state_attr": state_attr,
        "is_number": _is_number,
        "now": lambda: now,
        "timedelta": timedelta,
        "as_timestamp": _as_timestamp,
        "this": SimpleNamespace(**(this or {"last_changed": now, "attributes": SimpleNamespace(last_triggered=None)})),
        **(variables or {}),
    }
    return _COMPILED[template].render(**context).strip()


def condition_true(template, store, now, **kwargs):
    """A template condition: True when the rendered result is "True" (HA: a template error is False)."""
    try:
        return render(template, store, now, **kwargs) == "True"
    except TemplateError:
        return False
