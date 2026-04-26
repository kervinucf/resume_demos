from typing import Dict, List, Optional, Callable, Any
from dataclasses import dataclass, field
from atproto import IdResolver
from atproto_core.did_doc import DidDocument, Service


@dataclass
class DIDService:
    """Represents a single service within a DID Document."""
    id: str
    type: str
    service_endpoint: str


class DIDManager:
    """Manages Decentralized Identifiers (DIDs) and their associated DID Documents."""

    def __init__(self, did: str, services: Optional[List[Dict[str, str]]] = None, description: Optional[str] = None):
        """
        Initialize a DIDManager instance.

        Args:
            did: The Decentralized Identifier (DID) for the entity.
            services: A list of services associated with the DID.
            description: Optional description for the DID resource.
        """
        self.did = did
        self.services = services or []
        self.description = description or ""

    def create_did_document(self) -> DidDocument:
        """
        Generate a DID document based on the current DID and its services.

        Returns:
            A DidDocument object representing the DID and its associated services.
        """
        return DidDocument(
            id=self.did,
            service=[
                Service(
                    id=f"{self.did}#{svc['id']}",
                    type=svc['type'],
                    service_endpoint=svc['endpoint']
                ) for svc in self.services
            ]
        )

    def get_service_endpoint(self, service_type: str = "ResourceService") -> Optional[str]:
        """
        Retrieve a service endpoint of a specified type from the associated services.

        Args:
            service_type: The type of service to look for (default is "ResourceService").

        Returns:
            The service endpoint URL if found, else None.
        """
        for service in self.services:
            if service["type"] == service_type:
                return service["endpoint"]
        return None

    @staticmethod
    def resolve_did(did: str, resolver: Optional[IdResolver] = None) -> DidDocument:
        """
        Resolve a DID to its DID Document using the IdResolver.

        Args:
            did: The DID to resolve.
            resolver: An optional IdResolver instance (default is a new instance).

        Returns:
            The resolved DidDocument object.
        """
        resolver = resolver or IdResolver()
        return resolver.did.resolve(did)

    @staticmethod
    def resolve_handle(handle: str, resolver: Optional[IdResolver] = None) -> str:
        """
        Resolve a handle to its corresponding DID using the IdResolver.

        Args:
            handle: The handle to resolve (e.g., "user.bsky.social").
            resolver: An optional IdResolver instance (default is a new instance).

        Returns:
            The resolved DID as a string.
        """
        resolver = resolver or IdResolver()
        return resolver.handle.resolve(handle)

    def add_service(self, service: DIDService):
        """
        Add a service to the DID's associated services.

        Args:
            service: A DIDService object containing the service details.
        """
        self.services.append({
            "id": service.id,
            "type": service.type,
            "endpoint": service.service_endpoint
        })

    def remove_service(self, service_id: str):
        """
        Remove a service from the DID's associated services by its ID.

        Args:
            service_id: The ID of the service to remove.
        """
        self.services = [svc for svc in self.services if svc["id"] != service_id]

    def list_services(self) -> List[Dict[str, str]]:
        """
        List all services associated with the DID.

        Returns:
            A list of dictionaries representing the services.
        """
        return self.services

    def describe(self) -> Dict[str, Any]:
        """
        Provide a detailed description of the DID, including its services and metadata.

        Returns:
            A dictionary describing the DID and its associated data.
        """
        return {
            "did": self.did,
            "description": self.description,
            "services": self.services
        }
