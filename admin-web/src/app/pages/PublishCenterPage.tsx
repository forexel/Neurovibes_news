import { ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "../components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { StatusBadge } from "../components/StatusBadge";
import { ReasonActionDialog } from "../components/ReasonActionDialog";
import { CheckCircle2, Clock, Edit, Image as ImageIcon, Loader2, RefreshCw, Send, Trash2 } from "lucide-react";
import { api, ApiError, ArticleDetails, ArticleListItem, formatDateTime, ReasonTagOption, SetupState } from "../lib/api";

type PublishPanel = "actionable" | "scheduled" | "deleted" | "published";

const ML_REVIEW_MIN = 0.4;
const ML_REVIEW_MAX = 0.75;
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

function mlScore10(item: ArticleListItem): string {
  if (typeof item.ml_recommendation_confidence !== "number") return "—";
  return (item.ml_recommendation_confidence * 10).toFixed(1);
}

function statusKey(item: ArticleListItem): string {
  return String(item.status || "").toLowerCase().replace("articlestatus.", "");
}

function normalizePreviewText(input?: string | null): string {
  const raw = String(input || "").trim();
  if (!raw) return "";
  let text = raw
    .replace(/<br\s*\/?>/gi, "\n")
    .replace(/<\/p>\s*<p>/gi, "\n\n")
    .replace(/<\/li>\s*<li>/gi, "\n")
    .replace(/<li>/gi, "• ")
    .replace(/<\/?[^>]+>/g, "");
  if (typeof document !== "undefined" && text.includes("&")) {
    const el = document.createElement("textarea");
    el.innerHTML = text;
    text = el.value;
  }
  return text
    .replace(/\u00a0/g, " ")
    .replace(/[ \t]+\n/g, "\n")
    .replace(/\n{3,}/g, "\n\n")
    .trim();
}

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

export default function PublishCenterPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const [setupState, setSetupState] = useState<SetupState | null>(null);
  const [allArticles, setAllArticles] = useState<ArticleListItem[]>([]);
  const [deletedArticles, setDeletedArticles] = useState<ArticleListItem[]>([]);
  const [publishedArticles, setPublishedArticles] = useState<ArticleListItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [activePanel, setActivePanel] = useState<PublishPanel>("actionable");
  const [reasonDialogOpen, setReasonDialogOpen] = useState(false);
  const [reasonDialogAction, setReasonDialogAction] = useState<"publish" | "delete">("publish");
  const [reasonDialogArticleId, setReasonDialogArticleId] = useState<number | null>(null);
  const [reasonDialogText, setReasonDialogText] = useState("");
  const [reasonDialogTags, setReasonDialogTags] = useState<string[]>([]);
  const [reasonDialogCustomTag, setReasonDialogCustomTag] = useState("");
  const [catalogReasonTagOptions, setCatalogReasonTagOptions] = useState<ReasonTagOption[]>([]);
  const [previewArticle, setPreviewArticle] = useState<(ArticleListItem & Partial<ArticleDetails>) | null>(null);
  const [previewLoadingId, setPreviewLoadingId] = useState<number | null>(null);

  const previewPostText = useMemo(
    () =>
      normalizePreviewText(
        previewArticle?.post_preview || previewArticle?.ru_summary || previewArticle?.subtitle || "RU текст не готов.",
      ),
    [previewArticle?.post_preview, previewArticle?.ru_summary, previewArticle?.subtitle],
  );
  const previewEnglishText = useMemo(
    () => normalizePreviewText(previewArticle?.english_preview || ""),
    [previewArticle?.english_preview],
  );
  const showEnglishBlock = useMemo(() => {
    if (!previewEnglishText) return false;
    const left = previewEnglishText.replace(/\s+/g, " ").trim().toLowerCase();
    const right = previewPostText.replace(/\s+/g, " ").trim().toLowerCase();
    return left !== right;
  }, [previewEnglishText, previewPostText]);

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

  async function loadData() {
    setLoading(true);
    setError("");
    try {
      // Fast-path: fetch only one actionable card without total counting.
      const fastAll = await api.listArticles({
        view: "all",
        page: "1",
        page_size: "1",
        include_total: false,
      });
      setAllArticles(fastAll.items || []);
      setLoading(false);

      // Background loading for full data and secondary panels (do not block the whole page).
      const [setupRes, allRes, publishedRes, deletedRes, tagsRes] = await Promise.allSettled([
        api.getSetupState(),
        api.listArticles({ view: "all", page: "1", page_size: "50" }),
        api.listArticles({ view: "published", page: "1", page_size: "100" }),
        api.listArticles({ view: "deleted", page: "1", page_size: "100" }),
        api.getReasonTags(),
      ]);

      const errors: string[] = [];
      if (setupRes.status === "fulfilled") setSetupState(setupRes.value);
      else errors.push("настройки");
      if (allRes.status === "fulfilled") setAllArticles(allRes.value.items || []);
      else errors.push("очередь");
      if (publishedRes.status === "fulfilled") setPublishedArticles(publishedRes.value.items || []);
      else errors.push("опубликованные");
      if (deletedRes.status === "fulfilled") setDeletedArticles(deletedRes.value.items || []);
      else errors.push("удалённые");
      if (tagsRes.status === "fulfilled") setCatalogReasonTagOptions(Array.isArray(tagsRes.value.items) ? tagsRes.value.items : []);
      else errors.push("теги");
      if (errors.length) setError(`Часть данных не загрузилась (${errors.join(", ")}). Нажми «Обновить».`);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить publish center.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, [navigate]);

  async function runAction(label: string, action: () => Promise<unknown>) {
    setActionLoading(label);
    setError("");
    try {
      await action();
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : `Не удалось выполнить действие: ${label}`);
    } finally {
      setActionLoading(null);
    }
  }

  function openPreview(item: ArticleListItem) {
    setPreviewLoadingId(item.id);
    setPreviewArticle(item);
    setPreviewLoadingId(null);
  }

  function openReasonDialog(action: "publish" | "delete", articleId: number) {
    setReasonDialogAction(action);
    setReasonDialogArticleId(articleId);
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
  const isDeletingFromReasonDialog =
    reasonDialogAction === "delete" && Boolean(actionLoading && actionLoading.startsWith("Удалить #"));

  async function submitReasonDialog() {
    const articleId = reasonDialogArticleId;
    const reason = reasonDialogText.trim();
    if (!articleId) return;
    if (reason.length < 5) {
      setError("Комментарий должен быть не короче 5 символов.");
      return;
    }
    const tags = reasonDialogTags.map(normalizeTag).filter(Boolean);
    const label = reasonDialogAction === "publish" ? `Опубликовать #${articleId}` : `Удалить #${articleId}`;
    if (reasonDialogAction === "delete") {
      setActionLoading(label);
      setError("");
      try {
        const payload = ["decision=delete", `tags=${tags.join(",")}`, `reason_text=${reason}`].join("\n");
        await api.deleteArticle(articleId, payload);
        setReasonDialogOpen(false);
        setReasonDialogArticleId(null);
        setReasonDialogText("");
        setReasonDialogTags([]);
        setReasonDialogCustomTag("");
        setAllArticles((prev) => prev.filter((item) => item.id !== articleId));
        void loadData();
      } catch (err) {
        setError(err instanceof Error ? err.message : `Не удалось выполнить действие: ${label}`);
      } finally {
        setActionLoading(null);
      }
      return;
    }

    await runAction(label, async () => {
      const payload = tags.length
        ? ["decision=publish", `tags=${tags.join(",")}`, `reason_text=${reason}`].join("\n")
        : reason;
      await api.postArticleAction(articleId, "feedback", { explanation_text: payload });
      await api.postArticleAction(articleId, "publish");
    });
    setReasonDialogOpen(false);
  }

  const scheduledArticles = useMemo(
    () =>
      allArticles
        .filter((item) => Boolean(item.scheduled_publish_at))
        .sort((a, b) => String(a.scheduled_publish_at || "").localeCompare(String(b.scheduled_publish_at || ""))),
    [allArticles],
  );
  const needsMlValidation = (item: ArticleListItem) => {
    const st = statusKey(item);
    if (["published", "archived", "rejected", "double"].includes(st)) {
      return false;
    }
    if (String(item.content_mode || "").toLowerCase() === "summary_only") {
      return false;
    }
    const rec = String(item.ml_recommendation || "").toLowerCase();
    const hasMl = Boolean(rec) || Boolean(item.ml_recommendation_at);
    if (!hasMl) return false;

    const conf = typeof item.ml_recommendation_confidence === "number" ? item.ml_recommendation_confidence : null;
    const inGrayBand = conf !== null && conf >= ML_REVIEW_MIN && conf <= ML_REVIEW_MAX;
    const explicitReview = rec === "review";
    const notValidated = !item.ml_verdict_updated_at;
    return explicitReview || inGrayBand || notValidated;
  };
  const draftsArticles = useMemo(
    () =>
      allArticles
        .filter((item) => {
          const st = statusKey(item);
          const isOperationalQueue =
            ["review", "scored", "selected_hourly", "ready"].includes(st) &&
            String(item.content_mode || "").toLowerCase() !== "summary_only";
          return isOperationalQueue || needsMlValidation(item);
        })
        .sort((a, b) => {
          const aVal = needsMlValidation(a) ? 1 : 0;
          const bVal = needsMlValidation(b) ? 1 : 0;
          if (aVal !== bVal) return bVal - aVal;
          return String(b.created_at || b.published_at || "").localeCompare(String(a.created_at || a.published_at || ""));
        }),
    [allArticles],
  );

  const panelMeta = useMemo<Record<PublishPanel, { title: string; items: ArticleListItem[]; emptyTitle: string; emptyText: string; showSchedule?: boolean; showPublishNow?: boolean; showUnschedule?: boolean; showDelete?: boolean; emptyIcon: ReactNode }>>(
    () => ({
      actionable: {
        title: "Нужно сделать",
        items: draftsArticles,
        emptyTitle: "Нет статей для обработки",
        emptyText: "Здесь статьи, где надо принять решение: дописать RU текст, проверить карточку, опубликовать или удалить",
        emptyIcon: <ImageIcon className="w-12 h-12 text-muted-foreground mx-auto mb-4" />,
        showPublishNow: true,
        showDelete: true,
      },
      scheduled: {
        title: "Запланированные публикации",
        items: scheduledArticles,
        emptyTitle: "Нет запланированных публикаций",
        emptyText: "Поставь время публикации в карточке статьи",
        emptyIcon: <Clock className="w-12 h-12 text-muted-foreground mx-auto mb-4" />,
        showSchedule: true,
        showPublishNow: true,
        showUnschedule: true,
        showDelete: true,
      },
      deleted: {
        title: "Удалённые и архив",
        items: deletedArticles,
        emptyTitle: "Нет удалённых статей",
        emptyText: "Архивированные и удалённые материалы появятся здесь",
        emptyIcon: <Trash2 className="w-12 h-12 text-muted-foreground mx-auto mb-4" />,
      },
      published: {
        title: "Уже опубликовано",
        items: publishedArticles,
        emptyTitle: "Нет опубликованных статей",
        emptyText: "Публикации появятся здесь после отправки в Telegram",
        emptyIcon: <Send className="w-12 h-12 text-muted-foreground mx-auto mb-4" />,
      },
    }),
    [deletedArticles, draftsArticles, publishedArticles, scheduledArticles],
  );

  const activeList = panelMeta[activePanel];

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Центр публикаций</h1>
            <p className="text-sm text-muted-foreground">Главная страница по очереди, расписанию и истории публикаций.</p>
          </div>
          <div className="flex flex-wrap gap-3">
            <Button variant="outline" onClick={() => loadData()} disabled={loading || actionLoading !== null}>
              <RefreshCw className={`w-4 h-4 mr-2 ${loading ? "animate-spin" : ""}`} />
              Обновить
            </Button>
            <Button
              onClick={() => runAction("Опубликовать по расписанию", () => api.publishScheduledDue(20))}
              disabled={actionLoading !== null}
            >
              <Send className={`w-4 h-4 mr-2 ${actionLoading === "Опубликовать по расписанию" ? "animate-pulse" : ""}`} />
              Опубликовать по расписанию
            </Button>
          </div>
        </div>

        {error ? <div className="mb-4 text-sm text-destructive">{error}</div> : null}

        <div className="mb-6 bg-card border border-border rounded-lg p-6">
          <h3 className="font-semibold mb-4">Настройки публикации</h3>
          <div className="grid grid-cols-1 md:grid-cols-4 gap-4 text-sm">
            <div>
              <div className="text-muted-foreground mb-1">Часовой пояс</div>
              <div className="font-medium">{setupState?.timezone_name || "Europe/Moscow"}</div>
            </div>
            <div>
              <div className="text-muted-foreground mb-1">Канал по умолчанию</div>
              <div className="font-medium">{setupState?.telegram_channel_id || "—"}</div>
            </div>
            <div>
              <div className="text-muted-foreground mb-1">Подпись</div>
              <div className="font-medium">{setupState?.telegram_signature || "—"}</div>
            </div>
            <div>
              <div className="text-muted-foreground mb-1">Review chat</div>
              <div className="font-medium">{setupState?.telegram_review_chat_id || "—"}</div>
            </div>
          </div>
        </div>

        <div className="mb-6 grid grid-cols-1 md:grid-cols-4 gap-4">
          <StatCard
            icon={<Edit className="w-5 h-5 text-blue-400" />}
            label="Нужно сделать"
            description="Тексты, подготовка, ручные решения"
            value={draftsArticles.length}
            active={activePanel === "actionable"}
            onClick={() => setActivePanel("actionable")}
          />
          <StatCard
            icon={<Clock className="w-5 h-5 text-yellow-400" />}
            label="Запланировано"
            description="Все статьи с датой публикации"
            value={scheduledArticles.length}
            active={activePanel === "scheduled"}
            onClick={() => setActivePanel("scheduled")}
          />
          <StatCard
            icon={<Trash2 className="w-5 h-5 text-red-400" />}
            label="Удалённые"
            description="Архив и удалённые материалы"
            value={deletedArticles.length}
            active={activePanel === "deleted"}
            onClick={() => setActivePanel("deleted")}
          />
          <StatCard
            icon={<CheckCircle2 className="w-5 h-5 text-green-400" />}
            label="Опубликовано"
            description="Что уже ушло в канал"
            value={publishedArticles.length}
            active={activePanel === "published"}
            onClick={() => setActivePanel("published")}
          />
        </div>

        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 className="text-lg font-semibold">{activeList.title}</h2>
            <p className="text-sm text-muted-foreground">Клик по карточкам выше переключает список статей.</p>
          </div>
          <Badge variant="outline" className="text-xs">
            {activeList.items.length} шт.
          </Badge>
        </div>

        <ArticlesTable
          items={activeList.items}
          emptyIcon={activeList.emptyIcon}
          emptyTitle={activeList.emptyTitle}
          emptyText={activeList.emptyText}
          showSchedule={activeList.showSchedule}
          actionLoading={actionLoading}
          previewLoadingId={previewLoadingId}
          onOpenArticle={(item) => openPreview(item)}
          onDeleteArticle={
            activeList.showDelete
              ? (articleId) => openReasonDialog("delete", articleId)
              : undefined
          }
          onPublishNow={
            activeList.showPublishNow
              ? (articleId) => openReasonDialog("publish", articleId)
              : undefined
          }
          onUnschedule={
            activeList.showUnschedule
              ? (articleId) => runAction(`Снять расписание #${articleId}`, () => api.postArticleAction(articleId, "unschedule-publish"))
              : undefined
          }
        />

        <Dialog open={Boolean(previewArticle)} onOpenChange={(open) => !open && setPreviewArticle(null)}>
          <DialogContent className="max-w-4xl h-[92vh] overflow-hidden p-0 [&>button]:right-4 [&>button]:top-4 [&>button]:h-9 [&>button]:w-9 [&>button]:p-0 [&>button]:inline-flex [&>button]:items-center [&>button]:justify-center [&>button]:rounded-md [&>button]:border [&>button]:border-border [&>button_svg]:h-4 [&>button_svg]:w-4">
            <div className="flex h-full max-h-[92vh] flex-col overflow-hidden">
              <DialogHeader className="shrink-0 border-b border-border px-6 pt-6 pb-4">
                <DialogTitle className="pr-16 leading-tight">{previewArticle?.ru_title || previewArticle?.title || "Превью статьи"}</DialogTitle>
                <DialogDescription className="mt-1 flex flex-wrap items-center gap-2">
                  {previewArticle ? <StatusBadge status={previewArticle.status} /> : null}
                  <span>{previewArticle?.source_name || "Источник не указан"}</span>
                  <span>•</span>
                  <span>{formatDateTime(previewArticle?.published_at || previewArticle?.created_at)}</span>
                </DialogDescription>
              </DialogHeader>

              <div className="flex-1 overflow-y-auto px-6 py-4">
                <div className="space-y-3">
                  <div className="rounded-lg border border-border bg-muted/20 p-4 text-sm">
                    <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">Пост для Telegram</div>
                    <div className="whitespace-pre-wrap leading-7 break-words">{previewPostText || "RU текст не готов."}</div>
                  </div>
                  {showEnglishBlock ? (
                    <div className="rounded-lg border border-border bg-muted/20 p-4 text-sm text-muted-foreground">
                      <div className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">English Preview</div>
                      <div className="whitespace-pre-wrap leading-7 break-words">{previewEnglishText}</div>
                    </div>
                  ) : null}
                </div>

                {previewArticle?.generated_image_path ? (
                  <div className="mt-3">
                    <Badge variant="outline" className="text-xs gap-1">
                      <CheckCircle2 className="w-3 h-3 text-green-400" />
                      Изображение готово
                    </Badge>
                  </div>
                ) : null}
              </div>

              <div className="shrink-0 border-t border-border px-6 py-4">
                <div className="flex flex-wrap justify-end gap-2">
                  <Button
                    variant="outline"
                    onClick={() => {
                      if (!previewArticle) return;
                      navigate(`/article/${previewArticle.id}`, {
                        state: { from: `${location.pathname}${location.search || ""}` },
                      });
                    }}
                  >
                    Открыть в редакторе
                  </Button>
                  {previewArticle && activeList.showDelete ? (
                    <Button
                      variant="destructive"
                      onClick={() => {
                        const id = previewArticle.id;
                        setPreviewArticle(null);
                        openReasonDialog("delete", id);
                      }}
                    >
                      <Trash2 className="w-4 h-4 mr-1" />
                      Удалить
                    </Button>
                  ) : null}
                  {previewArticle && activeList.showPublishNow ? (
                    <Button
                      onClick={() => {
                        const id = previewArticle.id;
                        setPreviewArticle(null);
                        openReasonDialog("publish", id);
                      }}
                    >
                      <Send className="w-4 h-4 mr-1" />
                      Опубликовать
                    </Button>
                  ) : null}
                </div>
              </div>
            </div>
          </DialogContent>
        </Dialog>

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
          loading={actionLoading !== null}
          loadingDelete={isDeletingFromReasonDialog}
        />
      </div>
    </div>
  );
}

function StatCard({
  icon,
  label,
  description,
  value,
  active = false,
  onClick,
}: {
  icon: ReactNode;
  label: string;
  description: string;
  value: number;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`bg-card border rounded-lg p-6 text-left transition-colors hover:border-primary/40 hover:bg-muted/30 ${
        active ? "border-primary/50 bg-primary/5" : "border-border"
      }`}
    >
      <div className="flex items-center gap-3 mb-2">
        {icon}
        <div className="text-sm text-muted-foreground">{label}</div>
      </div>
      <div className="text-3xl font-semibold">{value}</div>
      <div className="mt-2 text-xs text-muted-foreground">{description}</div>
    </button>
  );
}

function ArticlesTable({
  items,
  emptyIcon,
  emptyTitle,
  emptyText,
  showSchedule = false,
  actionLoading = null,
  previewLoadingId = null,
  onOpenArticle,
  onDeleteArticle,
  onPublishNow,
  onUnschedule,
}: {
  items: ArticleListItem[];
  emptyIcon: ReactNode;
  emptyTitle: string;
  emptyText: string;
  showSchedule?: boolean;
  actionLoading?: string | null;
  previewLoadingId?: number | null;
  onOpenArticle?: (item: ArticleListItem) => void;
  onDeleteArticle?: (articleId: number) => void;
  onPublishNow?: (articleId: number) => void;
  onUnschedule?: (articleId: number) => void;
}) {
  if (items.length === 0) {
    return (
      <div className="bg-card border border-border rounded-lg p-12 text-center">
        {emptyIcon}
        <h3 className="text-lg font-semibold mb-2">{emptyTitle}</h3>
        <p className="text-muted-foreground mb-6">{emptyText}</p>
        <Button asChild>
          <Link to="/dashboard">Перейти к статьям</Link>
        </Button>
      </div>
    );
  }

  return (
    <div className="bg-card border border-border rounded-lg overflow-hidden">
      <Table>
        <TableHeader>
          <TableRow className="hover:bg-transparent border-b border-border">
            <TableHead className="w-16">ID</TableHead>
            <TableHead className="w-24">Статус</TableHead>
            <TableHead className="w-20">Оценка</TableHead>
            <TableHead>Заголовок</TableHead>
            {showSchedule ? <TableHead className="w-48">Дата публикации</TableHead> : <TableHead className="w-32">Изображение</TableHead>}
            <TableHead className="w-56">Действия</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((article) => {
            const publishLabel = `Опубликовать #${article.id}`;
            const unscheduleLabel = `Снять расписание #${article.id}`;
            return (
              <TableRow
                key={article.id}
                className="cursor-pointer hover:bg-muted/50"
                onClick={() => onOpenArticle?.(article)}
              >
                <TableCell className="font-mono text-xs text-muted-foreground">#{article.id}</TableCell>
                <TableCell>
                  <StatusBadge status={article.status} />
                </TableCell>
                <TableCell className="font-medium">{mlScore10(article)}</TableCell>
                <TableCell>
                  <div className="space-y-1 max-w-lg">
                    <div className="font-medium line-clamp-1">
                      {article.ru_title || article.title}
                      {previewLoadingId === article.id ? <Loader2 className="inline-block ml-2 w-3.5 h-3.5 animate-spin" /> : null}
                    </div>
                    {article.short_hook || article.subtitle ? (
                      <div className="text-sm text-muted-foreground line-clamp-2">{article.short_hook || article.subtitle}</div>
                    ) : null}
                  </div>
                </TableCell>
                {showSchedule ? (
                  <TableCell className="text-sm">{formatDateTime(article.scheduled_publish_at)}</TableCell>
                ) : (
                  <TableCell>
                    {article.generated_image_path ? (
                      <Badge variant="outline" className="text-xs gap-1">
                        <CheckCircle2 className="w-3 h-3 text-green-400" />
                        Есть
                      </Badge>
                    ) : (
                      <Badge variant="outline" className="text-xs gap-1">
                        <ImageIcon className="w-3 h-3" />
                        Нет
                      </Badge>
                    )}
                  </TableCell>
                )}
                <TableCell>
                  <div className="flex flex-wrap gap-2">
                    {onDeleteArticle ? (
                      <Button
                        size="sm"
                        variant="ghost"
                        className="px-3 text-destructive hover:bg-destructive/10 hover:text-destructive"
                        onClick={(event) => {
                          event.stopPropagation();
                          onDeleteArticle(article.id);
                        }}
                        aria-label={`Удалить статью #${article.id}`}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    ) : null}
                    {onPublishNow ? (
                      <Button
                        size="sm"
                        onClick={(event) => {
                          event.stopPropagation();
                          onPublishNow(article.id);
                        }}
                        disabled={actionLoading === publishLabel}
                      >
                        <Send className="w-4 h-4 mr-1" />
                        Сейчас
                      </Button>
                    ) : null}
                    {onUnschedule ? (
                      <Button
                        size="sm"
                        variant="outline"
                        onClick={(event) => {
                          event.stopPropagation();
                          onUnschedule(article.id);
                        }}
                        disabled={actionLoading === unscheduleLabel}
                      >
                        Снять
                      </Button>
                    ) : null}
                  </div>
                </TableCell>
              </TableRow>
            );
          })}
        </TableBody>
      </Table>
    </div>
  );
}
