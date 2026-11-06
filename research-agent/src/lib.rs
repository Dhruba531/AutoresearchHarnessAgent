//! A multi-agent research system, after Anthropic's "How we built our
//! multi-agent research system": a lead researcher plans and delegates to search
//! subagents that work in parallel, then a citation agent attributes the claims.
//!
//! The crate is self-contained (its own workspace) so it can be moved out of the
//! repository it was developed in.

pub mod agent;
pub mod budget;
pub mod citations;
pub mod config;
pub mod doctor;
pub mod evals;
pub mod judge;
pub mod llm;
pub mod prompts;
pub mod research;
#[cfg(feature = "server")]
pub mod server;
pub mod store;
pub mod tools;
pub mod trace;
pub mod util;
