import { Suspense } from "react";
import { Outlet } from "react-router";
import { Toaster } from "./ui/sonner";

export default function Root() {
  return (
    <div className="dark min-h-screen bg-background text-foreground">
      <Suspense
        fallback={
          <div className="flex min-h-[40vh] items-center justify-center text-sm text-muted-foreground">
            Загрузка интерфейса...
          </div>
        }
      >
        <Outlet />
      </Suspense>
      <Toaster />
    </div>
  );
}
