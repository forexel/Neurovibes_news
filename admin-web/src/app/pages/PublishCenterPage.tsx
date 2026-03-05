import { ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { Label } from "../components/ui/label";
import { Textarea } from "../components/ui/textarea";
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
import { ScoreBadge } from "../components/ScoreBadge";
import { CheckCircle2, Clock, Edit, Image as ImageIcon, RefreshCw, Send, Trash2 } from "lucide-react";
import { api, ApiError, ArticleListItem, formatDateTime, SetupState } from "../lib/api";

type PublishPanel = "actionable" | "scheduled" | "deleted" | "published";

const ML_REVIEW_MIN = 0.4;
const ML_REVIEW_MAX = 0.75;

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

  async function loadData() {
    setLoading(true);
    setError("");
    try {
      const [setup, allData, publishedData, deletedData] = await Promise.all([
        api.getSetupState(),
        api.listArticles({ view: "all", page: "1", page_size: "300" }),
        api.listArticles({ view: "published", page: "1", page_size: "200" }),
        api.listArticles({ view: "deleted", page: "1", page_size: "200" }),
      ]);
      setSetupState(setup);
      setAllArticles(allData.items || []);
      setPublishedArticles(publishedData.items || []);
      setDeletedArticles(deletedData.items || []);
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

  function openReasonDialog(action: "publish" | "delete", articleId: number) {
    setReasonDialogAction(action);
    setReasonDialogArticleId(articleId);
    setReasonDialogText("");
    setReasonDialogOpen(true);
  }

  async function submitReasonDialog() {
    const articleId = reasonDialogArticleId;
    const reason = reasonDialogText.trim();
    if (!articleId) return;
    if (reason.length < 5) {
      setError("Комментарий должен быть не короче 5 символов.");
      return;
    }
    const label = reasonDialogAction === "publish" ? `Опубликовать #${articleId}` : `Удалить #${articleId}`;
    await runAction(label, async () => {
      if (reasonDialogAction === "publish") {
        await api.postArticleAction(articleId, "feedback", { explanation_text: reason });
        await api.postArticleAction(articleId, "publish");
      } else {
        await api.deleteArticle(articleId, reason);
      }
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
    if (["published", "archived", "rejected", "double"].includes(String(item.status || "").toLowerCase())) {
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
          const isOperationalQueue =
            ["review", "scored", "selected_hourly", "ready"].includes(item.status) &&
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
          onOpenArticle={(articleId) =>
            navigate(`/article/${articleId}`, {
              state: { from: `${location.pathname}${location.search || ""}` },
            })
          }
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
        <Dialog open={reasonDialogOpen} onOpenChange={setReasonDialogOpen}>
          <DialogContent className="sm:max-w-lg">
            <DialogHeader>
              <DialogTitle>{reasonDialogAction === "publish" ? "Причина публикации" : "Причина удаления"}</DialogTitle>
              <DialogDescription>
                Добавь комментарий по статье #{reasonDialogArticleId ?? "—"}.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2">
              <Label htmlFor="publish-center-reason">
                {reasonDialogAction === "publish" ? "Почему публикуем?" : "Почему удаляем?"}
              </Label>
              <Textarea
                id="publish-center-reason"
                value={reasonDialogText}
                onChange={(e) => setReasonDialogText(e.target.value)}
                rows={6}
                placeholder="Комментарий для истории действий и обучения"
              />
            </div>
            <div className="flex justify-end gap-2">
              <Button variant="outline" onClick={() => setReasonDialogOpen(false)}>
                Отмена
              </Button>
              <Button
                onClick={submitReasonDialog}
                disabled={reasonDialogText.trim().length < 5 || actionLoading !== null}
                className={reasonDialogAction === "delete" ? "bg-destructive text-destructive-foreground hover:bg-destructive/90" : ""}
              >
                {reasonDialogAction === "publish" ? "Опубликовать" : "Удалить"}
              </Button>
            </div>
          </DialogContent>
        </Dialog>
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
  onOpenArticle?: (articleId: number) => void;
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
                onClick={() => onOpenArticle?.(article.id)}
              >
                <TableCell className="font-mono text-xs text-muted-foreground">#{article.id}</TableCell>
                <TableCell>
                  <StatusBadge status={article.status} />
                </TableCell>
                <TableCell>{article.score_10 != null ? <ScoreBadge score={article.score_10} size="sm" /> : "—"}</TableCell>
                <TableCell>
                  <div className="space-y-1 max-w-lg">
                    <div className="font-medium line-clamp-1">{article.ru_title || article.title}</div>
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
