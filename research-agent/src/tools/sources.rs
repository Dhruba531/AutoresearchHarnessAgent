//! The run's source registry. Every search hit, fetched page and corpus document
//! gets a short id (`S12`) that agents use in their findings. Full page text is
//! kept on disk, so the citation agent can check claims against the sources
//! themselves rather than against a summary of a summary.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::PathBuf;
use std::sync::Mutex;

use crate::util::{atomic_write, clip};

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Source {
    pub id: String,
    pub url: String,
    pub title: String,
    pub snippet: String,
    /// web_search | web_fetch | corpus | server_search | reported
    pub origin: String,
    pub published: Option<String>,
    /// Characters of full text stored under `sources/<id>.txt`.
    pub content_chars: usize,
    /// Agents that saw this source.
    pub seen_by: Vec<String>,
}

#[derive(Default, Serialize, Deserialize)]
struct Inner {
    list: Vec<Source>,
    #[serde(skip)]
    by_url: HashMap<String, usize>,
}

pub struct SourceRegistry {
    inner: Mutex<Inner>,
    dir: PathBuf,
}

/// Normalise a URL for de-duplication: lower-case scheme and host, no fragment,
/// no tracking parameters, no trailing slash.
pub fn normalize_url(url: &str) -> String {
    match reqwest::Url::parse(url.trim()) {
        Ok(mut u) => {
            u.set_fragment(None);
            let kept: Vec<(String, String)> = u
                .query_pairs()
                .filter(|(k, _)| !k.starts_with("utm_") && k != "fbclid" && k != "gclid")
                .map(|(k, v)| (k.into_owned(), v.into_owned()))
                .collect();
            if kept.is_empty() {
                u.set_query(None);
            } else {
                u.query_pairs_mut().clear().extend_pairs(kept);
            }
            let s = u.to_string();
            s.strip_suffix('/').map(str::to_string).unwrap_or(s)
        }
        Err(_) => url.trim().trim_end_matches('/').to_string(),
    }
}

impl SourceRegistry {
    /// Open (or create) the registry stored in `dir` (`sources.json` + `sources/`).
    pub fn open(dir: PathBuf) -> SourceRegistry {
        let mut inner: Inner = std::fs::read(dir.join("sources.json"))
            .ok()
            .and_then(|b| serde_json::from_slice(&b).ok())
            .unwrap_or_default();
        inner.by_url = inner
            .list
            .iter()
            .enumerate()
            .map(|(i, s)| (normalize_url(&s.url), i))
            .collect();
        SourceRegistry {
            inner: Mutex::new(inner),
            dir,
        }
    }

    /// Register a source (or update what we know about it) and return its id.
    pub fn upsert(
        &self,
        url: &str,
        title: &str,
        snippet: &str,
        origin: &str,
        agent: &str,
    ) -> String {
        let key = normalize_url(url);
        let mut g = self.inner.lock().unwrap();
        let idx = match g.by_url.get(&key) {
            Some(&i) => i,
            None => {
                let id = format!("S{}", g.list.len() + 1);
                g.list.push(Source {
                    id,
                    url: url.trim().to_string(),
                    title: String::new(),
                    snippet: String::new(),
                    origin: origin.to_string(),
                    published: None,
                    content_chars: 0,
                    seen_by: Vec::new(),
                });
                let i = g.list.len() - 1;
                g.by_url.insert(key, i);
                i
            }
        };
        let s = &mut g.list[idx];
        if s.title.is_empty() && !title.trim().is_empty() {
            s.title = clip(title.trim(), 200);
        }
        if s.snippet.len() < 40 && !snippet.trim().is_empty() {
            s.snippet = clip(snippet.trim(), 600);
        }
        if !agent.is_empty() && !s.seen_by.iter().any(|a| a == agent) {
            s.seen_by.push(agent.to_string());
        }
        s.id.clone()
    }

    pub fn set_published(&self, id: &str, published: &str) {
        let mut g = self.inner.lock().unwrap();
        if let Some(s) = g.list.iter_mut().find(|s| s.id == id) {
            if s.published.is_none() && !published.is_empty() {
                s.published = Some(published.to_string());
            }
        }
    }

    fn content_path(&self, id: &str) -> PathBuf {
        self.dir.join("sources").join(format!("{id}.txt"))
    }

    /// Store a source's full text.
    pub fn attach_content(&self, id: &str, text: &str) {
        if atomic_write(&self.content_path(id), text.as_bytes()).is_ok() {
            let mut g = self.inner.lock().unwrap();
            if let Some(s) = g.list.iter_mut().find(|s| s.id == id) {
                s.content_chars = text.chars().count();
            }
        }
    }

    pub fn content(&self, id: &str) -> Option<String> {
        std::fs::read_to_string(self.content_path(id)).ok()
    }

    pub fn get(&self, id: &str) -> Option<Source> {
        let id = id.trim().trim_matches(|c| c == '[' || c == ']');
        self.inner
            .lock()
            .unwrap()
            .list
            .iter()
            .find(|s| s.id.eq_ignore_ascii_case(id))
            .cloned()
    }

    pub fn by_url(&self, url: &str) -> Option<Source> {
        let g = self.inner.lock().unwrap();
        g.by_url
            .get(&normalize_url(url))
            .map(|&i| g.list[i].clone())
    }

    /// Resolve what an agent reported as a source: an id (`S3`) or a URL.
    pub fn resolve(&self, reference: &str, agent: &str) -> Option<String> {
        let r = reference.trim();
        if let Some(s) = self.get(r) {
            return Some(s.id);
        }
        if r.starts_with("http://") || r.starts_with("https://") || r.starts_with("file://") {
            return Some(
                self.by_url(r)
                    .map(|s| s.id)
                    .unwrap_or_else(|| self.upsert(r, "", "", "reported", agent)),
            );
        }
        None
    }

    pub fn all(&self) -> Vec<Source> {
        self.inner.lock().unwrap().list.clone()
    }

    pub fn len(&self) -> usize {
        self.inner.lock().unwrap().list.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }

    pub fn save(&self) -> std::io::Result<()> {
        let bytes = {
            let g = self.inner.lock().unwrap();
            serde_json::to_vec_pretty(&*g).unwrap_or_default()
        };
        atomic_write(&self.dir.join("sources.json"), &bytes)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dedupes_and_persists() {
        let dir = tempfile::tempdir().unwrap();
        let r = SourceRegistry::open(dir.path().to_path_buf());
        let a = r.upsert(
            "https://Example.com/a/?utm_source=x#top",
            "A",
            "snip",
            "web_search",
            "sub-1",
        );
        let b = r.upsert("https://example.com/a", "", "", "web_fetch", "sub-2");
        assert_eq!(a, b);
        assert_eq!(r.get("[S1]").unwrap().seen_by, vec!["sub-1", "sub-2"]);
        r.attach_content(&a, "full text");
        r.save().unwrap();
        let again = SourceRegistry::open(dir.path().to_path_buf());
        assert_eq!(again.content("S1").as_deref(), Some("full text"));
        assert_eq!(
            again.resolve("https://example.com/a/", "x").as_deref(),
            Some("S1")
        );
        assert_eq!(again.resolve("https://new.org", "x").as_deref(), Some("S2"));
        assert_eq!(again.resolve("nonsense", "x"), None);
    }
}
