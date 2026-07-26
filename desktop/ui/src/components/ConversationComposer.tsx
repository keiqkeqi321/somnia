import type { ChangeEvent, ClipboardEvent, KeyboardEvent, ReactNode, RefObject } from "react";

export type ConversationComposerProps = {
  value?: string;
  onChange?: (value: string, cursor: number) => void;
  onKeyDown?: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onKeyUp?: (event: KeyboardEvent<HTMLTextAreaElement>) => void;
  onSelect?: (cursor: number) => void;
  onClick?: (cursor: number) => void;
  onPaste?: (event: ClipboardEvent<HTMLTextAreaElement>) => void;
  placeholder?: string;
  disabled?: boolean;
  textareaRef?: RefObject<HTMLTextAreaElement>;
  context?: ReactNode;
  attachments?: ReactNode;
  suggestions?: ReactNode;
  controls?: ReactNode;
  actions?: ReactNode;
  fileInput?: ReactNode;
  children?: ReactNode;
  className?: string;
};

/** Shared composer frame; Desktop and Remote provide their own controls as slots. */
export default function ConversationComposer({
  value,
  onChange,
  onKeyDown,
  onKeyUp,
  onSelect,
  onClick,
  onPaste,
  placeholder,
  disabled = false,
  textareaRef,
  context,
  attachments,
  suggestions,
  controls,
  actions,
  fileInput,
  children,
  className,
}: ConversationComposerProps) {
  if (children && value === undefined) {
    return <div className={["conversation-composer", className].filter(Boolean).join(" ")}>{children}</div>;
  }
  return (
    <div className={["conversation-composer", className].filter(Boolean).join(" ")}>
      {context ? <div className="conversation-composer-context">{context}</div> : null}
      <div className="conversation-composer-input">
        <textarea
          ref={textareaRef}
          value={value ?? ""}
          onChange={(event: ChangeEvent<HTMLTextAreaElement>) => onChange?.(event.target.value, event.target.selectionStart)}
          onKeyDown={(event) => onKeyDown?.(event)}
          onKeyUp={(event) => onKeyUp?.(event)}
          onSelect={(event) => onSelect?.(event.currentTarget.selectionStart)}
          onClick={(event) => onClick?.(event.currentTarget.selectionStart)}
          onPaste={onPaste}
          placeholder={placeholder ?? ""}
          disabled={disabled}
          rows={1}
        />
        {attachments}
        {suggestions}
      </div>
      {fileInput}
      {controls ? <div className="conversation-composer-controls">{controls}</div> : null}
      {actions ? <div className="conversation-composer-actions">{actions}</div> : null}
    </div>
  );
}
