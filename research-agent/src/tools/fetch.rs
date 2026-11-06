//! `web_fetch`: read a page in full. Text is cached per source, so parallel
//! subagents that land on the same page fetch it once, and stored on disk for
//! the citation pass.

use async_trait::async_trait;
use futures::StreamExt;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use super::html;
use super::sources::SourceRegistry;
use super::{int_arg, str_arg, Tool, ToolOutput};
use crate::util::char_offset;

const MAX_BYTES: usize = 4 * 1024 * 1024;
const DEFAULT_CHARS: i64 = 8000;

/// Where pages come from: the network, or a fixed map (tests, offline demos).
pub enum Fetcher {
    Http(reqwest::Client),
    /// url → (title, body text)
    Static(HashMap<String, (String, String)>),
}

pub struct Page {
    pub title: String,
    pub text: String,
}

impl Fetcher {
    pub async fn get(&self, url: &str) -> Result<Page, String> {
        match self {
            Fetcher::Static(map) => {
                let key = super::sources::normalize_url(url);
                map.iter()
                    .find(|(u, _)| super::sources::normalize_url(u) == key)
                    .map(|(_, (t, b))| Page {
                        title: t.clone(),
                        text: b.clone(),
                    })
                    .ok_or_else(|| format!("HTTP 404 for {url}"))
            }
            Fetcher::Http(http) => fetch_http(http, url).await,
        }
    }
}

async fn fetch_http(http: &reqwest::Client, url: &str) -> Result<Page, String> {
    let parsed = reqwest::Url::parse(url).map_err(|_| format!("not a valid URL: {url}"))?;
    if !matches!(parsed.scheme(), "http" | "https") {
        return Err("only http(s) URLs can be fetched".into());
    }
    let resp = http
        .get(parsed)
        .header("User-Agent", "Mozilla/5.0 (compatible; research-agent/0.1)")
        .header(
            "Accept",
            "text/html,application/xhtml+xml,text/plain,application/json;q=0.9,*/*;q=0.5",
        )
        .timeout(Duration::from_secs(30))
        .send()
        .await
        .map_err(|e| format!("could not fetch {url}: {e}"))?;
    if !resp.status().is_success() {
        return Err(format!("HTTP {} for {url}", resp.status()));
    }
    let ctype = resp
        .headers()
        .get("content-type")
        .and_then(|v| v.to_str().ok())
        .unwrap_or("")
        .to_ascii_lowercase();
    if ctype.contains("pdf") {
        return Err("this is a PDF, which web_fetch cannot read; look for an HTML version or an abstract page".into());
    }
    if ctype.starts_with("image/")
        || ctype.starts_with("video/")
        || ctype.starts_with("audio/")
        || ctype.contains("zip")
    {
        return Err(format!("unsupported content type {ctype}"));
    }
    let mut body = Vec::new();
    let mut stream = resp.bytes_stream();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|e| format!("read failed: {e}"))?;
        body.extend_from_slice(&chunk);
        if body.len() > MAX_BYTES {
            break;
        }
    }
    let raw = String::from_utf8_lossy(&body).into_owned();
    let is_html = ctype.contains("html") || (ctype.is_empty() && raw.trim_start().starts_with('<'));
    if is_html {
        Ok(Page {
            title: html::title(&raw).unwrap_or_default(),
            text: html::to_text(&raw),
        })
    } else {
        Ok(Page {
            title: String::new(),
            text: raw,
        })
    }
}

pub struct WebFetchTool {
    pub fetcher: Arc<Fetcher>,
    pub sources: Arc<SourceRegistry>,
}

#[async_trait]
impl Tool for WebFetchTool {
    fn name(&self) -> &str {
        "web_fetch"
    }

    fn description(&self) -> String {
        "Read the full text of a web page. Use it after web_search when a result looks relevant and you \
         need the specifics (numbers, dates, names, exact wording) that snippets leave out. Accepts a URL \
         or a source id such as S4. Long pages come in slices: pass `offset` to continue where the last \
         slice ended. Prefer primary sources (official sites, papers, filings, datasets) over aggregators."
            .into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["url"],
            "properties": {
                "url": {"type": "string", "description": "A URL, or a source id like S4."},
                "offset": {"type": "integer", "minimum": 0, "description": "Character offset to start from (default 0)."},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000, "description": "Slice length (default 8000)."}
            }
        })
    }

    async fn call(&self, agent: &str, input: Value) -> ToolOutput {
        let target = str_arg(&input, "url");
        let offset = int_arg(&input, "offset", 0, 0, i64::MAX) as usize;
        let max_chars = int_arg(&input, "max_chars", DEFAULT_CHARS, 500, 20_000) as usize;
        let (url, known) = match self.sources.get(&target) {
            Some(s) => (s.url.clone(), Some(s)),
            None => (target.clone(), self.sources.by_url(&target)),
        };
        if url.is_empty() {
            return ToolOutput::err("url is empty");
        }
        let cached = known
            .as_ref()
            .filter(|s| s.content_chars > 0)
            .and_then(|s| self.sources.content(&s.id));
        let (id, text) = match cached {
            Some(text) => (self.sources.upsert(&url, "", "", "web_fetch", agent), text),
            None => match self.fetcher.get(&url).await {
                Ok(page) => {
                    if page.text.trim().is_empty() {
                        return ToolOutput::err(format!(
                            "{url} returned no readable text (it may need JavaScript)"
                        ));
                    }
                    let snippet = crate::util::clip(&page.text, 300);
                    let id = self
                        .sources
                        .upsert(&url, &page.title, &snippet, "web_fetch", agent);
                    self.sources.attach_content(&id, &page.text);
                    let _ = self.sources.save();
                    (id, page.text)
                }
                Err(e) => return ToolOutput::err(format!("{e}. Try another source.")),
            },
        };
        let total = text.chars().count();
        if offset >= total && total > 0 {
            return ToolOutput::err(format!("offset {offset} is past the end ({total} chars)"));
        }
        let start = char_offset(&text, offset);
        let end = char_offset(&text, offset + max_chars);
        let title = self.sources.get(&id).map(|s| s.title).unwrap_or_default();
        let mut out = format!(
            "[{id}] {}\nURL: {url}\nCharacters {}–{} of {total}\n\n{}",
            if title.is_empty() {
                "(untitled)"
            } else {
                &title
            },
            offset,
            (offset + max_chars).min(total),
            &text[start..end]
        );
        if offset + max_chars < total {
            out.push_str(&format!(
                "\n\n[… {} more characters; call web_fetch with offset={} to continue]",
                total - offset - max_chars,
                offset + max_chars
            ));
        }
        ToolOutput::ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn fetches_slices_and_caches() {
        let dir = tempfile::tempdir().unwrap();
        let sources = Arc::new(SourceRegistry::open(dir.path().to_path_buf()));
        let mut pages = HashMap::new();
        pages.insert(
            "https://a.org/x".to_string(),
            ("A page".to_string(), "abcdefghij".repeat(100)),
        );
        let tool = WebFetchTool {
            fetcher: Arc::new(Fetcher::Static(pages)),
            sources: sources.clone(),
        };
        let out = tool
            .call("sub-1", json!({"url": "https://a.org/x", "max_chars": 500}))
            .await;
        assert!(!out.is_error, "{}", out.content);
        assert!(out.content.starts_with("[S1] A page"));
        assert!(out.content.contains("offset=500"));
        let out = tool
            .call(
                "sub-2",
                json!({"url": "S1", "offset": 900, "max_chars": 500}),
            )
            .await;
        assert!(out.content.contains("Characters 900–1000 of 1000"));
        assert_eq!(sources.get("S1").unwrap().seen_by, vec!["sub-1", "sub-2"]);
        let missing = tool.call("sub-1", json!({"url": "https://nope.org"})).await;
        assert!(missing.is_error);
    }
}
