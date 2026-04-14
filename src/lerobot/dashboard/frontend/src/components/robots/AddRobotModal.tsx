import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { CheckCircle2, Loader2, Plus, Trash2, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  KNOWN_ROBOT_TYPES,
  createCamera,
  createRobot,
  listSerialDevices,
  listVideoDevices,
  probeNetwork,
  type CameraBackend,
  type CameraCreateRequest,
  type Connection,
  type NetworkProtocol,
  type RobotCreateRequest,
  type SerialConnection,
} from "@/lib/api/robots";

type ConnectionKind = "serial" | "network";

interface DraftSerial {
  port: string;
  baudrate: string;
}

interface DraftNetwork {
  protocol: NetworkProtocol;
  host: string;
  port: string;
  username: string;
  password: string;
}

type DraftCameraKind = "local" | "network";

interface DraftCamera {
  localId: string;
  kind: DraftCameraKind;
  name: string;
  backend: CameraBackend;
  devicePath: string;
  url: string;
  width: string;
  height: string;
  fps: string;
}

interface AddRobotModalProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

const defaultSerial = (): DraftSerial => ({ port: "", baudrate: "" });
const defaultNetwork = (): DraftNetwork => ({
  protocol: "rtde",
  host: "",
  port: "",
  username: "",
  password: "",
});

const defaultCamera = (): DraftCamera => ({
  localId: crypto.randomUUID(),
  kind: "local",
  name: "",
  backend: "opencv",
  devicePath: "",
  url: "",
  width: "640",
  height: "480",
  fps: "30",
});

export function AddRobotModal({ open, onOpenChange }: AddRobotModalProps): JSX.Element {
  const queryClient = useQueryClient();
  const [step, setStep] = useState<1 | 2 | 3>(1);
  const [name, setName] = useState("");
  const [robotType, setRobotType] = useState<string>("so101_follower");
  const [connectionKind, setConnectionKind] = useState<ConnectionKind>("serial");
  const [serial, setSerial] = useState<DraftSerial>(defaultSerial);
  const [network, setNetwork] = useState<DraftNetwork>(defaultNetwork);
  const [cameras, setCameras] = useState<DraftCamera[]>([]);
  const [submitError, setSubmitError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) {
      setStep(1);
      setName("");
      setRobotType("so101_follower");
      setConnectionKind("serial");
      setSerial(defaultSerial());
      setNetwork(defaultNetwork());
      setCameras([]);
      setSubmitError(null);
    }
  }, [open]);

  const serialDevicesQuery = useQuery({
    queryKey: ["devices", "serial"],
    queryFn: listSerialDevices,
    enabled: open && connectionKind === "serial",
  });

  const videoDevicesQuery = useQuery({
    queryKey: ["devices", "cameras"],
    queryFn: listVideoDevices,
    enabled: open && step === 3,
  });

  const mutation = useMutation({
    mutationFn: async () => {
      const createdCameraIds: string[] = [];
      for (const draft of cameras) {
        const payload = draftToCameraPayload(draft);
        const created = await createCamera(payload);
        createdCameraIds.push(created.id);
      }
      const connection = buildConnection(connectionKind, serial, network);
      const payload: RobotCreateRequest = {
        name: name.trim(),
        robot_type: robotType,
        connection,
        cameras: createdCameraIds,
      };
      return createRobot(payload);
    },
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["robots"] });
      await queryClient.invalidateQueries({ queryKey: ["cameras"] });
      onOpenChange(false);
    },
    onError: (err: unknown) => {
      setSubmitError(err instanceof Error ? err.message : "Failed to create robot");
    },
  });

  const canProceedFromStep1 = name.trim().length > 0 && robotType.length > 0;
  const canProceedFromStep2 =
    connectionKind === "serial"
      ? serial.port.trim().length > 0
      : network.host.trim().length > 0 && network.port.trim().length > 0;

  function handleAddCamera() {
    setCameras((prev) => [...prev, defaultCamera()]);
  }

  function handleRemoveCamera(id: string) {
    setCameras((prev) => prev.filter((c) => c.localId !== id));
  }

  function handleUpdateCamera(id: string, patch: Partial<DraftCamera>) {
    setCameras((prev) => prev.map((c) => (c.localId === id ? { ...c, ...patch } : c)));
  }

  function handleSubmit() {
    setSubmitError(null);
    try {
      for (const c of cameras) validateCameraDraft(c);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Invalid camera entry");
      return;
    }
    mutation.mutate();
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add robot</DialogTitle>
          <DialogDescription>Step {step} of 3</DialogDescription>
        </DialogHeader>

        {step === 1 ? (
          <Step1
            name={name}
            setName={setName}
            robotType={robotType}
            setRobotType={setRobotType}
            connectionKind={connectionKind}
            setConnectionKind={setConnectionKind}
          />
        ) : null}

        {step === 2 && connectionKind === "serial" ? (
          <Step2Serial
            state={serial}
            setState={setSerial}
            devices={serialDevicesQuery.data ?? []}
            isLoading={serialDevicesQuery.isLoading}
          />
        ) : null}

        {step === 2 && connectionKind === "network" ? (
          <Step2Network state={network} setState={setNetwork} />
        ) : null}

        {step === 3 ? (
          <Step3Cameras
            cameras={cameras}
            videoDevices={videoDevicesQuery.data ?? []}
            onAdd={handleAddCamera}
            onRemove={handleRemoveCamera}
            onUpdate={handleUpdateCamera}
          />
        ) : null}

        {submitError ? (
          <p role="alert" className="text-sm text-destructive">
            {submitError}
          </p>
        ) : null}

        <DialogFooter className="gap-2">
          {step > 1 ? (
            <Button
              type="button"
              variant="outline"
              onClick={() => setStep((s) => (s - 1) as 1 | 2)}
              disabled={mutation.isPending}
            >
              Back
            </Button>
          ) : (
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
              disabled={mutation.isPending}
            >
              Cancel
            </Button>
          )}

          {step < 3 ? (
            <Button
              type="button"
              onClick={() => setStep((s) => (s + 1) as 2 | 3)}
              disabled={step === 1 ? !canProceedFromStep1 : !canProceedFromStep2}
            >
              Next
            </Button>
          ) : (
            <Button type="button" onClick={handleSubmit} disabled={mutation.isPending}>
              {mutation.isPending ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
                  Creating…
                </>
              ) : (
                "Create robot"
              )}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

interface Step1Props {
  name: string;
  setName: (v: string) => void;
  robotType: string;
  setRobotType: (v: string) => void;
  connectionKind: ConnectionKind;
  setConnectionKind: (v: ConnectionKind) => void;
}

function Step1({
  name,
  setName,
  robotType,
  setRobotType,
  connectionKind,
  setConnectionKind,
}: Step1Props): JSX.Element {
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label htmlFor="robot-name">Name</Label>
        <Input
          id="robot-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="e.g. Lab arm #1"
          autoFocus
        />
      </div>

      <div className="grid gap-2">
        <Label htmlFor="robot-type">Robot type</Label>
        <Select
          id="robot-type"
          value={robotType}
          onChange={(e) => setRobotType(e.target.value)}
        >
          {KNOWN_ROBOT_TYPES.map((t) => (
            <option key={t} value={t}>
              {t}
            </option>
          ))}
        </Select>
      </div>

      <fieldset className="grid gap-2">
        <legend className="text-sm font-medium">Connection type</legend>
        <div className="grid grid-cols-2 gap-2">
          <ConnectionKindOption
            value="serial"
            current={connectionKind}
            onSelect={setConnectionKind}
            label="Serial / USB"
            hint="Feetech, Dynamixel, local motor bus"
          />
          <ConnectionKindOption
            value="network"
            current={connectionKind}
            onSelect={setConnectionKind}
            label="Network"
            hint="UR RTDE, Franka FCI, WebSocket, ..."
          />
        </div>
      </fieldset>
    </div>
  );
}

interface ConnectionKindOptionProps {
  value: ConnectionKind;
  current: ConnectionKind;
  onSelect: (v: ConnectionKind) => void;
  label: string;
  hint: string;
}

function ConnectionKindOption({
  value,
  current,
  onSelect,
  label,
  hint,
}: ConnectionKindOptionProps): JSX.Element {
  const selected = value === current;
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={() => onSelect(value)}
      className={`rounded-md border px-4 py-3 text-left transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring ${
        selected ? "border-primary bg-primary/5" : "border-input hover:bg-accent"
      }`}
    >
      <div className="text-sm font-semibold">{label}</div>
      <div className="text-xs text-muted-foreground">{hint}</div>
    </button>
  );
}

interface Step2SerialProps {
  state: DraftSerial;
  setState: React.Dispatch<React.SetStateAction<DraftSerial>>;
  devices: Array<{ port: string; description?: string | null; product?: string | null }>;
  isLoading: boolean;
}

function Step2Serial({ state, setState, devices, isLoading }: Step2SerialProps): JSX.Element {
  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label htmlFor="serial-port">Serial port</Label>
        {isLoading ? (
          <div className="flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Scanning…
          </div>
        ) : devices.length > 0 ? (
          <Select
            id="serial-port"
            value={state.port}
            onChange={(e) => setState((s) => ({ ...s, port: e.target.value }))}
          >
            <option value="">Select a port…</option>
            {devices.map((d) => {
              const label = d.product ?? d.description;
              return (
                <option key={d.port} value={d.port}>
                  {d.port}
                  {label ? ` — ${label}` : ""}
                </option>
              );
            })}
          </Select>
        ) : (
          <Input
            id="serial-port"
            value={state.port}
            onChange={(e) => setState((s) => ({ ...s, port: e.target.value }))}
            placeholder="/dev/ttyUSB0"
          />
        )}
      </div>

      <details className="rounded-md border border-input p-3">
        <summary className="cursor-pointer text-sm font-medium">Advanced</summary>
        <div className="mt-3 grid gap-2">
          <Label htmlFor="serial-baud">Baudrate</Label>
          <Input
            id="serial-baud"
            inputMode="numeric"
            value={state.baudrate}
            onChange={(e) => setState((s) => ({ ...s, baudrate: e.target.value }))}
            placeholder="auto (leave empty)"
          />
        </div>
      </details>
    </div>
  );
}

interface Step2NetworkProps {
  state: DraftNetwork;
  setState: React.Dispatch<React.SetStateAction<DraftNetwork>>;
}

const NETWORK_PROTOCOLS: NetworkProtocol[] = ["rtde", "fci", "xmlrpc", "websocket", "custom"];

function Step2Network({ state, setState }: Step2NetworkProps): JSX.Element {
  const [probeState, setProbeState] = useState<
    | { kind: "idle" }
    | { kind: "running" }
    | { kind: "ok"; latencyMs?: number | null }
    | { kind: "fail"; error: string }
  >({ kind: "idle" });

  const canProbe = state.host.trim() !== "" && state.port.trim() !== "";

  async function handleProbe() {
    setProbeState({ kind: "running" });
    const port = Number.parseInt(state.port, 10);
    if (Number.isNaN(port) || port < 1 || port > 65535) {
      setProbeState({ kind: "fail", error: "Port must be 1–65535" });
      return;
    }
    const result = await probeNetwork({ protocol: state.protocol, host: state.host, port });
    if (result.ok) {
      setProbeState({ kind: "ok", latencyMs: result.latency_ms });
    } else {
      setProbeState({ kind: "fail", error: result.error ?? "Probe failed" });
    }
  }

  return (
    <div className="grid gap-4">
      <div className="grid gap-2">
        <Label htmlFor="net-protocol">Protocol</Label>
        <Select
          id="net-protocol"
          value={state.protocol}
          onChange={(e) =>
            setState((s) => ({ ...s, protocol: e.target.value as NetworkProtocol }))
          }
        >
          {NETWORK_PROTOCOLS.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </Select>
      </div>

      <div className="grid grid-cols-[1fr_8rem] gap-3">
        <div className="grid gap-2">
          <Label htmlFor="net-host">Host</Label>
          <Input
            id="net-host"
            value={state.host}
            onChange={(e) => setState((s) => ({ ...s, host: e.target.value }))}
            placeholder="192.168.1.10"
          />
        </div>
        <div className="grid gap-2">
          <Label htmlFor="net-port">Port</Label>
          <Input
            id="net-port"
            inputMode="numeric"
            value={state.port}
            onChange={(e) => setState((s) => ({ ...s, port: e.target.value }))}
            placeholder="30004"
          />
        </div>
      </div>

      <details className="rounded-md border border-input p-3">
        <summary className="cursor-pointer text-sm font-medium">Auth (optional)</summary>
        <div className="mt-3 grid grid-cols-2 gap-3">
          <div className="grid gap-2">
            <Label htmlFor="net-user">Username</Label>
            <Input
              id="net-user"
              value={state.username}
              onChange={(e) => setState((s) => ({ ...s, username: e.target.value }))}
            />
          </div>
          <div className="grid gap-2">
            <Label htmlFor="net-pass">Password / token</Label>
            <Input
              id="net-pass"
              type="password"
              autoComplete="new-password"
              value={state.password}
              onChange={(e) => setState((s) => ({ ...s, password: e.target.value }))}
            />
          </div>
        </div>
      </details>

      <div className="flex items-center gap-3">
        <Button
          type="button"
          variant="outline"
          onClick={handleProbe}
          disabled={!canProbe || probeState.kind === "running"}
        >
          {probeState.kind === "running" ? (
            <>
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> Testing…
            </>
          ) : (
            "Test connection"
          )}
        </Button>
        <ProbeStatus state={probeState} />
      </div>
    </div>
  );
}

interface ProbeStatusProps {
  state:
    | { kind: "idle" }
    | { kind: "running" }
    | { kind: "ok"; latencyMs?: number | null }
    | { kind: "fail"; error: string };
}

function ProbeStatus({ state }: ProbeStatusProps): JSX.Element | null {
  if (state.kind === "idle" || state.kind === "running") return null;
  if (state.kind === "ok") {
    return (
      <span className="inline-flex items-center gap-1.5 text-sm text-emerald-600 dark:text-emerald-400">
        <CheckCircle2 className="h-4 w-4" aria-hidden />
        Reachable
        {state.latencyMs != null ? ` · ${state.latencyMs.toFixed(0)} ms` : ""}
      </span>
    );
  }
  return (
    <span className="inline-flex items-center gap-1.5 text-sm text-destructive">
      <XCircle className="h-4 w-4" aria-hidden />
      {state.error}
    </span>
  );
}

interface Step3CamerasProps {
  cameras: DraftCamera[];
  videoDevices: Array<{ id: string; path?: string | null; name?: string | null }>;
  onAdd: () => void;
  onRemove: (id: string) => void;
  onUpdate: (id: string, patch: Partial<DraftCamera>) => void;
}

function Step3Cameras({
  cameras,
  videoDevices,
  onAdd,
  onRemove,
  onUpdate,
}: Step3CamerasProps): JSX.Element {
  return (
    <div className="grid gap-3">
      <p className="text-sm text-muted-foreground">
        Attach cameras to this robot. You can skip this step and add cameras later.
      </p>

      {cameras.length === 0 ? (
        <div className="rounded-md border border-dashed border-input p-6 text-center text-sm text-muted-foreground">
          No cameras yet.
        </div>
      ) : (
        <ul className="grid gap-3">
          {cameras.map((cam, idx) => (
            <li
              key={cam.localId}
              className="rounded-md border border-input p-3"
              aria-label={`Camera ${idx + 1}`}
            >
              <CameraDraftRow
                camera={cam}
                videoDevices={videoDevices}
                onRemove={() => onRemove(cam.localId)}
                onUpdate={(patch) => onUpdate(cam.localId, patch)}
              />
            </li>
          ))}
        </ul>
      )}

      <Button type="button" variant="outline" onClick={onAdd} className="self-start">
        <Plus className="h-4 w-4" aria-hidden /> Add camera
      </Button>
    </div>
  );
}

interface CameraDraftRowProps {
  camera: DraftCamera;
  videoDevices: Array<{ id: string; path?: string | null; name?: string | null }>;
  onRemove: () => void;
  onUpdate: (patch: Partial<DraftCamera>) => void;
}

function CameraDraftRow({
  camera,
  videoDevices,
  onRemove,
  onUpdate,
}: CameraDraftRowProps): JSX.Element {
  const resolutionOptions = useMemo(
    () => [
      { label: "640 × 480 @ 30", w: 640, h: 480, fps: 30 },
      { label: "1280 × 720 @ 30", w: 1280, h: 720, fps: 30 },
      { label: "1920 × 1080 @ 30", w: 1920, h: 1080, fps: 30 },
    ],
    [],
  );

  return (
    <div className="grid gap-3">
      <div className="flex items-center justify-between gap-2">
        <Input
          aria-label="Camera name"
          value={camera.name}
          onChange={(e) => onUpdate({ name: e.target.value })}
          placeholder="Camera name"
        />
        <Button
          type="button"
          variant="ghost"
          size="icon"
          onClick={onRemove}
          aria-label="Remove camera"
        >
          <Trash2 className="h-4 w-4" aria-hidden />
        </Button>
      </div>

      <div className="grid grid-cols-2 gap-3">
        <div className="grid gap-1.5">
          <Label>Source</Label>
          <Select
            value={camera.kind}
            onChange={(e) => {
              const kind = e.target.value as DraftCameraKind;
              onUpdate({
                kind,
                backend: kind === "network" ? "network" : "opencv",
              });
            }}
          >
            <option value="local">Local device</option>
            <option value="network">Network camera</option>
          </Select>
        </div>

        <div className="grid gap-1.5">
          <Label>Resolution</Label>
          <Select
            value={`${camera.width}x${camera.height}@${camera.fps}`}
            onChange={(e) => {
              const match = resolutionOptions.find(
                (o) => `${o.w}x${o.h}@${o.fps}` === e.target.value,
              );
              if (match) {
                onUpdate({
                  width: String(match.w),
                  height: String(match.h),
                  fps: String(match.fps),
                });
              }
            }}
          >
            {resolutionOptions.map((o) => (
              <option key={o.label} value={`${o.w}x${o.h}@${o.fps}`}>
                {o.label}
              </option>
            ))}
          </Select>
        </div>
      </div>

      {camera.kind === "local" ? (
        <div className="grid gap-1.5">
          <Label>Device</Label>
          {videoDevices.length > 0 ? (
            <Select
              value={camera.devicePath}
              onChange={(e) => onUpdate({ devicePath: e.target.value })}
            >
              <option value="">Select device…</option>
              {videoDevices.map((d) => {
                const value = d.path ?? d.id;
                const label = d.name ?? d.id;
                return (
                  <option key={d.id} value={value}>
                    {value}
                    {label !== value ? ` — ${label}` : ""}
                  </option>
                );
              })}
            </Select>
          ) : (
            <Input
              value={camera.devicePath}
              onChange={(e) => onUpdate({ devicePath: e.target.value })}
              placeholder="/dev/video0"
            />
          )}
        </div>
      ) : (
        <div className="grid gap-1.5">
          <Label>URL</Label>
          <Input
            value={camera.url}
            onChange={(e) => onUpdate({ url: e.target.value })}
            placeholder="rtsp://user:pass@host:554/stream"
          />
        </div>
      )}
    </div>
  );
}

function buildConnection(
  kind: ConnectionKind,
  serial: DraftSerial,
  network: DraftNetwork,
): Connection {
  if (kind === "serial") {
    const connection: SerialConnection = { kind: "serial", port: serial.port.trim() };
    if (serial.baudrate.trim()) {
      const baud = Number.parseInt(serial.baudrate, 10);
      if (!Number.isNaN(baud)) connection.baudrate = baud;
    }
    return connection;
  }
  const port = Number.parseInt(network.port, 10);
  if (Number.isNaN(port)) throw new Error("Port must be a number");
  const auth =
    network.username || network.password
      ? { username: network.username, password: network.password }
      : null;
  return {
    kind: "network",
    protocol: network.protocol,
    host: network.host.trim(),
    port,
    auth,
  };
}

function validateCameraDraft(draft: DraftCamera): void {
  if (!draft.name.trim()) throw new Error("Every camera needs a name");
  if (draft.kind === "local" && !draft.devicePath.trim()) {
    throw new Error(`"${draft.name}" needs a device path`);
  }
  if (draft.kind === "network" && !draft.url.trim()) {
    throw new Error(`"${draft.name}" needs a URL`);
  }
}

function draftToCameraPayload(draft: DraftCamera): CameraCreateRequest {
  const width = Number.parseInt(draft.width, 10);
  const height = Number.parseInt(draft.height, 10);
  const fps = Number.parseFloat(draft.fps);
  if (Number.isNaN(width) || Number.isNaN(height) || Number.isNaN(fps)) {
    throw new Error(`"${draft.name}" has invalid resolution/fps`);
  }
  const source =
    draft.kind === "local" ? { path: draft.devicePath.trim() } : { url: draft.url.trim() };
  return {
    name: draft.name.trim(),
    backend: draft.backend,
    source,
    width,
    height,
    fps,
  };
}
