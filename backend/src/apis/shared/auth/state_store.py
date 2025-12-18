"""State storage abstraction for distributed OIDC state management."""

import logging
import os
import time
from abc import ABC, abstractmethod
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class StateStore(ABC):
    """Abstract interface for state token storage."""
    
    @abstractmethod
    def store_state(
        self,
        state: str,
        redirect_uri: Optional[str] = None,
        ttl_seconds: int = 600
    ) -> None:
        """
        Store a state token with optional redirect URI.
        
        Args:
            state: State token to store
            redirect_uri: Optional redirect URI to associate with state
            ttl_seconds: Time-to-live in seconds
        """
        pass
    
    @abstractmethod
    def get_and_delete_state(self, state: str) -> Tuple[bool, Optional[str]]:
        """
        Retrieve and delete a state token (one-time use).
        
        Args:
            state: State token to retrieve
            
        Returns:
            Tuple of (is_valid, redirect_uri)
        """
        pass


class InMemoryStateStore(StateStore):
    """In-memory state storage (for single-instance/local development)."""
    
    def __init__(self):
        """Initialize in-memory storage."""
        # Format: {state: (expires_at, redirect_uri)}
        self._store: dict[str, Tuple[float, Optional[str]]] = {}
    
    def store_state(
        self,
        state: str,
        redirect_uri: Optional[str] = None,
        ttl_seconds: int = 600
    ) -> None:
        """Store state in memory."""
        expires_at = time.time() + ttl_seconds
        self._store[state] = (expires_at, redirect_uri)
        self._cleanup_expired()
    
    def get_and_delete_state(self, state: str) -> Tuple[bool, Optional[str]]:
        """Retrieve and delete state from memory."""
        self._cleanup_expired()
        
        if state not in self._store:
            return False, None
        
        expires_at, redirect_uri = self._store[state]
        
        # Check expiration
        if time.time() > expires_at:
            del self._store[state]
            return False, None
        
        # Delete after retrieval (one-time use)
        del self._store[state]
        return True, redirect_uri
    
    def _cleanup_expired(self):
        """Remove expired states."""
        current_time = time.time()
        expired = [
            state for state, (expires_at, _) in self._store.items()
            if current_time > expires_at
        ]
        for state in expired:
            del self._store[state]


class DynamoDBStateStore(StateStore):
    """DynamoDB-based state storage for distributed systems."""
    
    def __init__(self, table_name: Optional[str] = None, region: Optional[str] = None):
        """
        Initialize DynamoDB state store.
        
        Args:
            table_name: DynamoDB table name (defaults to env var or 'oidc-state-store')
            region: AWS region (defaults to env var or 'us-west-2')
        """
        try:
            import boto3
            from botocore.exceptions import ClientError
        except ImportError:
            raise ImportError(
                "boto3 is required for DynamoDBStateStore. Install with: pip install boto3"
            )
        
        self.table_name = table_name or os.getenv('DYNAMODB_OIDC_STATE_TABLE_NAME', 'oidc-state-store')
        self.region = region or os.getenv('AWS_REGION', os.getenv('AWS_DEFAULT_REGION', 'us-west-2'))
        
        # Determine AWS profile
        profile = os.getenv('AWS_PROFILE')
        if profile:
            session = boto3.Session(profile_name=profile)
            self.dynamodb = session.resource('dynamodb', region_name=self.region)
        else:
            self.dynamodb = boto3.resource('dynamodb', region_name=self.region)
        
        self.table = self.dynamodb.Table(self.table_name)
        self._client_error = ClientError
        
        logger.info(f"Initialized DynamoDB state store: table={self.table_name}, region={self.region}")
    
    def store_state(
        self,
        state: str,
        redirect_uri: Optional[str] = None,
        ttl_seconds: int = 600
    ) -> None:
        """Store state in DynamoDB with TTL."""
        expires_at = int(time.time()) + ttl_seconds
        
        try:
            item = {
                'PK': f'STATE#{state}',
                'SK': f'STATE#{state}',
                'state': state,
                'expiresAt': expires_at,  # Match table TTL attribute name (camelCase)
                'created_at': int(time.time()),
            }
            
            if redirect_uri:
                item['redirect_uri'] = redirect_uri
            
            # Use expiresAt as TTL attribute (DynamoDB will auto-delete)
            self.table.put_item(Item=item)
            logger.debug(f"Stored state token: PK=STATE#{state[:8]}..., expiresAt={expires_at}")
            
        except self._client_error as e:
            logger.error(f"Failed to store state in DynamoDB: {e}")
            raise
    
    def get_and_delete_state(self, state: str) -> Tuple[bool, Optional[str]]:
        """
        Retrieve and delete state from DynamoDB atomically.
        
        Uses conditional delete to ensure one-time use and prevent race conditions.
        """
        try:
            # Get item with conditional delete (only if not expired)
            current_time = int(time.time())
            state_key = {
                'PK': f'STATE#{state}',
                'SK': f'STATE#{state}'
            }
            
            logger.debug(f"Looking up state token: PK=STATE#{state[:8]}...")
            
            response = self.table.get_item(
                Key=state_key,
                ConsistentRead=True  # Use consistent read for immediate consistency
            )
            
            if 'Item' not in response:
                logger.warning(f"State token not found in DynamoDB: PK=STATE#{state[:8]}...")
                return False, None
            
            item = response['Item']
            # Check both expires_at (old format) and expiresAt (table TTL format)
            expires_at = item.get('expiresAt') or item.get('expires_at', 0)
            
            logger.debug(f"Found state token: expiresAt={expires_at}, current_time={current_time}")
            
            # Check expiration
            if expires_at and current_time > expires_at:
                logger.warning(f"State token expired: expiresAt={expires_at}, current_time={current_time}")
                # Try to delete expired item (best effort)
                try:
                    self.table.delete_item(Key=state_key)
                except self._client_error:
                    pass  # Ignore delete errors for expired items
                return False, None
            
            # Atomically delete the item (ensures one-time use)
            # Use conditional delete to prevent race conditions
            try:
                self.table.delete_item(
                    Key=state_key,
                    ConditionExpression='attribute_exists(PK)'
                )
            except self._client_error as e:
                # If delete fails, item was already consumed (race condition)
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    logger.warning(f"State token {state} was already consumed")
                    return False, None
                raise
            
            redirect_uri = item.get('redirect_uri')
            return True, redirect_uri
            
        except self._client_error as e:
            logger.error(f"Failed to retrieve state from DynamoDB: {e}")
            return False, None


def create_state_store() -> StateStore:
    """
    Create appropriate state store based on environment configuration.
    
    Returns:
        StateStore instance (DynamoDB if configured, otherwise in-memory)
    """
    # Check if DynamoDB table name is configured
    table_name = os.getenv('DYNAMODB_OIDC_STATE_TABLE_NAME')
    
    if table_name:
        try:
            return DynamoDBStateStore(table_name=table_name)
        except Exception as e:
            logger.warning(
                f"Failed to initialize DynamoDB state store: {e}. "
                "Falling back to in-memory storage."
            )
            return InMemoryStateStore()
    else:
        logger.info(
            "DYNAMODB_OIDC_STATE_TABLE_NAME not set. Using in-memory state storage. "
            "This will not work in distributed deployments."
        )
        return InMemoryStateStore()

