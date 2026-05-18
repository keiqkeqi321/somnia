import { formatRelativeTime } from "../lib/messages";
import type { SettingsConfigScope, SettingsConfigScopeKey, SettingsConfigSectionKey } from "../types";

const SETTINGS_SECTIONS = [
  { key: "configuration", icon: "⚙", label: "配置", title: "配置" },
  { key: "archived", icon: "📋", label: "已归档线程", title: "已归档线程" },
] as const;

export type SettingsSectionKey = (typeof SETTINGS_SECTIONS)[number]["key"];

export type ArchivedSessionEntry = {
  key: string;
  projectPath: string;
  projectLabel: string;
  preview: string;
  updatedAt: number | null;
  session: {
    id: string;
  };
};

type SettingsViewProps = {
  activeSection: SettingsSectionKey;
  onSelectSection: (section: SettingsSectionKey) => void;
  onClose: () => void;
  archivedEntries: ArchivedSessionEntry[];
  archivedSelection: ArchivedSessionEntry[];
  selectedArchivedKeys: string[];
  allArchivedSelected: boolean;
  busy: boolean;
  onToggleArchivedSelection: (entryKey: string) => void;
  onToggleSelectAllArchived: () => void;
  onRestoreArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onDeleteArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onOpenPath: (path: string) => void | Promise<void>;
  configScopes: SettingsConfigScope[];
  configDrafts: Record<string, string>;
  selectedConfigScope: SettingsConfigScopeKey;
  selectedConfigSection: SettingsConfigSectionKey;
  configLoading: boolean;
  configSaving: boolean;
  configMessage: string;
  onSelectConfigScope: (scope: SettingsConfigScopeKey) => void;
  onSelectConfigSection: (section: SettingsConfigSectionKey) => void;
  onConfigDraftChange: (key: string, value: string) => void;
  onSaveConfigSection: () => void | Promise<void>;
  onReloadConfig: () => void | Promise<void>;
};

const CONFIG_SECTION_OPTIONS: Array<{ key: SettingsConfigSectionKey; label: string; title: string }> = [
  { key: "provider", label: "Provider", title: "Provider Profiles" },
  { key: "mcp", label: "MCP", title: "MCP Servers" },
  { key: "hooks", label: "Hooks", title: "Hooks" },
  { key: "system_prompt", label: "System Prompt", title: "System Prompt" },
];

function SettingsView({
  activeSection,
  onSelectSection,
  onClose,
  archivedEntries,
  archivedSelection,
  selectedArchivedKeys,
  allArchivedSelected,
  busy,
  onToggleArchivedSelection,
  onToggleSelectAllArchived,
  onRestoreArchived,
  onDeleteArchived,
  onOpenPath,
  configScopes,
  configDrafts,
  selectedConfigScope,
  selectedConfigSection,
  configLoading,
  configSaving,
  configMessage,
  onSelectConfigScope,
  onSelectConfigSection,
  onConfigDraftChange,
  onSaveConfigSection,
  onReloadConfig,
}: SettingsViewProps) {
  const section = SETTINGS_SECTIONS.find((item) => item.key === activeSection) ?? SETTINGS_SECTIONS[0];
  const activeConfigScope = configScopes.find((item) => item.scope === selectedConfigScope) ?? configScopes[0] ?? null;
  const activeDraftKey = `${selectedConfigScope}:${selectedConfigSection}`;
  const activeConfigOption = CONFIG_SECTION_OPTIONS.find((item) => item.key === selectedConfigSection);

  return (
    <section className="settings-shell">
      <aside className="settings-sidebar">
        <button className="settings-back" type="button" onClick={onClose}>
          <span aria-hidden="true">←</span>
          <span>返回应用</span>
        </button>
        <nav className="settings-nav" aria-label="Settings sections">
          {SETTINGS_SECTIONS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`settings-nav-item ${activeSection === item.key ? "selected" : ""}`}
              onClick={() => onSelectSection(item.key)}
            >
              <span aria-hidden="true">{item.icon}</span>
              <span>{item.label}</span>
            </button>
          ))}
        </nav>
      </aside>

      <div className="settings-main">
        <header className="settings-header">
          <h1>{section.title}</h1>
        </header>

        {activeSection === "configuration" ? (
          <div className="settings-group config-settings-group">
            <div className="config-toolbar">
              <div className="config-scope-toggle" role="tablist" aria-label="Configuration scope">
                {(["user", "project"] as SettingsConfigScopeKey[]).map((scope) => (
                  <button
                    key={scope}
                    type="button"
                    className={selectedConfigScope === scope ? "selected" : ""}
                    onClick={() => onSelectConfigScope(scope)}
                  >
                    {scope === "user" ? "User" : "Project"}
                  </button>
                ))}
              </div>
              <button className="settings-inline-button" type="button" onClick={onReloadConfig} disabled={configLoading || configSaving}>
                Reload
              </button>
            </div>
            <div className="config-section-tabs" role="tablist" aria-label="Configuration type">
              {CONFIG_SECTION_OPTIONS.map((item) => (
                <button
                  key={item.key}
                  type="button"
                  className={selectedConfigSection === item.key ? "selected" : ""}
                  onClick={() => onSelectConfigSection(item.key)}
                >
                  {item.label}
                </button>
              ))}
            </div>
            {activeConfigScope ? (
              <>
                <section className="config-panel">
                  <div className="config-path-row">
                    <span>{activeConfigScope.label} config</span>
                    <code>{activeConfigScope.config_path}</code>
                    <button
                      className="settings-inline-button"
                      type="button"
                      onClick={() => onOpenPath(activeConfigScope.config_exists ? activeConfigScope.config_path : parentPath(activeConfigScope.config_path))}
                    >
                      {activeConfigScope.config_exists ? "Open file" : "Open folder"}
                    </button>
                  </div>
                  <div className="config-editor-head">
                    <div>
                      <strong>{activeConfigOption?.title ?? "Configuration"}</strong>
                      <p>编辑当前类别的 TOML 片段。保存会写入所选 scope 的配置文件。</p>
                    </div>
                    <div className="config-editor-actions">
                      <button className="settings-action-button" type="button" onClick={onSaveConfigSection} disabled={configLoading || configSaving}>
                        {configSaving ? "Saving" : "Save"}
                      </button>
                    </div>
                  </div>
                  <textarea
                    className="config-editor"
                    spellCheck={false}
                    value={configDrafts[activeDraftKey] ?? ""}
                    onChange={(event) => onConfigDraftChange(activeDraftKey, event.currentTarget.value)}
                    placeholder={configPlaceholder(selectedConfigSection)}
                    disabled={configLoading}
                  />
                </section>

                <section className="config-panel" data-settings-panel="skills">
                  <div className="config-path-row">
                    <span>Skills · {activeConfigScope.label}</span>
                    <code>{activeConfigScope.skills_path}</code>
                    <button
                      className="settings-inline-button"
                      type="button"
                      onClick={() => onOpenPath(activeConfigScope.skills_exists ? activeConfigScope.skills_path : parentPath(activeConfigScope.skills_path))}
                    >
                      Open folder
                    </button>
                  </div>
                  {activeConfigScope.skills.length === 0 ? (
                    <div className="settings-empty-state">
                      <p>No skills found for this scope.</p>
                    </div>
                  ) : (
                    <div className="config-skill-list">
                      {activeConfigScope.skills.map((skill) => (
                        <div key={`${skill.scope}:${skill.path}`} className="config-skill-row">
                          <strong>{skill.name}</strong>
                          <span>{skill.description}</span>
                          <code>{skill.path}</code>
                        </div>
                      ))}
                    </div>
                  )}
                </section>
                {configMessage ? <p className="config-message">{configMessage}</p> : null}
              </>
            ) : (
              <div className="settings-empty-state">
                <p>{configLoading ? "Loading configuration..." : "Configuration unavailable."}</p>
              </div>
            )}
          </div>
        ) : null}

        {activeSection === "archived" ? (
          <div className="settings-group archived-settings-group">
            <div className="archived-toolbar">
              <label className="archived-select-all">
                <input
                  type="checkbox"
                  checked={allArchivedSelected}
                  onChange={onToggleSelectAllArchived}
                  disabled={archivedEntries.length === 0}
                />
                <span>Select all</span>
              </label>
              <div className="archived-toolbar-actions">
                <button
                  className="settings-action-button"
                  type="button"
                  onClick={() => onRestoreArchived(archivedSelection)}
                  disabled={busy || archivedSelection.length === 0}
                >
                  恢复所选
                </button>
                <button
                  className="settings-action-button danger"
                  type="button"
                  onClick={() => onDeleteArchived(archivedSelection)}
                  disabled={busy || archivedSelection.length === 0}
                >
                  彻底删除所选
                </button>
              </div>
            </div>
            {archivedEntries.length === 0 ? (
              <div className="settings-empty-state">
                <p>没有已归档会话。</p>
              </div>
            ) : (
              <div className="archived-list">
                {archivedEntries.map((entry) => {
                  const isSelected = selectedArchivedKeys.includes(entry.key);
                  return (
                    <div key={entry.key} className={`archived-row ${isSelected ? "selected" : ""}`}>
                      <label className="archived-row-check">
                        <input
                          type="checkbox"
                          checked={isSelected}
                          onChange={() => onToggleArchivedSelection(entry.key)}
                        />
                      </label>
                      <div className="archived-row-copy">
                        <div className="archived-row-head">
                          <strong>{entry.session.id}</strong>
                          <span>{entry.projectLabel}</span>
                          <em>{formatRelativeTime(entry.updatedAt)}</em>
                        </div>
                        <p title={entry.preview || "(empty session)"}>{entry.preview || "(empty session)"}</p>
                        <small>{entry.projectPath}</small>
                      </div>
                      <div className="archived-row-actions">
                        <button className="settings-inline-button" type="button" onClick={() => onRestoreArchived([entry])} disabled={busy}>
                          恢复
                        </button>
                        <button
                          className="settings-inline-button danger"
                          type="button"
                          onClick={() => onDeleteArchived([entry])}
                          disabled={busy}
                        >
                          彻底
                        </button>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function configPlaceholder(section: SettingsConfigSectionKey): string {
  if (section === "provider") {
    return '[providers]\ndefault = "openai"\n\n[providers.openai]\nprovider_type = "openai"\nmodels = ["gpt-4.1"]\ndefault_model = "gpt-4.1"\napi_key = "..."';
  }
  if (section === "mcp") {
    return '[mcp_servers.example]\ntransport = "stdio"\ncommand = "npx"\nargs = ["-y", "@modelcontextprotocol/server-filesystem"]';
  }
  if (section === "hooks") {
    return '[[hooks]]\nevent = "AssistantResponse"\ncommand = "python"\nargs = ["scripts/hook.py"]\nenabled = true';
  }
  return '[agent]\nsystem_prompt = "You are Somnia."';
}

function parentPath(path: string): string {
  const normalized = path.replace(/\\/g, "/").replace(/\/+$/, "");
  const index = normalized.lastIndexOf("/");
  if (index <= 0) {
    return path;
  }
  return normalized.slice(0, index);
}

export default SettingsView;
