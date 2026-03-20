import { useEffect, useMemo, useState } from "react";
import { Link, useNavigate } from "react-router";
import { TopNavigation } from "../components/TopNavigation";
import { Button } from "../components/ui/button";
import { Input } from "../components/ui/input";
import { Label } from "../components/ui/label";
import { Badge } from "../components/ui/badge";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "../components/ui/table";
import { api, ApiError, EvaluationOverview, EvaluationVersions } from "../lib/api";

function fmtPct(v: number | null | undefined): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return `${(v * 100).toFixed(1)}%`;
}

function fmtNum(v: number | null | undefined, digits = 3): string {
  if (typeof v !== "number" || Number.isNaN(v)) return "—";
  return v.toFixed(digits);
}

export default function EvaluationPage() {
  const navigate = useNavigate();
  const [days, setDays] = useState(14);
  const [k, setK] = useState(5);
  const [loading, setLoading] = useState(true);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState("");
  const [overview, setOverview] = useState<EvaluationOverview | null>(null);
  const [versions, setVersions] = useState<EvaluationVersions | null>(null);

  const confusionTotal = useMemo(() => {
    if (!overview) return 0;
    return (
      Number(overview.confusion.tp || 0) +
      Number(overview.confusion.fp || 0) +
      Number(overview.confusion.tn || 0) +
      Number(overview.confusion.fn || 0)
    );
  }, [overview]);

  async function load() {
    setLoading(true);
    setError("");
    try {
      const [ov, vs] = await Promise.all([
        api.getEvaluationOverview(days, k),
        api.getEvaluationVersions(Math.max(days, 30)),
      ]);
      setOverview(ov);
      setVersions(vs);
    } catch (err) {
      if (err instanceof ApiError && err.status === 401) {
        navigate("/login", { replace: true });
        return;
      }
      setError(err instanceof Error ? err.message : "Не удалось загрузить метрики.");
    } finally {
      setLoading(false);
    }
  }

  async function downloadEvalSet() {
    setDownloading(true);
    try {
      const data = await api.getEvaluationEvalSet(days, 1000);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `eval_set_${new Date().toISOString().slice(0, 10)}.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выгрузить eval set.");
    } finally {
      setDownloading(false);
    }
  }

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [navigate]);

  return (
    <div className="min-h-screen bg-background">
      <TopNavigation />

      <div className="max-w-7xl mx-auto px-6 py-6 space-y-6">
        <div className="flex flex-wrap items-end justify-between gap-4">
          <div>
            <h1 className="text-2xl font-semibold mb-1">Evaluation (RAG)</h1>
            <p className="text-sm text-muted-foreground">
              Retrieval quality, groundedness, FP/FN и сравнение версий пайплайна.
            </p>
          </div>
          <div className="flex items-end gap-3">
            <div className="space-y-1">
              <Label htmlFor="eval-days">Окно, дней</Label>
              <Input
                id="eval-days"
                type="number"
                min={1}
                max={180}
                value={days}
                onChange={(e) => setDays(Number(e.target.value) || 14)}
                className="w-28"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="eval-k">K</Label>
              <Input
                id="eval-k"
                type="number"
                min={1}
                max={20}
                value={k}
                onChange={(e) => setK(Number(e.target.value) || 5)}
                className="w-20"
              />
            </div>
            <Button onClick={load} disabled={loading}>Обновить</Button>
            <Button variant="outline" onClick={downloadEvalSet} disabled={downloading}>
              Скачать eval set
            </Button>
          </div>
        </div>

        {error ? <div className="text-sm text-destructive">{error}</div> : null}

        {loading && !overview ? (
          <div className="text-sm text-muted-foreground">Загрузка метрик...</div>
        ) : null}

        {overview ? (
          <>
            <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">hit@{overview.k}</div>
                <div className="text-2xl font-semibold">{fmtPct(overview.retrieval.hit_at_k)}</div>
                <div className="text-xs text-muted-foreground mt-1">eval items: {overview.retrieval.eval_items}</div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">precision@{overview.k}</div>
                <div className="text-2xl font-semibold">{fmtPct(overview.retrieval.precision_at_k)}</div>
                <div className="text-xs text-muted-foreground mt-1">MRR: {fmtNum(overview.retrieval.mrr, 3)}</div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">NDCG@{overview.k}</div>
                <div className="text-2xl font-semibold">{fmtNum(overview.retrieval.ndcg_at_k, 3)}</div>
                <div className="text-xs text-muted-foreground mt-1">
                  answer relevance: {fmtNum(overview.answer_relevance, 3)}
                </div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">source faithfulness</div>
                <div className="text-2xl font-semibold">{fmtNum(overview.source_faithfulness, 3)}</div>
                <div className="text-xs text-muted-foreground mt-1">
                  latency p95: {fmtNum(overview.latency.p95_ms, 1)} ms
                </div>
              </div>
            </section>

            <section className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">latency avg</div>
                <div className="text-xl font-semibold">{fmtNum(overview.latency.avg_ms, 1)} ms</div>
                <div className="text-xs text-muted-foreground mt-1">samples: {overview.latency.sample_size}</div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">total LLM cost</div>
                <div className="text-xl font-semibold">${fmtNum(overview.cost.total_usd, 4)}</div>
                <div className="text-xs text-muted-foreground mt-1">calls: {overview.cost.calls}</div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">cost per request</div>
                <div className="text-xl font-semibold">${fmtNum(overview.cost.cost_per_request_usd, 5)}</div>
                <div className="text-xs text-muted-foreground mt-1">за окно {overview.days} дней</div>
              </div>
              <div className="rounded-lg border border-border bg-card p-4">
                <div className="text-sm text-muted-foreground">ML threshold</div>
                <div className="text-xl font-semibold">{fmtNum(overview.confusion.publish_threshold, 2)}</div>
                <div className="text-xs text-muted-foreground mt-1">confusion samples: {confusionTotal}</div>
              </div>
            </section>

            <section className="rounded-lg border border-border bg-card p-4 space-y-3">
              <div className="flex items-center justify-between">
                <h2 className="text-lg font-semibold">False Positives / False Negatives</h2>
                <div className="flex items-center gap-2 text-xs text-muted-foreground">
                  <Badge variant="outline">TP {overview.confusion.tp}</Badge>
                  <Badge variant="outline">FP {overview.confusion.fp}</Badge>
                  <Badge variant="outline">TN {overview.confusion.tn}</Badge>
                  <Badge variant="outline">FN {overview.confusion.fn}</Badge>
                </div>
              </div>

              <div className="grid gap-4 lg:grid-cols-2">
                <div>
                  <div className="text-sm font-medium mb-2">FP (модель за publish, редактор против)</div>
                  <div className="rounded-md border border-border overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>ID</TableHead>
                          <TableHead>ML</TableHead>
                          <TableHead>Decision</TableHead>
                          <TableHead>Статья</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {overview.confusion.fp_examples.length === 0 ? (
                          <TableRow><TableCell colSpan={4} className="text-muted-foreground">Нет записей</TableCell></TableRow>
                        ) : overview.confusion.fp_examples.map((row) => (
                          <TableRow key={row.event_id}>
                            <TableCell>#{row.article_id}</TableCell>
                            <TableCell>{fmtNum(row.ml_score, 3)}</TableCell>
                            <TableCell>{row.decision}</TableCell>
                            <TableCell>
                              <Link to={`/article/${row.article_id}`} className="text-primary hover:underline">{row.title || "Открыть"}</Link>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>

                <div>
                  <div className="text-sm font-medium mb-2">FN (модель против publish, редактор за)</div>
                  <div className="rounded-md border border-border overflow-hidden">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>ID</TableHead>
                          <TableHead>ML</TableHead>
                          <TableHead>Decision</TableHead>
                          <TableHead>Статья</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {overview.confusion.fn_examples.length === 0 ? (
                          <TableRow><TableCell colSpan={4} className="text-muted-foreground">Нет записей</TableCell></TableRow>
                        ) : overview.confusion.fn_examples.map((row) => (
                          <TableRow key={row.event_id}>
                            <TableCell>#{row.article_id}</TableCell>
                            <TableCell>{fmtNum(row.ml_score, 3)}</TableCell>
                            <TableCell>{row.decision}</TableCell>
                            <TableCell>
                              <Link to={`/article/${row.article_id}`} className="text-primary hover:underline">{row.title || "Открыть"}</Link>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </div>
                </div>
              </div>
            </section>
          </>
        ) : null}

        {versions ? (
          <section className="rounded-lg border border-border bg-card p-4 space-y-3">
            <h2 className="text-lg font-semibold">Сравнение версий модели/пайплайна</h2>
            <div className="rounded-md border border-border overflow-hidden">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Версия</TableHead>
                    <TableHead>Events</TableHead>
                    <TableHead>Positive rate</TableHead>
                    <TableHead>Precision</TableHead>
                    <TableHead>Recall</TableHead>
                    <TableHead>Avg ML score</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {versions.versions.length === 0 ? (
                    <TableRow><TableCell colSpan={6} className="text-muted-foreground">Пока нет данных</TableCell></TableRow>
                  ) : versions.versions.map((row) => (
                    <TableRow key={row.model_version}>
                      <TableCell className="font-medium">{row.model_version}</TableCell>
                      <TableCell>{row.events}</TableCell>
                      <TableCell>{fmtPct(row.positive_rate)}</TableCell>
                      <TableCell>{fmtPct(row.precision)}</TableCell>
                      <TableCell>{fmtPct(row.recall)}</TableCell>
                      <TableCell>{fmtNum(row.avg_ml_score, 3)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          </section>
        ) : null}
      </div>
    </div>
  );
}
