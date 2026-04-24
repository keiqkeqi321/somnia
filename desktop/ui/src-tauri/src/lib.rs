mod sidecar;

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(sidecar::ManagedSidecar::default())
        .invoke_handler(tauri::generate_handler![
            sidecar::ensure_managed_sidecar,
            sidecar::stop_managed_sidecar
        ])
        .build(tauri::generate_context!())
        .expect("error while building Somnia desktop shell")
        .run(|app_handle, event| {
            if matches!(
                event,
                tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
            ) {
                sidecar::shutdown_managed_sidecar(app_handle);
            }
        });
}
