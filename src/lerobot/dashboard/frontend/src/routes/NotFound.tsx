import { Link } from "react-router-dom";
import { Button } from "@/components/ui/button";

export function NotFoundPage(): JSX.Element {
  return (
    <div className="flex min-h-[50vh] flex-col items-center justify-center gap-4 text-center">
      <h1 className="text-4xl font-bold tracking-tight">404</h1>
      <p className="text-muted-foreground">This page does not exist.</p>
      <Button asChild variant="secondary">
        <Link to="/">Back home</Link>
      </Button>
    </div>
  );
}
