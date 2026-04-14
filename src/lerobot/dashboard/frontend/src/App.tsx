import { Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { HomePage } from "@/routes/Home";
import { RobotsPage } from "@/routes/Robots";
import { BenchmarksPage } from "@/routes/Benchmarks";
import { NotFoundPage } from "@/routes/NotFound";
import { useTheme } from "@/hooks/useTheme";

export default function App(): JSX.Element {
  useTheme();
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="robots/*" element={<RobotsPage />} />
        <Route path="benchmarks/*" element={<BenchmarksPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
