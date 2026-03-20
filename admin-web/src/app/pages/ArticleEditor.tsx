import { useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import { Switch } from "../components/ui/switch";
import { Calendar as DateCalendar } from "../components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { StatusBadge } from "../components/StatusBadge";
import { LogPanel } from "../components/LogPanel";
import { ReasonActionDialog } from "../components/ReasonActionDialog";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import {
  Archive,
  ArrowLeft,
  Calendar,
  ExternalLink,
  FileText,
  Clock,
  Image as ImageIcon,
  Loader2,
  Save,
  Send,
  Trash2,
  Upload,
  Wand2,
} from "lucide-react";
import { api, ApiError, ArticleDetails, formatDateTime, ReasonTagOption } from "../lib/api";
import { format } from "date-fns";
import { ru } from "date-fns/locale";

type LogEntry = { type: "info" | "success" | "error"; message: string; timestamp?: string };

function toInputDate(value?: string | null) {
  if (!value) return "";
  const raw = String(value).trim();
  const hasOffset = /[zZ]|[+-]\d\d:\d\d$/.test(raw);
  const date = new Date(hasOffset ? raw : `${raw}Z`);
  if (Number.isNaN(date.getTime())) return "";
  const part = (n: number) => String(n).padStart(2, "0");
  return `${date.getFullYear()}-${part(date.getMonth() + 1)}-${part(date.getDate())}T${part(date.getHours())}:${part(date.getMinutes())}`;
}

function stamp() {
  return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function getScheduleDateValue(value: string): Date | undefined {
  if (!value) return undefined;
  const date = new Date(`${value}:00`);
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function updateScheduleValue(currentValue: string, nextDate?: Date, nextTime?: string) {
  const base = getScheduleDateValue(currentValue) || new Date();
  const datePart = nextDate
    ? `${nextDate.getFullYear()}-${String(nextDate.getMonth() + 1).padStart(2, "0")}-${String(nextDate.getDate()).padStart(2, "0")}`
    : `${base.getFullYear()}-${String(base.getMonth() + 1).padStart(2, "0")}-${String(base.getDate()).padStart(2, "0")}`;
  const timePart =
    nextTime ??
    `${String(base.getHours()).padStart(2, "0")}:${String(base.getMinutes()).padStart(2, "0")}`;
  return `${datePart}T${timePart}`;
}

function normalizeSchedulePublishAt(raw: string): string {
  const value = String(raw || "").trim();
  if (!value) return "";
  if (/^\d{1,2}:\d{2}$/.test(value)) {
    const now = new Date();
    const yyyy = now.getFullYear();
    const mm = String(now.getMonth() + 1).padStart(2, "0");
    const dd = String(now.getDate()).padStart(2, "0");
    const hhmm = value.length === 4 ? `0${value}` : value;
    return `${yyyy}-${mm}-${dd}T${hhmm}`;
  }
  return value;
}

function normalizeTimeOnly(raw: string): string {
  const value = String(raw || "").trim();
  if (!/^\d{1,2}:\d{2}$/.test(value)) return "";
  const [hhRaw, mmRaw] = value.split(":");
  const hh = Number(hhRaw);
  const mm = Number(mmRaw);
  if (!Number.isFinite(hh) || !Number.isFinite(mm)) return "";
  if (hh < 0 || hh > 23 || mm < 0 || mm > 59) return "";
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

function resolvePublishAt(scheduleDate: string, scheduleTimeDraft: string): string {
  const normalizedDateTime = normalizeSchedulePublishAt(scheduleDate);
  const normalizedTime = normalizeTimeOnly(scheduleTimeDraft);
  if (!normalizedDateTime) return normalizedTime || "";
  if (!normalizedTime) return normalizedDateTime;

  // If we only have "time intent" and it resolves to a past time for today,
  // pass HH:mm so backend schedules the nearest future slot (today/tomorrow).
  const parsed = new Date(`${normalizedDateTime}:00`);
  if (!Number.isNaN(parsed.getTime())) {
    const now = new Date();
    const isToday =
      parsed.getFullYear() === now.getFullYear() &&
      parsed.getMonth() === now.getMonth() &&
      parsed.getDate() === now.getDate();
    if (isToday && parsed <= now) return normalizedTime;
  }
  return normalizedDateTime;
}

type MlReasonParsed = {
  reason: string;
  tags: string[];
  mlProb: number | null;
  publishThreshold: number | null;
  deleteThreshold: number | null;
  drivers: Array<{ key: string; value: number }>;
};

function parseMlReason(input?: string | null): MlReasonParsed {
  const raw = String(input || "").trim();
  if (!raw) {
    return { reason: "", tags: [], mlProb: null, publishThreshold: null, deleteThreshold: null, drivers: [] };
  }
  const lines = raw
    .replace(/\s+\|\s+/g, "\n")
    .replace(/\r/g, "")
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

  const mlProbMatch = raw.match(/\bml_prob\s*=\s*([0-9]*\.?[0-9]+)/i);
  const publishThresholdMatch = raw.match(/\bpublish>=\s*([0-9]*\.?[0-9]+)/i);
  const deleteThresholdMatch = raw.match(/\bdelete<=\s*([0-9]*\.?[0-9]+)/i);
  const mlProbRaw = mlProbMatch ? Number(mlProbMatch[1]) : null;
  const publishThresholdRaw = publishThresholdMatch ? Number(publishThresholdMatch[1]) : null;
  const deleteThresholdRaw = deleteThresholdMatch ? Number(deleteThresholdMatch[1]) : null;

  const drivers: Array<{ key: string; value: number }> = [];
  const driversLine = lines.find((line) => /^drivers:\s*/i.test(line));
  if (driversLine) {
    for (const part of driversLine.replace(/^drivers:\s*/i, "").split(",")) {
      const [rawKey, rawValue] = part.split(":");
      const key = String(rawKey || "").trim();
      const value = Number(String(rawValue || "").trim());
      if (!key || Number.isNaN(value)) continue;
      drivers.push({ key, value });
    }
  }

  const reason = reasonLine
    ? reasonLine.replace(/^reason(_text)?=/i, "").trim()
    : lines
        .filter((line) => !/^tags=/i.test(line))
        .filter((line) => !/^drivers:/i.test(line))
        .filter((line) => !/^ml_prob=/i.test(line))
        .join(" ")
        .trim();
  return {
    reason,
    tags: Array.from(new Set(tags)),
    mlProb: Number.isFinite(mlProbRaw ?? NaN) ? mlProbRaw : null,
    publishThreshold: Number.isFinite(publishThresholdRaw ?? NaN) ? publishThresholdRaw : null,
    deleteThreshold: Number.isFinite(deleteThresholdRaw ?? NaN) ? deleteThresholdRaw : null,
    drivers,
  };
}

const ML_TAG_LABELS: Record<string, string> = {
  insufficient_content: "Недостаточно контента",
  practical_tool: "Практичный инструмент",
  practical_case: "Практичный кейс",
  industry_watch: "Радар индустрии",
  ru_relevance: "Релевантно РФ",
  wow_positive: "Вау-эффект",
  future_impact: "Влияние в будущем",
  business_impact: "Влияние на бизнес",
  low_significance: "Низкая значимость",
  no_business_use: "Нет практической пользы",
  no_ru: "Не релевантно для РФ",
  no_future_impact: "Нет влияния на будущее",
  too_technical: "Слишком техническая",
  politics_noise: "Политический шум",
  investment_noise: "Инвестиционный шум",
  hiring_roles_noise: "Найм/роли, не по теме",
  duplicate: "Дубликат",
  non_ai: "Не AI/ML",
};
const ML_DRIVER_LABELS: Record<string, string> = {
  practical_value: "Практическая ценность",
  audience_fit: "Соответствие аудитории",
  freshness: "Актуальность",
  source_quality: "Качество источника",
  actionability: "Применимость",
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

function formatMlTag(tag: string): string {
  const key = String(tag || "").trim();
  if (!key) return "";
  return ML_TAG_LABELS[key] ? `${ML_TAG_LABELS[key]} (${key})` : key;
}

function formatMlDriverLabel(key: string): string {
  return ML_DRIVER_LABELS[key] || key;
}

function formatMl10(value: number): string {
  return `${(value * 10).toFixed(1)}/10`;
}

function probabilityBand(prob: number): string {
  if (prob >= 0.8) return "высокая";
  if (prob >= 0.55) return "средняя";
  return "низкая";
}

function parseReasonPayload(text?: string | null): { reason: string; tags: string[] } {
  const raw = String(text || "").trim();
  if (!raw) return { reason: "", tags: [] };
  const lines = raw
    .replace(/\r/g, "")
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
  const tagsLine = lines.find((line) => /^tags=/i.test(line));
  const reasonLine = lines.find((line) => /^reason_text=/i.test(line));
  const tags = tagsLine
    ? tagsLine
        .replace(/^tags=/i, "")
        .split(",")
        .map((x) => x.trim())
        .filter(Boolean)
    : [];
  const reason = reasonLine ? reasonLine.replace(/^reason_text=/i, "").trim() : raw;
  return { reason, tags };
}

export default function ArticleEditor() {
  const { id } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const articleId = Number(id);
  const returnTo =
    typeof location.state === "object" &&
    location.state !== null &&
    "from" in location.state &&
    typeof (location.state as { from?: unknown }).from === "string" &&
    String((location.state as { from?: string }).from || "").startsWith("/")
      ? String((location.state as { from?: string }).from)
      : "/dashboard";

  const [article, setArticle] = useState<ArticleDetails | null>(null);
  const [loading, setLoading] = useState<string | null>(null);
  const [pageLoading, setPageLoading] = useState(true);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [ruFullText, setRuFullText] = useState("");

  const [fullText, setFullText] = useState("");
  const [ruTitle, setRuTitle] = useState("");
  const [ruSummary, setRuSummary] = useState("");
  const [imagePrompt, setImagePrompt] = useState("");
  const [feedback, setFeedback] = useState("");
  const [mlVerdictConfirmed, setMlVerdictConfirmed] = useState(false);
  const [mlVerdictComment, setMlVerdictComment] = useState("");
  const [mlVerdictTags, setMlVerdictTags] = useState<string[]>([]);
  const [mlVerdictCustomTag, setMlVerdictCustomTag] = useState("");
  const [scheduleDate, setScheduleDate] = useState("");
  const [scheduleTimeDraft, setScheduleTimeDraft] = useState("");
  const [schedulePopoverOpen, setSchedulePopoverOpen] = useState(false);
  const [reasonDialogOpen, setReasonDialogOpen] = useState(false);
  const [reasonDialogAction, setReasonDialogAction] = useState<"publish" | "delete">("publish");
  const [reasonDialogText, setReasonDialogText] = useState("");
  const [reasonDialogTags, setReasonDialogTags] = useState<string[]>([]);
  const [reasonDialogCustomTag, setReasonDialogCustomTag] = useState("");
  const [catalogReasonTagOptions, setCatalogReasonTagOptions] = useState<ReasonTagOption[]>([]);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState("");

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

  function addLog(type: LogEntry["type"], message: string) {
    setLogs((prev) => [...prev, { type, message, timestamp: stamp() }]);
  }

  async function loadArticle() {
    if (!Number.isFinite(articleId)) return;
    setPageLoading(true);
    setNotFound(false);
    setLoadError("");
    try {
      const data = await api.getArticle(articleId);
      setArticle(data);
      setFullText(data.text || "");
      setRuTitle(data.ru_title || "");
      setRuSummary(data.ru_summary || "");
      setImagePrompt(data.image_prompt || "");
      setFeedback(data.feedback || "");
      setMlVerdictConfirmed(Boolean(data.ml_verdict_confirmed));
      setMlVerdictComment(data.ml_verdict_comment || "");
      setMlVerdictTags(Array.isArray(data.ml_verdict_tags) ? data.ml_verdict_tags : []);
      setMlVerdictCustomTag("");
      const nextScheduleDate = toInputDate(data.scheduled_publish_at);
      setScheduleDate(nextScheduleDate);
      setScheduleTimeDraft(nextScheduleDate ? nextScheduleDate.slice(11, 16) : "");
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      if (err instanceof ApiError && err.status === 404) {
        setNotFound(true);
      } else {
        const message = err instanceof Error ? err.message : "Не удалось загрузить статью.";
        setLoadError(message);
        addLog("error", message);
      }
    } finally {
      setPageLoading(false);
    }
  }

  useEffect(() => {
    loadArticle();
  }, [articleId]);

  useEffect(() => {
    api
      .getReasonTags()
      .then((out) => setCatalogReasonTagOptions(Array.isArray(out.items) ? out.items : []))
      .catch(() => setCatalogReasonTagOptions([]));
  }, []);

  const postPreview = useMemo(() => article?.post_preview || "RU текст не готов. Сгенерируй пост и сохрани его.", [article]);
  const selectedScheduleDate = useMemo(() => getScheduleDateValue(scheduleDate), [scheduleDate]);
  const hasInsufficientContent = String(article?.content_mode || "").toLowerCase() === "summary_only";
  const hasMlVerdict = useMemo(() => {
    const value = String(article?.ml_recommendation || "").trim().toLowerCase();
    return Boolean(value) && value !== "unknown";
  }, [article?.ml_recommendation]);
  const parsedMlReason = useMemo(() => parseMlReason(article?.ml_recommendation_reason), [article?.ml_recommendation_reason]);
  const mlRecommendationLabel = useMemo(() => {
    const rec = String(article?.ml_recommendation || "").trim().toLowerCase();
    if (rec === "publish_candidate") return "К публикации";
    if (rec === "delete_candidate") return "К удалению";
    if (rec === "review") return "Нужна ручная проверка";
    return "Нет вердикта";
  }, [article?.ml_recommendation]);
  const mlProbability = useMemo(() => {
    if (typeof parsedMlReason.mlProb === "number") return parsedMlReason.mlProb;
    return typeof article?.ml_recommendation_confidence === "number" ? article.ml_recommendation_confidence : null;
  }, [article?.ml_recommendation_confidence, parsedMlReason.mlProb]);
  const mlReasonTags = useMemo(() => {
    const fromReason = parsedMlReason.tags;
    const saved = Array.isArray(article?.ml_verdict_tags) ? article.ml_verdict_tags : [];
    return Array.from(new Set([...fromReason, ...saved].filter(Boolean)));
  }, [article?.ml_verdict_tags, parsedMlReason.tags]);
  const mlDrivers = useMemo(
    () => parsedMlReason.drivers.map((item) => ({ ...item, label: formatMlDriverLabel(item.key) })),
    [parsedMlReason.drivers],
  );
  const mlVerdictTagOptions = useMemo(() => {
    const rec = String(article?.ml_recommendation || "").trim().toLowerCase();
    const base = rec === "publish_candidate"
      ? publishReasonTagOptions
      : rec === "delete_candidate"
        ? deleteReasonTagOptions
        : [...publishReasonTagOptions, ...deleteReasonTagOptions];
    const merged = [...base];
    const existing = new Set(merged.map((x) => x.value));
    for (const item of catalogReasonTagOptions) {
      if (!item?.value || existing.has(item.value)) continue;
      const isNegative = isLikelyNegativeReasonTag(item.value);
      if (rec === "publish_candidate" && isNegative) continue;
      if (rec === "delete_candidate" && !isNegative) continue;
      merged.push(item);
      existing.add(item.value);
    }
    return merged;
  }, [article?.ml_recommendation, catalogReasonTagOptions]);

  function openReasonDialog(action: "publish" | "delete") {
    if (action === "publish" && !scheduleDate) {
      const nextTime = normalizeTimeOnly(scheduleTimeDraft);
      if (nextTime) {
        setScheduleDate(updateScheduleValue("", new Date(), nextTime));
      }
    }
    setReasonDialogAction(action);
    if (action === "publish") {
      const parsed = parseReasonPayload(feedback || "");
      setReasonDialogText(parsed.reason || "");
      setReasonDialogTags(parsed.tags);
    } else {
      setReasonDialogText("");
      setReasonDialogTags([]);
    }
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

  function toggleMlVerdictTag(tag: string) {
    const normalized = normalizeTag(tag);
    if (!normalized) return;
    setMlVerdictTags((prev) => (prev.includes(normalized) ? prev.filter((x) => x !== normalized) : [...prev, normalized]));
  }

  function addCustomMlVerdictTag() {
    const normalized = normalizeTag(mlVerdictCustomTag);
    if (!normalized) return;
    setMlVerdictTags((prev) => (prev.includes(normalized) ? prev : [...prev, normalized]));
    setMlVerdictCustomTag("");
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
    const reason = reasonDialogText.trim();
    if (reason.length < 5) {
      addLog("error", "Комментарий должен быть не короче 5 символов.");
      return;
    }
    const tags = reasonDialogTags.map(normalizeTag).filter(Boolean);
    if (reasonDialogAction === "publish") {
      const payload = tags.length
        ? ["decision=publish", `tags=${tags.join(",")}`, `reason_text=${reason}`].join("\n")
        : reason;
      const publishAt = resolvePublishAt(scheduleDate, scheduleTimeDraft);
      const shouldSchedule = publishAt.length > 0;
      setLoading("Publish");
      try {
        await api.postArticleAction(articleId, "feedback", { explanation_text: payload });
        setFeedback(payload);
        if (shouldSchedule) {
          const out = await api.postArticleAction(articleId, "schedule-publish", { publish_at: publishAt });
          const scheduledAtRaw =
            typeof out?.scheduled_publish_at === "string" && out.scheduled_publish_at.trim()
              ? out.scheduled_publish_at
              : publishAt;
          addLog("success", `Запланировано на: ${formatDateTime(scheduledAtRaw)}`);
        } else {
          await api.postArticleAction(articleId, "publish");
          addLog("success", "Опубликовано сейчас");
        }
        setReasonDialogOpen(false);
        await loadArticle();
      } catch (err) {
        addLog("error", err instanceof Error ? err.message : "Publish failed");
      } finally {
        setLoading(null);
      }
      return;
    }

    setLoading("delete");
    try {
      const payload = ["decision=delete", `tags=${tags.join(",")}`, `reason_text=${reason}`].join("\n");
      await api.deleteArticle(articleId, payload);
      setReasonDialogOpen(false);
      navigate(returnTo);
    } catch (err) {
      addLog("error", err instanceof Error ? err.message : "Delete failed");
    } finally {
      setLoading(null);
    }
  }

  async function saveMlVerdict() {
    if (!article) return;
    setLoading("ml-verdict");
    addLog("info", "Сохранение ML-вердикта...");
    try {
      await api.saveMlVerdict(article.id, {
        confirmed: mlVerdictConfirmed,
        comment: mlVerdictComment.trim(),
        tags: mlVerdictTags.map(normalizeTag).filter(Boolean),
      });
      addLog("success", "ML-вердикт сохранен");
      await loadArticle();
    } catch (err) {
      addLog("error", err instanceof Error ? err.message : "Не удалось сохранить ML-вердикт");
    } finally {
      setLoading(null);
    }
  }

  async function run(label: string, action: () => Promise<Record<string, unknown>>) {
    setLoading(label);
    addLog("info", `${label}...`);
    try {
      const out = await action();
      addLog("success", `${label}: ok`);
      if (typeof out.ru_translation === "string") setRuFullText(out.ru_translation);
      if (typeof out.ru_title === "string") setRuTitle(out.ru_title);
      if (typeof out.ru_summary === "string") setRuSummary(out.ru_summary);
      if (typeof out.image_prompt === "string") setImagePrompt(out.image_prompt);
      await loadArticle();
    } catch (err) {
      addLog("error", err instanceof Error ? err.message : `${label}: failed`);
    } finally {
      setLoading(null);
    }
  }

  async function uploadPicture(file: File) {
    const formData = new FormData();
    formData.append("image", file);
    setLoading("upload");
    addLog("info", "Загрузка изображения...");
    try {
      const response = await fetch(`/articles/${articleId}/picture/upload`, { method: "POST", body: formData });
      const out = await response.json();
      if (!response.ok) throw new Error(String(out?.detail || "upload failed"));
      addLog("success", "Изображение загружено");
      await loadArticle();
    } catch (err) {
      addLog("error", err instanceof Error ? err.message : "upload failed");
    } finally {
      setLoading(null);
    }
  }

  if (pageLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (!article && notFound) {
    return (
      <div className="min-h-screen bg-background">
        <TopNavigation />
        <div className="max-w-7xl mx-auto px-6 py-12 text-center">
          <h1 className="text-2xl font-semibold mb-4">Статья не найдена</h1>
          <Button asChild>
            <Link to={returnTo}>Вернуться к списку</Link>
          </Button>
        </div>
      </div>
    );
  }

  if (!article && loadError) {
    return (
      <div className="min-h-screen bg-background">
        <TopNavigation />
        <div className="max-w-7xl mx-auto px-6 py-12 text-center">
          <h1 className="text-2xl font-semibold mb-4">Ошибка загрузки статьи</h1>
          <p className="text-muted-foreground mb-6 whitespace-pre-wrap">{loadError}</p>
          <div className="flex items-center justify-center gap-3">
            <Button onClick={() => void loadArticle()}>Повторить</Button>
            <Button variant="outline" asChild>
              <Link to={returnTo}>Вернуться к списку</Link>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (!article) {
    return (
      <div className="min-h-screen bg-background">
        <TopNavigation />
        <div className="max-w-7xl mx-auto px-6 py-12 text-center">
          <h1 className="text-2xl font-semibold mb-4">Статья временно недоступна</h1>
          <div className="flex items-center justify-center gap-3">
            <Button onClick={() => void loadArticle()}>Повторить</Button>
            <Button variant="outline" asChild>
              <Link to={returnTo}>Вернуться к списку</Link>
            </Button>
          </div>
        </div>
      </div>
    );
  }

  const isSelectedHourly = String(article.status).toUpperCase() === "SELECTED_HOURLY";

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="mb-6">
          <div className="flex items-center gap-3 mb-4">
            <Button variant="ghost" size="sm" asChild>
              <Link to={returnTo}>
                <ArrowLeft className="w-4 h-4 mr-2" />
                К списку
              </Link>
            </Button>
            <div className="flex items-center gap-2">
              <StatusBadge status={article.status} />
              <span className="text-sm text-muted-foreground">ID #{article.id}</span>
            </div>
          </div>
          <h1 className="text-2xl font-semibold mb-2">{ruTitle || article.ru_title || article.title}</h1>
          <div className="flex items-center gap-4 text-sm text-muted-foreground flex-wrap">
            <a href={article.canonical_url} target="_blank" rel="noopener noreferrer" className="flex items-center gap-1 hover:text-foreground">
              {article.source_name || `Source #${article.source_id}`}
              <ExternalLink className="w-3 h-3" />
            </a>
            <span>•</span>
            <span>{formatDateTime(article.published_at || article.created_at)}</span>
            {article.score_reasoning ? <span>• {article.score_reasoning}</span> : null}
          </div>
        </div>

        {hasInsufficientContent ? (
          <div className="mb-6 rounded-lg border border-yellow-500/30 bg-yellow-500/10 px-4 py-3 text-sm text-yellow-100">
            Недостаточно контента для публикации. Сейчас в статье только короткий RSS summary. Нажми "Загрузить с сайта", и если сайт не отдаёт полный текст, материал лучше не публиковать.
          </div>
        ) : null}

        {hasMlVerdict ? (
          <div className="mb-6 rounded-lg border border-border bg-card p-4">
            <div className="mb-3 text-sm font-medium">ML-рекомендация</div>
            <div className="mb-3 rounded-md border border-border/80 bg-muted/30 p-3 text-sm">
              <div>
                Рекомендация: <span className="font-medium">{mlRecommendationLabel}</span>
                {mlProbability !== null ? ` (${formatMl10(mlProbability)})` : ""}
              </div>
              {mlProbability !== null ? (
                <div className="mt-2 text-muted-foreground">
                  Уверенность ML: <span className="font-medium text-foreground">{formatMl10(mlProbability)}</span>
                  {" "}({probabilityBand(mlProbability)})
                </div>
              ) : null}
              {(parsedMlReason.publishThreshold !== null || parsedMlReason.deleteThreshold !== null) ? (
                <div className="mt-1 text-muted-foreground">
                  Пороги:
                  {parsedMlReason.publishThreshold !== null ? ` publish >= ${formatMl10(parsedMlReason.publishThreshold)}` : ""}
                  {parsedMlReason.publishThreshold !== null && parsedMlReason.deleteThreshold !== null ? ";" : ""}
                  {parsedMlReason.deleteThreshold !== null ? ` delete <= ${formatMl10(parsedMlReason.deleteThreshold)}` : ""}
                </div>
              ) : null}
              {mlDrivers.length > 0 ? (
                <div className="mt-3 space-y-1">
                  <div className="text-xs font-medium text-muted-foreground">Факторы ML</div>
                  <div className="flex flex-wrap gap-2">
                    {mlDrivers.map((driver) => {
                      const toneClass =
                        driver.value >= 0.67
                          ? "border-green-500/40 bg-green-500/10 text-green-300"
                          : driver.value <= 0.45
                            ? "border-red-500/40 bg-red-500/10 text-red-300"
                            : "border-yellow-500/40 bg-yellow-500/10 text-yellow-300";
                      return (
                        <Badge key={driver.key} variant="outline" className={`text-xs ${toneClass}`}>
                          {driver.label}: {driver.value.toFixed(2)}
                        </Badge>
                      );
                    })}
                  </div>
                </div>
              ) : null}
              {mlReasonTags.length > 0 ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {mlReasonTags.map((tag) => (
                    <Badge key={tag} variant="outline" className="text-xs">
                      {formatMlTag(tag)}
                    </Badge>
                  ))}
                </div>
              ) : null}
              {parsedMlReason.reason ? <div className="mt-2 text-muted-foreground whitespace-pre-wrap">{parsedMlReason.reason}</div> : null}
            </div>
            <div className="mb-3 flex items-center gap-3">
              <Switch id="ml-verdict-confirmed" checked={mlVerdictConfirmed} onCheckedChange={setMlVerdictConfirmed} />
              <Label htmlFor="ml-verdict-confirmed" className="cursor-pointer">
                Согласен с рекомендацией ML
              </Label>
            </div>
            <div className="space-y-2 rounded-md border border-border p-3">
              <div className="text-xs font-medium text-muted-foreground">Теги для дообучения ML</div>
              <div className="flex flex-wrap gap-2">
                {mlVerdictTagOptions.map((item) => {
                  const active = mlVerdictTags.includes(item.value);
                  return (
                    <button
                      key={item.value}
                      type="button"
                      onClick={() => toggleMlVerdictTag(item.value)}
                      className={`rounded-full border px-2.5 py-1 text-xs transition-colors ${
                        active
                          ? "border-primary/40 bg-primary/15 text-primary"
                          : "border-border bg-muted/20 text-muted-foreground hover:bg-muted/40"
                      }`}
                    >
                      {item.label}
                    </button>
                  );
                })}
              </div>
              <div className="flex gap-2">
                <Input
                  value={mlVerdictCustomTag}
                  onChange={(e) => setMlVerdictCustomTag(e.target.value)}
                  placeholder="Новый тег (например: local_policy_noise)"
                  className="h-8"
                />
                <Button type="button" variant="outline" size="sm" onClick={addCustomMlVerdictTag}>
                  Добавить тег
                </Button>
              </div>
            </div>
            <div className="mt-3 space-y-2">
              <Label htmlFor="ml-verdict-comment">Комментарий</Label>
              <Textarea
                id="ml-verdict-comment"
                value={mlVerdictComment}
                onChange={(e) => setMlVerdictComment(e.target.value)}
                rows={3}
                placeholder="Почему согласен / не согласен с выбором модели"
              />
            </div>
            <div className="mt-3">
              <Button size="sm" variant="secondary" onClick={saveMlVerdict} disabled={loading === "ml-verdict"}>
                {loading === "ml-verdict" ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Save className="w-4 h-4 mr-2" />}
                Сохранить ML-вердикт
              </Button>
            </div>
          </div>
        ) : (
          <div className="mb-6 text-sm text-muted-foreground">ML-вердикт пока отсутствует. Блок подтверждения скрыт.</div>
        )}

        <Tabs defaultValue="linear" className="space-y-6">
          <TabsList>
            <TabsTrigger value="linear">Линейный режим</TabsTrigger>
            <TabsTrigger value="workspace">Рабочее пространство</TabsTrigger>
          </TabsList>

          <TabsContent value="linear" className="space-y-6">
            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold">Полный текст (English)</h3>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => run("Read From Site", () => api.postArticleAction(articleId, "content/pull"))}
                  disabled={loading === "Read From Site"}
                >
                  {loading === "Read From Site" ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <ExternalLink className="w-4 h-4 mr-2" />}
                  Загрузить с сайта
                </Button>
              </div>
              <Textarea value={fullText} onChange={(e) => setFullText(e.target.value)} rows={10} className="font-mono text-sm" />
              <div className="mt-3">
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    run("Save English Full", () => api.postArticleAction(articleId, "text/override", { text: fullText }))
                  }
                  disabled={fullText.trim().length < 50 || loading === "Save English Full"}
                >
                  <Save className="w-4 h-4 mr-2" />
                  Сохранить текст
                </Button>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Краткое содержание (RSS)</h3>
              <div className="bg-muted/50 rounded-lg p-4 text-sm text-muted-foreground italic">
                {article.subtitle || "Краткое содержание из RSS недоступно"}
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold">Полный перевод (Russian)</h3>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => run("Translate Full", () => api.postArticleAction(articleId, "translate-full"))}
                  disabled={loading === "Translate Full"}
                >
                  {loading === "Translate Full" ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Wand2 className="w-4 h-4 mr-2" />}
                  Перевести
                </Button>
              </div>
              <Textarea value={ruFullText} onChange={(e) => setRuFullText(e.target.value)} rows={10} className="font-mono text-sm" />
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center justify-between mb-4">
                <h3 className="font-semibold">Пост для Telegram</h3>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => run("Generate Post", () => api.postArticleAction(articleId, "post/generate"))}
                    disabled={loading === "Generate Post"}
                  >
                    <Wand2 className="w-4 h-4 mr-2" />
                    Generate Post
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => run("Translate Preview", () => api.postArticleAction(articleId, "translate"))}
                    disabled={loading === "Translate Preview"}
                  >
                    <Wand2 className="w-4 h-4 mr-2" />
                    Translate Preview
                  </Button>
                </div>
              </div>
              <div className="grid gap-4">
                <div className="space-y-2">
                  <Label htmlFor="ru-title">RU Title</Label>
                  <Textarea id="ru-title" value={ruTitle} onChange={(e) => setRuTitle(e.target.value)} rows={3} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="ru-summary">RU Summary</Label>
                  <Textarea id="ru-summary" value={ruSummary} onChange={(e) => setRuSummary(e.target.value)} rows={8} />
                </div>
                <Button
                  onClick={() =>
                    run("Save RU Text", () =>
                      api.postArticleAction(articleId, "ru/save", { ru_title: ruTitle, ru_summary: ruSummary }),
                    )
                  }
                  disabled={!ruTitle.trim() || ruSummary.trim().length < 10 || loading === "Save RU Text"}
                  className="w-fit"
                >
                  <Save className="w-4 h-4 mr-2" />
                  Save RU Text
                </Button>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Image Prompt</h3>
              <Textarea value={imagePrompt} onChange={(e) => setImagePrompt(e.target.value)} rows={5} />
              <div className="mt-4 flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  onClick={() => run("Generate Image Prompt", () => api.postArticleAction(articleId, "image-prompt/generate"))}
                >
                  <Wand2 className="w-4 h-4 mr-2" />
                  Generate Image Prompt
                </Button>
                <Button
                  variant="secondary"
                  onClick={() =>
                    run("Save Prompt", () => api.postArticleAction(articleId, "image-prompt/save", { prompt: imagePrompt }))
                  }
                  disabled={imagePrompt.trim().length < 10}
                >
                  <Save className="w-4 h-4 mr-2" />
                  Save Prompt
                </Button>
                <Button onClick={() => run("Generate Picture", () => api.postArticleAction(articleId, "picture/generate"))}>
                  <Wand2 className="w-4 h-4 mr-2" />
                  Generate Picture
                </Button>
                <Label className="inline-flex items-center gap-2 rounded-md border border-border px-3 py-2 cursor-pointer">
                  <Upload className="w-4 h-4" />
                  Upload image
                  <input
                    type="file"
                    className="hidden"
                    accept="image/*"
                    onChange={(event) => {
                      const file = event.target.files?.[0];
                      if (file) uploadPicture(file);
                      event.currentTarget.value = "";
                    }}
                  />
                </Label>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Предпросмотр поста</h3>
              <div className="bg-gradient-to-br from-blue-950/50 to-purple-950/50 border border-border rounded-lg p-6 max-w-md">
                {article.image_web ? (
                  <img
                    src={article.image_web}
                    alt="Preview"
                    className="w-full h-48 object-cover rounded-lg mb-4"
                  />
                ) : (
                  <div className="w-full h-48 bg-muted/20 rounded-lg mb-4 flex items-center justify-center">
                    <ImageIcon className="w-12 h-12 text-muted-foreground" />
                  </div>
                )}
                <h4 className="font-semibold mb-2">
                  {ruTitle || article.ru_title || article.title}
                </h4>
                <p className="text-sm text-muted-foreground mb-3">
                  {ruSummary || article.ru_summary || article.subtitle || "Аннотация не создана"}
                </p>
                <div className="text-xs text-muted-foreground italic">
                  AI News Daily
                </div>
              </div>
              <pre className="mt-4 whitespace-pre-wrap rounded-lg bg-black/20 border border-border p-4 text-sm">{postPreview}</pre>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Действия</h3>
              <div className="space-y-4">
                <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                  <div>
                    <Label htmlFor="schedule-linear">Запланировать публикацию</Label>
                    <Input
                      id="schedule-linear"
                      type="datetime-local"
                      value={scheduleDate}
                      onChange={(e) => {
                        const next = e.target.value;
                        setScheduleDate(next);
                        setScheduleTimeDraft(next ? next.slice(11, 16) : "");
                      }}
                    />
                  </div>
                  <div className="flex items-end">
                    <Button
                      variant="outline"
                      className="w-full"
                      onClick={() =>
                        run("Clear Schedule", () => api.postArticleAction(articleId, "unschedule-publish")).then(() => {
                          setScheduleDate("");
                          setScheduleTimeDraft("");
                        })
                      }
                    >
                      <Calendar className="w-4 h-4 mr-2" />
                      Очистить расписание
                    </Button>
                  </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-2">
                  <Button variant="outline" size="sm" onClick={() => run("Score", () => api.postArticleAction(articleId, "score"))}>
                    <Wand2 className="w-4 h-4 mr-2" />
                    Оценить
                  </Button>
                  <Button variant="outline" size="sm" onClick={() => run("Prepare + Image", () => api.postArticleAction(articleId, "prepare"))}>
                    <FileText className="w-4 h-4 mr-2" />
                    Подготовить
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      run(isSelectedHourly ? "Remove Hour" : "Select Hour", () =>
                        api.postArticleAction(
                          articleId,
                          isSelectedHourly ? "unselect-hour" : "status",
                          isSelectedHourly ? undefined : { status: "selected_hourly" },
                        ),
                      )
                    }
                  >
                    <Clock className="w-4 h-4 mr-2" />
                    {isSelectedHourly ? "Снять с часа" : "Выбрать на час"}
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      run(article.is_selected_day ? "Remove Day" : "Select Day", () =>
                        api.postArticleAction(articleId, article.is_selected_day ? "unselect-day" : "select-day"),
                      )
                    }
                  >
                    <Calendar className="w-4 h-4 mr-2" />
                    {article.is_selected_day ? "Снять с дня" : "Выбрать на день"}
                  </Button>
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button
                    onClick={() => openReasonDialog("publish")}
                    disabled={hasInsufficientContent}
                    className="flex-1 min-w-[220px]"
                  >
                    <Send className="w-4 h-4 mr-2" />
                    {resolvePublishAt(scheduleDate, scheduleTimeDraft) ? "Запланировать публикацию" : "Опубликовать сейчас"}
                  </Button>
                  <Button variant="outline" onClick={() => run("Archive", () => api.postArticleAction(articleId, "status", { status: "rejected" }))}>
                    <Archive className="w-4 h-4 mr-2" />
                    В архив
                  </Button>
                  <Button variant="destructive" onClick={() => openReasonDialog("delete")}>
                    <Trash2 className="w-4 h-4 mr-2" />
                    Удалить
                  </Button>
                </div>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Обратная связь</h3>
              <div className="space-y-4">
                <div>
                  <Label htmlFor="feedback-linear">Почему выбрана эта статья?</Label>
                  <Textarea
                    id="feedback-linear"
                    value={feedback}
                    onChange={(e) => setFeedback(e.target.value)}
                    rows={4}
                    placeholder="Заметки для обучения алгоритмов..."
                  />
                </div>
                <Button
                  size="sm"
                  variant="secondary"
                  onClick={() =>
                    run("Save Feedback", () =>
                      api.postArticleAction(articleId, "feedback", { explanation_text: feedback }),
                    )
                  }
                  disabled={feedback.trim().length < 5}
                >
                  <Save className="w-4 h-4 mr-2" />
                  Сохранить обратную связь
                </Button>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="workspace">
            <div className="grid gap-6 lg:grid-cols-[1.4fr_0.8fr]">
              <div className="space-y-6">
                <div className="bg-card border border-border rounded-lg p-6">
                  <h3 className="font-semibold mb-4">Рабочие тексты</h3>
                  <div className="space-y-4">
                    <Textarea value={fullText} onChange={(e) => setFullText(e.target.value)} rows={8} />
                    <Textarea value={ruSummary} onChange={(e) => setRuSummary(e.target.value)} rows={8} />
                  </div>
                </div>
                <div className="bg-card border border-border rounded-lg p-6">
                  <h3 className="font-semibold mb-4">Превью публикации</h3>
                  <pre className="whitespace-pre-wrap rounded-lg bg-black/20 border border-border p-4 text-sm">{postPreview}</pre>
                </div>
              </div>
              <div className="space-y-6">
                <div className="bg-card border border-border rounded-lg p-6">
                  <h3 className="font-semibold mb-4">Публикация</h3>
                  <div className="space-y-4">
                    <div className="space-y-2">
                      <Label htmlFor="schedule">Отложенная публикация</Label>
                      <div className="flex gap-2">
                        <Popover open={schedulePopoverOpen} onOpenChange={setSchedulePopoverOpen}>
                          <PopoverTrigger asChild>
                            <Button id="schedule" type="button" variant="outline" className="flex-1 justify-start text-left font-normal">
                              <Calendar className="mr-2 h-4 w-4" />
                              {selectedScheduleDate
                                ? format(selectedScheduleDate, "d MMMM yyyy", { locale: ru })
                                : "Выбрать дату"}
                            </Button>
                          </PopoverTrigger>
                          <PopoverContent className="w-auto p-0" align="start">
                            <DateCalendar
                              mode="single"
                              selected={selectedScheduleDate}
                              onSelect={(date) => {
                                setScheduleDate((current) => updateScheduleValue(current, date));
                                if (!scheduleTimeDraft) {
                                  setScheduleTimeDraft("10:00");
                                }
                                setSchedulePopoverOpen(false);
                              }}
                            />
                          </PopoverContent>
                        </Popover>
                        <Input
                          type="time"
                          value={scheduleTimeDraft}
                          onChange={(e) => {
                            const nextTime = normalizeTimeOnly(e.target.value || "");
                            setScheduleTimeDraft(nextTime);
                            if (nextTime) {
                              setScheduleDate((current) => updateScheduleValue(current, undefined, nextTime));
                            }
                          }}
                          className="w-32"
                        />
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        onClick={() =>
                          run("Schedule", () =>
                            api.postArticleAction(articleId, "schedule-publish", {
                              publish_at: resolvePublishAt(scheduleDate, scheduleTimeDraft),
                            }),
                          )
                        }
                        disabled={!resolvePublishAt(scheduleDate, scheduleTimeDraft)}
                      >
                        <Calendar className="w-4 h-4 mr-2" />
                        Schedule
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() =>
                          run("Clear Schedule", () => api.postArticleAction(articleId, "unschedule-publish")).then(() => {
                            setScheduleDate("");
                            setScheduleTimeDraft("");
                          })
                        }
                      >
                        Clear
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button variant="outline" onClick={() => run("Score", () => api.postArticleAction(articleId, "score"))}>
                        Score
                      </Button>
                      <Button variant="outline" onClick={() => run("Prepare + Image", () => api.postArticleAction(articleId, "prepare"))}>
                        Prepare + Image
                      </Button>
                      <Button
                        onClick={() => openReasonDialog("publish")}
                        disabled={hasInsufficientContent}
                      >
                        <Send className="w-4 h-4 mr-2" />
                        {resolvePublishAt(scheduleDate, scheduleTimeDraft) ? "Schedule" : "Publish"}
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        onClick={() =>
                          run(article.is_selected_day ? "Remove Day" : "Select Day", () =>
                            api.postArticleAction(articleId, article.is_selected_day ? "unselect-day" : "select-day"),
                          )
                        }
                      >
                        {article.is_selected_day ? "Remove Day" : "Select Day"}
                      </Button>
                      <Button
                        variant="outline"
                        onClick={() =>
                          run(isSelectedHourly ? "Remove Hour" : "Select Hour", () =>
                            api.postArticleAction(
                              articleId,
                              isSelectedHourly ? "unselect-hour" : "status",
                              isSelectedHourly ? undefined : { status: "selected_hourly" },
                            ),
                          )
                        }
                      >
                        {isSelectedHourly ? "Remove Hour" : "Select Hour"}
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button variant="outline" onClick={() => run("Archive", () => api.postArticleAction(articleId, "status", { status: "rejected" }))}>
                        <Archive className="w-4 h-4 mr-2" />
                        Archive
                      </Button>
                      <Button variant="destructive" onClick={() => openReasonDialog("delete")}>
                        <Trash2 className="w-4 h-4 mr-2" />
                        Delete
                      </Button>
                    </div>
                  </div>
                </div>

                <div className="bg-card border border-border rounded-lg p-6">
                  <h3 className="font-semibold mb-4">Feedback</h3>
                  <Textarea value={feedback} onChange={(e) => setFeedback(e.target.value)} rows={6} />
                  <Button
                    className="mt-4"
                    onClick={() =>
                      run("Save Feedback", () =>
                        api.postArticleAction(articleId, "feedback", { explanation_text: feedback }),
                      )
                    }
                    disabled={feedback.trim().length < 5}
                  >
                    Save Feedback
                  </Button>
                </div>
              </div>
            </div>
          </TabsContent>
        </Tabs>

        <div className="mt-6">
          <LogPanel logs={logs} title="Лог операций" />
        </div>
      </div>
      <ReasonActionDialog
        open={reasonDialogOpen}
        onOpenChange={setReasonDialogOpen}
        action={reasonDialogAction}
        articleId={articleId}
        text={reasonDialogText}
        onTextChange={setReasonDialogText}
        tags={reasonDialogTags}
        options={reasonTagOptions}
        onToggleTag={toggleReasonTag}
        customTag={reasonDialogCustomTag}
        onCustomTagChange={setReasonDialogCustomTag}
        onAddCustomTag={addCustomReasonTag}
        onSubmit={submitReasonDialog}
        loading={loading !== null}
        loadingDelete={loading === "delete"}
      />
    </div>
  );
}
