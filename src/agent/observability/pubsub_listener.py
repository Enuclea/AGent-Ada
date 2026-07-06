"""Google Cloud Pub/Sub listener for Gmail push notifications.

This module listens to a Pub/Sub topic subscription for Gmail change events.
Upon receiving an event, it triggers an asynchronous sync of Gmail emails.
It also manages file-based process locking to ensure only a single instance runs.
"""

import asyncio
import base64
import json
import logging
import os
import signal
import sys
import fcntl
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Optional, List, Dict, Any

# Add project root to sys.path to allow imports from project packages
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from enuclea.gmail_client import load_gmail_paths, get_gmail_service
from enuclea.gmail_tool import sync_gmail_emails
from google.oauth2 import service_account
from googleapiclient.discovery import build

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger: logging.Logger = logging.getLogger("PubSubListener")

# Config paths and IDs for the Pub/Sub topic and subscription
WORKSPACE_ROOT: Path = Path(__file__).resolve().parent.parent.parent
SERVICE_ACCOUNT_PATH: Path = WORKSPACE_ROOT / "config" / "service_account.json"
PROJECT_ID: str = "calendar-tracker-491316"
TOPIC_NAME: str = f"projects/{PROJECT_ID}/topics/gmail-notifications"
SUBSCRIPTION_NAME: str = f"projects/{PROJECT_ID}/subscriptions/gmail-notifications-sub"
LOCK_FILE_PATH: str = "/tmp/pubsub_listener.lock"


class PubSubListener:
    """Listener that polls a GCP Pub/Sub subscription and triggers Gmail syncing.

    Maintains connections to GCP Pub/Sub and Gmail services, renews
    the Gmail watch subscription, and processes incoming messages.
    """

    def __init__(self) -> None:
        """Initialize the listener with default state."""
        self.running: bool = False
        self.last_watch_renewal: Optional[datetime] = None
        self.pubsub_service: Optional[Any] = None
        self.gmail_service: Optional[Any] = None

    def init_services(self) -> None:
        """Initialize both Google Pub/Sub and Gmail API discovery services.

        Raises:
            FileNotFoundError: If the service account credential file is missing.
        """
        # 1. Initialize Pub/Sub Client using Service Account
        if not SERVICE_ACCOUNT_PATH.exists():
            raise FileNotFoundError(f"Service Account key not found at {SERVICE_ACCOUNT_PATH}")
        
        scopes: List[str] = ["https://www.googleapis.com/auth/pubsub"]
        creds: service_account.Credentials = service_account.Credentials.from_service_account_file(
            str(SERVICE_ACCOUNT_PATH), 
            scopes=scopes
        )
        self.pubsub_service = build("pubsub", "v1", credentials=creds)
        logger.info("Pub/Sub discovery service initialized successfully.")

        # 2. Initialize Gmail Client using User OAuth (for watch registration)
        _, token_path = load_gmail_paths()
        self.gmail_service = get_gmail_service(token_path)
        logger.info("Gmail discovery service initialized successfully.")

    def renew_watch(self) -> None:
        """Register or renew the push notification watch on the Gmail API."""
        try:
            logger.info("Renewing Gmail API watch registration...")
            body: Dict[str, str] = {
                "topicName": TOPIC_NAME
            }
            res: Any = self.gmail_service.users().watch(userId="me", body=body).execute()
            logger.info("Gmail watch renewed successfully. Response: %s", res)
            self.last_watch_renewal = datetime.now(timezone.utc)
        except Exception as e:
            logger.error("Failed to renew Gmail watch: %s", e)

    async def run(self) -> None:
        """Start the Pub/Sub polling loop.

        Runs until self.running is set to False, pulling messages from
        the GCP subscription and executing the Gmail sync on new events.
        """
        self.running = True
        self.init_services()
        
        # Initial watch setup
        self.renew_watch()

        logger.info("Entering Pub/Sub pull listener loop...")
        backoff: int = 5

        while self.running:
            try:
                # 1. Periodically renew watch (once every 24 hours)
                if self.last_watch_renewal is None or (datetime.now(timezone.utc) - self.last_watch_renewal) > timedelta(hours=24):
                    self.renew_watch()

                # 2. Pull messages from the subscription (long-poll in background thread)
                # Setting returnImmediately=False makes the server wait for messages
                logger.debug("Polling Pub/Sub subscription...")
                
                def pull_call() -> Any:
                    """Synchronous call to pull messages from Pub/Sub."""
                    return self.pubsub_service.projects().subscriptions().pull(
                        subscription=SUBSCRIPTION_NAME,
                        body={"maxMessages": 10, "returnImmediately": False}
                    ).execute()

                res: Any = await asyncio.to_thread(pull_call)
                
                received: List[Dict[str, Any]] = res.get("receivedMessages", [])
                if not received:
                    # Reset backoff if connection succeeded but returned no messages
                    backoff = 5
                    await asyncio.sleep(1)
                    continue

                logger.info("Received %d messages from Pub/Sub.", len(received))
                
                # 3. Process each message
                for msg_item in received:
                    message: Dict[str, Any] = msg_item.get("message", {})
                    data_b64: str = message.get("data", "")
                    
                    if data_b64:
                        try:
                            data_str: str = base64.b64decode(data_b64).decode("utf-8")
                            data_json: Any = json.loads(data_str)
                            logger.info("Event payload: %s", data_json)
                        except Exception as decode_err:
                            logger.warning("Could not decode payload: %s", decode_err)
                    
                    # Trigger Gmail sync
                    logger.info("Triggering async Gmail sync...")
                    try:
                        sync_res: str = await sync_gmail_emails()
                        logger.info("Sync output: %s", sync_res)
                    except Exception as sync_err:
                        logger.error("Gmail sync failed: %s", sync_err)

                # 4. Acknowledge messages
                ack_ids: List[str] = [m["ackId"] for m in received]
                
                def ack_call() -> None:
                    """Synchronous call to acknowledge processed messages."""
                    self.pubsub_service.projects().subscriptions().acknowledge(
                        subscription=SUBSCRIPTION_NAME,
                        body={"ackIds": ack_ids}
                    ).execute()

                await asyncio.to_thread(ack_call)
                logger.info("Acknowledged %d messages.", len(ack_ids))
                
                # Reset backoff on success
                backoff = 5

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in listener loop: %s. Backing off for %d seconds...", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)

    def stop(self) -> None:
        """Signal the listener loop to shut down cleanly."""
        logger.info("Shutting down Pub/Sub listener...")
        self.running = False


async def main() -> None:
    """Acquire a file lock, register OS signals, and run the Pub/Sub listener."""
    # 1. Acquire file lock to prevent concurrent listener instances
    lock_file = open(LOCK_FILE_PATH, 'w')
    try:
        fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except IOError:
        logger.error("Another instance of pubsub_listener.py is already running. Exiting.")
        sys.exit(1)

    listener: PubSubListener = PubSubListener()

    # 2. Register signal handlers for clean exit
    try:
        loop: asyncio.AbstractEventLoop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, listener.stop)
    except NotImplementedError:
        pass

    try:
        await listener.run()
    finally:
        # Release lock file under all circumstances
        try:
            fcntl.lockf(lock_file, fcntl.LOCK_UN)
            lock_file.close()
        except Exception:
            pass

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
