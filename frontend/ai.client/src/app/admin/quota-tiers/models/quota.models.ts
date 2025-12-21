/**
 * TypeScript models for quota management.
 * Mirrors backend Pydantic models.
 */

// ========== Enums ==========

export enum QuotaAssignmentType {
  DIRECT_USER = 'direct_user',
  JWT_ROLE = 'jwt_role',
  EMAIL_DOMAIN = 'email_domain',
  DEFAULT_TIER = 'default_tier',
}

export enum QuotaEventType {
  WARNING = 'warning',
  BLOCK = 'block',
  RESET = 'reset',
  OVERRIDE_APPLIED = 'override_applied',
}

export type PeriodType = 'daily' | 'monthly';
export type ActionOnLimit = 'block' | 'warn';
export type WarningLevel = 'none' | '80%' | '90%';
export type OverrideType = 'custom_limit' | 'unlimited';

// ========== Quota Tier Models ==========

export interface QuotaTier {
  tierId: string;
  tierName: string;
  description?: string;

  // Limits
  monthlyCostLimit: number;
  dailyCostLimit?: number;
  periodType: PeriodType;

  // Soft limits
  softLimitPercentage: number;
  actionOnLimit: ActionOnLimit;

  // Metadata
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
}

export interface QuotaTierCreate {
  tierId: string;
  tierName: string;
  description?: string;
  monthlyCostLimit: number;
  dailyCostLimit?: number;
  periodType: PeriodType;
  softLimitPercentage?: number;
  actionOnLimit?: ActionOnLimit;
  enabled?: boolean;
}

export interface QuotaTierUpdate {
  tierName?: string;
  description?: string;
  monthlyCostLimit?: number;
  dailyCostLimit?: number;
  periodType?: PeriodType;
  softLimitPercentage?: number;
  actionOnLimit?: ActionOnLimit;
  enabled?: boolean;
}

// ========== Quota Assignment Models ==========

export interface QuotaAssignment {
  assignmentId: string;
  tierId: string;
  assignmentType: QuotaAssignmentType;

  // Conditional fields
  userId?: string;
  jwtRole?: string;
  emailDomain?: string;

  priority: number;
  enabled: boolean;
  createdAt: string;
  updatedAt: string;
  createdBy: string;
}

export interface QuotaAssignmentCreate {
  tierId: string;
  assignmentType: QuotaAssignmentType;
  userId?: string;
  jwtRole?: string;
  emailDomain?: string;
  priority?: number;
  enabled?: boolean;
}

export interface QuotaAssignmentUpdate {
  tierId?: string;
  priority?: number;
  enabled?: boolean;
}

// ========== Quota Override Models ==========

export interface QuotaOverride {
  overrideId: string;
  userId: string;
  overrideType: OverrideType;

  monthlyCostLimit?: number;
  dailyCostLimit?: number;

  validFrom: string;
  validUntil: string;

  reason: string;
  createdBy: string;
  createdAt: string;
  enabled: boolean;
}

export interface QuotaOverrideCreate {
  userId: string;
  overrideType: OverrideType;
  monthlyCostLimit?: number;
  dailyCostLimit?: number;
  validFrom: string;
  validUntil: string;
  reason: string;
}

export interface QuotaOverrideUpdate {
  validUntil?: string;
  enabled?: boolean;
  reason?: string;
}

// ========== Quota Event Models ==========

export interface QuotaEvent {
  eventId: string;
  userId: string;
  tierId: string;
  eventType: QuotaEventType;

  currentUsage: number;
  quotaLimit: number;
  percentageUsed: number;

  timestamp: string;
  metadata?: Record<string, any>;
}

// ========== User Quota Info (Inspector) ==========

export interface UserQuotaInfo {
  userId: string;
  email: string;
  roles: string[];

  tier?: QuotaTier;
  assignment?: QuotaAssignment;
  override?: QuotaOverride;
  matchedBy?: string;

  currentPeriod: string;
  currentUsage: number;
  quotaLimit?: number;
  percentageUsed: number;
  remaining?: number;

  recentBlocks: number;
  lastBlockTime?: string;
}

// ========== Helper Types ==========

export interface QuotaCheckResult {
  allowed: boolean;
  message: string;
  tier?: QuotaTier;
  currentUsage: number;
  quotaLimit?: number;
  percentageUsed: number;
  remaining?: number;
  warningLevel?: WarningLevel;
}
