import { createBrowserRouter } from "react-router";
import Root from "./components/Root";
import LoginPage from "./pages/LoginPage";
import RegisterPage from "./pages/RegisterPage";
import SetupWizard from "./pages/SetupWizard";
import ArticlesDashboard from "./pages/ArticlesDashboard";
import ArticleEditor from "./pages/ArticleEditor";
import SourcesPage from "./pages/SourcesPage";
import ScoreSettingsPage from "./pages/ScoreSettingsPage";
import BotControlPage from "./pages/BotControlPage";
import PublishCenterPage from "./pages/PublishCenterPage";

export const router = createBrowserRouter(
  [
    {
      path: "/",
      Component: Root,
      children: [
        { index: true, Component: ArticlesDashboard },
        { path: "login", Component: LoginPage },
        { path: "register", Component: RegisterPage },
        { path: "setup", Component: SetupWizard },
        { path: "dashboard", Component: ArticlesDashboard },
        { path: "published", Component: ArticlesDashboard },
        { path: "backlog", Component: ArticlesDashboard },
        { path: "selected-day", Component: ArticlesDashboard },
        { path: "selected-hour", Component: ArticlesDashboard },
        { path: "unsorted", Component: ArticlesDashboard },
        { path: "no-double", Component: ArticlesDashboard },
        { path: "deleted", Component: ArticlesDashboard },
        { path: "article/:id", Component: ArticleEditor },
        { path: "sources", Component: SourcesPage },
        { path: "score", Component: ScoreSettingsPage },
        { path: "score-settings", Component: ScoreSettingsPage },
        { path: "bot", Component: BotControlPage },
        { path: "bot-control", Component: BotControlPage },
        { path: "publish", Component: PublishCenterPage },
      ],
    },
  ],
);
