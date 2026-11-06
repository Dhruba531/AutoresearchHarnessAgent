//! A local document corpus (notes, docs, exports) searchable with BM25, for
//! context that exists only on your disk and not on the web. Point
//! `RESEARCH_CORPUS` (or `--corpus`) at a folder of .md/.txt/.html/... files.

use async_trait::async_trait;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;

use super::sources::SourceRegistry;
use super::{int_arg, str_arg, Tool, ToolOutput};
use crate::util::{char_offset, clip, words};

const EXTENSIONS: &[&str] = &[
    "md", "markdown", "txt", "text", "rst", "org", "html", "htm", "csv", "tsv", "json", "xml",
    "tex", "log",
];
const MAX_FILES: usize = 5000;
const MAX_FILE_BYTES: u64 = 2 * 1024 * 1024;
const CHUNK_CHARS: usize = 1200;
const STOPWORDS: &[&str] = &[
    "the", "and", "for", "are", "was", "were", "that", "this", "with", "from", "have", "has",
    "had", "not", "but", "you", "your", "its", "into", "than", "then", "they", "them", "their",
    "there", "what", "which", "when", "who", "how", "why", "can", "will", "would", "should",
    "could", "about", "of", "to", "in", "on", "at", "by", "is", "it", "be", "as", "or", "an", "if",
    "so", "do", "does", "did",
];

pub struct Doc {
    pub path: String,
    pub title: String,
    pub text: String,
}

struct Chunk {
    doc: usize,
    start: usize,
    terms: HashMap<String, u32>,
    len: usize,
}

pub struct Corpus {
    pub root: PathBuf,
    pub docs: Vec<Doc>,
    chunks: Vec<Chunk>,
    df: HashMap<String, usize>,
    avg_len: f64,
}

pub struct Hit {
    pub doc: usize,
    pub start: usize,
    pub score: f64,
}

fn terms(text: &str) -> Vec<String> {
    words(text)
        .into_iter()
        .filter(|w| !STOPWORDS.contains(&w.as_str()))
        .collect()
}

fn collect(dir: &Path, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };
    let mut entries: Vec<_> = entries.flatten().map(|e| e.path()).collect();
    entries.sort();
    for p in entries {
        if out.len() >= MAX_FILES {
            return;
        }
        let name = p.file_name().and_then(|n| n.to_str()).unwrap_or("");
        if name.starts_with('.') || name == "node_modules" || name == "target" {
            continue;
        }
        if p.is_dir() {
            collect(&p, out);
        } else if p
            .extension()
            .and_then(|e| e.to_str())
            .is_some_and(|e| EXTENSIONS.contains(&e.to_ascii_lowercase().as_str()))
            && p.metadata()
                .map(|m| m.len() <= MAX_FILE_BYTES)
                .unwrap_or(false)
        {
            out.push(p);
        }
    }
}

impl Corpus {
    pub fn load(root: &Path) -> anyhow::Result<Corpus> {
        if !root.is_dir() {
            anyhow::bail!("corpus folder {} does not exist", root.display());
        }
        let mut files = Vec::new();
        collect(root, &mut files);
        let mut docs = Vec::new();
        for f in files {
            let Ok(raw) = std::fs::read_to_string(&f) else {
                continue;
            };
            let is_html = f
                .extension()
                .and_then(|e| e.to_str())
                .is_some_and(|e| e.starts_with("htm"));
            let text = if is_html {
                super::html::to_text(&raw)
            } else {
                raw.clone()
            };
            let rel = f
                .strip_prefix(root)
                .unwrap_or(&f)
                .to_string_lossy()
                .replace('\\', "/");
            let title = if is_html {
                super::html::title(&raw)
            } else {
                None
            }
            .or_else(|| {
                text.lines()
                    .find(|l| l.starts_with('#'))
                    .map(|l| l.trim_start_matches('#').trim().to_string())
            })
            .filter(|t| !t.is_empty())
            .unwrap_or_else(|| rel.clone());
            docs.push(Doc {
                path: rel,
                title,
                text,
            });
        }
        Ok(Corpus::from_docs(root.to_path_buf(), docs))
    }

    pub fn from_docs(root: PathBuf, docs: Vec<Doc>) -> Corpus {
        let mut chunks = Vec::new();
        let mut df: HashMap<String, usize> = HashMap::new();
        for (di, d) in docs.iter().enumerate() {
            let total = d.text.chars().count();
            let mut start = 0;
            while start < total.max(1) {
                let a = char_offset(&d.text, start);
                let b = char_offset(&d.text, start + CHUNK_CHARS);
                // Title words count for every chunk of the document.
                let t = terms(&format!("{} {}", d.title, &d.text[a..b]));
                let mut tf: HashMap<String, u32> = HashMap::new();
                for w in &t {
                    *tf.entry(w.clone()).or_default() += 1;
                }
                for w in tf.keys() {
                    *df.entry(w.clone()).or_default() += 1;
                }
                chunks.push(Chunk {
                    doc: di,
                    start,
                    len: t.len(),
                    terms: tf,
                });
                start += CHUNK_CHARS * 5 / 6; // overlap a little so sentences are not cut off
            }
        }
        let avg_len = if chunks.is_empty() {
            1.0
        } else {
            chunks.iter().map(|c| c.len).sum::<usize>() as f64 / chunks.len() as f64
        };
        Corpus {
            root,
            docs,
            chunks,
            df,
            avg_len,
        }
    }

    /// Best chunk per document, highest BM25 score first.
    pub fn search(&self, query: &str, n: usize) -> Vec<Hit> {
        let q = terms(query);
        let total = self.chunks.len() as f64;
        let (k1, b) = (1.2, 0.75);
        let mut best: HashMap<usize, Hit> = HashMap::new();
        for c in &self.chunks {
            let mut score = 0.0;
            for w in &q {
                let Some(&tf) = c.terms.get(w) else { continue };
                let df = *self.df.get(w).unwrap_or(&0) as f64;
                let idf = ((total - df + 0.5) / (df + 0.5) + 1.0).ln();
                let tf = tf as f64;
                score +=
                    idf * tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * c.len as f64 / self.avg_len));
            }
            if score > 0.0 && best.get(&c.doc).is_none_or(|h| h.score < score) {
                best.insert(
                    c.doc,
                    Hit {
                        doc: c.doc,
                        start: c.start,
                        score,
                    },
                );
            }
        }
        let mut hits: Vec<Hit> = best.into_values().collect();
        hits.sort_by(|a, b| {
            b.score
                .partial_cmp(&a.score)
                .unwrap_or(std::cmp::Ordering::Equal)
        });
        hits.truncate(n);
        hits
    }

    pub fn url_of(&self, doc: usize) -> String {
        format!("file://corpus/{}", self.docs[doc].path)
    }

    pub fn find(&self, reference: &str) -> Option<usize> {
        let r = reference.trim().trim_start_matches("file://corpus/");
        self.docs.iter().position(|d| d.path == r)
    }
}

pub struct CorpusSearchTool {
    pub corpus: Arc<Corpus>,
    pub sources: Arc<SourceRegistry>,
}

#[async_trait]
impl Tool for CorpusSearchTool {
    fn name(&self) -> &str {
        "corpus_search"
    }

    fn description(&self) -> String {
        format!(
            "Search the user's local document collection ({} documents: notes, internal docs, exports). Use it \
             first whenever the question concerns the user's own material, which the web cannot see. Keyword \
             search (BM25): use distinctive terms. Returns document passages with source ids; read more with corpus_read.",
            self.corpus.docs.len()
        )
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["query"],
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "minimum": 1, "maximum": 10}
            }
        })
    }

    async fn call(&self, agent: &str, input: Value) -> ToolOutput {
        let query = str_arg(&input, "query");
        let n = int_arg(&input, "max_results", 5, 1, 10) as usize;
        let hits = self.corpus.search(&query, n);
        if hits.is_empty() {
            return ToolOutput::ok(format!("No local documents match \"{query}\"."));
        }
        let mut out = format!("Local documents for \"{query}\":\n");
        for h in hits {
            let d = &self.corpus.docs[h.doc];
            let a = char_offset(&d.text, h.start);
            let b = char_offset(&d.text, h.start + 500);
            let passage = d.text[a..b]
                .split_whitespace()
                .collect::<Vec<_>>()
                .join(" ");
            let id = self.sources.upsert(
                &self.corpus.url_of(h.doc),
                &d.title,
                &passage,
                "corpus",
                agent,
            );
            if self.sources.get(&id).is_some_and(|s| s.content_chars == 0) {
                self.sources.attach_content(&id, &d.text);
            }
            out.push_str(&format!(
                "\n[{id}] {} ({})\n    …{}…\n",
                d.title,
                d.path,
                clip(&passage, 450)
            ));
        }
        let _ = self.sources.save();
        ToolOutput::ok(out)
    }
}

pub struct CorpusReadTool {
    pub corpus: Arc<Corpus>,
    pub sources: Arc<SourceRegistry>,
}

#[async_trait]
impl Tool for CorpusReadTool {
    fn name(&self) -> &str {
        "corpus_read"
    }

    fn description(&self) -> String {
        "Read a local document found with corpus_search, by source id (S3) or path. Pass `offset` to page through long documents.".into()
    }

    fn input_schema(&self) -> Value {
        json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["document"],
            "properties": {
                "document": {"type": "string", "description": "Source id or document path."},
                "offset": {"type": "integer", "minimum": 0},
                "max_chars": {"type": "integer", "minimum": 500, "maximum": 20000}
            }
        })
    }

    async fn call(&self, agent: &str, input: Value) -> ToolOutput {
        let r = str_arg(&input, "document");
        let reference = self.sources.get(&r).map(|s| s.url).unwrap_or(r);
        let Some(doc) = self.corpus.find(&reference) else {
            return ToolOutput::err("no such local document; use corpus_search to find one");
        };
        let d = &self.corpus.docs[doc];
        let id = self
            .sources
            .upsert(&self.corpus.url_of(doc), &d.title, "", "corpus", agent);
        let offset = int_arg(&input, "offset", 0, 0, i64::MAX) as usize;
        let max = int_arg(&input, "max_chars", 8000, 500, 20_000) as usize;
        let total = d.text.chars().count();
        let (a, b) = (
            char_offset(&d.text, offset),
            char_offset(&d.text, offset + max),
        );
        let mut out = format!(
            "[{id}] {} ({})\nCharacters {}–{} of {total}\n\n{}",
            d.title,
            d.path,
            offset.min(total),
            (offset + max).min(total),
            &d.text[a..b]
        );
        if offset + max < total {
            out.push_str(&format!("\n\n[… continue with offset={}]", offset + max));
        }
        ToolOutput::ok(out)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bm25_prefers_relevant_docs() {
        let docs = vec![
            Doc {
                path: "a.md".into(),
                title: "Q3 planning".into(),
                text: "Budget for the semiconductor team grew. Lead times for chips are long."
                    .into(),
            },
            Doc {
                path: "b.md".into(),
                title: "Recipes".into(),
                text: "Bake the bread for forty minutes.".into(),
            },
            Doc {
                path: "c.md".into(),
                title: "Chips".into(),
                text: "semiconductor semiconductor supply chain shortage".into(),
            },
        ];
        let c = Corpus::from_docs(PathBuf::from("."), docs);
        let hits = c.search("semiconductor shortage", 5);
        assert_eq!(hits.len(), 2);
        assert_eq!(c.docs[hits[0].doc].path, "c.md");
        assert_eq!(c.find("file://corpus/a.md"), Some(0));
    }

    #[test]
    fn loads_folder() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(
            dir.path().join("notes.md"),
            "# Vendor notes\nAcme ships in 6 weeks.",
        )
        .unwrap();
        std::fs::create_dir(dir.path().join("sub")).unwrap();
        std::fs::write(
            dir.path().join("sub/page.html"),
            "<title>Page</title><p>Hello</p>",
        )
        .unwrap();
        std::fs::write(dir.path().join("image.png"), [0u8, 1, 2]).unwrap();
        let c = Corpus::load(dir.path()).unwrap();
        assert_eq!(c.docs.len(), 2);
        assert_eq!(c.docs[0].title, "Vendor notes");
        assert_eq!(c.docs[1].path, "sub/page.html");
    }
}
