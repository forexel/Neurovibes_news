import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Badge } from "../components/ui/badge";
import { LogPanel } from "../components/LogPanel";
import { Activity, AlertCircle, CheckCircle2, Clock, Loader2, Send } from "lucide-react";
import { api, ApiError, formatDateTime, SetupState, WorkerStatus } from "../lib/api";

type LogEntry = { type: "info" | "success" | "error"; message: string; timestamp?: string };

function stamp() {
  return new Date().toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export default function BotControlPage() {
  const navigate = useNavigate();
  const [setupState, setSetupState] = useState<SetupState | null>(null);
  const [worker, setWorker] = useState<WorkerStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<string | null>(null);
  const [logs, setLogs] = useState<LogEntry[]>([]);
  const [error, setError] = useState("");

  function addLog(type: LogEntry["type"], message: string) {
    setLogs((prev) => [...prev, { type, message, timestamp: stamp() }]);
  }

  async function loadState() {
    setLoading(true);
    setError("");
    try {
      const [setup, workerData] = await Promise.all([api.getSetupState(), api.getWorkerStatus()]);
      setSetupState(setup);
      setWorker(workerData);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить статус Telegram.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadState();
  }, [navigate]);

  async function runAction(label: string, action: () => Promise<Record<string, unknown>>) {
    setActionLoading(label);
    addLog("info", `${label}...`);
    try {
      const out = await action();
      addLog("success", `${label}: ${JSON.stringify(out)}`);
      await loadState();
    } catch (err) {
      addLog("error", err instanceof Error ? err.message : `${label}: failed`);
    } finally {
      setActionLoading(null);
    }
  }

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6">
        <div className="mb-6">
          <h1 className="text-2xl font-semibold mb-1">Управление Telegram ботом</h1>
          <p className="text-sm text-muted-foreground">Статус подключения и контроль review/publish pipeline.</p>
        </div>

        {error ? <div className="mb-4 text-sm text-destructive">{error}</div> : null}

        <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
          <div className="lg:col-span-3 grid grid-cols-1 md:grid-cols-4 gap-4">
            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center gap-3 mb-2">
                {setupState?.telegram_bot_token_set ? (
                  <CheckCircle2 className="w-5 h-5 text-green-400" />
                ) : (
                  <AlertCircle className="w-5 h-5 text-red-400" />
                )}
                <div className="text-sm text-muted-foreground">Статус бота</div>
              </div>
              <Badge
                variant="outline"
                className={
                  setupState?.telegram_bot_token_set
                    ? "bg-green-500/20 text-green-300 border-green-500/30"
                    : "bg-red-500/20 text-red-300 border-red-500/30"
                }
              >
                {setupState?.telegram_bot_token_set ? "Подключен" : "Не настроен"}
              </Badge>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center gap-3 mb-2">
                <Activity className="w-5 h-5 text-blue-400" />
                <div className="text-sm text-muted-foreground">Worker</div>
              </div>
              <Badge variant="outline" className="bg-blue-500/20 text-blue-300 border-blue-500/30">
                {worker?.worker_cycle_state || "unknown"}
              </Badge>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center gap-3 mb-2">
                <Clock className="w-5 h-5 text-purple-400" />
                <div className="text-sm text-muted-foreground">Последний цикл</div>
              </div>
              <div className="text-sm">{formatDateTime(worker?.worker_last_cycle_finish_utc)}</div>
            </div>

            <div className="bg-card border border-border rounded-lg p-6">
              <div className="flex items-center gap-3 mb-2">
                <Clock className="w-5 h-5 text-cyan-400" />
                <div className="text-sm text-muted-foreground">Следующий цикл</div>
              </div>
              <div className="text-sm">{formatDateTime(worker?.worker_next_cycle_utc)}</div>
            </div>
          </div>

          <div className="lg:col-span-2 space-y-6">
            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Конфигурация</h3>
              {loading ? (
                <div className="text-muted-foreground">Загрузка...</div>
              ) : (
                <div className="grid grid-cols-1 md:grid-cols-2 gap-4 text-sm">
                  <div>
                    <div className="text-muted-foreground mb-1">Review chat</div>
                    <div>{setupState?.telegram_review_chat_id || "—"}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground mb-1">Channel id</div>
                    <div>{setupState?.telegram_channel_id || "—"}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground mb-1">Signature</div>
                    <div>{setupState?.telegram_signature || "—"}</div>
                  </div>
                  <div>
                    <div className="text-muted-foreground mb-1">Timezone</div>
                    <div>{setupState?.timezone_name || "Europe/Moscow"}</div>
                  </div>
                </div>
              )}
              <div className="mt-4">
                <Button variant="outline" onClick={() => navigate("/setup")}>
                  Перейти в Setup
                </Button>
              </div>
            </div>

            {worker?.worker_last_cycle_error ? (
              <div className="bg-red-500/10 border border-red-500/30 rounded-lg p-4">
                <div className="flex items-start gap-3">
                  <AlertCircle className="w-5 h-5 text-red-400 flex-shrink-0 mt-0.5" />
                  <div>
                    <h4 className="font-medium text-red-300 mb-1">Последняя ошибка</h4>
                    <p className="text-sm text-red-200/80">{worker.worker_last_cycle_error}</p>
                  </div>
                </div>
              </div>
            ) : null}

            <div className="bg-card border border-border rounded-lg p-6">
              <h3 className="font-semibold mb-4">Операции</h3>
              <div className="flex flex-wrap gap-3">
                <Button onClick={() => runAction("Telegram Test", () => api.telegramTest())} disabled={actionLoading === "Telegram Test"}>
                  {actionLoading === "Telegram Test" ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : <Send className="w-4 h-4 mr-2" />}
                  Telegram Test
                </Button>
                <Button variant="outline" onClick={() => runAction("Poll TG Now", () => api.telegramPoll())} disabled={actionLoading === "Poll TG Now"}>
                  {actionLoading === "Poll TG Now" ? <Loader2 className="w-4 h-4 mr-2 animate-spin" /> : null}
                  Poll TG Now
                </Button>
                <Button
                  variant="outline"
                  onClick={() => runAction("24h Backfill", () => api.telegramHourlyBackfill(24, 24, false))}
                  disabled={actionLoading === "24h Backfill"}
                >
                  Send 24h Backfill
                </Button>
                <Button
                  variant="outline"
                  onClick={() => {
                    const hours = window.prompt("Сколько часов backfill отправить?", "24");
                    const normalized = Math.max(1, Math.min(168, Number(hours) || 24));
                    runAction(`Backfill ${normalized}h`, () => api.telegramHourlyBackfill(normalized, normalized, false));
                  }}
                >
                  Send Backfill (custom)
                </Button>
              </div>
            </div>
          </div>

          <div className="lg:col-span-1">
            <LogPanel logs={logs} title="Bot log" />
          </div>
        </div>
      </div>
    </div>
  );
}
