import { Route, Routes } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { HomePage } from "@/routes/Home";
import { RobotsPage } from "@/routes/Robots";
import { RobotDetailPage } from "@/routes/RobotDetail";
import { RobotCalibratePage } from "@/routes/RobotCalibrate";
import { RobotInferencePage } from "@/routes/RobotInference";
import { BenchmarksPage } from "@/routes/Benchmarks";
import { BenchmarkRunPage } from "@/routes/BenchmarkRun";
import { NotFoundPage } from "@/routes/NotFound";
import { useTheme } from "@/hooks/useTheme";

export default function App(): JSX.Element {
  useTheme();
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<HomePage />} />
        <Route path="robots" element={<RobotsPage />} />
        <Route path="robots/:id" element={<RobotDetailPage />} />
        <Route path="robots/:id/calibrate" element={<RobotCalibratePage />} />
        <Route path="robots/:id/inference" element={<RobotInferencePage />} />
        <Route path="benchmarks" element={<BenchmarksPage />} />
        <Route path="benchmarks/runs/:runId" element={<BenchmarkRunPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
