//! On-disk layout of a run, so runs survive crashes and can be resumed:
//!
//! ```text
//! research-data/runs/<run-id>/
//!   run.json            metadata (query, status, models, cost)
//!   state.json          checkpoint: phase, lead conversation, subagent results
//!   memory.json         the lead's memory (plan, progress)
//!   sources.json        source registry; sources/<id>.txt holds full page text
//!   subagents/<id>.md   each subagent's full output (+ .messages.json transcript)
//!   report.raw.md       the lead's report before citations
//!   report.md           the final report with citations and a source list
//!   citations.json      which claims were attributed to which sources
//!   trace.jsonl         every model and tool call
//! ```

use serde::de::DeserializeOwned;
use serde::{Deserialize, Serialize};
use std::path::{Path, PathBuf};

use crate::util::{atomic_write, now_iso};

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct RunMeta {
    pub id: String,
    pub query: String,
    pub created_at: String,
    pub updated_at: String,
    /// queued | running | done | failed | interrupted
    pub status: String,
    pub lead_model: String,
    pub subagent_model: String,
    pub search_backend: String,
    pub cost_usd: f64,
    pub subagents: u64,
    pub sources_cited: usize,
    pub error: Option<String>,
    #[serde(default)]
    pub tags: Vec<String>,
}

#[derive(Clone, Debug)]
pub struct RunStore {
    pub dir: PathBuf,
}

/// Run ids are generated, but they also arrive from the CLI and HTTP API.
pub fn valid_id(id: &str) -> bool {
    !id.is_empty()
        && id.len() <= 64
        && id
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_')
}

impl RunStore {
    pub fn create(runs_dir: &Path, id: &str) -> std::io::Result<RunStore> {
        let dir = runs_dir.join(id);
        std::fs::create_dir_all(dir.join("subagents"))?;
        std::fs::create_dir_all(dir.join("sources"))?;
        Ok(RunStore { dir })
    }

    pub fn open(runs_dir: &Path, id: &str) -> std::io::Result<RunStore> {
        if !valid_id(id) {
            return Err(std::io::Error::new(
                std::io::ErrorKind::InvalidInput,
                "invalid run id",
            ));
        }
        let dir = runs_dir.join(id);
        if !dir.join("run.json").is_file() {
            return Err(std::io::Error::new(
                std::io::ErrorKind::NotFound,
                format!("no run {id} in {}", runs_dir.display()),
            ));
        }
        Ok(RunStore { dir })
    }

    pub fn path(&self, name: &str) -> PathBuf {
        self.dir.join(name)
    }

    pub fn write_json<T: Serialize>(&self, name: &str, v: &T) -> std::io::Result<()> {
        let bytes = serde_json::to_vec_pretty(v).map_err(std::io::Error::other)?;
        atomic_write(&self.path(name), &bytes)
    }

    pub fn read_json<T: DeserializeOwned>(&self, name: &str) -> std::io::Result<T> {
        let bytes = std::fs::read(self.path(name))?;
        serde_json::from_slice(&bytes).map_err(std::io::Error::other)
    }

    pub fn write_text(&self, name: &str, s: &str) -> std::io::Result<()> {
        atomic_write(&self.path(name), s.as_bytes())
    }

    pub fn read_text(&self, name: &str) -> Option<String> {
        std::fs::read_to_string(self.path(name)).ok()
    }

    pub fn meta(&self) -> std::io::Result<RunMeta> {
        self.read_json("run.json")
    }

    pub fn save_meta(&self, meta: &mut RunMeta) -> std::io::Result<()> {
        meta.updated_at = now_iso();
        self.write_json("run.json", meta)
    }

    /// The best available report: cited if the run finished, else the raw draft.
    pub fn report(&self) -> Option<String> {
        self.read_text("report.md")
            .or_else(|| self.read_text("report.raw.md"))
    }
}

/// All runs, newest first.
pub fn list_runs(runs_dir: &Path) -> Vec<RunMeta> {
    let mut out: Vec<RunMeta> = std::fs::read_dir(runs_dir)
        .into_iter()
        .flatten()
        .flatten()
        .filter_map(|e| std::fs::read(e.path().join("run.json")).ok())
        .filter_map(|b| serde_json::from_slice(&b).ok())
        .collect();
    out.sort_by(|a, b| b.created_at.cmp(&a.created_at).then(b.id.cmp(&a.id)));
    out
}
