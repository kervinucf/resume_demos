import logging
from typing import Dict, List, Optional
import requests
from src.stream import ContentStreamManager
import os
from dotenv import load_dotenv

load_dotenv(
    dotenv_path=os.path.abspath("./setup/.env")
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BlueSkyClient:
    """General-purpose client software for managing a Bluesky repository"""

    DEFAULT_PDS_URL = "https://bsky.social"  # Default PDS URL

    def __init__(
            self,
            handle: Optional[str] = None,
            password: Optional[str] = None,
            pds_url: Optional[str] = None,
    ):
        self.handle = handle or os.getenv('BLUESKY_HANDLE')
        self.password = password or os.getenv('BLUESKY_PASSWORD')
        self.pds_url = pds_url or self.DEFAULT_PDS_URL
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self.auth_token = None
        self.stream_handler = None
        self._login()

    def _login(self):
        """Authenticate and obtain an access token."""
        if not self.handle or not self.password:
            raise ValueError("Handle and password must be provided.")

        url = f"{self.pds_url}/xrpc/com.atproto.server.createSession"
        payload = {"identifier": self.handle, "password": self.password}

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            self.auth_token = data["accessJwt"]
            self.session.headers.update({"Authorization": f"Bearer {self.auth_token}"})
            logger.info("Login successful.")
        except requests.RequestException as e:
            logger.error(f"Login failed: {e}")
            raise

    # Repo Navigation
    def create_collection(self, nsid: str, schema: Dict):
        """Create a new collection in the repo"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.putRecord"
        payload = {
            "repo": self._get_repo_id(),
            "collection": nsid,
            "record": schema,
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Collection {nsid} created successfully.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to create collection {nsid}: {e}")
            raise

    def repo_description(self) -> List[Dict]:
        """List all collections in the repo"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.describeRepo"
        params = {
            "repo": self._get_repo_id(),
        }

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            logger.info("Fetched collections successfully.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to list collections: {e}")
            raise

    def get_collection_records(self, collection: str, limit: int = 50, cursor: Optional[str] = None) -> Dict:
        """Get records from a specific collection with pagination support"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.listRecords"
        params = {
            "repo": self._get_repo_id(),
            "collection": collection,
            "limit": limit
        }
        if cursor:
            params["cursor"] = cursor

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            logger.info(f"Fetched records from collection {collection}.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch records from collection {collection}: {e}")
            raise

    def get_record(self, collection: str, rkey: str) -> Dict:
        """Get a specific record from a collection"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.getRecord"
        params = {
            "repo": self._get_repo_id(),
            "collection": collection,
            "rkey": rkey
        }

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            logger.info(f"Fetched record {rkey} from collection {collection}.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to fetch record {rkey} from collection {collection}: {e}")
            raise

    def search_collection(self, collection: str, query: str, limit: int = 50) -> List[Dict]:
        """Search for records within a collection"""
        url = f"{self.pds_url}/xrpc/app.bsky.feed.searchPosts"
        params = {
            "q": query,
            "limit": limit
        }

        try:
            response = self.session.get(url, params=params)
            response.raise_for_status()
            logger.info(f"Searched collection {collection} with query: {query}")
            return response.json().get("posts", [])
        except requests.RequestException as e:
            logger.error(f"Failed to search collection {collection}: {e}")
            raise

    def add_record(self, collection: str, record_data: Dict, rkey: Optional[str] = None):
        """Add a record to a collection"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.createRecord"
        payload = {
            "repo": self._get_repo_id(),
            "collection": collection,
            "record": record_data,
            "rkey": rkey,
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Record added to collection {collection}.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to add record to collection {collection}: {e}")
            raise

    def update_record(self, collection: str, rkey: str, record_data: Dict):
        """Update a record in a collection"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.putRecord"
        payload = {
            "repo": self._get_repo_id(),
            "collection": collection,
            "rkey": rkey,
            "record": record_data,
        }

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Record {rkey} updated in collection {collection}.")
            return response.json()
        except requests.RequestException as e:
            logger.error(f"Failed to update record {rkey} in collection {collection}: {e}")
            raise

    def delete_record(self, collection: str, rkey: str):
        """Delete a record from a collection"""
        url = f"{self.pds_url}/xrpc/com.atproto.repo.deleteRecord"
        payload = {"repo": self._get_repo_id(), "collection": collection, "rkey": rkey}

        try:
            response = self.session.post(url, json=payload)
            response.raise_for_status()
            logger.info(f"Record {rkey} deleted from collection {collection}.")
        except requests.RequestException as e:
            logger.error(f"Failed to delete record {rkey} from collection {collection}: {e}")
            raise

    # Helper Methods
    def _get_repo_id(self) -> str:
        """Retrieve the repo ID (DID)"""
        url = f"{self.pds_url}/xrpc/com.atproto.server.getSession"
        try:
            response = self.session.get(url)
            response.raise_for_status()
            return response.json().get("did")
        except requests.RequestException as e:
            logger.error("Failed to retrieve repo ID: {e}")
            raise

    # Stream Handling
    async def start_stream(self, callback=None, on_error=None, jetstream_mode=False):
        """Start streaming data from the repo"""
        self.stream_handler = ContentStreamManager(jetstream_mode=jetstream_mode)
        logger.info("Starting stream.")
        await self.stream_handler.start(
            event_handler=callback,
            error_handler=on_error
        )

    async def stop_stream(self):
        """Stop streaming data"""
        logger.info("Stopping stream.")
        await self.stream_handler.stop()
        self.stream_handler = None
