import { ScrollArea } from "./ui/scroll-area";
import { AlertCircle, CheckCircle2, Info } from "lucide-react";

interface LogEntry {
  type: "info" | "success" | "error";
  message: string;
  timestamp?: string;
}

interface LogPanelProps {
  logs: LogEntry[];
  title?: string;
}

export function LogPanel({ logs, title = "Лог операций" }: LogPanelProps) {
  const getIcon = (type: LogEntry["type"]) => {
    switch (type) {
      case "success":
        return <CheckCircle2 className="w-4 h-4 text-green-400" />;
      case "error":
        return <AlertCircle className="w-4 h-4 text-red-400" />;
      default:
        return <Info className="w-4 h-4 text-blue-400" />;
    }
  };

  return (
    <div className="border border-border rounded-lg bg-black/20">
      <div className="px-4 py-2 border-b border-border">
        <h4 className="text-sm text-muted-foreground">{title}</h4>
      </div>
      <ScrollArea className="h-48">
        <div className="p-3 space-y-2 font-mono text-xs">
          {logs.length === 0 ? (
            <div className="text-muted-foreground italic">Нет записей</div>
          ) : (
            logs.map((log, i) => (
              <div key={i} className="flex items-start gap-2">
                {getIcon(log.type)}
                <span className="text-muted-foreground">
                  {log.timestamp && <span className="text-xs mr-2">{log.timestamp}</span>}
                  {log.message}
                </span>
              </div>
            ))
          )}
        </div>
      </ScrollArea>
    </div>
  );
}
