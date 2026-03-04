import { Link, isRouteErrorResponse, useRouteError } from "react-router";
import { AlertTriangle } from "lucide-react";
import { Button } from "./ui/button";

export default function RouteErrorPage() {
  const error = useRouteError();

  let message = "Что-то пошло не так. Обнови страницу или вернись к списку статей.";
  if (isRouteErrorResponse(error)) {
    message = error.statusText || `Ошибка ${error.status}`;
  } else if (error instanceof Error && error.message) {
    message = error.message;
  }

  return (
    <div className="min-h-screen bg-background flex items-center justify-center px-6">
      <div className="w-full max-w-xl rounded-xl border border-border bg-card p-8 text-center">
        <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-red-500/15 text-red-300">
          <AlertTriangle className="h-6 w-6" />
        </div>
        <h1 className="mb-2 text-2xl font-semibold">Ошибка интерфейса</h1>
        <p className="mb-6 text-sm text-muted-foreground">{message}</p>
        <div className="flex items-center justify-center gap-3">
          <Button asChild variant="outline">
            <Link to="/dashboard">К списку</Link>
          </Button>
          <Button onClick={() => window.location.reload()}>Обновить страницу</Button>
        </div>
      </div>
    </div>
  );
}
