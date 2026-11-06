//! Web search behind one interface, with several backends:
//!
//! * `tavily`, `brave`, `searxng`: search APIs (keys or a self-hosted SearXNG URL).
//! * `duckduckgo`: the keyless HTML endpoint, a best-effort last resort.
//! * `anthropic`: Claude's server-side web search and fetch; agents get the
//!   provider's tools instead of these, and sources are harvested from the results.
//! * `static`: canned results for tests and offline demos.

use async_trait::async_trait;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use super::html::strip_tags;
use super::sources::SourceRegistry;
use super::{int_arg, str_arg, Tool, ToolOutput};
use crate::config::Config;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct SearchHit {
    pub title: String,
    pub url: String,
    pub snippet: String,
    #[serde(default)]
    pub published: Option<String>,
}

pub enum SearchBackend {
    Tavily {
        key: String,
    },
    Brave {
        key: String,
    },
    Searxng {
        url: String,
    },
    DuckDuckGo,
    /// Server-side search run by the model provider (Claude's web_search tool).
    Anthropic,
    Static(Vec<SearchHit>),
    None,
}

impl SearchBackend {
    /// Pick a backend: an explicit choice, else the first configured API, else
    /// Claude's server-side search when the subagents run on Claude, else DuckDuckGo.
    pub fn from_config(cfg: &Config, subagents_on_claude: bool) -> anyhow::Result<SearchBackend> {
        let s = &cfg.search;
        let need = |v: &Option<String>, what: &str| {
            v.clone()
                .ok_or_else(|| anyhow::anyhow!("search backend needs {what}"))
        };
        Ok(match s.backend.to_ascii_lowercase().as_str() {
            "tavily" => SearchBackend::Tavily {
                key: need(&s.tavily_key, "TAVILY_API_KEY")?,
            },
            "brave" => SearchBackend::Brave {
                key: need(&s.brave_key, "BRAVE_API_KEY")?,
            },
            "searxng" => SearchBackend::Searxng {
                url: need(&s.searxng_url, "SEARXNG_URL")?,
            },
            "duckduckgo" | "ddg" => SearchBackend::DuckDuckGo,
            "anthropic" | "claude" | "server" => {
                if !subagents_on_claude {
                    anyhow::bail!(
                        "the anthropic search backend needs the subagents to run on Claude"
                    );
                }
                SearchBackend::Anthropic
            }
            "none" | "off" => SearchBackend::None,
            _ => {
                if let Some(k) = &s.tavily_key {
                    SearchBackend::Tavily { key: k.clone() }
                } else if let Some(k) = &s.brave_key {
                    SearchBackend::Brave { key: k.clone() }
                } else if let Some(u) = &s.searxng_url {
                    SearchBackend::Searxng { url: u.clone() }
                } else if subagents_on_claude {
                    SearchBackend::Anthropic
                } else {
                    SearchBackend::DuckDuckGo
                }
            }
        })
    }

    pub fn label(&self) -> &'static str {
        match self {
            SearchBackend::Tavily { .. } => "tavily",
            SearchBackend::Brave { .. } => "brave",
            SearchBackend::Searxng { .. } => "searxng",
            SearchBackend::DuckDuckGo => "duckduckgo",
            SearchBackend::Anthropic => "anthropic (server-side)",
            SearchBackend::Static(_) => "static",
            SearchBackend::None => "none",
        }
    }

    /// True when searching happens through client tools defined here.
    pub fn is_client_side(&self) -> bool {
        !matches!(self, SearchBackend::Anthropic | SearchBackend::None)
    }

    pub async fn search(
        &self,
        http: &reqwest::Client,
        query: &str,
        n: usize,
    ) -> Result<Vec<SearchHit>, String> {
        let timeout = Duration::from_secs(30);
        let net = |e: reqwest::Error| format!("search request failed: {e}");
        let status = |r: &reqwest::Response| {
            if r.status().is_success() {
                Ok(())
            } else {
                Err(format!("search backend returned HTTP {}", r.status()))
            }
        };
        match self {
            SearchBackend::Tavily { key } => {
                let r = http
                    .post("https://api.tavily.com/search")
                    .bearer_auth(key)
                    .timeout(timeout)
                    .json(&json!({"query": query, "max_results": n, "search_depth": "basic"}))
                    .send()
                    .await
                    .map_err(net)?;
                status(&r)?;
                let v: Value = r.json().await.map_err(net)?;
                Ok(v["results"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .map(|x| SearchHit {
                        title: x["title"].as_str().unwrap_or("").into(),
                        url: x["url"].as_str().unwrap_or("").into(),
                        snippet: x["content"].as_str().unwrap_or("").into(),
                        published: x["published_date"].as_str().map(str::to_string),
                    })
                    .collect())
            }
            SearchBackend::Brave { key } => {
                let r = http
                    .get("https://api.search.brave.com/res/v1/web/search")
                    .query(&[("q", query), ("count", &n.to_string())])
                    .header("X-Subscription-Token", key)
                    .header("Accept", "application/json")
                    .timeout(timeout)
                    .send()
                    .await
                    .map_err(net)?;
                status(&r)?;
                let v: Value = r.json().await.map_err(net)?;
                Ok(v.pointer("/web/results")
                    .and_then(Value::as_array)
                    .into_iter()
                    .flatten()
                    .map(|x| SearchHit {
                        title: strip_tags(x["title"].as_str().unwrap_or("")),
                        url: x["url"].as_str().unwrap_or("").into(),
                        snippet: strip_tags(x["description"].as_str().unwrap_or("")),
                        published: x["age"]
                            .as_str()
                            .or(x["page_age"].as_str())
                            .map(str::to_string),
                    })
                    .collect())
            }
            SearchBackend::Searxng { url } => {
                let r = http
                    .get(format!("{}/search", url.trim_end_matches('/')))
                    .query(&[("q", query), ("format", "json")])
                    .timeout(timeout)
                    .send()
                    .await
                    .map_err(net)?;
                status(&r)?;
                let v: Value = r.json().await.map_err(net)?;
                Ok(v["results"]
                    .as_array()
                    .into_iter()
                    .flatten()
                    .take(n)
                    .map(|x| SearchHit {
                        title: x["title"].as_str().unwrap_or("").into(),
                        url: x["url"].as_str().unwrap_or("").into(),
                        snippet: x["content"].as_str().unwrap_or("").into(),
                        published: x["publishedDate"].as_str().map(str::to_string),
                    })
                    .collect())
            }
            SearchBackend::DuckDuckGo => {
                let r = http
                    .post("https://html.duckduckgo.com/html/")
                    .form(&[("q", query)])
                    .header("User-Agent", "Mozilla/5.0 (compatible; research-agent/0.1)")
                    .timeout(timeout)
                    .send()
                    .await
                    .map_err(net)?;
                status(&r)?;
                let body = r.text().await.map_err(net)?;
                let hits = parse_duckduckgo(&body, n);
                if hits.is_empty() && body.contains("anomaly") {
                    return Err("DuckDuckGo blocked the automated query; configure TAVILY_API_KEY, BRAVE_API_KEY or SEARXNG_URL".into());
                }
                Ok(hits)
            }
            SearchBackend::Static(all) => {
                let q = crate::util::words(query);
                let mut scored: Vec<(usize, &SearchHit)> = all
                    .iter()
                    .map(|h| {
                        let text = crate::util::words(&format!("{} {}", h.title, h.snippet));
                        (q.iter().filter(|w| text.contains(w)).count(), h)
                    })
                    .filter(|(s, _)| *s > 0)
                    .collect();
                scored.sort_by_key(|s| std::cmp::Reverse(s.0));
                Ok(scored.into_iter().take(n).map(|(_, h)| h.clone()).collect())
            }
            SearchBackend::Anthropic => Err("search runs server-side for this backend".into()),
            SearchBackend::None => Err("web search is disabled for this run".into()),
        }
    }
}

/// Parse DuckDuckGo's HTML results page.
pub fn parse_duckduckgo(html: &str, n: usize) -> Vec<SearchHit> {
    let mut hits = Vec::new();
    let mut rest = html;
    while hits.len() < n {
        let Some(pos) = rest.find("class=\"result__a\"") else {
            break;
        };
        let tag_start = rest[..pos].rfind('<').unwrap_or(pos);
        let after = &rest[tag_start..];
        let href = after
            .find("href=\"")
            .map(|h| &after[h + 6..])
            .and_then(|s| s.find('"').map(|e| &s[..e]))
            .unwrap_or("");
        let title_start = after.find('>').map(|g| g + 1).unwrap_or(0);
        let title_end = after.find("</a>").unwrap_or(title_start);
        let title = strip_tags(&after[title_start..title_end.max(title_start)]);
        let mut snippet = String::new();
        let consumed = tag_start + title_end.max(title_start);
        let next_rest = &rest[consumed..];
        if let Some(sp) = next_rest.find("class=\"result__snippet\"") {
            let limit = next_rest.find("class=\"result__a\"").unwrap_or(usize::MAX);
            if sp < limit {
                let s = &next_rest[sp..];
                let a = s.find('>').map(|g| g + 1).unwrap_or(0);
                let b = s.find("</a>").or_else(|| s.find("</td>")).unwrap_or(a);
                snippet = strip_tags(&s[a..b.max(a)]);
            }
        }
        if let Some(url) = ddg_target(&crate::tools::html::decode_entities(href)) {
            if !url.contains("duckduckgo.com/y.js") {
                hits.push(SearchHit {
                    title,
                    url,
                    snippet,
                    published: None,
                });
            }
        }
        rest = &rest[consumed.max(pos + 1)..];
    }
    hits
}

fn ddg_target(href: &str) -> Option<String> {
    let full = if href.starts_with("//") {
        format!("https:{href}")
    } else {
        href.to_string()
    };
    let u = reqwest::Url::parse(&full).ok()?;
    if u.host_str().is_some_and(|h| h.ends_with("duckduckgo.com")) {
        return u
            .query_pairs()
            .find(|(k, _)| k == "uddg")
            .map(|(_, v)| v.into_owned());
    }
    Some(full)
}

/// `web_search`: run a query and register the hits as sources.
pub struct WebSearchTool {
    pub backend: Arc<SearchBackend>,
    pub http: reqwest::Client,
    pub sources: Arc<SourceRegistry>,
    history: Mutex<HashMap<String, Vec<String>>>,
}

impl WebSearchTool {
    pub fn new(
        backend: Arc<SearchBackend>,
        http: reqwest::Client,
        sources: Arc<SourceRegistry>,
    ) -> WebSearchTool {
        WebSearchTool {
            backend,
            http,
            sources,
            history: Mutex::new(HashMap::new()),
        }
    }
}

#[async_trait]
impl Tool for WebSearchTool {
    fn name(&self) -> &str {
        "web_search"
    }

    fn description(&self) -> String {
        "Search the public web. Use it for anything outside the local corpus: news, docs, papers, \
         companies, statistics. Start with short, broad queries (2-6 words) to see what exists, then \
         narrow. Returns titles, URLs and snippets with source ids like [S4]; snippets are often not \
         enough for specifics, so follow up with web_fetch on the best results. Several searches can \
         run in parallel: issue independent queries in the same turn."
            .into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["query"],
            "properties": {
                "query": {"type": "string", "description": "The search query. Short and specific beats long and exhaustive."},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Default 6."}
            }
        })
    }

    async fn call(&self, agent: &str, input: Value) -> ToolOutput {
        let query = str_arg(&input, "query");
        if query.is_empty() {
            return ToolOutput::err("query is empty");
        }
        let n = int_arg(&input, "max_results", 6, 1, 10) as usize;
        let repeated = {
            let mut h = self.history.lock().unwrap();
            let seen = h.entry(agent.to_string()).or_default();
            let norm = query.to_lowercase();
            let rep = seen.contains(&norm);
            seen.push(norm);
            rep
        };
        let hits = match self.backend.search(&self.http, &query, n).await {
            Ok(h) => h,
            Err(e) => {
                return ToolOutput::err(format!(
                    "{e}. Try a different query, or work with what you have."
                ))
            }
        };
        if hits.is_empty() {
            return ToolOutput::ok(format!(
                "No results for \"{query}\". Try broader or different words."
            ));
        }
        let mut out = String::new();
        if repeated {
            out.push_str("Note: you already ran this exact query; vary it or move on.\n");
        }
        out.push_str(&format!("Results for \"{query}\":\n"));
        for h in hits.iter().filter(|h| !h.url.is_empty()) {
            let id = self
                .sources
                .upsert(&h.url, &h.title, &h.snippet, "web_search", agent);
            if let Some(p) = &h.published {
                self.sources.set_published(&id, p);
            }
            out.push_str(&format!(
                "\n[{id}] {}\n    {}\n",
                if h.title.is_empty() {
                    "(untitled)"
                } else {
                    &h.title
                },
                h.url
            ));
            if let Some(p) = &h.published {
                out.push_str(&format!("    published: {p}\n"));
            }
            if !h.snippet.is_empty() {
                out.push_str(&format!("    {}\n", crate::util::clip(&h.snippet, 400)));
            }
        }
        let _ = self.sources.save();
        ToolOutput::ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_duckduckgo_results() {
        let html = r#"<div class="result"><h2><a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.rust-lang.org%2F&amp;rut=abc">Rust <b>Programming</b> Language</a></h2>
            <a class="result__snippet" href="x">A language empowering <b>everyone</b>.</a></div>
            <div class="result"><a rel="nofollow" class="result__a" href="https://doc.rust-lang.org/book/">The Book</a></div>"#;
        let hits = parse_duckduckgo(html, 5);
        assert_eq!(hits.len(), 2);
        assert_eq!(hits[0].url, "https://www.rust-lang.org/");
        assert_eq!(hits[0].title, "Rust Programming Language");
        assert_eq!(hits[0].snippet, "A language empowering everyone.");
        assert_eq!(hits[1].url, "https://doc.rust-lang.org/book/");
        assert_eq!(hits[1].snippet, "");
    }

    #[tokio::test]
    async fn static_backend_ranks_by_overlap() {
        let b = SearchBackend::Static(vec![
            SearchHit {
                title: "Chip shortage 2021".into(),
                url: "https://a".into(),
                snippet: "automotive".into(),
                published: None,
            },
            SearchHit {
                title: "Unrelated".into(),
                url: "https://b".into(),
                snippet: "cooking".into(),
                published: None,
            },
        ]);
        let hits = b
            .search(&reqwest::Client::new(), "automotive chip shortage", 5)
            .await
            .unwrap();
        assert_eq!(hits.len(), 1);
        assert_eq!(hits[0].url, "https://a");
    }
}
