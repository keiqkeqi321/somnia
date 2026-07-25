export type RemoteConnectionViewState =
  | "unpaired"
  | "offline"
  | "starting"
  | "ready"
  | "connecting"
  | "reconnecting"
  | "resynchronizing"
  | "local-confirmation";

export type RemoteConnectionStateInput = {
  transport: string;
  deviceStatus?: string;
  hasProject: boolean;
  waitingForLocalConfirmation?: boolean;
  resynchronizing?: boolean;
};

export function deriveRemoteConnectionState(input: RemoteConnectionStateInput): RemoteConnectionViewState {
  if (!input.deviceStatus || input.deviceStatus === "revoked") return "unpaired";
  if (input.waitingForLocalConfirmation) return "local-confirmation";
  if (input.resynchronizing) return "resynchronizing";
  if (input.transport === "connecting") return "connecting";
  if (input.transport === "error" || input.transport === "disconnected") {
    return input.deviceStatus === "reconnecting" ? "reconnecting" : "offline";
  }
  if (input.deviceStatus !== "online") return "starting";
  return input.hasProject ? "ready" : "starting";
}

export function remoteConnectionCopy(state: RemoteConnectionViewState): { label: string; action: string } {
  switch (state) {
    case "unpaired": return { label: "No computer paired", action: "Add computer" };
    case "offline": return { label: "Computer offline; draft kept", action: "Retry connection" };
    case "starting": return { label: "Computer online; workspace starting", action: "Open diagnostics" };
    case "ready": return { label: "Ready to chat", action: "Connect" };
    case "connecting": return { label: "Connecting to computer", action: "Wait" };
    case "reconnecting": return { label: "Network interrupted; recovering safely", action: "Wait or reconnect" };
    case "resynchronizing": return { label: "Resynchronizing from computer", action: "Wait" };
    case "local-confirmation": return { label: "Waiting for confirmation on computer", action: "Confirm locally" };
  }
}
