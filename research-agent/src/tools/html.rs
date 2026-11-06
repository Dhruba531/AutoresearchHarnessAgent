//! Dependency-free HTML to readable text: drops scripts, styles and page chrome
//! (nav, header, footer, forms), keeps headings, paragraphs, list items and
//! table rows on their own lines, and decodes entities.

const SKIP: &[&str] = &[
    "script", "style", "noscript", "svg", "template", "iframe", "nav", "footer", "form", "button",
    "select", "head", "aside",
];
const BLOCK: &[&str] = &[
    "p",
    "div",
    "br",
    "hr",
    "section",
    "article",
    "main",
    "header",
    "table",
    "tr",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "blockquote",
    "pre",
    "figure",
    "figcaption",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "td",
    "th",
    "caption",
];

/// The `<title>` of a page, if any.
pub fn title(html: &str) -> Option<String> {
    let lower = html.to_ascii_lowercase();
    let start = lower.find("<title")?;
    let open_end = lower[start..].find('>')? + start + 1;
    let end = lower[open_end..].find("</title")? + open_end;
    let t = collapse(&decode_entities(&html[open_end..end]));
    (!t.is_empty()).then_some(t)
}

pub fn to_text(html: &str) -> String {
    let bytes = html.as_bytes();
    let lower = html.to_ascii_lowercase();
    let mut out = String::with_capacity(html.len() / 3);
    let mut i = 0;
    let mut text_start = 0;
    while i < bytes.len() {
        if bytes[i] != b'<' {
            i += 1;
            continue;
        }
        out.push_str(&decode_entities(&html[text_start..i]));
        if lower[i..].starts_with("<!--") {
            i = lower[i..]
                .find("-->")
                .map(|e| i + e + 3)
                .unwrap_or(bytes.len());
            text_start = i;
            continue;
        }
        let Some(rel) = lower[i..].find('>') else {
            // An unclosed '<': the rest is text.
            text_start = i;
            break;
        };
        let close = i + rel;
        let inner = &lower[i + 1..close];
        let closing = inner.starts_with('/');
        let name: String = inner
            .trim_start_matches('/')
            .chars()
            .take_while(|c| c.is_ascii_alphanumeric())
            .collect();
        i = close + 1;
        text_start = i;
        if !closing && SKIP.contains(&name.as_str()) && !inner.ends_with('/') {
            // Jump past the matching close tag.
            let needle = format!("</{name}");
            i = lower[i..]
                .find(&needle)
                .map(|e| {
                    let at = i + e;
                    lower[at..]
                        .find('>')
                        .map(|g| at + g + 1)
                        .unwrap_or(bytes.len())
                })
                .unwrap_or(bytes.len());
            text_start = i;
            continue;
        }
        if BLOCK.contains(&name.as_str()) {
            out.push('\n');
            if !closing {
                match name.as_str() {
                    "li" => out.push_str("- "),
                    "h1" | "h2" => out.push_str("## "),
                    "h3" | "h4" | "h5" | "h6" => out.push_str("### "),
                    "td" | "th" => out.push_str(" | "),
                    _ => {}
                }
            }
        }
    }
    if text_start < bytes.len() {
        out.push_str(&decode_entities(&html[text_start..]));
    }
    tidy(&out)
}

/// Collapse runs of spaces in each line, drop blank-line runs and empty bullets.
fn tidy(s: &str) -> String {
    let mut lines: Vec<String> = Vec::new();
    let mut blank = 0;
    for line in s.lines() {
        let l = collapse(line);
        if l.is_empty() || l == "-" || l == "|" || l == "##" || l == "###" {
            blank += 1;
            if blank == 1 && !lines.is_empty() {
                lines.push(String::new());
            }
            continue;
        }
        blank = 0;
        lines.push(l);
    }
    lines.join("\n").trim().to_string()
}

fn collapse(s: &str) -> String {
    s.split_whitespace().collect::<Vec<_>>().join(" ")
}

pub fn decode_entities(s: &str) -> String {
    if !s.contains('&') {
        return s.to_string();
    }
    let mut out = String::with_capacity(s.len());
    let mut rest = s;
    while let Some(amp) = rest.find('&') {
        out.push_str(&rest[..amp]);
        let tail = &rest[amp..];
        let semi = tail.find(';').filter(|&p| p <= 10);
        let decoded = semi.and_then(|p| {
            let ent = &tail[1..p];
            let ch = if let Some(num) = ent.strip_prefix("#x").or_else(|| ent.strip_prefix("#X")) {
                u32::from_str_radix(num, 16)
                    .ok()
                    .and_then(char::from_u32)
                    .map(|c| c.to_string())
            } else if let Some(num) = ent.strip_prefix('#') {
                num.parse::<u32>()
                    .ok()
                    .and_then(char::from_u32)
                    .map(|c| c.to_string())
            } else {
                named(ent).map(str::to_string)
            };
            ch.map(|c| (c, p + 1))
        });
        match decoded {
            Some((c, len)) => {
                out.push_str(&c);
                rest = &tail[len..];
            }
            None => {
                out.push('&');
                rest = &tail[1..];
            }
        }
    }
    out.push_str(rest);
    out
}

fn named(e: &str) -> Option<&'static str> {
    Some(match e {
        "amp" => "&",
        "lt" => "<",
        "gt" => ">",
        "quot" => "\"",
        "apos" => "'",
        "nbsp" => " ",
        "mdash" => "—",
        "ndash" => "–",
        "hellip" => "…",
        "rsquo" => "’",
        "lsquo" => "‘",
        "rdquo" => "”",
        "ldquo" => "“",
        "copy" => "©",
        "reg" => "®",
        "trade" => "™",
        "deg" => "°",
        "euro" => "€",
        "pound" => "£",
        "times" => "×",
        "middot" => "·",
        "bull" => "•",
        _ => return None,
    })
}

/// Strip tags from a short fragment (search snippets with `<strong>` etc).
pub fn strip_tags(s: &str) -> String {
    let mut out = String::with_capacity(s.len());
    let mut in_tag = false;
    for c in s.chars() {
        match c {
            '<' => in_tag = true,
            '>' if in_tag => in_tag = false,
            _ if !in_tag => out.push(c),
            _ => {}
        }
    }
    collapse(&decode_entities(&out))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn readable_text() {
        let html = r#"<html><head><title>Chip &amp; Co</title><style>p{}</style></head>
            <body><nav>Home | About</nav><h1>Supply</h1><p>Lead times hit&nbsp;<b>26</b> weeks.</p>
            <script>var x = "<p>no</p>";</script><ul><li>One</li><li>Two &#8212; 2</li></ul>
            <!-- comment --><footer>© 2026</footer></body></html>"#;
        assert_eq!(title(html).as_deref(), Some("Chip & Co"));
        let t = to_text(html);
        assert!(t.contains("## Supply"));
        assert!(t.contains("Lead times hit 26 weeks."));
        assert!(t.contains("- Two — 2"));
        assert!(
            !t.contains("var x")
                && !t.contains("Home | About")
                && !t.contains("comment")
                && !t.contains("2026")
        );
    }

    #[test]
    fn entities() {
        assert_eq!(
            decode_entities("a &lt;b&gt; &#x41; &unknown; & c"),
            "a <b> A &unknown; & c"
        );
        assert_eq!(
            strip_tags("<strong>Rust</strong> is &quot;fast&quot;"),
            "Rust is \"fast\""
        );
    }
}
