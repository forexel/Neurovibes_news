export type ArticleStatus =
  | "new"
  | "inbox"
  | "review"
  | "double"
  | "scored"
  | "selected_hourly"
  | "ready"
  | "published"
  | "archived"
  | "rejected";

export interface SetupState {
  email: string;
  channel_name: string;
  channel_theme: string;
  audience_description: string;
  openrouter_api_key_set: boolean;
  telegram_bot_token_set: boolean;
  telegram_review_chat_id: string;
  telegram_channel_id: string;
  telegram_signature: string;
  timezone_name: string;
  onboarding_step: number;
  onboarding_completed: boolean;
}

export interface ArticleListItem {
  id: number;
  status: ArticleStatus;
  content_mode?: string;
  title: string;
  ru_title?: string | null;
  subtitle?: string | null;
  short_hook?: string | null;
  source_id: number;
  source_name?: string | null;
  published_at?: string | null;
  created_at?: string | null;
  score_10?: number | null;
  final_score?: number | null;
  canonical_url: string;
  generated_image_path?: string | null;
  scheduled_publish_at?: string | null;
  ml_recommendation?: "publish_candidate" | "delete_candidate" | "review" | "unknown" | null;
  ml_recommendation_confidence?: number | null;
  ml_recommendation_reason?: string | null;
  ml_model_version?: string | null;
  ml_recommendation_at?: string | null;
  is_selected_day?: boolean;
  archived_kind?: string | null;
  archived_reason?: string | null;
  archived_at?: string | null;
  ml_verdict_confirmed?: boolean | null;
  ml_verdict_comment?: string | null;
  ml_verdict_tags?: string[] | null;
  ml_verdict_updated_at?: string | null;
  english_preview?: string | null;
}

export interface ArticleListResponse {
  items: ArticleListItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
  view: string;
  q: string;
}

export interface ArticleDetails extends ArticleListItem {
  title: string;
  text?: string | null;
  ru_summary?: string | null;
  short_hook?: string | null;
  image?: string | null;
  image_web?: string | null;
  score?: number | null;
  score_reasoning?: string | null;
  post_preview?: string | null;
  image_prompt?: string | null;
  feedback?: string | null;
  embedding_preview?: number[] | null;
  embedding_dim?: number | null;
  article_vector_model?: string | null;
  ml_verdict_confirmed?: boolean | null;
  ml_verdict_comment?: string | null;
  ml_verdict_tags?: string[] | null;
  ml_verdict_updated_at?: string | null;
}

export interface SourceItem {
  id: number;
  name: string;
  rss_url: string;
  kind: "rss" | "html";
  priority_rank: number;
  is_active: boolean;
  articles_count: number;
  latest_published_at?: string | null;
}

export interface ScoreParameter {
  id: number;
  key: string;
  title: string;
  description: string;
  weight: number;
  influence_rule: string;
  is_active: boolean;
}

export interface RuntimeSetting {
  id: number;
  key: string;
  value: string;
  scope: "global" | "topic";
  topic_key?: string | null;
}

export interface WorkerStatus {
  ok: boolean;
  tz: string;
  now_utc: string;
  worker_last_cycle_start_utc?: string;
  worker_last_cycle_finish_utc?: string;
  worker_next_cycle_utc?: string;
  worker_cycle_state?: string;
  worker_last_cycle_error?: string;
}

export interface AggregateJobStatus {
  job_id: string;
  status: string;
  period?: "hour" | "day" | "week" | "month";
  stage?: string | null;
  stage_detail?: string | null;
  processed?: number;
  total?: number;
  eta_seconds?: number | null;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  result?: {
    ok?: boolean;
    period?: string;
    inserted_total?: number;
    by_source?: Record<string, number>;
    dedup_processed?: number;
    enrich_summary_only?: number;
    scored?: number;
  } | null;
}

export interface TelegramReviewJob {
  id: number;
  article_id?: number | null;
  chat_id?: string | null;
  review_message_id?: string | null;
  status?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
}

export interface CostSummary {
  ok: boolean;
  estimated_cost_usd_total: number;
  estimated_cost_usd_24h: number;
  estimated_cost_usd_7d: number;
  estimated_cost_usd_30d: number;
  note?: string;
}

export interface EvaluationOverview {
  ok: boolean;
  days: number;
  k: number;
  retrieval: {
    eval_items: number;
    hit_at_k: number | null;
    precision_at_k: number | null;
    mrr: number | null;
    ndcg_at_k: number | null;
  };
  answer_relevance: number | null;
  source_faithfulness: number | null;
  latency: {
    avg_ms: number | null;
    p95_ms: number | null;
    sample_size: number;
  };
  cost: {
    total_usd: number;
    calls: number;
    cost_per_request_usd: number | null;
  };
  confusion: {
    publish_threshold: number;
    tp: number;
    fp: number;
    tn: number;
    fn: number;
    fp_examples: Array<{ event_id: number; article_id: number; title: string; decision: string; ml_score: number }>;
    fn_examples: Array<{ event_id: number; article_id: number; title: string; decision: string; ml_score: number }>;
  };
  miss_examples: Array<{ decision_id: number; article_id: number; chosen_rank: number | null; top_article_id: number | null }>;
}

export interface EvaluationVersions {
  ok: boolean;
  days: number;
  publish_threshold: number;
  versions: Array<{
    model_version: string;
    events: number;
    positive_events: number;
    positive_rate: number | null;
    tp: number;
    fp: number;
    tn: number;
    fn: number;
    precision: number | null;
    recall: number | null;
    avg_ml_score: number | null;
  }>;
  artifacts: Array<{
    name: string;
    version: string;
    active: boolean;
    created_at: string;
    metrics: Record<string, unknown>;
  }>;
}

export interface EvalSetResponse {
  ok: boolean;
  days: number;
  count: number;
  items: Array<{
    decision_id: number;
    created_at: string;
    chosen_article_id: number;
    rejected_article_ids: number[];
    decision_mode: string | null;
    selector_kind: string | null;
    confidence: number | null;
    candidates: Array<Record<string, unknown>>;
  }>;
}

export interface ReasonTagOption {
  value: string;
  label: string;
}

export class ApiError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

const REQUEST_TIMEOUT_MS = 45_000;

async function fetchWithTimeout(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const timeoutController = new AbortController();
  const timeoutId = window.setTimeout(() => timeoutController.abort(), REQUEST_TIMEOUT_MS);
  const externalSignal = init?.signal;

  if (externalSignal?.aborted) {
    window.clearTimeout(timeoutId);
    throw new DOMException("The operation was aborted.", "AbortError");
  }

  const abortForwarder = () => timeoutController.abort();
  if (externalSignal) {
    externalSignal.addEventListener("abort", abortForwarder, { once: true });
  }

  try {
    return await fetch(input, { ...init, signal: timeoutController.signal });
  } finally {
    if (externalSignal) {
      externalSignal.removeEventListener("abort", abortForwarder);
    }
    window.clearTimeout(timeoutId);
  }
}

async function requestJson<T>(url: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetchWithTimeout(url, init);
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(408, "Сервер не ответил вовремя. Повтори действие.");
    }
    throw err;
  }
  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    const rawDetail =
      typeof payload === "string"
        ? payload
        : String(payload?.detail || payload?.error || `Request failed: ${response.status}`);
    const detail = rawDetail === "delete_conflict_retry" ? "Статья сейчас занята другим процессом. Повтори удаление через пару секунд." : rawDetail;
    throw new ApiError(response.status, detail);
  }

  if (payload && typeof payload === "object" && "ok" in (payload as Record<string, unknown>) && (payload as Record<string, unknown>).ok === false) {
    const data = payload as Record<string, unknown>;
    throw new ApiError(response.status, String(data.hint || data.error || data.detail || "Action failed"));
  }

  return payload as T;
}

async function postForm(url: string, params: Record<string, string>) {
  const body = new URLSearchParams(params);
  try {
    return await fetchWithTimeout(url, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
      credentials: "same-origin",
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") {
      throw new ApiError(408, "Сервер не ответил вовремя. Повтори действие.");
    }
    throw err;
  }
}

export const api = {
  async login(login: string, password: string) {
    const response = await postForm("/login", { login, password });
    if (response.redirected) {
      try {
        const target = new URL(response.url);
        if (target.pathname === "/login") {
          throw new ApiError(401, "Неверный email или пароль.");
        }
        return { redirectTo: target.pathname || "/" };
      } catch {
        // Ignore URL parsing issues and fall back to setup-state check.
      }
    }
    return { redirectTo: "/" };
  },

  async register(login: string, password: string) {
    await postForm("/register", { login, password });
  },

  getSetupState() {
    return requestJson<SetupState>("/setup/state");
  },

  saveSetupStep1(body: {
    channel_name: string;
    channel_theme: string;
    openrouter_api_key?: string;
  }) {
    return requestJson<{ ok: boolean; step: number }>("/setup/step1", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, sources_text: "" }),
    });
  },

  saveSetupStep2(body: { audience_description: string }) {
    return requestJson<{ ok: boolean }>("/setup/step2/save", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  analyzeSetupStep2(body: { audience_description: string }) {
    return requestJson<{ ok: boolean; params: ScoreParameter[] }>("/setup/step2/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  saveTelegramSettings(body: {
    telegram_bot_token?: string;
    telegram_review_chat_id: string;
    telegram_channel_id: string;
    telegram_signature: string;
    timezone_name: string;
  }) {
    return requestJson<{ ok: boolean }>("/setup/telegram", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  completeSetup() {
    return requestJson<Record<string, unknown>>("/setup/complete", { method: "POST" });
  },

  listArticles(params: Record<string, string | number | boolean>) {
    const qs = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      qs.set(key, String(value));
    });
    return requestJson<ArticleListResponse>(`/admin-data/articles?${qs.toString()}`);
  },

  getArticle(id: number) {
    return requestJson<ArticleDetails>(`/articles/${id}`);
  },

  postArticleAction<T = Record<string, unknown>>(id: number, path: string, body?: unknown) {
    return requestJson<T>(`/articles/${id}/${path}`, {
      method: "POST",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
  },

  deleteArticle(id: number, reason: string) {
    return requestJson<Record<string, unknown>>(`/articles/${id}`, {
      method: "DELETE",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ reason }),
    });
  },

  saveMlVerdict(id: number, body: { confirmed: boolean; comment?: string; tags?: string[] }) {
    return requestJson<{ ok: boolean; confirmed: boolean; comment: string; tags: string[] }>(`/articles/${id}/ml-verdict`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  getReasonTags() {
    return requestJson<{ ok: boolean; items: ReasonTagOption[] }>("/reason-tags");
  },

  getCosts() {
    return requestJson<CostSummary>("/admin-data/costs");
  },

  getEvaluationOverview(days = 14, k = 5) {
    return requestJson<EvaluationOverview>(`/admin-data/evaluation?days=${encodeURIComponent(String(days))}&k=${encodeURIComponent(String(k))}`);
  },

  getEvaluationVersions(days = 30) {
    return requestJson<EvaluationVersions>(`/admin-data/evaluation/versions?days=${encodeURIComponent(String(days))}`);
  },

  getEvaluationEvalSet(days = 30, limit = 500) {
    return requestJson<EvalSetResponse>(
      `/admin-data/evaluation/eval-set?days=${encodeURIComponent(String(days))}&limit=${encodeURIComponent(String(limit))}`,
    );
  },

  getWorkerStatus() {
    return requestJson<WorkerStatus>("/admin-data/worker-status");
  },

  refreshMlRecommendations(limit = 2000, onlyMissing = true) {
    return requestJson<{ ok: boolean; scanned: number; updated: number }>(
      `/admin-actions/ml-recommendations/refresh?limit=${encodeURIComponent(String(limit))}&only_missing=${onlyMissing ? "1" : "0"}`,
      {
        method: "POST",
      },
    );
  },

  getSources() {
    return requestJson<SourceItem[]>("/admin-data/sources");
  },

  addSource(body: { name: string; rss_url: string; priority_rank: number; kind: "rss" | "html" }) {
    return requestJson<{ ok: boolean; source_id: number }>("/sources/add", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  updateSource(
    id: number,
    body: { name: string; rss_url: string; priority_rank: number; kind: "rss" | "html"; is_active?: boolean },
  ) {
    return requestJson<{ ok: boolean; source_id: number }>(`/sources/${id}/update`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  setSourceActive(id: number, is_active: boolean) {
    return requestJson<{ ok: boolean }>(`/sources/${id}/active`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ is_active }),
    });
  },

  checkSource(id: number) {
    return requestJson<Record<string, unknown>>(`/sources/${id}/check`, { method: "POST" });
  },

  deleteSource(id: number) {
    return requestJson<Record<string, unknown>>(`/sources/${id}`, { method: "DELETE" });
  },

  getScoreParameters() {
    return requestJson<ScoreParameter[]>("/admin-data/score-params");
  },

  upsertScoreParameter(body: {
    key: string;
    title: string;
    description: string;
    weight: number;
    influence_rule: string;
    is_active: boolean;
  }) {
    return requestJson<{ ok: boolean; id: number }>("/score-params/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  deleteScoreParameter(id: number) {
    return requestJson<{ ok: boolean }>(`/score-params/${id}`, { method: "DELETE" });
  },

  getRuntimeSettings() {
    return requestJson<{ ok: boolean; items: RuntimeSetting[]; defaults: Record<string, string> }>(
      "/admin-data/runtime-settings",
    );
  },

  upsertRuntimeSetting(body: {
    key: string;
    value: string;
    scope: "global" | "topic";
    topic_key?: string | null;
  }) {
    return requestJson<{ ok: boolean; item: RuntimeSetting }>("/runtime-settings/upsert", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  },

  deleteRuntimeSetting(id: number) {
    return requestJson<{ ok: boolean }>(`/runtime-settings/${id}`, { method: "DELETE" });
  },

  telegramTest() {
    return requestJson<Record<string, unknown>>("/telegram/test", { method: "POST" });
  },

  telegramPoll() {
    return requestJson<Record<string, unknown>>("/telegram/review/poll", { method: "POST" });
  },

  telegramHourlyBackfill(hours: number, limit: number, force: boolean) {
    return requestJson<Record<string, unknown>>(
      `/telegram/review/send-hourly-backfill?hours=${hours}&limit=${limit}&force=${force ? "true" : "false"}`,
      { method: "POST" },
    );
  },

  getTelegramReviewJobs(limit = 20) {
    return requestJson<{ ok: boolean; items: TelegramReviewJob[] }>(`/telegram/review/jobs?limit=${limit}`);
  },

  publishScheduledDue(limit = 20) {
    return requestJson<Record<string, unknown>>(`/publish/process-due?limit=${limit}`, { method: "POST" });
  },

  startAggregate(period: "hour" | "day" | "week" | "month") {
    return requestJson<Record<string, unknown>>("/ingestion/aggregate-start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ period }),
    });
  },

  getAggregateJob(jobId: string) {
    return requestJson<AggregateJobStatus>(`/ingestion/jobs/${jobId}`);
  },

  startPipeline(backfill_days: number) {
    return requestJson<Record<string, unknown>>("/pipeline/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ backfill_days }),
    });
  },

  startScoring(limit: number) {
    return requestJson<Record<string, unknown>>("/scoring/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
  },

  startEnrich(days_back: number, limit: number) {
    return requestJson<Record<string, unknown>>("/content/enrich/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ days_back, limit }),
    });
  },

  pruneNonAi(limit: number) {
    return requestJson<Record<string, unknown>>("/scoring/prune-non-ai", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit }),
    });
  },

  rebuildProfile() {
    return requestJson<Record<string, unknown>>("/feedback/rebuild-profile", {
      method: "POST",
    });
  },
};

export function formatDateTime(value?: string | null, locale = "ru-RU") {
  if (!value) return "—";
  const raw = String(value).trim();
  const hasOffset = /[zZ]|[+-]\d\d:\d\d$/.test(raw);
  const date = new Date(hasOffset ? raw : `${raw}Z`);
  if (Number.isNaN(date.getTime())) return raw;
  return date.toLocaleString(locale);
}
