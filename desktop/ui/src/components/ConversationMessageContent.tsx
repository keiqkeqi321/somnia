import type { ReactNode } from "react";

import type { ConversationImageReferenceBlock, ConversationRow, ConversationRowPart } from "../types";
import ConversationMessageRow from "./ConversationMessageRow";

export type ConversationMessageContentProps = {
  row: ConversationRow;
  className?: string;
  showRole?: boolean;
  renderPart: (part: ConversationRowPart) => ReactNode;
  renderImages?: (images: ConversationImageReferenceBlock[]) => ReactNode;
  loading?: ReactNode;
  footer?: ReactNode;
};

/** Shared message tree; clients only provide capability-specific part and image renderers. */
export default function ConversationMessageContent({
  row,
  className,
  showRole = false,
  renderPart,
  renderImages,
  loading,
  footer,
}: ConversationMessageContentProps) {
  const parts = row.parts ?? (row.text ? [{ id: `${row.id}-text`, type: "text" as const, text: row.text }] : []);
  return (
    <ConversationMessageRow row={row} showRole={showRole} className={className}>
      {parts.map((part) => renderPart(part))}
      {row.images?.length ? renderImages?.(row.images) : null}
      {row.isLoading ? loading : null}
      {footer}
    </ConversationMessageRow>
  );
}
