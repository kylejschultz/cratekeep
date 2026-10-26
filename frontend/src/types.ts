export type Bootstrap = {
  page: "overview" | "inbox" | "library-import" | "library" | "settings";
  buildSha: string;
  threshold: number;
  toastDurationSeconds?: number;
  data: Record<string, any>;
};

export type Candidate = {
  id: number | string;
  confidence?: number;
  artist?: string;
  album?: string;
  year?: number | string;
  provider_id?: string;
  genre?: string;
  release_type?: string;
  country?: string;
  artwork_url?: string;
  media?: Array<string | { format?: string; [key: string]: unknown }>;
  recordings?: Array<Record<string, unknown>>;
  matched_track_count?: number;
  unmatched_track_count?: number;
  proposed_diff?: Record<string, { from?: unknown; to?: unknown }>;
  track_details?: Array<{
    status?: string;
    local?: unknown;
    proposed?: unknown;
    local_artist?: unknown;
    proposed_artist?: unknown;
    local_position?: unknown;
    proposed_position?: unknown;
    [key: string]: unknown;
  }>;
  duplicate_preflight?: DuplicatePreflight;
};

export type DuplicatePreflight = {
  status: "clear" | "possible" | "ambiguous";
  message?: string;
  warnings?: Array<{
    incoming_path?: string;
    managed_paths?: string[];
    ambiguous?: boolean;
  }>;
};

export type Album = {
  id: number;
  name?: string;
  album?: string;
  artist?: string;
  albumartist?: string;
  path?: string;
  relative_path?: string;
  file_count?: number;
  track_count?: number;
  files?: Array<Record<string, any>>;
  tracks?: Array<Record<string, any>>;
  candidates?: Candidate[];
  proposed_match?: Candidate | null;
  selected_candidate_id?: number | string | null;
  candidate_status?: string;
  candidate_error?: string;
  execution_status?: string;
  execution_error?: string;
  duplicate_action?: string;
  duplicate_preflight?: DuplicatePreflight;
  [key: string]: any;
};

export type QueueScope = {
  id?: string;
  name?: string;
  path?: string;
  folder?: string;
  album_count?: number;
  file_count?: number;
  status?: string;
  error?: string;
  [key: string]: any;
};
