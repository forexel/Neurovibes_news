import { useEffect, useState } from "react";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import { Switch } from "../components/ui/switch";
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
  DialogTrigger,
} from "../components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../components/ui/dropdown-menu";
import { ExternalLink, Loader2, MoreVertical, Plus, Power, RefreshCw, Trash2 } from "lucide-react";
import { api, ApiError, formatDateTime, SourceItem } from "../lib/api";
import { useNavigate } from "react-router";

export default function SourcesPage() {
  const navigate = useNavigate();
  const [sources, setSources] = useState<SourceItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [showAddDialog, setShowAddDialog] = useState(false);
  const [error, setError] = useState("");
  const [newSource, setNewSource] = useState({
    name: "",
    kind: "rss" as "rss" | "html",
    url: "",
    rank: 50,
  });
  const [checkingSource, setCheckingSource] = useState<number | null>(null);
  const [togglingSource, setTogglingSource] = useState<number | null>(null);

  async function loadSources() {
    setLoading(true);
    setError("");
    try {
      const items = await api.getSources();
      setSources(items);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить источники.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadSources();
  }, [navigate]);

  async function handleAddSource() {
    try {
      await api.addSource({
        name: newSource.name.trim(),
        kind: newSource.kind,
        rss_url: newSource.url.trim(),
        priority_rank: newSource.rank,
      });
      setShowAddDialog(false);
      setNewSource({ name: "", kind: "rss", url: "", rank: 50 });
      await loadSources();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось добавить источник.");
    }
  }

  async function handleToggleActive(id: number, next: boolean) {
    const prev = sources;
    setTogglingSource(id);
    setSources((items) =>
      items.map((source) => (source.id === id ? { ...source, is_active: next } : source)),
    );
    try {
      await api.setSourceActive(id, next);
    } catch (err) {
      setSources(prev);
      setError(err instanceof Error ? err.message : "Не удалось обновить статус.");
    } finally {
      setTogglingSource(null);
    }
  }

  async function handleCheckSource(id: number) {
    setCheckingSource(id);
    try {
      await api.checkSource(id);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Проверка источника завершилась ошибкой.");
    } finally {
      setCheckingSource(null);
    }
  }

  async function handleEditSource(source: SourceItem) {
    const name = window.prompt("Название источника", source.name);
    if (name == null) return;
    const url = window.prompt("URL источника", source.rss_url);
    if (url == null) return;
    const rankValue = window.prompt("Priority rank", String(source.priority_rank));
    if (rankValue == null) return;
    try {
      await api.updateSource(source.id, {
        name: name.trim(),
        rss_url: url.trim(),
        priority_rank: Number(rankValue) || source.priority_rank,
        kind: source.kind,
        is_active: source.is_active,
      });
      await loadSources();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось обновить источник.");
    }
  }

  async function handleDeleteSource(id: number) {
    if (!window.confirm(`Удалить источник #${id}?`)) return;
    try {
      await api.deleteSource(id);
      await loadSources();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить источник.");
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="flex items-center justify-between mb-6">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Источники новостей</h1>
            <p className="text-sm text-muted-foreground">Управление RSS-лентами и веб-источниками</p>
          </div>
          <Dialog open={showAddDialog} onOpenChange={setShowAddDialog}>
            <DialogTrigger asChild>
              <Button>
                <Plus className="w-4 h-4 mr-2" />
                Добавить источник
              </Button>
            </DialogTrigger>
            <DialogContent>
              <DialogHeader>
                <DialogTitle>Добавить новый источник</DialogTitle>
                <DialogDescription>Настрой параметры RSS или HTML-источника.</DialogDescription>
              </DialogHeader>
              <div className="space-y-4 py-4">
                <div className="space-y-2">
                  <Label htmlFor="source-name">Название</Label>
                  <Input id="source-name" value={newSource.name} onChange={(e) => setNewSource({ ...newSource, name: e.target.value })} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="source-kind">Тип</Label>
                  <Select value={newSource.kind} onValueChange={(value) => setNewSource({ ...newSource, kind: value as "rss" | "html" })}>
                    <SelectTrigger id="source-kind">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="rss">RSS Feed</SelectItem>
                      <SelectItem value="html">HTML Page</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <div className="space-y-2">
                  <Label htmlFor="source-url">URL</Label>
                  <Input id="source-url" type="url" value={newSource.url} onChange={(e) => setNewSource({ ...newSource, url: e.target.value })} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="source-rank">Приоритет</Label>
                  <Input
                    id="source-rank"
                    type="number"
                    min="1"
                    max="999"
                    value={newSource.rank}
                    onChange={(e) => setNewSource({ ...newSource, rank: Number(e.target.value) || 50 })}
                  />
                </div>
              </div>
              <div className="flex gap-2">
                <Button onClick={handleAddSource} disabled={!newSource.name.trim() || !newSource.url.trim()}>
                  Добавить
                </Button>
                <Button variant="outline" onClick={() => setShowAddDialog(false)}>
                  Отмена
                </Button>
              </div>
            </DialogContent>
          </Dialog>
        </div>

        {error ? <div className="mb-4 text-sm text-destructive">{error}</div> : null}

        <div className="bg-card border border-border rounded-lg overflow-hidden">
          <Table>
            <TableHeader>
              <TableRow className="hover:bg-transparent border-b border-border">
                <TableHead className="w-16">ID</TableHead>
                <TableHead className="w-24">Статус</TableHead>
                <TableHead className="w-24">Тип</TableHead>
                <TableHead className="w-20">Ранг</TableHead>
                <TableHead>Название</TableHead>
                <TableHead>URL</TableHead>
                <TableHead className="w-32 text-right">Статей</TableHead>
                <TableHead className="w-40">Последняя</TableHead>
                <TableHead className="w-16" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {loading ? (
                <TableRow>
                  <TableCell colSpan={9} className="h-32 text-center text-muted-foreground">
                    Загрузка...
                  </TableCell>
                </TableRow>
              ) : (
                sources.map((source) => (
                  <TableRow key={source.id} className="hover:bg-muted/50">
                    <TableCell className="font-mono text-xs text-muted-foreground">#{source.id}</TableCell>
                    <TableCell>
                      <div className="flex items-center gap-2">
                        <Switch
                          checked={source.is_active}
                          disabled={togglingSource === source.id}
                          onCheckedChange={(checked) => handleToggleActive(source.id, checked)}
                        />
                        <Badge
                          variant="outline"
                          className={
                            source.is_active
                              ? "bg-green-500/20 text-green-300 border-green-500/30"
                              : "bg-gray-500/20 text-gray-400 border-gray-500/30"
                          }
                        >
                          {source.is_active ? "Активен" : "Отключен"}
                        </Badge>
                      </div>
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline" className="text-xs">
                        {source.kind.toUpperCase()}
                      </Badge>
                    </TableCell>
                    <TableCell>
                      <Badge variant="secondary" className="font-mono">
                        {source.priority_rank}
                      </Badge>
                    </TableCell>
                    <TableCell className="font-medium">{source.name}</TableCell>
                    <TableCell>
                      <a href={source.rss_url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:underline inline-flex items-center gap-1 text-sm">
                        <span className="truncate max-w-xs">{source.rss_url}</span>
                        <ExternalLink className="w-3 h-3 flex-shrink-0" />
                      </a>
                    </TableCell>
                    <TableCell className="text-right">
                      <Badge variant="outline">{source.articles_count}</Badge>
                    </TableCell>
                    <TableCell className="text-sm text-muted-foreground">{formatDateTime(source.latest_published_at)}</TableCell>
                    <TableCell>
                      <DropdownMenu>
                        <DropdownMenuTrigger asChild>
                          <Button variant="ghost" size="sm" className="h-8 w-8 p-0">
                            <MoreVertical className="w-4 h-4" />
                          </Button>
                        </DropdownMenuTrigger>
                        <DropdownMenuContent align="end" className="w-48">
                          <DropdownMenuItem onClick={() => handleCheckSource(source.id)} disabled={checkingSource === source.id}>
                            {checkingSource === source.id ? (
                              <>
                                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                                Проверка...
                              </>
                            ) : (
                              <>
                                <RefreshCw className="w-4 h-4 mr-2" />
                                Проверить сейчас
                              </>
                            )}
                          </DropdownMenuItem>
                          <DropdownMenuItem onClick={() => handleEditSource(source)}>
                            Редактировать
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem onClick={() => handleToggleActive(source.id, !source.is_active)}>
                            <Power className="w-4 h-4 mr-2" />
                            {source.is_active ? "Отключить" : "Включить"}
                          </DropdownMenuItem>
                          <DropdownMenuSeparator />
                          <DropdownMenuItem onClick={() => handleDeleteSource(source.id)} className="text-destructive">
                            <Trash2 className="w-4 h-4 mr-2" />
                            Удалить
                          </DropdownMenuItem>
                        </DropdownMenuContent>
                      </DropdownMenu>
                    </TableCell>
                  </TableRow>
                ))
              )}
            </TableBody>
          </Table>
        </div>

        <div className="mt-6 grid grid-cols-1 md:grid-cols-3 gap-4">
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="text-sm text-muted-foreground mb-1">Всего источников</div>
            <div className="text-3xl font-semibold">{sources.length}</div>
          </div>
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="text-sm text-muted-foreground mb-1">Активных</div>
            <div className="text-3xl font-semibold text-green-400">
              {sources.filter((s) => s.is_active).length}
            </div>
          </div>
          <div className="bg-card border border-border rounded-lg p-6">
            <div className="text-sm text-muted-foreground mb-1">Всего статей</div>
            <div className="text-3xl font-semibold">
              {sources.reduce((sum, s) => sum + (s.articles_count || 0), 0)}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
