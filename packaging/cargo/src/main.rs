use std::env;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};
use std::process::{Command, ExitCode, Stdio};
use std::time::{SystemTime, UNIX_EPOCH};

#[cfg(unix)]
use std::os::unix::fs::{DirBuilderExt, PermissionsExt};

include!(concat!(env!("OUT_DIR"), "/payload.rs"));

fn python() -> Option<String> {
    let mut candidates = Vec::new();
    if let Ok(value) = env::var("NIGHTFALCON_PYTHON") { candidates.push(value); }
    candidates.extend(["python3".to_string(), "python".to_string()]);
    candidates.into_iter().find(|candidate| {
        Command::new(candidate)
            .args(["-c", "import sys; raise SystemExit(sys.version_info < (3, 11))"])
            .stdout(Stdio::null()).stderr(Stdio::null()).status()
            .map(|status| status.success()).unwrap_or(false)
    })
}

fn create_private_directory(path: &Path) -> io::Result<()> {
    #[cfg(unix)]
    {
        let mut builder = fs::DirBuilder::new();
        builder.mode(0o700);
        builder.create(path)
    }
    #[cfg(not(unix))]
    {
        fs::create_dir(path)
    }
}

fn create_private_temp_root() -> io::Result<PathBuf> {
    let nonce = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    for counter in 0..128u16 {
        let candidate = env::temp_dir().join(format!(
            "nightfalcon-{}-{nonce:x}-{counter}",
            std::process::id()
        ));
        match create_private_directory(&candidate) {
            Ok(()) => return Ok(candidate),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(error) => return Err(error),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "could not create unique NightFalcon temporary directory",
    ))
}

fn extract_payload(root: &Path) -> io::Result<()> {
    for (relative, content, is_executable) in FILES {
        let target = root.join(relative);
        if let Some(parent) = target.parent() {
            fs::create_dir_all(parent)?;
        }
        fs::write(&target, content)?;
        #[cfg(unix)]
        fs::set_permissions(
            &target,
            fs::Permissions::from_mode(if *is_executable { 0o700 } else { 0o600 }),
        )?;
    }
    Ok(())
}

fn main() -> ExitCode {
    let Some(python) = python() else {
        eprintln!("nightfalcon: Python 3.11 or later is required");
        return ExitCode::FAILURE;
    };
    let root = match create_private_temp_root() {
        Ok(path) => path,
        Err(error) => {
            eprintln!("nightfalcon: cannot create temporary directory: {error}");
            return ExitCode::FAILURE;
        }
    };
    if let Err(error) = extract_payload(&root) {
        let _ = fs::remove_dir_all(&root);
        eprintln!("nightfalcon: cannot extract payload: {error}");
        return ExitCode::FAILURE;
    }
    let mut paths = vec![root.join("src").to_string_lossy().to_string()];
    if let Some(existing) = env::var_os("PYTHONPATH") { paths.push(existing.to_string_lossy().to_string()); }
    let python_path = match env::join_paths(paths) {
        Ok(value) => value,
        Err(error) => {
            let _ = fs::remove_dir_all(&root);
            eprintln!("nightfalcon: invalid PYTHONPATH: {error}");
            return ExitCode::FAILURE;
        }
    };
    let status = Command::new(python).args(["-m", "nightfalcon"])
        .args(env::args().skip(1)).current_dir(&root)
        .env("PYTHONPATH", python_path)
        .stdin(Stdio::inherit()).stdout(Stdio::inherit()).stderr(Stdio::inherit()).status();
    let _ = fs::remove_dir_all(&root);
    match status { Ok(value) => ExitCode::from(value.code().unwrap_or(1) as u8), Err(error) => { eprintln!("nightfalcon: {error}"); ExitCode::FAILURE } }
}
