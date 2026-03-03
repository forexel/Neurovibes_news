import { Link, useLocation, useNavigate } from "react-router";
import { Button } from "./ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "./ui/dropdown-menu";
import { 
  FileText, 
  Settings, 
  Zap, 
  Wrench, 
  User, 
  LogOut,
  Database,
  Send,
  Bot
} from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "../lib/api";

export function TopNavigation() {
  const location = useLocation();
  const navigate = useNavigate();

  const articlePaths = new Set([
    "/",
    "/dashboard",
    "/published",
    "/backlog",
    "/selected-day",
    "/selected-hour",
    "/unsorted",
    "/no-double",
    "/deleted",
  ]);

  const isArticlesRoute =
    articlePaths.has(location.pathname) || location.pathname.startsWith("/article/");
  const isActive = (path: string) => location.pathname === path || location.pathname.startsWith(`${path}/`);

  async function runAction(label: string, action: () => Promise<Record<string, unknown>>) {
    try {
      const out = await action();
      toast.success(label, {
        description: typeof out.job_id === "string" ? `Запущено, job ${out.job_id}` : JSON.stringify(out),
      });
      if (isArticlesRoute) {
        navigate(0);
      }
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : err instanceof Error ? err.message : `${label}: failed`;
      toast.error(label, { description: detail });
    }
  }

  return (
    <nav className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-50">
      <div className="mx-auto px-6 h-16 flex items-center justify-between">
        <div className="flex items-center gap-8">
          <Link to="/" className="flex items-center gap-2">
            <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-blue-500 to-purple-600 flex items-center justify-center">
              <FileText className="w-4 h-4 text-white" />
            </div>
            <span className="font-semibold text-lg">AI News Hub</span>
          </Link>

          <div className="flex items-center gap-1">
            <Button
              variant={isArticlesRoute ? "secondary" : "ghost"}
              size="sm"
              asChild
            >
              <Link to="/" className="gap-2">
                <FileText className="w-4 h-4" />
                Статьи
              </Link>
            </Button>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="gap-2">
                  <Zap className="w-4 h-4" />
                  Действия
                </Button>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="start" className="w-56">
                <DropdownMenuItem onClick={() => runAction("Синхронизация", () => api.startAggregate("day"))}>
                  <Database className="w-4 h-4 mr-2" />
                  Синхронизация
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => runAction("Pipeline", () => api.startPipeline(1))}>
                  <Zap className="w-4 h-4 mr-2" />
                  Запустить Pipeline
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => runAction("Скоринг", () => api.startScoring(300))}>
                  <FileText className="w-4 h-4 mr-2" />
                  Оценить новые
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => runAction("Получение полного текста", () => api.startEnrich(30, 300))}>
                  <FileText className="w-4 h-4 mr-2" />
                  Получить полный текст
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => runAction("Prune non-AI", () => api.pruneNonAi(20000))}>
                  Prune
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => runAction("Rebuild Profile", () => api.rebuildProfile())}>
                  Rebuild Profile
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button variant="ghost" size="sm" className="gap-2">
                  <Wrench className="w-4 h-4" />
                  Инструменты
                </Button>
              </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-48">
              <DropdownMenuItem asChild>
                <Link to="/setup" className="cursor-pointer">
                  <Settings className="w-4 h-4 mr-2" />
                  Setup
                </Link>
              </DropdownMenuItem>
              <DropdownMenuItem asChild>
                <Link to="/bot" className="cursor-pointer">
                  <Bot className="w-4 h-4 mr-2" />
                    Бот
                  </Link>
                </DropdownMenuItem>
                <DropdownMenuItem asChild>
                  <Link to="/publish" className="cursor-pointer">
                    <Send className="w-4 h-4 mr-2" />
                    Публикация
                  </Link>
                </DropdownMenuItem>
              <DropdownMenuItem asChild>
                  <Link to="/score" className="cursor-pointer">
                    <Settings className="w-4 h-4 mr-2" />
                    Оценка
                  </Link>
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>

            <Button
              variant={isActive("/sources") ? "secondary" : "ghost"}
              size="sm"
              asChild
            >
              <Link to="/sources" className="gap-2">
                <Database className="w-4 h-4" />
                Источники
              </Link>
            </Button>
          </div>
        </div>

        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="sm" className="gap-2">
              <User className="w-4 h-4" />
              Аккаунт
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end" className="w-48">
            <DropdownMenuItem asChild>
              <Link to="/setup" className="cursor-pointer">
                <Settings className="w-4 h-4 mr-2" />
                Настройки
              </Link>
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <a href="/logout" className="cursor-pointer text-destructive">
                <LogOut className="w-4 h-4 mr-2" />
                Выйти
              </a>
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </div>
    </nav>
  );
}
