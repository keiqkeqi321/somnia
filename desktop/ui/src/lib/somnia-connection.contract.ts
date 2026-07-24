import { describe, expect, it } from "vitest";

import type { AgentSession, SidecarEvent } from "../types";
import type { SomniaConnection, SomniaConnectionNotification } from "./somnia-connection";

export interface SomniaConnectionContractHarness {
  connection: SomniaConnection;
  openStream(): void;
  emitEvent(event: SidecarEvent): void;
  closeStream(): void;
}

export function describeSomniaConnectionContract(
  name: string,
  createHarness: () => SomniaConnectionContractHarness,
  loadedSession: AgentSession,
) {
  describe(`${name} Somnia Connection contract`, () => {
    it("loads a Session and starts a Turn through the shared interface", async () => {
      const harness = createHarness();
      const { connection } = harness;
      connection.subscribe(() => undefined);
      harness.openStream();

      await expect(connection.execute({ type: "session.create" })).resolves.toEqual(loadedSession);
      await expect(connection.query({ type: "session.list" })).resolves.toEqual([loadedSession]);
      await expect(connection.query({ type: "session.load", sessionId: loadedSession.id })).resolves.toEqual(loadedSession);
      await expect(connection.execute({ type: "session.delete", sessionId: loadedSession.id })).resolves.toEqual({ session_id: loadedSession.id, deleted: true });
      await expect(
        connection.execute({ type: "turn.start", sessionId: loadedSession.id, userInput: "Continue" }),
      ).resolves.toEqual({ turn_id: "turn-1", session_id: loadedSession.id });
    });

    it("publishes connection state and Runtime events in order", () => {
      const harness = createHarness();
      const notifications: SomniaConnectionNotification[] = [];
      const unsubscribe = harness.connection.subscribe((notification) => notifications.push(notification));
      const event: SidecarEvent = {
        type: "assistant_delta",
        session_id: loadedSession.id,
        turn_id: "turn-1",
        payload: { delta: "Hello" },
      };

      harness.openStream();
      harness.emitEvent(event);
      harness.closeStream();
      unsubscribe();

      expect(notifications).toEqual([
        { kind: "state", state: "connecting" },
        { kind: "state", state: "connected" },
        { kind: "event", event },
        { kind: "state", state: "disconnected" },
      ]);
      expect(harness.connection.connectionState()).toBe("disconnected");
    });
  });
}
