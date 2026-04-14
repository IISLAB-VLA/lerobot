import { useQuery } from "@tanstack/react-query";
import { CheckCircle2, Loader2, XCircle } from "lucide-react";
import { fetchHealth, type HealthResponse } from "@/lib/api";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export function HomePage(): JSX.Element {
  const { data, status, error, isFetching } = useQuery<HealthResponse>({
    queryKey: ["health"],
    queryFn: fetchHealth,
    refetchInterval: 5_000,
    refetchOnWindowFocus: false,
  });

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-3xl font-bold tracking-tight">LeRobot Dashboard</h1>
        <p className="text-muted-foreground">
          Real-time robot control, streaming, and observability.
        </p>
      </header>

      <Card>
        <CardHeader>
          <CardTitle>Backend health</CardTitle>
          <CardDescription>
            Polls <code className="font-mono text-xs">/api/health</code> every 5s.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <HealthRow status={status} data={data} error={error} isFetching={isFetching} />
        </CardContent>
      </Card>
    </div>
  );
}

interface HealthRowProps {
  status: "pending" | "error" | "success";
  data: HealthResponse | undefined;
  error: unknown;
  isFetching: boolean;
}

function HealthRow({ status, data, error, isFetching }: HealthRowProps): JSX.Element {
  if (status === "pending") {
    return (
      <div className="flex items-center gap-2 text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
        <span>Checking…</span>
      </div>
    );
  }
  if (status === "error") {
    const message = error instanceof Error ? error.message : "unknown error";
    return (
      <div className="flex items-center gap-2 text-destructive">
        <XCircle className="h-4 w-4" aria-hidden />
        <span>
          Backend unreachable: <span className="font-mono text-xs">{message}</span>
        </span>
      </div>
    );
  }
  return (
    <div className="flex items-center gap-2">
      <CheckCircle2 className="h-4 w-4 text-emerald-500" aria-hidden />
      <span className="font-medium">{data?.status ?? "ok"}</span>
      {data ? (
        <span className="font-mono text-sm text-foreground/70">
          v{data.version} · py{data.python} · uptime {data.uptime_seconds.toFixed(1)}s
        </span>
      ) : null}
      {isFetching ? (
        <Loader2 className="ml-2 h-3 w-3 animate-spin text-muted-foreground" aria-hidden />
      ) : null}
    </div>
  );
}
