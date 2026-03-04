import { useEffect, useState } from "react";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Textarea } from "../components/ui/textarea";
import { Switch } from "../components/ui/switch";
import { Badge } from "../components/ui/badge";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "../components/ui/tabs";
import { Plus, RefreshCw, Save, Trash2 } from "lucide-react";
import { api, ApiError, RuntimeSetting, ScoreParameter } from "../lib/api";
import { useNavigate } from "react-router";

export default function ScoreSettingsPage() {
  const navigate = useNavigate();
  const [parameters, setParameters] = useState<ScoreParameter[]>([]);
  const [runtimeSettings, setRuntimeSettings] = useState<RuntimeSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [paramsLoading, setParamsLoading] = useState(true);
  const [runtimeLoading, setRuntimeLoading] = useState(true);

  const [editParam, setEditParam] = useState<Partial<ScoreParameter>>({
    key: "",
    title: "",
    weight: 1.0,
    description: "",
    influence_rule: "",
    is_active: true,
  });

  const [newSetting, setNewSetting] = useState({
    key: "",
    scope: "global" as "global" | "topic",
    topic_key: "",
    value: "",
  });

  async function loadData() {
    setLoading(true);
    setError("");
    setParamsLoading(true);
    setRuntimeLoading(true);
    try {
      const [paramsResult, runtimeResult] = await Promise.allSettled([
        api.getScoreParameters(),
        api.getRuntimeSettings(),
      ]);

      let nextError = "";

      if (paramsResult.status === "fulfilled") {
        setParameters(paramsResult.value);
      } else {
        const err = paramsResult.reason;
        if (err instanceof ApiError && err.status === 401) {
          navigate("/login", { replace: true });
          return;
        }
        setParameters([]);
        nextError = err instanceof Error ? err.message : "Не удалось загрузить параметры оценки.";
      }
      setParamsLoading(false);

      if (runtimeResult.status === "fulfilled") {
        setRuntimeSettings(runtimeResult.value.items || []);
      } else {
        const err = runtimeResult.reason;
        if (err instanceof ApiError && err.status === 401) {
          navigate("/login", { replace: true });
          return;
        }
        setRuntimeSettings([]);
        nextError = nextError || (err instanceof Error ? err.message : "Не удалось загрузить runtime-настройки.");
      }
      setRuntimeLoading(false);

      setError(nextError);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить настройки оценки.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadData();
  }, [navigate]);

  async function handleSaveParameter() {
    try {
      await api.upsertScoreParameter({
        key: String(editParam.key || "").trim(),
        title: String(editParam.title || "").trim(),
        weight: Number(editParam.weight || 0),
        description: String(editParam.description || "").trim(),
        influence_rule: String(editParam.influence_rule || "").trim(),
        is_active: Boolean(editParam.is_active),
      });
      setEditParam({
        key: "",
        title: "",
        weight: 1.0,
        description: "",
        influence_rule: "",
        is_active: true,
      });
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить параметр.");
    }
  }

  async function handleDeleteParameter(id: number) {
    if (!window.confirm(`Удалить параметр #${id}?`)) return;
    try {
      await api.deleteScoreParameter(id);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить параметр.");
    }
  }

  async function handleToggleParameter(param: ScoreParameter) {
    try {
      await api.upsertScoreParameter({
        key: param.key,
        title: param.title,
        weight: Number(param.weight),
        description: param.description || "",
        influence_rule: param.influence_rule || "",
        is_active: !param.is_active,
      });
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось переключить параметр.");
    }
  }

  async function handleSaveRuntime() {
    try {
      await api.upsertRuntimeSetting({
        key: newSetting.key.trim(),
        scope: newSetting.scope,
        topic_key: newSetting.scope === "topic" ? newSetting.topic_key.trim() || null : null,
        value: newSetting.value,
      });
      setNewSetting({ key: "", scope: "global", topic_key: "", value: "" });
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить runtime setting.");
    }
  }

  async function handleDeleteRuntime(id: number) {
    if (!window.confirm(`Удалить runtime setting #${id}?`)) return;
    try {
      await api.deleteRuntimeSetting(id);
      await loadData();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось удалить runtime setting.");
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold mb-1">Настройки оценки</h1>
          <p className="text-sm text-muted-foreground">
            Управление параметрами скоринга и алгоритмами ранжирования
          </p>
        </div>

        {error ? <div className="mb-4 text-sm text-destructive">{error}</div> : null}

        <Tabs defaultValue="parameters" className="space-y-6">
          <TabsList>
            <TabsTrigger value="parameters">Параметры оценки</TabsTrigger>
            <TabsTrigger value="runtime">Runtime настройки</TabsTrigger>
          </TabsList>

          <TabsContent value="parameters" className="space-y-6">
            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">{editParam.id ? "Редактировать параметр" : "Добавить новый параметр"}</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="param-key">Ключ</Label>
                  <Input
                    id="param-key"
                    placeholder="technical_depth"
                    value={editParam.key}
                    onChange={(e) => setEditParam({ ...editParam, key: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="param-title">Название</Label>
                  <Input
                    id="param-title"
                    placeholder="Техническая глубина"
                    value={editParam.title}
                    onChange={(e) => setEditParam({ ...editParam, title: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="param-weight">Вес</Label>
                  <Input
                    id="param-weight"
                    type="number"
                    step="0.1"
                    min="0"
                    max="10"
                    value={editParam.weight}
                    onChange={(e) => setEditParam({ ...editParam, weight: Number(e.target.value) })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="param-rule">Правило влияния</Label>
                  <Input
                    id="param-rule"
                    placeholder="nlp_score * weight"
                    value={editParam.influence_rule}
                    onChange={(e) => setEditParam({ ...editParam, influence_rule: e.target.value })}
                  />
                </div>
                <div className="md:col-span-2 space-y-2">
                  <Label htmlFor="param-description">Описание</Label>
                  <Textarea
                    id="param-description"
                    rows={3}
                    placeholder="Описание параметра и его влияния на оценку..."
                    value={editParam.description}
                    onChange={(e) => setEditParam({ ...editParam, description: e.target.value })}
                  />
                </div>
                <div className="flex items-center gap-2">
                  <Switch
                    id="param-active"
                    checked={Boolean(editParam.is_active)}
                    onCheckedChange={(checked) => setEditParam({ ...editParam, is_active: checked })}
                  />
                  <Label htmlFor="param-active" className="cursor-pointer">
                    Активен
                  </Label>
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <Button onClick={handleSaveParameter} disabled={!editParam.key || !editParam.title}>
                  <Save className="w-4 h-4 mr-2" />
                  Сохранить параметр
                </Button>
                <Button variant="outline" onClick={loadData}>
                  <RefreshCw className="w-4 h-4 mr-2" />
                  Перезагрузить
                </Button>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent border-b border-border">
                    <TableHead className="w-16">ID</TableHead>
                    <TableHead className="w-24">Статус</TableHead>
                    <TableHead className="w-48">Ключ</TableHead>
                    <TableHead>Название</TableHead>
                    <TableHead className="w-24">Вес</TableHead>
                    <TableHead>Правило</TableHead>
                    <TableHead className="w-32">Действия</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {paramsLoading ? (
                    <TableRow>
                      <TableCell colSpan={8} className="h-24 text-center text-muted-foreground">
                        Загрузка...
                      </TableCell>
                    </TableRow>
                  ) : !parameters.length ? (
                    <TableRow>
                      <TableCell colSpan={8} className="h-24 text-center text-muted-foreground">
                        Параметры оценки не найдены.
                      </TableCell>
                    </TableRow>
                  ) : (
                    parameters.map((param) => (
                      <TableRow key={param.id} className="hover:bg-muted/50">
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          #{param.id}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <Switch
                              checked={param.is_active}
                              onCheckedChange={() => handleToggleParameter(param)}
                            />
                            <Badge
                              variant="outline"
                              className={
                                param.is_active
                                  ? "bg-green-500/20 text-green-300 border-green-500/30"
                                  : "bg-gray-500/20 text-gray-400 border-gray-500/30"
                              }
                            >
                              {param.is_active ? "Активен" : "Выключен"}
                            </Badge>
                          </div>
                        </TableCell>
                        <TableCell className="font-mono text-sm">{param.key}</TableCell>
                        <TableCell>
                          <div>
                            <div className="font-medium">{param.title}</div>
                            <div className="text-sm text-muted-foreground line-clamp-1">
                              {param.description}
                            </div>
                          </div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="secondary" className="font-mono">
                            {Number(param.weight).toFixed(1)}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">{param.influence_rule}</TableCell>
                        <TableCell>
                          <div className="flex items-center gap-2">
                            <Button variant="ghost" size="sm" onClick={() => setEditParam(param)}>
                              Изменить
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive hover:text-destructive"
                              onClick={() => handleDeleteParameter(param.id)}
                            >
                              <Trash2 className="w-4 h-4" />
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>

            <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
              <div className="bg-card border border-border rounded-lg p-6">
                <div className="text-sm text-muted-foreground mb-1">Всего параметров</div>
                <div className="text-3xl font-semibold">{parameters.length}</div>
              </div>
              <div className="bg-card border border-border rounded-lg p-6">
                <div className="text-sm text-muted-foreground mb-1">Активных</div>
                <div className="text-3xl font-semibold text-green-400">
                  {parameters.filter((p) => p.is_active).length}
                </div>
              </div>
              <div className="bg-card border border-border rounded-lg p-6">
                <div className="text-sm text-muted-foreground mb-1">Суммарный вес</div>
                <div className="text-3xl font-semibold">
                  {parameters
                    .filter((p) => p.is_active)
                    .reduce((sum, p) => sum + Number(p.weight), 0)
                    .toFixed(1)}
                </div>
              </div>
            </div>
          </TabsContent>

          <TabsContent value="runtime" className="space-y-6">
            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Добавить runtime настройку</h3>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div className="space-y-2">
                  <Label htmlFor="runtime-key">Ключ</Label>
                  <Input
                    id="runtime-key"
                    placeholder="min_score_threshold"
                    value={newSetting.key}
                    onChange={(e) => setNewSetting({ ...newSetting, key: e.target.value })}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="runtime-scope">Область действия</Label>
                  <Select value={newSetting.scope} onValueChange={(value) => setNewSetting({ ...newSetting, scope: value as "global" | "topic" })}>
                    <SelectTrigger id="runtime-scope">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="global">Global</SelectItem>
                      <SelectItem value="topic">Topic</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                {newSetting.scope === "topic" ? (
                  <div className="space-y-2">
                    <Label htmlFor="runtime-topic">Topic Key</Label>
                    <Input
                      id="runtime-topic"
                      placeholder="ai"
                      value={newSetting.topic_key}
                      onChange={(e) => setNewSetting({ ...newSetting, topic_key: e.target.value })}
                    />
                  </div>
                ) : null}
                <div className="space-y-2">
                  <Label htmlFor="runtime-value">Значение</Label>
                  <Input
                    id="runtime-value"
                    placeholder="3.0"
                    value={newSetting.value}
                    onChange={(e) => setNewSetting({ ...newSetting, value: e.target.value })}
                  />
                </div>
              </div>
              <div className="flex gap-2 mt-4">
                <Button onClick={handleSaveRuntime} disabled={!newSetting.key.trim()}>
                  <Plus className="w-4 h-4 mr-2" />
                  Добавить настройку
                </Button>
                <Button variant="outline" onClick={loadData}>
                  <RefreshCw className="w-4 h-4 mr-2" />
                  Перезагрузить
                </Button>
              </div>
            </div>

            <div className="bg-card border border-border rounded-lg overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow className="hover:bg-transparent border-b border-border">
                    <TableHead className="w-16">ID</TableHead>
                    <TableHead>Ключ</TableHead>
                    <TableHead className="w-32">Область</TableHead>
                    <TableHead className="w-32">Topic</TableHead>
                    <TableHead>Значение</TableHead>
                    <TableHead className="w-24">Действия</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runtimeLoading ? (
                    <TableRow>
                      <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
                        Загрузка...
                      </TableCell>
                    </TableRow>
                  ) : !runtimeSettings.length ? (
                    <TableRow>
                      <TableCell colSpan={6} className="h-24 text-center text-muted-foreground">
                        Runtime-настройки не найдены.
                      </TableCell>
                    </TableRow>
                  ) : (
                    runtimeSettings.map((setting) => (
                      <TableRow key={setting.id} className="hover:bg-muted/50">
                        <TableCell className="font-mono text-xs text-muted-foreground">
                          #{setting.id}
                        </TableCell>
                        <TableCell className="font-mono text-sm">{setting.key}</TableCell>
                        <TableCell>
                          <Badge variant="outline">
                            {setting.scope === "global" ? "Глобальная" : "Тематическая"}
                          </Badge>
                        </TableCell>
                        <TableCell className="font-mono text-sm">{setting.topic_key || "—"}</TableCell>
                        <TableCell className="font-mono font-medium">{setting.value}</TableCell>
                        <TableCell>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive hover:text-destructive"
                            onClick={() => handleDeleteRuntime(setting.id)}
                          >
                            <Trash2 className="w-4 h-4" />
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
          </TabsContent>
        </Tabs>
      </div>
    </div>
  );
}
