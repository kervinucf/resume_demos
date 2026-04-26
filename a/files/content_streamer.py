import asyncio
import logging
from src.client import BlueSkyClient


# Event Handlers
async def handle_event(event_data):
    print("Received event:", event_data)


async def handle_error(error):
    print("Error occurred:", error)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    logger = logging.getLogger(__name__)

    # Initialize the BlueSkyClient
    api = BlueSkyClient()

    # Run the stream in an asyncio loop
    loop = asyncio.get_event_loop()
    try:
        loop.run_until_complete(
            api.start_stream(
                callback=handle_event,
                on_error=handle_error,
                jetstream_mode=True
            )
        )
    except KeyboardInterrupt:
        logger.info("Stopping the stream...")
        loop.run_until_complete(api.stop_stream())
        print("Firehose stopped.")
    finally:
        loop.close()
