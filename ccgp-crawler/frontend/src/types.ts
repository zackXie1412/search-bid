export type JobStatus =
  | 'queued'
  | 'running'
  | 'stopping'
  | 'stopped'
  | 'blocked'
  | 'failed'
  | 'completed'

export interface CrawlSlice {
  job_id: string
  slice_date: string
  status: JobStatus
  total_pages: number | null
  next_page: number
  pages_completed: number
  error_message: string | null
}

export interface CrawlJob {
  id: string
  job_type: 'manual' | 'scheduled'
  start_date: string
  end_date: string
  status: JobStatus
  current_date: string | null
  current_page: number | null
  current_total_pages: number | null
  current_title: string | null
  dates_total: number
  dates_completed: number
  pages_total: number
  pages_completed: number
  new_count: number
  duplicate_count: number
  parse_error_count: number
  filtered_count: number
  error_message: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  slices?: CrawlSlice[]
}

export interface Notice {
  canonical_url: string
  title: string
  published_at: string
  region_raw: string | null
  region_code: string | null
  category: 'zygg' | 'dfgg'
  source_list_url: string
  is_new: 0 | 1
}

export interface SystemStatus {
  service: string
  worker: 'running' | 'stopped'
  sqlite: string
  chrome: string
  source: string
  database_path: string
  throttle: ThrottleStatus
}

export interface ThrottleStatus {
  state: 'ready' | 'waiting' | 'cooldown' | 'daily_limit'
  day_key: string
  day_request_count: number
  daily_limit: number
  batch_request_count: number
  next_request_at: string | null
  next_job_at: string | null
  cooldown_until: string | null
  wait_until: string | null
  active_job_id: string | null
  resume_required: 0 | 1
  confirmed_job_id: string | null
  pause_resume_job_id: string | null
}

export interface ScheduleStatus {
  enabled: boolean
  run_time: string
  timezone: string
  last_success_date: string | null
  next_run_at: string | null
}

export type SyncConnectionState = 'connected' | 'unavailable' | 'unauthorized' | 'error' | 'not_configured'

export interface SyncRun {
  status: 'idle' | 'running' | 'completed' | 'failed'
  current_batch_key: string | null
  current_batch_size: number
  current_batch_completed: number
  batches_completed: number
  synced: number
  inserted: number
  updated: number
  replayed_batches: number
  started_at: string | null
  finished_at: string | null
  last_error: string | null
}

export interface SyncStatus {
  connection: {
    state: SyncConnectionState
    message: string
  }
  counts: {
    pending: number
    syncing: number
    synced: number
    failed: number
    total: number
    synced_batches: number
    failed_batches: number
    last_success_at: string | null
    last_error: string | null
  }
  run: SyncRun
}
