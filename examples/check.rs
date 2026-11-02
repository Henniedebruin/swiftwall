//! Read-only deployment check using the same parser and validators as the daemon.
//! Usage: cargo run --release --example check -- /absolute/config.toml
#[path = "../src/collection.rs"]
mod collection;
// Runtime locations are not needed by this read-only deployment tool.
#[allow(dead_code)]
#[path = "../src/config.rs"]
mod config;

use serde_json::json;
use std::collections::BTreeMap;
use std::path::PathBuf;

fn main() -> Result<(), String> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() != 1 {
        return Err("Usage: check /absolute/path/to/config.toml".into());
    }
    let config = config::Config::load(&PathBuf::from(&args[0]))?;
    let mut aliases: BTreeMap<PathBuf, Vec<PathBuf>> = BTreeMap::new();
    let mut candidates = 0;
    for root in &config.folders {
        for entry in std::fs::read_dir(root).map_err(|e| e.to_string())? {
            let path = entry.map_err(|e| e.to_string())?.path();
            if path.is_file()
                && path.extension().and_then(|e| e.to_str()).is_some_and(|e| {
                    ["jpg", "jpeg", "png", "webp"]
                        .iter()
                        .any(|allowed| e.eq_ignore_ascii_case(allowed))
                })
            {
                candidates += 1;
                let canonical = path.canonicalize().map_err(|e| e.to_string())?;
                aliases.entry(canonical).or_default().push(path);
            }
        }
    }
    let discovered = collection::scan(&config.folders);
    let mut eligible = Vec::new();
    let mut rejected = Vec::new();
    for path in &discovered.paths {
        match collection::validate(path) {
            Ok(_) => eligible.push(path),
            Err(reason) => rejected.push(json!({"path":path,"reason":reason})),
        }
    }
    let duplicates: Vec<_> = aliases
        .iter()
        .filter(|(_, paths)| paths.len() > 1)
        .map(|(canonical, paths)| json!({"canonical":canonical,"aliases":paths}))
        .collect();
    println!(
        "{}",
        serde_json::to_string_pretty(&json!({
            "config":config,
            "candidate_count":candidates,
            "canonical_unique_candidates":discovered.paths.len(),
            "canonical_duplicate_count":candidates - discovered.paths.len(),
            "duplicate_groups":duplicates,
            "eligible_count":eligible.len(),
            "eligible_paths":eligible,
            "rejected_count":rejected.len(),
            "rejected":rejected,
            "discovery_warnings":discovered.warnings,
        }))
        .map_err(|e| e.to_string())?
    );
    if eligible.is_empty() {
        return Err("No eligible images; do not activate".into());
    }
    Ok(())
}
