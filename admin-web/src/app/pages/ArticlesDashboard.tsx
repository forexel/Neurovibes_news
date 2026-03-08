import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { Badge } from "../components/ui/badge";
import { Switch } from "../components/ui/switch";
import { Label } from "../components/ui/label";
import { Progress } from "../components/ui/progress";
import { StatusBadge } from "../components/StatusBadge";
import { ScoreBadge } from "../components/ScoreBadge";
import { ReasonActionDialog } from "../components/ReasonActionDialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "../components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "../components/ui/dialog";
import {
  Activity,
  Archive,
  ArrowDown,
  ArrowUp,
  Calendar,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Clock,
  DollarSign,
  ExternalLink,
  Loader2,
  MoreVertical,
  Download,
  RotateCcw,
  Save,
  Search,
  Send,
  Trash2,
} from "lucide-react";
import { api, AggregateJobStatus, ApiError, ArticleDetails, ArticleListItem, CostSummary, formatDateTime, ReasonTagOption, WorkerStatus } from "../lib/api";

const sections = [
  { id: "all", label: "Все статьи" },
  { id: "backlog", label: "Бэклог" },
  { id: "unsorted", label: "Несортированные" },
  { id: "published", label: "Опубликованные" },
  { id: "selected_day", label: "Выбрано на день" },
  { id: "selected_hour", label: "Выбрано на час" },
  { id: "deleted", label: "Удаленные" },
] as const;

const pathToSection: Record<string, (typeof sections)[number]["id"]> = {
  "/": "all",
  "/dashboard": "all",
  "/backlog": "backlog",
  "/unsorted": "unsorted",
  "/published": "published",
  "/selected-day": "selected_day",
  "/selected-hour": "selected_hour",
  "/no-double": "all",
  "/deleted": "deleted",
};

const sectionToPath: Record<(typeof sections)[number]["id"], string> = {
  all: "/dashboard",
  backlog: "/backlog",
  unsorted: "/unsorted",
  published: "/published",
  selected_day: "/selected-day",
  selected_hour: "/selected-hour",
  deleted: "/deleted",
};

const POSITIVE_REASON_TAGS = new Set([
  "practical_tool",
  "practical_case",
  "industry_watch",
  "ru_relevance",
  "wow_positive",
  "future_impact",
  "business_impact",
  "breakthrough",
  "product_release",
  "benchmark",
  "regulation",
  "market_signal",
  "future_trend",
  "mass_audience",
  "global_shift",
]);
const NEGATIVE_REASON_TAGS = new Set([
  "insufficient_content",
  "low_significance",
  "no_business_use",
  "no_ru",
  "no_future_impact",
  "too_technical",
  "politics_noise",
  "investment_noise",
  "hiring_roles_noise",
  "duplicate",
  "non_ai",
  "too_local",
  "not_mass_audience",
  "short_lived",
]);

function isLikelyNegativeReasonTag(tag: string): boolean {
  const t = String(tag || "").trim().toLowerCase();
  if (!t) return false;
  if (NEGATIVE_REASON_TAGS.has(t)) return true;
  if (POSITIVE_REASON_TAGS.has(t)) return false;
  return (
    t.startsWith("no_") ||
    t.startsWith("not_") ||
    t.startsWith("non_") ||
    t.startsWith("too_") ||
    t.includes("noise") ||
    t.includes("duplicate") ||
    t.includes("low_")
  );
}

function mlRecommendationLabel(article: ArticleListItem) {
  const value = String(article.ml_recommendation || "").toLowerCase();
  const conf =
    typeof article.ml_recommendation_confidence === "number"
      ? `${(article.ml_recommendation_confidence * 10).toFixed(1)}/10`
      : null;
  if (value === "publish_candidate") {
    return { text: conf ? `ML: к публикации (${conf})` : "ML: к публикации", className: "bg-green-500/20 text-green-300 border-green-500/30" };
  }
  if (value === "delete_candidate") {
    return { text: conf ? `ML: к удалению (${conf})` : "ML: к удалению", className: "bg-red-500/20 text-red-300 border-red-500/30" };
  }
  if (value === "review") {
    return { text: conf ? `ML: на проверку (${conf})` : "ML: на проверку", className: "bg-yellow-500/20 text-yellow-300 border-yellow-500/30" };
  }
  if (value === "unknown") {
    return { text: "ML: нет модели", className: "bg-gray-500/20 text-gray-300 border-gray-500/30" };
  }
  return null;
}

function mlScoreValue(article: ArticleListItem): number | null {
  if (typeof article.ml_recommendation_confidence !== "number") return null;
  return article.ml_recommendation_confidence * 10;
}

function sanitizePreviewHtml(input: string): string {
  if (!input) return "";
  const parser = new DOMParser();
  const doc = parser.parseFromString(input, "text/html");
  const allowed = new Set(["b", "strong", "i", "em", "u", "a", "br", "p", "ul", "ol", "li"]);

  function clean(node: Element) {
    const tag = node.tagName.toLowerCase();
    if (!allowed.has(tag)) {
      const text = document.createTextNode(node.textContent || "");
      node.replaceWith(text);
      return;
    }
    for (const attr of Array.from(node.attributes)) {
      if (tag === "a" && attr.name === "href") continue;
      node.removeAttribute(attr.name);
    }
    if (tag === "a") {
      const href = (node.getAttribute("href") || "").trim();
      if (!/^https?:\/\//i.test(href)) {
        node.removeAttribute("href");
      } else {
        node.setAttribute("target", "_blank");
        node.setAttribute("rel", "noreferrer noopener");
      }
    }
    for (const child of Array.from(node.children)) clean(child);
  }

  for (const el of Array.from(doc.body.children)) clean(el);
  return doc.body.innerHTML;
}

function parseMlReason(input?: string | null): { reason: string; tags: string[] } {
  const raw = String(input || "").trim();
  if (!raw) return { reason: "", tags: [] };
  const normalized = raw.replace(/\s+\|\s+/g, "\n").replace(/\r/g, "");
  const lines = normalized
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  const tagsLine = lines.find((line) => /^tags=/i.test(line));
  const reasonLine = lines.find((line) => /^reason_text=/i.test(line) || /^reason=/i.test(line));
  const tags = tagsLine
    ? tagsLine
        .replace(/^tags=/i, "")
        .split(",")
        .map((tag) => tag.trim())
        .filter(Boolean)
    : [];
  const inferredTags: string[] = [];
  const low = raw.toLowerCase();
  if (/(инвестиц|valuation|market cap|оценк[аи])/.test(low)) inferredTags.push("investment_noise");
  if (/(полит|election|government|geopolit)/.test(low)) inferredTags.push("politics_noise");
  if (/(слишком техническ|too technical|agentic|benchmark)/.test(low)) inferredTags.push("too_technical");
  if (/(не про ai|non-ai|not ai)/.test(low)) inferredTags.push("non_ai");
  if (/(нет практическ|не про практическую пользу|low practical)/.test(low)) inferredTags.push("no_business_use");
  const gates = Array.from(raw.matchAll(/\b([a-z_]+_gate)\b/g)).map((m) => m[1]);
  const allTags = Array.from(new Set([...tags, ...inferredTags, ...gates]));

  if (reasonLine) {
    return { reason: reasonLine.replace(/^reason(_text)?=/i, "").trim(), tags: allTags };
  }
  const fallback = lines
    .filter((line) => !/^drivers:/i.test(line))
    .filter((line) => !/^ml_prob/i.test(line))
    .filter((line) => !/^publish>=/i.test(line))
    .filter((line) => !/^delete<=/i.test(line))
    .filter((line) => !/^decision=/i.test(line))
    .filter((line) => !/^(ai_ml_relevance|audience_fit|practical_value|source_quality|content_completeness|non_duplicate|risk_level_ok|novelty_signal)=/i.test(line))
    .join(" ");
  const cleaned = (fallback || raw)
    .replace(/\bpublish>=\s*\d+(\.\d+)?/gi, "")
    .replace(/\bdelete<=\s*\d+(\.\d+)?/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
  return { reason: cleaned, tags: allTags };
}

const ML_TAG_LABELS: Record<string, string> = {
  local_practical_gate: "Локальная практичность не пройдена",
  summary_boring_gate: "Скучный/слабый summary",
  technical_gate: "Слишком техническая",
  deep_technical_gate: "Слишком узко техническая",
  geek_gate: "Гик-контент для узкой аудитории",
  investing_gate: "Инвестиционный шум",
  mass_audience_gate: "Слабый fit под массовую аудиторию",
  editor_style_gate: "Не подходит редакционный стиль",
  personnel_move_gate: "Кадровая новость",
  non_ai: "Не про AI/ML",
  investment_noise: "Инвестиционный шум",
  politics_noise: "Политический шум",
  too_technical: "Слишком техническая",
  no_business_use: "Нет практической пользы",
  industry_watch: "Радар индустрии",
};

function formatMlTag(tag: string): string {
  const key = String(tag || "").trim();
  if (!key) return "";
  return ML_TAG_LABELS[key] ? `${ML_TAG_LABELS[key]} (${key})` : key;
}

export default function ArticlesDashboard() {
  const location = useLocation();
  const navigate = useNavigate();
  const [articles, setArticles] = useState<ArticleListItem[]>([]);
  const [total, setTotal] = useState(0);
  const [selectedSection, setSelectedSection] = useState<(typeof sections)[number]["id"]>(
    pathToSection[location.pathname] || "all",
  );
  const [searchQuery, setSearchQuery] = useState("");
  const [pageSize, setPageSize] = useState("25");
  const [currentPage, setCurrentPage] = useState(1);
  const [sortBy, setSortBy] = useState<"published_at" | "created_at" | "score">("published_at");
  const [sortDir, setSortDir] = useState<"asc" | "desc">("desc");
  const [noDoubleFilter, setNoDoubleFilter] = useState(false);
  const [loading, setLoading] = useState(true);
  const [previewArticle, setPreviewArticle] = useState<ArticleDetails | null>(null);
  const [costs, setCosts] = useState<CostSummary | null>(null);
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [error, setError] = useState("");
  const [aggregatePeriod, setAggregatePeriod] = useState<"hour" | "day" | "week" | "month">("day");
  const [aggregateLoading, setAggregateLoading] = useState(false);
  const [aggregateJob, setAggregateJob] = useState<AggregateJobStatus | null>(null);
  const [lastCollectionTime, setLastCollectionTime] = useState<string | null>(null);
  const [collectionResult, setCollectionResult] = useState<AggregateJobStatus["result"] | null>(null);
  const [openActionsId, setOpenActionsId] = useState<number | null>(null);
  const [previewActionLoading, setPreviewActionLoading] = useState<"publish" | "delete" | null>(null);
  const [previewMlConfirmed, setPreviewMlConfirmed] = useState(false);
  const [previewMlComment, setPreviewMlComment] = useState("");
  const [previewMlSaving, setPreviewMlSaving] = useState(false);
  const [reasonDialogOpen, setReasonDialogOpen] = useState(false);
  const [reasonDialogAction, setReasonDialogAction] = useState<"publish" | "delete">("publish");
  const [reasonDialogArticleId, setReasonDialogArticleId] = useState<number | null>(null);
  const [reasonDialogText, setReasonDialogText] = useState("");
  const [reasonDialogTags, setReasonDialogTags] = useState<string[]>([]);
  const [reasonDialogCustomTag, setReasonDialogCustomTag] = useState("");
  const [catalogReasonTagOptions, setCatalogReasonTagOptions] = useState<ReasonTagOption[]>([]);
  const actionsRef = useRef<HTMLDivElement | null>(null);
  const loadRequestSeq = useRef(0);
  const hasLoadedOnceRef = useRef(false);
  const [refreshing, setRefreshing] = useState(false);

  const deleteReasonTagOptions: Array<{ value: string; label: string }> = [
    { value: "insufficient_content", label: "Недостаточно контента" },
    { value: "low_significance", label: "Низкая значимость" },
    { value: "no_business_use", label: "Нет практической пользы" },
    { value: "no_ru", label: "Не релевантно для РФ" },
    { value: "no_future_impact", label: "Нет влияния на будущее" },
    { value: "too_technical", label: "Слишком техническая" },
    { value: "politics_noise", label: "Политический шум" },
    { value: "investment_noise", label: "Инвестиционный шум" },
    { value: "hiring_roles_noise", label: "Найм/роли, не по теме" },
    { value: "duplicate", label: "Дубликат" },
    { value: "non_ai", label: "Не AI/ML" },
  ];
  const publishReasonTagOptions: Array<{ value: string; label: string }> = [
    { value: "practical_tool", label: "Практичный инструмент" },
    { value: "practical_case", label: "Практичный кейс" },
    { value: "industry_watch", label: "Радар индустрии" },
    { value: "ru_relevance", label: "Релевантно РФ" },
    { value: "wow_positive", label: "Вау-эффект" },
    { value: "future_impact", label: "Влияние в будущем" },
    { value: "business_impact", label: "Влияние на бизнес" },
  ];

  const totalPages = useMemo(() => Math.max(1, Math.ceil(total / Number(pageSize))), [pageSize, total]);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!actionsRef.current?.contains(event.target as Node)) {
        setOpenActionsId(null);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, []);

  useEffect(() => {
    setSelectedSection(pathToSection[location.pathname] || "all");
    setCurrentPage(1);
  }, [location.pathname]);

  useEffect(() => {
    if (!aggregateJob?.job_id || aggregateJob.status === "done" || aggregateJob.status === "error") {
      return;
    }
    const timer = window.setInterval(async () => {
      try {
        const nextJob = await api.getAggregateJob(aggregateJob.job_id);
        setAggregateJob(nextJob);
        if (nextJob.status === "done") {
          setAggregateLoading(false);
          setLastCollectionTime(nextJob.finished_at || nextJob.started_at || new Date().toISOString());
          setCollectionResult(nextJob.result || null);
          await loadDashboard();
        } else if (nextJob.status === "error") {
          setAggregateLoading(false);
          setError(nextJob.error || "Сбор статей завершился ошибкой.");
        }
      } catch (err) {
        setAggregateLoading(false);
        setError(err instanceof Error ? err.message : "Не удалось получить статус сбора.");
      }
    }, 1500);
    return () => window.clearInterval(timer);
  }, [aggregateJob?.job_id, aggregateJob?.status]);

  async function loadDashboard() {
    const requestId = ++loadRequestSeq.current;
    const initialLoad = !hasLoadedOnceRef.current;
    if (initialLoad) setLoading(true);
    else setRefreshing(true);
    setError("");
    try {
      const articleData = await api.listArticles({
        view: selectedSection,
        page: String(currentPage),
        page_size: pageSize,
        sort_by: sortBy,
        sort_dir: sortDir,
        q: searchQuery.trim(),
        ...(noDoubleFilter ? { hide_double: "1" } : {}),
      });
      if (requestId !== loadRequestSeq.current) return;
      setArticles(articleData.items || []);
      setTotal(articleData.total || 0);
      hasLoadedOnceRef.current = true;

      const [costResult, workerResult] = await Promise.allSettled([api.getCosts(), api.getWorkerStatus()]);
      if (requestId !== loadRequestSeq.current) return;
      if (costResult.status === "fulfilled") setCosts(costResult.value);
      if (workerResult.status === "fulfilled") setWorker(workerResult.value);
      try {
        const tags = await api.getReasonTags();
        if (requestId !== loadRequestSeq.current) return;
        setCatalogReasonTagOptions(Array.isArray(tags.items) ? tags.items : []);
      } catch {
        if (requestId !== loadRequestSeq.current) return;
        setCatalogReasonTagOptions([]);
      }
    } catch (err) {
      if (requestId !== loadRequestSeq.current) return;
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить статьи.");
    } finally {
      if (requestId !== loadRequestSeq.current) return;
      setLoading(false);
      setRefreshing(false);
    }
  }

  useEffect(() => {
    loadDashboard();
  }, [currentPage, navigate, noDoubleFilter, pageSize, searchQuery, selectedSection, sortBy, sortDir]);

  useEffect(() => {
    setPreviewMlConfirmed(Boolean(previewArticle?.ml_verdict_confirmed));
    setPreviewMlComment(String(previewArticle?.ml_verdict_comment || ""));
    setPreviewMlSaving(false);
  }, [previewArticle?.id, previewArticle?.ml_verdict_confirmed, previewArticle?.ml_verdict_comment]);

  async function openPreview(articleId: number) {
    try {
      const details = await api.getArticle(articleId);
      setPreviewArticle(details);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось открыть превью.");
      navigate(`/article/${articleId}`, {
        state: { from: `${location.pathname}${location.search || ""}` },
      });
    }
  }

  async function runAction(action: () => Promise<unknown>) {
    setError("");
    try {
      await action();
      setOpenActionsId(null);
      await loadDashboard();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Операция завершилась ошибкой.");
    }
  }

  async function handleAggregateSync() {
    setError("");
    setAggregateLoading(true);
    setCollectionResult(null);
    try {
      const out = await api.startAggregate(aggregatePeriod);
      const jobId = typeof out.job_id === "string" ? out.job_id : "";
      if (!jobId) {
        throw new Error("Не удалось получить job_id для сбора.");
      }
      setAggregateJob({
        job_id: jobId,
        status: "running",
        period: aggregatePeriod,
        stage: "starting",
        processed: 0,
        total: 0,
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось запустить сбор статей.");
      setAggregateLoading(false);
    }
  }

  const periodLabels: Record<"hour" | "day" | "week" | "month", string> = {
    hour: "Час",
    day: "День",
    week: "Неделя",
    month: "Месяц",
  };

  const aggregateProgress =
    aggregateJob && (aggregateJob.total || 0) > 0
      ? Math.max(3, Math.min(100, Math.round(((aggregateJob.processed || 0) / Math.max(1, aggregateJob.total || 1)) * 100)))
      : aggregateLoading
        ? 8
        : 0;

  function toggleColumnSort(column: "score" | "published_at") {
    setCurrentPage(1);
    setSortBy((prevBy) => {
      if (prevBy === column) {
        setSortDir((prevDir) => (prevDir === "desc" ? "asc" : "desc"));
        return prevBy;
      }
      setSortDir("desc");
      return column;
    });
  }

  function sortIcon(column: "score" | "published_at") {
    if (sortBy !== column) return null;
    return sortDir === "desc" ? <ArrowDown className="w-3.5 h-3.5" /> : <ArrowUp className="w-3.5 h-3.5" />;
  }

  function openReasonDialog(action: "publish" | "delete", id: number) {
    setReasonDialogAction(action);
    setReasonDialogArticleId(id);
    setReasonDialogText("");
    setReasonDialogTags([]);
    setReasonDialogCustomTag("");
    setReasonDialogOpen(true);
  }

  function normalizeTag(raw: string): string {
    return String(raw || "")
      .trim()
      .toLowerCase()
      .replace(/[^\w-]+/g, "_")
      .replace(/_+/g, "_")
      .replace(/^_+|_+$/g, "");
  }

  function toggleReasonTag(tag: string) {
    setReasonDialogTags((prev) => (prev.includes(tag) ? prev.filter((x) => x !== tag) : [...prev, tag]));
  }

  function addCustomReasonTag() {
    const normalized = normalizeTag(reasonDialogCustomTag);
    if (!normalized) return;
    setReasonDialogTags((prev) => (prev.includes(normalized) ? prev : [...prev, normalized]));
    setReasonDialogCustomTag("");
  }

  const reasonTagOptions = useMemo(() => {
    const base = reasonDialogAction === "publish" ? publishReasonTagOptions : deleteReasonTagOptions;
    const merged = [...base];
    const existing = new Set(merged.map((x) => x.value));
    for (const item of catalogReasonTagOptions) {
      if (!item?.value || existing.has(item.value)) continue;
      const isNegative = isLikelyNegativeReasonTag(item.value);
      if (reasonDialogAction === "publish" && isNegative) continue;
      if (reasonDialogAction === "delete" && !isNegative) continue;
      merged.push(item);
      existing.add(item.value);
    }
    return merged;
  }, [reasonDialogAction, catalogReasonTagOptions]);

  async function submitReasonDialog() {
    const id = reasonDialogArticleId;
    const reason = reasonDialogText.trim();
    if (!id) return;
    if (reason.length < 5) {
      setError("Комментарий должен быть не короче 5 символов.");
      return;
    }
    if (reasonDialogAction === "delete") {
      const tags = reasonDialogTags.map(normalizeTag).filter(Boolean);
      const payload = [
        "decision=delete",
        `tags=${tags.join(",")}`,
        `reason_text=${reason}`,
      ].join("\n");
      setPreviewActionLoading("delete");
      setError("");
      try {
        await api.deleteArticle(id, payload);
        setPreviewArticle(null);
        setReasonDialogOpen(false);
        setOpenActionsId(null);
        setArticles((prev) => prev.filter((item) => item.id !== id));
        setTotal((prev) => Math.max(0, prev - 1));
        void loadDashboard();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Операция завершилась ошибкой.");
      } finally {
        setPreviewActionLoading(null);
      }
      return;
    }
    const publishTags = reasonDialogTags.map(normalizeTag).filter(Boolean);
    const publishFeedback = publishTags.length
      ? [`decision=publish`, `tags=${publishTags.join(",")}`, `reason_text=${reason}`].join("\n")
      : reason;
    setPreviewActionLoading("publish");
    try {
      await runAction(async () => {
        await api.postArticleAction(id, "feedback", { explanation_text: publishFeedback });
        await api.postArticleAction(id, "publish");
      });
      setPreviewArticle(null);
      setReasonDialogOpen(false);
    } finally {
      setPreviewActionLoading(null);
    }
  }

  async function promptDelete(id: number) {
    openReasonDialog("delete", id);
  }

  async function promptPublish(id: number) {
    openReasonDialog("publish", id);
  }

  async function savePreviewMlVerdict() {
    if (!previewArticle) return;
    setPreviewMlSaving(true);
    setError("");
    try {
      await api.saveMlVerdict(previewArticle.id, {
        confirmed: previewMlConfirmed,
        comment: previewMlComment.trim(),
      });
      const details = await api.getArticle(previewArticle.id);
      setPreviewArticle(details);
      await loadDashboard();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить ML-вердикт.");
    } finally {
      setPreviewMlSaving(false);
    }
  }

  const previewMlParsed = useMemo(
    () => parseMlReason(previewArticle?.ml_recommendation_reason),
    [previewArticle?.ml_recommendation_reason],
  );

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="mx-auto px-6 py-6">
        <div className="mb-6 flex items-center gap-2 overflow-x-auto pb-2">
          {sections.map((section) => (
            <Button
              key={section.id}
              variant={selectedSection === section.id ? "secondary" : "ghost"}
              size="sm"
              onClick={() => {
                setSearchQuery("");
                setNoDoubleFilter(false);
                navigate(sectionToPath[section.id]);
              }}
              className="whitespace-nowrap gap-2"
            >
              {section.label}
            </Button>
          ))}
        </div>

        <div className="mb-6 bg-card border border-border rounded-lg p-4">
          <div className="flex flex-wrap items-center gap-4">
            <div className="flex items-center gap-2">
              <Switch id="no-double" checked={noDoubleFilter} onCheckedChange={setNoDoubleFilter} />
              <Label htmlFor="no-double" className="text-sm cursor-pointer">
                Без дублей
              </Label>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Показывать:</span>
              <Select
                value={pageSize}
                onValueChange={(value) => {
                  setCurrentPage(1);
                  setPageSize(value);
                }}
              >
                <SelectTrigger className="w-20 h-8">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="10">10</SelectItem>
                  <SelectItem value="25">25</SelectItem>
                  <SelectItem value="50">50</SelectItem>
                  <SelectItem value="100">100</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Сортировка:</span>
              <Select
                value={`${sortBy}:${sortDir}`}
                onValueChange={(value) => {
                  const [by, dir] = value.split(":");
                  if ((by === "published_at" || by === "created_at" || by === "score") && (dir === "asc" || dir === "desc")) {
                    setCurrentPage(1);
                    setSortBy(by);
                    setSortDir(dir);
                  }
                }}
              >
                <SelectTrigger className="w-60 h-8">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="published_at:desc">Сначала новые</SelectItem>
                  <SelectItem value="published_at:asc">Сначала старые</SelectItem>
                  <SelectItem value="score:asc">Оценка: низкие сначала</SelectItem>
                  <SelectItem value="score:desc">Оценка: высокие сначала</SelectItem>
                </SelectContent>
              </Select>
            </div>

            <div className="flex-1 max-w-md">
              <div className="relative">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-muted-foreground" />
                <Input
                  placeholder="Поиск по статьям..."
                  value={searchQuery}
                  onChange={(e) => {
                    setCurrentPage(1);
                    setSearchQuery(e.target.value);
                  }}
                  className="pl-9 h-8"
                />
              </div>
            </div>

          <div className="flex items-center gap-3 ml-auto">
            {refreshing ? (
              <Badge variant="outline" className="gap-1 bg-muted">
                <Loader2 className="w-3 h-3 animate-spin" />
                <span className="text-xs">Обновление...</span>
              </Badge>
            ) : null}
            <Badge variant="outline" className="gap-1 bg-muted">
              <DollarSign className="w-3 h-3" />
              <span className="text-xs">
                ${Number(costs?.estimated_cost_usd_total || 0).toFixed(3)} / 24h ${Number(costs?.estimated_cost_usd_24h || 0).toFixed(3)}
              </span>
              </Badge>
              <Badge variant="outline" className="gap-1 bg-green-500/20 text-green-300 border-green-500/30">
                <Activity className="w-3 h-3" />
                <span className="text-xs">{worker?.worker_cycle_state || "worker"}</span>
              </Badge>
            </div>
          </div>
          {error ? <div className="mt-4 text-sm text-destructive">{error}</div> : null}
        </div>

        <div className="mb-6 bg-card border border-border rounded-lg p-4">
          <div className="flex flex-wrap items-center gap-5">
            <div className="flex min-w-[300px] items-center gap-4">
              <div className="flex h-12 w-12 items-center justify-center rounded-xl border border-blue-500/30 bg-blue-500/10 text-blue-400">
                <Download className="h-5 w-5" />
              </div>
              <div>
                <div className="text-[15px] font-semibold">Сбор статей из источников</div>
                <div className="text-sm text-muted-foreground">Импорт новых статей из RSS и веб-источников</div>
              </div>
            </div>

            <div className="flex flex-wrap items-center gap-3">
              <span className="text-sm text-muted-foreground">Период:</span>
              <div className="flex items-center gap-1 rounded-xl bg-muted/50 p-1">
                {([
                  ["hour", "Час"],
                  ["day", "День"],
                  ["week", "Неделя"],
                  ["month", "Месяц"],
                ] as const).map(([value, label]) => (
                  <button
                    key={value}
                    type="button"
                    onClick={() => setAggregatePeriod(value)}
                    disabled={aggregateLoading}
                    className={`rounded-lg px-5 py-2 text-sm font-medium transition-all ${
                      aggregatePeriod === value
                        ? "bg-primary text-primary-foreground shadow-sm"
                        : "text-muted-foreground hover:bg-muted hover:text-foreground"
                    } ${aggregateLoading ? "cursor-not-allowed opacity-50" : ""}`}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>

            <Button type="button" onClick={handleAggregateSync} disabled={aggregateLoading} className="gap-2 px-6">
              {aggregateLoading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Сбор...
                </>
              ) : (
                <>
                  <Download className="h-4 w-4" />
                  Собрать статьи
                </>
              )}
            </Button>

            <div className="ml-auto flex flex-wrap items-center gap-4 text-sm text-muted-foreground">
              {collectionResult && !aggregateLoading ? (
                <div className="flex items-center gap-2 text-green-300">
                  <CheckCircle2 className="h-4 w-4" />
                  <span>+{Number(collectionResult.inserted_total || 0)} новых</span>
                </div>
              ) : null}
              {lastCollectionTime ? (
                <div className="flex items-center gap-2">
                  <Clock className="h-4 w-4" />
                  <span>Последний сбор: {formatDateTime(lastCollectionTime)}</span>
                </div>
              ) : null}
            </div>
          </div>

          {aggregateLoading || aggregateJob?.status === "running" ? (
            <div className="mt-4 border-t border-border pt-4">
              <div className="mb-2 flex items-center justify-between">
                <span className="text-xs text-muted-foreground">
                  Загрузка статей за {periodLabels[aggregatePeriod].toLowerCase()}
                  {aggregateJob?.stage_detail ? ` (${aggregateJob.stage_detail})` : "..."}
                </span>
                <span className="text-xs text-muted-foreground">
                  {(aggregateJob?.processed || 0) > 0 && (aggregateJob?.total || 0) > 0
                    ? `${aggregateJob?.processed}/${aggregateJob?.total}`
                    : "Обработка источников"}
                </span>
              </div>
              <Progress value={aggregateProgress} className="h-1.5" />
            </div>
          ) : null}
        </div>

        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent border-b border-border">
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-32">Статус</TableHead>
                <TableHead className="w-24">Режим</TableHead>
                <TableHead className="w-20">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 hover:text-foreground"
                    onClick={() => toggleColumnSort("score")}
                  >
                    Оценка
                    {sortIcon("score")}
                  </button>
                </TableHead>
                <TableHead>Заголовок</TableHead>
                <TableHead className="w-40">Источник</TableHead>
                <TableHead className="w-40">
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 hover:text-foreground"
                    onClick={() => toggleColumnSort("published_at")}
                  >
                    Дата
                    {sortIcon("published_at")}
                  </button>
                </TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading && articles.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8} className="h-32 text-center text-muted-foreground">
                    Загрузка...
                  </TableCell>
                </TableRow>
              ) : articles.length === 0 ? (
                <TableRow>
                  <TableCell colSpan={8} className="h-32 text-center text-muted-foreground">
                    Статьи не найдены
                  </TableCell>
                </TableRow>
              ) : (
                articles.map((article) => {
                  return (
                  <TableRow
                    key={article.id}
                    className="hover:bg-muted/50 cursor-pointer group"
                    onClick={() => openPreview(article.id)}
                  >
                    <TableCell className="font-mono text-xs text-muted-foreground">#{article.id}</TableCell>
                    <TableCell>
                      <StatusBadge status={article.status} />
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {article.content_mode || "summary_only"}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      {mlScoreValue(article) !== null ? <ScoreBadge score={mlScoreValue(article) as number} size="sm" /> : "—"}
                    </TableCell>
                    <TableCell>
                      <div className="space-y-1 max-w-lg">
                        <div className="font-medium line-clamp-1">{article.ru_title || article.title}</div>
                        {article.short_hook || article.subtitle ? (
                          <div className="text-sm text-muted-foreground line-clamp-2">
                            {article.short_hook || article.subtitle}
                          </div>
                        ) : null}
                        <a
                          href={article.canonical_url}
                          target="_blank"
                          rel="noreferrer"
                          onClick={(event) => event.stopPropagation()}
                          className="inline-flex items-center gap-1 text-xs text-blue-400 hover:underline"
                        >
                          source <ExternalLink className="w-3 h-3" />
                        </a>
                      </div>
                    </TableCell>
                    <TableCell>{article.source_name || `#${article.source_id}`}</TableCell>
                    <TableCell className="text-sm text-muted-foreground">
                      {formatDateTime(article.published_at || article.created_at)}
                    </TableCell>
                    <TableCell onClick={(event) => event.stopPropagation()} className="relative">
                      <div ref={openActionsId === article.id ? actionsRef : null}>
                        <Button
                          variant="ghost"
                          size="sm"
                          className="h-8 w-8 p-0"
                          onClick={() => setOpenActionsId((value) => (value === article.id ? null : article.id))}
                        >
                          <MoreVertical className="w-4 h-4" />
                        </Button>
                        {openActionsId === article.id ? (
                          <div className="absolute right-0 top-10 z-20 w-56 rounded-md border border-border bg-popover p-1 shadow-md">
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                              onClick={() => {
                                setOpenActionsId(null);
                                navigate(`/article/${article.id}`, {
                                  state: { from: `${location.pathname}${location.search || ""}` },
                                });
                              }}
                            >
                              <ExternalLink className="w-4 h-4" />
                              Открыть редактор
                            </button>
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                              onClick={() =>
                                runAction(() =>
                                  api.postArticleAction(article.id, article.is_selected_day ? "unselect-day" : "select-day"),
                                )
                              }
                            >
                              <Calendar className="w-4 h-4" />
                              {article.is_selected_day ? "Снять с дня" : "Выбрать на день"}
                            </button>
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                              onClick={() =>
                                runAction(() =>
                                  api.postArticleAction(
                                    article.id,
                                    String(article.status).toUpperCase() === "SELECTED_HOURLY" ? "unselect-hour" : "status",
                                    String(article.status).toUpperCase() === "SELECTED_HOURLY" ? undefined : { status: "selected_hourly" },
                                  ),
                                )
                              }
                            >
                              <RotateCcw className="w-4 h-4" />
                              {String(article.status).toUpperCase() === "SELECTED_HOURLY" ? "Снять с часа" : "Выбрать на час"}
                            </button>
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                              onClick={() => runAction(() => api.postArticleAction(article.id, "status", { status: "rejected" }))}
                            >
                              <Archive className="w-4 h-4" />
                              Archive
                            </button>
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm text-destructive hover:bg-accent"
                              onClick={() => promptDelete(article.id)}
                            >
                              <Trash2 className="w-4 h-4" />
                              Delete
                            </button>
                            <button
                              className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                              onClick={() => promptPublish(article.id)}
                            >
                              <Send className="w-4 h-4" />
                              Publish
                            </button>
                          </div>
                        ) : null}
                      </div>
                    </TableCell>
                  </TableRow>
                )})
              )}
            </TableBody>
          </Table>
        </div>

        <div className="mt-4 flex items-center justify-between">
          <div className="text-sm text-muted-foreground">
            Page {currentPage}/{totalPages} · total {total}
          </div>
          <div className="flex gap-2">
            <Button variant="outline" size="sm" disabled={currentPage <= 1} onClick={() => setCurrentPage((value) => value - 1)}>
              <ChevronLeft className="w-4 h-4 mr-1" />
              Prev
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={currentPage >= totalPages}
              onClick={() => setCurrentPage((value) => value + 1)}
            >
              Next
              <ChevronRight className="w-4 h-4 ml-1" />
            </Button>
          </div>
        </div>
      </div>

      {previewArticle ? (
        <Dialog open={Boolean(previewArticle)} onOpenChange={(open) => !open && setPreviewArticle(null)}>
          <DialogContent className="max-w-4xl h-[92vh] overflow-hidden p-0 [&>button]:right-4 [&>button]:top-4 [&>button]:h-9 [&>button]:w-9 [&>button]:p-0 [&>button]:inline-flex [&>button]:items-center [&>button]:justify-center [&>button]:rounded-md [&>button]:border [&>button]:border-border [&>button_svg]:h-4 [&>button_svg]:w-4">
          <div className="h-full overflow-y-auto px-6 py-6">
          <DialogHeader>
            <DialogTitle className="pr-20 leading-tight">{previewArticle?.ru_title || previewArticle?.title}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 flex-wrap">
              {previewArticle ? <StatusBadge status={previewArticle.status} /> : null}
              <span>{previewArticle?.source_name}</span>
              <span>•</span>
              <span>{formatDateTime(previewArticle?.published_at || previewArticle?.created_at)}</span>
            </DialogDescription>
          </DialogHeader>
          <div className="mt-4 space-y-4">
            <div
              className="text-sm leading-7 text-foreground/90 whitespace-pre-wrap break-words [&_a]:text-blue-500 [&_a]:underline [&_b]:font-semibold [&_strong]:font-semibold"
              dangerouslySetInnerHTML={{
                __html: sanitizePreviewHtml(
                  previewArticle?.post_preview || previewArticle?.ru_summary || previewArticle?.subtitle || "Превью пока не готово.",
                ),
              }}
            />
            {previewArticle?.english_preview ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
                <div className="font-medium">English summary</div>
                <div className="mt-2 text-muted-foreground whitespace-pre-wrap break-words">
                  {previewArticle.english_preview}
                </div>
              </div>
            ) : null}
            {previewArticle?.archived_reason ? (
              <div className="rounded-lg border border-red-500/30 bg-red-500/10 p-3 text-sm">
                <div className="font-medium text-red-300">Причина удаления</div>
                <div className="mt-1 text-red-200/90">{previewArticle.archived_reason}</div>
              </div>
            ) : null}
            {previewMlParsed.reason || previewMlParsed.tags.length ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
                <div className="font-medium">Причина от ML</div>
                {previewMlParsed.tags.length > 0 ? (
                  <div className="mt-2 flex flex-wrap gap-1.5">
                    {previewMlParsed.tags.map((tag) => (
                      <Badge key={tag} variant="outline" className="text-xs">
                        {formatMlTag(tag)}
                      </Badge>
                    ))}
                  </div>
                ) : null}
                {previewMlParsed.reason ? (
                  <div className="mt-2 text-muted-foreground whitespace-pre-wrap">{previewMlParsed.reason}</div>
                ) : null}
              </div>
            ) : null}
            <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm space-y-3">
              <div className="font-medium">Валидация ML</div>
              <div className="text-muted-foreground">
                Рекомендация:{" "}
                {mlRecommendationLabel(previewArticle as ArticleListItem)?.text || "ML: нет рекомендации"}
                {previewArticle?.ml_model_version ? ` · модель ${previewArticle.ml_model_version}` : ""}
              </div>
              <div className="flex items-center gap-3">
                <Switch
                  id="preview-ml-verdict-confirmed"
                  checked={previewMlConfirmed}
                  onCheckedChange={setPreviewMlConfirmed}
                />
                <Label htmlFor="preview-ml-verdict-confirmed" className="cursor-pointer">
                  Согласен с вердиктом ML
                </Label>
              </div>
              <div className="space-y-2">
                <Label htmlFor="preview-ml-verdict-comment">Комментарий</Label>
                <Textarea
                  id="preview-ml-verdict-comment"
                  value={previewMlComment}
                  onChange={(event) => setPreviewMlComment(event.target.value)}
                  placeholder="Почему согласен / не согласен с выбором модели"
                  rows={3}
                />
              </div>
              <div>
                <Button
                  variant="outline"
                  size="sm"
                  className="gap-2"
                  onClick={savePreviewMlVerdict}
                  disabled={previewMlSaving}
                >
                  {previewMlSaving ? <Loader2 className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />}
                  Сохранить валидацию ML
                </Button>
              </div>
            </div>
            {previewArticle?.feedback ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
                <div className="font-medium">Комментарий редактора</div>
                <div className="mt-1 text-muted-foreground whitespace-pre-wrap">{previewArticle.feedback}</div>
              </div>
            ) : null}
            {previewArticle?.ml_verdict_updated_at ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-sm">
                <div className="font-medium">
                  Валидация ML: {previewArticle.ml_verdict_confirmed ? "согласен" : "не согласен"}
                </div>
                {previewArticle.ml_verdict_comment ? (
                  <div className="mt-1 text-muted-foreground whitespace-pre-wrap">{previewArticle.ml_verdict_comment}</div>
                ) : null}
              </div>
            ) : null}
            {previewArticle?.embedding_preview && previewArticle.embedding_preview.length > 0 ? (
              <div className="rounded-lg border border-border bg-muted/40 p-3 text-xs">
                <div className="font-medium text-sm">
                  Вектор статьи ({previewArticle.article_vector_model || "embedding"})
                  {typeof previewArticle.embedding_dim === "number" ? ` · dim ${previewArticle.embedding_dim}` : ""}
                </div>
                <div className="mt-1 text-muted-foreground break-all">
                  [{previewArticle.embedding_preview.map((v) => Number(v).toFixed(4)).join(", ")}]
                </div>
              </div>
            ) : null}
            {previewArticle?.image_web ? (
              <img src={previewArticle.image_web} alt="" className="rounded-lg border border-border max-h-80 object-cover" />
            ) : null}
            <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
              {previewArticle ? (
                <Button asChild className="w-full">
                  <Link
                    to={`/article/${previewArticle.id}`}
                    state={{ from: `${location.pathname}${location.search || ""}` }}
                  >
                    Открыть в редакторе
                  </Link>
                </Button>
              ) : null}
              {previewArticle && String(previewArticle.status || "").toUpperCase() !== "PUBLISHED" ? (
                <Button
                  variant="outline"
                  className="gap-2 w-full"
                  onClick={() => promptPublish(previewArticle.id)}
                  disabled={previewActionLoading === "publish"}
                >
                  {previewActionLoading === "publish" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                  Опубликовать
                </Button>
              ) : null}
              {previewArticle && String(previewArticle.status || "").toUpperCase() !== "PUBLISHED" ? (
                <Button
                  variant="outline"
                  className="gap-2 w-full border-red-500/30 text-red-300 hover:bg-red-500/10"
                  onClick={() => promptDelete(previewArticle.id)}
                  disabled={previewActionLoading === "delete"}
                >
                  {previewActionLoading === "delete" ? <Loader2 className="h-4 w-4 animate-spin" /> : <Trash2 className="h-4 w-4" />}
                  Удалить
                </Button>
              ) : null}
            </div>
          </div>
          </div>
          </DialogContent>
        </Dialog>
      ) : null}
      <ReasonActionDialog
        open={reasonDialogOpen}
        onOpenChange={setReasonDialogOpen}
        action={reasonDialogAction}
        articleId={reasonDialogArticleId}
        text={reasonDialogText}
        onTextChange={setReasonDialogText}
        tags={reasonDialogTags}
        options={reasonTagOptions}
        onToggleTag={toggleReasonTag}
        customTag={reasonDialogCustomTag}
        onCustomTagChange={setReasonDialogCustomTag}
        onAddCustomTag={addCustomReasonTag}
        onSubmit={submitReasonDialog}
        loading={previewActionLoading !== null}
        loadingDelete={previewActionLoading === "delete"}
      />
    </div>
  );
}
