//! The citation agent: attribute the report's claims to the sources behind them.
//!
//! The model never rewrites the report. It returns (claim, source ids) pairs with
//! each claim copied verbatim; the markers are inserted here, in code, at the
//! exact place each claim appears. So the cited report is provably the lead's
//! text plus markers, a claim that cannot be located is dropped rather than
//! guessed, and only sources that exist in the run can be cited.

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::Arc;

use crate::agent::{self, AgentEnd, AgentSpec, AgentState, Env, NoHooks};
use crate::llm::Model;
use crate::tools::sources::{Source, SourceRegistry};
use crate::tools::{FinishTool, Tool};
use crate::util::{char_offset, clip, words};

const MAX_SOURCES: usize = 40;
const EXCERPT_CHARS: usize = 1600;

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
pub struct Citation {
    pub claim: String,
    pub sources: Vec<String>,
}

#[derive(Clone, Debug, Default, Serialize, Deserialize)]
pub struct CitedReport {
    pub markdown: String,
    /// Sources in citation-number order (`[1]` is the first).
    pub cited: Vec<Source>,
    pub claims: usize,
    pub dropped: usize,
    pub citations: Vec<Citation>,
}

fn finish_tool() -> FinishTool {
    FinishTool {
        name: "add_citations".into(),
        description: "Submit every citation for the report in one call.".into(),
        schema: json!({
            "type": "object",
            "additionalProperties": false,
            "required": ["citations"],
            "properties": {"citations": {"type": "array", "items": {
                "type": "object",
                "additionalProperties": false,
                "required": ["claim", "sources"],
                "properties": {
                    "claim": {"type": "string", "description": "Exact text copied from the report (one sentence or clause)."},
                    "sources": {"type": "array", "minItems": 1, "items": {"type": "string"}, "description": "Source ids, e.g. [\"S3\", \"S8\"]."}
                }}}}
        }),
    }
}

/// Up to `EXCERPT_CHARS` of a source: its snippet and the passages of its text
/// that share the most words with the report.
fn excerpt(source: &Source, content: Option<String>, report_words: &[String]) -> String {
    let mut out = source.snippet.clone();
    let Some(text) = content.filter(|t| !t.trim().is_empty()) else {
        return out;
    };
    let total = text.chars().count();
    let window = 700;
    let mut scored: Vec<(usize, usize)> = (0..total)
        .step_by(window / 2)
        .map(|start| {
            let (a, b) = (
                char_offset(&text, start),
                char_offset(&text, start + window),
            );
            let w = words(&text[a..b]);
            (
                w.iter()
                    .filter(|x| x.len() > 3 && report_words.contains(x))
                    .count(),
                start,
            )
        })
        .collect();
    scored.sort_by(|x, y| y.0.cmp(&x.0).then(x.1.cmp(&y.1)));
    let mut picked: Vec<usize> = Vec::new();
    for (_, start) in scored {
        if picked.iter().all(|p| p.abs_diff(start) >= window) {
            picked.push(start);
        }
        if picked.len() == 2 {
            break;
        }
    }
    picked.sort();
    for start in picked {
        let (a, b) = (
            char_offset(&text, start),
            char_offset(&text, start + window),
        );
        let passage = text[a..b].split_whitespace().collect::<Vec<_>>().join(" ");
        out.push_str(&format!("\n…{passage}…"));
    }
    clip(&out, EXCERPT_CHARS)
}

/// Ask the citation model for citations and apply them to `report`.
/// `candidates` are source ids, most important first.
pub async fn cite(
    model: Arc<dyn Model>,
    env: &Env,
    system: &str,
    report: &str,
    registry: &SourceRegistry,
    candidates: &[String],
) -> CitedReport {
    let sources: Vec<Source> = candidates
        .iter()
        .filter_map(|id| registry.get(id))
        .take(MAX_SOURCES)
        .collect();
    if sources.is_empty() {
        return CitedReport {
            markdown: report.to_string(),
            ..CitedReport::default()
        };
    }
    let report_words = words(report);
    let mut listing = String::new();
    for s in &sources {
        listing.push_str(&format!(
            "<source id=\"{}\" title=\"{}\" url=\"{}\"{}>\n{}\n</source>\n",
            s.id,
            s.title.replace('"', "'"),
            s.url,
            s.published
                .as_ref()
                .map(|p| format!(" published=\"{p}\""))
                .unwrap_or_default(),
            excerpt(s, registry.content(&s.id), &report_words)
        ));
    }
    let tool: Arc<dyn Tool> = Arc::new(finish_tool());
    let spec = AgentSpec {
        name: "citations".into(),
        model,
        system: system.to_string(),
        tools: vec![tool],
        server_tools: vec![],
        finish_tool: "add_citations".into(),
        max_turns: 3,
        max_tool_calls: 2,
        max_tokens: 32_000,
        effort: Some("medium".into()),
        thinking_display: None,
    };
    let first = format!("<report>\n{report}\n</report>\n\n<sources>\n{listing}</sources>");
    let (end, _) = agent::run(&spec, env, AgentState::new(&first), &NoHooks).await;
    let citations: Vec<Citation> = match end {
        AgentEnd::Finished(v) => serde_json::from_value(v["citations"].clone()).unwrap_or_default(),
        _ => Vec::new(),
    };
    let allowed: Vec<String> = sources.iter().map(|s| s.id.clone()).collect();
    apply(report, &citations, registry, &allowed)
}

/// Lower-cased text with whitespace collapsed and emphasis/quote characters
/// normalised, plus a map from each normalised byte back to the original.
fn normalize(s: &str) -> (String, Vec<usize>) {
    let mut out = String::new();
    let mut map = Vec::new();
    let mut last_space = true;
    for (i, c) in s.char_indices() {
        let c = match c {
            '*' | '_' | '`' => continue,
            '‘' | '’' => '\'',
            '“' | '”' => '"',
            '–' | '—' => '-',
            c if c.is_whitespace() => ' ',
            c => c,
        };
        if c == ' ' {
            if last_space {
                continue;
            }
            last_space = true;
        } else {
            last_space = false;
        }
        for lc in c.to_lowercase() {
            let mut buf = [0u8; 4];
            for _ in lc.encode_utf8(&mut buf).bytes() {
                map.push(i);
            }
            out.push(lc);
        }
    }
    (out, map)
}

/// Byte range of `claim` in `body`: exact match first, then a whitespace-,
/// case- and emphasis-insensitive one.
fn locate(body: &str, claim: &str, from: usize) -> Option<(usize, usize)> {
    if let Some(p) = body[from..].find(claim) {
        return Some((from + p, from + p + claim.len()));
    }
    let (nb, map) = normalize(&body[from..]);
    let (nc, _) = normalize(claim);
    let nc = nc.trim();
    if nc.is_empty() {
        return None;
    }
    let p = nb.find(nc)?;
    let start = from + map[p];
    let last = map[p + nc.len() - 1];
    let end = from
        + last
        + body[from + last..]
            .chars()
            .next()
            .map(char::len_utf8)
            .unwrap_or(1);
    Some((start, end))
}

fn heading_cut(report: &str) -> usize {
    let lower = report.to_lowercase();
    [
        "\n## sources",
        "\n## references",
        "\n# sources",
        "\n# references",
        "\n## bibliography",
    ]
    .iter()
    .filter_map(|h| lower.find(h))
    .min()
    .unwrap_or(report.len())
}

/// Insert `[n]` markers after each located claim and append a numbered source list.
pub fn apply(
    report: &str,
    citations: &[Citation],
    registry: &SourceRegistry,
    allowed: &[String],
) -> CitedReport {
    let cut = heading_cut(report);
    let (body, tail) = report.split_at(cut);
    let mut inserts: Vec<(usize, Vec<String>)> = Vec::new();
    let mut kept = Vec::new();
    let mut dropped = 0;
    for c in citations {
        let ids: Vec<String> = c
            .sources
            .iter()
            .filter_map(|r| registry.get(r).map(|s| s.id))
            .filter(|id| allowed.contains(id))
            .fold(Vec::new(), |mut acc, id| {
                if !acc.contains(&id) {
                    acc.push(id);
                }
                acc
            });
        let claim = c.claim.trim().trim_start_matches(['-', '*', ' ']).trim();
        if ids.is_empty() || claim.chars().count() < 8 {
            dropped += 1;
            continue;
        }
        // Prefer an occurrence that has no citation yet.
        let mut found = None;
        let mut from = 0;
        while let Some((s, e)) = locate(body, claim, from) {
            let pos = marker_pos(body, s, e);
            if found.is_none() {
                found = Some(pos);
            }
            if !inserts.iter().any(|(p, _)| *p == pos) {
                found = Some(pos);
                break;
            }
            from = e;
            if from >= body.len() {
                break;
            }
        }
        let Some(pos) = found else {
            dropped += 1;
            continue;
        };
        match inserts.iter_mut().find(|(p, _)| *p == pos) {
            Some((_, existing)) => {
                for id in ids.iter() {
                    if !existing.contains(id) {
                        existing.push(id.clone());
                    }
                }
            }
            None => inserts.push((pos, ids.clone())),
        }
        kept.push(Citation {
            claim: claim.to_string(),
            sources: ids,
        });
    }
    inserts.sort_by_key(|(p, _)| *p);

    let mut numbers: HashMap<String, usize> = HashMap::new();
    let mut order: Vec<String> = Vec::new();
    for (_, ids) in &inserts {
        for id in ids {
            if !numbers.contains_key(id) {
                order.push(id.clone());
                numbers.insert(id.clone(), order.len());
            }
        }
    }
    let mut out = String::with_capacity(report.len() + inserts.len() * 6);
    let mut last = 0;
    for (pos, ids) in &inserts {
        out.push_str(&body[last..*pos]);
        let mut nums: Vec<usize> = ids.iter().map(|id| numbers[id]).collect();
        nums.sort();
        nums.dedup();
        if !out.ends_with(char::is_whitespace) {
            out.push(' ');
        }
        out.push_str(&nums.iter().map(|n| format!("[{n}]")).collect::<String>());
        last = *pos;
    }
    out.push_str(&body[last..]);
    let mut markdown = out.trim_end().to_string();
    if !tail.trim().is_empty() {
        markdown.push_str("\n\n");
        markdown.push_str(tail.trim());
    }
    let cited: Vec<Source> = order.iter().filter_map(|id| registry.get(id)).collect();
    if !cited.is_empty() {
        markdown.push_str("\n\n## Sources\n\n");
        for (i, s) in cited.iter().enumerate() {
            let title = if s.title.is_empty() {
                s.url.clone()
            } else {
                s.title.replace(['[', ']'], "")
            };
            let line = if let Some(path) = s.url.strip_prefix("file://corpus/") {
                format!("{}. {title} — local document `{path}`", i + 1)
            } else {
                format!(
                    "{}. [{title}]({}){}",
                    i + 1,
                    s.url,
                    s.published
                        .as_ref()
                        .map(|p| format!(" ({p})"))
                        .unwrap_or_default()
                )
            };
            markdown.push_str(&line);
            markdown.push('\n');
        }
    }
    CitedReport {
        markdown,
        cited,
        claims: kept.len(),
        dropped,
        citations: kept,
    }
}

/// Where the marker goes: after the claim, before trailing punctuation.
fn marker_pos(body: &str, start: usize, end: usize) -> usize {
    let mut pos = end;
    while pos > start {
        let prev = body[..pos].chars().next_back().unwrap_or(' ');
        if matches!(prev, '.' | ',' | ';' | ':' | '!' | '?') {
            pos -= prev.len_utf8();
        } else {
            break;
        }
    }
    pos
}

/// Strip `[n]` markers (for checking that citing changed nothing else).
pub fn strip_markers(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut chars = s.chars().peekable();
    while let Some(c) = chars.next() {
        if c == ' ' && chars.peek() == Some(&'[') {
            let rest: String = chars.clone().take(6).collect();
            if rest.len() > 2
                && rest[1..].starts_with(|ch: char| ch.is_ascii_digit())
                && rest.contains(']')
            {
                continue;
            }
        }
        if c == '[' {
            let rest: String = chars.clone().take_while(|ch| *ch != ']').collect();
            if !rest.is_empty()
                && rest.chars().all(|ch| ch.is_ascii_digit())
                && chars.clone().nth(rest.len()) == Some(']')
            {
                for _ in 0..=rest.len() {
                    chars.next();
                }
                continue;
            }
        }
        out.push(c);
    }
    out
}

pub fn citation_stats(c: &CitedReport) -> Value {
    json!({"claims": c.claims, "dropped": c.dropped, "sources": c.cited.len()})
}

#[cfg(test)]
mod tests {
    use super::*;

    fn registry() -> (tempfile::TempDir, SourceRegistry) {
        let dir = tempfile::tempdir().unwrap();
        let r = SourceRegistry::open(dir.path().to_path_buf());
        r.upsert("https://a.org", "Alpha Report", "", "web_search", "x");
        r.upsert("https://b.org", "Beta Paper", "", "web_search", "x");
        r.upsert("file://corpus/notes.md", "Notes", "", "corpus", "x");
        (dir, r)
    }

    #[test]
    fn inserts_markers_without_changing_text() {
        let (_d, reg) = registry();
        let report = "# Answer\n\nLead times reached 26 weeks in 2022. Prices rose **sharply**, by 30%.\n\nSome opinion here.";
        let cites = vec![
            Citation {
                claim: "Lead times reached 26 weeks in 2022.".into(),
                sources: vec!["S2".into(), "S1".into()],
            },
            Citation {
                claim: "Prices rose sharply, by 30%".into(),
                sources: vec!["[S1]".into()],
            },
            Citation {
                claim: "A claim that is not in the report".into(),
                sources: vec!["S1".into()],
            },
            Citation {
                claim: "Some opinion here.".into(),
                sources: vec!["S9".into()],
            },
        ];
        let allowed = vec!["S1".to_string(), "S2".to_string(), "S3".to_string()];
        let out = apply(report, &cites, &reg, &allowed);
        assert!(out.markdown.contains("in 2022 [1][2]."), "{}", out.markdown);
        assert!(out.markdown.contains("by 30% [2]."), "{}", out.markdown);
        assert_eq!(out.claims, 2);
        assert_eq!(out.dropped, 2);
        assert_eq!(
            out.cited.iter().map(|s| s.id.as_str()).collect::<Vec<_>>(),
            vec!["S2", "S1"]
        );
        assert!(out.markdown.contains(
            "## Sources\n\n1. [Beta Paper](https://b.org)\n2. [Alpha Report](https://a.org)"
        ));
        let body = out.markdown.split("\n\n## Sources").next().unwrap();
        assert_eq!(strip_markers(body), report);
    }

    #[test]
    fn keeps_existing_reference_section_last() {
        let (_d, reg) = registry();
        let report = "Acme ships in six weeks per the notes.\n\n## References\nold list";
        let cites = vec![Citation {
            claim: "Acme ships in six weeks per the notes".into(),
            sources: vec!["S3".into()],
        }];
        let out = apply(report, &cites, &reg, &["S3".to_string()]);
        assert!(out
            .markdown
            .starts_with("Acme ships in six weeks per the notes [1]."));
        assert!(out.markdown.contains(
            "## References\nold list\n\n## Sources\n\n1. Notes — local document `notes.md`"
        ));
    }

    #[test]
    fn locate_is_forgiving() {
        let body = "The  **Fed** raised rates\nto 5.5% — a 22-year high.";
        let (s, e) = locate(body, "the fed raised rates to 5.5% - a 22-year high", 0).unwrap();
        assert_eq!(
            &body[s..e],
            "The  **Fed** raised rates\nto 5.5% — a 22-year high"
        );
    }
}
