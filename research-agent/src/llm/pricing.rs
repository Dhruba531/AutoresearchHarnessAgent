//! List prices in USD per million tokens, so budgets are enforced on real spend.
//! Override any model with `RESEARCH_PRICE_<MODEL>="input,output"` (non
//! alphanumerics in the model id become `_`), e.g.
//! `RESEARCH_PRICE_DEEPSEEK_CHAT="0.28,0.42"`.

use super::Usage;

/// (model prefix, input, output, cache read). Longest matching prefix wins.
const PRICES: &[(&str, f64, f64, f64)] = &[
    ("claude-fable-5-1", 10.0, 50.0, 0.25),
    ("claude-fable-5", 10.0, 50.0, 1.0),
    ("claude-mythos-5", 10.0, 50.0, 1.0),
    ("claude-opus-5-5", 4.0, 20.0, 0.20),
    ("claude-opus-5", 5.0, 25.0, 0.50),
    ("claude-opus-4", 5.0, 25.0, 0.50),
    ("claude-sonnet-5-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-5", 2.0, 10.0, 0.20),
    ("claude-sonnet-4", 3.0, 15.0, 0.30),
    ("claude-haiku-5-5", 0.10, 0.50, 0.01),
    ("claude-haiku-4-5", 1.0, 5.0, 0.10),
    ("gpt-4.1-mini", 0.4, 1.6, 0.1),
    ("gpt-4.1-nano", 0.1, 0.4, 0.025),
    ("gpt-4.1", 2.0, 8.0, 0.5),
    ("gpt-4o-mini", 0.15, 0.6, 0.075),
    ("gpt-4o", 2.5, 10.0, 1.25),
    ("o4-mini", 1.1, 4.4, 0.275),
    ("deepseek-chat", 0.28, 0.42, 0.028),
    ("deepseek-reasoner", 0.28, 0.42, 0.028),
];

/// Unknown models are priced conservatively high rather than as free.
const FALLBACK: (f64, f64, f64) = (10.0, 50.0, 1.0);
/// Local models cost nothing per token.
const LOCAL: (f64, f64, f64) = (0.0, 0.0, 0.0);
/// Anthropic server-side web search: $10 per 1,000 searches.
pub const WEB_SEARCH_USD: f64 = 0.01;

pub fn price_for(model: &str, provider: &str) -> (f64, f64, f64) {
    let key = format!(
        "RESEARCH_PRICE_{}",
        model
            .chars()
            .map(|c| if c.is_ascii_alphanumeric() {
                c.to_ascii_uppercase()
            } else {
                '_'
            })
            .collect::<String>()
    );
    if let Ok(v) = std::env::var(&key) {
        let nums: Vec<f64> = v.split(',').filter_map(|x| x.trim().parse().ok()).collect();
        if nums.len() >= 2 {
            return (
                nums[0],
                nums[1],
                nums.get(2).copied().unwrap_or(nums[0] * 0.1),
            );
        }
    }
    if provider == "ollama" {
        return LOCAL;
    }
    let bare = model.rsplit('/').next().unwrap_or(model);
    PRICES
        .iter()
        .filter(|(p, ..)| bare.starts_with(p))
        .max_by_key(|(p, ..)| p.len())
        .map(|&(_, i, o, c)| (i, o, c))
        .unwrap_or(FALLBACK)
}

pub fn cost(model: &str, provider: &str, u: &Usage) -> f64 {
    let (pin, pout, pcache) = price_for(model, provider);
    let cache_write = pin * 1.25;
    (u.input_tokens as f64 * pin
        + u.cache_creation_input_tokens as f64 * cache_write
        + u.cache_read_input_tokens as f64 * pcache
        + u.output_tokens as f64 * pout)
        / 1_000_000.0
        + u.web_search_requests as f64 * WEB_SEARCH_USD
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn longest_prefix_wins() {
        assert_eq!(price_for("claude-opus-5-5", "anthropic").0, 4.0);
        assert_eq!(price_for("claude-opus-5", "anthropic").0, 5.0);
        assert_eq!(price_for("gpt-4.1-mini-2025", "openai").0, 0.4);
        assert_eq!(price_for("openai/gpt-4.1", "openai").0, 2.0);
        assert_eq!(price_for("mystery", "openai"), FALLBACK);
        assert_eq!(price_for("qwen3:8b", "ollama"), LOCAL);
    }

    #[test]
    fn cost_counts_cache_and_searches() {
        let u = Usage {
            input_tokens: 1_000_000,
            output_tokens: 0,
            cache_read_input_tokens: 1_000_000,
            web_search_requests: 2,
            ..Usage::default()
        };
        let c = cost("claude-opus-5-5", "anthropic", &u);
        assert!((c - (4.0 + 0.2 + 0.02)).abs() < 1e-9);
    }
}
