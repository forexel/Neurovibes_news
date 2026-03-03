import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "react-router";
import { Button } from "./ui/button";
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
  const [openMenu, setOpenMenu] = useState<"actions" | "tools" | "account" | null>(null);
  const navRef = useRef<HTMLElement | null>(null);
  const closeTimerRef = useRef<number | null>(null);

  const articlePaths = new Set([
    "/dashboard",
    "/published",
    "/backlog",
    "/selected-day",
    "/selected-hour",
    "/unsorted",
    "/deleted",
  ]);

  const isArticlesRoute =
    articlePaths.has(location.pathname) || location.pathname.startsWith("/article/");
  const isActive = (path: string) => location.pathname === path || location.pathname.startsWith(`${path}/`);

  useEffect(() => {
    function handlePointerDown(event: MouseEvent) {
      if (!navRef.current?.contains(event.target as Node)) {
        setOpenMenu(null);
      }
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => {
      document.removeEventListener("mousedown", handlePointerDown);
      if (closeTimerRef.current !== null) {
        window.clearTimeout(closeTimerRef.current);
      }
    };
  }, []);

  function openMenuOnHover(menu: "actions" | "tools" | "account") {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
    setOpenMenu(menu);
  }

  function closeMenuOnLeave(menu: "actions" | "tools" | "account") {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
    }
    closeTimerRef.current = window.setTimeout(() => {
      setOpenMenu((value) => (value === menu ? null : value));
      closeTimerRef.current = null;
    }, 140);
  }

  async function runAction(label: string, action: () => Promise<Record<string, unknown>>) {
    try {
      const out = await action();
      toast.success(label, {
        description: typeof out.job_id === "string" ? `Запущено, job ${out.job_id}` : JSON.stringify(out),
      });
      if (isArticlesRoute) {
        navigate(0);
      }
      setOpenMenu(null);
    } catch (err) {
      const detail = err instanceof ApiError ? err.detail : err instanceof Error ? err.message : `${label}: failed`;
      toast.error(label, { description: detail });
    }
  }

  return (
    <nav ref={navRef} className="border-b border-border bg-card/50 backdrop-blur-sm sticky top-0 z-50">
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
              <Link to="/dashboard" className="gap-2">
                <FileText className="w-4 h-4" />
                Статьи
              </Link>
            </Button>

            <div
              className="relative"
              onMouseEnter={() => openMenuOnHover("actions")}
              onMouseLeave={() => closeMenuOnLeave("actions")}
            >
              <Button
                variant={openMenu === "actions" ? "secondary" : "ghost"}
                size="sm"
                className="gap-2"
              >
                <Zap className="w-4 h-4" />
                Действия
              </Button>
              {openMenu === "actions" ? (
                <div className="absolute left-0 top-full w-64 pt-2">
                  <div className="rounded-md border border-border bg-popover p-1 shadow-md">
                  <div className="px-2 py-1 text-xs text-muted-foreground">Сбор статей</div>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Собрать за час", () => api.startAggregate("hour"))}
                  >
                    <Database className="w-4 h-4" />
                    Собрать за час
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Собрать за день", () => api.startAggregate("day"))}
                  >
                    <Database className="w-4 h-4" />
                    Собрать за день
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Собрать за неделю", () => api.startAggregate("week"))}
                  >
                    <Database className="w-4 h-4" />
                    Собрать за неделю
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Собрать за месяц", () => api.startAggregate("month"))}
                  >
                    <Database className="w-4 h-4" />
                    Собрать за месяц
                  </button>
                  <div className="my-1 h-px bg-border" />
                  <div className="px-2 py-1 text-xs text-muted-foreground">Обработка</div>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Pipeline", () => api.startPipeline(1))}
                  >
                    <Zap className="w-4 h-4" />
                    Запустить Pipeline
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Скоринг", () => api.startScoring(300))}
                  >
                    <FileText className="w-4 h-4" />
                    Оценить новые
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Получение полного текста", () => api.startEnrich(30, 300))}
                  >
                    <FileText className="w-4 h-4" />
                    Получить полный текст
                  </button>
                  <div className="my-1 h-px bg-border" />
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Отсев не-AI", () => api.pruneNonAi(20000))}
                  >
                    Отсев не-AI
                  </button>
                  <button
                    className="flex w-full items-center gap-2 rounded-sm px-2 py-1.5 text-left text-sm hover:bg-accent"
                    onClick={() => runAction("Пересобрать профиль", () => api.rebuildProfile())}
                  >
                    Пересобрать профиль
                  </button>
                  </div>
                </div>
              ) : null}
            </div>

            <div
              className="relative"
              onMouseEnter={() => openMenuOnHover("tools")}
              onMouseLeave={() => closeMenuOnLeave("tools")}
            >
              <Button
                variant={openMenu === "tools" ? "secondary" : "ghost"}
                size="sm"
                className="gap-2"
              >
                <Wrench className="w-4 h-4" />
                Инструменты
              </Button>
              {openMenu === "tools" ? (
                <div className="absolute left-0 top-full w-48 pt-2">
                  <div className="rounded-md border border-border bg-popover p-1 shadow-md">
                  <Link
                    to="/setup"
                    className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                    onClick={() => setOpenMenu(null)}
                  >
                    <Settings className="w-4 h-4" />
                    Setup
                  </Link>
                  <Link
                    to="/bot"
                    className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                    onClick={() => setOpenMenu(null)}
                  >
                    <Bot className="w-4 h-4" />
                    Бот
                  </Link>
                  <Link
                    to="/publish"
                    className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                    onClick={() => setOpenMenu(null)}
                  >
                    <Send className="w-4 h-4" />
                    Публикация
                  </Link>
                  <Link
                    to="/score"
                    className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                    onClick={() => setOpenMenu(null)}
                  >
                    <Settings className="w-4 h-4" />
                    Оценка
                  </Link>
                  </div>
                </div>
              ) : null}
            </div>

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

        <div
          className="relative"
          onMouseEnter={() => openMenuOnHover("account")}
          onMouseLeave={() => closeMenuOnLeave("account")}
        >
          <Button
            variant={openMenu === "account" ? "secondary" : "ghost"}
            size="sm"
            className="gap-2"
          >
            <User className="w-4 h-4" />
            Аккаунт
          </Button>
          {openMenu === "account" ? (
            <div className="absolute right-0 top-full w-48 pt-2">
              <div className="rounded-md border border-border bg-popover p-1 shadow-md">
              <Link
                to="/setup"
                className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm hover:bg-accent"
                onClick={() => setOpenMenu(null)}
              >
                <Settings className="w-4 h-4" />
                Настройки
              </Link>
              <div className="my-1 h-px bg-border" />
              <a
                href="/logout"
                className="flex items-center gap-2 rounded-sm px-2 py-1.5 text-sm text-destructive hover:bg-accent"
                onClick={() => setOpenMenu(null)}
              >
                <LogOut className="w-4 h-4" />
                Выйти
              </a>
              </div>
            </div>
          ) : null}
        </div>
      </div>
    </nav>
  );
}
