export interface WsHandlers {
  onOpen?: (ev: Event) => void;
  onMessage?: (ev: MessageEvent) => void;
  onClose?: (ev: CloseEvent) => void;
  onError?: (ev: Event) => void;
}

export interface WsClientOptions {
  reconnect?: boolean;
  reconnectDelayMs?: number;
  maxReconnectDelayMs?: number;
}

export class WsClient {
  private socket: WebSocket | null = null;
  private closedByUser = false;
  private reconnectAttempts = 0;

  constructor(
    private readonly path: string,
    private readonly handlers: WsHandlers = {},
    private readonly options: WsClientOptions = {},
  ) {}

  connect(): void {
    this.closedByUser = false;
    const url = resolveWsUrl(this.path);
    const ws = new WebSocket(url);
    this.socket = ws;

    ws.addEventListener("open", (ev) => {
      this.reconnectAttempts = 0;
      this.handlers.onOpen?.(ev);
    });
    ws.addEventListener("message", (ev) => this.handlers.onMessage?.(ev));
    ws.addEventListener("error", (ev) => this.handlers.onError?.(ev));
    ws.addEventListener("close", (ev) => {
      this.handlers.onClose?.(ev);
      if (!this.closedByUser && this.options.reconnect) {
        const base = this.options.reconnectDelayMs ?? 500;
        const max = this.options.maxReconnectDelayMs ?? 10_000;
        const delay = Math.min(base * 2 ** this.reconnectAttempts, max);
        this.reconnectAttempts += 1;
        window.setTimeout(() => this.connect(), delay);
      }
    });
  }

  send(data: string | ArrayBufferLike | Blob | ArrayBufferView): void {
    if (this.socket?.readyState === WebSocket.OPEN) {
      this.socket.send(data);
    }
  }

  sendJson(payload: unknown): void {
    this.send(JSON.stringify(payload));
  }

  close(code?: number, reason?: string): void {
    this.closedByUser = true;
    this.socket?.close(code, reason);
    this.socket = null;
  }

  get readyState(): number {
    return this.socket?.readyState ?? WebSocket.CLOSED;
  }
}

export function resolveWsUrl(path: string): string {
  if (/^wss?:\/\//i.test(path)) return path;
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const normalized = path.startsWith("/") ? path : `/${path}`;
  return `${proto}//${window.location.host}${normalized}`;
}
