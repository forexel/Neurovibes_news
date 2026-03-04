import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Textarea } from "../components/ui/textarea";
import { Label } from "../components/ui/label";
import { Switch } from "../components/ui/switch";
import { Calendar as DateCalendar } from "../components/ui/calendar";
import { Popover, PopoverContent, PopoverTrigger } from "../components/ui/popover";
import { StatusBadge } from "../components/StatusBadge";
import { ScoreBadge } from "../components/ScoreBadge";
import { LogPanel } from "../components/LogPanel";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import {
  Archive,
  ArrowLeft,
  Calendar,
  ExternalLink,
  FileText,
  Clock,
  Loader2,
  Save,
  Send,
  Trash2,
  Upload,
  Wand2,
} from "lucide-react";
import { api, ApiError, ArticleDetails, formatDateTime } from "../lib/api";
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

export default function ArticleEditor() {
  const { id } = useParams();
  const navigate = useNavigate();
  const articleId = Number(id);

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
  const [scheduleDate, setScheduleDate] = useState("");
  const [schedulePopoverOpen, setSchedulePopoverOpen] = useState(false);

  function addLog(type: LogEntry["type"], message: string) {
    setLogs((prev) => [...prev, { type, message, timestamp: stamp() }]);
  }

  async function loadArticle() {
    if (!Number.isFinite(articleId)) return;
    setPageLoading(true);
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
      setScheduleDate(toInputDate(data.scheduled_publish_at));
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      addLog("error", err instanceof Error ? err.message : "Не удалось загрузить статью.");
    } finally {
      setPageLoading(false);
    }
  }

  useEffect(() => {
    loadArticle();
  }, [articleId]);

  const postPreview = useMemo(() => article?.post_preview || "RU текст не готов. Сгенерируй пост и сохрани его.", [article]);
  const selectedScheduleDate = useMemo(() => getScheduleDateValue(scheduleDate), [scheduleDate]);
  const hasInsufficientContent = String(article?.content_mode || "").toLowerCase() === "summary_only";

  async function publishWithReason() {
    const reason = window.prompt(`Почему публикуем статью #${articleId}?`, feedback || "");
    if (!reason || reason.trim().length < 5) return;
    await api.postArticleAction(articleId, "feedback", { explanation_text: reason.trim() });
    setFeedback(reason.trim());
    await api.postArticleAction(articleId, "publish");
  }

  async function deleteWithReason() {
    const reason = window.prompt(`Почему удалить статью #${articleId}?`);
    if (!reason || reason.trim().length < 5) return;
    setLoading("delete");
    try {
      await api.deleteArticle(articleId, reason.trim());
      navigate("/dashboard");
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

  if (!article) {
    return (
      <div className="min-h-screen bg-background">
        <TopNavigation />
        <div className="max-w-7xl mx-auto px-6 py-12 text-center">
          <h1 className="text-2xl font-semibold mb-4">Статья не найдена</h1>
          <Button asChild>
            <Link to="/dashboard">Вернуться к списку</Link>
          </Button>
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
              <Link to="/dashboard">
                <ArrowLeft className="w-4 h-4 mr-2" />
                К списку
              </Link>
            </Button>
            <div className="flex items-center gap-2">
              <StatusBadge status={article.status} />
              {article.score_10 != null ? <ScoreBadge score={article.score_10} /> : null}
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

        <div className="mb-6 rounded-lg border border-border bg-card p-4">
          <div className="mb-3 text-sm font-medium">ML-вердикт</div>
          <div className="mb-3 flex items-center gap-3">
            <Switch id="ml-verdict-confirmed" checked={mlVerdictConfirmed} onCheckedChange={setMlVerdictConfirmed} />
            <Label htmlFor="ml-verdict-confirmed" className="cursor-pointer">
              Согласен с рекомендацией ML
            </Label>
          </div>
          <div className="space-y-2">
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
              <pre className="whitespace-pre-wrap rounded-lg bg-black/20 border border-border p-4 text-sm">{postPreview}</pre>
              {article.image_web ? <img src={article.image_web} alt="" className="mt-4 rounded-lg border border-border max-h-96 object-cover" /> : null}
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
                      onChange={(e) => setScheduleDate(e.target.value)}
                    />
                  </div>
                  <div className="flex items-end">
                    <Button
                      variant="outline"
                      className="w-full"
                      onClick={() => run("Clear Schedule", () => api.postArticleAction(articleId, "unschedule-publish"))}
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
                    onClick={() => run("Publish", publishWithReason)}
                    disabled={hasInsufficientContent}
                    className="flex-1 min-w-[220px]"
                  >
                    <Send className="w-4 h-4 mr-2" />
                    Опубликовать сейчас
                  </Button>
                  <Button variant="outline" onClick={() => run("Archive", () => api.postArticleAction(articleId, "status", { status: "rejected" }))}>
                    <Archive className="w-4 h-4 mr-2" />
                    В архив
                  </Button>
                  <Button variant="destructive" onClick={deleteWithReason}>
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
                                setSchedulePopoverOpen(false);
                              }}
                            />
                          </PopoverContent>
                        </Popover>
                        <Input
                          type="time"
                          value={scheduleDate ? scheduleDate.slice(11, 16) : ""}
                          onChange={(e) => setScheduleDate((current) => updateScheduleValue(current, undefined, e.target.value || "10:00"))}
                          className="w-32"
                        />
                      </div>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        onClick={() =>
                          run("Schedule", () => api.postArticleAction(articleId, "schedule-publish", { publish_at: scheduleDate }))
                        }
                        disabled={!scheduleDate}
                      >
                        <Calendar className="w-4 h-4 mr-2" />
                        Schedule
                      </Button>
                      <Button variant="outline" onClick={() => run("Clear Schedule", () => api.postArticleAction(articleId, "unschedule-publish"))}>
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
                        onClick={() => run("Publish", publishWithReason)}
                        disabled={hasInsufficientContent}
                      >
                        <Send className="w-4 h-4 mr-2" />
                        Publish
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
                      <Button variant="destructive" onClick={deleteWithReason}>
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
    </div>
  );
}
