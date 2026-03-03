import { ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
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
import { Calendar, CheckCircle2, Clock, Edit, Image as ImageIcon, Send } from "lucide-react";
import { api, ApiError, ArticleListItem, formatDateTime, SetupState } from "../lib/api";

export default function PublishCenterPage() {
  const navigate = useNavigate();
  const [setupState, setSetupState] = useState<SetupState | null>(null);
  const [articles, setArticles] = useState<ArticleListItem[]>([]);
  const [error, setError] = useState("");

  useEffect(() => {
    async function loadData() {
      try {
        const [setup, articleData] = await Promise.all([
          api.getSetupState(),
          api.listArticles({ view: "all", page: "1", page_size: "100" }),
        ]);
        setSetupState(setup);
        setArticles(articleData.items || []);
      } catch (err) {
        if (err instanceof ApiError && err.status === 401) {
          navigate("/login", { replace: true });
          return;
        }
        setError(err instanceof Error ? err.message : "Не удалось загрузить publish center.");
      }
    }

    loadData();
  }, [navigate]);

  const draftArticles = useMemo(
    () => articles.filter((item) => ["ready", "selected_hourly"].includes(item.status)),
    [articles],
  );
  const scheduledArticles = useMemo(() => articles.filter((item) => Boolean(item.scheduled_publish_at)), [articles]);
  const publishedArticles = useMemo(() => articles.filter((item) => item.status === "published"), [articles]);

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold mb-1">Центр публикаций</h1>
          <p className="text-sm text-muted-foreground">Очередь публикаций, расписание и опубликованные материалы.</p>
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
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="flex items-center gap-3 mb-2">
              <Edit className="w-5 h-5 text-blue-400" />
              <div className="text-sm text-muted-foreground">Черновики</div>
            </div>
            <div className="text-3xl font-semibold">{draftArticles.length}</div>
          </div>
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="flex items-center gap-3 mb-2">
              <Clock className="w-5 h-5 text-yellow-400" />
              <div className="text-sm text-muted-foreground">Запланировано</div>
            </div>
            <div className="text-3xl font-semibold">{scheduledArticles.length}</div>
          </div>
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="flex items-center gap-3 mb-2">
              <CheckCircle2 className="w-5 h-5 text-green-400" />
              <div className="text-sm text-muted-foreground">Опубликовано</div>
            </div>
            <div className="text-3xl font-semibold">{publishedArticles.length}</div>
          </div>
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="flex items-center gap-3 mb-2">
              <Calendar className="w-5 h-5 text-purple-400" />
              <div className="text-sm text-muted-foreground">Ближайшие</div>
            </div>
            <div className="text-3xl font-semibold">{scheduledArticles.slice(0, 3).length}</div>
          </div>
        </div>

        <Tabs defaultValue="queue" className="space-y-6">
          <TabsList>
            <TabsTrigger value="queue">Очередь публикаций</TabsTrigger>
            <TabsTrigger value="scheduled">Запланированные</TabsTrigger>
            <TabsTrigger value="published">Опубликованные</TabsTrigger>
          </TabsList>

          <TabsContent value="queue">
            <ArticlesTable
              items={draftArticles}
              emptyIcon={<Edit className="w-12 h-12 text-muted-foreground mx-auto mb-4" />}
              emptyTitle="Нет готовых статей"
              emptyText='Статьи со статусом "ready" или "selected_hourly" появятся здесь'
            />
          </TabsContent>

          <TabsContent value="scheduled">
            <ArticlesTable
              items={scheduledArticles}
              emptyIcon={<Clock className="w-12 h-12 text-muted-foreground mx-auto mb-4" />}
              emptyTitle="Нет запланированных публикаций"
              emptyText="Поставь время публикации в карточке статьи"
              showSchedule
            />
          </TabsContent>

          <TabsContent value="published">
            <ArticlesTable
              items={publishedArticles}
              emptyIcon={<Send className="w-12 h-12 text-muted-foreground mx-auto mb-4" />}
              emptyTitle="Нет опубликованных статей"
              emptyText="Публикации появятся здесь после отправки в Telegram"
            />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}

function ArticlesTable({
  items,
  emptyIcon,
  emptyTitle,
  emptyText,
  showSchedule = false,
}: {
  items: ArticleListItem[];
  emptyIcon: ReactNode;
  emptyTitle: string;
  emptyText: string;
  showSchedule?: boolean;
}) {
  if (items.length === 0) {
    return (
        <div className="bg-card border border-border rounded-lg p-12 text-center">
        {emptyIcon}
        <h3 className="text-lg font-semibold mb-2">{emptyTitle}</h3>
        <p className="text-muted-foreground mb-6">{emptyText}</p>
        <Button asChild>
          <Link to="/">Перейти к статьям</Link>
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
            <TableHead className="w-32">Действия</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {items.map((article) => (
            <TableRow key={article.id} className="hover:bg-muted/50">
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
                <Button size="sm" variant="outline" asChild>
                  <Link to={`/article/${article.id}`}>
                    <Edit className="w-4 h-4 mr-1" />
                    Открыть
                  </Link>
                </Button>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}
