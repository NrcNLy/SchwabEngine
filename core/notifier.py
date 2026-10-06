"""
core/notifier.py
================
Lightweight push notification dispatch via ntfy.sh directly into the VM-deployed
execution engine.
"""

from __future__ import annotations

import logging
import os
from typing import List, Optional, Union

import requests

logger = logging.getLogger("notifier")

DEFAULT_TOPIC = "schwab-trader-nrc-8472"


def send_alert(
    title: str,
    message: str,
    priority: str = "default",
    tags: Optional[Union[str, List[str]]] = None,
    topic: Optional[str] = None,
) -> bool:
    """
    Dispatches a push notification to ntfy.sh with safe timeouts and exception suppression.

    Args:
        title: Notification header title.
        message: Notification body content.
        priority: Priority level ("min", "low", "default", "high", "urgent").
        tags: Emojis or label tags (e.g. "rotating_light", "chart_with_upwards_trend").
        topic: ntfy topic name. Defaults to ALERT_TOPIC in env or schwab-trader-nrc-8472.

    Returns:
        bool: True if dispatched successfully with HTTP 200, False otherwise.
    """
    target_topic = topic or os.getenv("ALERT_TOPIC", DEFAULT_TOPIC).strip()
    if not target_topic:
        target_topic = DEFAULT_TOPIC

    url = f"https://ntfy.sh/{target_topic}"

    headers = {
        "Title": str(title),
        "Priority": str(priority),
    }

    if tags is not None:
        if isinstance(tags, (list, tuple, set)):
            headers["Tags"] = ",".join(str(t).strip() for t in tags if str(t).strip())
        elif isinstance(tags, str) and tags.strip():
            headers["Tags"] = tags.strip()

    try:
        session = requests.Session()
        response = session.post(
            url,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=3.0,
        )
        if response.status_code == 200:
            logger.debug("ntfy alert '%s' dispatched successfully to %s", title, target_topic)
            return True
        logger.warning(
            "ntfy alert '%s' returned status %d: %s",
            title, response.status_code, response.text[:200],
        )
        return False
    except Exception as exc:
        logger.warning("Failed to dispatch ntfy alert '%s': %s", title, exc)
        return False
