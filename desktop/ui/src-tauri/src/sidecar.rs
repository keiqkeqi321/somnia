use serde::Serialize;
use std::{
    collections::{hash_map::DefaultHasher, HashMap},
    fs::{self, OpenOptions},
    hash::{Hash, Hasher},
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{Mutex, MutexGuard},
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
        let _ = child.kill();
        let _ = child.wait();
    }
    state.connection = None;
    state.port = None;
    state.stdout_log_path = None;
    state.stderr_log_path = None;
    stopped
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
            "Workspace path '{}' does not exist.",
            target.display()
        ));
    }

    #[cfg(target_os = "windows")]
    {
        Command::new("explorer")
            .arg(&target)
            .spawn()
            .map_err(|error| format!("Unable to open workspace folder '{}': {error}", target.display()))?;
    }

    #[cfg(target_os = "macos")]
    {
        Command::new("open")
            .arg(&target)
            .spawn()
            .map_err(|error| format!("Unable to open workspace folder '{}': {error}", target.display()))?;
    }

    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        Command::new("xdg-open")
            .arg(&target)
            .spawn()
            .map_err(|error| format!("Unable to open workspace folder '{}': {error}", target.display()))?;
    }

    Ok(())
}

pub fn shutdown_managed_sidecar(app: &tauri::AppHandle) {
    let sidecar = app.state::<ManagedSidecar>();
    let _ = sidecar.stop_all();
}
