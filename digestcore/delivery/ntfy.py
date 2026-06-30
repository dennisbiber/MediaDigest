"""ntfy push notifier behind the ``Notifier`` port.

This is the original ``notify_ntfy`` as a class. It is already front-end
agnostic — any runner can use it, or swap in ``NullNotifier`` to disable
out-of-band pings.
"""

from __future__ import annotations

from typing import Optional

import requests


class NtfyNotifier:
    def __init__(self, config):
        self.cfg = config

    def notify(self, subscription: dict, title: str, message: str,
               click_url: Optional[str] = None) -> None:
        topic = subscription.get("ntfy_topic", "")
        if not (self.cfg.NTFY_BASE_URL and topic):
            return
        headers = {"Title": title}
        if click_url:
            headers["Click"] = click_url
        try:
            requests.post(f"{self.cfg.NTFY_BASE_URL.rstrip('/')}/{topic}",
                          data=message.encode("utf-8"), headers=headers, timeout=15)
        except requests.RequestException:
            pass
