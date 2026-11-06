//! Spend and effort accounting shared by every agent in a run.
//!
//! Multi-agent research burns tokens fast, so limits are enforced in code, not
//! left to the prompt: a USD cap, a wall-clock cap, and caps on subagents. Agents
//! are warned before a limit (so they can wrap up) and stopped at it.

use serde::{Deserialize, Serialize};
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::config::Limits;
use crate::llm::Usage;

#[derive(Clone, Debug, Default, Serialize, Deserialize, PartialEq)]
pub struct Spend {
    pub usd: f64,
    pub input_tokens: u64,
    pub output_tokens: u64,
    pub cache_read_tokens: u64,
    pub model_calls: u64,
    pub tool_calls: u64,
    pub web_searches: u64,
    pub subagents: u64,
    /// Seconds spent in earlier sessions of this run (before a resume).
    pub prior_seconds: u64,
}

impl Spend {
    pub fn add_usage(&mut self, usage: &Usage, cost: f64) {
        self.usd += cost;
        self.input_tokens += usage.input_tokens + usage.cache_creation_input_tokens;
        self.cache_read_tokens += usage.cache_read_input_tokens;
        self.output_tokens += usage.output_tokens;
        self.web_searches += usage.web_search_requests;
        self.model_calls += 1;
    }

    pub fn total_tokens(&self) -> u64 {
        self.input_tokens + self.cache_read_tokens + self.output_tokens
    }
}

pub struct Budget {
    spend: Mutex<Spend>,
    pub limits: Limits,
    started: Instant,
}

impl Budget {
    pub fn new(limits: Limits) -> Budget {
        Budget::resume(limits, Spend::default())
    }

    /// Continue counting from what an earlier session of the run already spent.
    pub fn resume(limits: Limits, spent: Spend) -> Budget {
        Budget {
            spend: Mutex::new(spent),
            limits,
            started: Instant::now(),
        }
    }

    /// A budget with no limits (judging, doctor).
    pub fn unlimited() -> Budget {
        let limits = Limits {
            max_usd: f64::INFINITY,
            max_minutes: u64::MAX / 120,
            max_subagents_total: usize::MAX,
            ..Limits::default()
        };
        Budget::new(limits)
    }

    pub fn record_call(&self, usage: &Usage, cost: f64) {
        self.spend.lock().unwrap().add_usage(usage, cost);
    }

    pub fn record_tool_calls(&self, n: u64) {
        self.spend.lock().unwrap().tool_calls += n;
    }

    pub fn elapsed(&self) -> Duration {
        let prior = self.spend.lock().unwrap().prior_seconds;
        self.started.elapsed() + Duration::from_secs(prior)
    }

    /// Reserve up to `wanted` subagent slots; returns how many were granted.
    pub fn reserve_subagents(&self, wanted: usize) -> usize {
        let mut s = self.spend.lock().unwrap();
        let left = self
            .limits
            .max_subagents_total
            .saturating_sub(s.subagents as usize);
        let granted = wanted.min(left);
        s.subagents += granted as u64;
        granted
    }

    /// Fraction of the tightest limit used (USD or wall clock), 0.0..=∞.
    pub fn fraction_used(&self) -> f64 {
        let usd = self.spend.lock().unwrap().usd;
        let money = if self.limits.max_usd.is_finite() && self.limits.max_usd > 0.0 {
            usd / self.limits.max_usd
        } else {
            0.0
        };
        let secs = self.limits.max_minutes.saturating_mul(60) as f64;
        let time = if secs > 0.0 {
            self.elapsed().as_secs_f64() / secs
        } else {
            0.0
        };
        money.max(time)
    }

    /// `Some(reason)` once a hard limit is reached.
    pub fn exhausted(&self) -> Option<String> {
        let usd = self.spend.lock().unwrap().usd;
        if usd >= self.limits.max_usd {
            return Some(format!(
                "spend limit reached (${usd:.2} of ${:.2})",
                self.limits.max_usd
            ));
        }
        let mins = self.elapsed().as_secs() / 60;
        if mins >= self.limits.max_minutes {
            return Some(format!(
                "time limit reached ({mins} of {} minutes)",
                self.limits.max_minutes
            ));
        }
        None
    }

    pub fn snapshot(&self) -> Spend {
        let mut s = self.spend.lock().unwrap().clone();
        s.prior_seconds = self.elapsed().as_secs();
        s
    }

    pub fn summary(&self) -> String {
        let s = self.snapshot();
        format!(
            "${:.3} of ${:.2} · {} model calls · {} tool calls · {} subagents · {}s",
            s.usd, self.limits.max_usd, s.model_calls, s.tool_calls, s.subagents, s.prior_seconds
        )
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn limits_and_reservations() {
        let limits = Limits {
            max_usd: 1.0,
            max_subagents_total: 3,
            ..Limits::default()
        };
        let b = Budget::new(limits);
        assert_eq!(b.reserve_subagents(2), 2);
        assert_eq!(b.reserve_subagents(5), 1);
        assert_eq!(b.reserve_subagents(1), 0);
        assert!(b.exhausted().is_none());
        b.record_call(
            &Usage {
                input_tokens: 10,
                output_tokens: 5,
                ..Usage::default()
            },
            0.6,
        );
        assert!(b.fraction_used() > 0.5);
        b.record_call(&Usage::default(), 0.5);
        assert!(b.exhausted().unwrap().contains("spend limit"));
    }
}
