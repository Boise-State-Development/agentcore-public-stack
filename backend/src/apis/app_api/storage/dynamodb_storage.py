"""DynamoDB storage implementation for production

This storage backend uses AWS DynamoDB to store message metadata and cost summaries.
It's designed for production deployments with high scalability and performance.

Schema:
    SessionsMetadata Table:
        Session Records:
            PK: USER#<user_id>
            SK: S#ACTIVE#<last_message_at>#<session_id> (active sessions)
                S#DELETED#<deleted_at>#<session_id> (deleted sessions)
            Attributes: sessionId, title, status, createdAt, lastMessageAt, messageCount, etc.

        Cost Records:
            PK: USER#<user_id>
            SK: C#<timestamp>#<uuid>
            Attributes: cost, tokenUsage, latency, modelInfo, pricingSnapshot, etc.

        GSI: SessionLookupIndex
            GSI_PK: SESSION#<session_id>
            GSI_SK: META (for session records) or C#<timestamp> (for cost records)

    UserCostSummary Table:
        PK: USER#<user_id>
        SK: PERIOD#<YYYY-MM>
        Attributes: totalCost, totalRequests, totalTokens, modelBreakdown, etc.
"""

import os
from typing import Optional, List, Dict, Any
from datetime import datetime, timedelta, timezone
from decimal import Decimal

try:
    import boto3
    from botocore.exceptions import ClientError
except ImportError:
    # Allow module to load without boto3 in development
    boto3 = None
    ClientError = Exception

from .metadata_storage import MetadataStorage


class DynamoDBStorage(MetadataStorage):
    """DynamoDB storage for production environments"""

    def __init__(self):
        """Initialize DynamoDB client and table references"""
        if boto3 is None:
            raise ImportError(
                "boto3 is required for DynamoDB storage. "
                "Install with: pip install boto3"
            )

        self.dynamodb = boto3.resource('dynamodb')

        # Get table names from environment
        self.sessions_metadata_table_name = os.environ.get(
            "DYNAMODB_SESSIONS_METADATA_TABLE_NAME",
            "SessionsMetadata"
        )
        self.cost_summary_table_name = os.environ.get(
            "DYNAMODB_COST_SUMMARY_TABLE_NAME",
            "UserCostSummary"
        )
        self.system_rollup_table_name = os.environ.get(
            "DYNAMODB_SYSTEM_ROLLUP_TABLE_NAME",
            "SystemCostRollup"
        )

        # Initialize table references
        self.sessions_metadata_table = self.dynamodb.Table(self.sessions_metadata_table_name)
        self.cost_summary_table = self.dynamodb.Table(self.cost_summary_table_name)
        self.system_rollup_table = self.dynamodb.Table(self.system_rollup_table_name)

    def _convert_floats_to_decimal(self, obj: Any) -> Any:
        """
        Recursively convert floats to Decimal for DynamoDB

        DynamoDB doesn't support float type, requires Decimal instead.
        """
        if isinstance(obj, float):
            return Decimal(str(obj))
        elif isinstance(obj, dict):
            return {k: self._convert_floats_to_decimal(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_floats_to_decimal(item) for item in obj]
        else:
            return obj

    def _convert_decimal_to_float(self, obj: Any) -> Any:
        """
        Recursively convert Decimal to float for JSON serialization

        DynamoDB returns Decimal objects, which need to be converted back to float.
        """
        if isinstance(obj, Decimal):
            return float(obj)
        elif isinstance(obj, dict):
            return {k: self._convert_decimal_to_float(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [self._convert_decimal_to_float(item) for item in obj]
        else:
            return obj

    async def store_message_metadata(
        self,
        user_id: str,
        session_id: str,
        message_id: int,
        metadata: Dict[str, Any]
    ) -> None:
        """
        Store message metadata (cost record) in DynamoDB

        Schema:
            PK: USER#<user_id>
            SK: C#<timestamp>#<uuid>

            GSI: SessionLookupIndex
                GSI_PK: SESSION#<session_id>
                GSI_SK: C#<timestamp>
        """
        import uuid as uuid_lib

        # Convert floats to Decimal for DynamoDB
        metadata_decimal = self._convert_floats_to_decimal(metadata)

        # Extract timestamp for SK and GSI
        timestamp = metadata.get("attribution", {}).get("timestamp", datetime.now(timezone.utc).isoformat())

        # Generate unique ID for SK to prevent collisions
        unique_id = str(uuid_lib.uuid4())

        # Calculate TTL (365 days from now) - only cost records have TTL
        ttl = int((datetime.now(timezone.utc) + timedelta(days=365)).timestamp())

        # Build item with new C# prefix SK pattern
        item = {
            # Primary key with C# prefix for cost records
            "PK": f"USER#{user_id}",
            "SK": f"C#{timestamp}#{unique_id}",

            # GSI keys for per-session cost queries
            "GSI_PK": f"SESSION#{session_id}",
            "GSI_SK": f"C#{timestamp}",

            # Session reference
            "userId": user_id,
            "sessionId": session_id,
            "messageId": message_id,
            "timestamp": timestamp,

            # TTL - only cost records have this attribute
            "ttl": ttl,

            # Cost and usage metadata
            **metadata_decimal
        }

        # Store in DynamoDB
        try:
            self.sessions_metadata_table.put_item(Item=item)
        except ClientError as e:
            raise Exception(f"Failed to store message metadata: {e}")

    async def get_message_metadata(
        self,
        user_id: str,
        session_id: str,
        message_id: int
    ) -> Optional[Dict[str, Any]]:
        """
        Get metadata for a specific message by querying cost records via GSI

        With the new C# SK pattern, we can't use get_item directly.
        Instead, we query the GSI and filter by messageId.

        Schema:
            GSI: SessionLookupIndex
                GSI_PK: SESSION#<session_id>
                GSI_SK: begins_with C#
        """
        from boto3.dynamodb.conditions import Key

        try:
            # Query cost records for this session via GSI
            response = self.sessions_metadata_table.query(
                IndexName='SessionLookupIndex',
                KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').begins_with('C#'),
                FilterExpression='messageId = :mid AND userId = :uid',
                ExpressionAttributeValues={
                    ':mid': message_id,
                    ':uid': user_id
                }
            )

            items = response.get("Items", [])
            if not items:
                return None

            # Convert Decimal back to float
            item = self._convert_decimal_to_float(items[0])

            # Remove DynamoDB-specific keys
            for key in ["PK", "SK", "GSI_PK", "GSI_SK", "ttl"]:
                item.pop(key, None)

            return item

        except ClientError as e:
            raise Exception(f"Failed to get message metadata: {e}")

    async def get_session_metadata(
        self,
        user_id: str,
        session_id: str
    ) -> List[Dict[str, Any]]:
        """
        Get all message metadata (cost records) for a session via GSI

        Schema:
            GSI: SessionLookupIndex
                GSI_PK: SESSION#<session_id>
                GSI_SK: begins_with C#
        """
        from boto3.dynamodb.conditions import Key

        try:
            response = self.sessions_metadata_table.query(
                IndexName='SessionLookupIndex',
                KeyConditionExpression=Key('GSI_PK').eq(f'SESSION#{session_id}') & Key('GSI_SK').begins_with('C#'),
                FilterExpression='userId = :uid',
                ExpressionAttributeValues={
                    ':uid': user_id
                }
            )

            items = response.get("Items", [])

            # Convert Decimal to float and remove DynamoDB keys
            results = []
            for item in items:
                item_float = self._convert_decimal_to_float(item)
                for key in ["PK", "SK", "GSI_PK", "GSI_SK", "ttl"]:
                    item_float.pop(key, None)
                results.append(item_float)

            return results

        except ClientError as e:
            raise Exception(f"Failed to get session metadata: {e}")

    async def get_user_cost_summary(
        self,
        user_id: str,
        period: str
    ) -> Optional[Dict[str, Any]]:
        """
        Get pre-aggregated cost summary for a user

        This provides <10ms quota checks by reading pre-aggregated data.
        """
        try:
            response = self.cost_summary_table.get_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PERIOD#{period}"
                }
            )

            if "Item" not in response:
                return None

            # Convert Decimal back to float
            item = self._convert_decimal_to_float(response["Item"])

            # Remove DynamoDB-specific keys
            for key in ["PK", "SK", "GSI2PK", "GSI2SK"]:
                item.pop(key, None)

            return item

        except ClientError as e:
            raise Exception(f"Failed to get user cost summary: {e}")

    async def update_user_cost_summary(
        self,
        user_id: str,
        period: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        timestamp: str,
        model_id: Optional[str] = None,
        model_name: Optional[str] = None,
        cache_savings_delta: float = 0.0,
        provider: Optional[str] = None
    ) -> None:
        """
        Update pre-aggregated cost summary (atomic increment)

        Uses DynamoDB's atomic ADD operation for concurrent safety.
        Also updates per-model breakdown and cache savings for detailed cost analysis.

        Additionally maintains GSI2 attributes for the PeriodCostIndex GSI:
        - GSI2PK: PERIOD#{YYYY-MM} - enables querying all users in a period
        - GSI2SK: COST#{15-digit-zero-padded-cents} - enables sorting by cost
        """
        try:
            key = {
                "PK": f"USER#{user_id}",
                "SK": f"PERIOD#{period}"
            }

            # Base update expression for aggregate totals
            # GSI2PK is set immediately (static per period)
            # GSI2SK will be updated after we know the new total
            update_expression = """
                ADD totalCost :cost,
                    totalRequests :one,
                    totalInputTokens :input,
                    totalOutputTokens :output,
                    totalCacheReadTokens :cacheRead,
                    totalCacheWriteTokens :cacheWrite,
                    cacheSavings :savings
                SET lastUpdated = :now,
                    periodStart = if_not_exists(periodStart, :periodStart),
                    periodEnd = if_not_exists(periodEnd, :periodEnd),
                    userId = :userId,
                    GSI2PK = :gsi2pk
            """

            expression_values = {
                ":cost": Decimal(str(cost_delta)),
                ":one": 1,
                ":input": usage_delta.get("inputTokens", 0),
                ":output": usage_delta.get("outputTokens", 0),
                ":cacheRead": usage_delta.get("cacheReadInputTokens", 0),
                ":cacheWrite": usage_delta.get("cacheWriteInputTokens", 0),
                ":savings": Decimal(str(cache_savings_delta)),
                ":now": timestamp,
                ":periodStart": f"{period}-01T00:00:00Z",
                ":periodEnd": f"{period}-31T23:59:59Z",
                ":userId": user_id,
                ":gsi2pk": f"PERIOD#{period}"
            }

            # Perform atomic increment and get new values
            response = self.cost_summary_table.update_item(
                Key=key,
                UpdateExpression=update_expression,
                ExpressionAttributeValues=expression_values,
                ReturnValues="UPDATED_NEW"
            )

            # Update GSI2SK with the new total cost for proper sorting
            # This is a separate update to get the accurate post-increment value
            new_total_cost = float(response.get("Attributes", {}).get("totalCost", 0))
            await self._update_cost_sort_key(user_id, period, new_total_cost)

            # Update per-model breakdown if model info is provided
            # This is a separate update to handle the nested map structure
            if model_id:
                await self._update_model_breakdown(
                    user_id=user_id,
                    period=period,
                    model_id=model_id,
                    model_name=model_name or model_id,
                    cost_delta=cost_delta,
                    usage_delta=usage_delta,
                    provider=provider or "unknown"
                )

        except ClientError as e:
            raise Exception(f"Failed to update user cost summary: {e}")

    async def _update_model_breakdown(
        self,
        user_id: str,
        period: str,
        model_id: str,
        model_name: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        provider: str = "unknown"
    ) -> None:
        """
        Update per-model breakdown in cost summary

        Uses a multi-step approach to handle nested map structure in DynamoDB:
        1. First ensure modelBreakdown map exists (separate update to avoid path overlap)
        2. Then ensure the specific model entry exists
        3. Finally, atomically increment the model's counters
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Sanitize model_id for use as a DynamoDB map key
            # Replace dots, colons, and hyphens with underscores for DynamoDB compatibility
            safe_model_id = model_id.replace(".", "_").replace(":", "_").replace("-", "_")

            key = {
                "PK": f"USER#{user_id}",
                "SK": f"PERIOD#{period}"
            }

            # Step 1: Ensure modelBreakdown map exists (if not, create it)
            # This is a separate update to avoid path overlap issues
            try:
                self.cost_summary_table.update_item(
                    Key=key,
                    UpdateExpression="SET #mb = if_not_exists(#mb, :empty_map)",
                    ExpressionAttributeNames={"#mb": "modelBreakdown"},
                    ExpressionAttributeValues={":empty_map": {}}
                )
            except ClientError as e:
                # If the item doesn't exist yet, that's fine - the main update created it
                if e.response['Error']['Code'] != 'ConditionalCheckFailedException':
                    logger.warning(f"Error ensuring modelBreakdown exists: {e}")

            # Step 2: Ensure the specific model entry exists with initial values
            try:
                self.cost_summary_table.update_item(
                    Key=key,
                    UpdateExpression="SET #mb.#model = if_not_exists(#mb.#model, :init_model)",
                    ExpressionAttributeNames={
                        "#mb": "modelBreakdown",
                        "#model": safe_model_id
                    },
                    ExpressionAttributeValues={
                        ":init_model": {
                            "modelName": model_name,
                            "provider": provider,
                            "cost": Decimal("0"),
                            "requests": 0,
                            "inputTokens": 0,
                            "outputTokens": 0,
                            "cacheReadTokens": 0,
                            "cacheWriteTokens": 0
                        }
                    }
                )
            except ClientError as e:
                logger.warning(f"Error initializing model entry: {e}")

            # Step 3: Atomically increment the model's counters
            self.cost_summary_table.update_item(
                Key=key,
                UpdateExpression="""
                    ADD #mb.#model.#cost :cost,
                        #mb.#model.#requests :one,
                        #mb.#model.#inputTokens :input,
                        #mb.#model.#outputTokens :output,
                        #mb.#model.#cacheReadTokens :cacheRead,
                        #mb.#model.#cacheWriteTokens :cacheWrite
                """,
                ExpressionAttributeNames={
                    "#mb": "modelBreakdown",
                    "#model": safe_model_id,
                    "#cost": "cost",
                    "#requests": "requests",
                    "#inputTokens": "inputTokens",
                    "#outputTokens": "outputTokens",
                    "#cacheReadTokens": "cacheReadTokens",
                    "#cacheWriteTokens": "cacheWriteTokens"
                },
                ExpressionAttributeValues={
                    ":cost": Decimal(str(cost_delta)),
                    ":one": 1,
                    ":input": usage_delta.get("inputTokens", 0),
                    ":output": usage_delta.get("outputTokens", 0),
                    ":cacheRead": usage_delta.get("cacheReadInputTokens", 0),
                    ":cacheWrite": usage_delta.get("cacheWriteInputTokens", 0)
                }
            )

            logger.debug(f"Updated model breakdown for {model_id} (key: {safe_model_id})")

        except ClientError as e:
            # Log but don't raise - model breakdown is supplementary data
            import logging
            logging.getLogger(__name__).error(
                f"Failed to update model breakdown for {model_id}: {e}"
            )

    async def _update_cost_sort_key(
        self,
        user_id: str,
        period: str,
        total_cost: float
    ) -> None:
        """
        Update GSI2SK (cost sort key) for PeriodCostIndex GSI

        The sort key is formatted as COST#{15-digit-zero-padded-cents} to enable
        proper string-based sorting. Higher costs sort later alphabetically,
        so we use ScanIndexForward=False to get descending order.

        Example: $125.50 → COST#000000000012550
        Max supported: $999,999,999,999.99 (15 digits in cents)
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Convert to cents and pad to 15 digits
            cost_cents = int(total_cost * 100)
            gsi2_sk = f"COST#{cost_cents:015d}"

            self.cost_summary_table.update_item(
                Key={
                    "PK": f"USER#{user_id}",
                    "SK": f"PERIOD#{period}"
                },
                UpdateExpression="SET GSI2SK = :gsi2sk",
                ExpressionAttributeValues={
                    ":gsi2sk": gsi2_sk
                }
            )

            logger.debug(f"Updated cost sort key for user {user_id}: {gsi2_sk}")

        except ClientError as e:
            # Log but don't raise - GSI update is supplementary
            logger.error(f"Failed to update cost sort key for {user_id}: {e}")

    async def get_user_messages_in_range(
        self,
        user_id: str,
        start_date: datetime,
        end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Get all message metadata for a user in a date range

        Uses GSI1 (UserTimestampIndex) for efficient time-range queries.
        """
        try:
            # Convert datetimes to ISO strings
            start_iso = start_date.isoformat()
            end_iso = end_date.isoformat()

            response = self.sessions_metadata_table.query(
                IndexName="UserTimestampIndex",
                KeyConditionExpression="GSI1PK = :pk AND GSI1SK BETWEEN :start AND :end",
                ExpressionAttributeValues={
                    ":pk": f"USER#{user_id}",
                    ":start": start_iso,
                    ":end": end_iso
                }
            )

            items = response.get("Items", [])

            # Handle pagination if needed
            while "LastEvaluatedKey" in response:
                response = self.sessions_metadata_table.query(
                    IndexName="UserTimestampIndex",
                    KeyConditionExpression="GSI1PK = :pk AND GSI1SK BETWEEN :start AND :end",
                    ExpressionAttributeValues={
                        ":pk": f"USER#{user_id}",
                        ":start": start_iso,
                        ":end": end_iso
                    },
                    ExclusiveStartKey=response["LastEvaluatedKey"]
                )
                items.extend(response.get("Items", []))

            # Convert Decimal to float, remove DynamoDB keys, and flatten for aggregation
            results = []
            for item in items:
                item_float = self._convert_decimal_to_float(item)

                # Remove DynamoDB-specific keys
                for key in ["PK", "SK", "GSI1PK", "GSI1SK", "GSI_PK", "GSI_SK", "ttl"]:
                    item_float.pop(key, None)

                # Flatten nested structures for aggregation compatibility
                # (matches local_file_storage.get_user_messages_in_range format)
                model_info = item_float.get("modelInfo", {})
                token_usage = item_float.get("tokenUsage", {})

                flattened = {
                    "cost": item_float.get("cost", 0.0),
                    "inputTokens": token_usage.get("inputTokens", 0),
                    "outputTokens": token_usage.get("outputTokens", 0),
                    "cacheReadTokens": token_usage.get("cacheReadInputTokens", 0),
                    "cacheWriteTokens": token_usage.get("cacheWriteInputTokens", 0),
                    "modelId": model_info.get("modelId", "unknown"),
                    "modelName": model_info.get("modelName", "Unknown"),
                    "provider": model_info.get("provider", "unknown"),
                    "pricingSnapshot": model_info.get("pricingSnapshot", {}),
                    "timestamp": item_float.get("timestamp", ""),
                    "sessionId": item_float.get("sessionId", ""),
                    "messageId": item_float.get("messageId", "")
                }
                results.append(flattened)

            return results

        except ClientError as e:
            raise Exception(f"Failed to get user messages in range: {e}")

    async def get_top_users_by_cost(
        self,
        period: str,
        limit: int = 100,
        min_cost: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Get top users by cost for a period using PeriodCostIndex GSI

        Uses GSI2 (PeriodCostIndex) for efficient sorted queries:
        - GSI2PK: PERIOD#{YYYY-MM}
        - GSI2SK: COST#{15-digit-zero-padded-cents}

        Args:
            period: The billing period (YYYY-MM format)
            limit: Maximum number of users to return (default 100, max 1000)
            min_cost: Optional minimum cost threshold in dollars

        Returns:
            List of user cost summaries sorted by cost descending
        """
        try:
            query_params = {
                "IndexName": "PeriodCostIndex",
                "KeyConditionExpression": "GSI2PK = :period",
                "ExpressionAttributeValues": {
                    ":period": f"PERIOD#{period}"
                },
                "ScanIndexForward": False,  # Descending order (highest cost first)
                "Limit": min(limit, 1000)
            }

            # Add minimum cost filter if specified
            if min_cost is not None:
                min_cost_cents = int(min_cost * 100)
                min_cost_key = f"COST#{min_cost_cents:015d}"
                query_params["KeyConditionExpression"] += " AND GSI2SK >= :min_cost"
                query_params["ExpressionAttributeValues"][":min_cost"] = min_cost_key

            response = self.cost_summary_table.query(**query_params)
            items = response.get("Items", [])

            # Convert Decimal to float and clean up response
            results = []
            for item in items:
                item_float = self._convert_decimal_to_float(item)
                # Remove DynamoDB-specific keys but keep userId
                for key in ["PK", "SK", "GSI2PK", "GSI2SK"]:
                    item_float.pop(key, None)
                results.append(item_float)

            return results

        except ClientError as e:
            raise Exception(f"Failed to get top users by cost: {e}")

    # ============================================================
    # SystemCostRollup Table Methods
    # ============================================================

    async def track_active_user(
        self,
        user_id: str,
        period: str,
        date: str
    ) -> tuple[bool, bool]:
        """
        Track a user as active for a period using conditional writes.

        Uses separate tracking items to avoid set size limits and enable
        scalability to millions of users. Items use conditional writes
        to ensure each user is only counted once per period.

        Schema:
            Daily tracking:
                PK: ACTIVE#DAILY#<YYYY-MM-DD>
                SK: <user_id>
                TTL: 90 days from creation

            Monthly tracking:
                PK: ACTIVE#MONTHLY#<YYYY-MM>
                SK: <user_id>
                TTL: 400 days from creation (keep ~13 months)

        Args:
            user_id: The user identifier
            period: Monthly period (YYYY-MM format)
            date: Daily date (YYYY-MM-DD format)

        Returns:
            Tuple of (is_new_user_today, is_new_user_this_month)
            True means this is the user's first request for that period.
        """
        import logging
        logger = logging.getLogger(__name__)

        is_new_today = False
        is_new_this_month = False

        now = datetime.now(timezone.utc)

        # Try to mark user as active today
        try:
            daily_ttl = int((now + timedelta(days=90)).timestamp())
            self.system_rollup_table.put_item(
                Item={
                    "PK": f"ACTIVE#DAILY#{date}",
                    "SK": user_id,
                    "trackedAt": now.isoformat(),
                    "TTL": daily_ttl
                },
                ConditionExpression="attribute_not_exists(PK)"
            )
            is_new_today = True
            logger.debug(f"Tracked new active user {user_id} for date {date}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # User already tracked today - this is expected
                logger.debug(f"User {user_id} already tracked for date {date}")
            else:
                logger.error(f"Failed to track daily active user: {e}")

        # Try to mark user as active this month
        try:
            monthly_ttl = int((now + timedelta(days=400)).timestamp())
            self.system_rollup_table.put_item(
                Item={
                    "PK": f"ACTIVE#MONTHLY#{period}",
                    "SK": user_id,
                    "trackedAt": now.isoformat(),
                    "TTL": monthly_ttl
                },
                ConditionExpression="attribute_not_exists(PK)"
            )
            is_new_this_month = True
            logger.debug(f"Tracked new active user {user_id} for period {period}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # User already tracked this month - this is expected
                logger.debug(f"User {user_id} already tracked for period {period}")
            else:
                logger.error(f"Failed to track monthly active user: {e}")

        return is_new_today, is_new_this_month

    async def track_active_user_for_model(
        self,
        user_id: str,
        period: str,
        model_id: str
    ) -> bool:
        """
        Track a user as active for a specific model in a period.

        Uses conditional writes to ensure each user is only counted once
        per model per period.

        Schema:
            PK: ACTIVE#MODEL#<YYYY-MM>#<sanitized_model_id>
            SK: <user_id>
            TTL: 400 days from creation

        Args:
            user_id: The user identifier
            period: Monthly period (YYYY-MM format)
            model_id: The model identifier

        Returns:
            True if this is the user's first request for this model this period.
        """
        import logging
        logger = logging.getLogger(__name__)

        # Sanitize model_id for partition key (same logic as update_model_rollup)
        safe_model_id = model_id.replace(".", "_").replace(":", "_").replace("-", "_")

        now = datetime.now(timezone.utc)

        try:
            monthly_ttl = int((now + timedelta(days=400)).timestamp())
            self.system_rollup_table.put_item(
                Item={
                    "PK": f"ACTIVE#MODEL#{period}#{safe_model_id}",
                    "SK": user_id,
                    "trackedAt": now.isoformat(),
                    "TTL": monthly_ttl
                },
                ConditionExpression="attribute_not_exists(PK)"
            )
            logger.debug(f"Tracked new active user {user_id} for model {model_id} in {period}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                # User already tracked for this model this month
                logger.debug(f"User {user_id} already tracked for model {model_id} in {period}")
                return False
            else:
                logger.error(f"Failed to track model active user: {e}")
                return False

    async def update_daily_rollup(
        self,
        date: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        is_new_user: bool = False,
        model_id: Optional[str] = None
    ) -> None:
        """
        Update daily system-wide cost rollup (atomic increment)

        Schema:
            PK: ROLLUP#DAILY
            SK: <YYYY-MM-DD>

        Args:
            date: The date (YYYY-MM-DD format)
            cost_delta: Cost to add to daily total
            usage_delta: Token counts to add
            is_new_user: Whether this is the user's first request today
            model_id: Model identifier for tracking active users per model
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Atomic increment of daily totals
            update_expression = """
                ADD totalCost :cost,
                    totalRequests :one,
                    totalInputTokens :input,
                    totalOutputTokens :output,
                    totalCacheReadTokens :cacheRead,
                    totalCacheWriteTokens :cacheWrite
                SET lastUpdated = :now,
                    #type = :type
            """

            expression_values = {
                ":cost": Decimal(str(cost_delta)),
                ":one": 1,
                ":input": usage_delta.get("inputTokens", 0),
                ":output": usage_delta.get("outputTokens", 0),
                ":cacheRead": usage_delta.get("cacheReadInputTokens", 0),
                ":cacheWrite": usage_delta.get("cacheWriteInputTokens", 0),
                ":now": datetime.now(timezone.utc).isoformat(),
                ":type": "daily"
            }

            # Track active users (increment only if new user today)
            if is_new_user:
                update_expression = update_expression.replace(
                    "ADD totalCost :cost",
                    "ADD totalCost :cost, activeUsers :one"
                )

            self.system_rollup_table.update_item(
                Key={
                    "PK": "ROLLUP#DAILY",
                    "SK": date
                },
                UpdateExpression=update_expression,
                ExpressionAttributeNames={"#type": "type"},
                ExpressionAttributeValues=expression_values
            )

            logger.debug(f"Updated daily rollup for {date}")

        except ClientError as e:
            logger.error(f"Failed to update daily rollup: {e}")
            # Don't raise - rollup updates are supplementary

    async def update_monthly_rollup(
        self,
        period: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        cache_savings_delta: float = 0.0,
        is_new_user: bool = False,
        model_id: Optional[str] = None
    ) -> None:
        """
        Update monthly system-wide cost rollup (atomic increment)

        Schema:
            PK: ROLLUP#MONTHLY
            SK: <YYYY-MM>

        Args:
            period: The month (YYYY-MM format)
            cost_delta: Cost to add to monthly total
            usage_delta: Token counts to add
            cache_savings_delta: Cache savings to add
            is_new_user: Whether this is the user's first request this month
            model_id: Model identifier for model breakdown
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            update_expression = """
                ADD totalCost :cost,
                    totalRequests :one,
                    totalInputTokens :input,
                    totalOutputTokens :output,
                    totalCacheReadTokens :cacheRead,
                    totalCacheWriteTokens :cacheWrite,
                    totalCacheSavings :savings
                SET lastUpdated = :now,
                    #type = :type
            """

            expression_values = {
                ":cost": Decimal(str(cost_delta)),
                ":one": 1,
                ":input": usage_delta.get("inputTokens", 0),
                ":output": usage_delta.get("outputTokens", 0),
                ":cacheRead": usage_delta.get("cacheReadInputTokens", 0),
                ":cacheWrite": usage_delta.get("cacheWriteInputTokens", 0),
                ":savings": Decimal(str(cache_savings_delta)),
                ":now": datetime.now(timezone.utc).isoformat(),
                ":type": "monthly"
            }

            # Track active users (increment only if new user this month)
            if is_new_user:
                update_expression = update_expression.replace(
                    "ADD totalCost :cost",
                    "ADD totalCost :cost, activeUsers :one"
                )

            self.system_rollup_table.update_item(
                Key={
                    "PK": "ROLLUP#MONTHLY",
                    "SK": period
                },
                UpdateExpression=update_expression,
                ExpressionAttributeNames={"#type": "type"},
                ExpressionAttributeValues=expression_values
            )

            logger.debug(f"Updated monthly rollup for {period}")

        except ClientError as e:
            logger.error(f"Failed to update monthly rollup: {e}")

    async def update_model_rollup(
        self,
        period: str,
        model_id: str,
        model_name: str,
        provider: str,
        cost_delta: float,
        usage_delta: Dict[str, int],
        is_new_user_for_model: bool = False
    ) -> None:
        """
        Update per-model cost rollup (atomic increment)

        Schema:
            PK: ROLLUP#MODEL
            SK: <YYYY-MM>#<model_id>

        Args:
            period: The month (YYYY-MM format)
            model_id: Model identifier
            model_name: Human-readable model name
            provider: LLM provider
            cost_delta: Cost to add
            usage_delta: Token counts to add
            is_new_user_for_model: Whether this is the user's first request for this model
        """
        import logging
        logger = logging.getLogger(__name__)

        try:
            # Sanitize model_id for sort key
            safe_model_id = model_id.replace(".", "_").replace(":", "_").replace("-", "_")

            update_expression = """
                ADD totalCost :cost,
                    totalRequests :one,
                    totalInputTokens :input,
                    totalOutputTokens :output
                SET lastUpdated = :now,
                    modelId = :modelId,
                    modelName = :modelName,
                    provider = :provider,
                    #type = :type
            """

            expression_values = {
                ":cost": Decimal(str(cost_delta)),
                ":one": 1,
                ":input": usage_delta.get("inputTokens", 0),
                ":output": usage_delta.get("outputTokens", 0),
                ":now": datetime.now(timezone.utc).isoformat(),
                ":modelId": model_id,
                ":modelName": model_name,
                ":provider": provider,
                ":type": "model"
            }

            if is_new_user_for_model:
                update_expression = update_expression.replace(
                    "ADD totalCost :cost",
                    "ADD totalCost :cost, uniqueUsers :one"
                )

            self.system_rollup_table.update_item(
                Key={
                    "PK": "ROLLUP#MODEL",
                    "SK": f"{period}#{safe_model_id}"
                },
                UpdateExpression=update_expression,
                ExpressionAttributeNames={"#type": "type"},
                ExpressionAttributeValues=expression_values
            )

            logger.debug(f"Updated model rollup for {model_id} in {period}")

        except ClientError as e:
            logger.error(f"Failed to update model rollup: {e}")

    async def get_system_summary(
        self,
        period: str,
        period_type: str = "monthly"
    ) -> Optional[Dict[str, Any]]:
        """
        Get system-wide cost summary for a period

        Args:
            period: The period (YYYY-MM for monthly, YYYY-MM-DD for daily)
            period_type: Either "daily" or "monthly"

        Returns:
            System cost summary or None if not found
        """
        try:
            pk = f"ROLLUP#{period_type.upper()}"

            response = self.system_rollup_table.get_item(
                Key={
                    "PK": pk,
                    "SK": period
                }
            )

            if "Item" not in response:
                return None

            item = self._convert_decimal_to_float(response["Item"])

            # Remove DynamoDB keys
            for key in ["PK", "SK"]:
                item.pop(key, None)

            return item

        except ClientError as e:
            raise Exception(f"Failed to get system summary: {e}")

    async def get_daily_trends(
        self,
        start_date: str,
        end_date: str
    ) -> List[Dict[str, Any]]:
        """
        Get daily cost trends for a date range

        Args:
            start_date: Start date (YYYY-MM-DD)
            end_date: End date (YYYY-MM-DD)

        Returns:
            List of daily rollups sorted by date
        """
        try:
            response = self.system_rollup_table.query(
                KeyConditionExpression="PK = :pk AND SK BETWEEN :start AND :end",
                ExpressionAttributeValues={
                    ":pk": "ROLLUP#DAILY",
                    ":start": start_date,
                    ":end": end_date
                },
                ScanIndexForward=True  # Ascending order (earliest first)
            )

            items = response.get("Items", [])

            results = []
            for item in items:
                item_float = self._convert_decimal_to_float(item)
                # Add date field from SK
                item_float["date"] = item_float.pop("SK", "")
                item_float.pop("PK", None)
                results.append(item_float)

            return results

        except ClientError as e:
            raise Exception(f"Failed to get daily trends: {e}")

    async def get_model_usage(
        self,
        period: str
    ) -> List[Dict[str, Any]]:
        """
        Get per-model usage for a period

        Args:
            period: The month (YYYY-MM format)

        Returns:
            List of model usage summaries sorted by cost descending
        """
        try:
            response = self.system_rollup_table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :period)",
                ExpressionAttributeValues={
                    ":pk": "ROLLUP#MODEL",
                    ":period": f"{period}#"
                }
            )

            items = response.get("Items", [])

            results = []
            for item in items:
                item_float = self._convert_decimal_to_float(item)
                item_float.pop("PK", None)
                item_float.pop("SK", None)
                results.append(item_float)

            # Sort by cost descending
            results.sort(key=lambda x: x.get("totalCost", 0), reverse=True)

            return results

        except ClientError as e:
            raise Exception(f"Failed to get model usage: {e}")
