"""Sync-policy API request/response models"""

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from apis.shared.sync_policies.models import SyncInterval, SyncPolicy, SyncPolicyState, SyncSourceType


class CreateSyncPolicyRequest(BaseModel):
    """Request body for creating a sync policy on a content source"""

    model_config = ConfigDict(populate_by_name=True)

    source_type: SyncSourceType = Field(..., alias="sourceType", description="Kind of content source")
    source_ref: str = Field(
        ...,
        alias="sourceRef",
        min_length=1,
        max_length=128,
        description="drive_file: document id; web_crawl: crawl id",
    )
    interval: SyncInterval = Field(..., description="Re-sync cadence")


class UpdateSyncPolicyRequest(BaseModel):
    """Request body for changing a policy's interval or pausing/resuming.

    `state` accepts only the user-owned transitions: "paused_user" (pause)
    and "active" (resume). paused_reauth resumes only via a fresh OAuth
    consent; the other paused states resume here too since resuming is an
    explicit user decision.
    """

    model_config = ConfigDict(populate_by_name=True)

    interval: Optional[SyncInterval] = Field(None, description="New re-sync cadence")
    state: Optional[Literal["active", "paused_user"]] = Field(None, description="Pause or resume the policy")


class SyncPolicyResponse(BaseModel):
    """Public view of a sync policy"""

    model_config = ConfigDict(populate_by_name=True)

    policy_id: str = Field(..., alias="policyId")
    assistant_id: str = Field(..., alias="assistantId")
    source_type: SyncSourceType = Field(..., alias="sourceType")
    source_ref: str = Field(..., alias="sourceRef")
    interval: SyncInterval
    state: SyncPolicyState
    state_reason: Optional[str] = Field(None, alias="stateReason")
    next_sync_at: Optional[str] = Field(None, alias="nextSyncAt")
    last_sync_at: Optional[str] = Field(None, alias="lastSyncAt")
    last_result: Optional[str] = Field(None, alias="lastResult")
    created_at: str = Field(..., alias="createdAt")
    updated_at: str = Field(..., alias="updatedAt")

    @classmethod
    def from_policy(cls, policy: SyncPolicy) -> "SyncPolicyResponse":
        return cls(
            policy_id=policy.policy_id,
            assistant_id=policy.assistant_id,
            source_type=policy.source_type,
            source_ref=policy.source_ref,
            interval=policy.interval,
            state=policy.state,
            state_reason=policy.state_reason,
            next_sync_at=policy.next_sync_at,
            last_sync_at=policy.last_sync_at,
            last_result=policy.last_result,
            created_at=policy.created_at,
            updated_at=policy.updated_at,
        )


class SyncPoliciesListResponse(BaseModel):
    """Response for listing an assistant's sync policies"""

    model_config = ConfigDict(populate_by_name=True)

    policies: List[SyncPolicyResponse] = Field(..., description="Sync policies for the assistant")
