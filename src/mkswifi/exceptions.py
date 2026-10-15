"""Exceptions raised by mkswifi."""


class MksWifiError(Exception):
    """Base error."""


class MksWifiConnectionError(MksWifiError):
    """The printer could not be reached, or did not answer in time."""
