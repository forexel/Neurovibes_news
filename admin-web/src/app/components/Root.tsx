import { Outlet } from "react-router";
import { ThemeProvider } from "next-themes";
import { Toaster } from "./ui/sonner";

export default function Root() {
  return (
    <ThemeProvider attribute="class" forcedTheme="dark">
      <div className="dark min-h-screen bg-background text-foreground">
        <Outlet />
        <Toaster />
      </div>
    </ThemeProvider>
  );
}
