// Automated reviewer findings raised against the draft.

import { useMemo } from "react";
import { type ReviewFindingOut } from "@/lib/api";
import { BentoHead, Dot, DotTone, MetaChip } from "./primitives";
import { RunSetupCard } from "./gates";
import { StatusTimeline, TimelineDetailSheet, TimelineEvent, toneStyles } from "./timeline";

// ─── Reviewer prose rendering ───────────────────────────────────────────────
//
// A reviewer returns MARKDOWN, not a sentence: "## Summary", "## Strengths",
// "## Weaknesses & Issues" with numbered points, and often a trailing score.
// Dropping that into a <p> rendered the literal "##" and "**" and produced an
// unreadable wall of text in a narrow column, which defeats the point of
// surfacing the verdicts at all.
//
// These helpers do the minimum needed to make that structure legible: split on
// headings, treat "- " and "1. " lines as list items, and honour **bold**. A
// markdown library would be far heavier than the three constructs the reviewer
// prompts can actually emit.

type Block = { kind: "para"; text: string } | { kind: "list"; items: string[] };
type Section = { heading: string | null; blocks: Block[] };

/** Render **bold** spans without pulling in a markdown dependency. */
function inline(text: string) {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) =>
    i % 2 === 1 ? (
      <strong key={i} className="font-semibold text-foreground">
        {part}
      </strong>
    ) : (
      <span key={i}>{part}</span>
    ),
  );
}

/** Split reviewer markdown into headed sections of paragraphs and lists. */
function parseReview(md: string): Section[] {
  const sections: Section[] = [];
  let current: Section = { heading: null, blocks: [] };
  let list: string[] = [];

  const flushList = () => {
    if (list.length) {
      current.blocks.push({ kind: "list", items: list });
      list = [];
    }
  };
  const flushSection = () => {
    flushList();
    if (current.heading || current.blocks.length) sections.push(current);
  };

  for (const raw of md.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) {
      flushList();
      continue;
    }
    // Models sometimes emit "## Summary The paper investigates..." on one line,
    // so prose trailing a heading is kept rather than swallowed into the title.
    const head = line.match(/^#{1,4}\s+(.+)$/);
    if (head) {
      const rest = head[1];
      const split = rest.match(/^([A-Z][A-Za-z&\s]{2,40}?)\s{2,}(.*)$/);
      flushSection();
      current = { heading: (split ? split[1] : rest).trim(), blocks: [] };
      if (split && split[2]) current.blocks.push({ kind: "para", text: split[2].trim() });
      continue;
    }
    const item = line.match(/^(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      list.push(item[1]);
      continue;
    }
    flushList();
    current.blocks.push({ kind: "para", text: line });
  }
  flushSection();
  return sections.length ? sections : [{ heading: null, blocks: [{ kind: "para", text: md }] }];
}

/** Pull a "Score: 7/10" style verdict out so it can be shown as a chip. */
function extractScore(md: string): string | null {
  const m = md.match(/score[^0-9]{0,12}(\d+(?:\.\d+)?)\s*\/\s*10/i);
  return m ? `${m[1]}/10` : null;
}

function ReviewBody({ md }: { md: string }) {
  const sections = useMemo(() => parseReview(md), [md]);
  return (
    <div className="space-y-4">
      {sections.map((sec, i) => (
        <section key={i}>
          {sec.heading && <h4 className="mono-label mb-1.5 text-foreground/70">{sec.heading}</h4>}
          <div className="space-y-2">
            {sec.blocks.map((b, j) =>
              b.kind === "para" ? (
                <p key={j} className="text-[13px] leading-relaxed text-foreground/85">
                  {inline(b.text)}
                </p>
              ) : (
                <ul key={j} className="space-y-1.5">
                  {b.items.map((it, k) => (
                    <li
                      key={k}
                      className="relative pl-4 text-[13px] leading-relaxed text-foreground/85 before:absolute before:left-0 before:top-[0.6em] before:h-1 before:w-1 before:rounded-full before:bg-foreground/30"
                    >
                      {inline(it)}
                    </li>
                  ))}
                </ul>
              ),
            )}
          </div>
        </section>
      ))}
    </div>
  );
}

export function ReviewerPanel({ findings }: { findings: ReviewFindingOut[] }) {
  const grouped = useMemo(() => {
    const bag = new Map<string, ReviewFindingOut[]>();
    for (const f of findings) {
      const list = bag.get(f.category) ?? [];
      list.push(f);
      bag.set(f.category, list);
    }
    return Array.from(bag.entries());
  }, [findings]);

  const severityCounts = useMemo(() => {
    const c = { high: 0, medium: 0, low: 0 };
    for (const f of findings) {
      const s = (f.severity ?? "low").toLowerCase();
      if (s === "high") c.high++;
      else if (s === "medium") c.medium++;
      else c.low++;
    }
    return c;
  }, [findings]);

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="review · gate g2 verdicts"
        title="Reviewer findings"
        subtitle="Automated reviewers score the run across methods, statistics, novelty, and safety. Each card groups findings by category with the worst severity surfaced."
        meta={
          findings.length ? (
            <>
              {severityCounts.high > 0 && (
                <MetaChip tone="error">{severityCounts.high} high</MetaChip>
              )}
              {severityCounts.medium > 0 && (
                <MetaChip tone="warning">{severityCounts.medium} medium</MetaChip>
              )}
              {severityCounts.low > 0 && (
                <MetaChip tone="success">{severityCounts.low} low</MetaChip>
              )}
            </>
          ) : (
            <MetaChip>awaiting run</MetaChip>
          )
        }
      />
      {findings.length === 0 ? (
        <div className="grid place-items-center px-6 py-12 text-center">
          <div>
            <div className="font-serif text-2xl text-foreground/80">No reviewer findings yet.</div>
            <p className="mx-auto mt-2 max-w-sm text-[13px] text-muted-foreground">
              Verdicts appear here after a run completes and the reviewer chain finishes scoring.
            </p>
          </div>
        </div>
      ) : (
        <div className="grid gap-px bg-panel-border md:grid-cols-2 xl:grid-cols-3">
          {grouped.map(([category, items]) => {
            const worst = items.reduce<string>(
              (acc, f) =>
                f.severity === "high"
                  ? "high"
                  : acc === "high"
                    ? "high"
                    : f.severity === "medium"
                      ? "medium"
                      : acc,
              "low",
            );
            const tone: DotTone =
              worst === "high" ? "error" : worst === "medium" ? "warning" : "success";
            return (
              <div key={category} className="bg-panel p-6">
                <div className="flex items-center justify-between">
                  <div>
                    <div className="mono-label">category</div>
                    <h3 className="mt-1 font-serif text-2xl leading-tight text-foreground">
                      {category}
                    </h3>
                  </div>
                  <span
                    className={`inline-flex items-center gap-1.5 rounded-sm border px-1.5 py-0.5 font-mono text-[9px] tracking-[0.15em] ${
                      tone === "error"
                        ? "border-destructive/50 text-destructive"
                        : tone === "warning"
                          ? "border-warning/50 text-warning"
                          : "border-primary/40 text-primary"
                    }`}
                  >
                    <Dot tone={tone} pulse={false} /> {worst.toUpperCase()}
                  </span>
                </div>
                <div className="mt-4 space-y-3">
                  {items.map((f, i) => {
                    const score = extractScore(f.finding);
                    return (
                      // Collapsed past the first: a full panel is four reviews of
                      // several hundred words each, and an operator scanning for the
                      // worst problem should not have to scroll through all of them.
                      <details
                        key={i}
                        open={i === 0}
                        className="group border-t border-panel-border pt-3 first:border-t-0 first:pt-0"
                      >
                        <summary className="flex cursor-pointer list-none items-center justify-between gap-3 [&::-webkit-details-marker]:hidden">
                          <span className="mono-label text-foreground/60 transition-colors group-hover:text-foreground">
                            reviewer verdict
                          </span>
                          <span className="flex items-center gap-2">
                            {score && (
                              <span className="rounded-sm border border-panel-border px-1.5 py-0.5 font-mono text-[10px] text-foreground/70">
                                {score}
                              </span>
                            )}
                            <span className="font-mono text-[10px] text-muted-foreground transition-transform group-open:rotate-90">
                              &#9656;
                            </span>
                          </span>
                        </summary>
                        <div className="mt-3">
                          <ReviewBody md={f.finding} />
                        </div>
                        {f.suggested_fix && (
                          <div className="mt-3 rounded-md border border-primary/25 bg-primary/5 p-3">
                            <div className="mono-label mb-1 text-primary/80">suggested fix</div>
                            <p className="text-[13px] leading-relaxed text-foreground/85">
                              {inline(f.suggested_fix)}
                            </p>
                          </div>
                        )}
                      </details>
                    );
                  })}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ─── Status timeline ───────────────────────────────────────────────────────
//
// The vertical progress rail down the page: every gate and state change in the
// run's life, newest at the bottom. At ~700 lines this is the second-largest
// block in the file after RunSetupCard.
//
// Read it in three parts:
//   1. `TimelineEvent` + `toneStyles` — the data shape and its colour lookup
//   2. `StatusTimeline`  — derives the event list from the current run state
//   3. `TimelineDetailSheet` — the side panel for one expanded event

// Note this is a THIRD tone vocabulary in the codebase, distinct from `DotTone`
// above and `Tone` in campaign/primitives.tsx. It adds "pending" and "active",
// which the others have no need for.
