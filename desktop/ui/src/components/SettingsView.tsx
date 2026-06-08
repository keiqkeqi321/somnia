import { formatRelativeTime } from "../lib/messages";
import { SUPPORTED_LOCALES, useI18n, type Locale, type TranslationKey } from "../lib/i18n";
import type { McpServerSummary, ModelDescriptor, ProviderDescriptor, SettingsConfigScope, SettingsConfigScopeKey, SettingsConfigSectionKey } from "../types";
import { useEffect, useState } from "react";

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
  onSetVisionProviderDraft: (provider: string) => void;
  onSetVisionModelDraft: (model: string) => void;
  onDebugProviderModel: (provider: string, model: string) => Promise<{ ok: boolean; message: string }>;
};

type ProviderProfileDraft = {
  name: string;
  providerType: string;
  modelsText: string;
  defaultModel: string;
  apiKey: string;
  baseUrl: string;
  organization: string;
  contextWindowTokens: string;
  maxTokens: string;
  timeoutSeconds: string;
  reasoningLevel: string;
};

type ModelTraitDraft = {
  provider: string;
  model: string;
  contextWindowTokens: string;
  maxTokens: string;
  reasoningLevel: string;
  supportsReasoning: string;
  supportsAdaptiveReasoning: string;
};

type ProviderConfigDraft = {
  defaultProvider: string;
  profiles: ProviderProfileDraft[];
  modelTraits: Record<string, ModelTraitDraft>;
  extraSections: string[];
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
  onSetVisionProviderDraft,
  onSetVisionModelDraft,
  onDebugProviderModel,
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

  useEffect(() => {
    if (!isConfigSectionKey(activeSection)) {
      return;
    }

    function handleKeyDown(event: KeyboardEvent) {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
        event.preventDefault();
        if (!configLoading && !configSaving) {
          void onSaveConfigSection();
        }
      }
    }

    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [activeSection, configLoading, configSaving, onSaveConfigSection]);

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
              <div className="config-toolbar-actions">
                {activeSection === "skills" ? null : (
                  <button className="settings-inline-button" type="button" onClick={onSaveConfigSection} disabled={configLoading || configSaving}>
                    {configSaving ? t("settings.config.saving") : t("settings.config.save")}
                  </button>
                )}
                <button className="settings-inline-button" type="button" onClick={onReloadConfig} disabled={configLoading || configSaving}>
                  {t("settings.config.reload")}
                </button>
              </div>
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
                    <>
                    <ProviderProfilesEditor
                      text={configDrafts[activeDraftKey] ?? ""}
                      inheritedText={selectedConfigScope === "project" ? configDrafts[`user:${activeConfigSection}`] ?? "" : ""}
                      scope={selectedConfigScope}
                      onChange={(value) => onConfigDraftChange(activeDraftKey, value)}
                      onDebugModel={onDebugProviderModel}
                    />
                    <div className="vision-model-panel">
                      <div className="config-editor-head">
                        <div>
                          <strong>{t("settings.config.visionModel")}</strong>
                        </div>
                      </div>
                      <div className="vision-model-controls">
                        <label>
                          <span>{t("settings.config.visionProvider")}</span>
                          <select
                            value={selectedVisionProvider}
                            onChange={(event) => onSetVisionProviderDraft(event.currentTarget.value)}
                            disabled={configLoading || configSaving || providers.length === 0}
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
                            disabled={configLoading || configSaving || !selectedVisionProvider}
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
                    </>
                  ) : null}
                  <div className="config-editor-head">
                    <div>
                      <strong>
                        {activeConfigSection === "provider"
                          ? selectedConfigScope === "project"
                            ? t("settings.providerProfiles.projectTomlPreview")
                            : t("settings.providerProfiles.tomlPreview")
                          : t(activeConfigOption?.titleKey ?? "settings.config.providerTitle")}
                      </strong>
                      {activeConfigSection === "provider" ? null : <p>{t("settings.config.editorHint")}</p>}
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

function ProviderProfilesEditor({
  text,
  inheritedText,
  scope,
  onChange,
  onDebugModel,
}: {
  text: string;
  inheritedText: string;
  scope: SettingsConfigScopeKey;
  onChange: (value: string) => void;
  onDebugModel: (provider: string, model: string) => Promise<{ ok: boolean; message: string }>;
}) {
  const { t } = useI18n();
  const [selectedProvider, setSelectedProvider] = useState("");
  const [addingModelProvider, setAddingModelProvider] = useState("");
  const [newModelName, setNewModelName] = useState("");
  const [expandedModels, setExpandedModels] = useState<Record<string, boolean>>({});
  const [probeState, setProbeState] = useState<Record<string, { status: "running" | "ok" | "error"; message: string }>>({});
  const config = parseProviderConfigDraft(text);
  const inheritedConfig = scope === "project" ? parseProviderConfigDraft(inheritedText) : emptyProviderConfigDraft();
  const effectiveConfig = scope === "project" ? mergeProviderConfigDrafts(inheritedConfig, config) : config;
  const isProjectScope = scope === "project";
  const activeProvider =
    effectiveConfig.profiles.find((profile) => profile.name === selectedProvider) ??
    effectiveConfig.profiles.find((profile) => profile.name === effectiveConfig.defaultProvider) ??
    effectiveConfig.profiles[0] ??
    null;
  const activeLocalProvider = activeProvider ? config.profiles.find((profile) => profile.name === activeProvider.name) ?? null : null;
  const activeInheritedProvider = activeProvider ? inheritedConfig.profiles.find((profile) => profile.name === activeProvider.name) ?? null : null;
  const activeModels = activeProvider ? modelsFromText(activeProvider.modelsText) : [];
  const defaultModelOptions =
    activeProvider && activeProvider.defaultModel && !activeModels.includes(activeProvider.defaultModel)
      ? [activeProvider.defaultModel, ...activeModels]
      : activeModels;

  function updateConfig(nextConfig: ProviderConfigDraft) {
    onChange(renderProviderConfigDraft(nextConfig));
  }

  function updateProfile(profileName: string, patch: Partial<ProviderProfileDraft>) {
    const existingProfile = config.profiles.find((profile) => profile.name === profileName);
    const profiles = existingProfile
      ? config.profiles.map((profile) => (profile.name === profileName ? { ...profile, ...patch } : profile))
      : [...config.profiles, { ...emptyProviderProfile(profileName), ...patch }];
    updateConfig({ ...config, profiles });
  }

  function revertProfileField(profileName: string, field: keyof ProviderProfileDraft) {
    const profiles = config.profiles
      .map((profile) => (profile.name === profileName ? { ...profile, [field]: field === "name" ? profile.name : "" } : profile))
      .filter((profile) => !isEmptyProviderProfile(profile));
    updateConfig({ ...config, profiles });
  }

  function renameProfile(previousName: string, nextName: string) {
    const normalized = normalizeProviderName(nextName);
    const profiles = config.profiles.map((profile) => (profile.name === previousName ? { ...profile, name: normalized } : profile));
    const modelTraits: Record<string, ModelTraitDraft> = {};
    for (const trait of Object.values(config.modelTraits)) {
      const nextTrait = trait.provider === previousName ? { ...trait, provider: normalized } : trait;
      modelTraits[modelTraitKey(nextTrait.provider, nextTrait.model)] = nextTrait;
    }
    updateConfig({
      ...config,
      defaultProvider: config.defaultProvider === previousName ? normalized : config.defaultProvider,
      profiles,
      modelTraits,
    });
    setSelectedProvider(normalized);
  }

  function addProfile() {
    const name = uniqueProviderName(config.profiles, "provider");
    updateConfig({
      ...config,
      defaultProvider: config.defaultProvider || name,
      modelTraits: config.modelTraits,
      profiles: [
        ...config.profiles,
        {
          name,
          providerType: "openai",
          modelsText: "",
          defaultModel: "",
          apiKey: "",
          baseUrl: "",
          organization: "",
          contextWindowTokens: "",
          maxTokens: "",
          timeoutSeconds: "",
          reasoningLevel: "",
        },
      ],
    });
    setSelectedProvider(name);
  }

  function removeProfile(profileName: string) {
    const profiles = config.profiles.filter((profile) => profile.name !== profileName);
    const modelTraits = Object.fromEntries(
      Object.entries(config.modelTraits).filter(([, trait]) => trait.provider !== profileName)
    );
    updateConfig({
      ...config,
      defaultProvider: config.defaultProvider === profileName ? profiles[0]?.name ?? "" : config.defaultProvider,
      profiles,
      modelTraits,
    });
    setSelectedProvider(isProjectScope && inheritedConfig.profiles.some((profile) => profile.name === profileName) ? profileName : profiles[0]?.name ?? "");
  }

  function updateModelTrait(providerName: string, model: string, patch: Partial<ModelTraitDraft>) {
    const key = modelTraitKey(providerName, model);
    const current = config.modelTraits[key] ?? emptyModelTrait(providerName, model);
    const nextTrait = { ...current, ...patch, provider: providerName, model };
    const modelTraits = { ...config.modelTraits };
    if (isEmptyModelTrait(nextTrait)) {
      delete modelTraits[key];
    } else {
      modelTraits[key] = nextTrait;
    }
    updateConfig({ ...config, modelTraits });
  }

  function addModelsToProfile(profileName: string) {
    const profile = effectiveConfig.profiles.find((item) => item.name === profileName);
    if (!profile) {
      return;
    }
    const nextModels = modelsFromText(profile.modelsText);
    for (const model of modelsFromText(newModelName)) {
      if (!nextModels.includes(model)) {
        nextModels.push(model);
      }
    }
    if (nextModels.length === modelsFromText(profile.modelsText).length) {
      return;
    }
    updateProfile(profileName, {
      modelsText: nextModels.join(", "),
      defaultModel: profile.defaultModel || nextModels[0] || "",
    });
    setNewModelName("");
    setAddingModelProvider("");
  }

  function removeModelFromProfile(providerName: string, model: string) {
    const profile = effectiveConfig.profiles.find((item) => item.name === providerName);
    if (!profile) {
      return;
    }
    const nextModels = modelsFromText(profile.modelsText).filter((item) => item !== model);
    const modelTraits = { ...config.modelTraits };
    delete modelTraits[modelTraitKey(providerName, model)];
    const localProfile = config.profiles.find((item) => item.name === providerName) ?? emptyProviderProfile(providerName);
    const nextProfile = {
      ...localProfile,
      modelsText: nextModels.join(", "),
      defaultModel: profile.defaultModel === model ? nextModels[0] ?? "" : profile.defaultModel,
    };
    const profiles = config.profiles.some((item) => item.name === providerName)
      ? config.profiles.map((item) => (item.name === providerName ? nextProfile : item))
      : [...config.profiles, nextProfile];
    updateConfig({ ...config, profiles, modelTraits });
    setExpandedModels((previous) => {
      const next = { ...previous };
      delete next[modelTraitKey(providerName, model)];
      return next;
    });
    setProbeState((previous) => {
      const next = { ...previous };
      delete next[`${providerName}/${model}`];
      return next;
    });
  }

  async function debugModel(providerName: string, model: string) {
    const key = `${providerName}/${model}`;
    setProbeState((previous) => ({ ...previous, [key]: { status: "running", message: "" } }));
    try {
      const result = await onDebugModel(providerName, model);
      setProbeState((previous) => ({
        ...previous,
        [key]: { status: result.ok ? "ok" : "error", message: result.message },
      }));
    } catch (error) {
      setProbeState((previous) => ({
        ...previous,
        [key]: { status: "error", message: error instanceof Error ? error.message : String(error) },
      }));
    }
  }

  function toggleModelSettings(providerName: string, model: string) {
    const key = modelTraitKey(providerName, model);
    setExpandedModels((previous) => ({ ...previous, [key]: !previous[key] }));
  }

  function profileFieldSource(field: keyof ProviderProfileDraft): "project" | "inherited" {
    if (!isProjectScope || !activeInheritedProvider) {
      return "project";
    }
    if (!activeLocalProvider) {
      return "inherited";
    }
    return providerProfileFieldValue(activeLocalProvider, field) ? "project" : "inherited";
  }

  function fieldLabel(labelKey: TranslationKey, field: keyof ProviderProfileDraft) {
    if (!isProjectScope) {
      return <span>{t(labelKey)}</span>;
    }
    const source = profileFieldSource(field);
    return (
      <span className="provider-field-label">
        <span>{t(labelKey)}</span>
        <span className={`provider-source-pill ${source}`}>{sourceLabel(source, t)}</span>
        {field !== "name" && source === "project" && activeInheritedProvider ? (
          <button className="provider-field-revert" type="button" onClick={() => revertProfileField(activeProvider?.name ?? "", field)}>
            {t("settings.providerProfiles.revert")}
          </button>
        ) : null}
      </span>
    );
  }

  return (
    <div className="provider-profiles-editor">
      <div className="config-editor-head">
        <div>
          <strong>{t("settings.providerProfiles.title")}</strong>
        </div>
        <button
          className="settings-inline-button provider-model-icon-button"
          type="button"
          onClick={addProfile}
          title={t("settings.providerProfiles.add")}
          aria-label={t("settings.providerProfiles.add")}
        >
          +
        </button>
      </div>
      {effectiveConfig.profiles.length === 0 ? (
        <div className="settings-empty-state">
          <p>{t("settings.providerProfiles.empty")}</p>
        </div>
      ) : (
        <div className="provider-profile-layout">
          <div className="provider-profile-list">
            {effectiveConfig.profiles.map((profile) => {
              const profileSource = providerSource(profile.name, config, inheritedConfig, isProjectScope);
              const isDefaultProvider = effectiveConfig.defaultProvider === profile.name;
              return (
              <button
                key={profile.name}
                type="button"
                className={`provider-profile-item ${activeProvider?.name === profile.name ? "selected" : ""}`}
                onClick={() => setSelectedProvider(profile.name)}
              >
                <span className="provider-profile-item-head">
                  <span className="provider-profile-name-wrap">
                    <strong>{profile.name}</strong>
                    {isDefaultProvider ? <span className="provider-default-pill">{t("settings.providerProfiles.defaultBadge")}</span> : null}
                  </span>
                  <small>{t("settings.providerProfiles.modelCount", { count: modelsFromText(profile.modelsText).length })}</small>
                </span>
                <span>{profile.providerType || "openai"} · {profile.defaultModel || t("settings.providerProfiles.noDefault")}</span>
                {isProjectScope ? <span className={`provider-source-pill ${profileSource}`}>{sourceLabel(profileSource, t)}</span> : null}
              </button>
              );
            })}
          </div>
          {activeProvider ? (
            <div className="provider-profile-form">
              <label>
                <span>{t("settings.providerProfiles.name")}</span>
                <input
                  value={activeProvider.name}
                  onChange={(event) => renameProfile(activeProvider.name, event.currentTarget.value)}
                  disabled={isProjectScope && Boolean(activeInheritedProvider)}
                />
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.type", "providerType")}
                <select
                  value={activeProvider.providerType || "openai"}
                  onChange={(event) => updateProfile(activeProvider.name, { providerType: event.currentTarget.value })}
                >
                  <option value="openai">openai</option>
                  <option value="anthropic">anthropic</option>
                </select>
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.baseUrl", "baseUrl")}
                <input value={activeProvider.baseUrl} onChange={(event) => updateProfile(activeProvider.name, { baseUrl: event.currentTarget.value })} />
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.apiKey", "apiKey")}
                <input value={activeProvider.apiKey} onChange={(event) => updateProfile(activeProvider.name, { apiKey: event.currentTarget.value })} />
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.defaultModel", "defaultModel")}
                <select
                  value={activeProvider.defaultModel}
                  onChange={(event) => updateProfile(activeProvider.name, { defaultModel: event.currentTarget.value })}
                >
                  <option value="">{t("settings.providerProfiles.noDefault")}</option>
                  {defaultModelOptions.map((model) => (
                    <option key={model} value={model}>
                      {model}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.organization", "organization")}
                <input value={activeProvider.organization} onChange={(event) => updateProfile(activeProvider.name, { organization: event.currentTarget.value })} />
              </label>
              <label>
                {fieldLabel("settings.providerProfiles.timeoutSeconds", "timeoutSeconds")}
                <input value={activeProvider.timeoutSeconds} onChange={(event) => updateProfile(activeProvider.name, { timeoutSeconds: event.currentTarget.value })} />
              </label>
              <div className="provider-profile-wide provider-model-section">
                <div className="provider-model-section-head">
                  {fieldLabel("settings.providerProfiles.models", "modelsText")}
                  <button
                    className="settings-inline-button provider-model-icon-button"
                    type="button"
                    onClick={() => {
                      setAddingModelProvider(activeProvider.name);
                      setNewModelName("");
                    }}
                    title={t("settings.providerProfiles.addModel")}
                    aria-label={t("settings.providerProfiles.addModel")}
                  >
                    +
                  </button>
                </div>
                {addingModelProvider === activeProvider.name ? (
                  <div className="provider-model-add-panel">
                    <label>
                      <span>{t("settings.providerProfiles.modelName")}</span>
                      <input
                        value={newModelName}
                        onChange={(event) => setNewModelName(event.currentTarget.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") {
                            event.preventDefault();
                            addModelsToProfile(activeProvider.name);
                          }
                          if (event.key === "Escape") {
                            setAddingModelProvider("");
                            setNewModelName("");
                          }
                        }}
                        placeholder="gpt-4.1"
                        autoFocus
                      />
                    </label>
                    <div className="provider-model-add-actions">
                      <button className="settings-inline-button" type="button" onClick={() => addModelsToProfile(activeProvider.name)}>
                        {t("settings.providerProfiles.addModelConfirm")}
                      </button>
                      <button
                        className="settings-inline-button"
                        type="button"
                        onClick={() => {
                          setAddingModelProvider("");
                          setNewModelName("");
                        }}
                      >
                        {t("settings.providerProfiles.cancel")}
                      </button>
                    </div>
                  </div>
                ) : null}
                <div className="provider-model-debug-list">
                  {activeModels.map((model) => {
                    const modelKey = modelTraitKey(activeProvider.name, model);
                    const probe = probeState[`${activeProvider.name}/${model}`];
                    const expanded = Boolean(expandedModels[modelKey]);
                    const trait = effectiveConfig.modelTraits[modelKey] ?? emptyModelTrait(activeProvider.name, model);
                    const isAnthropic = activeProvider.providerType === "anthropic";
                    return (
                      <div key={model} className={`provider-model-debug ${probe?.status ?? ""} ${expanded ? "expanded" : ""}`}>
                        <button
                          className="provider-model-debug-main"
                          type="button"
                          onClick={() => toggleModelSettings(activeProvider.name, model)}
                          aria-expanded={expanded}
                        >
                          <span>{model}</span>
                        </button>
                        <button
                          className="settings-inline-button provider-model-test-button"
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            void debugModel(activeProvider.name, model);
                          }}
                          disabled={probe?.status === "running"}
                          title={t("settings.providerProfiles.test")}
                          aria-label={t("settings.providerProfiles.test")}
                        >
                          {probe?.status === "running" ? (
                            <span className="provider-model-test-running">{t("settings.providerProfiles.testing")}</span>
                          ) : (
                            <svg className="icon" viewBox="0 0 1028 1024" aria-hidden="true">
                              <path d="M868.1 871.8c-8.4 0-16.9-3-23.6-9.1-14.3-13-15.3-35.2-2.3-49.4 74.6-81.9 115.7-188.1 115.7-299 0-244.8-199.2-444-444-444S70 269.5 70 514.3c0 101.7 33.4 197.6 96.7 276.6 12.1 15.1 9.6 37.1-5.5 49.2-15.1 12.1-37.1 9.6-49.2-5.5-35.6-44.6-63.2-94.3-82.3-147.7C10 631.6 0 573.5 0 514.3c0-69.4 13.6-136.7 40.4-200.1 25.9-61.2 63-116.2 110.1-163.4 47.2-47.2 102.2-84.3 163.4-110.1C377.3 13.9 444.6 0.3 514 0.3s136.7 13.6 200.1 40.4c61.2 25.9 116.2 62.9 163.4 110.1C924.6 198 961.7 253 987.6 314.2c26.8 63.4 40.4 130.7 40.4 200.1 0 128.4-47.6 251.3-134 346.1-6.9 7.6-16.4 11.4-25.9 11.4z" />
                              <path d="M681.3 492.8c0.1-0.2 0.2-0.3 0.2-0.5 36.6-76.2 40.7-216.6-1.6-236.9-4.1-2-8.8-2.9-14-2.9-48.1 0-138.1 79.1-171.9 147.5-98.9 10-176 93.5-176 195 0 108.2 87.8 196 196 196s196-87.8 196-196c0-34.2-8.8-66.4-24.2-94.4-1.5-2.7-3-5.2-4.5-7.8z m-78.2 191.3C579.3 707.9 547.7 721 514 721s-65.3-13.1-89.1-36.9C401.1 660.3 388 628.7 388 595s13.1-65.3 36.9-89.1c23.8-23.8 55.4-36.9 89.1-36.9 22.7 0 44.9 6.1 64.2 17.6 4.5 2.7 8.9 5.7 13.1 8.9 13.7 10.6 24.8 23.7 33.2 38.8C634.8 553 640 573.4 640 595c0 33.7-13.1 65.3-36.9 89.1z" />
                            </svg>
                          )}
                        </button>
                        <button
                          className="settings-inline-button provider-model-icon-button danger"
                          type="button"
                          onClick={(event) => {
                            event.stopPropagation();
                            removeModelFromProfile(activeProvider.name, model);
                          }}
                          title={t("settings.providerProfiles.removeModel")}
                          aria-label={t("settings.providerProfiles.removeModel")}
                        >
                          x
                        </button>
                        {probe ? <small>{probe.message}</small> : null}
                        {expanded ? (
                          <div className="provider-model-settings">
                          <label>
                            <span>{t("settings.providerProfiles.contextTokens")}</span>
                            <input
                              value={trait.contextWindowTokens}
                              onChange={(event) =>
                                updateModelTrait(activeProvider.name, model, { contextWindowTokens: event.currentTarget.value })
                              }
                            />
                          </label>
                          <label>
                            <span>{t("settings.providerProfiles.maxTokens")}</span>
                            <input
                              value={trait.maxTokens}
                              onChange={(event) => updateModelTrait(activeProvider.name, model, { maxTokens: event.currentTarget.value })}
                            />
                          </label>
                          <label>
                            <span>{t("settings.providerProfiles.reasoning")}</span>
                            <select
                              value={trait.reasoningLevel}
                              onChange={(event) => updateModelTrait(activeProvider.name, model, { reasoningLevel: event.currentTarget.value })}
                            >
                              <option value="">{t("settings.providerProfiles.auto")}</option>
                              <option value="low">low</option>
                              <option value="medium">medium</option>
                              <option value="high">high</option>
                              <option value="deep">deep</option>
                            </select>
                          </label>
                          <label>
                            <span>{t("settings.providerProfiles.supportsReasoning")}</span>
                            <select
                              value={trait.supportsReasoning}
                              onChange={(event) => updateModelTrait(activeProvider.name, model, { supportsReasoning: event.currentTarget.value })}
                            >
                              <option value="">{t("settings.providerProfiles.auto")}</option>
                              <option value="true">{t("settings.providerProfiles.yes")}</option>
                              <option value="false">{t("settings.providerProfiles.no")}</option>
                            </select>
                          </label>
                          {isAnthropic ? (
                            <label>
                              <span>{t("settings.providerProfiles.adaptiveReasoning")}</span>
                              <select
                                value={trait.supportsAdaptiveReasoning}
                                onChange={(event) =>
                                  updateModelTrait(activeProvider.name, model, { supportsAdaptiveReasoning: event.currentTarget.value })
                                }
                              >
                                <option value="">{t("settings.providerProfiles.auto")}</option>
                                <option value="true">{t("settings.providerProfiles.yes")}</option>
                                <option value="false">{t("settings.providerProfiles.no")}</option>
                              </select>
                            </label>
                          ) : null}
                        </div>
                      ) : null}
                    </div>
                  );
                })}
              </div>
              </div>
              <div className="provider-profile-actions">
                <button className="settings-inline-button" type="button" onClick={() => updateConfig({ ...config, defaultProvider: activeProvider.name })}>
                  {effectiveConfig.defaultProvider === activeProvider.name ? t("settings.providerProfiles.defaultProvider") : t("settings.providerProfiles.setDefault")}
                </button>
                {isProjectScope && activeInheritedProvider && !activeLocalProvider ? null : (
                  <button className="settings-inline-button danger" type="button" onClick={() => removeProfile(activeProvider.name)}>
                    {isProjectScope && activeInheritedProvider ? t("settings.providerProfiles.revertProvider") : t("settings.providerProfiles.remove")}
                  </button>
                )}
              </div>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
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

function emptyProviderConfigDraft(): ProviderConfigDraft {
  return {
    defaultProvider: "",
    profiles: [],
    modelTraits: {},
    extraSections: [],
  };
}

function emptyProviderProfile(name: string): ProviderProfileDraft {
  return {
    name,
    providerType: "",
    modelsText: "",
    defaultModel: "",
    apiKey: "",
    baseUrl: "",
    organization: "",
    contextWindowTokens: "",
    maxTokens: "",
    timeoutSeconds: "",
    reasoningLevel: "",
  };
}

function mergeProviderConfigDrafts(inheritedConfig: ProviderConfigDraft, projectConfig: ProviderConfigDraft): ProviderConfigDraft {
  const profilesByName = new Map<string, { inherited?: ProviderProfileDraft; project?: ProviderProfileDraft }>();
  for (const profile of inheritedConfig.profiles) {
    profilesByName.set(profile.name, { inherited: profile });
  }
  for (const profile of projectConfig.profiles) {
    profilesByName.set(profile.name, { ...profilesByName.get(profile.name), project: profile });
  }
  const profiles = Array.from(profilesByName.entries()).map(([name, item]) =>
    mergeProviderProfileDrafts(name, item.inherited, item.project)
  );
  return {
    defaultProvider: projectConfig.defaultProvider || inheritedConfig.defaultProvider,
    profiles,
    modelTraits: mergeModelTraits(inheritedConfig.modelTraits, projectConfig.modelTraits),
    extraSections: projectConfig.extraSections,
  };
}

function mergeProviderProfileDrafts(
  name: string,
  inheritedProfile: ProviderProfileDraft | undefined,
  projectProfile: ProviderProfileDraft | undefined
): ProviderProfileDraft {
  return {
    name,
    providerType: projectProfile?.providerType || inheritedProfile?.providerType || "openai",
    modelsText: projectProfile?.modelsText || inheritedProfile?.modelsText || "",
    defaultModel: projectProfile?.defaultModel || inheritedProfile?.defaultModel || "",
    apiKey: projectProfile?.apiKey || inheritedProfile?.apiKey || "",
    baseUrl: projectProfile?.baseUrl || inheritedProfile?.baseUrl || "",
    organization: projectProfile?.organization || inheritedProfile?.organization || "",
    contextWindowTokens: projectProfile?.contextWindowTokens || inheritedProfile?.contextWindowTokens || "",
    maxTokens: projectProfile?.maxTokens || inheritedProfile?.maxTokens || "",
    timeoutSeconds: projectProfile?.timeoutSeconds || inheritedProfile?.timeoutSeconds || "",
    reasoningLevel: "",
  };
}

function mergeModelTraits(
  inheritedTraits: Record<string, ModelTraitDraft>,
  projectTraits: Record<string, ModelTraitDraft>
): Record<string, ModelTraitDraft> {
  const merged = { ...inheritedTraits };
  for (const [key, trait] of Object.entries(projectTraits)) {
    const inherited = merged[key] ?? emptyModelTrait(trait.provider, trait.model);
    merged[key] = {
      provider: trait.provider || inherited.provider,
      model: trait.model || inherited.model,
      contextWindowTokens: trait.contextWindowTokens || inherited.contextWindowTokens,
      maxTokens: trait.maxTokens || inherited.maxTokens,
      reasoningLevel: trait.reasoningLevel || inherited.reasoningLevel,
      supportsReasoning: trait.supportsReasoning || inherited.supportsReasoning,
      supportsAdaptiveReasoning: trait.supportsAdaptiveReasoning || inherited.supportsAdaptiveReasoning,
    };
  }
  return merged;
}

function providerProfileFieldValue(profile: ProviderProfileDraft, field: keyof ProviderProfileDraft): string {
  return String(profile[field] ?? "").trim();
}

function isEmptyProviderProfile(profile: ProviderProfileDraft): boolean {
  return (
    !profile.providerType.trim() &&
    !profile.modelsText.trim() &&
    !profile.defaultModel.trim() &&
    !profile.apiKey.trim() &&
    !profile.baseUrl.trim() &&
    !profile.organization.trim() &&
    !profile.contextWindowTokens.trim() &&
    !profile.maxTokens.trim() &&
    !profile.timeoutSeconds.trim()
  );
}

function providerSource(
  profileName: string,
  projectConfig: ProviderConfigDraft,
  inheritedConfig: ProviderConfigDraft,
  isProjectScope: boolean
): "project" | "inherited" {
  if (!isProjectScope) {
    return "project";
  }
  return projectConfig.profiles.some((profile) => profile.name === profileName && !isEmptyProviderProfile(profile)) ||
    !inheritedConfig.profiles.some((profile) => profile.name === profileName)
    ? "project"
    : "inherited";
}

function sourceLabel(source: "project" | "inherited", t: (key: TranslationKey, values?: Record<string, string | number>) => string): string {
  return source === "project" ? t("settings.providerProfiles.projectOverride") : t("settings.providerProfiles.inherited");
}

type TomlSectionDraft = {
  name: string;
  lines: string[];
};

function parseProviderConfigDraft(text: string): ProviderConfigDraft {
  const sections = splitTomlSections(text);
  const providerSection = sections.find((section) => section.name === "providers");
  const defaultProvider = readTomlString(readTomlValue(providerSection?.lines ?? [], "default"));
  const modelTraits: Record<string, ModelTraitDraft> = {};
  for (const section of sections) {
    const modelTraitPath = providerModelTraitPath(section.name);
    if (!modelTraitPath) {
      continue;
    }
    const trait: ModelTraitDraft = {
      provider: modelTraitPath.provider,
      model: modelTraitPath.model,
      contextWindowTokens: readTomlBare(readTomlValue(section.lines, "context_window_tokens")) || readTomlBare(readTomlValue(section.lines, "cwt")),
      maxTokens: readTomlBare(readTomlValue(section.lines, "max_tokens")),
      reasoningLevel: readTomlString(readTomlValue(section.lines, "reasoning_level")),
      supportsReasoning: readTomlBare(readTomlValue(section.lines, "supports_reasoning")),
      supportsAdaptiveReasoning:
        readTomlBare(readTomlValue(section.lines, "supports_adaptive_reasoning")) ||
        readTomlBare(readTomlValue(section.lines, "adaptive_reasoning")),
    };
    modelTraits[modelTraitKey(trait.provider, trait.model)] = trait;
  }
  const profiles = sections
    .filter((section) => section.name.startsWith("providers."))
    .map((section) => {
      const name = section.name.slice("providers.".length).trim();
      return {
        name,
        providerType: readTomlString(readTomlValue(section.lines, "provider_type")),
        modelsText: readTomlArray(readTomlValue(section.lines, "models")).join(", "),
        defaultModel: readTomlString(readTomlValue(section.lines, "default_model")),
        apiKey: readTomlString(readTomlValue(section.lines, "api_key")),
        baseUrl: readTomlString(readTomlValue(section.lines, "base_url")),
        organization: readTomlString(readTomlValue(section.lines, "organization")),
        contextWindowTokens: readTomlBare(readTomlValue(section.lines, "context_window_tokens")),
        maxTokens: readTomlBare(readTomlValue(section.lines, "max_tokens")),
        timeoutSeconds: readTomlBare(readTomlValue(section.lines, "timeout_seconds")),
        reasoningLevel: "",
      };
    });
  const extraSections = sections
    .filter(
      (section) =>
        section.name !== "providers" && !section.name.startsWith("providers.") && !providerModelTraitPath(section.name)
    )
    .map((section) => section.lines.join("\n").trim())
    .filter(Boolean);
  return { defaultProvider, profiles, modelTraits, extraSections };
}

function renderProviderConfigDraft(config: ProviderConfigDraft): string {
  if (
    !config.defaultProvider &&
    config.profiles.length === 0 &&
    Object.values(config.modelTraits).every((item) => isEmptyModelTrait(item)) &&
    config.extraSections.length === 0
  ) {
    return "";
  }
  const lines: string[] = ["[providers]"];
  if (config.defaultProvider) {
    lines.push(`default = ${tomlString(config.defaultProvider)}`);
  }
  for (const profile of config.profiles.filter((item) => !isEmptyProviderProfile(item))) {
    const models = modelsFromText(profile.modelsText);
    lines.push("", `[providers.${normalizeProviderName(profile.name)}]`);
    appendTomlString(lines, "provider_type", profile.providerType);
    if (models.length > 0) {
      lines.push(`models = [${models.map(tomlString).join(", ")}]`);
    }
    appendTomlString(lines, "default_model", profile.defaultModel);
    appendTomlString(lines, "api_key", profile.apiKey);
    appendTomlString(lines, "base_url", profile.baseUrl);
    appendTomlString(lines, "organization", profile.organization);
    appendTomlBare(lines, "context_window_tokens", profile.contextWindowTokens);
    appendTomlBare(lines, "max_tokens", profile.maxTokens);
    appendTomlBare(lines, "timeout_seconds", profile.timeoutSeconds);
  }
  for (const trait of Object.values(config.modelTraits).filter((item) => !isEmptyModelTrait(item))) {
    lines.push("", `[model_traits.${normalizeProviderName(trait.provider)}.${tomlString(trait.model)}]`);
    appendTomlBare(lines, "context_window_tokens", trait.contextWindowTokens);
    appendTomlBare(lines, "max_tokens", trait.maxTokens);
    appendTomlString(lines, "reasoning_level", trait.reasoningLevel);
    appendTomlBoolString(lines, "supports_reasoning", trait.supportsReasoning);
    appendTomlBoolString(lines, "supports_adaptive_reasoning", trait.supportsAdaptiveReasoning);
  }
  for (const section of config.extraSections) {
    lines.push("", section);
  }
  return `${lines.join("\n").trim()}\n`;
}

function splitTomlSections(text: string): TomlSectionDraft[] {
  const sections: TomlSectionDraft[] = [];
  let current: TomlSectionDraft | null = null;
  for (const line of text.split(/\r?\n/)) {
    const trimmed = line.trim();
    const match = trimmed.match(/^\[([^\]]+)\]$/);
    if (match) {
      current = { name: match[1].trim(), lines: [line] };
      sections.push(current);
      continue;
    }
    if (!current) {
      current = { name: "", lines: [] };
      sections.push(current);
    }
    current.lines.push(line);
  }
  return sections;
}

function readTomlValue(lines: string[], key: string): string {
  for (const line of lines) {
    const match = line.match(new RegExp(`^\\s*${key}\\s*=\\s*(.+?)\\s*$`));
    if (match) {
      return stripTomlComment(match[1].trim());
    }
  }
  return "";
}

function stripTomlComment(value: string): string {
  let inString = false;
  for (let index = 0; index < value.length; index += 1) {
    const char = value[index];
    if (char === '"' && value[index - 1] !== "\\") {
      inString = !inString;
    }
    if (char === "#" && !inString) {
      return value.slice(0, index).trim();
    }
  }
  return value;
}

function readTomlString(value: string): string {
  const trimmed = value.trim();
  if (!trimmed) {
    return "";
  }
  if (trimmed.startsWith('"') && trimmed.endsWith('"')) {
    return trimmed.slice(1, -1).replace(/\\"/g, '"').replace(/\\\\/g, "\\");
  }
  return trimmed;
}

function readTomlBare(value: string): string {
  return value.trim();
}

function readTomlArray(value: string): string[] {
  const trimmed = value.trim();
  if (!trimmed.startsWith("[") || !trimmed.endsWith("]")) {
    return [];
  }
  return trimmed
    .slice(1, -1)
    .split(",")
    .map((item) => readTomlString(item.trim()))
    .filter(Boolean);
}

function tomlString(value: string): string {
  return `"${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`;
}

function appendTomlString(lines: string[], key: string, value: string) {
  const trimmed = value.trim();
  if (trimmed) {
    lines.push(`${key} = ${tomlString(trimmed)}`);
  }
}

function appendTomlBare(lines: string[], key: string, value: string) {
  const trimmed = value.trim();
  if (trimmed) {
    lines.push(`${key} = ${trimmed}`);
  }
}

function appendTomlBoolString(lines: string[], key: string, value: string) {
  const trimmed = value.trim().toLowerCase();
  if (trimmed === "true" || trimmed === "false") {
    lines.push(`${key} = ${trimmed}`);
  }
}

function modelsFromText(value: string): string[] {
  return value
    .split(/[,，、]/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function normalizeProviderName(value: string): string {
  return value.trim().toLowerCase().replace(/\s+/g, "-");
}

function uniqueProviderName(profiles: ProviderProfileDraft[], baseName: string): string {
  const existing = new Set(profiles.map((profile) => profile.name));
  let candidate = normalizeProviderName(baseName);
  let index = 2;
  while (existing.has(candidate)) {
    candidate = `${normalizeProviderName(baseName)}-${index}`;
    index += 1;
  }
  return candidate;
}

function modelTraitKey(provider: string, model: string): string {
  return `${normalizeProviderName(provider)}\u0000${model.trim()}`;
}

function emptyModelTrait(provider: string, model: string): ModelTraitDraft {
  return {
    provider: normalizeProviderName(provider),
    model: model.trim(),
    contextWindowTokens: "",
    maxTokens: "",
    reasoningLevel: "",
    supportsReasoning: "",
    supportsAdaptiveReasoning: "",
  };
}

function isEmptyModelTrait(trait: ModelTraitDraft): boolean {
  return (
    !trait.contextWindowTokens.trim() &&
    !trait.maxTokens.trim() &&
    !trait.reasoningLevel.trim() &&
    !trait.supportsReasoning.trim() &&
    !trait.supportsAdaptiveReasoning.trim()
  );
}

function providerModelTraitPath(sectionName: string): { provider: string; model: string } | null {
  const parts = splitTomlPath(sectionName);
  if (parts.length < 3 || parts[0] !== "model_traits") {
    return null;
  }
  const provider = normalizeProviderName(parts[1]);
  const model = parts.slice(2).join(".").trim();
  if (!provider || !model) {
    return null;
  }
  return { provider, model };
}

function splitTomlPath(value: string): string[] {
  const parts: string[] = [];
  let current = "";
  let quote: '"' | "'" | "" = "";
  let escaping = false;
  for (const char of value.trim()) {
    if (escaping) {
      current += char;
      escaping = false;
      continue;
    }
    if (char === "\\" && quote === '"') {
      escaping = true;
      continue;
    }
    if ((char === '"' || char === "'") && !quote) {
      quote = char;
      continue;
    }
    if (char === quote) {
      quote = "";
      continue;
    }
    if (char === "." && !quote) {
      parts.push(current.trim());
      current = "";
      continue;
    }
    current += char;
  }
  parts.push(current.trim());
  return parts.filter(Boolean);
}

export default SettingsView;
