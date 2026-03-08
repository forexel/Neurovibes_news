import { Badge } from "./ui/badge";

const statusConfig = {
  new: { label: "Новая", variant: "default" as const, color: "bg-blue-500/20 text-blue-300 border-blue-500/30" },
  inbox: { label: "Входящие", variant: "secondary" as const, color: "bg-slate-500/20 text-slate-300 border-slate-500/30" },
  review: { label: "Проверка", variant: "secondary" as const, color: "bg-yellow-500/20 text-yellow-300 border-yellow-500/30" },
  double: { label: "Дубликат", variant: "secondary" as const, color: "bg-orange-500/20 text-orange-300 border-orange-500/30" },
  scored: { label: "Оценена", variant: "secondary" as const, color: "bg-purple-500/20 text-purple-300 border-purple-500/30" },
  selected_hourly: { label: "На час", variant: "secondary" as const, color: "bg-cyan-500/20 text-cyan-300 border-cyan-500/30" },
  ready: { label: "Готова", variant: "secondary" as const, color: "bg-green-500/20 text-green-300 border-green-500/30" },
  published: { label: "Опубликована", variant: "secondary" as const, color: "bg-emerald-500/20 text-emerald-300 border-emerald-500/30" },
  archived: { label: "В архиве", variant: "outline" as const, color: "bg-gray-500/10 text-gray-400 border-gray-500/30" },
  rejected: { label: "Отклонена", variant: "destructive" as const, color: "bg-red-500/20 text-red-300 border-red-500/30" },
};

export type ArticleStatus = keyof typeof statusConfig;

interface StatusBadgeProps {
  status: ArticleStatus | string | null | undefined;
}

export function StatusBadge({ status }: StatusBadgeProps) {
  const key = String(status || "").trim().toLowerCase() as ArticleStatus;
  const config = statusConfig[key] ?? {
    label: String(status || "Неизвестно"),
    variant: "outline" as const,
    color: "bg-gray-500/10 text-gray-400 border-gray-500/30",
  };
  return (
    <Badge variant="outline" className={`${config.color} border text-xs px-2 py-0.5`}>
      {config.label}
    </Badge>
  );
}
