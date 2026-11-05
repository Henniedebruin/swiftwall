mod backend;
mod carousel;
mod collection;
mod config;
mod daemon;
mod path_serde;
mod protocol;
mod runtime;
mod state;

fn main() {
    if let Err(error) = run() {
        eprintln!("swiftwall: {error}");
        std::process::exit(1);
    }
}
fn run() -> Result<(), String> {
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    if args.len() == 2 && args[0] == "request" {
        let request = args[1].to_str().ok_or("JSON request is not UTF-8")?;
        return daemon::json_client(&config::Locations::from_env()?, request);
    }
    if args.len() != 1 {
        return Err(
            "Usage: swiftwall <daemon|next|previous|pause|resume|status|rescan|reload|settings|check-config> or swiftwall request '<JSON>'".into(),
        );
    }
    let command = args[0].to_str().ok_or("Command is not UTF-8")?;
    if matches!(command, "--help" | "-h") {
        println!(
            "Usage: swiftwall <daemon|next|previous|pause|resume|status|rescan|reload|settings|check-config> or swiftwall request '<JSON>'"
        );
        return Ok(());
    }
    if command == "--version" {
        println!("swiftwall {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    if command == "check-config" {
        let settings = config::Config::load(&config::config_path_from_env()?)?;
        println!(
            "{}",
            serde_json::to_string_pretty(&settings).map_err(|e| e.to_string())?
        );
        return Ok(());
    }
    let locations = config::Locations::from_env()?;
    if command == "settings" {
        daemon::json_client(&locations, r#"{"v":1,"op":"getsettings"}"#)
    } else if command == "daemon" {
        daemon::run(locations)
    } else {
        daemon::client(&locations, command)
    }
}
