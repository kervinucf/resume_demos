from typing import Dict, Callable, Optional, List
import asyncio
import json
import aiohttp
import logging
from aiohttp import ClientSession, ClientWebSocketResponse

logger = logging.getLogger(__name__)


class ContentStreamManager:
    """
    Manages Firehose/Jetstream streaming using aiohttp, with support for dynamic endpoint configuration.
    """

    DEFAULT_FIREHOSE_ENDPOINT = "wss://bsky.network/xrpc/com.atproto.sync.subscribeRepos"
    DEFAULT_JETSTREAM_ENDPOINT = \
        "wss://jetstream2.us-west.bsky.network/subscribe?wantedCollections=app.bsky.feed.post"

    def __init__(self,
                 jetstream_mode=False,
                 stream_url: Optional[str] = None
                 ):
        """
        Initialize the ContentStreamManager with a specific stream URL.
        If no URL is provided, it defaults to the public AT Protocol Firehose endpoint.
        """
        self.stream_url = (stream_url or
                           self.DEFAULT_JETSTREAM_ENDPOINT
                           if jetstream_mode else self.DEFAULT_FIREHOSE_ENDPOINT)
        self.is_running = False
        self.event_count = 0
        self.filters: List[Callable[[Dict], bool]] = []
        self.reconnect_delay = 5  # Delay in seconds for reconnect attempts
        self.max_retries = 5  # Max reconnect attempts before giving up

    def add_filter(self, filter_func: Callable[[Dict], bool]):
        """
        Add a filter function to selectively process events.

        Args:
            filter_func (Callable[[Dict], bool]): A function that returns True for events to process.
        """
        self.filters.append(filter_func)
        logger.info("Added filter function.")

    def apply_filters(self, event: Dict) -> bool:
        """
        Apply all registered filters to an event.

        Args:
            event (Dict): The event to filter.

        Returns:
            bool: True if the event passes all filters, False otherwise.
        """
        return all(filter_func(event) for filter_func in self.filters)

    async def _connect(self) -> ClientWebSocketResponse:
        """
        Establish a WebSocket connection using aiohttp.

        Returns:
            ClientWebSocketResponse: The WebSocket connection object.
        """
        session = ClientSession()
        try:
            ws = await session.ws_connect(self.stream_url)
            logger.info(f"Connected to {self.stream_url}.")
            return ws
        except Exception as e:
            logger.error(f"Failed to connect to {self.stream_url}: {e}")
            await session.close()
            raise

    async def start(
            self,
            event_handler: Callable[[Dict], None],
            error_handler: Optional[Callable[[Exception], None]] = None,
    ):
        """
        Start a WebSocket connection to the Firehose to stream events.

        Args:
            event_handler (Callable[[Dict], None]): Function to handle each event.
            error_handler (Optional[Callable[[Exception], None]]): Function to handle errors.
        """
        retries = 0
        self.is_running = True

        while self.is_running and retries < self.max_retries:
            try:
                ws = await self._connect()
                retries = 0

                async for msg in ws:
                    if not self.is_running:
                        break

                    if msg.type == aiohttp.WSMsgType.TEXT:
                        try:
                            # Decode JSON data (replace with CBOR decoding if needed)
                            event_data = json.loads(msg.data)
                            if self.apply_filters(event_data):
                                await event_handler(event_data)
                                self.event_count += 1
                        except Exception as e:
                            logger.error(f"Error processing event: {e}")
                            if error_handler:
                                await error_handler(e)
                    elif msg.type == aiohttp.WSMsgType.ERROR:
                        logger.error(f"WebSocket error: {msg.data}")
                        break

            except Exception as e:
                retries += 1
                logger.warning(
                    f"Connection failed: {e}. Retrying in {self.reconnect_delay} seconds... (Attempt {retries}/{self.max_retries})"
                )
                await asyncio.sleep(self.reconnect_delay)

        if retries >= self.max_retries:
            logger.error("Maximum retry attempts reached. Exiting.")

    def stop(self):
        """
        Stop the WebSocket connection.
        """
        self.is_running = False
        logger.info("Streaming stopped.")
