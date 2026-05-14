export type UserMenuLinkKind = 'external' | 'modal';

export interface UserMenuLink {
  link_id: string;
  label: string;
  kind: UserMenuLinkKind;
  enabled: boolean;
  order: number;
  url?: string | null;
  body_markdown?: string | null;
  created_at: string;
  updated_at: string;
  created_by?: string | null;
}

export interface UserMenuLinksListResponse {
  links: UserMenuLink[];
  total: number;
}

export interface UserMenuLinkFormData {
  label: string;
  kind: UserMenuLinkKind;
  enabled: boolean;
  order: number;
  url?: string | null;
  body_markdown?: string | null;
}
