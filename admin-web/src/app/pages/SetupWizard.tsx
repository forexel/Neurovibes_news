import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Textarea } from "../components/ui/textarea";
import { Progress } from "../components/ui/progress";
import { CheckCircle2, Eye, EyeOff, FileText, Loader2 } from "lucide-react";
import { LogPanel } from "../components/LogPanel";
import { api, ApiError, SetupState } from "../lib/api";

type Step = 1 | 2 | 3 | 4;
type LogEntry = { type: "info" | "success" | "error"; message: string; timestamp?: string };

const steps = [
  { number: 1, title: "Канал", description: "Основные настройки" },
  { number: 2, title: "Аудитория", description: "Профиль и скоринг" },
  { number: 3, title: "Telegram", description: "Интеграция с ботом" },
  { number: 4, title: "Запуск", description: "Начальный импорт" },
] as const;

function toLog(message: string, type: LogEntry["type"] = "info"): LogEntry {
  return {
    type,
    message,
    timestamp: new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" }),
  };
}

export default function SetupWizard() {
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState<Step>(1);
  const [initialLoading, setInitialLoading] = useState(true);
  const [loading, setLoading] = useState<string | null>(null);
  const [error, setError] = useState("");

  const [channelName, setChannelName] = useState("");
  const [channelTheme, setChannelTheme] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);

  const [audienceDescription, setAudienceDescription] = useState("");
  const [scoringResult, setScoringResult] = useState("");

  const [botToken, setBotToken] = useState("");
  const [botTokenSaved, setBotTokenSaved] = useState(false);
  const [showBotToken, setShowBotToken] = useState(false);
  const [reviewChatId, setReviewChatId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [signature, setSignature] = useState("");
  const [timezone, setTimezone] = useState("Europe/Moscow");

  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [bootstrapComplete, setBootstrapComplete] = useState(false);

  useEffect(() => {
    let cancelled = false;

    api
      .getSetupState()
      .then((state: SetupState) => {
        if (cancelled) return;
        setChannelName(state.channel_name || "");
        setChannelTheme(state.channel_theme || "");
        setApiKeySaved(state.openrouter_api_key_set);
        setAudienceDescription(state.audience_description || "");
        setBotTokenSaved(state.telegram_bot_token_set);
        setReviewChatId(state.telegram_review_chat_id || "");
        setChannelId(state.telegram_channel_id || "");
        setSignature(state.telegram_signature || "");
        setTimezone(state.timezone_name || "Europe/Moscow");
        setCurrentStep(Math.min(4, Math.max(1, state.onboarding_step || 1)) as Step);
        if (state.onboarding_completed) {
          setBootstrapComplete(true);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          navigate("/login", { replace: true });
          return;
        }
        setError(err instanceof Error ? err.message : "Не удалось загрузить состояние setup.");
      })
      .finally(() => {
        if (!cancelled) setInitialLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [navigate]);

  const progress = (currentStep / steps.length) * 100;

  async function saveStep1() {
    setLoading("step1");
    setError("");
    try {
      await api.saveSetupStep1({
        channel_name: channelName.trim(),
        channel_theme: channelTheme.trim(),
        openrouter_api_key: apiKey.trim() || undefined,
      });
      setApiKey("");
      if (apiKey.trim()) setApiKeySaved(true);
      setCurrentStep(2);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить шаг 1.");
    } finally {
      setLoading(null);
    }
  }

  async function saveStep2(analyze = false) {
    setLoading(analyze ? "analyze" : "step2");
    setError("");
    try {
      if (analyze) {
        const result = await api.analyzeSetupStep2({ audience_description: audienceDescription.trim() });
        setScoringResult(
          result.params
            .map((item) => `${item.title} (${item.key}) · weight ${Number(item.weight).toFixed(2)}`)
            .join("\n"),
        );
      } else {
        await api.saveSetupStep2({ audience_description: audienceDescription.trim() });
      }
      setCurrentStep(3);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить шаг 2.");
    } finally {
      setLoading(null);
    }
  }

  async function saveTelegram() {
    setLoading("telegram");
    setError("");
    try {
      await api.saveTelegramSettings({
        telegram_bot_token: botToken.trim() || undefined,
        telegram_review_chat_id: reviewChatId.trim(),
        telegram_channel_id: channelId.trim(),
        telegram_signature: signature.trim(),
        timezone_name: timezone.trim() || "Europe/Moscow",
      });
      setBotToken("");
      if (botToken.trim()) setBotTokenSaved(true);
      setCurrentStep(4);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить Telegram settings.");
    } finally {
      setLoading(null);
    }
  }

  async function runBootstrap() {
    setLoading("bootstrap");
    setError("");
    setLogs([toLog("Запуск начального импорта...")]);
    try {
      const out = await api.completeSetup();
      const nextLogs = Object.entries(out).map(([key, value]) =>
        toLog(`${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`, "success"),
      );
      setLogs((prev) => [...prev, ...nextLogs]);
      setBootstrapComplete(true);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Начальный импорт завершился ошибкой.";
      setLogs((prev) => [...prev, toLog(detail, "error")]);
      setError(detail);
    } finally {
      setLoading(null);
    }
  }

  if (initialLoading) {
    return (
      <div className="min-h-screen bg-background flex items-center justify-center">
        <Loader2 className="w-6 h-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background">
      <div className="border-b border-border bg-card/50 backdrop-blur-sm">
        <div className="max-w-4xl mx-auto px-6 py-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center">
              <FileText className="w-5 h-5 text-white" />
            </div>
            <div>
              <h1 className="text-xl font-semibold">Настройка рабочего пространства</h1>
              <p className="text-sm text-muted-foreground">Шаг {currentStep} из {steps.length}</p>
            </div>
          </div>
          <Progress value={progress} className="h-1" />
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-6 py-8">
        <div className="flex gap-8 mb-8 flex-wrap">
          {steps.map((step) => (
            <div
              key={step.number}
              className={`flex items-center gap-3 ${
                step.number === currentStep ? "opacity-100" : step.number < currentStep ? "opacity-60" : "opacity-40"
              }`}
            >
              <div
                className={`w-10 h-10 rounded-full flex items-center justify-center border-2 ${
                  step.number < currentStep
                    ? "bg-green-500/20 border-green-500"
                    : step.number === currentStep
                      ? "bg-blue-500/20 border-blue-500"
                      : "border-border"
                }`}
              >
                {step.number < currentStep ? (
                  <CheckCircle2 className="w-5 h-5 text-green-400" />
                ) : (
                  <span className="text-sm font-medium">{step.number}</span>
                )}
              </div>
              <div className="hidden sm:block">
                <div className="font-medium text-sm">{step.title}</div>
                <div className="text-xs text-muted-foreground">{step.description}</div>
              </div>
            </div>
          ))}
        </div>

        <div className="bg-card border border-border rounded-lg p-8 space-y-6">
          {error ? (
            <div className="text-sm text-destructive bg-destructive/10 border border-destructive/20 rounded-lg px-3 py-2">
              {error}
            </div>
          ) : null}

          {currentStep === 1 ? (
            <>
              <div>
                <h2 className="text-xl font-semibold mb-2">Настройка канала</h2>
                <p className="text-sm text-muted-foreground">Название, позиционирование и LLM-ключ для workspace.</p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="channelName">Название канала</Label>
                  <Input id="channelName" value={channelName} onChange={(e) => setChannelName(e.target.value)} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="channelTheme">Тематика канала</Label>
                  <Textarea id="channelTheme" value={channelTheme} onChange={(e) => setChannelTheme(e.target.value)} rows={4} />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="apiKey">OpenRouter API Key</Label>
                  <div className="flex gap-2">
                    <div className="relative flex-1">
                      <Input
                        id="apiKey"
                        type={showApiKey ? "text" : "password"}
                        placeholder={apiKeySaved ? "Ключ уже сохранен" : "sk-or-v1-..."}
                        value={apiKey}
                        onChange={(e) => setApiKey(e.target.value)}
                        className={apiKeySaved ? "bg-green-500/10" : ""}
                      />
                      <button
                        type="button"
                        onClick={() => setShowApiKey((value) => !value)}
                        className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                      >
                        {showApiKey ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                      </button>
                    </div>
                    <Button onClick={saveStep1} disabled={!channelName.trim() || !channelTheme.trim() || loading === "step1"}>
                      {loading === "step1" ? <Loader2 className="w-4 h-4 animate-spin" /> : "Сохранить"}
                    </Button>
                  </div>
                  {apiKeySaved ? <p className="text-xs text-green-400">Ключ уже хранится в базе и скрыт.</p> : null}
                </div>
              </div>
            </>
          ) : null}

          {currentStep === 2 ? (
            <>
              <div>
                <h2 className="text-xl font-semibold mb-2">Аудитория и скоринг</h2>
                <p className="text-sm text-muted-foreground">Опиши аудиторию. На этой базе строятся параметры оценки новостей.</p>
              </div>
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="audience">Описание аудитории</Label>
                  <Textarea
                    id="audience"
                    rows={8}
                    value={audienceDescription}
                    onChange={(e) => setAudienceDescription(e.target.value)}
                  />
                </div>
                <div className="flex flex-wrap gap-3">
                  <Button onClick={() => saveStep2(false)} disabled={!audienceDescription.trim() || loading === "step2"}>
                    {loading === "step2" ? <Loader2 className="w-4 h-4 animate-spin" /> : "Сохранить"}
                  </Button>
                  <Button
                    variant="outline"
                    onClick={() => saveStep2(true)}
                    disabled={!audienceDescription.trim() || loading === "analyze"}
                  >
                    {loading === "analyze" ? <Loader2 className="w-4 h-4 animate-spin" /> : "Анализировать scoring"}
                  </Button>
                </div>
                {scoringResult ? (
                  <div className="rounded-lg border border-border bg-black/20 p-4">
                    <pre className="text-xs text-muted-foreground whitespace-pre-wrap">{scoringResult}</pre>
                  </div>
                ) : null}
              </div>
            </>
          ) : null}

          {currentStep === 3 ? (
            <>
              <div>
                <h2 className="text-xl font-semibold mb-2">Telegram</h2>
                <p className="text-sm text-muted-foreground">Review chat, publish channel и токен бота для workspace.</p>
              </div>
              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="botToken">Bot Token</Label>
                  <div className="relative">
                    <Input
                      id="botToken"
                      type={showBotToken ? "text" : "password"}
                      placeholder={botTokenSaved ? "Токен уже сохранен" : "123456:AA..."}
                      value={botToken}
                      onChange={(e) => setBotToken(e.target.value)}
                      className={botTokenSaved ? "bg-green-500/10" : ""}
                    />
                    <button
                      type="button"
                      onClick={() => setShowBotToken((value) => !value)}
                      className="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground"
                    >
                      {showBotToken ? <EyeOff className="w-4 h-4" /> : <Eye className="w-4 h-4" />}
                    </button>
                  </div>
                </div>
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="space-y-2">
                    <Label htmlFor="reviewChat">Review chat</Label>
                    <Input id="reviewChat" value={reviewChatId} onChange={(e) => setReviewChatId(e.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="channelId">Channel id</Label>
                    <Input id="channelId" value={channelId} onChange={(e) => setChannelId(e.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="signature">Signature</Label>
                    <Input id="signature" value={signature} onChange={(e) => setSignature(e.target.value)} />
                  </div>
                  <div className="space-y-2">
                    <Label htmlFor="timezone">Timezone</Label>
                    <Input id="timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} />
                  </div>
                </div>
                <Button onClick={saveTelegram} disabled={loading === "telegram" || !reviewChatId.trim() || !channelId.trim()}>
                  {loading === "telegram" ? <Loader2 className="w-4 h-4 animate-spin" /> : "Сохранить Telegram"}
                </Button>
              </div>
            </>
          ) : null}

          {currentStep === 4 ? (
            <>
              <div>
                <h2 className="text-xl font-semibold mb-2">Начальный импорт</h2>
                <p className="text-sm text-muted-foreground">Сбор за месяц, дедуп, скоринг и первый top pick.</p>
              </div>
              <div className="flex flex-wrap gap-3">
                <Button onClick={runBootstrap} disabled={loading === "bootstrap" || bootstrapComplete}>
                  {loading === "bootstrap" ? <Loader2 className="w-4 h-4 animate-spin" /> : bootstrapComplete ? "Уже выполнено" : "Запустить"}
                </Button>
                {bootstrapComplete ? (
                  <Button variant="secondary" onClick={() => navigate("/")}>
                    Перейти к центру публикаций
                  </Button>
                ) : null}
              </div>
              <LogPanel logs={logs} title="Bootstrap log" />
            </>
          ) : null}

          <div className="flex justify-between pt-4 border-t border-border">
            <Button variant="ghost" onClick={() => setCurrentStep((value) => Math.max(1, value - 1) as Step)} disabled={currentStep === 1}>
              Назад
            </Button>
            {currentStep < 4 ? (
              <Button variant="outline" onClick={() => setCurrentStep((value) => Math.min(4, value + 1) as Step)}>
                Пропустить вперед
              </Button>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
