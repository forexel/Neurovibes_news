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

function logEntry(message: string, type: LogEntry["type"] = "info"): LogEntry {
  return {
    type,
    message,
    timestamp: new Date().toLocaleTimeString("ru-RU", {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }),
  };
}

export default function SetupWizard() {
  const navigate = useNavigate();
  const [currentStep, setCurrentStep] = useState<Step>(1);
  const [loading, setLoading] = useState(false);
  const [initialLoading, setInitialLoading] = useState(true);
  const [error, setError] = useState("");

  const [channelName, setChannelName] = useState("");
  const [channelTheme, setChannelTheme] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [apiKeySaved, setApiKeySaved] = useState(false);
  const [showApiKey, setShowApiKey] = useState(false);

  const [audienceDescription, setAudienceDescription] = useState("");
  const [scoringAnalyzed, setScoringAnalyzed] = useState(false);
  const [scoringResult, setScoringResult] = useState("");

  const [botToken, setBotToken] = useState("");
  const [showBotToken, setShowBotToken] = useState(false);
  const [reviewChatId, setReviewChatId] = useState("");
  const [channelId, setChannelId] = useState("");
  const [signature, setSignature] = useState("");
  const [timezone, setTimezone] = useState("Europe/Moscow");

  const [bootstrapLogs, setBootstrapLogs] = useState<LogEntry[]>([]);
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
        setReviewChatId(state.telegram_review_chat_id || "");
        setChannelId(state.telegram_channel_id || "");
        setSignature(state.telegram_signature || "");
        setTimezone(state.timezone_name || "Europe/Moscow");
        setCurrentStep(Math.min(4, Math.max(1, state.onboarding_step || 1)) as Step);
        if (state.onboarding_completed) {
          setScoringAnalyzed(true);
          setBootstrapComplete(true);
        }
      })
      .catch((err) => {
        if (cancelled) return;
        if (err instanceof ApiError && err.status === 401) {
          navigate("/login", { replace: true });
          return;
        }
        setError(err instanceof Error ? err.message : "Не удалось загрузить setup.");
      })
      .finally(() => {
        if (!cancelled) setInitialLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  const progress = (currentStep / steps.length) * 100;

  async function handleSaveApiKey() {
    setLoading(true);
    setError("");
    try {
      await api.saveSetupStep1({
        channel_name: channelName.trim(),
        channel_theme: channelTheme.trim(),
        openrouter_api_key: apiKey.trim() || undefined,
      });
      setApiKeySaved(true);
      setApiKey("");
      setShowApiKey(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось сохранить шаг 1.");
    } finally {
      setLoading(false);
    }
  }

  async function handleAnalyzeScoring() {
    setLoading(true);
    setError("");
    try {
      const result = await api.analyzeSetupStep2({
        audience_description: audienceDescription.trim(),
      });
      setScoringAnalyzed(true);
      if (result.params.length > 0) {
        setScoringResult(
          `Профиль аудитории успешно проанализирован. Создано ${result.params.length} параметров оценки с учетом вашей тематики.`,
        );
      } else {
        setScoringResult("Профиль аудитории успешно проанализирован.");
      }
      await api.saveSetupStep2({
        audience_description: audienceDescription.trim(),
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось настроить скоринг.");
    } finally {
      setLoading(false);
    }
  }

  async function saveStep3() {
    await api.saveTelegramSettings({
      telegram_bot_token: botToken.trim() || undefined,
      telegram_review_chat_id: reviewChatId.trim(),
      telegram_channel_id: channelId.trim(),
      telegram_signature: signature.trim(),
      timezone_name: timezone.trim() || "Europe/Moscow",
    });
    setBotToken("");
  }

  async function handleBootstrap() {
    setLoading(true);
    setError("");
    setBootstrapLogs([logEntry("Запуск начального импорта...")]);
    try {
      const result = await api.completeSetup();
      setBootstrapLogs((prev) => [
        ...prev,
        ...Object.entries(result).map(([key, value]) =>
          logEntry(`${key}: ${typeof value === "object" ? JSON.stringify(value) : String(value)}`, "success"),
        ),
      ]);
      setBootstrapComplete(true);
    } catch (err) {
      const detail = err instanceof Error ? err.message : "Начальный импорт завершился ошибкой.";
      setBootstrapLogs((prev) => [...prev, logEntry(detail, "error")]);
      setError(detail);
    } finally {
      setLoading(false);
    }
  }

  async function handleNext() {
    setError("");
    if (currentStep === 1) {
      setCurrentStep(2);
      return;
    }
    if (currentStep === 2) {
      setCurrentStep(3);
      return;
    }
    if (currentStep === 3) {
      setLoading(true);
      try {
        await saveStep3();
        setCurrentStep(4);
      } catch (err) {
        setError(err instanceof Error ? err.message : "Не удалось сохранить Telegram-настройки.");
      } finally {
        setLoading(false);
      }
      return;
    }
    navigate("/");
  }

  function handleBack() {
    if (currentStep > 1) {
      setCurrentStep((currentStep - 1) as Step);
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
              <p className="text-sm text-muted-foreground">
                Шаг {currentStep} из {steps.length}
              </p>
            </div>
          </div>
          <Progress value={progress} className="h-1" />
        </div>
      </div>

      <div className="max-w-4xl mx-auto px-6 py-8">
        <div className="flex gap-8 mb-8">
          {steps.map((step) => (
            <div
              key={step.number}
              className={`flex items-center gap-3 ${
                step.number === currentStep
                  ? "opacity-100"
                  : step.number < currentStep
                    ? "opacity-60"
                    : "opacity-40"
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

        <div className="bg-card border border-border rounded-lg p-8">
          {currentStep === 1 && (
            <div className="space-y-6">
              <div>
                <h2 className="text-xl font-semibold mb-2">Настройка канала</h2>
                <p className="text-sm text-muted-foreground">
                  Основная информация о вашем новостном канале
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="channelName">Название канала</Label>
                  <Input
                    id="channelName"
                    placeholder="AI News Daily"
                    value={channelName}
                    onChange={(e) => setChannelName(e.target.value)}
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="channelTheme">Тематика канала</Label>
                  <Input
                    id="channelTheme"
                    placeholder="Новости искусственного интеллекта"
                    value={channelTheme}
                    onChange={(e) => setChannelTheme(e.target.value)}
                  />
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
                        disabled={loading || apiKeySaved}
                        className={apiKeySaved ? "bg-green-500/10" : ""}
                      />
                      {!apiKeySaved ? (
                        <button
                          type="button"
                          onClick={() => setShowApiKey(!showApiKey)}
                          className="absolute right-3 top-1/2 -translate-y-1/2"
                        >
                          {showApiKey ? (
                            <EyeOff className="w-4 h-4 text-muted-foreground" />
                          ) : (
                            <Eye className="w-4 h-4 text-muted-foreground" />
                          )}
                        </button>
                      ) : null}
                    </div>
                    {!apiKeySaved ? (
                      <Button
                        onClick={handleSaveApiKey}
                        disabled={!channelName.trim() || !channelTheme.trim() || !apiKey.trim() || loading}
                      >
                        {loading ? <Loader2 className="w-4 h-4 animate-spin" /> : "Сохранить"}
                      </Button>
                    ) : (
                      <Button variant="outline" disabled className="gap-2">
                        <CheckCircle2 className="w-4 h-4 text-green-400" />
                        Сохранено
                      </Button>
                    )}
                  </div>
                  {apiKeySaved && (
                    <p className="text-xs text-green-400">API ключ безопасно сохранен</p>
                  )}
                </div>
              </div>
            </div>
          )}

          {currentStep === 2 && (
            <div className="space-y-6">
              <div>
                <h2 className="text-xl font-semibold mb-2">Аудитория и скоринг</h2>
                <p className="text-sm text-muted-foreground">
                  Опишите вашу аудиторию для настройки алгоритмов оценки
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="audience">Описание целевой аудитории</Label>
                  <Textarea
                    id="audience"
                    placeholder="Профессионалы в области ML/AI: исследователи, инженеры, технические руководители. Интересуются новыми моделями, исследованиями, инструментами и практическими применениями ИИ. Ценят технические детали и глубокий анализ."
                    value={audienceDescription}
                    onChange={(e) => setAudienceDescription(e.target.value)}
                    rows={6}
                    disabled={scoringAnalyzed}
                  />
                </div>

                {!scoringAnalyzed ? (
                  <Button
                    onClick={handleAnalyzeScoring}
                    disabled={!audienceDescription.trim() || loading}
                    className="w-full"
                  >
                    {loading ? (
                      <>
                        <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                        Анализ профиля...
                      </>
                    ) : (
                      "Проанализировать и настроить скоринг"
                    )}
                  </Button>
                ) : (
                  <div className="border border-green-500/30 bg-green-500/10 rounded-lg p-4">
                    <div className="flex items-start gap-3">
                      <CheckCircle2 className="w-5 h-5 text-green-400 flex-shrink-0 mt-0.5" />
                      <div>
                        <h4 className="font-medium text-green-300 mb-1">Скоринг настроен</h4>
                        <p className="text-sm text-green-200/80">{scoringResult}</p>
                      </div>
                    </div>
                  </div>
                )}
              </div>
            </div>
          )}

          {currentStep === 3 && (
            <div className="space-y-6">
              <div>
                <h2 className="text-xl font-semibold mb-2">Интеграция Telegram</h2>
                <p className="text-sm text-muted-foreground">
                  Подключите бота для публикации в канал
                </p>
              </div>

              <div className="space-y-4">
                <div className="space-y-2">
                  <Label htmlFor="botToken">Bot Token</Label>
                  <div className="relative">
                    <Input
                      id="botToken"
                      type={showBotToken ? "text" : "password"}
                      placeholder="1234567890:ABCdefGHIjklMNOpqrsTUVwxyz"
                      value={botToken}
                      onChange={(e) => setBotToken(e.target.value)}
                      disabled={loading}
                    />
                    <button
                      type="button"
                      onClick={() => setShowBotToken(!showBotToken)}
                      className="absolute right-3 top-1/2 -translate-y-1/2"
                    >
                      {showBotToken ? (
                        <EyeOff className="w-4 h-4 text-muted-foreground" />
                      ) : (
                        <Eye className="w-4 h-4 text-muted-foreground" />
                      )}
                    </button>
                  </div>
                  <p className="text-xs text-muted-foreground">Получить у @BotFather</p>
                </div>

                <div className="grid grid-cols-2 gap-4">
                  <div className="space-y-2">
                    <Label htmlFor="reviewChatId">Review Chat ID</Label>
                    <Input
                      id="reviewChatId"
                      placeholder="-1001234567890"
                      value={reviewChatId}
                      onChange={(e) => setReviewChatId(e.target.value)}
                      disabled={loading}
                    />
                  </div>

                  <div className="space-y-2">
                    <Label htmlFor="channelId">Channel ID</Label>
                    <Input
                      id="channelId"
                      placeholder="@ai_news_channel"
                      value={channelId}
                      onChange={(e) => setChannelId(e.target.value)}
                      disabled={loading}
                    />
                  </div>
                </div>

                <div className="space-y-2">
                  <Label htmlFor="signature">Подпись публикаций</Label>
                  <Input
                    id="signature"
                    placeholder="AI News Daily"
                    value={signature}
                    onChange={(e) => setSignature(e.target.value)}
                    disabled={loading}
                  />
                </div>

                <div className="space-y-2">
                  <Label htmlFor="timezone">Часовой пояс</Label>
                  <Input
                    id="timezone"
                    placeholder="Europe/Moscow"
                    value={timezone}
                    onChange={(e) => setTimezone(e.target.value)}
                    disabled={loading}
                  />
                </div>
              </div>
            </div>
          )}

          {currentStep === 4 && (
            <div className="space-y-6">
              <div>
                <h2 className="text-xl font-semibold mb-2">Начальный импорт</h2>
                <p className="text-sm text-muted-foreground">
                  Загрузите первую партию статей из настроенных источников
                </p>
              </div>

              {!bootstrapComplete ? (
                <Button
                  onClick={handleBootstrap}
                  disabled={loading}
                  className="w-full"
                  size="lg"
                >
                  {loading ? (
                    <>
                      <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                      Импорт данных...
                    </>
                  ) : (
                    "Запустить начальный импорт"
                  )}
                </Button>
              ) : null}

              {bootstrapLogs.length > 0 ? (
                <LogPanel logs={bootstrapLogs} title="Прогресс импорта" />
              ) : null}

              {bootstrapComplete ? (
                <div className="border border-green-500/30 bg-green-500/10 rounded-lg p-6 text-center">
                  <CheckCircle2 className="w-12 h-12 text-green-400 mx-auto mb-3" />
                  <h3 className="font-semibold text-lg mb-2">Настройка завершена!</h3>
                  <p className="text-sm text-muted-foreground mb-4">
                    Система готова к работе.
                  </p>
                </div>
              ) : null}
            </div>
          )}
        </div>

        {error ? (
          <div className="mt-4 text-sm text-destructive bg-destructive/10 border border-destructive/20 rounded-lg px-3 py-2">
            {error}
          </div>
        ) : null}

        <div className="flex justify-between mt-6">
          <Button
            variant="outline"
            onClick={handleBack}
            disabled={currentStep === 1 || loading}
          >
            Назад
          </Button>
          <Button
            onClick={handleNext}
            disabled={
              loading ||
              (currentStep === 1 && !apiKeySaved) ||
              (currentStep === 2 && !scoringAnalyzed) ||
              (currentStep === 3 && (!reviewChatId.trim() || !channelId.trim())) ||
              (currentStep === 4 && !bootstrapComplete)
            }
          >
            {loading && currentStep === 3 ? (
              <>
                <Loader2 className="w-4 h-4 mr-2 animate-spin" />
                Сохранение...
              </>
            ) : currentStep === 4 ? (
              "Перейти к панели"
            ) : (
              "Далее"
            )}
          </Button>
        </div>
      </div>
    </div>
  );
}
