//! The lead researcher's external memory. The plan and progress notes live here
//! rather than only in the context window, so when the context is reset (it
//! grew too large, or the run was resumed) the lead picks up its plan again.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Mutex;

use super::{str_arg, Tool, ToolOutput};
use crate::util::{atomic_write, clip, now_iso};

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Entry {
    pub content: String,
    pub updated: String,
}

pub struct MemoryStore {
    path: PathBuf,
    entries: Mutex<BTreeMap<String, Entry>>,
}

impl MemoryStore {
    pub fn open(path: PathBuf) -> MemoryStore {
        let entries = std::fs::read(&path)
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        MemoryStore {
            path,
            entries: Mutex::new(entries),
        }
    }

    pub fn save(&self, key: &str, content: &str) {
        let bytes = {
            let mut g = self.entries.lock().unwrap();
            g.insert(
                key.to_string(),
                Entry {
                    content: content.to_string(),
                    updated: now_iso(),
                },
            );
            serde_json::to_vec_pretty(&*g).unwrap_or_default()
        };
        let _ = atomic_write(&self.path, &bytes);
    }

    pub fn get(&self, key: &str) -> Option<String> {
        self.entries
            .lock()
            .unwrap()
            .get(key)
            .map(|e| e.content.clone())
    }

    pub fn all(&self) -> BTreeMap<String, Entry> {
        self.entries.lock().unwrap().clone()
    }
}

pub struct MemorySaveTool(pub std::sync::Arc<MemoryStore>);

#[async_trait]
impl Tool for MemorySaveTool {
    fn name(&self) -> &str {
        "memory_save"
    }

    fn description(&self) -> String {
        "Save text to your persistent memory under a key, replacing what was there. Save your research \
         plan as key \"plan\" before delegating, and keep \"progress\" up to date after each round (what is \
         answered, what is open, what to do next). Memory survives context resets; your conversation may not."
            .into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["key", "content"],
            "properties": {
                "key": {"type": "string", "description": "e.g. plan, progress, open_questions"},
                "content": {"type": "string"}
            }
        })
    }

    async fn call(&self, _agent: &str, input: Value) -> ToolOutput {
        let key = str_arg(&input, "key");
        if key.is_empty() {
            return ToolOutput::err("key is empty");
        }
        self.0.save(&key, input["content"].as_str().unwrap_or(""));
        ToolOutput::ok(format!("Saved \"{key}\"."))
    }
}

pub struct MemoryReadTool(pub std::sync::Arc<MemoryStore>);

#[async_trait]
impl Tool for MemoryReadTool {
    fn name(&self) -> &str {
        "memory_read"
    }

    fn description(&self) -> String {
        "Read your persistent memory: one key, or omit the key to list every key with its first line.".into()
    }

    fn input_schema(&self) -> Value {
        json!({"type": "object", "additionalProperties": false, "properties": {"key": {"type": "string"}}})
    }

    async fn call(&self, _agent: &str, input: Value) -> ToolOutput {
        let key = str_arg(&input, "key");
        if key.is_empty() {
            let all = self.0.all();
            if all.is_empty() {
                return ToolOutput::ok("Memory is empty.");
            }
            let lines: Vec<String> = all
                .iter()
                .map(|(k, e)| {
                    format!(
                        "- {k} (updated {}): {}",
                        e.updated,
                        clip(e.content.lines().next().unwrap_or(""), 120)
                    )
                })
                .collect();
            return ToolOutput::ok(lines.join("\n"));
        }
        match self.0.get(&key) {
            Some(c) => ToolOutput::ok(c),
            None => ToolOutput::err(format!("no memory under \"{key}\"")),
        }
    }
}
