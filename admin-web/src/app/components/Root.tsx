import { Outlet } from "react-router";
import { Toaster } from "./ui/sonner";

export default function Root() {
  return (
    <div className="dark min-h-screen bg-background text-foreground">
      <Outlet />
      <Toaster />
    </div>
  );
}
