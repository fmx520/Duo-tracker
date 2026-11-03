"""Push alerts via ntfy.sh (free; install the ntfy app and subscribe to your topic)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, server: str, topic: str):
        self.url = f"{server.rstrip('/')}/{topic}" if topic else ""

    def send(self, title: str, message: str, *, click: str = "", urgent: bool = True, tags: str = "iphone") -> bool:
        log.info("🔔 %s — %s", title, message)
        if not self.url:
            return False
        headers = {
            "Title": title.encode("utf-8"),
            "Priority": "urgent" if urgent else "default",
            "Tags": tags,
        }
        if click:
            headers["Click"] = click
        try:
            r = requests.post(self.url, data=message.encode("utf-8"), headers=headers, timeout=10)
            r.raise_for_status()
            return True
        except requests.RequestException as e:
            log.error("ntfy push failed: %s", e)
            return False
