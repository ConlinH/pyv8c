"""Asyncio Browser / Profile / Page API on the caller's thread.

Navigation, input, network and cleanup are awaited. JavaScript evaluation and
DOM queries are immediate and complete their microtask checkpoint before return.
"""

from .foundation.event_loop import run
from .platform.browser import Browser, Profile
from .platform.page import Page

__all__ = ("Browser", "Profile", "Page", "run")
