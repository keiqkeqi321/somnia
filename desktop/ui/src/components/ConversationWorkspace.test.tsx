import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import type { ConversationRow } from "../types";
import ContextPanel from "./ContextPanel";
import ConversationMessageList from "./ConversationMessageList";
import ConversationMessageRow from "./ConversationMessageRow";
import ConversationPanel from "./ConversationPanel";
import ConversationWorkspace from "./ConversationWorkspace";
import ProgressPanel from "./ProgressPanel";

type RenderFixture = {
  name: "empty" | "streaming" | "completed" | "interrupted" | "resynchronized";
  rows: ConversationRow[];
};

const fixtures: RenderFixture[] = [
  { name: "empty", rows: [] },
  { name: "streaming", rows: [{ id: "stream-1", role: "assistant", text: "Working", isStreaming: true }] },
  { name: "completed", rows: [{ id: "complete-1", role: "assistant", text: "Done", isStreaming: false }] },
  { name: "interrupted", rows: [{ id: "interrupted-1", role: "assistant", text: "Partial response", isStreaming: false }] },
  { name: "resynchronized", rows: [{ id: "resync-1", role: "assistant", text: "Recovered response", isStreaming: false }] },
];

function renderConversation(fixture: RenderFixture): string {
  return renderToStaticMarkup(
    <ConversationWorkspace className="fixture-workspace">
      <ConversationPanel className="fixture-panel" ariaLabel="Conversation">
        <ConversationMessageList className="fixture-list">
          {fixture.rows.map((row) => <ConversationMessageRow key={row.id} row={row}>{row.text}</ConversationMessageRow>)}
        </ConversationMessageList>
        <ProgressPanel className="fixture-progress" ariaLabel="Execution progress">Progress</ProgressPanel>
        <ContextPanel className="fixture-context" ariaLabel="Session context">Context</ContextPanel>
      </ConversationPanel>
    </ConversationWorkspace>,
  );
}

describe("shared ConversationWorkspace rendering", () => {
  it.each(fixtures)("renders the $name conversation fixture through the shared component tree", (fixture) => {
    const html = renderConversation(fixture);

    expect(html).toContain("fixture-workspace");
    expect(html).toContain("fixture-panel");
    expect(html).toContain("fixture-list");
    expect(html).toContain("fixture-progress");
    expect(html).toContain("fixture-context");
    for (const row of fixture.rows) {
      expect(html).toContain(row.text);
      expect(html).toContain(`conversation-message-row-${row.role}`);
      expect(html.includes("conversation-message-row-streaming")).toBe(Boolean(row.isStreaming));
    }
  });
});
