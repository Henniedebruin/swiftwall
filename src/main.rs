mod backend;
mod carousel;
mod collection;
mod config;
mod daemon;
mod path_serde;
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
    if args.len() != 1 {
        return Err(
            "Usage: swiftwall <daemon|next|previous|pause|resume|status|rescan|reload>".into(),
        );
    }
    let command = args[0].to_str().ok_or("Command is not UTF-8")?;
    if matches!(command, "--help" | "-h") {
        println!("Usage: swiftwall <daemon|next|previous|pause|resume|status|rescan|reload>");
        return Ok(());
    }
    if command == "--version" {
        println!("swiftwall {}", env!("CARGO_PKG_VERSION"));
        return Ok(());
    }
    let locations = config::Locations::from_env()?;
    if command == "daemon" {
        daemon::run(locations)
    } else {
        daemon::client(&locations, command)
    }
}
