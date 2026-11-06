//! Runtime configuration, read from the environment and overridden by CLI flags.
//!
//! Every knob has a default, so `ANTHROPIC_API_KEY=... research-agent run "..."`
//! is enough to start.

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// A model provider. Model names may be prefixed with one (`deepseek:deepseek-chat`)
/// to mix providers, e.g. a Claude lead with cheaper subagents elsewhere.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "lowercase")]
pub enum Provider {
    Anthropic,
    Openai,
    Deepseek,
    Ollama,
}

impl Provider {
    pub fn parse(s: &str) -> Option<Provider> {
        match s.trim().to_ascii_lowercase().as_str() {
            "anthropic" | "claude" => Some(Provider::Anthropic),
            "openai" | "openai-compatible" | "openrouter" | "lmstudio" => Some(Provider::Openai),
            "deepseek" => Some(Provider::Deepseek),
            "ollama" => Some(Provider::Ollama),
            _ => None,
        }
    }

    pub fn label(self) -> &'static str {
        match self {
            Provider::Anthropic => "anthropic",
            Provider::Openai => "openai",
            Provider::Deepseek => "deepseek",
            Provider::Ollama => "ollama",
        }
    }

    /// (lead, subagent, citation, judge) defaults. The lead/subagent split follows
    /// the orchestrator-worker design: a strong lead, faster workers.
    pub fn default_models(self) -> [&'static str; 4] {
        match self {
            Provider::Anthropic => [
                "claude-opus-5-5",
                "claude-sonnet-5-5",
                "claude-sonnet-5-5",
                "claude-opus-5-5",
            ],
            Provider::Openai => ["gpt-4.1", "gpt-4.1-mini", "gpt-4.1-mini", "gpt-4.1"],
            Provider::Deepseek => [
                "deepseek-chat",
                "deepseek-chat",
                "deepseek-chat",
                "deepseek-chat",
            ],
            Provider::Ollama => ["qwen3:32b", "qwen3:8b", "qwen3:8b", "qwen3:32b"],
        }
    }
}

/// A model reference: provider plus model id.
#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct ModelRef {
    pub provider: Provider,
    pub model: String,
}

impl ModelRef {
    /// `"claude-opus-5-5"` uses `default`; `"deepseek:deepseek-chat"` names its provider.
    /// Unknown prefixes are part of the model id (`qwen3:8b` on Ollama stays intact).
    pub fn parse(spec: &str, default: Provider) -> ModelRef {
        if let Some((prefix, rest)) = spec.split_once(':') {
            if let Some(p) = Provider::parse(prefix) {
                return ModelRef {
                    provider: p,
                    model: rest.to_string(),
                };
            }
        }
        ModelRef {
            provider: default,
            model: spec.to_string(),
        }
    }
}

impl std::fmt::Display for ModelRef {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}:{}", self.provider.label(), self.model)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Limits {
    /// Hard spend cap for one run, in USD. The lead is told to wrap up at 80%.
    pub max_usd: f64,
    /// Subagents one `run_subagents` call may start.
    pub max_subagents_per_round: usize,
    /// Subagents across the whole run.
    pub max_subagents_total: usize,
    /// How many subagents run at the same time.
    pub parallel_subagents: usize,
    /// Upper bound on any subagent's tool calls, whatever the lead asks for.
    pub subagent_max_tool_calls: u32,
    pub subagent_max_turns: u32,
    pub lead_max_turns: u32,
    pub lead_max_tool_calls: u32,
    /// Wall-clock limit for one run, in minutes.
    pub max_minutes: u64,
    /// When the lead's prompt grows past this many tokens, it continues in a fresh
    /// context seeded from memory (plan, notes) and the subagents' saved outputs.
    pub lead_context_tokens: u64,
}

impl Default for Limits {
    fn default() -> Self {
        Limits {
            max_usd: 5.0,
            max_subagents_per_round: 6,
            max_subagents_total: 18,
            parallel_subagents: 5,
            subagent_max_tool_calls: 25,
            subagent_max_turns: 20,
            lead_max_turns: 30,
            lead_max_tool_calls: 60,
            max_minutes: 45,
            lead_context_tokens: 160_000,
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct SearchConfig {
    /// auto | anthropic | tavily | brave | searxng | duckduckgo | none
    pub backend: String,
    #[serde(skip)]
    pub tavily_key: Option<String>,
    #[serde(skip)]
    pub brave_key: Option<String>,
    pub searxng_url: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Config {
    pub provider: Provider,
    pub lead_model: ModelRef,
    pub subagent_model: ModelRef,
    pub citation_model: ModelRef,
    pub judge_model: ModelRef,
    pub lead_effort: String,
    pub subagent_effort: String,
    /// `summarized` keeps readable thinking summaries in the trace; `omitted` hides them.
    pub thinking_display: String,
    pub search: SearchConfig,
    pub corpus_dir: Option<PathBuf>,
    pub data_dir: PathBuf,
    pub prompts_dir: Option<PathBuf>,
    pub limits: Limits,
    /// Record queries, outputs and thinking in the trace. Off = structure only
    /// (tools, timings, tokens, errors), for privacy-preserving observability.
    pub trace_content: bool,
    pub max_retries: u32,
    #[serde(skip)]
    pub keys: Keys,
    pub anthropic_base_url: String,
    pub openai_base_url: String,
    pub deepseek_base_url: String,
    pub ollama_base_url: String,
}

#[derive(Clone, Debug, Default)]
pub struct Keys {
    pub anthropic: Option<String>,
    pub openai: Option<String>,
    pub deepseek: Option<String>,
    pub ollama: Option<String>,
}

fn env(name: &str) -> Option<String> {
    std::env::var(name)
        .ok()
        .map(|v| v.trim().to_string())
        .filter(|v| !v.is_empty())
}

fn env_num<T: std::str::FromStr>(name: &str) -> Option<T> {
    env(name).and_then(|v| v.parse().ok())
}

fn env_bool(name: &str, default: bool) -> bool {
    match env(name).as_deref() {
        Some("0" | "false" | "no" | "off") => false,
        Some("1" | "true" | "yes" | "on") => true,
        _ => default,
    }
}

impl Config {
    /// Build from environment variables (all optional).
    pub fn from_env() -> Config {
        let keys = Keys {
            anthropic: env("ANTHROPIC_API_KEY"),
            openai: env("OPENAI_API_KEY"),
            deepseek: env("DEEPSEEK_API_KEY"),
            ollama: env("OLLAMA_API_KEY"),
        };
        let provider = env("RESEARCH_PROVIDER")
            .and_then(|p| Provider::parse(&p))
            .unwrap_or(if keys.anthropic.is_none() && keys.deepseek.is_some() {
                Provider::Deepseek
            } else if keys.anthropic.is_none() && keys.openai.is_some() {
                Provider::Openai
            } else {
                Provider::Anthropic
            });
        let mut cfg = Config {
            provider,
            lead_model: ModelRef::parse("", provider),
            subagent_model: ModelRef::parse("", provider),
            citation_model: ModelRef::parse("", provider),
            judge_model: ModelRef::parse("", provider),
            lead_effort: env("RESEARCH_LEAD_EFFORT").unwrap_or_else(|| "high".into()),
            subagent_effort: env("RESEARCH_SUBAGENT_EFFORT").unwrap_or_else(|| "medium".into()),
            thinking_display: env("RESEARCH_THINKING_DISPLAY")
                .unwrap_or_else(|| "summarized".into()),
            search: SearchConfig {
                backend: env("RESEARCH_SEARCH").unwrap_or_else(|| "auto".into()),
                tavily_key: env("TAVILY_API_KEY"),
                brave_key: env("BRAVE_API_KEY").or_else(|| env("BRAVE_SEARCH_API_KEY")),
                searxng_url: env("SEARXNG_URL"),
            },
            corpus_dir: env("RESEARCH_CORPUS").map(PathBuf::from),
            data_dir: env("RESEARCH_DATA_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| PathBuf::from("research-data")),
            prompts_dir: env("RESEARCH_PROMPTS_DIR").map(PathBuf::from),
            limits: Limits::default(),
            trace_content: env_bool("RESEARCH_TRACE_CONTENT", true),
            max_retries: env_num("RESEARCH_MAX_RETRIES").unwrap_or(4),
            keys,
            anthropic_base_url: env("ANTHROPIC_BASE_URL")
                .unwrap_or_else(|| "https://api.anthropic.com".into()),
            openai_base_url: env("OPENAI_BASE_URL")
                .unwrap_or_else(|| "https://api.openai.com/v1".into()),
            deepseek_base_url: env("DEEPSEEK_BASE_URL")
                .unwrap_or_else(|| "https://api.deepseek.com".into()),
            ollama_base_url: env("OLLAMA_BASE_URL")
                .unwrap_or_else(|| "http://localhost:11434/v1".into()),
        };
        cfg.apply_models(
            env("RESEARCH_LEAD_MODEL"),
            env("RESEARCH_SUBAGENT_MODEL"),
            env("RESEARCH_CITATION_MODEL"),
            env("RESEARCH_JUDGE_MODEL"),
        );
        let l = &mut cfg.limits;
        if let Some(v) = env_num("RESEARCH_MAX_USD") {
            l.max_usd = v;
        }
        if let Some(v) = env_num("RESEARCH_MAX_SUBAGENTS") {
            l.max_subagents_total = v;
        }
        if let Some(v) = env_num("RESEARCH_PARALLEL_SUBAGENTS") {
            l.parallel_subagents = v;
        }
        if let Some(v) = env_num("RESEARCH_MAX_MINUTES") {
            l.max_minutes = v;
        }
        if let Some(v) = env_num("RESEARCH_LEAD_CONTEXT_TOKENS") {
            l.lead_context_tokens = v;
        }
        cfg
    }

    /// Set the provider and re-derive any model that was left at its default.
    pub fn set_provider(&mut self, provider: Provider) {
        self.provider = provider;
        self.apply_models(None, None, None, None);
    }

    /// Fill in models: explicit specs win; otherwise the provider's defaults.
    pub fn apply_models(
        &mut self,
        lead: Option<String>,
        sub: Option<String>,
        cite: Option<String>,
        judge: Option<String>,
    ) {
        let d = self.provider.default_models();
        let pick = |spec: Option<String>,
                    current: &ModelRef,
                    default: &str,
                    provider: Provider|
         -> ModelRef {
            match spec {
                Some(s) => ModelRef::parse(&s, provider),
                None if current.model.is_empty() || current.provider != provider => {
                    ModelRef::parse(default, provider)
                }
                None => current.clone(),
            }
        };
        let p = self.provider;
        self.lead_model = pick(lead, &self.lead_model, d[0], p);
        self.subagent_model = pick(sub, &self.subagent_model, d[1], p);
        self.citation_model = pick(cite, &self.citation_model, d[2], p);
        self.judge_model = pick(judge, &self.judge_model, d[3], p);
    }

    pub fn key_for(&self, p: Provider) -> Option<String> {
        match p {
            Provider::Anthropic => self.keys.anthropic.clone(),
            Provider::Openai => self.keys.openai.clone(),
            Provider::Deepseek => self.keys.deepseek.clone(),
            Provider::Ollama => self.keys.ollama.clone().or_else(|| Some("ollama".into())),
        }
    }

    pub fn base_url_for(&self, p: Provider) -> String {
        match p {
            Provider::Anthropic => self.anthropic_base_url.clone(),
            Provider::Openai => self.openai_base_url.clone(),
            Provider::Deepseek => self.deepseek_base_url.clone(),
            Provider::Ollama => self.ollama_base_url.clone(),
        }
    }

    pub fn runs_dir(&self) -> PathBuf {
        self.data_dir.join("runs")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn model_ref_prefixes() {
        let m = ModelRef::parse("deepseek:deepseek-chat", Provider::Anthropic);
        assert_eq!(m.provider, Provider::Deepseek);
        assert_eq!(m.model, "deepseek-chat");
        let m = ModelRef::parse("qwen3:8b", Provider::Ollama);
        assert_eq!(m.provider, Provider::Ollama);
        assert_eq!(m.model, "qwen3:8b");
        let m = ModelRef::parse("ollama:qwen3:8b", Provider::Anthropic);
        assert_eq!(
            (m.provider, m.model.as_str()),
            (Provider::Ollama, "qwen3:8b")
        );
    }

    #[test]
    fn provider_switch_resets_default_models() {
        let mut c = Config::from_env();
        c.set_provider(Provider::Anthropic);
        assert_eq!(c.lead_model.model, "claude-opus-5-5");
        assert_eq!(c.subagent_model.model, "claude-sonnet-5-5");
        c.set_provider(Provider::Deepseek);
        assert_eq!(c.lead_model.provider, Provider::Deepseek);
    }
}
