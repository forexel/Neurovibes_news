import { Badge } from "./ui/badge";

interface ScoreBadgeProps {
  score: number;
  size?: "sm" | "md" | "lg";
}

export function ScoreBadge({ score, size = "md" }: ScoreBadgeProps) {
  const getColor = (score: number) => {
    if (score >= 8) return "bg-emerald-500/20 text-emerald-300 border-emerald-500/40";
    if (score >= 6) return "bg-green-500/20 text-green-300 border-green-500/40";
    if (score >= 4) return "bg-yellow-500/20 text-yellow-300 border-yellow-500/40";
    if (score >= 2) return "bg-orange-500/20 text-orange-300 border-orange-500/40";
    return "bg-red-500/20 text-red-300 border-red-500/40";
  };

  const sizeClasses = {
    sm: "text-xs px-1.5 py-0",
    md: "text-sm px-2 py-0.5",
    lg: "text-base px-3 py-1",
  };

  return (
    <Badge variant="outline" className={`${getColor(score)} ${sizeClasses[size]} border font-mono`}>
      {score.toFixed(1)}
    </Badge>
  );
}
