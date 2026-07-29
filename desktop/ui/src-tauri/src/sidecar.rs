use serde::{Deserialize, Serialize};
use std::{
    collections::{hash_map::DefaultHasher, HashMap},
    fs::{self, OpenOptions},
    hash::{Hash, Hasher},
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{Mutex, MutexGuard, Once},
    thread,
    time::{Duration, Instant},
};
use tauri::{Manager, State};

#[cfg(target_os = "windows")]
use std::os::windows::process::CommandExt;

#[cfg(target_os = "windows")]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

const SIDECAR_HOST: &str = "127.0.0.1";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(45);

#[derive(Clone, Serialize)]
#[serde(rename_all = "camelCase")]
pub struct ManagedSidecarConnection {
    pub base_url: String,
    pub ws_url: String,
    pub workspace_root: String,
}

#[derive(Default)]
struct ManagedSidecarState {
    child: Option<Child>,
    connection: Option<ManagedSidecarConnection>,
    port: Option<u16>,
    stdout_log_path: Option<PathBuf>,
    stderr_log_path: Option<PathBuf>,
}

enum SidecarLauncher {
    Bundled(PathBuf),
    PythonModule {
        python: String,
        python_args: Vec<String>,
        repo_root: PathBuf,
    },
}

#[derive(Default)]
pub struct ManagedSidecar {
    inner: Mutex<HashMap<String, ManagedSidecarState>>,
}

impl ManagedSidecar {
    pub fn ensure(
        &self,
        app: &tauri::AppHandle,
        workspace_path: Option<String>,
    ) -> Result<ManagedSidecarConnection, String> {
        let workspace_root = resolve_workspace_root(app, workspace_path)?;
        let workspace_key = workspace_key(&workspace_root)?;
        ORPHAN_SWEEP.call_once(sweep_orphan_sidecars);
        if let Some(connection) = discover_connector_runtime(&workspace_root)? {
            return Ok(connection);
        }
        let mut states = self.lock()?;
        let state = states.entry(workspace_key.clone()).or_default();
        if let Some(connection) = refresh_existing_connection(state)? {
            return Ok(connection);
        }

        let launcher = resolve_sidecar_launcher(app)?;
        let (stdout_log_path, stderr_log_path) = resolve_log_paths(app, &workspace_key)?;
        let port = pick_available_port()?;
        let connection = build_connection(port, &workspace_root);
        let child = spawn_sidecar(
            &launcher,
            port,
            &workspace_root,
            &stdout_log_path,
            &stderr_log_path,
        )?;

        state.child = Some(child);
        state.connection = Some(connection.clone());
        state.port = Some(port);
        state.stdout_log_path = Some(stdout_log_path);
        state.stderr_log_path = Some(stderr_log_path);

        if let Err(error) = wait_for_sidecar_ready(state) {
            stop_locked(state);
            return Err(error);
        }

        Ok(connection)
    }

    pub fn stop_all(&self) -> Result<bool, String> {
        let mut states = self.lock()?;
        let mut stopped = false;
        for state in states.values_mut() {
            stopped |= stop_locked(state);
        }
        states.clear();
        Ok(stopped)
    }

    pub fn stop_workspace(&self, workspace_path: String) -> Result<bool, String> {
        let workspace_root = PathBuf::from(workspace_path.trim())
            .canonicalize()
            .map_err(|error| format!("Unable to resolve workspace path '{}': {error}", workspace_path.trim()))?;
        let workspace_key = workspace_key(&workspace_root)?;
        let mut states = self.lock()?;
        let Some(mut state) = states.remove(&workspace_key) else {
            return Ok(false);
        };
        Ok(stop_locked(&mut state))
    }

    fn lock(&self) -> Result<MutexGuard<'_, HashMap<String, ManagedSidecarState>>, String> {
        self.inner
            .lock()
            .map_err(|_| "Managed sidecar state is unavailable.".to_string())
    }
}

fn refresh_existing_connection(
    state: &mut ManagedSidecarState,
) -> Result<Option<ManagedSidecarConnection>, String> {
    let child_status = match state.child.as_mut() {
        Some(child) => Some(
            child
                .try_wait()
                .map_err(|error| format!("Unable to inspect bundled sidecar process: {error}"))?,
        ),
        None => None,
    };

    match child_status {
        Some(Some(_status)) => {
            stop_locked(state);
            return Ok(None);
        }
        Some(None) | None => {}
    }

    if let (Some(port), Some(connection)) = (state.port, state.connection.clone()) {
        if sidecar_is_ready(port) {
            return Ok(Some(connection));
        }
    }

    if state.child.is_some() {
        stop_locked(state);
    }
    Ok(None)
}

fn resolve_sidecar_launcher(app: &tauri::AppHandle) -> Result<SidecarLauncher, String> {
    let resource_dir = app
        .path()
        .resource_dir()
        .map_err(|error| format!("Unable to resolve the Tauri resource directory: {error}"))?;
    let sidecar_path = resource_dir.join(sidecar_binary_name());
    if sidecar_path.is_file() {
        return Ok(SidecarLauncher::Bundled(sidecar_path));
    }

    let manifest_dir = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    if let Some(repo_root) = manifest_dir
        .parent()
        .and_then(|path| path.parent())
        .and_then(|path| path.parent())
    {
        let bootstrap_path = repo_root.join("desktop").join("backend").join("bootstrap.py");
        if bootstrap_path.is_file() {
            return Ok(SidecarLauncher::PythonModule {
                python: std::env::var("PYTHON").unwrap_or_else(|_| "python".to_string()),
                python_args: std::env::var("SOMNIA_DESKTOP_PYTHON_ARGS")
                    .ok()
                    .map(|value| {
                        value
                            .split('\u{1f}')
                            .filter(|item| !item.is_empty())
                            .map(|item| item.to_string())
                            .collect()
                    })
                    .unwrap_or_default(),
                repo_root: repo_root.to_path_buf(),
            });
        }
    }

    Err(format!(
        "Bundled sidecar executable is missing at '{}' and the development Python sidecar could not be resolved.",
        sidecar_path.display()
    ))
}

fn resolve_workspace_root(
    app: &tauri::AppHandle,
    workspace_path: Option<String>,
) -> Result<PathBuf, String> {
    let workspace_root = match workspace_path {
        Some(path) if !path.trim().is_empty() => PathBuf::from(path.trim()),
        _ => app
            .path()
            .app_data_dir()
            .map_err(|error| format!("Unable to resolve the desktop data directory: {error}"))?
            .join("workspace"),
    };
    fs::create_dir_all(&workspace_root)
        .map_err(|error| format!("Unable to create the managed workspace at '{}': {error}", workspace_root.display()))?;
    workspace_root
        .canonicalize()
        .map_err(|error| format!("Unable to resolve workspace path '{}': {error}", workspace_root.display()))
}

fn resolve_log_paths(app: &tauri::AppHandle, workspace_key: &str) -> Result<(PathBuf, PathBuf), String> {
    let log_dir = app
        .path()
        .app_log_dir()
        .or_else(|_| app.path().app_local_data_dir().map(|path| path.join("logs")))
        .map_err(|error| format!("Unable to resolve the desktop log directory: {error}"))?;
    fs::create_dir_all(&log_dir)
        .map_err(|error| format!("Unable to create the desktop log directory at '{}': {error}", log_dir.display()))?;
    let log_stem = format!("managed-sidecar-{}", stable_workspace_id(workspace_key));
    Ok((
        log_dir.join(format!("{log_stem}.stdout.log")),
        log_dir.join(format!("{log_stem}.stderr.log")),
    ))
}

fn spawn_sidecar(
    launcher: &SidecarLauncher,
    port: u16,
    workspace_root: &PathBuf,
    stdout_log_path: &PathBuf,
    stderr_log_path: &PathBuf,
) -> Result<Child, String> {
    let stdout_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(stdout_log_path)
        .map_err(|error| format!("Unable to open sidecar stdout log '{}': {error}", stdout_log_path.display()))?;
    let stderr_file = OpenOptions::new()
        .create(true)
        .append(true)
        .open(stderr_log_path)
        .map_err(|error| format!("Unable to open sidecar stderr log '{}': {error}", stderr_log_path.display()))?;

    let mut command = match launcher {
        SidecarLauncher::Bundled(sidecar_path) => {
            let mut command = Command::new(sidecar_path);
            command.current_dir(workspace_root);
            command
        }
        SidecarLauncher::PythonModule {
            python,
            python_args,
            repo_root,
        } => {
            let mut command = Command::new(python);
            command
                .args(python_args)
                .arg("-m")
                .arg("desktop.backend.bootstrap")
                .current_dir(repo_root);
            command
        }
    };
    command
        .arg("--workspace")
        .arg(workspace_root)
        .arg("--host")
        .arg(SIDECAR_HOST)
        .arg("--port")
        .arg(port.to_string())
        .arg("--quiet")
        .env("OPEN_SOMNIA_SKIP_BUILTIN_NOTIFY_BOOTSTRAP", "1")
        .env("SOMNIA_SIDECAR_PARENT_PID", std::process::id().to_string())
        .stdout(Stdio::from(stdout_file))
        .stderr(Stdio::from(stderr_file));

    #[cfg(target_os = "windows")]
    command.creation_flags(CREATE_NO_WINDOW);

    command.spawn().map_err(|error| {
        format!(
            "Unable to launch sidecar for workspace '{}': {error}",
            workspace_root.display()
        )
    })
}

fn build_connection(port: u16, workspace_root: &PathBuf) -> ManagedSidecarConnection {
    ManagedSidecarConnection {
        base_url: format!("http://{SIDECAR_HOST}:{port}"),
        ws_url: format!("ws://{SIDECAR_HOST}:{port}/ws"),
        workspace_root: workspace_root.display().to_string(),
    }
}

#[derive(Deserialize)]
struct RuntimeConnectionRegistry {
    connections: Vec<ConnectorRuntimeConnection>,
}

#[derive(Deserialize)]
struct ConnectorRuntimeConnection {
    workspace_root: String,
    base_url: String,
    ws_url: String,
}

fn discover_connector_runtime(workspace_root: &PathBuf) -> Result<Option<ManagedSidecarConnection>, String> {
    let Some(home) = user_home_dir() else {
        return Ok(None);
    };
    let path = home
        .join(".open_somnia")
        .join("remote")
        .join("runtime-connections.json");
    let Ok(contents) = fs::read_to_string(path) else {
        return Ok(None);
    };
    let Ok(registry) = serde_json::from_str::<RuntimeConnectionRegistry>(&contents) else {
        return Ok(None);
    };
    let expected = workspace_key(workspace_root)?;
    for candidate in registry.connections {
        let candidate_root = PathBuf::from(candidate.workspace_root);
        let Ok(candidate_key) = workspace_key(&candidate_root) else {
            continue;
        };
        if candidate_key != expected {
            continue;
        }
        let Some(port) = loopback_port(&candidate.base_url) else {
            continue;
        };
        if sidecar_is_ready(port) {
            return Ok(Some(ManagedSidecarConnection {
                base_url: candidate.base_url,
                ws_url: candidate.ws_url,
                workspace_root: expected,
            }));
        }
    }
    Ok(None)
}

fn user_home_dir() -> Option<PathBuf> {
    std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .map(PathBuf::from)
}

fn loopback_port(base_url: &str) -> Option<u16> {
    let address = base_url.strip_prefix("http://127.0.0.1:")?;
    if address.contains('/') || address.is_empty() {
        return None;
    }
    address.parse().ok()
}

fn pick_available_port() -> Result<u16, String> {
    let listener = TcpListener::bind((SIDECAR_HOST, 0))
        .map_err(|error| format!("Unable to reserve a local port for the bundled sidecar: {error}"))?;
    let port = listener
        .local_addr()
        .map_err(|error| format!("Unable to inspect the reserved sidecar port: {error}"))?
        .port();
    drop(listener);
    Ok(port)
}

fn wait_for_sidecar_ready(state: &mut ManagedSidecarState) -> Result<(), String> {
    let deadline = Instant::now() + STARTUP_TIMEOUT;
    let port = state
        .port
        .ok_or_else(|| "Managed sidecar port is missing.".to_string())?;
    loop {
        if sidecar_is_ready(port) {
            return Ok(());
        }
        let child_status = match state.child.as_mut() {
            Some(child) => Some(
                child
                    .try_wait()
                    .map_err(|error| format!("Unable to inspect bundled sidecar process: {error}"))?,
            ),
            None => None,
        };
        if let Some(Some(status)) = child_status {
            let stderr_log = state
                .stderr_log_path
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|| "unknown stderr log".to_string());
            return Err(format!(
                "Bundled sidecar exited before it became ready (status: {status}). Check {stderr_log}."
            ));
        }
        if Instant::now() >= deadline {
            let stderr_log = state
                .stderr_log_path
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|| "unknown stderr log".to_string());
            let stdout_log = state
                .stdout_log_path
                .as_ref()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|| "unknown stdout log".to_string());
            return Err(format!(
                "Bundled sidecar did not become ready within {} seconds. Check {} and {}.",
                STARTUP_TIMEOUT.as_secs(),
                stdout_log,
                stderr_log
            ));
        }
        thread::sleep(Duration::from_millis(200));
    }
}

fn sidecar_is_ready(port: u16) -> bool {
    let address = format!("{SIDECAR_HOST}:{port}");
    let Ok(mut stream) = TcpStream::connect(&address) else {
        return false;
    };
    if stream
        .set_read_timeout(Some(Duration::from_millis(250)))
        .is_err()
    {
        return false;
    }
    if stream
        .set_write_timeout(Some(Duration::from_millis(250)))
        .is_err()
    {
        return false;
    }

    let request = format!(
        "GET /health HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }

    let mut response = String::new();
    if stream.read_to_string(&mut response).is_err() {
        return false;
    }
    (response.starts_with("HTTP/1.0 200") || response.starts_with("HTTP/1.1 200"))
        && response.contains("\"status\"")
        && response.contains("ready")
}

fn stop_locked(state: &mut ManagedSidecarState) -> bool {
    let mut stopped = false;
    if let Some(mut child) = state.child.take() {
        stopped = true;
        terminate_child(&mut child);
        let _ = child.wait();
    }
    state.connection = None;
    state.port = None;
    state.stdout_log_path = None;
    state.stderr_log_path = None;
    stopped
}

#[cfg(target_os = "windows")]
fn terminate_child(child: &mut Child) {
    if matches!(child.try_wait(), Ok(Some(_))) {
        return;
    }

    let mut command = Command::new("taskkill");
    command
        .args(["/PID", &child.id().to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .stdout(Stdio::null())
        .stderr(Stdio::null());

    if !matches!(command.status(), Ok(status) if status.success()) {
        let _ = child.kill();
    }
}

#[cfg(not(target_os = "windows"))]
fn terminate_child(child: &mut Child) {
    let _ = child.kill();
}

fn sidecar_binary_name() -> &'static str {
    #[cfg(target_os = "windows")]
    {
        "somnia-sidecar.exe"
    }
    #[cfg(not(target_os = "windows"))]
    {
        "somnia-sidecar"
    }
}

fn workspace_key(workspace_root: &PathBuf) -> Result<String, String> {
    workspace_root
        .canonicalize()
        .map(|path| path.display().to_string())
        .map_err(|error| format!("Unable to resolve workspace path '{}': {error}", workspace_root.display()))
}

fn stable_workspace_id(workspace_key: &str) -> String {
    let mut hasher = DefaultHasher::new();
    workspace_key.hash(&mut hasher);
    format!("{:016x}", hasher.finish())
}

#[tauri::command]
pub fn ensure_managed_sidecar(
    app: tauri::AppHandle,
    sidecar: State<'_, ManagedSidecar>,
    workspace_path: Option<String>,
) -> Result<ManagedSidecarConnection, String> {
    sidecar.ensure(&app, workspace_path)
}

#[tauri::command]
pub fn stop_managed_sidecar(
    sidecar: State<'_, ManagedSidecar>,
    workspace_path: Option<String>,
) -> Result<bool, String> {
    match workspace_path {
        Some(path) if !path.trim().is_empty() => sidecar.stop_workspace(path),
        _ => sidecar.stop_all(),
    }
}

#[tauri::command]
pub fn choose_project_folder() -> Result<Option<String>, String> {
    Ok(rfd::FileDialog::new()
        .set_title("Choose Somnia project folder")
        .pick_folder()
        .map(|path| path.display().to_string()))
}

#[tauri::command]
pub fn open_workspace_root(path: String) -> Result<(), String> {
    let target = PathBuf::from(path.trim());
    if !target.exists() {
        return Err(format!(
            "Path '{}' does not exist.",
            target.display()
        ));
    }

    #[cfg(target_os = "windows")]
    {
        let mut command = Command::new("explorer");
        if target.is_file() {
            command.arg("/select,").arg(&target);
        } else {
            command.arg(&target);
        }
        command
            .spawn()
            .map_err(|error| format!("Unable to open path '{}': {error}", target.display()))?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&target)
            .spawn()
            .map_err(|error| format!("Unable to open path '{}': {error}", target.display()))?;
    }

    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&target)
            .spawn()
            .map_err(|error| format!("Unable to open path '{}': {error}", target.display()))?;
    }

    Ok(())
}

pub fn shutdown_managed_sidecar(app: &tauri::AppHandle) {
    let sidecar = app.state::<ManagedSidecar>();
    let _ = sidecar.stop_all();
}

static ORPHAN_SWEEP: Once = Once::new();

/// Kill sidecar processes whose spawning app is already gone.
///
/// Runs once per app launch, before the first managed sidecar starts. The app
/// normally terminates its sidecars on exit, but a crash, force-kill, or
/// `tauri dev` restart skips that path; sweeping here clears the orphans those
/// leaks left behind. Sidecars with a living parent (supervisors, manual dev
/// runs) are never touched. Best-effort: failures are logged and ignored.
fn sweep_orphan_sidecars() {
    if let Err(error) = try_sweep_orphan_sidecars() {
        eprintln!("orphan sidecar sweep failed: {error}");
    }
}

fn try_sweep_orphan_sidecars() -> Result<(), String> {
    let processes = list_processes()?;
    let alive: std::collections::HashSet<u32> = processes.iter().map(|entry| entry.pid).collect();
    let self_pid = std::process::id();
    for entry in processes.iter().filter(|entry| {
        entry.pid != self_pid
            && !alive.contains(&entry.parent_pid)
            && entry
                .command_line
                .as_deref()
                .map(|command| {
                    let command = command.to_ascii_lowercase();
                    command.contains("desktop.backend.bootstrap") || command.contains("somnia-sidecar")
                })
                .unwrap_or(false)
    }) {
        kill_process_tree(entry.pid);
    }
    Ok(())
}

struct ProcessEntry {
    pid: u32,
    parent_pid: u32,
    command_line: Option<String>,
}

#[cfg(target_os = "windows")]
fn list_processes() -> Result<Vec<ProcessEntry>, String> {
    #[derive(Deserialize)]
    #[serde(rename_all = "PascalCase")]
    struct Win32Process {
        process_id: u32,
        parent_process_id: u32,
        command_line: Option<String>,
    }

    let output = Command::new("powershell")
        .args([
            "-NoProfile",
            "-Command",
            "Get-CimInstance Win32_Process | Select-Object ProcessId,ParentProcessId,CommandLine | ConvertTo-Json -Compress",
        ])
        .creation_flags(CREATE_NO_WINDOW)
        .output()
        .map_err(|error| format!("unable to enumerate processes: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        return Ok(Vec::new());
    }
    // ConvertTo-Json emits a bare object instead of an array for a single result.
    let entries: Vec<Win32Process> = if trimmed.starts_with('[') {
        serde_json::from_str(trimmed)
    } else {
        serde_json::from_str::<Win32Process>(trimmed).map(|single| vec![single])
    }
    .map_err(|error| format!("unable to parse the process list: {error}"))?;
    Ok(entries
        .into_iter()
        .map(|entry| ProcessEntry {
            pid: entry.process_id,
            parent_pid: entry.parent_process_id,
            command_line: entry.command_line,
        })
        .collect())
}

#[cfg(not(target_os = "windows"))]
fn list_processes() -> Result<Vec<ProcessEntry>, String> {
    let output = Command::new("ps")
        .args(["-eo", "pid=,ppid=,args="])
        .output()
        .map_err(|error| format!("unable to enumerate processes: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    Ok(stdout
        .lines()
        .filter_map(|line| {
            let mut parts = line.trim_start().splitn(3, char::is_whitespace);
            let pid = parts.next()?.trim().parse().ok()?;
            let parent_pid = parts.next()?.trim().parse().ok()?;
            let command = parts.next().map(str::trim).filter(|value| !value.is_empty());
            Some(ProcessEntry {
                pid,
                parent_pid,
                command_line: command.map(str::to_string),
            })
        })
        .collect())
}

#[cfg(target_os = "windows")]
fn kill_process_tree(pid: u32) {
    let _ = Command::new("taskkill")
        .args(["/PID", &pid.to_string(), "/T", "/F"])
        .creation_flags(CREATE_NO_WINDOW)
        .output();
}

#[cfg(not(target_os = "windows"))]
fn kill_process_tree(pid: u32) {
    let _ = Command::new("kill").args(["-TERM", &pid.to_string()]).output();
}
