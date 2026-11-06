//! Agent prompts. Prompt engineering is the main lever on how a multi-agent
//! system behaves, so prompts live in plain Markdown files: the defaults are
//! compiled in from `prompts/`, and `RESEARCH_PROMPTS_DIR` (or `--prompts`)
//! points at a folder of edited copies (`research-agent prompts --dump DIR`).

use std::path::Path;

const LEAD: &str = include_str!("../prompts/lead.md");
const SUBAGENT: &str = include_str!("../prompts/subagent.md");
const CITATIONS: &str = include_str!("../prompts/citations.md");
const JUDGE: &str = include_str!("../prompts/judge.md");
const DOCTOR: &str = include_str!("../prompts/doctor.md");

#[derive(Clone, Debug)]
pub struct Prompts {
    pub lead: String,
    pub subagent: String,
    pub citations: String,
    pub judge: String,
    pub doctor: String,
}

impl Default for Prompts {
    fn default() -> Self {
        Prompts {
            lead: LEAD.into(),
            subagent: SUBAGENT.into(),
            citations: CITATIONS.into(),
            judge: JUDGE.into(),
            doctor: DOCTOR.into(),
        }
    }
}

impl Prompts {
    /// Defaults, with any file present in `dir` taking precedence.
    pub fn load(dir: Option<&Path>) -> Prompts {
        let mut p = Prompts::default();
        if let Some(dir) = dir {
            let read = |name: &str, slot: &mut String| {
                if let Ok(s) = std::fs::read_to_string(dir.join(name)) {
                    if !s.trim().is_empty() {
                        *slot = s;
                    }
                }
            };
            read("lead.md", &mut p.lead);
            read("subagent.md", &mut p.subagent);
            read("citations.md", &mut p.citations);
            read("judge.md", &mut p.judge);
            read("doctor.md", &mut p.doctor);
        }
        p
    }

    /// Write the current prompts to `dir` for editing.
    pub fn dump(&self, dir: &Path) -> std::io::Result<()> {
        std::fs::create_dir_all(dir)?;
        for (name, text) in [
            ("lead.md", &self.lead),
            ("subagent.md", &self.subagent),
            ("citations.md", &self.citations),
            ("judge.md", &self.judge),
            ("doctor.md", &self.doctor),
        ] {
            std::fs::write(dir.join(name), text)?;
        }
        Ok(())
    }
}

/// Replace `{{name}}` placeholders.
pub fn render(template: &str, vars: &[(&str, String)]) -> String {
    let mut out = template.to_string();
    for (k, v) in vars {
        out = out.replace(&format!("{{{{{k}}}}}"), v);
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn renders_and_overrides() {
        assert_eq!(
            render(
                "on {{date}}, {{n}}",
                &[("date", "2026-11-06".into()), ("n", "3".into())]
            ),
            "on 2026-11-06, 3"
        );
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("lead.md"), "custom lead").unwrap();
        let p = Prompts::load(Some(dir.path()));
        assert_eq!(p.lead, "custom lead");
        assert!(p.subagent.contains("research subagent"));
    }
}
