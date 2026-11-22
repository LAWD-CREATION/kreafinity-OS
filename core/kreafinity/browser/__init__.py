"""The assistant's own browser: a separate window it can read, point at and use.

It has its own profile (no saved passwords, history or sessions of the user)
and is driven over WebDriver BiDi (Firefox) or the Chrome DevTools Protocol
(Chromium, Chrome), on 127.0.0.1 only.
"""

from kreafinity.browser.protocol import KEYS, BrowserClosedError, BrowserError
from kreafinity.browser.session import BrowserSession, PageState, get_session, shutdown

__all__ = [
    "KEYS",
    "BrowserClosedError",
    "BrowserError",
    "BrowserSession",
    "PageState",
    "get_session",
    "shutdown",
]
