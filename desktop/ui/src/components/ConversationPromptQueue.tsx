export type ConversationQueuedPrompt = {
  id: string;
  text: string;
  injectionRequested?: boolean;
};

export type ConversationPromptQueueProps = {
  prompts: ConversationQueuedPrompt[];
  canInject: boolean;
  busy: boolean;
  onInject: (prompt: ConversationQueuedPrompt) => void | Promise<void>;
  onRemove?: (prompt: ConversationQueuedPrompt) => void;
  labels: {
    title: string;
    waiting: string;
    inject: string;
    remove?: string;
  };
  className?: string;
};

/** Shared queue card; clients own the scheduling and removal callbacks. */
export default function ConversationPromptQueue({ prompts, canInject, busy, onInject, onRemove, labels, className }: ConversationPromptQueueProps) {
  return (
    <section className={["prompt-queue-card", className].filter(Boolean).join(" ")} aria-live="polite" aria-label={labels.title}>
      <div className="prompt-queue-head">
        <p className="eyebrow">{labels.title}</p>
        <span>{prompts.length}</span>
      </div>
      <ol>
        {prompts.map((prompt) => (
          <li key={prompt.id}>
            <span>{prompt.text}</span>
            <button
              className="queue-inject-button"
              onClick={() => void onInject(prompt)}
              disabled={!canInject || busy || prompt.injectionRequested}
              title={prompt.injectionRequested ? labels.waiting : labels.inject}
            >
              {prompt.injectionRequested ? labels.waiting : labels.inject}
            </button>
            {onRemove ? <button className="queue-remove-button" type="button" onClick={() => onRemove(prompt)} aria-label={`${labels.remove ?? "Remove"} ${prompt.text}`}>{labels.remove ?? "Remove"}</button> : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
