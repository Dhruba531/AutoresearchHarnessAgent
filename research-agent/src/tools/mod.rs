//! Tools are plugins: one trait, one definition each. Every tool has a distinct
//! purpose and a description that says *when* to use it, because a vague tool
//! description sends agents down the wrong path.
//!
//! Inputs are validated against the tool's JSON schema before the tool runs; a
//! bad input becomes an error result the model can correct, never a crash.

pub mod corpus;
pub mod fetch;
pub mod html;
pub mod memory;
pub mod search;
pub mod sources;

use async_trait::async_trait;
use serde_json::{json, Value};

#[derive(Clone, Debug)]
pub struct ToolOutput {
    pub content: String,
    pub is_error: bool,
}

impl ToolOutput {
    pub fn ok(s: impl Into<String>) -> ToolOutput {
        ToolOutput {
            content: s.into(),
            is_error: false,
        }
    }
    pub fn err(s: impl Into<String>) -> ToolOutput {
        ToolOutput {
            content: s.into(),
            is_error: true,
        }
    }
}

#[async_trait]
pub trait Tool: Send + Sync {
    fn name(&self) -> &str;
    fn description(&self) -> String;
    fn input_schema(&self) -> Value;
    /// Run the tool for `agent` (its name, for tracing and per-agent state).
    async fn call(&self, agent: &str, input: Value) -> ToolOutput;
}

/// The tool's definition in the Messages API format.
pub fn definition(t: &dyn Tool) -> Value {
    json!({"name": t.name(), "description": t.description(), "input_schema": t.input_schema()})
}

/// A tool that ends the agent's work; the loop returns its input as the result.
pub struct FinishTool {
    pub name: String,
    pub description: String,
    pub schema: Value,
}

#[async_trait]
impl Tool for FinishTool {
    fn name(&self) -> &str {
        &self.name
    }
    fn description(&self) -> String {
        self.description.clone()
    }
    fn input_schema(&self) -> Value {
        self.schema.clone()
    }
    async fn call(&self, _agent: &str, _input: Value) -> ToolOutput {
        ToolOutput::ok("Received.")
    }
}

/// Validate `input` against the subset of JSON Schema the tools use: types,
/// `properties`, `required`, `additionalProperties: false`, `enum`, `items`,
/// `minItems`/`maxItems` and `minimum`/`maximum`.
pub fn validate(schema: &Value, input: &Value) -> Result<(), String> {
    check(schema, input, "input")
}

fn type_ok(t: &str, v: &Value) -> bool {
    match t {
        "object" => v.is_object(),
        "array" => v.is_array(),
        "string" => v.is_string(),
        "integer" => v.is_i64() || v.is_u64() || v.as_f64().is_some_and(|f| f.fract() == 0.0),
        "number" => v.is_number(),
        "boolean" => v.is_boolean(),
        "null" => v.is_null(),
        _ => true,
    }
}

fn check(schema: &Value, v: &Value, path: &str) -> Result<(), String> {
    match &schema["type"] {
        Value::String(t) if !type_ok(t, v) => return Err(format!("{path} must be of type {t}")),
        Value::Array(ts) if !ts.iter().filter_map(Value::as_str).any(|t| type_ok(t, v)) => {
            return Err(format!("{path} has the wrong type"));
        }
        _ => {}
    }
    if let Some(options) = schema["enum"].as_array() {
        if !options.contains(v) {
            return Err(format!(
                "{path} must be one of {}",
                Value::Array(options.clone())
            ));
        }
    }
    if let Some(n) = v.as_f64() {
        if let Some(min) = schema["minimum"].as_f64().filter(|m| n < *m) {
            return Err(format!("{path} must be at least {min}"));
        }
        if let Some(max) = schema["maximum"].as_f64().filter(|m| n > *m) {
            return Err(format!("{path} must be at most {max}"));
        }
    }
    if let Some(obj) = v.as_object() {
        let props = schema["properties"].as_object();
        for req in schema["required"]
            .as_array()
            .into_iter()
            .flatten()
            .filter_map(Value::as_str)
        {
            if obj.get(req).is_none_or(Value::is_null) {
                return Err(format!("{path}.{req} is required"));
            }
        }
        for (k, child) in obj {
            match props.and_then(|p| p.get(k)) {
                Some(s) => check(s, child, &format!("{path}.{k}"))?,
                None if schema["additionalProperties"] == false => {
                    return Err(format!("{path}.{k} is not an allowed field"));
                }
                None => {}
            }
        }
    }
    if let Some(arr) = v.as_array() {
        if let Some(min) = schema["minItems"]
            .as_u64()
            .filter(|m| (arr.len() as u64) < *m)
        {
            return Err(format!("{path} needs at least {min} item(s)"));
        }
        if let Some(max) = schema["maxItems"]
            .as_u64()
            .filter(|m| (arr.len() as u64) > *m)
        {
            return Err(format!("{path} allows at most {max} item(s)"));
        }
        if schema.get("items").is_some() {
            for (i, item) in arr.iter().enumerate() {
                check(&schema["items"], item, &format!("{path}[{i}]"))?;
            }
        }
    }
    Ok(())
}

/// Helpers for reading validated inputs.
pub fn str_arg(input: &Value, key: &str) -> String {
    input[key].as_str().unwrap_or("").trim().to_string()
}

pub fn int_arg(input: &Value, key: &str, default: i64, min: i64, max: i64) -> i64 {
    input[key]
        .as_i64()
        .or_else(|| input[key].as_f64().map(|f| f as i64))
        .unwrap_or(default)
        .clamp(min, max)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn validates_nested_inputs() {
        let schema = json!({
            "type": "object", "additionalProperties": false, "required": ["tasks"],
            "properties": {"tasks": {"type": "array", "minItems": 1, "maxItems": 2, "items": {
                "type": "object", "required": ["objective"],
                "properties": {"objective": {"type": "string"}, "tool_budget": {"type": "integer", "minimum": 1, "maximum": 30}}}}}
        });
        assert!(validate(
            &schema,
            &json!({"tasks": [{"objective": "x", "tool_budget": 5}]})
        )
        .is_ok());
        assert!(validate(&schema, &json!({}))
            .unwrap_err()
            .contains("tasks is required"));
        assert!(validate(&schema, &json!({"tasks": []}))
            .unwrap_err()
            .contains("at least 1"));
        assert!(validate(&schema, &json!({"tasks": [{"objective": 3}]}))
            .unwrap_err()
            .contains("type string"));
        assert!(validate(
            &schema,
            &json!({"tasks": [{"objective": "x", "tool_budget": 99}]})
        )
        .unwrap_err()
        .contains("at most"));
        assert!(
            validate(&schema, &json!({"tasks": [{"objective": "x"}], "extra": 1}))
                .unwrap_err()
                .contains("not an allowed")
        );
    }

    #[test]
    fn enums() {
        let schema = json!({"type": "object", "properties": {"c": {"type": "string", "enum": ["high", "low"]}}});
        assert!(validate(&schema, &json!({"c": "high"})).is_ok());
        assert!(validate(&schema, &json!({"c": "mid"})).is_err());
    }
}
