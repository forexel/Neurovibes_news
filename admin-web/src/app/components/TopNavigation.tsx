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
  Bot,
} from "lucide-react";
import { toast } from "sonner";
import { api, ApiError } from "../lib/api";

export function TopNavigation() {
  const location = useLocation();
  const navigate = useNavigate();
  const [openMenu, setOpenMenu] = useState<"actions" | "tools" | "account" | null>(null);
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

  function clearCloseTimer() {
    if (closeTimerRef.current !== null) {
      window.clearTimeout(closeTimerRef.current);
      closeTimerRef.current = null;
    }
  }

  function openMenuHover(menu: "actions" | "tools" | "account") {
    clearCloseTimer();
    setOpenMenu(menu);
  }

  function closeMenuHover(menu: "actions" | "tools" | "account") {
    clearCloseTimer();
    closeTimerRef.current = window.setTimeout(() => {
      setOpenMenu((current) => (current === menu ? null : current));
      closeTimerRef.current = null;
    }, 650);
  }

  useEffect(
    () => () => {
      clearCloseTimer();
    },
    [],
  );

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
            <Button variant={isArticlesRoute ? "secondary" : "ghost"} size="sm" asChild>
              <Link to="/dashboard" className="gap-2">
                <FileText className="w-4 h-4" />
                Статьи
              </Link>
            </Button>

            <div
              className="relative"
              onPointerEnter={() => openMenuHover("actions")}
              onMouseEnter={() => openMenuHover("actions")}
              onMouseLeave={() => closeMenuHover("actions")}
            >
              <Button
                variant="ghost"
                size="sm"
                className="gap-2"
                onClick={() => setOpenMenu((value) => (value === "actions" ? null : "actions"))}
              >
                <Zap className="w-4 h-4" />
                Действия
              </Button>
              {openMenu === "actions" ? (
                <div className="absolute left-0 top-full z-[120] mt-1 w-56 rounded-md border border-border bg-popover p-1 shadow-md">
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Собрать за час", () => api.startAggregate("hour"))}>
                    <Database className="w-4 h-4 mr-2" />
                    Собрать за час
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Собрать за день", () => api.startAggregate("day"))}>
                    <Database className="w-4 h-4 mr-2" />
                    Собрать за день
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Собрать за неделю", () => api.startAggregate("week"))}>
                    <Database className="w-4 h-4 mr-2" />
                    Собрать за неделю
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Собрать за месяц", () => api.startAggregate("month"))}>
                    <Database className="w-4 h-4 mr-2" />
                    Собрать за месяц
                  </button>
                  <div className="my-1 h-px bg-border" />
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Запустить Pipeline", () => api.startPipeline(1))}>
                    <Zap className="w-4 h-4 mr-2" />
                    Запустить Pipeline
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Оценить новые", () => api.startScoring(300))}>
                    <FileText className="w-4 h-4 mr-2" />
                    Оценить новые
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Получить полный текст", () => api.startEnrich(30, 300))}>
                    <FileText className="w-4 h-4 mr-2" />
                    Получить полный текст
                  </button>
                  <div className="my-1 h-px bg-border" />
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Отсев не-AI", () => api.pruneNonAi(20000))}>
                    Очистить не-AI
                  </button>
                  <button type="button" className="top-nav-menu-item" onClick={() => runAction("Пересобрать профиль", () => api.rebuildProfile())}>
                    Пересобрать профиль
                  </button>
                </div>
              ) : null}
            </div>

            <div
              className="relative"
              onPointerEnter={() => openMenuHover("tools")}
              onMouseEnter={() => openMenuHover("tools")}
              onMouseLeave={() => closeMenuHover("tools")}
            >
              <Button
                variant="ghost"
                size="sm"
                className="gap-2"
                onClick={() => setOpenMenu((value) => (value === "tools" ? null : "tools"))}
              >
                <Wrench className="w-4 h-4" />
                Инструменты
              </Button>
              {openMenu === "tools" ? (
                <div className="absolute left-0 top-full z-[120] mt-1 w-48 rounded-md border border-border bg-popover p-1 shadow-md">
                  <Link to="/bot" className="top-nav-menu-item">
                    <Bot className="w-4 h-4 mr-2" />
                    Бот
                  </Link>
                  <Link to="/publish" className="top-nav-menu-item">
                    <Send className="w-4 h-4 mr-2" />
                    Публикация
                  </Link>
                  <Link to="/score" className="top-nav-menu-item">
                    <Settings className="w-4 h-4 mr-2" />
                    Оценка
                  </Link>
                </div>
              ) : null}
            </div>

            <Button variant={isActive("/sources") ? "secondary" : "ghost"} size="sm" asChild>
              <Link to="/sources" className="gap-2">
                <Database className="w-4 h-4" />
                Источники
              </Link>
            </Button>
          </div>
        </div>

        <div
          className="relative"
          onPointerEnter={() => openMenuHover("account")}
          onMouseEnter={() => openMenuHover("account")}
          onMouseLeave={() => closeMenuHover("account")}
        >
          <Button
            variant="ghost"
            size="sm"
            className="gap-2"
            onClick={() => setOpenMenu((value) => (value === "account" ? null : "account"))}
          >
            <User className="w-4 h-4" />
            Аккаунт
          </Button>
          {openMenu === "account" ? (
            <div className="absolute right-0 top-full z-[120] mt-1 w-48 rounded-md border border-border bg-popover p-1 shadow-md">
              <Link to="/setup" className="top-nav-menu-item">
                <Settings className="w-4 h-4 mr-2" />
                Настройки
              </Link>
              <div className="my-1 h-px bg-border" />
              <a href="/logout" className="top-nav-menu-item text-destructive">
                <LogOut className="w-4 h-4 mr-2" />
                Выйти
              </a>
            </div>
          ) : null}
        </div>
      </div>
    </nav>
  );
}
