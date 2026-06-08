import { formatRelativeTime } from "../lib/messages";
import { SUPPORTED_LOCALES, useI18n, type Locale, type TranslationKey } from "../lib/i18n";
import type { McpServerSummary, ModelDescriptor, ProviderDescriptor, SettingsConfigScope, SettingsConfigScopeKey, SettingsConfigSectionKey } from "../types";
import { useState } from "react";

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
  onSetArchivedSelection: (entryKeys: string[]) => void;
  onRestoreArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onDeleteArchived: (entries: ArchivedSessionEntry[]) => void | Promise<void>;
  onOpenPath: (path: string) => void | Promise<void>;
  configScopes: SettingsConfigScope[];
  configDrafts: Record<string, string>;
  mcpServers: McpServerSummary[];
  selectedConfigScope: SettingsConfigScopeKey;
  configLoading: boolean;
  configSaving: boolean;
  configMessage: string;
  onSelectConfigScope: (scope: SettingsConfigScopeKey) => void;
  onConfigDraftChange: (key: string, value: string) => void;
  onSaveConfigSection: () => void | Promise<void>;
  onDebugMcpServer: (serverName: string) => Promise<number>;
  onSetMcpServerEnabled: (serverName: string, enabled: boolean) => Promise<number>;
  onReloadConfig: () => void | Promise<void>;
  providers: ProviderDescriptor[];
  visionModels: ModelDescriptor[];
  selectedVisionProvider: string;
  selectedVisionModel: string;
  visionModelSaving: boolean;
  onSetVisionProviderDraft: (provider: string) => void;
  onSetVisionModelDraft: (model: string) => void;
  onSaveVisionModel: () => void | Promise<void>;
};

const CONFIG_SECTION_OPTIONS: Array<{ key: SettingsConfigSectionKey; labelKey: TranslationKey; titleKey: TranslationKey }> = [
  { key: "provider", labelKey: "settings.config.provider", titleKey: "settings.config.providerTitle" },
  { key: "mcp", labelKey: "settings.config.mcp", titleKey: "settings.config.mcpTitle" },
  { key: "hooks", labelKey: "settings.config.hooks", titleKey: "settings.config.hooksTitle" },
  { key: "system_prompt", labelKey: "settings.config.systemPrompt", titleKey: "settings.config.systemPromptTitle" },
];

const SETTINGS_SECTIONS = [
  { key: "provider", icon: "P", labelKey: "settings.config.provider", titleKey: "settings.config.providerTitle" },
  { key: "mcp", icon: "M", labelKey: "settings.config.mcp", titleKey: "settings.config.mcpTitle" },
  { key: "hooks", icon: "H", labelKey: "settings.config.hooks", titleKey: "settings.config.hooksTitle" },
  { key: "system_prompt", icon: "S", labelKey: "settings.config.systemPrompt", titleKey: "settings.config.systemPromptTitle" },
  { key: "skills", icon: "K", labelKey: "settings.config.skills", titleKey: "settings.config.skills" },
  { key: "archived", icon: "A", labelKey: "settings.section.archived", titleKey: "settings.section.archived" },
] as const;

export type SettingsSectionKey = (typeof SETTINGS_SECTIONS)[number]["key"];

function isConfigSectionKey(section: SettingsSectionKey): section is SettingsConfigSectionKey {
  return section === "provider" || section === "mcp" || section === "hooks" || section === "system_prompt";
}

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
  onSetArchivedSelection,
  onRestoreArchived,
  onDeleteArchived,
  onOpenPath,
  configScopes,
  configDrafts,
  mcpServers,
  selectedConfigScope,
  configLoading,
  configSaving,
  configMessage,
  onSelectConfigScope,
  onConfigDraftChange,
  onSaveConfigSection,
  onDebugMcpServer,
  onSetMcpServerEnabled,
  onReloadConfig,
  providers,
  visionModels,
  selectedVisionProvider,
  selectedVisionModel,
  visionModelSaving,
  onSetVisionProviderDraft,
  onSetVisionModelDraft,
  onSaveVisionModel,
}: SettingsViewProps) {
  const { locale, setLocale, t } = useI18n();
  const section = SETTINGS_SECTIONS.find((item) => item.key === activeSection) ?? SETTINGS_SECTIONS[0];
  const activeConfigScope = configScopes.find((item) => item.scope === selectedConfigScope) ?? configScopes[0] ?? null;
  const activeConfigSection = isConfigSectionKey(activeSection) ? activeSection : "provider";
  const activeDraftKey = `${selectedConfigScope}:${activeConfigSection}`;
  const activeConfigOption = CONFIG_SECTION_OPTIONS.find((item) => item.key === activeConfigSection);
  const settingsEditorActive = isConfigSectionKey(activeSection) || activeSection === "skills";
  const [expandedMcpServer, setExpandedMcpServer] = useState<string | null>(null);
  const [debuggingMcpServer, setDebuggingMcpServer] = useState<string | null>(null);
  const [togglingMcpServer, setTogglingMcpServer] = useState<string | null>(null);
  const [mcpDebugMessage, setMcpDebugMessage] = useState("");
  const archivedProjectGroups = groupArchivedEntriesByProject(archivedEntries);

  async function handleDebugServer(serverName: string) {
    setExpandedMcpServer(serverName);
    setDebuggingMcpServer(serverName);
    setMcpDebugMessage("");
    try {
      const toolCount = await onDebugMcpServer(serverName);
      setMcpDebugMessage(t("settings.config.debugSuccess", { count: toolCount, name: serverName }));
    } catch (error) {
      setMcpDebugMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setDebuggingMcpServer(null);
    }
  }

  async function handleToggleServer(serverName: string, enabled: boolean) {
    setExpandedMcpServer(serverName);
    setTogglingMcpServer(serverName);
    setMcpDebugMessage("");
    try {
      const toolCount = await onSetMcpServerEnabled(serverName, enabled);
      setMcpDebugMessage(
        enabled
          ? t("settings.config.mcpEnabled", { name: serverName, count: toolCount })
          : t("settings.config.mcpDisabled", { name: serverName }),
      );
    } catch (error) {
      setMcpDebugMessage(error instanceof Error ? error.message : String(error));
    } finally {
      setTogglingMcpServer(null);
    }
  }

  function toggleArchivedProjectSelection(entries: ArchivedSessionEntry[]) {
    const projectKeys = entries.map((entry) => entry.key);
    const projectKeySet = new Set(projectKeys);
    const allProjectSelected = projectKeys.every((key) => selectedArchivedKeys.includes(key));
    if (allProjectSelected) {
      onSetArchivedSelection(selectedArchivedKeys.filter((key) => !projectKeySet.has(key)));
      return;
    }
    onSetArchivedSelection([...selectedArchivedKeys, ...projectKeys.filter((key) => !selectedArchivedKeys.includes(key))]);
  }

  return (
    <section className="settings-shell">
      <aside className="settings-sidebar">
        <button className="settings-back" type="button" onClick={onClose}>
          <span aria-hidden="true">←</span>
          <span>{t("settings.back")}</span>
        </button>
        <div className="settings-language-switcher">
          <span>{t("settings.language.label")}</span>
          <div className="settings-language-options" role="group" aria-label={t("settings.language.label")}>
            {SUPPORTED_LOCALES.map((item) => (
              <button
                key={item}
                type="button"
                className={locale === item ? "selected" : ""}
                onClick={() => setLocale(item as Locale)}
              >
                {t(`settings.language.${item}` as TranslationKey)}
              </button>
            ))}
          </div>
        </div>
        <nav className="settings-nav" aria-label="Settings sections">
          {SETTINGS_SECTIONS.map((item) => (
            <button
              key={item.key}
              type="button"
              className={`settings-nav-item ${activeSection === item.key ? "selected" : ""}`}
              onClick={() => onSelectSection(item.key)}
            >
              <span aria-hidden="true">{item.icon}</span>
              <span>{t(item.labelKey)}</span>
            </button>
          ))}
        </nav>
      </aside>

      <div className="settings-main">
        <header className="settings-header">
          <h1>{t(section.titleKey)}</h1>
        </header>

        {settingsEditorActive ? (
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
                    {scope === "user" ? t("settings.config.user") : t("settings.config.project")}
                  </button>
                ))}
              </div>
              <button className="settings-inline-button" type="button" onClick={onReloadConfig} disabled={configLoading || configSaving}>
                {t("settings.config.reload")}
              </button>
            </div>
            {activeConfigScope ? (
              <>
                {activeSection === "skills" ? (
                  <section className="config-panel" data-settings-panel="skills">
                    <div className="config-path-row">
                      <span>{t("settings.config.skills")} · {activeConfigScope.label}</span>
                      <code>{activeConfigScope.skills_path}</code>
                      <button
                        className="settings-inline-button"
                        type="button"
                        onClick={() => onOpenPath(activeConfigScope.skills_exists ? activeConfigScope.skills_path : parentPath(activeConfigScope.skills_path))}
                      >
                        {t("settings.config.openFolder")}
                      </button>
                    </div>
                    {activeConfigScope.skills.length === 0 ? (
                      <div className="settings-empty-state">
                        <p>{t("settings.config.noSkills")}</p>
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
                ) : (
                  <>
                    <section className="config-panel">
                  <div className="config-path-row">
                    <span>{activeConfigScope.label} {t("settings.config.configLabel")}</span>
                    <code>{activeConfigScope.config_path}</code>
                    <button
                      className="settings-inline-button"
                      type="button"
                      onClick={() => onOpenPath(activeConfigScope.config_exists ? activeConfigScope.config_path : parentPath(activeConfigScope.config_path))}
                    >
                      {activeConfigScope.config_exists ? t("settings.config.openFile") : t("settings.config.openFolder")}
                    </button>
                  </div>
                  {activeConfigSection === "provider" ? (
                    <div className="vision-model-panel">
                      <div className="config-editor-head">
                        <div>
                          <strong>{t("settings.config.visionModel")}</strong>
                          <p>{t("settings.config.visionModelHint")}</p>
                        </div>
                        <button
                          className="settings-action-button"
                          type="button"
                          onClick={onSaveVisionModel}
                          disabled={visionModelSaving || providers.length === 0}
                        >
                          {visionModelSaving ? t("settings.config.saving") : t("settings.config.saveVisionModel")}
                        </button>
                      </div>
                      <div className="vision-model-controls">
                        <label>
                          <span>{t("settings.config.visionProvider")}</span>
                          <select
                            value={selectedVisionProvider}
                            onChange={(event) => onSetVisionProviderDraft(event.currentTarget.value)}
                            disabled={visionModelSaving || providers.length === 0}
                          >
                            <option value="">{t("settings.config.noVisionModel")}</option>
                            {providers.map((provider) => (
                              <option key={provider.name} value={provider.name}>
                                {provider.name}
                              </option>
                            ))}
                          </select>
                        </label>
                        <label>
                          <span>{t("settings.config.visionModel")}</span>
                          <select
                            value={selectedVisionModel}
                            onChange={(event) => onSetVisionModelDraft(event.currentTarget.value)}
                            disabled={visionModelSaving || !selectedVisionProvider}
                          >
                            <option value="">{t("settings.config.noVisionModel")}</option>
                            {visionModels.map((model) => (
                              <option key={model.name} value={model.name}>
                                {model.name}
                              </option>
                            ))}
                          </select>
                        </label>
                      </div>
                    </div>
                  ) : null}
                  <div className="config-editor-head">
                    <div>
                      <strong>{t(activeConfigOption?.titleKey ?? "settings.config.providerTitle")}</strong>
                      <p>{t("settings.config.editorHint")}</p>
                    </div>
                    <div className="config-editor-actions">
                      <button className="settings-action-button" type="button" onClick={onSaveConfigSection} disabled={configLoading || configSaving}>
                        {configSaving ? t("settings.config.saving") : t("settings.config.save")}
                      </button>
                    </div>
                  </div>
                  <textarea
                    className="config-editor"
                    spellCheck={false}
                    value={configDrafts[activeDraftKey] ?? ""}
                    onChange={(event) => onConfigDraftChange(activeDraftKey, event.currentTarget.value)}
                    placeholder={configPlaceholder(activeConfigSection)}
                    disabled={configLoading}
                  />
                    </section>
                    {activeConfigSection === "mcp" ? (
                  <section className="config-panel mcp-runtime-panel">
                    <div className="config-editor-head">
                      <div>
                        <strong>{t("settings.config.runtimeMcpServers")}</strong>
                        <p>{t("settings.config.runtimeMcpHint")}</p>
                      </div>
                    </div>
                    {mcpServers.length === 0 ? (
                      <div className="settings-empty-state">
                        <p>{t("settings.config.noMcpServers")}</p>
                      </div>
                    ) : (
                      <div className="mcp-server-list">
                        {mcpDebugMessage ? <p className="mcp-debug-message">{mcpDebugMessage}</p> : null}
                        {mcpServers.map((server) => {
                          const isExpanded = expandedMcpServer === server.name;
                          return (
                            <div key={server.name} className={`mcp-server-row ${isExpanded ? "expanded" : ""}`}>
                              <div className="mcp-server-summary">
                                <span className={`mcp-status-dot ${server.status}`} aria-hidden="true" />
                                <strong>{server.name}</strong>
                                <span>{server.status}</span>
                                <span>{server.transport}</span>
                                <span>{server.tool_count} {t("settings.config.tools")}</span>
                                <div className="mcp-server-actions">
                                  <label className="mcp-toggle" title={t(server.enabled ? "settings.config.disableMcp" : "settings.config.enableMcp")}>
                                    <input
                                      type="checkbox"
                                      checked={server.enabled}
                                      disabled={togglingMcpServer === server.name}
                                      onChange={(event) => void handleToggleServer(server.name, event.currentTarget.checked)}
                                    />
                                    <span>{togglingMcpServer === server.name ? "..." : server.enabled ? t("settings.config.on") : t("settings.config.off")}</span>
                                  </label>
                                  <button
                                    type="button"
                                    className="settings-inline-button"
                                    onClick={() => void handleDebugServer(server.name)}
                                    disabled={debuggingMcpServer === server.name || !server.enabled}
                                  >
                                    {debuggingMcpServer === server.name ? t("settings.config.fetching") : t("settings.config.debug")}
                                  </button>
                                  <button
                                    type="button"
                                    className="settings-inline-button"
                                    onClick={() => setExpandedMcpServer(isExpanded ? null : server.name)}
                                  >
                                    {isExpanded ? t("settings.config.hide") : t("settings.config.toolsButton")}
                                  </button>
                                </div>
                              </div>
                              {isExpanded ? (
                                <div className="mcp-server-details">
                                  <div className="mcp-server-meta">
                                    <span>{t("settings.config.target")}</span>
                                    <code>{server.target || t("settings.config.unconfigured")}</code>
                                  </div>
                                  {server.error ? <p className="mcp-server-error">{server.error}</p> : null}
                                  {server.tools.length === 0 ? (
                                    <div className="settings-empty-state">
                                      <p>{t("settings.config.noToolsRegistered")}</p>
                                    </div>
                                  ) : (
                                    <div className="mcp-tool-list">
                                      {server.tools.map((tool) => (
                                        <details key={tool.name} className="mcp-tool-card">
                                          <summary>
                                            <strong>{tool.name}</strong>
                                            <span>{tool.description || t("settings.config.noDescription")}</span>
                                          </summary>
                                          <pre>{JSON.stringify(tool.input_schema ?? {}, null, 2)}</pre>
                                        </details>
                                      ))}
                                    </div>
                                  )}
                                </div>
                              ) : null}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </section>
                    ) : null}
                  </>
                )}

                {configMessage ? <p className="config-message">{configMessage}</p> : null}
              </>
            ) : (
              <div className="settings-empty-state">
                <p>{configLoading ? t("settings.config.loading") : t("settings.config.unavailable")}</p>
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
                <span>{t("settings.archived.selectAll")}</span>
              </label>
              <div className="archived-toolbar-actions">
                <button
                  className="settings-action-button"
                  type="button"
                  onClick={() => onRestoreArchived(archivedSelection)}
                  disabled={busy || archivedSelection.length === 0}
                >
                  {t("settings.archived.restoreSelected")}
                </button>
                <button
                  className="settings-action-button danger"
                  type="button"
                  onClick={() => onDeleteArchived(archivedSelection)}
                  disabled={busy || archivedSelection.length === 0}
                >
                  {t("settings.archived.deleteSelected")}
                </button>
              </div>
            </div>
            {archivedEntries.length === 0 ? (
              <div className="settings-empty-state">
                <p>{t("settings.archived.empty")}</p>
              </div>
            ) : (
              <div className="archived-list">
                {archivedProjectGroups.map((group) => (
                  <section key={group.projectPath} className="archived-project-group">
                    <header className="archived-project-head">
                      <label className="archived-project-select">
                        <input
                          type="checkbox"
                          checked={group.entries.every((entry) => selectedArchivedKeys.includes(entry.key))}
                          onChange={() => toggleArchivedProjectSelection(group.entries)}
                        />
                        <span>
                          <strong>{group.projectLabel}</strong>
                          <small>{group.projectPath}</small>
                        </span>
                      </label>
                      <em>{group.entries.length} {group.entries.length === 1 ? t("settings.archived.session") : t("settings.archived.sessions")}</em>
                    </header>
                    <div className="archived-project-rows">
                      {group.entries.map((entry) => {
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
                                <em>{formatRelativeTime(entry.updatedAt)}</em>
                              </div>
                              <p title={entry.preview || t("settings.archived.emptySession")}>{entry.preview || t("settings.archived.emptySession")}</p>
                            </div>
                            <div className="archived-row-actions">
                              <button className="settings-inline-button" type="button" onClick={() => onRestoreArchived([entry])} disabled={busy}>
                                {t("settings.archived.restore")}
                              </button>
                              <button
                                className="settings-inline-button danger"
                                type="button"
                                onClick={() => onDeleteArchived([entry])}
                                disabled={busy}
                              >
                                {t("settings.archived.delete")}
                              </button>
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  </section>
                ))}
              </div>
            )}
          </div>
        ) : null}
      </div>
    </section>
  );
}

function groupArchivedEntriesByProject(entries: ArchivedSessionEntry[]): Array<{
  projectPath: string;
  projectLabel: string;
  entries: ArchivedSessionEntry[];
}> {
  const groups: Array<{ projectPath: string; projectLabel: string; entries: ArchivedSessionEntry[] }> = [];
  const byPath = new Map<string, (typeof groups)[number]>();
  for (const entry of entries) {
    let group = byPath.get(entry.projectPath);
    if (!group) {
      group = {
        projectPath: entry.projectPath,
        projectLabel: entry.projectLabel,
        entries: [],
      };
      byPath.set(entry.projectPath, group);
      groups.push(group);
    }
    group.entries.push(entry);
  }
  return groups;
}

function configPlaceholder(section: SettingsConfigSectionKey): string {
  if (section === "provider") {
    return '[providers]\ndefault = "openai"\n\n[providers.openai]\nprovider_type = "openai"\nmodels = ["gpt-4.1", "gpt-4.1-mini"]\ndefault_model = "gpt-4.1"\napi_key = "..."\n\n[routing]\nvision_provider = "openai"\nvision_model = "gpt-4.1-mini"';
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
