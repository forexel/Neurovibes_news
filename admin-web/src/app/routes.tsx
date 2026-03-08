import { createBrowserRouter } from "react-router";
import type { ComponentType } from "react";
import Root from "./components/Root";
import RouteErrorPage from "./components/RouteErrorPage";

const lazyPage = (importer: () => Promise<{ default: ComponentType }>) => async () => {
  const mod = await importer();
  return { Component: mod.default };
};

export const router = createBrowserRouter(
  [
    {
      path: "/",
      Component: Root,
      errorElement: <RouteErrorPage />,
      children: [
        { index: true, lazy: lazyPage(() => import("./pages/PublishCenterPage")) },
        { path: "login", lazy: lazyPage(() => import("./pages/LoginPage")) },
        { path: "register", lazy: lazyPage(() => import("./pages/RegisterPage")) },
        { path: "setup", lazy: lazyPage(() => import("./pages/SetupWizard")) },
        { path: "dashboard", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "published", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "backlog", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "selected-day", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "selected-hour", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "unsorted", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "no-double", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "deleted", lazy: lazyPage(() => import("./pages/ArticlesDashboard")) },
        { path: "article/:id", lazy: lazyPage(() => import("./pages/ArticleEditor")) },
        { path: "sources", lazy: lazyPage(() => import("./pages/SourcesPage")) },
        { path: "score", lazy: lazyPage(() => import("./pages/ScoreSettingsPage")) },
        { path: "score-settings", lazy: lazyPage(() => import("./pages/ScoreSettingsPage")) },
        { path: "bot", lazy: lazyPage(() => import("./pages/BotControlPage")) },
        { path: "bot-control", lazy: lazyPage(() => import("./pages/BotControlPage")) },
        { path: "publish", lazy: lazyPage(() => import("./pages/PublishCenterPage")) },
      ],
    },
  ],
);
