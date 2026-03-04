import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Badge } from "../components/ui/badge";
import { Switch } from "../components/ui/switch";
import { Label } from "../components/ui/label";
import { Progress } from "../components/ui/progress";
import { StatusBadge } from "../components/StatusBadge";
import { ScoreBadge } from "../components/ScoreBadge";
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
  Search,
  Send,
  Trash2,
} from "lucide-react";
import { api, AggregateJobStatus, ApiError, ArticleDetails, ArticleListItem, CostSummary, formatDateTime, WorkerStatus } from "../lib/api";

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
  const actionsRef = useRef<HTMLDivElement | null>(null);

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
    setLoading(true);
    setError("");
    try {
      const [articleData, costData, workerData] = await Promise.all([
        api.listArticles({
          view: selectedSection,
          page: String(currentPage),
          page_size: pageSize,
          q: searchQuery.trim(),
          ...(noDoubleFilter ? { hide_double: "1" } : {}),
        }),
        api.getCosts(),
        api.getWorkerStatus(),
      ]);
      setArticles(articleData.items || []);
      setTotal(articleData.total || 0);
      setCosts(costData);
      setWorker(workerData);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить статьи.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadDashboard();
  }, [currentPage, navigate, noDoubleFilter, pageSize, searchQuery, selectedSection]);

  async function openPreview(articleId: number) {
    try {
      const details = await api.getArticle(articleId);
      setPreviewArticle(details);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось открыть превью.");
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

  function promptDelete(id: number) {
    const reason = window.prompt(`Почему удалить статью #${id}?`);
    if (!reason || reason.trim().length < 5) return;
    runAction(() => api.deleteArticle(id, reason.trim()));
  }

  function promptPublish(id: number) {
    const reason = window.prompt(`Почему публикуем статью #${id}?`);
    if (!reason || reason.trim().length < 5) return;
    runAction(async () => {
      await api.postArticleAction(id, "feedback", { explanation_text: reason.trim() });
      await api.postArticleAction(id, "publish");
    });
  }

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
                <TableHead className="w-20">Оценка</TableHead>
                <TableHead>Заголовок</TableHead>
                <TableHead className="w-40">Источник</TableHead>
                <TableHead className="w-40">Дата</TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
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
                articles.map((article) => (
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
                    <TableCell>{article.score_10 != null ? <ScoreBadge score={article.score_10} size="sm" /> : "—"}</TableCell>
                    <TableCell>
                      <div className="space-y-1 max-w-lg">
                        <div className="font-medium line-clamp-1">
                          <Link
                            to={`/article/${article.id}`}
                            className="hover:underline"
                            onClick={(event) => event.stopPropagation()}
                          >
                            {article.ru_title || article.title}
                          </Link>
                        </div>
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
                                navigate(`/article/${article.id}`);
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
                ))
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

      <Dialog open={Boolean(previewArticle)} onOpenChange={(open) => !open && setPreviewArticle(null)}>
        <DialogContent className="max-w-4xl">
          <DialogHeader>
            <DialogTitle>{previewArticle?.ru_title || previewArticle?.title}</DialogTitle>
            <DialogDescription className="flex items-center gap-2 flex-wrap">
              {previewArticle ? <StatusBadge status={previewArticle.status} /> : null}
              {previewArticle?.score_10 != null ? <ScoreBadge score={previewArticle.score_10} size="sm" /> : null}
              <span>{previewArticle?.source_name}</span>
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <div className="text-sm text-muted-foreground whitespace-pre-wrap">
              {previewArticle?.post_preview || previewArticle?.ru_summary || previewArticle?.subtitle || "Превью пока не готово."}
            </div>
            {previewArticle?.image_web ? (
              <img src={previewArticle.image_web} alt="" className="rounded-lg border border-border max-h-80 object-cover" />
            ) : null}
            <div className="flex gap-2">
              {previewArticle ? (
                <Button asChild>
                  <Link to={`/article/${previewArticle.id}`}>Открыть редактор</Link>
                </Button>
              ) : null}
              {previewArticle?.canonical_url ? (
                <Button variant="outline" asChild>
                  <a href={previewArticle.canonical_url} target="_blank" rel="noreferrer">
                    Original source
                  </a>
                </Button>
              ) : null}
            </div>
          </div>
        </DialogContent>
      </Dialog>
    </div>
  );
}
