"""Errors shared by the domain modules. The web layer maps them to HTTP:
NotFound -> 404, Conflict -> 409, ValueError -> 400."""


class NotFound(LookupError):
    """No such record."""


class Conflict(ValueError):
    """The record exists but is in a state where this action does not apply."""
