"""State storage abstraction for distributed OIDC state management."""

import logging
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class OIDCStateData:
    """Data stored with OIDC state for security validation."""

    redirect_uri: Optional[str] = None
    code_verifier: Optional[str] = None  # PKCE code verifier (S256)
    nonce: Optional[str] = None  # ID token nonce binding
    provider_id: Optional[str] = None  # Auth provider that initiated this flow
    # Same-origin SPA path the user was on when they were bounced to login;
    # the BFF callback redirects here on success. Validated server-side
    # before storage — a path that fails the allowlist is dropped, never
    # round-tripped to the browser.
    return_to: Optional[str] = None
    # Set only by `GET /auth/cli/verify`. Carries the *user* code (not the
    # device code, which the server never sees in the clear) of the device
    # grant this browser round-trip is approving. Its presence is what makes
    # the callback take the device branch: mint the session, approve the
    # grant, and set NO cookies. Server-side only and one-time-use, like the
    # rest of the state row.
    device_user_code: Optional[str] = None
    # SHA-256 (hex) of the browser-binding secret handed to the client in a
    # short-lived, HttpOnly cookie when this state was minted. The callback
    # recomputes the digest from the cookie it receives and refuses the
    # exchange unless it matches. This is what makes `state` useless to a
    # browser other than the one that started the flow — without it, anyone
    # who can read a `state` value out of a 302 Location can have a victim's
    # browser complete *their* login (OAuth login CSRF / session fixation).
    #
    # Only the digest is persisted: a read-only compromise of the state table
    # then yields nothing that can be replayed as the cookie.
    browser_binding_hash: Optional[str] = None
    # Set only by `GET /auth/login?client=native`. The app's own redirect URI
    # (already exact-matched against `NATIVE_AUTH_REDIRECT_URIS`) and its S256
    # PKCE challenge. Their presence is what makes the callback take the
    # native exit: no cookies, a handoff code on a redirect to the app. Kept
    # apart from `redirect_uri`, which is the Cognito callback URL, and from
    # `code_verifier`, which is *our* verifier for the Cognito leg.
    native_redirect_uri: Optional[str] = None
    native_code_challenge: Optional[str] = None


@dataclass
class NativeHandoff:
    """A one-time handoff of a fresh native session to the app that began it.

    Written by the callback's native exit under `HANDOFF#<code>` and consumed
    by `POST /auth/native/exchange`, which must present the verifier whose S256
    digest is `code_challenge`. `expires_at` travels with the row so an expired
    handoff can still name the session it would have handed over, and the
    exchange can delete that orphaned row rather than leave it to its TTL.
    """

    session_id: str
    code_challenge: str
    expires_at: int


class StateStore(ABC):
    """Abstract interface for state token storage."""

    @abstractmethod
    def store_state(
        self,
        state: str,
        data: Optional[OIDCStateData] = None,
        ttl_seconds: int = 600
    ) -> None:
        """
        Store a state token with associated OIDC data.

        Args:
            state: State token to store
            data: Optional OIDC state data (redirect_uri, code_verifier, nonce)
            ttl_seconds: Time-to-live in seconds
        """
        pass

    @abstractmethod
    def get_and_delete_state(self, state: str) -> tuple[bool, Optional[OIDCStateData]]:
        """
        Retrieve and delete a state token (one-time use).

        Args:
            state: State token to retrieve

        Returns:
            Tuple of (is_valid, OIDCStateData or None)
        """
        pass

    @abstractmethod
    def store_handoff(self, code: str, handoff: NativeHandoff) -> None:
        """Store a native handoff under `HANDOFF#<code>` until `expires_at`."""
        pass

    @abstractmethod
    def take_handoff(self, code: str) -> Optional[NativeHandoff]:
        """Atomically read and delete a handoff (one-time use).

        Returns the row even when it has expired, so the caller can clean up
        the session it names; the caller checks `expires_at`. Returns None
        when no such handoff exists or another caller consumed it first.
        """
        pass


def _handoff_key(code: str) -> str:
    return f"HANDOFF#{code}"


class InMemoryStateStore(StateStore):
    """In-memory state storage (for single-instance/local development)."""

    def __init__(self):
        """Initialize in-memory storage."""
        # Format: {state: (expires_at, OIDCStateData)}
        self._store: dict[str, tuple[float, Optional[OIDCStateData]]] = {}
        # Expired handoffs are kept until taken so the exchange can name the
        # orphaned session; local dev only, so the leak is bounded and moot.
        self._handoffs: dict[str, NativeHandoff] = {}

    def store_state(
        self,
        state: str,
        data: Optional[OIDCStateData] = None,
        ttl_seconds: int = 600
    ) -> None:
        """Store state in memory."""
        expires_at = time.time() + ttl_seconds
        self._store[state] = (expires_at, data)
        self._cleanup_expired()

    def get_and_delete_state(self, state: str) -> tuple[bool, Optional[OIDCStateData]]:
        """Retrieve and delete state from memory."""
        self._cleanup_expired()

        if state not in self._store:
            return False, None

        expires_at, data = self._store[state]

        # Check expiration
        if time.time() > expires_at:
            del self._store[state]
            return False, None

        # Delete after retrieval (one-time use)
        del self._store[state]
        return True, data

    def store_handoff(self, code: str, handoff: NativeHandoff) -> None:
        self._handoffs[_handoff_key(code)] = handoff

    def take_handoff(self, code: str) -> Optional[NativeHandoff]:
        # `dict.pop` is atomic under the GIL, so two concurrent exchanges of
        # one code cannot both receive it.
        return self._handoffs.pop(_handoff_key(code), None)

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
                "boto3 is required for DynamoDBStateStore. Install with: uv sync"
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
        data: Optional[OIDCStateData] = None,
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

            if data:
                if data.redirect_uri:
                    item['redirect_uri'] = data.redirect_uri
                if data.code_verifier:
                    item['code_verifier'] = data.code_verifier
                if data.nonce:
                    item['nonce'] = data.nonce
                if data.provider_id:
                    item['provider_id'] = data.provider_id
                if data.return_to:
                    item['return_to'] = data.return_to
                if data.device_user_code:
                    item['device_user_code'] = data.device_user_code
                if data.browser_binding_hash:
                    item['browser_binding_hash'] = data.browser_binding_hash
                if data.native_redirect_uri:
                    item['native_redirect_uri'] = data.native_redirect_uri
                if data.native_code_challenge:
                    item['native_code_challenge'] = data.native_code_challenge

            # Use expiresAt as TTL attribute (DynamoDB will auto-delete)
            self.table.put_item(Item=item)
            logger.debug(f"Stored state token: PK=STATE#{state[:8]}..., expiresAt={expires_at}")

        except self._client_error as e:
            logger.error(f"Failed to store state in DynamoDB: {e}")
            raise

    def get_and_delete_state(self, state: str) -> tuple[bool, Optional[OIDCStateData]]:
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

            # Build OIDCStateData from stored item
            data = OIDCStateData(
                redirect_uri=item.get('redirect_uri'),
                code_verifier=item.get('code_verifier'),
                nonce=item.get('nonce'),
                provider_id=item.get('provider_id'),
                return_to=item.get('return_to'),
                device_user_code=item.get('device_user_code'),
                browser_binding_hash=item.get('browser_binding_hash'),
                native_redirect_uri=item.get('native_redirect_uri'),
                native_code_challenge=item.get('native_code_challenge'),
            )
            return True, data
            
        except self._client_error as e:
            logger.error(f"Failed to retrieve state from DynamoDB: {e}")
            return False, None


    def store_handoff(self, code: str, handoff: NativeHandoff) -> None:
        """Store a handoff row. `expiresAt` is the table's TTL attribute."""
        key = _handoff_key(code)
        self.table.put_item(
            Item={
                'PK': key,
                'SK': key,
                'session_id': handoff.session_id,
                'code_challenge': handoff.code_challenge,
                'expiresAt': handoff.expires_at,
                'created_at': int(time.time()),
            }
        )

    def take_handoff(self, code: str) -> Optional[NativeHandoff]:
        """One conditional delete that returns the old item.

        Unlike `get_and_delete_state`'s read-then-delete, this is a single
        atomic request: two exchanges of one code race on the condition and
        exactly one receives the item.
        """
        key = _handoff_key(code)
        try:
            response = self.table.delete_item(
                Key={'PK': key, 'SK': key},
                ConditionExpression='attribute_exists(PK)',
                ReturnValues='ALL_OLD',
            )
        except self._client_error as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                return None
            raise
        item = response.get('Attributes')
        if not item or not item.get('session_id'):
            return None
        return NativeHandoff(
            session_id=item['session_id'],
            code_challenge=item.get('code_challenge') or '',
            expires_at=int(item.get('expiresAt') or 0),
        )


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

