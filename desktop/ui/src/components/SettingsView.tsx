import type { ReactNode } from "react";

import { formatRelativeTime } from "../lib/messages";
import type { SettingsConfigScope, SettingsConfigScopeKey, SettingsConfigSectionKey } from "../types";

const SETTINGS_SECTIONS = [
  { key: "general", icon: "⚙", label: "常规", title: "常规" },
  { key: "configuration", icon: "⚙", label: "配置", title: "配置" },
  { key: "environment", icon: "🖥", label: "环境", title: "环境" },
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
  onOpenWorkspaceRoot: () => void;
  workspaceRootPath: string;
  providerLabel: string;
  modelLabel: string;
  reasoningLabel: string;
  executionModeLabel: string;
  connectionState: string;
  archivedEntries: ArchivedSessionEntry[];
  archivedSelection: ArchivedSessionEntry[];
  selectedArchivedKeys: string[];
  allArchivedSelected: boolean;
  busy: boolean;
  onToggleArchivedSelection: (entryKey: string) => void;
  onToggleSelectAllArchived: () => void;
  onRestoreArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onDeleteArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onOpenProviders: () => void;
  onOpenHooks: () => void;
  onOpenModelPicker: () => void;
  onOpenModePicker: () => void;
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
  onOpenWorkspaceRoot,
  workspaceRootPath,
  providerLabel,
  modelLabel,
  reasoningLabel,
  executionModeLabel,
  connectionState,
  archivedEntries,
  archivedSelection,
  selectedArchivedKeys,
  allArchivedSelected,
  busy,
  onToggleArchivedSelection,
  onToggleSelectAllArchived,
  onRestoreArchived,
  onDeleteArchived,
  onOpenProviders,
  onOpenHooks,
  onOpenModelPicker,
  onOpenModePicker,
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

        {activeSection === "general" ? (
          <div className="settings-group">
            <SettingRow
              title="工作区"
              description={workspaceRootPath || "Workspace unavailable"}
              control={
                <button className="settings-action-button" type="button" onClick={onOpenWorkspaceRoot} disabled={!workspaceRootPath}>
                  Open
                </button>
              }
            />
            <SettingRow
              title="默认模型"
              description={`${providerLabel} / ${modelLabel}`}
              control={
                <button className="settings-action-button" type="button" onClick={onOpenModelPicker}>
                  Change
                </button>
              }
            />
            <SettingRow
              title="推理等级"
              description={reasoningLabel}
              control={
                <button className="settings-action-button" type="button" onClick={onOpenModelPicker}>
                  Adjust
                </button>
              }
            />
            <SettingRow
              title="执行模式"
              description={executionModeLabel}
              control={
                <button className="settings-action-button" type="button" onClick={onOpenModePicker}>
                  Change
                </button>
              }
            />
            <SettingRow
              title="连接状态"
              description="Desktop shell talks to the managed sidecar over the local HTTP bridge."
              control={<span className={`settings-status-pill ${connectionState}`}>{connectionState}</span>}
            />
          </div>
        ) : null}

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
                <div className="config-path-row">
                  <span>{activeConfigScope.label} config</span>
                  <code>{activeConfigScope.config_path}</code>
                </div>
                <div className="config-editor-head">
                  <div>
                    <strong>{activeConfigOption?.title ?? "Configuration"}</strong>
                    <p>编辑当前类别的 TOML 片段。保存会写入所选 scope 的配置文件。</p>
                  </div>
                  <div className="config-editor-actions">
                    <button className="settings-inline-button" type="button" onClick={onOpenProviders}>
                      /providers
                    </button>
                    <button className="settings-inline-button" type="button" onClick={onOpenHooks}>
                      /hooks
                    </button>
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
                <div className="config-path-row">
                  <span>Skills · {activeConfigScope.label}</span>
                  <code>{activeConfigScope.skills_path}</code>
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
                {configMessage ? <p className="config-message">{configMessage}</p> : null}
              </>
            ) : (
              <div className="settings-empty-state">
                <p>{configLoading ? "Loading configuration..." : "Configuration unavailable."}</p>
              </div>
            )}
          </div>
        ) : null}

        {activeSection === "environment" ? (
          <div className="settings-group">
            <SettingRow title="Agent environment" description="Windows native" control={<span className="settings-static-value">Windows</span>} />
            <SettingRow title="Integrated terminal shell" description="PowerShell" control={<span className="settings-static-value">PowerShell</span>} />
            <SettingRow
              title="Session storage"
              description="Desktop keeps runtime state under the workspace .open_somnia directory."
              control={<span className="settings-static-value">.open_somnia</span>}
            />
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

function SettingRow({
  title,
  description,
  control,
}: {
  title: string;
  description: string;
  control: ReactNode;
}) {
  return (
    <section className="settings-row">
      <div className="settings-row-copy">
        <strong>{title}</strong>
        <p>{description}</p>
      </div>
      <div className="settings-row-control">{control}</div>
    </section>
  );
}

export default SettingsView;
