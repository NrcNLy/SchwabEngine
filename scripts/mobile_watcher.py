"""
scripts/mobile_watcher.py
=========================
Resilient, headless background monitoring daemon that periodically polls
the Schwab Engine FastAPI backend status and sends real-time push notifications
to Android devices via ntfy.sh when engine states transition (e.g. AUTH_LOCKED -> ACTIVE).
"""

import logging
import time
import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
STATUS_ENDPOINT = "http://127.0.0.1:8080/api/status?env=sandbox"
NTFY_TOPIC_URL = "https://ntfy.sh/schwab-trader-nrc-8472"
POLL_INTERVAL_SECONDS = 60
REQUEST_TIMEOUT_SECONDS = 10

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filename=r"C:\Projects\schwab_engine\mobile_watcher.log",
)
logger = logging.getLogger("MobileWatcher")


def send_push_notification(title: str, message: str, priority: str = "high") -> None:
    """Dispatches a push notification via the public ntfy.sh publish endpoint."""
    try:
        headers = {
            "Title": title,
            "Priority": priority,
            "Tags": "chart_with_upwards_trend,rotating_light",
        }
        resp = requests.post(
            NTFY_TOPIC_URL,
            data=message.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
        if resp.status_code == 200:
            logger.info("Push notification delivered to %s", NTFY_TOPIC_URL)
        else:
            logger.warning("Failed to publish push alert, HTTP %d: %s", resp.status_code, resp.text)
    except Exception as exc:
        logger.warning("Network failure dispatching ntfy notification: %s", exc)


def main() -> None:
    logger.info("Starting MobileWatcher daemon...")
    logger.info("Polling target: %s every %ds", STATUS_ENDPOINT, POLL_INTERVAL_SECONDS)
    logger.info("Alert channel: %s", NTFY_TOPIC_URL)

    previous_state = None

    while True:
        try:
            resp = requests.get(STATUS_ENDPOINT, timeout=REQUEST_TIMEOUT_SECONDS)
            if resp.status_code == 200:
                payload = resp.json()
                current_state = payload.get("system_state") or payload.get("state")

                # Detect transition from AUTH_LOCKED -> OK
                if previous_state == "AUTH_LOCKED" and current_state == "OK":
                    logger.critical("Circuit recovery detected: AUTH_LOCKED -> ACTIVE")
                    send_push_notification(
                        title="Circuit Breaker Auto-Recovered",
                        message="Schwab Engine Reactivated - Auth Circuit Closed!",
                        priority="urgent",
                    )
                elif previous_state is not None and previous_state != current_state:
                    logger.info("Engine status changed: %s -> %s", previous_state, current_state)

                if current_state:
                    previous_state = current_state

        except (requests.ConnectionError, requests.Timeout) as net_err:
            # Silently handle tunnel drop or service restart without exiting
            logger.info("Backend unreachable (tunnel down or rebooting): %s", net_err)
        except Exception as exc:
            logger.warning("Unexpected poll exception: %s", exc)

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
