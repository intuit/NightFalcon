use std::env;
use std::collections::HashSet;
use std::fs;
use std::path::{Path, PathBuf};

fn collect(root: &Path, current: &Path, files: &mut Vec<PathBuf>) {
    let mut entries: Vec<_> = fs::read_dir(current).expect("read payload directory")
        .map(|entry| entry.expect("read payload entry").path()).collect();
    entries.sort();
    for path in entries {
        let metadata = fs::symlink_metadata(&path).expect("read payload metadata");
        assert!(!metadata.file_type().is_symlink(), "payload symlinks are forbidden");
        if metadata.is_dir() {
            collect(root, &path, files);
        } else if metadata.is_file() {
            files.push(path.strip_prefix(root).expect("relative payload path").to_path_buf());
        } else {
            panic!("payload must contain regular files");
        }
    }
}

fn main() {
    let root = PathBuf::from(env::var("CARGO_MANIFEST_DIR").expect("manifest root"));
    let executable_manifest = "packaging/runtime-executables.txt";
    let executables: HashSet<String> = fs::read_to_string(root.join(executable_manifest))
        .expect("read runtime executable inventory")
        .lines().map(str::to_owned).collect();
    let mut files = Vec::new();
    for relative in ["src/nightfalcon", "claude", "codex", "cursor"] {
        collect(&root, &root.join(relative), &mut files);
    }
    files.sort();
    let mut generated = String::from("static FILES: &[(&str, &[u8], bool)] = &[\n");
    for relative in files {
        let source = root.join(&relative);
        let name = relative.to_string_lossy().to_string();
        generated.push_str(&format!(
            "    ({:?}, include_bytes!({:?}), {}),\n",
            name, source.to_string_lossy(), executables.contains(&name)
        ));
    }
    generated.push_str("];\n");
    let output = PathBuf::from(env::var("OUT_DIR").expect("build output")).join("payload.rs");
    fs::write(output, generated).expect("write embedded payload index");
    println!("cargo:rerun-if-changed=src/nightfalcon");
    println!("cargo:rerun-if-changed=claude");
    println!("cargo:rerun-if-changed=codex");
    println!("cargo:rerun-if-changed=cursor");
    println!("cargo:rerun-if-changed={executable_manifest}");
}
