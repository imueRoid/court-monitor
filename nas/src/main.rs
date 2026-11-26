mod releases;
mod supervise;
mod web;

use anyhow::Result;
use std::{env, path::PathBuf};

#[tokio::main]
async fn main() -> Result<()> {
    let mut args = env::args().skip(1);
    if args.next().as_deref() == Some("--serve") {
        let run_dir = PathBuf::from(args.next().expect("missing run directory"));
        let options = supervise::Listen::from_args(args)?;
        if web::run(&run_dir, options).await? {
            std::process::exit(supervise::RESTART);
        }
    } else {
        let options = supervise::Listen::from_args(env::args().skip(1))?;
        supervise::run(options).await?;
    }
    Ok(())
}
