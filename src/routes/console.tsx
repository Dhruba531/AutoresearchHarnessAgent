// ===========================================================================
//  THE OPERATOR CONSOLE — 4,300+ lines, 45 components, ~70 state hooks
// ===========================================================================
//
//  ⚠ READ THIS MAP BEFORE READING THE CODE. This is by far the largest file in
//  the project (roughly 30% of the whole codebase) and it is NOT meant to be
//  read top to bottom. It is a page assembled from many small components, and
//  every one of them is independently understandable.
//
//  Honest assessment: this file is too big. Everything below `ConsolePage`
//  could live in its own file under `src/components/console/`, exactly as the
//  campaign surfaces already do in `src/components/campaign/`. Nothing here is
//  badly written — there is simply too much of it in one place. If you are
//  looking for a first refactor, splitting this file is it.
//
//  ---------------------------------------------------------------------------
//  WHAT THIS PAGE DOES
//  ---------------------------------------------------------------------------
//  It drives one research run from idea to published paper, through a series of
//  HUMAN GATES. That gating is the product: an AI agent does the work, but a
//  person must approve at each checkpoint before it proceeds — and crucially,
//  before it spends money.
//
//      idea → brief → [G1 approve brief] → cost estimate → [G2 approve spend]
//           → run executes → live logs → artifacts → reviewer findings
//           → [G3 revisions] → [G4 approve final export] → paper
//
//  Each gate maps to a permission in `src/lib/permissions.tsx`, so what a user
//  can approve depends on their role.
//
//  ---------------------------------------------------------------------------
//  READING ORDER — start at the bottom
//  ---------------------------------------------------------------------------
//  `ConsolePage` (~line 3894) is the LAST function in the file and the one that
//  ties everything together. Read it first: it owns the shared state and shows
//  how the pieces fit. Then dip into whichever component you need.
//
//  ---------------------------------------------------------------------------
//  COMPONENT MAP  (approximate line numbers; they shift as the file is edited)
//  ---------------------------------------------------------------------------
//
//  ~128  SMALL PRIMITIVES — the local equivalent of campaign/primitives.tsx
//        Dot, SectionHeader, StateCard, StatusBadge, ToggleTile, Logomark,
//        MetaChip, StatTile, BentoHead, friendlyError
//        · Start here if you want easy wins. Each is a few lines.
//
//  ~271  PERMISSIONS & GATING
//        RoleBadge, Gated, ProviderKeysGate, RoleBanner
//        · `Gated` is the important one — the wrapper that disables a control
//          and explains why, using the RBAC helpers from lib/permissions.tsx.
//
//  ~362  NAVIGATION & CHROME
//        NAV_ITEMS, ConsoleSidebar, ConsoleTopbar, ProjectRail, NewProjectSheet
//
//  ~618  SETUP
//        AgentConnectionPanel  — points the app at a backend (see getApiBase)
//        IdeaCard              — the starting research question
//
//  ~791  THE GATE MACHINERY
//        DecisionDialog        — the shared confirm-before-acting dialog
//        BriefCard             — gate G1: approve the research plan
//        RunSetupCard          — gate G2: approve the cost, then start the run
//        · RunSetupCard (~415 lines) is the single largest component here and
//          the one that spends money. Read it carefully before changing it.
//
//  ~1638 LIVE EXECUTION
//        LiveLogs              — WebSocket log stream (see openRunLogs in api.ts)
//        ArtifactsAndDraft     — figures, code, and the draft paper
//        AgenticActionsCard    — what the agent did, step by step
//        ExecutionResultsCard  — measured outcomes
//
//  ~2021 REVIEW
//        ReviewerPanel         — automated review findings
//
//  ~2141 THE TIMELINE  (the second-largest block, ~700 lines)
//        TimelineEvent, formatTs, toneStyles, StatusTimeline,
//        TimelineDetailSheet
//        · The vertical progress rail down the page. `toneStyles` is the
//          colour lookup; `StatusTimeline` builds the event list.
//
//  ~2883 EXPORT
//        FinalExport           — gate G4: approve publication
//        GroundednessList      — dead links, placeholders, and fabricated
//                                citations found in the draft (see
//                                GroundednessReport in lib/api.ts)
//
//  ~3216 SETTINGS
//        PROVIDERS, ProviderKeysPanel  — LLM API keys (write-only; the server
//                                        returns only the last 4 characters)
//        UsageBudgetPanel              — spend against budget
//
//  ~3616 REVISIONS
//        RevisionGate          — gate G3: request targeted edits to the paper
//
//  ~3894 ConsolePage           — ⭐ THE ENTRY POINT. Read this first.
//
//  ---------------------------------------------------------------------------
//  RECURRING PATTERNS — learn these once and most of the file reads easily
//  ---------------------------------------------------------------------------
//  • async action:  setBusy(true) → try/catch/finally → toast → setBusy(false)
//    The `finally` matters: without it a failed request leaves a spinner stuck.
//  • gating:        <Gated perm="run:start"> disables and explains, rather than
//                   hiding — a hidden control is indistinguishable from a bug.
//  • confirmation:  irreversible or costly actions route through DecisionDialog.
//  • polling:       useEffect + setInterval, with cleanup that clears the timer.
//  • lifting state: children receive callbacks (onRunUpdated, onChange) and
//                   report results upward; ConsolePage owns the shared state.
// ===========================================================================

import { createFileRoute, isRedirect, Link, redirect, useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { toast, Toaster } from "sonner";
import { Home, LogOut } from "lucide-react";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { supabase } from "@/integrations/supabase/client";
import { WorkspaceTheme } from "@/components/workspace-theme";
import {
  approveBrief as apiApproveBrief,
  approveFinal,
  ApiError,
  cancelRun as apiCancelRun,
  clearApiBase,
  createProject,
  deleteProviderKey,
  estimateRun,
  estimateRevision,
  previewRevision,
  listRevisions,
  applyRevision,
  getCapabilities,
  getUsage,
  generateBrief,
  getApiBase,
  getProject,
  getRun,
  hasRealBackend,
  health,
  listProjects,
  listProviderKeys,
  listReviews,
  logout as apiLogout,
  me,
  openRunLogs,
  saveIdea,
  saveProviderKey,
  setApiBase,
  startRun,
  testProviderKey,
  updateBrief,
  type BriefOut,
  type LogEvent,
  type ProjectOut,
  type ProviderId,
  type ProviderKeyOut,
  type ReviewFindingOut,
  type RunOut,
  type UserOut,
  type GroundednessReport,
  type CapabilitiesOut,
  type UsageOut,
  type RevisionOut,
  artifactUrl,
  type ArtifactOut,
} from "@/lib/api";
import {
  AuthProvider,
  useAuth,
  useCan,
  roleLabel,
  deniedReason,
  type Permission,
} from "@/lib/permissions";


export const Route = createFileRoute("/console")({
  ssr: false,
  validateSearch: (search: Record<string, unknown>) => ({
    tl: typeof search.tl === "string" && search.tl.length > 0 ? search.tl : undefined,
  }),
  // Route guard: the console is client-only and requires a valid session.
  // A missing, expired, or unverifiable session bounces to /auth before the
  // workspace renders so protected UI never flashes.
  beforeLoad: async ({ location }) => {
    try {
      const user = await me();
      if (!user) {
        throw redirect({
          to: "/auth",
          search: { redirect: location.href },
        });
      }
    } catch (err) {
      if (isRedirect(err)) throw err;
      throw redirect({
        to: "/auth",
        search: { redirect: location.href },
      });
    }
  },
  pendingComponent: () => (
    <main className="theme-console grid min-h-screen place-items-center bg-background px-6">
      <div className="panel max-w-md p-6 text-center">
        <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-md border border-panel-border bg-background font-serif text-foreground">
          AL
        </div>
        <p className="mono-label">checking session</p>
      </div>
    </main>
  ),
  head: () => ({
    meta: [
      { title: "Console — AgentLab" },
      {
        name: "description",
        content:
          "Operator console for governed agent research runs: brief approval, cost gates, live logs, artifacts, reviewer panel, and final export.",
      },
    ],
  }),
  component: ConsolePage,
});

// The semantic colour vocabulary for this file. Note it is a SEPARATE type
// from `Tone` in `components/campaign/primitives.tsx` — same idea, different
// member names ("error" here vs "danger" there). Duplication worth collapsing
// if this file is ever split up.
type DotTone = "success" | "warning" | "error" | "muted" | "info";


// ─── UI atoms ──────────────────────────────────────────────────────────────
// Small presentational pieces, the local counterpart of campaign/primitives.tsx.
// Each is a few lines; skim them and move on.

/** A small coloured status dot. */
// The `pulse` prop is declared but never used in the body — dead API surface,
// harmless but safe to remove.
function Dot({ tone = "success" }: { tone?: DotTone; pulse?: boolean }) {
  const map: Record<DotTone, string> = {
    success: "bg-primary",
    warning: "bg-warning",
    error: "bg-destructive",
    info: "bg-accent",
    muted: "bg-muted-foreground",
  };
  return <span className={`block h-2 w-2 rounded-full ${map[tone]}`} />;
}

/** A small icon-plus-label heading used at the top of each panel. */
function SectionHeader({
  icon,
  children,
  right,
}: {
  icon: string;
  children: React.ReactNode;
  right?: React.ReactNode;
}) {
  return (
    <div className="flex items-center justify-between border-b border-panel-border px-5 py-3 font-mono text-[11px] tracking-[0.15em] text-muted-foreground">
      <span className="flex items-center gap-2">
        <span className="text-foreground">{icon}</span> {children}
      </span>
      {right}
    </div>
  );
}

/** A large single-metric card used across the pipeline summary. */
function StateCard({ label, value, caption }: { label: string; value: string; caption: string }) {
  return (
    <div className="relative min-w-0 px-5 py-4">
      <div className="flex items-center gap-2">
        <span className="h-1.5 w-1.5 rounded-full bg-primary/70" />
        <span className="font-mono text-[10px] uppercase tracking-[0.16em] text-muted-foreground">
          {label}
        </span>
      </div>
      <div className="mt-2 truncate font-serif text-[26px] leading-[1.1] tracking-tight text-foreground">
        {value}
      </div>
      <div className="mono-label mt-1.5 truncate">{caption}</div>
    </div>
  );
}


/** Renders a run status as a dot plus an upper-case label. */
function StatusBadge({ status }: { status: string }) {
  // Normalised before matching so casing or a null from the backend cannot
  // silently fall through to the "muted" default.
  const s = (status || "").toLowerCase();
  // A ternary chain rather than the lookup-object approach used by
  // `campaign/primitives.tsx` — several statuses map to the same tone, so a
  // full table would repeat itself. Both are reasonable; be aware the codebase
  // uses each in different places.
  const tone: DotTone =
    s === "running" || s === "active"
      ? "success"
      : s === "queued" || s === "briefing" || s === "review" || s === "revising"
        ? "warning"
        : s === "failed" || s === "cancelled"
          ? "error"
          : s === "completed"
            ? "success"
            : "muted";
  return (
    <span className="inline-flex items-center gap-2 font-mono text-[10px] tracking-[0.15em] text-muted-foreground">
      <Dot tone={tone} /> {(status || "draft").toUpperCase()}
    </span>
  );
}

/**
 * A large clickable tile that behaves as an on/off switch.
 *
 * `role="switch"` with `aria-checked` is what makes this announce correctly to
 * screen readers — without them it is just a button, and its state is invisible
 * to assistive technology. Using a real <button> also gives keyboard operation
 * for free.
 */
function ToggleTile({
  on,
  onChange,
  icon,
  label,
  caption,
  tone = "primary",
}: {
  on: boolean;
  onChange: (v: boolean) => void;
  icon: string;
  label: string;
  caption: string;
  tone?: "primary" | "danger";
}) {
  const activeBorder = tone === "danger" ? "border-destructive/60" : "border-primary/60";
  const activeBg = tone === "danger" ? "bg-destructive/10" : "bg-primary/10";
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={() => onChange(!on)}
      className={`flex items-start gap-3 rounded-md border px-3 py-2.5 text-left transition-colors ${
        on
          ? `${activeBorder} ${activeBg} text-foreground`
          : "border-panel-border bg-background/40 text-muted-foreground hover:text-foreground"
      }`}
    >
      <span className="mt-0.5 text-base leading-none">{icon}</span>
      <span className="flex-1">
        <span className="block font-mono text-xs">{label}</span>
        <span className="block font-mono text-[10px] text-muted-foreground">{caption}</span>
      </span>
      <span
        className={`mt-1 inline-block h-2 w-2 rounded-full ${
          on ? (tone === "danger" ? "bg-destructive" : "bg-primary") : "bg-muted-foreground/40"
        }`}
      />
    </button>
  );
}


/**
 * Extract a displayable message from an unknown thrown value.
 *
 * Used by nearly every catch block in this file. It exists because JavaScript
 * permits throwing anything, so `e.message` is not safe to read directly.
 *
 * The `ApiError` branch is first for intent rather than necessity — `ApiError`
 * extends `Error`, so the second branch would catch it anyway, but checking it
 * explicitly documents that API failures are the expected case and leaves an
 * obvious place to add status-specific handling later.
 */
function friendlyError(e: unknown, fallback = "Request failed"): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return fallback;
}

// ─── Shell (nav + project rail) ────────────────────────────────────────────

/** The AgentLab wordmark, linking back to the landing page. */
function Logomark() {
  return (
    <Link to="/" className="flex items-center gap-2.5">
      <span
        className="grid h-8 w-8 shrink-0 place-items-center rounded-lg font-mono text-[12px] text-white"
        style={{ background: "var(--gradient-signal)" }}
      >
        [/]
      </span>
      <span className="font-serif text-[19px] leading-none text-foreground">
        AgentLab
      </span>
    </Link>
  );
}


// ─── RBAC UI helpers ───────────────────────────────────────────────────────
// The UI half of the role system defined in `src/lib/permissions.tsx`. Read
// that file first — it defines the roles, the permission matrix, and the
// `useCan` hook these components consume.
//
// ⚠ Remember the caveat from that file: this is CLIENT-SIDE gating only. It
// governs which buttons are usable, not what the API will accept. The backend
// must enforce the same rules independently.




/**
 * Wraps a button so it renders disabled with a tooltip when the current role
 * lacks the required permission. Children compose as normal button content.
 *
 * THE MOST IMPORTANT COMPONENT IN THIS SECTION — it appears throughout the
 * file wherever an action is permission-controlled.
 *
 * The design decision worth noting: it DISABLES rather than HIDES. A hidden
 * button is indistinguishable from a missing feature or a bug, whereas a
 * disabled one with an explanatory tooltip tells the user the action exists and
 * what they would need in order to use it. `deniedReason` supplies that text.
 */
function Gated({
  perm,
  disabled,
  onClick,
  className,
  title,
  children,
  type,
}: {
  perm: Permission;
  disabled?: boolean;
  onClick?: () => void;
  className?: string;
  title?: string;
  type?: "button" | "submit" | "reset";
  children: ReactNode;
}) {
  const allowed = useCan(perm);
  const { role } = useAuth();
  return (
    <button
      // Defaults to "button" so the control cannot accidentally submit a
      // surrounding form — the same guard as in leaderboard-table.tsx.
      type={type ?? "button"}
      // BELT AND BRACES: the handler is stripped as well as the button being
      // disabled. Either alone would do, but dropping the handler means that
      // even a programmatic `.click()` cannot fire the action.
      onClick={allowed ? onClick : undefined}
      // Two independent reasons to disable: the role lacks the permission, or
      // the caller passed `disabled` for its own reasons (a request in flight).
      disabled={!allowed || disabled}
      // The tooltip explains a permission denial, otherwise defers to whatever
      // the caller supplied.
      title={!allowed ? deniedReason(role, perm) : title}
      // `|| undefined` OMITS the attribute when false rather than rendering
      // aria-disabled="false", which is the correct way to express "not
      // applicable" in ARIA.
      aria-disabled={!allowed || disabled || undefined}
      className={className}
    >
      {children}
    </button>
  );
}

/**
 * Guards the provider-keys panel at the SECTION level.
 *
 * A different strategy from `Gated`: rather than disabling controls, this
 * replaces the entire panel with an explanation. Appropriate here because API
 * keys are sensitive — showing a viewer the shape of the settings, greyed out,
 * would leak more than it helps.
 *
 * The rule of thumb: disable individual ACTIONS, replace whole SECTIONS.
 */
function ProviderKeysGate() {
  const canManage = useCan("keys:manage");
  const { role } = useAuth();
  if (!canManage) {
    return (
      <div className="panel p-5">
        <div className="mono-label">provider keys</div>
        <p className="mt-2 font-mono text-[12px] text-muted-foreground">
          Provider key management is restricted to privileged roles. Signed
          in as <span className="text-foreground">{roleLabel(role)}</span>.
        </p>
      </div>
    );
  }
  return <ProviderKeysPanel />;
}

/**
 * A one-line banner shown to viewers explaining why nothing is clickable.
 *
 * Without it, a read-only user sees a console full of disabled buttons and
 * reasonably concludes the app is broken. Stating the reason once at the top is
 * far better than making them hover each control for a tooltip.
 */
function RoleBanner() {
  const { role, isReadOnly } = useAuth();
  // Renders nothing for anyone who is not read-only.
  if (!isReadOnly) return null;
  return (
    <div className="mt-8 rounded-md border border-panel-border bg-panel/60 px-4 py-3 font-mono text-[12px] text-muted-foreground">
      <span className="mr-2 inline-flex items-center gap-1.5 rounded-full border border-panel-border bg-background px-2 py-0.5 text-[10px] uppercase tracking-[0.16em] text-foreground">
        {roleLabel(role)}
      </span>
      Read-only session. You can browse projects, runs, and review findings, but
      action buttons are disabled.
    </div>
  );
}





// ─── Navigation & chrome ───────────────────────────────────────────────────

// The sidebar links. These are ANCHOR hrefs (#workspace), not routes — the
// console is a single long page, so navigation scrolls within it rather than
// changing the URL path. That is why `Section` in index.tsx needs `scroll-mt`.
const NAV_ITEMS: { href: string; label: string; hint: string }[] = [
  { href: "#workspace", label: "Workspace", hint: "projects" },
  { href: "#pipeline", label: "Pipeline", hint: "run state" },
  { href: "#review", label: "Review", hint: "gates" },
  { href: "#export", label: "Export", hint: "bundle" },
];

/** The fixed left sidebar: logo, section links, role badge, sign-out. */
function ConsoleSidebar({
  user,
  onLogout,
  loggingOut,
}: {
  user: UserOut;
  onLogout: () => void;
  loggingOut: boolean;
}) {
  return (
    <aside className="sticky top-0 hidden h-screen w-[260px] shrink-0 flex-col border-r border-border bg-panel/70 px-5 py-6 lg:flex">
      <Logomark />

      <div className="mt-6 flex items-center gap-2 rounded-lg border border-primary/25 bg-primary/10 px-3 py-2 font-mono text-[10.5px] tracking-[0.12em] text-foreground/90">
        <Dot /> session active
      </div>

      <nav className="relative mt-7 pl-3">
        <p className="mono-label pb-3">pipeline</p>
        <span
          aria-hidden
          className="absolute bottom-2 left-[1.35rem] top-9 w-px bg-gradient-to-b from-primary/60 via-accent/40 to-transparent"
        />
        <div className="space-y-1">
          {NAV_ITEMS.map((item, i) => (
            <a
              key={item.href}
              href={item.href}
              className="group relative flex items-center gap-3 rounded-lg py-2 pl-0 pr-2 text-sm text-muted-foreground transition-colors hover:text-foreground"
            >
              <span className="relative z-10 grid h-6 w-6 shrink-0 place-items-center rounded-full border border-panel-border bg-background font-mono text-[10px] text-muted-foreground transition-colors group-hover:border-primary group-hover:text-primary">
                {i + 1}
              </span>
              <span className="min-w-0 flex-1 truncate">{item.label}</span>
              <span className="font-mono text-[10px] text-muted-foreground/60">
                {item.hint}
              </span>
            </a>
          ))}
        </div>
      </nav>

      <div className="mt-6 pl-3">
        <p className="mono-label pb-3">search harness</p>
        <Link
          to="/campaigns"
          className="flex items-center gap-3 rounded-lg border border-panel-border bg-background/60 px-3 py-2 text-sm text-muted-foreground hover:border-primary/50 hover:text-foreground"
        >
          <span className="font-mono text-[10px] text-primary">∞</span>
          <span className="min-w-0 flex-1 truncate">Campaigns</span>
          <span className="font-mono text-[10px] text-muted-foreground/60">archive</span>
        </Link>
      </div>

      <div className="mt-auto space-y-2 pt-6">
        <div className="rounded-xl border border-panel-border bg-background/70 px-3 py-2.5">
          <div className="flex min-w-0 items-center gap-2.5">
            <span className="grid h-8 w-8 shrink-0 place-items-center rounded-full border border-primary/40 bg-primary/10 font-mono text-[12px] uppercase text-primary">
              {(user.name || user.email || "?").slice(0, 1)}
            </span>
            <p
              className="min-w-0 flex-1 truncate text-[13px] text-foreground"
              title={user.name || user.email || undefined}
            >
              {user.name || (user.email ?? "").split("@")[0] || "account"}
            </p>
          </div>
          <div className="mt-2.5 grid grid-cols-2 gap-2">
            <Link
              to="/"
              aria-label="Back to site"
              title="Back to site"
              className="flex h-8 items-center justify-center gap-1.5 rounded-md border border-panel-border text-[11px] text-muted-foreground hover:border-primary/50 hover:text-foreground"
            >
              <Home className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">Site</span>
            </Link>
            <button
              onClick={onLogout}
              disabled={loggingOut}
              aria-label="Sign out"
              title="Sign out"
              className="flex h-8 items-center justify-center gap-1.5 rounded-md border border-panel-border text-[11px] text-muted-foreground hover:border-destructive/50 hover:text-foreground disabled:opacity-60"
            >
              <LogOut className="h-3.5 w-3.5 shrink-0" />
              <span className="truncate">Sign out</span>
            </button>
          </div>
        </div>
      </div>



    </aside>
  );
}


/** The sticky top bar: the active project's name and its current run state. */
function ConsoleTopbar({
  user,
  onLogout,
  loggingOut,
}: {
  user: UserOut;
  onLogout: () => void;
  loggingOut: boolean;
}) {
  return (
    <header className="sticky top-0 z-40 border-b border-border bg-background/95 lg:hidden">
      <div className="flex h-14 items-center justify-between gap-4 px-4">
        <Logomark />
        <div className="flex items-center gap-2">
          <span className="grid h-7 w-7 place-items-center rounded-full border border-primary/40 bg-primary/10 font-mono text-[11px] uppercase text-primary">
            {(user.name || user.email || "?").slice(0, 1)}
          </span>
          <button
            onClick={onLogout}
            disabled={loggingOut}
            aria-label="Sign out"
            title="Sign out"
            className="grid h-7 w-7 place-items-center rounded-md border border-panel-border bg-panel text-muted-foreground hover:text-foreground disabled:opacity-60"
          >
            <LogOut className="h-3.5 w-3.5" />
          </button>
        </div>

      </div>
    </header>
  );
}

/**
 * The "New project" button and its inline creation form.
 *
 * The button itself is `Gated` on `project:create`, so a viewer sees it
 * disabled with an explanatory tooltip rather than missing entirely.
 */
function NewProjectSheet({
  onCreate,
}: {
  onCreate: (project: ProjectOut) => void;
}) {
  const [open, setOpen] = useState(false);
  const [title, setTitle] = useState("");
  const [objective, setObjective] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!title.trim()) {
      toast.error("Title is required");
      return;
    }
    setBusy(true);
    try {
      const project = await createProject({
        title: title.trim(),
        objective: objective.trim() || undefined,
      });
      onCreate(project);
      setTitle("");
      setObjective("");
      setOpen(false);
      toast.success("Project created", { description: project.title });
    } catch (e) {
      toast.error(friendlyError(e, "Could not create project"));
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <Gated
        perm="project:create"
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
      >
        + New project
      </Gated>
    );
  }


  return (
    <div className="panel w-full max-w-xl overflow-hidden p-5">
      <div className="mono-label mb-3">new project</div>
      <div className="space-y-3">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Project title"
          className="w-full rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
        />
        <textarea
          value={objective}
          onChange={(e) => setObjective(e.target.value)}
          placeholder="Objective (optional)"
          rows={3}
          className="w-full resize-none rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
        />
        <div className="flex items-center gap-2">
          <button
            onClick={submit}
            disabled={busy}
            className="rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:opacity-95 disabled:opacity-60"
          >
            {busy ? "Creating…" : "Create"}
          </button>
          <button
            onClick={() => setOpen(false)}
            className="rounded-md border border-panel-border px-4 py-2 font-mono text-[11px] text-muted-foreground hover:border-foreground/40 hover:text-foreground"
          >
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

/** The horizontal project switcher. Selecting one drives `activeId` upward. */
function ProjectRail({
  projects,
  activeId,
  setActiveId,
}: {
  projects: ProjectOut[];
  activeId: number | null;
  setActiveId: (id: number) => void;
}) {
  if (projects.length === 0) return null;
  return (
    <div className="flex gap-2 overflow-x-auto pb-1 [scrollbar-width:none] [&::-webkit-scrollbar]:hidden">
      {projects.map((p) => {
        const active = p.id === activeId;
        return (
          <button
            key={p.id}
            onClick={() => setActiveId(p.id)}
            className={`group shrink-0 rounded-md border px-4 py-2.5 text-left transition-colors ${
              active
                ? "border-primary/50 bg-primary/10 text-foreground"
                : "border-panel-border bg-panel/50 text-muted-foreground hover:border-foreground/30 hover:text-foreground"
            }`}
          >
            <div className="flex items-center gap-2 font-mono text-[11px]">
              <StatusBadge status={p.status} />
            </div>
            <div className="mt-1 max-w-[220px] truncate font-mono text-[12px] text-foreground">
              {p.title}
            </div>
          </button>
        );
      })}
    </div>
  );
}


// ─── Agent connection panel ────────────────────────────────────────────────

// ─── Setup ─────────────────────────────────────────────────────────────────

/**
 * Point the app at a backend, and test that it responds.
 *
 * This is the UI over `getApiBase` / `setApiBase` in lib/api.ts — it writes the
 * base URL into localStorage, which takes precedence over the build-time
 * `VITE_API_BASE`. That is what lets a user aim the deployed app at their own
 * FastAPI runner without a rebuild.
 *
 * The "Test" button calls `health()` first, so a typo is caught here rather
 * than surfacing later as an unexplained failure on every other request.
 */
function AgentConnectionPanel({ onChange }: { onChange: () => void }) {
  const [value, setValue] = useState(getApiBase());
  const [status, setStatus] = useState<"idle" | "ok" | "fail" | "checking">("idle");
  const [detail, setDetail] = useState("");

  const save = () => {
    const cleaned = value.trim().replace(/\/$/, "");
    if (cleaned) setApiBase(cleaned);
    else clearApiBase();
    setValue(cleaned);
    toast.success(
      cleaned
        ? "Agent connection saved"
        : "Cleared — set VITE_API_BASE or an agent URL to reach a backend",
    );
    onChange();
  };

  const test = async () => {
    setStatus("checking");
    setDetail("");
    // Test against current value (not persisted state) so users can try before saving.
    const cleaned = value.trim().replace(/\/$/, "");
    try {
      const url = `${cleaned}/health`;
      const res = await fetch(url, { credentials: "include" });
      const text = await res.text();
      if (res.ok) {
        setStatus("ok");
        setDetail(text.slice(0, 140) || `${res.status} OK`);
      } else {
        setStatus("fail");
        setDetail(`${res.status} · ${text.slice(0, 100)}`);
      }
    } catch (e) {
      setStatus("fail");
      setDetail(friendlyError(e, "network error"));
    }
  };

  return (
    <div className="panel overflow-hidden">
      <SectionHeader
        icon="⌁"
        right={
          <span className="flex items-center gap-2 normal-case tracking-normal">
            <Dot
              tone={status === "ok" ? "success" : status === "fail" ? "error" : "muted"}
              pulse={status === "ok"}
            />
            {status === "checking"
              ? "testing…"
              : status === "ok"
                ? "backend reachable"
                : status === "fail"
                  ? "unreachable"
                  : hasRealBackend()
                    ? "backend configured"
                    : "no backend"}
          </span>
        }
      >
        AGENT CONNECTION
      </SectionHeader>
      <div className="grid gap-3 p-5 md:grid-cols-[1fr_auto_auto]">
        <input
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="https://agentlab.<your-tunnel>.example.com"
          className="min-w-0 rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
          spellCheck={false}
          autoComplete="off"
        />
        <button
          type="button"
          onClick={test}
          className="rounded-md border border-panel-border px-4 py-2 font-mono text-[11px] text-foreground hover:border-foreground/40"
        >
          Test
        </button>
        <button
          type="button"
          onClick={save}
          className="rounded-md bg-primary px-4 py-2 font-mono text-[11px] font-medium text-primary-foreground hover:opacity-95"
        >
          Save
        </button>
        {detail && (
          <div
            className={`md:col-span-3 rounded-md border px-3 py-2 font-mono text-[11px] ${
              status === "ok"
                ? "border-primary/30 bg-primary/5 text-foreground"
                : "border-destructive/30 bg-destructive/5 text-destructive"
            }`}
          >
            {detail}
          </div>
        )}
        <p className="md:col-span-3 font-mono text-[11px] text-muted-foreground">
          Base URL for the FastAPI agent (named tunnel or deployed host). A reachable backend is
          required — there is no offline demo mode, and every run uses real provider keys. Set{" "}
          <code className="text-foreground/80">VITE_API_BASE</code> at build time or paste one here.
          Cookies use <code className="text-foreground/80">credentials: include</code>; the backend
          must allow this origin.
        </p>
      </div>
    </div>
  );
}

// ─── Idea capture ──────────────────────────────────────────────────────────
// The first step of the workflow: the research question the agent will pursue.

/**
 * A textarea for the project's research idea.
 *
 * Note the `useEffect` below resetting local state when `project.id` changes.
 * That is a genuine pattern to know: this component copies a prop into state so
 * the field is editable, which means the copy goes stale when the prop changes.
 * Switching projects would otherwise leave the previous project's idea in the
 * box — and, worse, saving would write it to the wrong project.
 */
function IdeaCard({
  project,
  onIdeaSaved,
}: {
  project: ProjectOut;
  onIdeaSaved: (p: ProjectOut) => void;
}) {
  const [idea, setIdea] = useState(
    (project as ProjectOut & { idea_text?: string }).idea_text ?? "",
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    setIdea((project as ProjectOut & { idea_text?: string }).idea_text ?? "");
  }, [project.id]); // reset when switching projects
  // ^ Keyed on `project.id` rather than `project`, so an unrelated field
  //   changing on the same project does not wipe out the user's unsaved edits.
  //
  //   (React's own suggested alternative is to give the component a `key` prop
  //   at the call site — `<IdeaCard key={project.id} …>` — which remounts it
  //   and resets all state automatically. Worth knowing as the cleaner fix.)

  const save = async () => {
    if (!idea.trim()) {
      toast.error("Describe your research idea first");
      return;
    }
    setBusy(true);
    try {
      const updated = await saveIdea(project.id, idea.trim());
      onIdeaSaved(updated);
      toast.success("Idea saved");
    } catch (e) {
      toast.error(friendlyError(e, "Could not save idea"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel overflow-hidden">
      <SectionHeader icon="▤">IDEA CAPTURE</SectionHeader>
      <div className="p-5">
        <label className="mono-label">research idea</label>
        <textarea
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          rows={8}
          placeholder="Describe the research question, hypothesis, and success signals…"
          className="mt-2 w-full rounded-md border border-panel-border bg-background/50 p-4 font-sans text-sm leading-relaxed text-foreground focus:border-primary focus:outline-none"
        />
        <div className="mt-4 flex gap-3">
          <button
            onClick={save}
            disabled={busy}
            className="flex items-center gap-2 rounded-md border border-panel-border bg-background/60 px-4 py-2.5 font-mono text-xs text-foreground hover:border-foreground/40 disabled:opacity-60"
          >
            {busy ? "Saving…" : "⬒ Save Idea"}
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Brief ────────────────────────────────────────────────────────────────
// Gate G1 and the shared confirmation machinery used by all four gates.

type DecisionTone = "approve" | "warn" | "danger";

/**
 * THE SHARED CONFIRMATION DIALOG — used by every gate in this file.
 *
 * Rather than four bespoke dialogs for approving a brief, a cost, a revision,
 * and an export, there is one heavily-parameterised component. That is why the
 * prop list is long: each gate supplies its own wording, tone, summary rows,
 * and confirm handler.
 *
 * The design principle it encodes: a costly or irreversible action must be
 * DELIBERATE. Every gate therefore requires the user to type a note (see
 * `minChars`, default 10) before the confirm button enables. That note lands in
 * the audit trail, so the record shows not just what was approved but why —
 * and the typing requirement makes reflexive click-through much harder.
 */
function DecisionDialog({
  open,
  onOpenChange,
  tone,
  kicker,
  title,
  description,
  summary,
  confirmLabel,
  confirmingLabel,
  minChars = 10,
  placeholder = "Add a note for the audit trail (required)…",
  busy,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (v: boolean) => void;
  tone: DecisionTone;
  kicker: string;
  title: string;
  description: string;
  summary?: Array<{ label: string; value: React.ReactNode; tone?: DecisionTone }>;
  confirmLabel: string;
  confirmingLabel: string;
  minChars?: number;
  placeholder?: string;
  busy: boolean;
  onConfirm: (notes: string) => void | Promise<void>;
}) {
  const [notes, setNotes] = useState("");
  useEffect(() => {
    if (!open) setNotes("");
  }, [open]);

  const trimmed = notes.trim();
  const valid = trimmed.length >= minChars;
  const remaining = Math.max(0, minChars - trimmed.length);

  const toneRing =
    tone === "danger"
      ? "border-destructive/40"
      : tone === "warn"
        ? "border-warning/40"
        : "border-primary/40";
  const toneKicker =
    tone === "danger" ? "text-destructive" : tone === "warn" ? "text-warning" : "text-primary";
  const confirmBtn =
    tone === "danger"
      ? "bg-destructive text-destructive-foreground hover:opacity-95"
      : tone === "warn"
        ? "bg-warning text-warning-foreground hover:opacity-95"
        : "bg-primary text-primary-foreground hover:opacity-95";

  return (
    <Dialog open={open} onOpenChange={(v) => (!busy ? onOpenChange(v) : undefined)}>
      <DialogContent className={`max-w-lg border ${toneRing} bg-background p-0`}>
        <DialogHeader className="space-y-2 border-b border-panel-border p-6 text-left">
          <div className={`mono-label ${toneKicker}`}>{kicker}</div>
          <DialogTitle className="font-serif text-2xl leading-tight tracking-tight text-foreground">
            {title}
          </DialogTitle>
          <DialogDescription className="text-[13px] leading-relaxed text-muted-foreground">
            {description}
          </DialogDescription>
        </DialogHeader>

        {summary && summary.length > 0 && (
          <div className="grid grid-cols-2 gap-px bg-panel-border">
            {summary.map((s) => (
              <div key={s.label} className="bg-panel p-3">
                <div className="mono-label">{s.label}</div>
                <div
                  className={`mt-1 font-mono text-sm ${
                    s.tone === "danger"
                      ? "text-destructive"
                      : s.tone === "warn"
                        ? "text-warning"
                        : "text-foreground"
                  }`}
                >
                  {s.value}
                </div>
              </div>
            ))}
          </div>
        )}

        <div className="space-y-2 p-6">
          <label className="mono-label" htmlFor="decision-notes">
            reviewer notes · required
          </label>
          <textarea
            id="decision-notes"
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder={placeholder}
            rows={4}
            disabled={busy}
            autoFocus
            className="w-full resize-none rounded-md border border-panel-border bg-background/50 p-3 font-mono text-[12px] leading-relaxed text-foreground/90 focus:border-primary focus:outline-none disabled:opacity-60"
          />
          <div className="flex items-center justify-between font-mono text-[10.5px] tracking-[0.14em] text-muted-foreground">
            <span>{valid ? "✓ note captured" : `min ${minChars} chars · ${remaining} to go`}</span>
            <span>{trimmed.length} chars</span>
          </div>
        </div>

        <DialogFooter className="gap-2 border-t border-panel-border p-4">
          <button
            type="button"
            onClick={() => onOpenChange(false)}
            disabled={busy}
            className="rounded-md border border-panel-border bg-background/60 px-4 py-2.5 font-mono text-xs text-muted-foreground hover:border-foreground/40 hover:text-foreground disabled:opacity-60"
          >
            Cancel
          </button>
          <button
            type="button"
            disabled={!valid || busy}
            onClick={() => onConfirm(trimmed)}
            className={`rounded-md px-4 py-2.5 font-mono text-xs font-medium ${confirmBtn} disabled:opacity-50`}
          >
            {busy ? confirmingLabel : confirmLabel}
          </button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}


/**
 * GATE G1 — the research brief: generate, edit, approve or send back.
 *
 * The brief is the AI-written research plan. Approving it is the first human
 * checkpoint, and nothing expensive happens until it clears: `RunSetupCard`
 * below refuses to start without `briefApproved`.
 *
 * Note `busy` is a STRING here ("gen" | "save" | "approve" | "revise"), not a
 * boolean. Four actions share the card, and each button needs to show its own
 * spinner while all of them disable — one string does what four booleans would.
 * You will see the same trick in `RunSetupCard` and `RevisionGate`.
 */
function BriefCard({
  project,
  brief,
  setBrief,
}: {
  project: ProjectOut;
  brief: BriefOut | null;
  setBrief: (b: BriefOut | null) => void;
}) {
  const [busy, setBusy] = useState<string>("");
  const [draft, setDraft] = useState(brief?.content_markdown ?? "");

  useEffect(() => {
    setDraft(brief?.content_markdown ?? "");
  }, [brief?.id, brief?.version]);

  const doGenerate = async () => {
    setBusy("gen");
    try {
      const b = await generateBrief(project.id);
      setBrief(b);
      toast.success("Brief generated", { description: `v${b.version}` });
    } catch (e) {
      toast.error(friendlyError(e, "Could not generate brief"));
    } finally {
      setBusy("");
    }
  };

  const doSave = async () => {
    if (!brief) return;
    setBusy("save");
    try {
      const b = await updateBrief(brief.id, draft);
      setBrief(b);
      toast.success("Brief saved");
    } catch (e) {
      toast.error(friendlyError(e, "Could not save brief"));
    } finally {
      setBusy("");
    }
  };

  const [approveOpen, setApproveOpen] = useState(false);
  const [reviseOpen, setReviseOpen] = useState(false);

  const doApprove = async (notes: string) => {
    if (!brief) return;
    setBusy("approve");
    try {
      const b = await apiApproveBrief(brief.id, notes);
      setBrief(b);
      setApproveOpen(false);
      toast.success("Brief approved · gate G1 cleared", { description: notes });
    } catch (e) {
      toast.error(friendlyError(e, "Could not approve brief"));
    } finally {
      setBusy("");
    }
  };

  const doRevise = async (notes: string) => {
    if (!brief) return;
    setBusy("revise");
    try {
      const b = await generateBrief(project.id);
      setBrief(b);
      setReviseOpen(false);
      toast.warning("Revision requested · new draft generated", {
        description: notes,
      });
    } catch (e) {
      toast.error(friendlyError(e, "Could not request revision"));
    } finally {
      setBusy("");
    }
  };


  const wordCount = draft.trim() ? draft.trim().split(/\s+/).length : 0;
  const charCount = draft.length;
  const approved = Boolean(brief?.is_approved);
  const stateTone: DotTone = approved ? "success" : brief ? "warning" : "muted";
  const stateLabel = approved ? "approved" : brief ? "draft" : "empty";

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="brief · gate g1"
        title="Research brief"
        subtitle="Generate a structured brief from the saved idea, refine it, then approve to unlock the run gate."
        meta={
          <>
            <MetaChip tone={stateTone}>{stateLabel}</MetaChip>
            {brief && <MetaChip>v{brief.version}</MetaChip>}
          </>
        }
      />
      <div className="grid grid-cols-2 gap-px bg-panel-border sm:grid-cols-4">
        <div className="bg-panel p-3">
          <div className="mono-label">version</div>
          <div className="mt-1 font-mono text-sm text-foreground">
            {brief ? `v${brief.version}` : "—"}
          </div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">status</div>
          <div
            className={`mt-1 font-mono text-sm ${approved ? "text-primary" : brief ? "text-warning" : "text-muted-foreground"}`}
          >
            {stateLabel}
          </div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">words</div>
          <div className="mt-1 font-mono text-sm text-foreground">{wordCount}</div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">chars</div>
          <div className="mt-1 font-mono text-sm text-foreground">{charCount}</div>
        </div>
      </div>
      <div className="p-6">
        {brief ? (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="h-[300px] w-full resize-none rounded-md border border-panel-border bg-background/50 p-4 font-mono text-[12px] leading-relaxed text-foreground/90 focus:border-primary focus:outline-none"
            spellCheck={false}
            readOnly={approved}
          />
        ) : (
          <div className="grid h-[300px] place-items-center rounded-md border border-dashed border-panel-border bg-background/30 px-6 text-center font-mono text-xs text-muted-foreground">
            <div className="space-y-2">
              <div className="text-2xl text-foreground/40">◈</div>
              <div>No brief yet — generate one from the saved idea.</div>
            </div>
          </div>
        )}

        <div className="mt-5 flex flex-wrap items-center justify-between gap-3">
          <div className="font-mono text-[11px] text-muted-foreground">
            {approved
              ? "Gate G1 cleared · brief locked. Regenerate to iterate."
              : brief
                ? "Save edits, then approve to clear gate G1."
                : "Draft a brief to begin the workflow."}
          </div>
          <div className="flex flex-wrap justify-end gap-2">
            <button
              onClick={doGenerate}
              disabled={Boolean(busy)}
              className="flex items-center gap-2 rounded-md border border-panel-border bg-background/60 px-4 py-2.5 font-mono text-xs text-foreground hover:border-foreground/40 disabled:opacity-60"
            >
              {busy === "gen" ? "Generating…" : brief ? "⟳ Regenerate" : "⚗ Generate Brief"}
            </button>
            {brief && !approved && (
              <>
                <button
                  onClick={doSave}
                  disabled={Boolean(busy)}
                  className="flex items-center gap-2 rounded-md border border-panel-border bg-background/60 px-4 py-2.5 font-mono text-xs text-foreground hover:border-foreground/40 disabled:opacity-60"
                >
                  {busy === "save" ? "Saving…" : "⬒ Save edits"}
                </button>
                <button
                  onClick={() => setReviseOpen(true)}
                  disabled={Boolean(busy)}
                  className="flex items-center gap-2 rounded-md border border-warning/40 bg-warning/5 px-4 py-2.5 font-mono text-xs text-warning hover:bg-warning/10 disabled:opacity-60"
                >
                  {busy === "revise" ? "Revising…" : "✕ Request revision"}
                </button>
                <Gated
                  perm="brief:approve"
                  onClick={() => setApproveOpen(true)}
                  disabled={Boolean(busy)}
                  className="flex items-center gap-2 rounded-md bg-primary px-4 py-2.5 font-mono text-xs font-medium text-primary-foreground hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
                >
                  {busy === "approve" ? "Approving…" : "⊙ Approve brief"}
                </Gated>
              </>
            )}
            {approved && (
              <span className="inline-flex items-center gap-2 rounded-md bg-primary/10 px-4 py-2.5 font-mono text-xs text-primary">
                <Dot pulse={false} /> Approved
              </span>
            )}
          </div>
        </div>
      </div>

      <DecisionDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        tone="approve"
        kicker="decision · gate g1"
        title="Approve research brief"
        description="Approving locks the brief version and clears gate G1. Runs cannot start until this decision is recorded."
        summary={[
          { label: "version", value: brief ? `v${brief.version}` : "—" },
          { label: "words", value: wordCount },
          { label: "chars", value: charCount },
          { label: "state", value: "draft / approved", tone: "approve" },
        ]}
        confirmLabel="⊙ Confirm approval"
        confirmingLabel="Approving…"
        placeholder="Why is this brief ready to run? Scope, assumptions, risks reviewed…"
        busy={busy === "approve"}
        onConfirm={doApprove}
      />

      <DecisionDialog
        open={reviseOpen}
        onOpenChange={setReviseOpen}
        tone="warn"
        kicker="decision · gate g1"
        title="Request brief revision"
        description="This discards the current draft and generates a new one. The note is stored with the request so the next reviewer can see what changed."
        summary={[
          { label: "current", value: brief ? `v${brief.version}` : "—" },
          { label: "next", value: brief ? `v${brief.version + 1}` : "v1", tone: "warn" },
        ]}
        confirmLabel="✕ Request revision"
        confirmingLabel="Revising…"
        placeholder="What needs to change? Missing scope, unclear method, weak citations…"
        busy={busy === "revise"}
        onConfirm={doRevise}
      />
    </div>
  );
}


// ─── Run setup ─────────────────────────────────────────────────────────────

/**
 * GATE G2 — approve the cost, then start the run.
 *
 * ⚠ THE COMPONENT THAT SPENDS MONEY. At ~415 lines it is the largest in the
 * file. Read it carefully before changing anything.
 *
 * The flow it enforces:
 *   1. the user sets a budget threshold and toggles run options
 *   2. `doEstimate` asks the backend what it would cost — no spending yet
 *   3. `DecisionDialog` shows that estimate and demands a typed note
 *   4. `doStart` launches with `approved_cost: true`
 *
 * That flag is the crux. As documented in `lib/api.ts`, `startRun` will not
 * proceed without it, so a run cannot begin unless a human has seen the
 * estimate and confirmed. The gate is enforced by the API contract, not just
 * by this UI.
 *
 * `doStart` is also the best example of HTTP-status-aware error handling in the
 * codebase — see the 402/429/400 branches, which turn status codes into
 * actionable messages.
 */
function RunSetupCard({
  project,
  briefApproved,
  activeRun,
  capabilities,
  usage,
  onRunStarted,
  onRunCancelled,
}: {
  project: ProjectOut;
  briefApproved: boolean;
  activeRun: RunOut | null;
  capabilities: CapabilitiesOut | null;
  usage: UsageOut | null;
  onRunStarted: (run: RunOut) => void;
  onRunCancelled: (run: RunOut) => void;
}) {
  const [budget, setBudget] = useState("12");
  const [agentic, setAgentic] = useState(false);
  const [execute, setExecute] = useState(false);
  const [figures, setFigures] = useState(false);
  const [estimate, setEstimate] = useState<{ cost: number; assumptions: string } | null>(null);
  const [busy, setBusy] = useState<string>("");

  const running = activeRun && ["queued", "running", "revising"].includes(activeRun.status);

  const reasons = capabilities?.reasons ?? {};
  const runReady = capabilities ? capabilities.real_run_ready : false;
  const executeReady = capabilities ? capabilities.execute_ready : false;
  const figuresReady = capabilities ? capabilities.figures_ready : false;
  const orchestrationModes = capabilities?.orchestration_modes ?? [];
  const agenticReady = orchestrationModes.length === 0 || orchestrationModes.includes("agentic");
  const overBudgetCap = Boolean(usage?.over_budget);

  // Never send a config the server says it cannot honour.
  useEffect(() => {
    if (!executeReady) setExecute(false);
  }, [executeReady]);
  useEffect(() => {
    if (!figuresReady) setFigures(false);
  }, [figuresReady]);
  useEffect(() => {
    if (!agenticReady) setAgentic(false);
  }, [agenticReady]);

  // Builds the run configuration from the toggles. Extracted as a function
  // because BOTH `doEstimate` and `doStart` need it — and they must agree, or
  // the user would be quoted for one configuration and charged for another.
  const buildConfig = (): Record<string, unknown> => ({
    orchestration: agentic ? "agentic" : "linear",
    execute,
    figures,
  });

  /** Ask the backend what this run would cost. Spends nothing. */
  const doEstimate = async () => {
    setBusy("est");
    try {
      const e = await estimateRun(project.id, {
        budget_threshold: Number(budget) || 0,
        config_json: buildConfig(),
      });
      setEstimate({
        cost: e.cost_estimate,
        assumptions:
          typeof e.assumptions === "string" ? e.assumptions : JSON.stringify(e.assumptions),
      });
      toast.success(`Estimate: $${e.cost_estimate.toFixed(2)}`);
    } catch (err) {
      toast.error(friendlyError(err, "Could not estimate cost"));
    } finally {
      setBusy("");
    }
  };

  const [launchOpen, setLaunchOpen] = useState(false);
  const [cancelOpen, setCancelOpen] = useState(false);

  const doStart = async (notes: string) => {
    setBusy("start");
    try {
      const run = await startRun(project.id, {
        budget_threshold: Number(budget) || 0,
        // ⚠ THE SPEND GATE. The backend rejects the request without this flag,
        // and it is only ever set here — inside the confirm handler of a dialog
        // that required the user to read an estimate and type a note. That is
        // what makes "the user approved this cost" true rather than assumed.
        approved_cost: true,
        // "real" — there is no dry-run mode. See the header of lib/api.ts.
        mode: "real",
        config_json: buildConfig(),
      });
      onRunStarted(run);
      setLaunchOpen(false);
      toast.success(`Run started${agentic ? " · agentic" : ""}`, { description: notes });
    } catch (err) {
      // STATUS-AWARE ERROR HANDLING — the payoff of the custom `ApiError`
      // class in lib/api.ts, which carries the HTTP status alongside the
      // message. Each branch tells the user something different about what to
      // do next, which a single generic "request failed" cannot.
      const msg = friendlyError(err, "Could not start run");
      if (err instanceof ApiError) {
        // 402 Payment Required — out of budget. Nothing to retry.
        if (err.status === 402) toast.error("Monthly budget cap reached", { description: msg });
        // 429 Too Many Requests — retrying later will work.
        else if (err.status === 429) toast.error("Rate limited", { description: msg });
        // 400 — a precondition failed. The appended hint names the two usual
        // causes so the user is not left guessing which gate blocked them.
        else if (err.status === 400)
          toast.error("Gate check failed", {
            description:
              msg + " (needs an approved brief and an enabled provider key)",
          });
        else toast.error(msg);
      } else toast.error(msg);
    } finally {
      setBusy("");
    }
  };



  const doCancel = async (notes: string) => {
    if (!activeRun) return;
    setBusy("cancel");
    try {
      const r = await apiCancelRun(activeRun.id);
      onRunCancelled(r);
      setCancelOpen(false);
      toast.warning("Run cancelled", { description: notes });
    } catch (err) {
      toast.error(friendlyError(err, "Could not cancel run"));
    } finally {
      setBusy("");
    }
  };

  const budgetNum = Number(budget) || 0;
  const estCost = estimate?.cost ?? 0;
  const headroom = budgetNum - estCost;
  const overBudget = estimate ? estCost > budgetNum : false;
  const gateTone: DotTone = !briefApproved
    ? "warning"
    : overBudget || overBudgetCap
      ? "error"
      : !runReady
        ? "warning"
        : running
          ? "success"
          : "muted";
  const gateLabel = !briefApproved
    ? "awaiting brief"
    : overBudgetCap
      ? "monthly cap reached"
      : overBudget
        ? "over budget"
        : !runReady
          ? "provider key required"
          : running
            ? "run active"
            : "ready";
  const launchBlocked =
    !briefApproved || Boolean(running) || Boolean(busy) || overBudget || !runReady || overBudgetCap;

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="run gate · g2"
        title="Budget & launch"
        subtitle="Set the ceiling, estimate cost, then approve launch. Every run is real: it calls your enabled provider and spends credits. Gate G2 opens once a brief is approved, capability checks pass, and cost is within budget."
        meta={
          <>
            <MetaChip tone={gateTone}>{gateLabel}</MetaChip>
            <MetaChip tone="warning">real run · spends $</MetaChip>
          </>
        }
      />
      <div className="grid grid-cols-2 gap-px bg-panel-border sm:grid-cols-4">
        <div className="bg-panel p-3">
          <div className="mono-label">budget</div>
          <div className="mt-1 font-mono text-sm text-foreground">${budgetNum.toFixed(2)}</div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">estimate</div>
          <div
            className={`mt-1 font-mono text-sm ${overBudget ? "text-destructive" : estimate ? "text-foreground" : "text-muted-foreground"}`}
          >
            {estimate ? `$${estCost.toFixed(2)}` : "—"}
          </div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">headroom</div>
          <div
            className={`mt-1 font-mono text-sm ${overBudget ? "text-destructive" : "text-foreground"}`}
          >
            {estimate ? `${headroom >= 0 ? "+" : ""}$${headroom.toFixed(2)}` : "—"}
          </div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">providers</div>
          <div
            className={`mt-1 truncate font-mono text-sm ${runReady ? "text-foreground" : "text-warning"}`}
            title={capabilities?.enabled_providers?.join(", ")}
          >
            {capabilities
              ? capabilities.enabled_providers.length > 0
                ? capabilities.enabled_providers.join(", ")
                : "none enabled"
              : "—"}
          </div>
        </div>
      </div>

      <div className="space-y-6 p-6">
        <div className="grid gap-4 md:grid-cols-2">
          <div>
            <label className="mono-label">budget threshold (usd)</label>
            <input
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              inputMode="decimal"
              className="mt-2 w-full rounded-md border border-panel-border bg-background/50 px-3 py-2.5 font-mono text-sm text-foreground focus:border-primary focus:outline-none"
            />
          </div>
          <div>
            <label className="mono-label">execution</label>
            <div
              className={`mt-2 rounded-md border px-3 py-2.5 font-mono text-xs ${
                runReady
                  ? "border-destructive/40 bg-destructive/5 text-destructive"
                  : "border-warning/40 bg-warning/5 text-warning"
              }`}
            >
              {runReady
                ? "Real run · spends provider credits"
                : reasons.real_run_ready || "Enable a provider key to unlock runs"}
            </div>
          </div>
        </div>

        <div>
          <label className="mono-label">orchestration &amp; tools</label>
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            <ToggleTile
              on={agentic}
              onChange={agenticReady ? setAgentic : () => {}}
              icon="✦"
              label="Agentic"
              caption={
                agenticReady
                  ? "planner + tool loop"
                  : reasons.orchestration || "agentic loop unavailable"
              }
            />
            <ToggleTile
              on={execute}
              onChange={executeReady ? setExecute : () => {}}
              icon="⚙"
              label="Execute on RunPod"
              caption={
                executeReady
                  ? "run generated code"
                  : reasons.execute_ready || "execution backend unavailable"
              }
              tone="danger"
            />
            <ToggleTile
              on={figures}
              onChange={figuresReady ? setFigures : () => {}}
              icon="◨"
              label="Generate figures"
              caption={
                figuresReady
                  ? "via Gemini image"
                  : reasons.figures_ready || "figure model unavailable"
              }
            />
          </div>
        </div>

        {estimate && (
          <div
            className={`rounded-md border px-4 py-3 font-mono text-[12px] ${
              overBudget
                ? "border-destructive/40 bg-destructive/10 text-destructive"
                : "border-primary/30 bg-primary/5 text-foreground"
            }`}
          >
            <div className="flex items-center justify-between">
              <span className="mono-label">
                {overBudget ? "over budget · adjust ceiling" : "estimated cost"}
              </span>
              <strong>${estCost.toFixed(2)}</strong>
            </div>
            {estimate.assumptions && (
              <div className="mt-1.5 text-[11px] text-muted-foreground">{estimate.assumptions}</div>
            )}
          </div>
        )}

        <div className="flex flex-wrap items-center justify-between gap-3 border-t border-panel-border pt-5">
          <div className="flex items-center gap-2 font-mono text-[12px]">
            {!briefApproved ? (
              <>
                <Dot tone="warning" />
                <span className="text-warning">Approve a brief before estimating or starting runs.</span>
              </>
            ) : activeRun ? (
              <>
                <Dot
                  tone={
                    activeRun.status === "running" || activeRun.status === "completed"
                      ? "success"
                      : activeRun.status === "failed" || activeRun.status === "cancelled"
                        ? "error"
                        : "warning"
                  }
                />
                <span className="text-foreground">
                  run #{activeRun.id} · {activeRun.status} · $
                  {activeRun.actual_cost.toFixed(2)} of ${activeRun.budget_threshold.toFixed(2)}
                </span>
              </>
            ) : (
              <>
                <Dot />
                <span className="text-foreground">Gate cleared · ready to launch a run.</span>
              </>
            )}
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              disabled={!briefApproved || Boolean(busy) || !runReady}
              onClick={doEstimate}
              className="flex items-center gap-2 rounded-md border border-panel-border bg-background/60 px-4 py-2.5 font-mono text-xs text-muted-foreground disabled:opacity-50 enabled:hover:border-foreground/40 enabled:hover:text-foreground"
            >
              {busy === "est" ? "Estimating…" : "⊡ Estimate"}
            </button>
            <Gated
              perm="run:start"
              disabled={launchBlocked}
              onClick={() => setLaunchOpen(true)}
              className="flex items-center gap-2 rounded-md bg-destructive px-4 py-2.5 font-mono text-xs font-medium text-destructive-foreground hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {busy === "start" ? "Starting…" : "Approve & start real run"}
            </Gated>
            {running && (
              <Gated
                perm="run:cancel"
                onClick={() => setCancelOpen(true)}
                disabled={busy === "cancel"}
                className="flex items-center gap-2 rounded-md border border-destructive/40 px-4 py-2.5 font-mono text-xs text-destructive hover:border-destructive disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === "cancel" ? "Cancelling…" : "■ Cancel run"}
              </Gated>
            )}
          </div>
        </div>
      </div>

      <DecisionDialog
        open={launchOpen}
        onOpenChange={setLaunchOpen}
        tone="danger"
        kicker="decision · gate g2"
        title="Approve real-money launch"
        description="This spends real credits on the connected agent using your saved provider key. There is no mock path. Your note is recorded with the run."
        summary={[
          { label: "budget", value: `$${budgetNum.toFixed(2)}` },
          {
            label: "estimate",
            value: estimate ? `$${estCost.toFixed(2)}` : "not estimated",
            tone: overBudget ? "danger" : undefined,
          },
          {
            label: "headroom",
            value: estimate ? `${headroom >= 0 ? "+" : ""}$${headroom.toFixed(2)}` : "—",
            tone: overBudget ? "danger" : undefined,
          },
          {
            label: "monthly remaining",
            value: usage ? `$${usage.remaining.toFixed(2)}` : "—",
            tone: overBudgetCap ? "danger" : undefined,
          },
          {
            label: "orchestration",
            value: agentic ? "agentic loop" : "linear",
          },
          {
            label: "code execution",
            value: execute ? "RunPod enabled" : "disabled",
            tone: execute ? "danger" : undefined,
          },
        ]}
        confirmLabel="Confirm & spend"
        confirmingLabel="Starting…"
        placeholder="Why is this real-money spend authorized? Reviewer, budget owner, expected outcome…"
        busy={busy === "start"}
        onConfirm={doStart}
      />

      <DecisionDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        tone="danger"
        kicker="decision · run"
        title="Cancel active run"
        description="Cancelling stops the orchestrator immediately. Partial artifacts are kept but the run is marked cancelled."
        summary={
          activeRun
            ? [
                { label: "run", value: `#${activeRun.id}` },
                { label: "status", value: activeRun.status, tone: "warn" },
                { label: "spent", value: `$${activeRun.actual_cost.toFixed(2)}` },
                { label: "budget", value: `$${activeRun.budget_threshold.toFixed(2)}` },
              ]
            : undefined
        }
        confirmLabel="■ Confirm cancellation"
        confirmingLabel="Cancelling…"
        placeholder="Reason for cancelling — cost overrun, wrong scope, provider issue…"
        busy={busy === "cancel"}
        onConfirm={doCancel}
      />
    </div>
  );
}

// ─── Live logs ─────────────────────────────────────────────────────────────

// ─── Bento header shared by run-session cards ──────────────────────────────

// ─── Bento-grid atoms ──────────────────────────────────────────────────────
// A second small set of presentational pieces, used by the live-execution
// panels below. ("Bento" is the layout style: a grid of variously-sized boxes.)

/** A panel header with a title, optional kicker, and a right-hand slot. */
function BentoHead({
  kicker,
  title,
  subtitle,
  meta,
}: {
  kicker: string;
  title: string;
  subtitle?: string;
  meta?: React.ReactNode;
}) {
  return (
    <div className="flex flex-wrap items-end justify-between gap-4 border-b border-panel-border px-6 py-5">
      <div className="min-w-0">
        <div className="mono-label">{kicker}</div>
        <h3 className="mt-1.5 font-serif text-2xl leading-[1.1] tracking-tight text-foreground">
          {title}
        </h3>
        {subtitle && (
          <p className="mt-1 max-w-xl text-[13px] leading-relaxed text-muted-foreground">
            {subtitle}
          </p>
        )}
      </div>
      {meta && <div className="flex shrink-0 items-center gap-2">{meta}</div>}
    </div>
  );
}

/** A pill with a status dot. Local twin of `Chip` in campaign/primitives.tsx. */
function MetaChip({
  tone = "muted",
  children,
}: {
  tone?: DotTone;
  children: React.ReactNode;
}) {
  return (
    <span className="inline-flex items-center gap-2 rounded-full border border-panel-border bg-background/50 px-3 py-1 font-mono text-[10.5px] tracking-[0.14em] text-muted-foreground">
      <Dot tone={tone} pulse={tone === "success" || tone === "warning"} />
      {children}
    </span>
  );
}

/**
 * A labelled metric tile.
 *
 * Note this is character-for-character the same component as `Stat` in
 * `components/campaign/primitives.tsx` — a clear duplication, and one of the
 * easier wins if this file is ever split up: import the shared one instead.
 */
function StatTile({
  label,
  value,
  hint,
}: {
  label: string;
  value: React.ReactNode;
  hint?: string;
}) {
  return (
    <div className="rounded-md border border-panel-border bg-background/40 p-3">
      <div className="mono-label">{label}</div>
      <div className="mt-1.5 font-mono text-[15px] text-foreground">{value}</div>
      {hint && <div className="mt-0.5 font-mono text-[10px] text-muted-foreground">{hint}</div>}
    </div>
  );
}

// ─── Live logs ─────────────────────────────────────────────────────────────
// The execution phase: what the agent is doing right now.

/**
 * The streaming log viewer.
 *
 * Purely presentational — the WebSocket lives in `ConsolePage`, which passes
 * the accumulated `events` down. Keeping the socket in the parent means the log
 * survives this component unmounting (when a panel is collapsed, say) and only
 * one connection is ever open.
 */
function LiveLogs({ run, events }: { run: RunOut | null; events: LogEvent[] }) {
  // Auto-scroll to the newest line — the same ref-plus-effect pattern as
  // `SearchTracePanel` in campaigns.$id.tsx, and for the same reason: scroll
  // position is not expressible as React state.
  const scroller = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scroller.current) scroller.current.scrollTop = scroller.current.scrollHeight;
  }, [events.length]);

  // Tally warnings and errors in one pass so the header can summarise the
  // stream without the user reading it. Memoised because a busy run re-renders
  // this component on every incoming frame.
  const counts = useMemo(() => {
    let warn = 0;
    let err = 0;
    let status = 0;
    for (const e of events) {
      if (e.type === "status") status++;
      else if (e.level === "warning") warn++;
      else if (e.level === "error") err++;
    }
    return { total: events.length, warn, err, status };
  }, [events]);

  const running = (run?.status ?? "").toLowerCase() === "running";
  const streamTone: DotTone = running ? "success" : run ? "muted" : "muted";

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="transcript · gate g2"
        title="Live session stream"
        subtitle="WebSocket feed from the orchestrator — status transitions, tool calls, and observations arrive in real time."
        meta={
          run ? (
            <>
              <MetaChip tone={streamTone}>
                {running ? "streaming" : "idle"}
              </MetaChip>
              <MetaChip>run #{run.id}</MetaChip>
            </>
          ) : (
            <MetaChip>no run</MetaChip>
          )
        }
      />
      <div className="grid grid-cols-2 gap-px bg-panel-border sm:grid-cols-4">
        <div className="bg-panel p-3">
          <div className="mono-label">events</div>
          <div className="mt-1 font-mono text-sm text-foreground">{counts.total}</div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">status</div>
          <div className="mt-1 font-mono text-sm text-foreground">{counts.status}</div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">warnings</div>
          <div
            className={`mt-1 font-mono text-sm ${counts.warn ? "text-warning" : "text-foreground"}`}
          >
            {counts.warn}
          </div>
        </div>
        <div className="bg-panel p-3">
          <div className="mono-label">errors</div>
          <div
            className={`mt-1 font-mono text-sm ${counts.err ? "text-destructive" : "text-foreground"}`}
          >
            {counts.err}
          </div>
        </div>
      </div>
      <div
        ref={scroller}
        className="h-[360px] overflow-y-auto bg-background/40 p-5 font-mono text-[12px] leading-relaxed"
      >
        {events.length === 0 && (
          <div className="grid h-full place-items-center text-center text-muted-foreground">
            <div>
              <div className="font-serif text-2xl text-foreground/80">
                {run ? "Waiting for events…" : "Stream is quiet."}
              </div>
              <p className="mt-2 max-w-xs text-[11px]">
                {run
                  ? "The orchestrator will emit status and log frames as the agent works."
                  : "Start a run to open the WebSocket and stream tool activity here."}
              </p>
            </div>
          </div>
        )}
        {events.map((ev, i) => {
          if (ev.type === "status") {
            return (
              <div
                key={i}
                className="my-1 flex items-center gap-2 border-l-2 border-warning/60 pl-3 text-warning"
              >
                <Dot tone="warning" pulse={false} /> status: {ev.status}
              </div>
            );
          }
          const tone =
            ev.level === "error"
              ? "text-destructive"
              : ev.level === "warning"
                ? "text-warning"
                : "text-foreground/90";
          return (
            <div key={i} className="grid grid-cols-[48px_60px_1fr] gap-3">
              <span className="text-muted-foreground">
                {String(ev.sequence ?? i + 1).padStart(3, "0")}
              </span>
              <span className="text-primary/80">{ev.level ?? "info"}</span>
              <span className={tone}>{ev.message}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Artifacts & draft ─────────────────────────────────────────────────────

/**
 * Figures, code, and the draft paper produced by a run.
 *
 * `exportApproved` gates the download controls: artifacts are viewable while a
 * run is in progress, but nothing can be exported until gate G4 clears in
 * `FinalExport` below. See `artifactUrl` in lib/api.ts for how each artifact's
 * URL is resolved — the backend can describe one in four different ways.
 */
function ArtifactsAndDraft({ run, exportApproved }: { run: RunOut | null; exportApproved: boolean }) {
  const artifacts = run?.artifacts ?? [];
  const figures = artifacts.filter((a) => a.kind === "figure");
  const others = artifacts.filter((a) => a.kind !== "figure");
  const draft = run?.draft_markdown ?? "";
  const draftWords = draft.trim() ? draft.trim().split(/\s+/).length : 0;

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="deliverables · gate g2"
        title="Artifacts & draft manuscript"
        subtitle="Every figure, log, and file the run produced — plus the working draft the reviewer will read."
        meta={
          <>
            <MetaChip tone={run ? "info" : "muted"}>
              {artifacts.length} artifact{artifacts.length === 1 ? "" : "s"}
            </MetaChip>
            <MetaChip>{draftWords ? `${draftWords} words` : "no draft"}</MetaChip>
            <MetaChip tone={exportApproved ? "success" : "warning"}>
              {exportApproved ? "exports unlocked" : "exports locked · G3"}
            </MetaChip>
          </>
        }
      />
      <div className="grid grid-cols-3 gap-px bg-panel-border">
        <div className="bg-panel p-4">
          <div className="mono-label">actual cost</div>
          <div className="mt-1 font-mono text-[15px] text-foreground">
            ${(run?.actual_cost ?? 0).toFixed(2)}
          </div>
        </div>
        <div className="bg-panel p-4">
          <div className="mono-label">files</div>
          <div className="mt-1 font-mono text-[15px] text-foreground">{others.length}</div>
        </div>
        <div className="bg-panel p-4">
          <div className="mono-label">figures</div>
          <div className="mt-1 font-mono text-[15px] text-foreground">{figures.length}</div>
        </div>
      </div>
      <div className="grid gap-6 p-6 lg:grid-cols-[1fr_1.4fr]">
        <div className="space-y-5">
          <div>
            <div className="mono-label mb-2">files</div>
            <div className="flex flex-wrap gap-2">
              {others.length === 0 && (
                <span className="font-mono text-[11px] text-muted-foreground">
                  No files yet.
                </span>
              )}
              {others.map((a, i) => {
                const url = artifactUrl(a);
                const locked = !exportApproved;
                const cls =
                  "inline-flex items-center gap-2 rounded-full border border-panel-border bg-background/60 px-3 py-1 font-mono text-[11px] text-foreground";
                const tag = (
                  <>
                    <span className="text-muted-foreground">{a.kind}:</span> {a.name}
                    {locked && <span className="text-warning">· locked</span>}
                  </>
                );
                return url && !locked ? (
                  <a
                    key={`${a.name}-${i}`}
                    href={url}
                    target="_blank"
                    rel="noreferrer"
                    className={`${cls} hover:border-foreground/40`}
                  >
                    {tag}
                  </a>
                ) : (
                  <span
                    key={`${a.name}-${i}`}
                    className={`${cls} ${locked ? "opacity-70" : ""}`}
                    title={locked ? "Approve final export (Gate G3) to download" : undefined}
                  >
                    {tag}
                  </span>
                );
              })}
            </div>
            {!exportApproved && others.length > 0 && (
              <p className="mt-3 font-mono text-[10.5px] leading-relaxed text-warning">
                Downloads are locked until Gate G3 (final export) is approved below.
              </p>
            )}
          </div>
          {figures.length > 0 && (
            <div>
              <div className="mono-label mb-2">figures</div>
              <div className="grid gap-3 sm:grid-cols-2">
                {figures.map((a, i) => {
                  const url = artifactUrl(a);
                  return (
                    <figure
                      key={`${a.name}-${i}`}
                      className="overflow-hidden rounded-md border border-panel-border bg-background/40"
                    >
                      {url ? (
                        <img
                          src={url}
                          alt={a.name}
                          className="block h-auto w-full object-contain"
                          loading="lazy"
                        />
                      ) : (
                        <div className="grid aspect-video place-items-center font-mono text-[11px] text-muted-foreground">
                          {a.name}
                        </div>
                      )}
                      <figcaption className="border-t border-panel-border px-3 py-1.5 font-mono text-[10px] text-muted-foreground">
                        {a.name}
                      </figcaption>
                    </figure>
                  );
                })}
              </div>
            </div>
          )}
        </div>
        <div className="flex flex-col">
          <div className="mono-label mb-2">draft manuscript</div>
          <pre className="h-[320px] flex-1 overflow-auto rounded-md border border-panel-border bg-background/50 p-4 font-mono text-[12px] leading-relaxed whitespace-pre-wrap text-foreground/90">
            {draft || "Draft will appear here once the run produces one."}
          </pre>
        </div>
      </div>
    </div>
  );
}

// ─── Agentic decision log ──────────────────────────────────────────────────

/**
 * The agent's step-by-step actions: which tool it called and what happened.
 *
 * The console's equivalent of the trace viewer in
 * `components/campaign/candidate-detail.tsx` — the audit trail that makes a
 * result checkable rather than merely reported.
 */
function AgenticActionsCard({ run }: { run: RunOut | null }) {
  const actions = run?.analysis_json?.actions ?? [];
  if (!run || actions.length === 0) return null;
  const tools = new Set(actions.map((a) => a.tool ?? a.action ?? "step"));
  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="analysis · agentic trace"
        title="Decision trace"
        subtitle="Ordered tool calls the agent chose, with the reasoning and I/O behind each move."
        meta={
          <>
            <MetaChip tone="info">{actions.length} steps</MetaChip>
            <MetaChip>{tools.size} tools</MetaChip>
          </>
        }
      />
      <ol className="divide-y divide-panel-border">
        {actions.map((a, i) => (
          <li
            key={i}
            className="grid grid-cols-[56px_1fr] gap-4 px-6 py-4 font-mono text-[12px]"
          >
            <span className="text-muted-foreground">
              #{String(a.step ?? i + 1).padStart(2, "0")}
            </span>
            <div className="space-y-1.5">
              <div className="flex flex-wrap items-baseline gap-2">
                <span className="rounded-sm border border-primary/30 bg-primary/5 px-1.5 py-0.5 text-[10.5px] tracking-[0.1em] text-primary">
                  {(a.tool ?? a.action ?? "step").toUpperCase()}
                </span>
                {a.reason && (
                  <span className="text-[12px] leading-snug text-foreground/85">
                    {a.reason}
                  </span>
                )}
              </div>
              {(a.input !== undefined || a.output !== undefined) && (
                <details className="text-[11px] text-muted-foreground">
                  <summary className="cursor-pointer hover:text-foreground">
                    inspect i/o
                  </summary>
                  <pre className="mt-2 max-h-56 overflow-auto rounded-md border border-panel-border bg-background/50 p-3 whitespace-pre-wrap break-all text-foreground/80">
                    {JSON.stringify({ input: a.input, output: a.output }, null, 2)}
                  </pre>
                </details>
              )}
            </div>
          </li>
        ))}
      </ol>
    </div>
  );
}

// ─── Execution results (RunPod) ────────────────────────────────────────────

/** Measured outcomes of the run: metrics the agent produced and recorded. */
function ExecutionResultsCard({ run }: { run: RunOut | null }) {
  const exec = run?.analysis_json?.execution;
  if (!run || !exec) return null;
  const okTone: DotTone =
    exec.status === "ok" || exec.exit_code === 0
      ? "success"
      : exec.status === "error" || (exec.exit_code ?? 0) !== 0
        ? "error"
        : "warning";
  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="execution · runpod sandbox"
        title="Code execution results"
        subtitle="Sandboxed program output, cost, and duration returned by the RunPod endpoint."
        meta={
          <MetaChip tone={okTone}>
            {exec.status ?? `exit ${exec.exit_code ?? "?"}`}
          </MetaChip>
        }
      />
      <div className="grid grid-cols-2 gap-px bg-panel-border sm:grid-cols-3">
        <div className="bg-panel p-4">
          <StatTile label="exit" value={exec.exit_code ?? "—"} />
        </div>
        <div className="bg-panel p-4">
          <StatTile
            label="duration"
            value={exec.duration_ms != null ? `${exec.duration_ms} ms` : "—"}
          />
        </div>
        <div className="bg-panel p-4">
          <StatTile
            label="cost"
            value={exec.cost != null ? `$${exec.cost.toFixed(4)}` : "—"}
          />
        </div>
      </div>
      <div className="space-y-4 p-6">
        {exec.stdout && (
          <div>
            <div className="mono-label">stdout</div>
            <pre className="mt-2 max-h-64 overflow-auto rounded-md border border-panel-border bg-background/50 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground/90">
              {exec.stdout}
            </pre>
          </div>
        )}
        {exec.stderr && (
          <div>
            <div className="mono-label">stderr</div>
            <pre className="mt-2 max-h-40 overflow-auto rounded-md border border-destructive/40 bg-destructive/5 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-destructive">
              {exec.stderr}
            </pre>
          </div>
        )}
        {exec.results !== undefined && (
          <div>
            <div className="mono-label">results.json</div>
            <pre className="mt-2 max-h-64 overflow-auto rounded-md border border-panel-border bg-background/50 p-3 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-foreground/90">
              {typeof exec.results === "string"
                ? exec.results
                : JSON.stringify(exec.results, null, 2)}
            </pre>
          </div>
        )}
      </div>
    </div>
  );
}


// ─── Reviewer panel ────────────────────────────────────────────────────────

// ─── Review ────────────────────────────────────────────────────────────────

/**
 * Automated review findings for the completed run.
 *
 * These are the machine checks a human reviewer reads before approving the
 * export — the same findings surfaced by `listReviews` in lib/api.ts.
 */
function ReviewerPanel({ findings }: { findings: ReviewFindingOut[] }) {
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
            <div className="font-serif text-2xl text-foreground/80">
              No reviewer findings yet.
            </div>
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
                  {items.map((f, i) => (
                    <div
                      key={i}
                      className="border-t border-panel-border pt-3 first:border-t-0 first:pt-0"
                    >
                      <p className="text-sm leading-relaxed text-foreground/90">{f.finding}</p>
                      {f.suggested_fix && (
                        <p className="mt-2 font-mono text-[11px] leading-relaxed text-muted-foreground">
                          fix: {f.suggested_fix}
                        </p>
                      )}
                    </div>
                  ))}
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
type TimelineTone = "muted" | "pending" | "active" | "success" | "warning" | "danger";

/**
 * One entry on the timeline.
 *
 * `tone` and `state` look redundant but are not: `state` is the SEMANTIC
 * position in the workflow (done / current / pending / skipped), while `tone`
 * is how it should LOOK. They usually correlate, but a "done" step that failed
 * still needs a danger tone, so the two are kept independent.
 */
interface TimelineEvent {
  key: string;
  gate?: string;
  label: string;
  detail: string;
  actor: string;
  timestamp: string | null;
  tone: TimelineTone;
  state: "done" | "current" | "pending" | "skipped";
  note?: string;
  sessionRef?: string | null;
  runId?: number | null;
  artifacts?: ArtifactOut[];
  meta?: { label: string; value: string }[];
}

/**
 * Format a timestamp for display, degrading gracefully at every step.
 *
 * Three guards, because timestamps come from a backend and cannot be trusted:
 * null yields an em dash, an unparseable string is shown verbatim rather than
 * as "Invalid Date", and only a genuine date is formatted.
 */
function formatTs(ts: string | null): string {
  if (!ts) return "—";
  const d = new Date(ts);
  // `new Date("nonsense")` does not throw — it returns an Invalid Date whose
  // `getTime()` is NaN. This is the only reliable way to detect that.
  if (Number.isNaN(d.getTime())) return ts;
  // `undefined` as the locale means "use the viewer's own locale", so dates
  // render in the reader's regional format rather than a hard-coded one.
  return d.toLocaleString(undefined, {
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/**
 * Map a tone to its three Tailwind class strings.
 *
 * A `switch` returning full class strings, rather than the lookup object used
 * elsewhere. Same underlying constraint either way: every class must appear
 * LITERALLY in the source for Tailwind's scanner to generate it — see the note
 * in campaign/primitives.tsx `Dot`.
 *
 * Three variants per tone because each timeline row has three coloured parts:
 * the dot, the ring around it, and the chip beside it.
 */
function toneStyles(tone: TimelineTone): { dot: string; ring: string; chip: string } {
  switch (tone) {
    case "success":
      return {
        dot: "bg-primary",
        ring: "ring-primary/30",
        chip: "border-primary/30 bg-primary/10 text-foreground",
      };
    case "active":
      return {
        dot: "bg-primary",
        ring: "ring-primary/40",
        chip: "border-primary/40 bg-primary/10 text-foreground",
      };
    case "warning":
      return {
        dot: "bg-amber-500",
        ring: "ring-amber-500/30",
        chip: "border-amber-500/30 bg-amber-500/10 text-foreground",
      };
    case "danger":
      return {
        dot: "bg-destructive",
        ring: "ring-destructive/30",
        chip: "border-destructive/30 bg-destructive/10 text-foreground",
      };
    case "pending":
      return {
        dot: "bg-muted-foreground/40",
        ring: "ring-panel-border",
        chip: "border-panel-border bg-panel/60 text-muted-foreground",
      };
    default:
      return {
        dot: "bg-muted-foreground/30",
        ring: "ring-panel-border",
        chip: "border-panel-border bg-panel/40 text-muted-foreground",
      };
  }
}

/**
 * Builds and renders the timeline from the current run state.
 *
 * The long body is mostly one large derivation: it inspects the project, brief,
 * and run and produces the ordered `TimelineEvent[]`. The timeline is therefore
 * DERIVED, never stored — which is what stops it drifting out of sync with the
 * data it describes.
 */
function StatusTimeline({
  project,
  brief,
  activeRun,
  findings,
  exported,
  actorName,
  projects,
  onSelectProject,
}: {
  project: ProjectOut | null;
  brief: BriefOut | null;
  activeRun: RunOut | null;
  findings: ReviewFindingOut[];
  exported: boolean;
  actorName: string;
  projects: ProjectOut[];
  onSelectProject: (id: number) => void;
}) {
  const [gateFilter, setGateFilter] = useState<string[]>([]);
  const [sessionFilter, setSessionFilter] = useState<"all" | "current" | "pre-run">("all");
  const search = Route.useSearch();
  const navigate = useNavigate();
  const selectedKey = search.tl ?? null;
  const setSelectedKey = (key: string | null) => {
    void navigate({
      to: "/console",
      search: (prev: { tl?: string }) => ({ ...prev, tl: key ?? undefined }),
      replace: key === null,
      resetScroll: false,
    });
  };

  const events: TimelineEvent[] = useMemo(() => {
    const list: TimelineEvent[] = [];
    const runArtifacts = activeRun?.artifacts ?? [];
    const sessionRef = activeRun?.session_id ?? null;
    const runId = activeRun?.id ?? null;

    // Project created
    list.push({
      key: "project",
      gate: "init",
      label: "Project opened",
      detail: project ? `#${project.id} · ${project.title}` : "no project selected",
      actor: project ? actorName : "—",
      timestamp: null,
      tone: project ? "success" : "muted",
      state: project ? "done" : "pending",
      note: project?.objective
        ? `Objective: ${project.objective}`
        : "Workspace opened for this project.",
      meta: project
        ? [
            { label: "Project id", value: `#${project.id}` },
            { label: "Status", value: project.status },
          ]
        : undefined,
    });

    // Brief drafted
    list.push({
      key: "brief-draft",
      gate: "G1",
      label: "Brief drafted",
      detail: brief ? `v${brief.version} · ${brief.content_markdown.length.toLocaleString()} chars` : "awaiting draft",
      actor: brief ? "brief-writer agent" : "—",
      timestamp: null,
      tone: brief ? "success" : "pending",
      state: brief ? "done" : "pending",
      note: brief
        ? brief.content_markdown.slice(0, 480) + (brief.content_markdown.length > 480 ? "…" : "")
        : "The brief-writer agent has not produced a draft yet.",
      meta: brief
        ? [
            { label: "Brief id", value: `#${brief.id}` },
            { label: "Version", value: `v${brief.version}` },
            { label: "Length", value: `${brief.content_markdown.length.toLocaleString()} chars` },
          ]
        : undefined,
    });

    // Brief approval (G1)
    const briefApproved = Boolean(brief?.is_approved);
    list.push({
      key: "brief-approval",
      gate: "G1",
      label: "Brief approved",
      detail: briefApproved
        ? "human gate cleared"
        : brief
          ? "waiting for reviewer sign-off"
          : "blocked · no brief",
      actor: briefApproved ? actorName : "—",
      timestamp: null,
      tone: briefApproved ? "success" : brief ? "warning" : "muted",
      state: briefApproved ? "done" : brief ? "current" : "pending",
      note: briefApproved
        ? "Reviewer confirmed scope, methods, and success criteria for the run."
        : brief
          ? "Reviewer must approve the brief before Gate G2 (budget) unlocks."
          : "No brief exists yet — nothing to approve.",
      meta: brief
        ? [
            { label: "Brief id", value: `#${brief.id}` },
            { label: "Approved", value: briefApproved ? "yes" : "no" },
          ]
        : undefined,
    });

    // Budget / run launch (G2)
    if (activeRun) {
      list.push({
        key: "run-launch",
        gate: "G2",
        label: "Budget approved · run launched",
        detail: `run #${activeRun.id} · mode ${activeRun.mode ?? "?"} · budget $${activeRun.budget_threshold.toFixed(2)}`,
        actor: actorName,
        timestamp: activeRun.created_at,
        tone: "success",
        state: "done",
        sessionRef,
        runId,
        note: `Operator authorized a budget of $${activeRun.budget_threshold.toFixed(2)} (${activeRun.mode ?? "?"} mode) to launch this run.`,
        meta: [
          { label: "Run id", value: `#${activeRun.id}` },
          { label: "Mode", value: activeRun.mode ?? "—" },
          { label: "Budget", value: `$${activeRun.budget_threshold.toFixed(2)}` },
          { label: "Estimate", value: `$${activeRun.cost_estimate.toFixed(2)}` },
        ],
      });

      // Run started
      list.push({
        key: "run-start",
        gate: "run",
        label: "Run started",
        detail: activeRun.session_id
          ? `session ${activeRun.session_id.slice(0, 8)}`
          : `status ${activeRun.status}`,
        actor: "agent runtime",
        timestamp: activeRun.started_at,
        tone: activeRun.started_at ? "success" : "pending",
        state: activeRun.started_at ? "done" : activeRun.status === "queued" ? "current" : "pending",
        sessionRef,
        runId,
        note: activeRun.started_at
          ? "Agent runtime picked up the queued run and began executing planned steps."
          : "Run is queued and waiting for a runtime slot.",
        meta: [
          { label: "Run id", value: `#${activeRun.id}` },
          { label: "Session", value: sessionRef ?? "—" },
          { label: "Status", value: activeRun.status },
        ],
      });

      // Run terminal state
      const status = activeRun.status;
      const terminal = ["completed", "failed", "cancelled"].includes(status);
      const runTone: TimelineTone =
        status === "completed"
          ? "success"
          : status === "failed"
            ? "danger"
            : status === "cancelled"
              ? "warning"
              : "active";
      list.push({
        key: "run-end",
        gate: "run",
        label:
          status === "completed"
            ? "Run completed"
            : status === "failed"
              ? "Run failed"
              : status === "cancelled"
                ? "Run cancelled"
                : "Run in progress",
        detail: `spent $${activeRun.actual_cost.toFixed(2)} of $${activeRun.budget_threshold.toFixed(2)}`,
        actor: "agent runtime",
        timestamp: activeRun.ended_at,
        tone: runTone,
        state: terminal ? "done" : "current",
        sessionRef,
        runId,
        artifacts: runArtifacts,
        note:
          status === "completed"
            ? "Agent run finished. All artifacts below were produced during this session."
            : status === "failed"
              ? "The run terminated with an error. See logs and artifacts for diagnostics."
              : status === "cancelled"
                ? "The run was cancelled before completion."
                : "The agent is still executing planned steps.",
        meta: [
          { label: "Run id", value: `#${activeRun.id}` },
          { label: "Actual cost", value: `$${activeRun.actual_cost.toFixed(2)}` },
          { label: "Budget", value: `$${activeRun.budget_threshold.toFixed(2)}` },
          { label: "Artifacts", value: String(runArtifacts.length) },
        ],
      });
    } else {
      list.push({
        key: "run-launch",
        gate: "G2",
        label: "Budget approval · run launch",
        detail: briefApproved ? "ready to launch" : "waiting on brief",
        actor: "—",
        timestamp: null,
        tone: briefApproved ? "warning" : "muted",
        state: briefApproved ? "current" : "pending",
        note: briefApproved
          ? "Brief is approved. Operator must approve the budget to launch the run."
          : "Budget gate is blocked until the brief clears Gate G1.",
      });
    }

    // Review findings
    if (activeRun && activeRun.status === "completed") {
      const highs = findings.filter((f) => (f.severity ?? "").toLowerCase() === "high").length;
      const previews = findings.slice(0, 4).map((f) => `• [${f.severity}] ${f.finding}`).join("\n");
      list.push({
        key: "review",
        gate: "review",
        label: "Reviewer analysis",
        detail: findings.length
          ? `${findings.length} findings · ${highs} high severity`
          : "no findings raised",
        actor: "reviewer agent",
        timestamp: activeRun.ended_at,
        tone: highs > 0 ? "warning" : "success",
        state: "done",
        sessionRef,
        runId,
        note: findings.length
          ? previews + (findings.length > 4 ? `\n…and ${findings.length - 4} more` : "")
          : "Reviewer agent found no issues that need attention.",
        meta: [
          { label: "Findings", value: String(findings.length) },
          { label: "High severity", value: String(highs) },
        ],
      });
    }

    // Final export (G3)
    const finalArtifacts = runArtifacts.filter((a) =>
      ["report", "final", "package"].some((k) => (a.kind ?? "").toLowerCase().includes(k)),
    );
    list.push({
      key: "export",
      gate: "G3",
      label: "Final package approved",
      detail: exported
        ? "signed record exported"
        : activeRun?.status === "completed"
          ? "awaiting sign-off"
          : "blocked · run not complete",
      actor: exported ? actorName : "—",
      timestamp: null,
      tone: exported ? "success" : activeRun?.status === "completed" ? "warning" : "muted",
      state: exported ? "done" : activeRun?.status === "completed" ? "current" : "pending",
      sessionRef,
      runId,
      artifacts: finalArtifacts.length ? finalArtifacts : runArtifacts,
      note: exported
        ? "Operator signed off on the final package. Downloads are unlocked."
        : activeRun?.status === "completed"
          ? "Run is complete. Approve or force-export to unlock the final artifacts."
          : "Final export unlocks after the run reaches a completed state.",
      meta: [
        { label: "Exported", value: exported ? "yes" : "no" },
        { label: "Run status", value: activeRun?.status ?? "—" },
      ],
    });

    return list;
  }, [project, brief, activeRun, findings, exported, actorName]);


  const gateTypes = useMemo(() => {
    const seen = new Set<string>();
    events.forEach((e) => e.gate && seen.add(e.gate));
    return Array.from(seen);
  }, [events]);

  const preRunGates = new Set(["init", "G1"]);
  const runGates = new Set(["G2", "run", "review", "G3"]);

  const filteredEvents = useMemo(() => {
    return events.filter((ev) => {
      if (gateFilter.length > 0 && (!ev.gate || !gateFilter.includes(ev.gate))) return false;
      if (sessionFilter === "pre-run" && ev.gate && !preRunGates.has(ev.gate)) return false;
      if (sessionFilter === "current" && ev.gate && !runGates.has(ev.gate)) return false;
      return true;
    });
  }, [events, gateFilter, sessionFilter]);

  const doneCount = filteredEvents.filter((e) => e.state === "done").length;

  const toggleGate = (g: string) =>
    setGateFilter((prev) => (prev.includes(g) ? prev.filter((x) => x !== g) : [...prev, g]));

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="lifecycle"
        title="Status timeline"
        subtitle="Every gate transition, with the actor that made the call."
        meta={
          <>
            <MetaChip>{doneCount}/{filteredEvents.length} shown</MetaChip>
            {activeRun ? (
              <MetaChip tone="info">run #{activeRun.id}</MetaChip>
            ) : (
              <MetaChip tone="warning">no active run</MetaChip>
            )}
          </>
        }
      />
      <div className="border-b border-panel-border/70 bg-muted/20 px-6 py-4">
        <div className="flex flex-wrap items-end gap-x-6 gap-y-3">
          <div className="flex flex-col gap-1.5">
            <span className="mono-label text-[10px]">Project</span>
            <select
              value={project?.id ?? ""}
              onChange={(e) => {
                const v = e.target.value;
                if (v) onSelectProject(Number(v));
              }}
              className="rounded-md border border-panel-border bg-background px-2 py-1 font-mono text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            >
              {!project && <option value="">— none —</option>}
              {projects.map((p) => (
                <option key={p.id} value={p.id}>
                  #{p.id} · {p.title}
                </option>
              ))}
            </select>
          </div>
          <div className="flex flex-col gap-1.5">
            <span className="mono-label text-[10px]">Run session</span>
            <select
              value={sessionFilter}
              onChange={(e) => setSessionFilter(e.target.value as typeof sessionFilter)}
              className="rounded-md border border-panel-border bg-background px-2 py-1 font-mono text-[12px] text-foreground focus:outline-none focus:ring-1 focus:ring-primary"
            >
              <option value="all">All events</option>
              <option value="pre-run">Pre-run only</option>
              <option value="current">
                {activeRun ? `Run #${activeRun.id} only` : "Current run only"}
              </option>
            </select>
          </div>
          <div className="flex min-w-0 flex-col gap-1.5">
            <div className="flex items-center gap-3">
              <span className="mono-label text-[10px]">Gate type</span>
              {gateFilter.length > 0 && (
                <button
                  onClick={() => setGateFilter([])}
                  className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground hover:text-foreground"
                >
                  clear
                </button>
              )}
            </div>
            <div className="flex flex-wrap gap-1.5">
              {gateTypes.map((g) => {
                const active = gateFilter.includes(g);
                return (
                  <button
                    key={g}
                    onClick={() => toggleGate(g)}
                    className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] transition ${
                      active
                        ? "border-primary/50 bg-primary/15 text-foreground"
                        : "border-panel-border bg-background text-muted-foreground hover:text-foreground"
                    }`}
                  >
                    {g}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>
      <div className="p-6">
        {filteredEvents.length === 0 ? (
          <div className="flex flex-col items-start gap-4 rounded-lg border border-dashed border-panel-border bg-background/40 p-6">
            <div className="flex items-center gap-3">
              <span className="grid h-8 w-8 place-items-center rounded-full border border-panel-border bg-panel/60 font-mono text-sm">
                ∅
              </span>
              <div>
                <p className="font-serif text-base text-foreground">No transitions match the current filters.</p>
                <p className="mt-0.5 font-mono text-[11px] text-muted-foreground">
                  {gateFilter.length > 0 && sessionFilter !== "all"
                    ? `Both gate (${gateFilter.join(", ")}) and run-session filters are active.`
                    : gateFilter.length > 0
                      ? `Gate filter is set to ${gateFilter.join(", ")}.`
                      : sessionFilter !== "all"
                        ? `Run-session filter is set to “${sessionFilter === "pre-run" ? "Pre-run only" : "Current run only"}”.`
                        : "Adjust the filters above to see lifecycle events."}
                </p>
              </div>
            </div>
            <div className="flex flex-wrap gap-2">
              {gateFilter.length > 0 && (
                <button
                  onClick={() => setGateFilter([])}
                  className="inline-flex items-center gap-1.5 rounded-md border border-panel-border bg-background px-3 py-1.5 font-mono text-[11px] text-foreground hover:border-primary/50 hover:text-primary"
                >
                  <span>×</span> Clear gate filters
                </button>
              )}
              {sessionFilter !== "all" && (
                <button
                  onClick={() => setSessionFilter("all")}
                  className="inline-flex items-center gap-1.5 rounded-md border border-panel-border bg-background px-3 py-1.5 font-mono text-[11px] text-foreground hover:border-primary/50 hover:text-primary"
                >
                  <span>↺</span> Show all events
                </button>
              )}
              {(gateFilter.length > 0 || sessionFilter !== "all") && (
                <button
                  onClick={() => {
                    setGateFilter([]);
                    setSessionFilter("all");
                  }}
                  className="inline-flex items-center gap-1.5 rounded-md bg-primary px-3 py-1.5 font-mono text-[11px] font-medium text-primary-foreground hover:opacity-95"
                >
                  <span>↻</span> Reset all filters
                </button>
              )}
            </div>
          </div>
        ) : (

          <ol className="relative space-y-5 border-l border-panel-border pl-6">
            {filteredEvents.map((ev) => {
              const styles = toneStyles(ev.tone);
              const isSelected = selectedKey === ev.key;
              return (
                <li key={ev.key} className="relative">
                  <span
                    className={`absolute -left-[27px] top-1.5 grid h-3.5 w-3.5 place-items-center rounded-full ring-4 ${styles.ring}`}
                  >
                    <span className={`h-2 w-2 rounded-full ${styles.dot}`} />
                  </span>
                  <button
                    type="button"
                    onClick={() => setSelectedKey(ev.key)}
                    aria-expanded={isSelected}
                    className={`group w-full rounded-md border border-transparent px-3 py-2 -mx-3 text-left transition hover:border-panel-border hover:bg-panel/40 focus:outline-none focus:ring-1 focus:ring-primary ${
                      isSelected ? "border-panel-border bg-panel/50" : ""
                    }`}
                  >
                    <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                      <div className="flex flex-wrap items-center gap-2">
                        {ev.gate && (
                          <span
                            className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] ${styles.chip}`}
                          >
                            {ev.gate}
                          </span>
                        )}
                        <span className="font-serif text-lg leading-tight text-foreground">
                          {ev.label}
                        </span>
                      </div>
                      <span className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                        {formatTs(ev.timestamp)}
                      </span>
                    </div>
                    <p className="mt-1 text-[13px] leading-[1.55] text-muted-foreground">
                      {ev.detail}
                    </p>
                    <div className="mt-1 flex items-center justify-between gap-3">
                      <p className="font-mono text-[11px] uppercase tracking-[0.12em] text-muted-foreground/80">
                        actor · <span className="text-foreground/80">{ev.actor}</span>
                      </p>
                      <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground/60 opacity-0 transition group-hover:opacity-100">
                        details
                      </span>
                    </div>
                  </button>
                </li>
              );
            })}
          </ol>
        )}
      </div>
      <TimelineDetailSheet
        event={events.find((e) => e.key === selectedKey) ?? null}
        onClose={() => setSelectedKey(null)}
      />


    </div>
  );
}

/**
 * The slide-over panel showing one timeline event in full: its note, actor,
 * artifacts, and metadata.
 *
 * A Radix `Sheet` rather than a `Dialog`. Both are modal; a sheet slides in
 * from the edge and suits browsing detail, whereas a dialog interrupts and
 * suits decisions. Compare `DecisionDialog`, which is deliberately a dialog
 * because it demands an answer.
 */
function TimelineDetailSheet({
  event,
  onClose,
}: {
  event: TimelineEvent | null;
  onClose: () => void;
}) {
  const open = Boolean(event);
  const styles = event ? toneStyles(event.tone) : null;
  const artifacts = event?.artifacts ?? [];
  return (
    <Sheet open={open} onOpenChange={(v) => { if (!v) onClose(); }}>
      <SheetContent
        side="right"
        className="w-full sm:max-w-lg overflow-y-auto border-l border-panel-border bg-background p-0"
      >
        {event && styles ? (
          <>
            <SheetHeader className="space-y-3 border-b border-panel-border/70 bg-muted/20 px-6 py-5 text-left">
              <div className="flex flex-wrap items-center gap-2">
                {event.gate && (
                  <span
                    className={`inline-flex items-center rounded-full border px-2 py-0.5 font-mono text-[10px] uppercase tracking-[0.14em] ${styles.chip}`}
                  >
                    {event.gate}
                  </span>
                )}
                <span className={`inline-flex h-2 w-2 rounded-full ${styles.dot}`} />
                <span className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                  {event.state}
                </span>
                <div className="ml-auto flex items-center gap-2">
                  <button
                    type="button"
                    onClick={() => window.open(window.location.href, "_blank", "noopener,noreferrer")}
                    className="inline-flex items-center gap-1.5 rounded-md border border-panel-border bg-background px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground transition hover:border-primary/50 hover:text-primary"
                  >
                    <span>↗</span> Open
                  </button>
                  <button
                    type="button"
                    onClick={async () => {
                      try {
                        await navigator.clipboard.writeText(window.location.href);
                        toast.success("Link copied", {
                          description: "Transition detail URL copied to clipboard.",
                        });
                      } catch {
                        toast.error("Copy failed", {
                          description: "Could not access the clipboard. Copy the URL manually.",
                        });
                      }
                    }}
                    className="inline-flex items-center gap-1.5 rounded-md border border-panel-border bg-background px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground transition hover:border-primary/50 hover:text-primary"
                  >
                    <span>⎘</span> Copy link
                  </button>
                </div>
              </div>
              <SheetTitle className="font-serif text-2xl leading-tight text-foreground">
                {event.label}
              </SheetTitle>
              <SheetDescription className="font-mono text-[11px] uppercase tracking-[0.14em] text-muted-foreground">
                {formatTs(event.timestamp)}
              </SheetDescription>
            </SheetHeader>
            <div className="space-y-6 px-6 py-6">
              <section className="space-y-2">
                <p className="mono-label text-[10px]">Summary</p>
                <p className="text-[13px] leading-[1.6] text-foreground">{event.detail}</p>
              </section>

              <section className="grid grid-cols-2 gap-3">
                <div className="rounded-md border border-panel-border bg-panel/40 p-3">
                  <p className="mono-label text-[10px]">Actor</p>
                  <p className="mt-1 font-mono text-[12px] text-foreground">{event.actor}</p>
                </div>
                <div className="rounded-md border border-panel-border bg-panel/40 p-3">
                  <p className="mono-label text-[10px]">Run session</p>
                  <p className="mt-1 font-mono text-[12px] text-foreground">
                    {event.runId ? `#${event.runId}` : "—"}
                    {event.sessionRef ? ` · ${event.sessionRef.slice(0, 8)}` : ""}
                  </p>
                </div>
              </section>

              {event.meta && event.meta.length > 0 && (
                <section className="space-y-2">
                  <p className="mono-label text-[10px]">Metadata</p>
                  <dl className="grid grid-cols-2 gap-x-4 gap-y-2 rounded-md border border-panel-border bg-background p-3">
                    {event.meta.map((m) => (
                      <div key={m.label} className="flex flex-col">
                        <dt className="font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                          {m.label}
                        </dt>
                        <dd className="font-mono text-[12px] text-foreground">{m.value}</dd>
                      </div>
                    ))}
                  </dl>
                </section>
              )}

              {event.note && (
                <section className="space-y-2">
                  <p className="mono-label text-[10px]">Gate note</p>
                  <p className="whitespace-pre-wrap rounded-md border border-panel-border bg-panel/40 p-3 text-[13px] leading-[1.6] text-foreground">
                    {event.note}
                  </p>
                </section>
              )}

              <section className="space-y-2">
                <p className="mono-label text-[10px]">
                  Related artifacts {artifacts.length ? `· ${artifacts.length}` : ""}
                </p>
                {artifacts.length === 0 ? (
                  <p className="rounded-md border border-dashed border-panel-border bg-background/40 p-3 font-mono text-[11px] text-muted-foreground">
                    No artifacts associated with this transition.
                  </p>
                ) : (
                  <ul className="space-y-2">
                    {artifacts.map((a, i) => {
                      const url = artifactUrl(a);
                      return (
                        <li
                          key={(a.id ?? i) + "-" + a.name}
                          className="flex items-center justify-between gap-3 rounded-md border border-panel-border bg-background p-3"
                        >
                          <div className="min-w-0">
                            <p className="truncate font-mono text-[12px] text-foreground">
                              {a.name}
                            </p>
                            <p className="mt-0.5 font-mono text-[10px] uppercase tracking-[0.14em] text-muted-foreground">
                              {a.kind}
                              {a.mime_type ? ` · ${a.mime_type}` : ""}
                              {typeof a.size_bytes === "number"
                                ? ` · ${a.size_bytes.toLocaleString()} B`
                                : ""}
                            </p>
                          </div>
                          {url ? (
                            <a
                              href={url}
                              target="_blank"
                              rel="noreferrer"
                              className="shrink-0 rounded-md border border-panel-border bg-panel/60 px-2.5 py-1 font-mono text-[10px] uppercase tracking-[0.14em] text-foreground hover:border-primary/50 hover:text-primary"
                            >
                              open
                            </a>
                          ) : null}
                        </li>
                      );
                    })}
                  </ul>
                )}
              </section>
            </div>
          </>
        ) : null}
      </SheetContent>
    </Sheet>
  );
}

// ─── Final export ──────────────────────────────────────────────────────────



// ─── Final export ──────────────────────────────────────────────────────────

/**
 * GATE G4 — the last checkpoint: approve publication of the paper.
 *
 * Two paths through this component, and the distinction matters:
 *
 *   approve — the normal route, available once the groundedness checks pass
 *   force   — override those checks and publish anyway. Restricted to
 *             `final:force`, which only reviewers and admins hold (see the
 *             permission matrix in lib/permissions.tsx).
 *
 * "Groundedness" is the check for an AI-written paper citing sources that do
 * not exist, leaving placeholder text behind, or fabricating results — see
 * `GroundednessReport` in lib/api.ts. Forcing past a failed check is sometimes
 * legitimate (a checker false-positive), which is why the escape hatch exists
 * at all; separate state and a separate dialog make it impossible to trigger by
 * accident.
 */
function FinalExport({
  project,
  runComplete,
  hasFindings,
  activeRun,
  onExported,
}: {
  project: ProjectOut;
  runComplete: boolean;
  hasFindings: boolean;
  activeRun: RunOut | null;
  onExported: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const [grounding, setGrounding] = useState<GroundednessReport | null>(null);
  const [approveOpen, setApproveOpen] = useState(false);
  const [forceOpen, setForceOpen] = useState(false);

  // Both preconditions: the run must have finished AND produced review
  // findings. A completed run with no findings means the review step did not
  // run, so there is nothing to have approved.
  const ready = runComplete && hasFindings;
  const groundScore =
    grounding?.score !== undefined ? `${(grounding.score * 100).toFixed(1)}%` : "—";
  const groundIssues =
    (grounding?.dead_urls?.length ?? 0) +
    (grounding?.placeholders?.length ?? 0) +
    (grounding?.fabrications?.length ?? 0);

  const gateTone: DotTone = done
    ? "success"
    : grounding
      ? "warning"
      : ready
        ? "info"
        : "muted";
  const gateLabel = done
    ? "G3 · APPROVED"
    : grounding
      ? "G3 · BLOCKED"
      : ready
        ? "G3 · READY FOR REVIEW"
        : "G3 · LOCKED";

  const extractReport = (body: unknown): GroundednessReport | null => {
    if (!body || typeof body !== "object") return null;
    const b = body as Record<string, unknown>;
    const src =
      (b.detail && typeof b.detail === "object" ? (b.detail as Record<string, unknown>) : null) ??
      (b.groundedness && typeof b.groundedness === "object"
        ? (b.groundedness as Record<string, unknown>)
        : null) ??
      b;
    return {
      score: typeof src.score === "number" ? src.score : undefined,
      dead_urls: Array.isArray(src.dead_urls) ? (src.dead_urls as string[]) : undefined,
      placeholders: Array.isArray(src.placeholders) ? (src.placeholders as string[]) : undefined,
      fabrications: Array.isArray(src.fabrications) ? (src.fabrications as string[]) : undefined,
    };
  };

  const doExport = async (notes: string, force: boolean) => {
    setBusy(true);
    try {
      await approveFinal(project.id, { notes, force });
      setDone(true);
      setGrounding(null);
      setApproveOpen(false);
      setForceOpen(false);
      onExported();
      toast.success(
        force
          ? "Package force-exported · gate G3 bypassed"
          : "Package signed and exported · gate G3 cleared",
        { description: notes.length > 60 ? `${notes.slice(0, 60)}…` : notes },
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        const rep = extractReport(e.body);
        setGrounding(rep ?? { score: undefined });
        setApproveOpen(false);
        toast.warning("Groundedness gate blocked export", {
          description: "Review issues below, then use Force export if you accept them.",
        });
      } else {
        toast.error(friendlyError(e, "Could not approve export"));
      }
    } finally {
      setBusy(false);
    }
  };

  const checklist = [
    { ok: runComplete, label: "run completed", detail: activeRun ? activeRun.status : "no run" },
    {
      ok: hasFindings,
      label: "reviewer findings recorded",
      detail: hasFindings ? "present" : "run reviewer panel",
    },
    {
      ok: !grounding || done,
      label: "groundedness gate",
      detail: grounding ? `${groundIssues} issues` : "not yet run",
    },
  ];

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="approval · gate g3"
        title={
          done
            ? "Final package signed and exported."
            : grounding
              ? "Groundedness check failed — resolve or force."
              : ready
                ? "Final package awaits human approval."
                : "Final export is locked."
        }
        subtitle="Every download and hand-off is blocked until this gate is approved. Notes are recorded to the audit trail."
        meta={
          <>
            <MetaChip tone={gateTone}>{gateLabel}</MetaChip>
            <MetaChip tone={runComplete ? "success" : "muted"}>
              run · {activeRun ? activeRun.status : "none"}
            </MetaChip>
            <MetaChip tone={hasFindings ? "success" : "warning"}>
              findings · {hasFindings ? "yes" : "missing"}
            </MetaChip>
            {grounding && (
              <MetaChip tone="warning">groundedness · {groundIssues} issues</MetaChip>
            )}
          </>
        }
      />

      <div className="grid grid-cols-4 gap-px bg-panel-border">
        <div className="bg-panel p-4">
          <div className="mono-label">gate</div>
          <div className="mt-1 font-mono text-[13px] text-foreground">G3 · export</div>
        </div>
        <div className="bg-panel p-4">
          <div className="mono-label">project</div>
          <div className="mt-1 font-mono text-[13px] text-foreground">#{project.id}</div>
        </div>
        <div className="bg-panel p-4">
          <div className="mono-label">groundedness</div>
          <div
            className={`mt-1 font-mono text-[13px] ${
              grounding ? "text-warning" : "text-foreground"
            }`}
          >
            {groundScore}
          </div>
        </div>
        <div className="bg-panel p-4">
          <div className="mono-label">status</div>
          <div
            className={`mt-1 font-mono text-[13px] ${
              done ? "text-success" : ready ? "text-foreground" : "text-muted-foreground"
            }`}
          >
            {done ? "approved" : ready ? "awaiting" : "locked"}
          </div>
        </div>
      </div>

      <div className="grid gap-6 p-6 lg:grid-cols-[1.1fr_1fr]">
        <div>
          <div className="mono-label mb-3">pre-flight checklist</div>
          <ul className="space-y-2">
            {checklist.map((c) => (
              <li
                key={c.label}
                className="flex items-center justify-between rounded-md border border-panel-border bg-background/40 px-3 py-2 font-mono text-[12px]"
              >
                <span className="flex items-center gap-2">
                  <Dot tone={c.ok ? "success" : "warning"} pulse={false} />
                  <span className="text-foreground">{c.label}</span>
                </span>
                <span className="text-muted-foreground">{c.detail}</span>
              </li>
            ))}
          </ul>
          {!ready && !done && (
            <p className="mt-3 font-mono text-[11px] text-muted-foreground">
              Complete the run and the reviewer panel to unlock the approval dialog.
            </p>
          )}
        </div>

        <div className="flex flex-col justify-between gap-4 rounded-md border border-panel-border bg-background/40 p-4">
          <div>
            <div className="mono-label">decision</div>
            <p className="mt-2 font-serif text-lg leading-snug text-foreground">
              {done
                ? "Exports are unlocked. Reviewers can download the signed package."
                : grounding
                  ? "Groundedness gate raised issues. Approving now requires a Force export note."
                  : ready
                    ? "Approve to sign the package and unlock artifact downloads."
                    : "Approval is disabled until the checklist above is green."}
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Gated
              perm="final:approve"
              type="button"
              disabled={!ready || busy || done || Boolean(grounding)}
              onClick={() => setApproveOpen(true)}
              className="rounded-md bg-primary px-4 py-2.5 font-mono text-xs font-medium text-primary-foreground hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {done ? "✓ Approved" : busy ? "Approving…" : "⬒ Approve & export"}
            </Gated>
            <Gated
              perm="final:force"
              type="button"
              disabled={busy || done || !ready}
              onClick={() => setForceOpen(true)}
              className="rounded-md border border-destructive/40 px-4 py-2.5 font-mono text-xs text-destructive hover:border-destructive disabled:cursor-not-allowed disabled:opacity-50"
            >
              Force export
            </Gated>
          </div>
        </div>
      </div>

      {grounding && (
        <div className="mx-6 mb-6 rounded-md border border-warning/50 bg-warning/5 p-4 font-mono text-[12px] text-foreground">
          <div className="flex items-center justify-between border-b border-warning/30 pb-2">
            <span className="mono-label">GROUNDEDNESS REPORT</span>
            {grounding.score !== undefined && (
              <span>
                score: <strong>{(grounding.score * 100).toFixed(1)}%</strong>
              </span>
            )}
          </div>
          <GroundednessList label="dead URLs" items={grounding.dead_urls} tone="error" />
          <GroundednessList
            label="unresolved placeholders"
            items={grounding.placeholders}
            tone="warning"
          />
          <GroundednessList
            label="suspected fabrications"
            items={grounding.fabrications}
            tone="error"
          />
        </div>
      )}

      <DecisionDialog
        open={approveOpen}
        onOpenChange={setApproveOpen}
        tone="approve"
        kicker="decision · gate g3"
        title="Approve final export"
        description="Signs the package and unlocks artifact downloads for reviewers. Your note becomes part of the audit trail."
        summary={[
          { label: "project", value: `#${project.id}` },
          { label: "run", value: activeRun ? `#${activeRun.id} · ${activeRun.status}` : "—" },
          {
            label: "actual cost",
            value: activeRun ? `$${activeRun.actual_cost.toFixed(2)}` : "—",
          },
          { label: "findings", value: hasFindings ? "recorded" : "missing" },
        ]}
        confirmLabel="⬒ Sign & export"
        confirmingLabel="Approving…"
        placeholder="What was validated? Reviewer, scope, follow-ups…"
        busy={busy}
        onConfirm={(notes) => doExport(notes, false)}
      />

      <DecisionDialog
        open={forceOpen}
        onOpenChange={setForceOpen}
        tone="danger"
        kicker="decision · gate g3 bypass"
        title="Force export (bypass groundedness gate)"
        description="Exports the package even though groundedness checks failed. Only proceed if you have manually verified the issues are acceptable."
        summary={[
          { label: "project", value: `#${project.id}` },
          {
            label: "groundedness",
            value: grounding?.score !== undefined ? groundScore : "not run",
            tone: "danger",
          },
          {
            label: "issues",
            value: grounding ? `${groundIssues}` : "unknown",
            tone: "danger",
          },
          { label: "gate", value: "G3 bypass", tone: "danger" },
        ]}
        confirmLabel="Force sign & export"
        confirmingLabel="Bypassing…"
        minChars={20}
        placeholder="Explain why bypass is acceptable — verified sources, accepted risks, sign-off…"
        busy={busy}
        onConfirm={(notes) => doExport(notes, true)}
      />
    </div>
  );
}

/**
 * Renders one category of groundedness problem — dead URLs, leftover
 * placeholders, or suspected fabrications.
 *
 * Returns null when the list is empty, so a clean report shows nothing at all
 * rather than three empty headings.
 */
function GroundednessList({
  label,
  items,
  tone,
}: {
  label: string;
  items?: string[];
  tone: DotTone;
}) {
  if (!items || items.length === 0) return null;
  return (
    <div className="mt-3">
      <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
        <Dot tone={tone} pulse={false} /> {label} ({items.length})
      </div>
      <ul className="mt-1 space-y-1 pl-4">
        {items.map((s, i) => (
          <li key={i} className="break-all text-foreground/90">
            • {s}
          </li>
        ))}
      </ul>
    </div>
  );
}


// ─── Provider keys ─────────────────────────────────────────────────────────

/**
 * Describes one LLM provider for the settings panel: its display strings, its
 * model options, and the helper text explaining where to get a key.
 *
 * Driving the UI from this array means adding a provider is appending an object
 * to `PROVIDERS` below rather than writing another block of form markup — the
 * same content-as-data approach as `RELEASES` in changelog.tsx.
 */
interface ProviderDef {
  id: ProviderId;
  title: string;
  label: string;
  placeholder: string;
  modelLabel: string;
  models: string[]; // dropdown options; last selected freeform allowed
  helper: string;
}

const PROVIDERS: ProviderDef[] = [
  {
    id: "anthropic",
    title: "Claude / Anthropic",
    label: "Paste Claude API key",
    placeholder: "sk-ant-...",
    modelLabel: "model",
    models: [
      "claude-opus-4-8",
      "claude-fable-5",
      "claude-3-5-sonnet-latest",
      "claude-3-5-haiku-latest",
      "claude-3-opus-latest",
    ],
    helper: "Drives brief + draft generation on real runs.",
  },
  {
    id: "openai",
    title: "OpenAI / ChatGPT",
    label: "Paste OpenAI API key",
    placeholder: "sk-...",
    modelLabel: "model",
    models: ["gpt-4.1", "gpt-4.1-mini", "gpt-4o", "gpt-4o-mini", "o4-mini"],
    helper: "Alternate LLM for real runs.",
  },
  {
    id: "runpod",
    title: "RunPod (code execution)",
    label: "Paste RunPod API key",
    placeholder: "rpa_...",
    modelLabel: "endpoint id",
    models: ["your-endpoint-id"],
    helper: 'Required when "Execute on RunPod" is enabled. Endpoint id is stored as model name.',
  },
  {
    id: "gemini",
    title: "Gemini (figures)",
    label: "Paste Gemini API key",
    placeholder: "AIza...",
    modelLabel: "model",
    models: ["gemini-2.5-flash-image", "gemini-2.0-flash", "gemini-1.5-pro"],
    helper: 'Required when "Generate figures" is enabled.',
  },
];

type ProviderDraft = { apiKey: string; modelName: string; enabled: boolean };

/**
 * Manage the user's LLM provider API keys.
 *
 * ⚠ SECURITY-RELEVANT. Keys are WRITE-ONLY: once submitted, the server never
 * returns one again, and `ProviderKeyOut` (lib/api.ts) exposes only
 * `masked_key` and `key_last4`. So a leaked API response cannot leak a
 * credential, and this panel can never display a full key even to its owner.
 *
 * `testProviderKey` exists because a stored key may still be invalid — expired,
 * revoked, or out of quota. It makes a live call to the provider so the user
 * finds out here rather than when a run fails halfway through.
 *
 * Reached through `ProviderKeysGate`, which hides this panel entirely from
 * roles without `keys:manage`.
 */
function ProviderKeysPanel() {
  const [keys, setKeys] = useState<ProviderKeyOut[]>([]);
  const [drafts, setDrafts] = useState<Record<ProviderId, ProviderDraft>>({
    openai: { apiKey: "", modelName: "gpt-4.1-mini", enabled: false },
    anthropic: { apiKey: "", modelName: "claude-3-5-sonnet-latest", enabled: false },
    runpod: { apiKey: "", modelName: "", enabled: false },
    gemini: { apiKey: "", modelName: "gemini-2.5-flash-image", enabled: false },
  });
  const [visible, setVisible] = useState<Record<ProviderId, boolean>>({
    openai: false,
    anthropic: false,
    runpod: false,
    gemini: false,
  });

  const [busy, setBusy] = useState("");

  useEffect(() => {
    listProviderKeys()
      .then((rows) => {
        setKeys(rows);
        setDrafts((current) => {
          const next = { ...current };
          for (const row of rows) {
            next[row.provider] = {
              apiKey: "",
              modelName: row.model_name || current[row.provider].modelName,
              enabled: row.is_enabled,
            };
          }
          return next;
        });
      })
      .catch(() => {
        /* backend may not have provider-keys yet — that's fine, don't spam */
      });
  }, []);

  const upsertKey = (row: ProviderKeyOut) =>
    setKeys((items) => [row, ...items.filter((item) => item.provider !== row.provider)]);

  const savedFor = (provider: ProviderId) => keys.find((item) => item.provider === provider);
  const updateDraft = (provider: ProviderId, patch: Partial<(typeof drafts)[ProviderId]>) =>
    setDrafts((current) => ({ ...current, [provider]: { ...current[provider], ...patch } }));

  const saveKey = async (provider: ProviderId) => {
    const draft = drafts[provider];
    if (!draft.apiKey.trim()) {
      toast.error("Paste an API key before saving.");
      return;
    }
    setBusy(`save-${provider}`);
    try {
      const row = await saveProviderKey({
        provider,
        api_key: draft.apiKey.trim(),
        model_name: draft.modelName.trim(),
        is_enabled: draft.enabled,
      });
      upsertKey(row);
      updateDraft(provider, { apiKey: "" });
      toast.success(`${provider === "openai" ? "OpenAI" : "Claude"} key saved`, {
        description: `Stored as ${row.masked_key}. Raw key never leaves the server.`,
      });
    } catch (e) {
      toast.error(friendlyError(e, "Could not save provider key"));
    } finally {
      setBusy("");
    }
  };

  const clearKey = async (provider: ProviderId) => {
    setBusy(`clear-${provider}`);
    try {
      await deleteProviderKey(provider);
      setKeys((items) => items.filter((item) => item.provider !== provider));
      updateDraft(provider, { apiKey: "", enabled: false });
      toast.success("Provider key cleared");
    } catch (e) {
      toast.error(friendlyError(e, "Could not clear provider key"));
    } finally {
      setBusy("");
    }
  };

  const testKey = async (provider: ProviderId) => {
    setBusy(`test-${provider}`);
    try {
      const result = await testProviderKey(provider);
      const notify = result.ok ? toast.success : toast.warning;
      notify(result.status.replace("_", " "), { description: result.detail });
      setKeys((items) =>
        items.map((item) =>
          item.provider === provider
            ? { ...item, test_status: result.status, last_tested_at: result.tested_at }
            : item,
        ),
      );
    } catch (e) {
      toast.error(friendlyError(e, "Could not test provider key"));
    } finally {
      setBusy("");
    }
  };

  return (
    <div className="panel overflow-hidden">
      <SectionHeader icon="⌘">PROVIDER KEYS</SectionHeader>
      <div className="grid gap-5 p-5 xl:grid-cols-2">
        {PROVIDERS.map((provider) => {
          const saved = savedFor(provider.id);
          const draft = drafts[provider.id];
          return (
            <div
              key={provider.id}
              className="rounded-md border border-panel-border bg-background/40 p-4"
            >
              <div className="flex items-start justify-between gap-4">
                <div>
                  <h3 className="font-serif text-2xl text-foreground">{provider.title}</h3>
                  <p className="mt-1 text-sm leading-relaxed text-muted-foreground">
                    {provider.helper}
                  </p>
                </div>
                <span className="inline-flex items-center gap-2 rounded-full border border-panel-border px-3 py-1 font-mono text-[10px] tracking-[0.12em] text-muted-foreground">
                  <Dot tone={saved ? "success" : "muted"} pulse={Boolean(saved)} />
                  {saved ? saved.masked_key : "NOT SET"}
                </span>
              </div>

              <div className="mt-4 grid gap-3">
                <label>
                  <span className="mono-label">{provider.label}</span>
                  <div className="mt-2 flex gap-2">
                    <input
                      value={draft.apiKey}
                      onChange={(event) => updateDraft(provider.id, { apiKey: event.target.value })}
                      type={visible[provider.id] ? "text" : "password"}
                      autoComplete="off"
                      spellCheck={false}
                      placeholder={provider.placeholder}
                      className="min-w-0 flex-1 rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
                    />
                    <button
                      type="button"
                      onClick={() =>
                        setVisible((state) => ({ ...state, [provider.id]: !state[provider.id] }))
                      }
                      className="rounded-md border border-panel-border px-3 py-2 font-mono text-[11px] text-muted-foreground hover:text-foreground"
                    >
                      {visible[provider.id] ? "Hide" : "Show"}
                    </button>
                  </div>
                </label>

                <label>
                  <span className="mono-label">{provider.modelLabel}</span>
                  {provider.id === "runpod" ? (
                    <input
                      value={draft.modelName}
                      onChange={(event) =>
                        updateDraft(provider.id, { modelName: event.target.value })
                      }
                      placeholder="endpoint id"
                      className="mt-2 w-full rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
                    />
                  ) : (
                    <div className="mt-2 flex gap-2">
                      <select
                        value={
                          provider.models.includes(draft.modelName) ? draft.modelName : "__custom"
                        }
                        onChange={(event) => {
                          const v = event.target.value;
                          if (v === "__custom") return;
                          updateDraft(provider.id, { modelName: v });
                        }}
                        className="min-w-0 flex-1 rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground focus:border-primary focus:outline-none"
                      >
                        {provider.models.map((m) => (
                          <option key={m} value={m}>
                            {m}
                          </option>
                        ))}
                        <option value="__custom">custom…</option>
                      </select>
                      {!provider.models.includes(draft.modelName) && (
                        <input
                          value={draft.modelName}
                          onChange={(event) =>
                            updateDraft(provider.id, { modelName: event.target.value })
                          }
                          placeholder="custom model id"
                          className="min-w-0 flex-1 rounded-md border border-panel-border bg-background/60 px-3 py-2.5 font-mono text-xs text-foreground placeholder:text-muted-foreground/60 focus:border-primary focus:outline-none"
                        />
                      )}
                    </div>
                  )}
                </label>


                <label className="flex items-center gap-2 font-mono text-[11px] text-muted-foreground">
                  <input
                    type="checkbox"
                    checked={draft.enabled}
                    onChange={(event) =>
                      updateDraft(provider.id, { enabled: event.target.checked })
                    }
                  />
                  Enable for real runs
                </label>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => saveKey(provider.id)}
                    disabled={busy === `save-${provider.id}`}
                    className="rounded-md bg-primary px-4 py-2 text-xs font-medium text-primary-foreground hover:opacity-95 disabled:opacity-50"
                  >
                    Save key
                  </button>
                  <button
                    type="button"
                    onClick={() => testKey(provider.id)}
                    disabled={!saved || busy === `test-${provider.id}`}
                    className="rounded-md border border-panel-border px-4 py-2 font-mono text-xs text-foreground hover:border-foreground/40 disabled:opacity-50"
                  >
                    Test
                  </button>
                  <button
                    type="button"
                    onClick={() => clearKey(provider.id)}
                    disabled={!saved || busy === `clear-${provider.id}`}
                    className="rounded-md border border-destructive/40 px-4 py-2 font-mono text-xs text-destructive hover:border-destructive disabled:opacity-50"
                  >
                    Clear
                  </button>
                </div>
                <p className="font-mono text-[11px] text-muted-foreground">
                  {saved
                    ? `Configured · last test: ${saved.test_status.replace("_", " ")}`
                    : "Mock runs work without an API key."}
                </p>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─── Console page ─────────────────────────────────────────────────────────

// ─── Budget / capability meter ─────────────────────────────────────────────

/**
 * Spend against the monthly budget.
 *
 * Fed by `getUsage()`, polled every 60 seconds from `ConsolePage` and refreshed
 * immediately whenever a run finishes. Exceeding the cap is what produces the
 * HTTP 402 handled in `RunSetupCard.doStart`.
 */
function UsageBudgetPanel({
  usage,
  capabilities,
  loading,
}: {
  usage: UsageOut | null;
  capabilities: CapabilitiesOut | null;
  loading: boolean;
}) {
  const spend = usage?.monthly_spend ?? 0;
  const limit = usage?.monthly_limit ?? 0;
  const pct = limit > 0 ? Math.min(100, (spend / limit) * 100) : 0;
  const over = Boolean(usage?.over_budget);
  const reasons = capabilities?.reasons ?? {};

  const caps: Array<{ key: string; label: string; ok: boolean }> = [
    { key: "real_run_ready", label: "runs", ok: Boolean(capabilities?.real_run_ready) },
    { key: "execute_ready", label: "code execution", ok: Boolean(capabilities?.execute_ready) },
    { key: "figures_ready", label: "figures", ok: Boolean(capabilities?.figures_ready) },
  ];

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="spend · capability"
        title="Monthly budget"
        subtitle="Live from the runner. Runs are blocked once the monthly cap is reached."
        meta={
          <>
            <MetaChip tone={over ? "error" : usage ? "success" : "muted"}>
              {loading ? "loading" : over ? "over budget" : usage ? "within budget" : "unavailable"}
            </MetaChip>
            {capabilities?.enabled_providers?.length ? (
              <MetaChip>{capabilities.enabled_providers.join(" · ")}</MetaChip>
            ) : null}
          </>
        }
      />
      <div className="p-5">
        {over && (
          <div className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 px-3 py-2 font-mono text-[11px] text-destructive">
            Monthly spend cap reached — new runs are disabled until the limit resets or is raised.
          </div>
        )}

        <div className="grid gap-3 sm:grid-cols-3">
          <StatTile label="spent" value={usage ? `$${spend.toFixed(2)}` : "—"} />
          <StatTile label="limit" value={usage ? `$${limit.toFixed(2)}` : "—"} />
          <StatTile
            label="remaining"
            value={usage ? `$${usage.remaining.toFixed(2)}` : "—"}
            hint={over ? "cap reached" : undefined}
          />
        </div>

        <div className="mt-4 h-1.5 w-full overflow-hidden rounded-full bg-panel-border">
          <div
            className={`h-full rounded-full ${over ? "bg-destructive" : "bg-primary"}`}
            style={{ width: `${over ? 100 : pct}%` }}
          />
        </div>

        <div className="mt-5 grid gap-2 sm:grid-cols-3">
          {caps.map((c) => (
            <div
              key={c.key}
              className="rounded-md border border-panel-border bg-background/40 px-3 py-2"
            >
              <div className="flex items-center gap-2">
                <Dot tone={c.ok ? "success" : "warning"} />
                <span className="font-mono text-[11px] text-foreground">{c.label}</span>
              </div>
              {!c.ok && (
                <p className="mt-1 text-[11px] leading-snug text-muted-foreground">
                  {reasons[c.key] ?? "unavailable on the server"}
                </p>
              )}
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

// ─── Gate G3 · targeted paper revision ─────────────────────────────────────

// ─── Revisions ─────────────────────────────────────────────────────────────

/**
 * GATE G3 — request targeted edits to the finished paper.
 *
 * The alternative to regenerating everything: describe a specific change and
 * have the agent apply just that. Far cheaper than a fresh run, and it keeps
 * the parts that were already approved.
 *
 * Follows the same estimate → preview → apply discipline as gate G2, with one
 * addition — `previewRevision` returns the actual DIFF, so the reviewer sees
 * the exact edit before committing to it, not merely its projected cost. The
 * matching API functions are in the "Targeted paper revisions" section of
 * lib/api.ts.
 */
function RevisionGate({ run, onRunUpdated }: { run: RunOut | null; onRunUpdated: (r: RunOut) => void }) {
  const [excerpt, setExcerpt] = useState("");
  const [instruction, setInstruction] = useState("");
  const [estimate, setEstimate] = useState<number | null>(null);
  const [preview, setPreview] = useState<RevisionOut | null>(null);
  const [history, setHistory] = useState<RevisionOut[]>([]);
  const [busy, setBusy] = useState<null | "estimate" | "preview" | "apply">(null);
  const [applyOpen, setApplyOpen] = useState(false);

  const runId = run?.id ?? null;
  const draft = run?.draft_markdown ?? "";
  const eligible = Boolean(run && ["review", "completed", "revising"].includes(run.status));

  useEffect(() => {
    setExcerpt("");
    setInstruction("");
    setEstimate(null);
    setPreview(null);
    setHistory([]);
    if (runId == null) return;
    listRevisions(runId)
      .then(setHistory)
      .catch(() => {
        /* backend may not expose history yet */
      });
  }, [runId]);

  const captureSelection = () => {
    const sel = typeof window !== "undefined" ? window.getSelection()?.toString().trim() : "";
    if (!sel) {
      toast.error("Select text inside the draft first.");
      return;
    }
    setExcerpt(sel);
    setPreview(null);
    setEstimate(null);
    toast.success(`Target captured · ${sel.length} chars`);
  };

  const doEstimate = async () => {
    if (runId == null) return;
    setBusy("estimate");
    try {
      const res = await estimateRevision(runId, { instruction, target_excerpt: excerpt });
      setEstimate(res.cost_estimate);
      toast.success(`Revision estimate · $${res.cost_estimate.toFixed(2)}`);
    } catch (e) {
      toast.error(friendlyError(e, "Could not estimate revision"));
    } finally {
      setBusy(null);
    }
  };

  const doPreview = async () => {
    if (runId == null) return;
    setBusy("preview");
    try {
      const rev = await previewRevision(runId, { instruction, target_excerpt: excerpt });
      setPreview(rev);
      toast.success("Replacement drafted — review before applying.");
    } catch (e) {
      toast.error(friendlyError(e, "Could not draft revision"));
    } finally {
      setBusy(null);
    }
  };

  const doApply = async (notes: string) => {
    if (runId == null || !preview) return;
    setBusy("apply");
    try {
      const updated = await applyRevision(runId, preview.id, notes);
      onRunUpdated(updated);
      setApplyOpen(false);
      setPreview(null);
      setEstimate(null);
      setExcerpt("");
      setInstruction("");
      const list = await listRevisions(runId).catch(() => [] as RevisionOut[]);
      setHistory(list);
      toast.success("Revision applied to the paper draft.");
    } catch (e) {
      toast.error(friendlyError(e, "Could not apply revision"));
    } finally {
      setBusy(null);
    }
  };

  const ready = excerpt.trim().length > 20 && instruction.trim().length > 8;

  return (
    <div className="panel overflow-hidden">
      <BentoHead
        kicker="human gate · g3"
        title="Targeted paper revision"
        subtitle="Select a passage in the draft, describe the fix, preview the replacement with its cost, then approve the apply. Scoped edits only — this never regenerates the whole paper."
        meta={
          <>
            <MetaChip tone={eligible ? "success" : "muted"}>
              {eligible ? "gate open" : "awaiting run output"}
            </MetaChip>
            {history.length > 0 && <MetaChip>{history.length} revisions</MetaChip>}
          </>
        }
      />

      <div className="space-y-4 p-5">
        {!eligible ? (
          <p className="text-[13px] text-muted-foreground">
            Revisions unlock once a run reaches <span className="font-mono">review</span> or{" "}
            <span className="font-mono">completed</span> and a draft exists.
          </p>
        ) : (
          <>
            <div>
              <div className="mb-2 flex items-center justify-between">
                <label className="mono-label">1 · target passage</label>
                <button
                  type="button"
                  onClick={captureSelection}
                  className="rounded-md border border-panel-border px-2.5 py-1 font-mono text-[10.5px] text-muted-foreground hover:text-foreground"
                >
                  use draft selection
                </button>
              </div>
              <textarea
                value={excerpt}
                onChange={(e) => {
                  setExcerpt(e.target.value);
                  setPreview(null);
                }}
                rows={4}
                placeholder="Paste (or select above) the exact passage to replace…"
                className="w-full resize-y rounded-md border border-panel-border bg-background/50 p-3 font-mono text-[12px] leading-relaxed text-foreground outline-none focus:border-primary/60"
              />
              {excerpt && draft && !draft.includes(excerpt.trim()) && (
                <p className="mt-1 font-mono text-[10.5px] text-warning">
                  passage not found verbatim in the current draft
                </p>
              )}
            </div>

            <div>
              <label className="mono-label">2 · instruction</label>
              <textarea
                value={instruction}
                onChange={(e) => {
                  setInstruction(e.target.value);
                  setPreview(null);
                }}
                rows={3}
                placeholder="e.g. Replace the unsupported claim with a hedged statement and cite the 2024 benchmark table."
                className="mt-2 w-full resize-y rounded-md border border-panel-border bg-background/50 p-3 text-[13px] leading-relaxed text-foreground outline-none focus:border-primary/60"
              />
            </div>

            <div className="flex flex-wrap items-center gap-2">
              <Gated
                perm="run:start"
                disabled={!ready || Boolean(busy)}
                onClick={doEstimate}
                className="rounded-md border border-panel-border px-3.5 py-2 font-mono text-xs text-foreground hover:border-primary/50 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === "estimate" ? "Estimating…" : "Estimate cost"}
              </Gated>
              <Gated
                perm="run:start"
                disabled={!ready || Boolean(busy)}
                onClick={doPreview}
                className="rounded-md bg-primary px-3.5 py-2 font-mono text-xs font-medium text-primary-foreground hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {busy === "preview" ? "Drafting…" : "Preview replacement"}
              </Gated>
              {estimate != null && (
                <span className="font-mono text-[11px] text-muted-foreground">
                  est. ${estimate.toFixed(2)}
                </span>
              )}
            </div>

            {preview && (
              <div className="rounded-md border border-panel-border bg-background/40 p-4">
                <div className="mono-label">3 · proposed replacement</div>
                <div className="mt-3 grid gap-3 lg:grid-cols-2">
                  <div>
                    <div className="mono-label text-destructive">removing</div>
                    <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap rounded border border-destructive/25 bg-destructive/5 p-3 font-mono text-[11.5px] leading-relaxed text-foreground">
                      {preview.target_excerpt || excerpt}
                    </pre>
                  </div>
                  <div>
                    <div className="mono-label text-success">inserting</div>
                    <pre className="mt-1 max-h-52 overflow-auto whitespace-pre-wrap rounded border border-panel-border bg-panel p-3 font-mono text-[11.5px] leading-relaxed text-foreground">
                      {preview.replacement_markdown}
                    </pre>
                  </div>
                </div>
                <div className="mt-4 flex flex-wrap items-center gap-2">
                  <Gated
                    perm="run:start"
                    disabled={Boolean(busy)}
                    onClick={() => setApplyOpen(true)}
                    className="rounded-md bg-success px-3.5 py-2 font-mono text-xs font-medium text-background hover:opacity-95 disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    Approve & apply
                  </Gated>
                  <button
                    type="button"
                    onClick={() => setPreview(null)}
                    className="rounded-md border border-panel-border px-3 py-2 font-mono text-xs text-muted-foreground hover:text-foreground"
                  >
                    Discard preview
                  </button>
                  {preview.cost_estimate != null && (
                    <span className="font-mono text-[11px] text-muted-foreground">
                      cost ${preview.cost_estimate.toFixed(2)}
                    </span>
                  )}
                </div>
              </div>
            )}

            {history.length > 0 && (
              <div>
                <div className="mono-label">applied history</div>
                <ul className="mt-2 space-y-1.5">
                  {history.map((r) => (
                    <li
                      key={r.id}
                      className="flex items-start gap-3 rounded-md border border-panel-border bg-background/40 px-3 py-2"
                    >
                      <span className="mt-0.5 font-mono text-[10.5px] text-muted-foreground">
                        #{r.id}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-[12.5px] text-foreground">
                        {r.instruction}
                      </span>
                      <StatusBadge status={r.status} />
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>

      <DecisionDialog
        open={applyOpen}
        onOpenChange={setApplyOpen}
        tone="approve"
        kicker="decision · gate g3"
        title="Apply revision to the paper"
        description="This rewrites the selected passage in the run's draft. The prior text is kept in the revision history for audit."
        summary={[
          { label: "run", value: run ? `#${run.id}` : "—" },
          { label: "revision", value: preview ? `#${preview.id}` : "—" },
          {
            label: "cost",
            value:
              preview?.cost_estimate != null
                ? `$${preview.cost_estimate.toFixed(2)}`
                : estimate != null
                  ? `$${estimate.toFixed(2)}`
                  : "—",
          },
          { label: "scope", value: `${excerpt.trim().length} chars replaced` },
        ]}
        confirmLabel="Confirm & apply"
        confirmingLabel="Applying…"
        placeholder="Why is this edit correct? Reviewer, source, finding it resolves…"
        busy={busy === "apply"}
        onConfirm={doApply}
      />
    </div>
  );
}

// ===========================================================================
//  ⭐ THE ENTRY POINT — start here. Everything above is a piece this assembles.
// ===========================================================================
//
//  This component owns ALL the shared state for the console and passes it down.
//  That is why it is long: the ~15 `useState` calls below are the page's single
//  source of truth, and the components above are largely presentational.
//
//  Its five effects, in order:
//    1. auth gate         — verify the session, bounce to /auth if absent
//    2. load projects     — once authenticated
//    3. capabilities+usage — polled every 60s, drives gating across the page
//    4. load active project — whenever the selected project changes
//    5. live logs         — WebSocket subscription with a polling fallback
//
//  Effect 5 is the most intricate code in the file; it is commented in detail.
function ConsolePage() {
  // ── Session ──────────────────────────────────────────────────────────────
  // `authChecked` is separate from `user` on purpose. Before the check
  // completes both are falsy, but "still checking" and "definitely signed out"
  // demand different UI — without this flag the page would flash a signed-out
  // state before the session resolved.
  const [user, setUser] = useState<UserOut | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
  const [loggingOut, setLoggingOut] = useState(false);

  // ── Projects ─────────────────────────────────────────────────────────────
  // `activeId` (the selection) is deliberately separate from `activeProject`
  // (the loaded detail). Changing the id triggers a fetch; keeping them apart
  // means the previous project stays on screen while the next one loads,
  // instead of the page blanking between the two.
  const [projects, setProjects] = useState<ProjectOut[]>([]);
  const [activeId, setActiveId] = useState<number | null>(null);
  const [activeProject, setActiveProject] = useState<ProjectOut | null>(null);

  // ── The active project's workflow state ──────────────────────────────────
  // These five mirror the gate sequence described in the file header:
  // brief (G1) → run (G2) → live events → findings → export (G4).
  const [brief, setBrief] = useState<BriefOut | null>(null);
  const [activeRun, setActiveRun] = useState<RunOut | null>(null);
  const [events, setEvents] = useState<LogEvent[]>([]);
  const [findings, setFindings] = useState<ReviewFindingOut[]>([]);
  const [exported, setExported] = useState(false);

  // ── Account-level metadata ───────────────────────────────────────────────
  // What this deployment can do, and how much has been spent. Both feed the
  // gating logic — controls that would fail are disabled rather than offered.
  const [capabilities, setCapabilities] = useState<CapabilitiesOut | null>(null);
  const [usage, setUsage] = useState<UsageOut | null>(null);
  const [metaLoading, setMetaLoading] = useState(true);

  // ── Refs for things that are NOT rendered ────────────────────────────────
  // A socket handle and an interval id are infrastructure, not display data.
  // Holding them in `useRef` avoids a re-render every time they change — and
  // crucially, a ref survives re-renders so cleanup can still reach them.
  const wsRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const navigate = useNavigate();

  // ── auth gate
  // A SECOND auth check, in addition to the route's `beforeLoad` guard above.
  // Not redundant: `beforeLoad` runs once on entry, while this also catches a
  // session expiring during a long-lived console session.
  useEffect(() => {
    // Same `cancelled` guard as `LiveTelemetry` in index.tsx — stops a late
    // response from navigating a component that has already unmounted.
    let cancelled = false;
    me()
      .then((u) => {
        if (cancelled) return;
        if (!u) {
          navigate({ to: "/auth", search: { redirect: window.location.href }, replace: true });
          return;
        }
        setUser(u);
        setAuthChecked(true);
      })
      .catch(() => {
        if (!cancelled) {
          navigate({ to: "/auth", search: { redirect: window.location.href }, replace: true });
        }
      });
    return () => {
      cancelled = true;
    };
  }, [navigate]);

  // ── load projects when logged in
  const refreshProjects = async () => {
    try {
      const list = await listProjects();
      setProjects(list);
      if (list.length > 0 && activeId == null) setActiveId(list[0].id);
    } catch (e) {
      toast.error(friendlyError(e, "Could not load projects"));
    }
  };

  useEffect(() => {
    if (authChecked) void refreshProjects();
    // The eslint-disable suppresses the exhaustive-deps warning about
    // `refreshProjects`, which is redefined on every render and would therefore
    // re-trigger this effect endlessly if listed.
    //
    // This suppression appears several times below. It works, but it is a smell
    // — the idiomatic fixes are to wrap the function in `useCallback` or to
    // move the fetch into TanStack Query, as `lib/campaign-queries.ts` does.
    // Treat each one as a marker for future cleanup rather than a pattern to
    // copy.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authChecked]);

  // ── capabilities + monthly usage drive gating across the console
  const refreshMeta = async () => {
    setMetaLoading(true);
    // `Promise.all` runs both requests CONCURRENTLY rather than sequentially,
    // so the pair costs one round trip instead of two.
    //
    // The per-promise `.catch(() => null)` is the important detail: `Promise.all`
    // rejects as soon as ANY input rejects, which would discard a successful
    // result alongside the failed one. Catching inside each promise converts a
    // failure into `null`, so one endpoint being down does not blank the other.
    const [caps, use] = await Promise.all([
      getCapabilities().catch(() => null),
      getUsage().catch(() => null),
    ]);
    setCapabilities(caps);
    setUsage(use);
    setMetaLoading(false);
  };

  useEffect(() => {
    if (!authChecked) return;
    void refreshMeta();
    const id = setInterval(() => void refreshMeta(), 60_000);
    return () => clearInterval(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authChecked]);

  useEffect(() => {
    // Refresh spend immediately when a run finishes, rather than waiting up to
    // 60 seconds for the next poll. A run is what consumes budget, so this is
    // exactly the moment the usage figures become stale.
    if (authChecked && activeRun && ["completed", "failed", "cancelled"].includes(activeRun.status)) {
      void refreshMeta();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRun?.status]);

  // ── refresh active project & derived state
  const loadActive = async (projectId: number) => {
    try {
      const p = await getProject(projectId);
      setActiveProject(p);
      setActiveRun(p.latest_run ?? null);
      setFindings(p.review_findings ?? []);
      setExported(false);

      // brief: try to pull the latest brief from the project payload if the
      // backend inlines it; otherwise the user can generate a fresh one.
      // Defensive field probing: the backend may inline the brief under either
      // name, or omit it entirely. The intersection casts (`ProjectOut & {...}`)
      // tell TypeScript about fields absent from the declared type — a sign
      // that `ProjectOut` in lib/api.ts has drifted from what the API actually
      // returns. The proper fix is to add these fields to that interface.
      const bp =
        (p as ProjectOut & { latest_brief?: BriefOut | null; brief?: BriefOut | null })
          .latest_brief ??
        (p as ProjectOut & { brief?: BriefOut | null }).brief ??
        null;
      setBrief(bp);
    } catch (e) {
      toast.error(friendlyError(e, "Could not load project"));
    }
  };

  useEffect(() => {
    if (activeId != null) void loadActive(activeId);
  }, [activeId]);

  // ── live logs subscription
  //
  // THE MOST INTRICATE EFFECT IN THE FILE. It does four things:
  //   1. tears down any previous socket and poll timer
  //   2. seeds the view with historical logs already on the run
  //   3. opens a WebSocket for live updates (only if the run is active)
  //   4. falls back to HTTP polling if the socket fails
  //
  // The fallback is what makes it long. The design principle is the same as in
  // `campaigns.$id.tsx`: a failed socket must degrade to something slower, not
  // to a broken page.
  useEffect(() => {
    // Reset log stream when the active run changes.
    // This teardown runs at the START of the effect as well as in the cleanup
    // below, because switching runs must not leave the previous run's socket
    // open or its log lines on screen.
    setEvents([]);
    wsRef.current?.close();
    wsRef.current = null;
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }

    if (!activeRun) return;
    // A finished run emits nothing further, so there is no point opening a
    // socket — just show the stored logs and stop.
    if (!["queued", "running", "review"].includes(activeRun.status)) {
      // seed with historical logs if present
      if (activeRun.logs?.length) {
        setEvents(activeRun.logs.map((l) => ({ type: "log", ...l })));
      }
      return;
    }

    // Seed with historical logs first
    if (activeRun.logs?.length) {
      setEvents(activeRun.logs.map((l) => ({ type: "log", ...l })));
    }

    try {
      const ws = openRunLogs(activeRun.id, (ev) => {
        // NOTE: unbounded growth. Unlike the search trace in
        // `campaigns.$id.tsx`, which caps its buffer with `.slice(-400)`, this
        // array grows for the life of the run. Fine for a run of ordinary
        // length; a very long or very chatty run would accumulate memory.
        setEvents((cur) => [...cur, ev]);
        if (ev.type === "status" && ev.status) {
          setActiveRun((r) => (r ? { ...r, status: ev.status ?? r.status } : r));
          // On a terminal status, fetch the full record once more. The socket
          // reports the status change but not the artifacts, final cost, or
          // review findings the backend writes as the run closes out — so
          // without this refresh the page would show "completed" with none of
          // the results attached.
          if (["completed", "failed", "cancelled"].includes(ev.status)) {
            // final refresh to pull cost/draft/artifacts/findings
            void (async () => {
              try {
                const fresh = await getRun(activeRun.id);
                setActiveRun(fresh);
                if (activeId != null) {
                  const flist = await listReviews(activeId);
                  setFindings(flist);
                }
              } catch {
                /* ignore */
              }
            })();
          }
        }
      });
      ws.onerror = () => {
        // Fallback: poll run status if the WS handshake fails (e.g. auth quirk).
        // The user sees a slightly less responsive page rather than a dead one.
        //
        // The `pollRef.current` guard makes this IDEMPOTENT: `onerror` can fire
        // more than once, and without it each firing would start another timer,
        // multiplying the request rate with every error.
        if (pollRef.current) return;
        pollRef.current = setInterval(async () => {
          try {
            const fresh = await getRun(activeRun.id);
            setActiveRun(fresh);
            if (fresh.logs) {
              setEvents(fresh.logs.map((l) => ({ type: "log", ...l })));
            }
            // The polling loop stops ITSELF once the run reaches a terminal
            // state — otherwise it would keep hitting the backend every three
            // seconds for a run that can no longer change.
            if (["completed", "failed", "cancelled"].includes(fresh.status)) {
              if (pollRef.current) {
                clearInterval(pollRef.current);
                pollRef.current = null;
              }
            }
          } catch {
            /* ignore */
          }
        }, 3000);
      };
      wsRef.current = ws;
    } catch {
      /* ignore */
    }

    // Cleanup must tear down BOTH mechanisms, since either could be active.
    // Missing either one leaks: a socket that stays open, or a timer that polls
    // forever after the user navigates away.
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
      if (pollRef.current) {
        clearInterval(pollRef.current);
        pollRef.current = null;
      }
    };
  }, [activeRun?.id, activeRun?.status]);
  // ^ Depends on the id AND the status — using `activeRun` itself would re-run
  //   this whole teardown/setup cycle on every unrelated field change.

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      // Clear the in-app/FastAPI session cookie first, then the Supabase
      // OAuth session (Google/Apple/etc.) so the next visit is unauthenticated.
      // ORDER MATTERS, and so does the `.catch`. The FastAPI session is cleared
      // first, but its failure is swallowed — if the backend is unreachable we
      // must still clear the local Supabase session, or the user would be
      // trapped in a signed-in UI they cannot sign out of.
      await apiLogout().catch(() => {
        /* backend may be unavailable; still clear local session */
      });
      await supabase.auth.signOut();
      toast.success("Signed out");
      navigate({ to: "/auth", replace: true });
    } catch {
      toast.error("Could not sign out.");
      setLoggingOut(false);
    }
  };

  // Two derived booleans driving the gate sequence: the run cannot start until
  // the brief is approved, and export cannot happen until the run completes.
  // Deriving them on each render (rather than storing them in state) means they
  // can never fall out of sync with the data they come from.
  const briefApproved = Boolean(brief?.is_approved);
  const runComplete = activeRun?.status === "completed";


  // EARLY RETURN while the session is being verified. Everything below this
  // point can safely assume an authenticated `user`, which is what keeps the
  // main render free of null checks.
  if (!authChecked || !user) {
    return (
      <main className="theme-console grid min-h-screen place-items-center bg-background px-6">
        <div className="panel max-w-md p-6 text-center">
          <div className="mx-auto mb-4 flex h-10 w-10 items-center justify-center rounded-md border border-panel-border bg-background font-serif text-foreground">
            AL
          </div>
          <p className="mono-label">checking session</p>
          <h1 className="mt-3 font-serif text-3xl text-foreground">Console requires sign in.</h1>
        </div>
      </main>
    );
  }

  return (
    <AuthProvider user={user}>
    <div className="theme-console relative flex min-h-screen bg-background">
      <WorkspaceTheme />
      <ConsoleSidebar user={user} onLogout={handleLogout} loggingOut={loggingOut} />

      <div className="min-w-0 flex-1">
      <ConsoleTopbar user={user} onLogout={handleLogout} loggingOut={loggingOut} />

      <main className="relative mx-auto w-full max-w-[1280px] px-6 pb-24 pt-6">

        {/* Command bar */}
        <section
          id="workspace"
          className="sticky top-0 z-30 -mx-6 border-b border-border/70 bg-background/85 px-6 py-4 backdrop-blur-md"
        >
          <div className="grid grid-cols-[minmax(0,1fr)_auto] items-center gap-4">
            <div className="min-w-0">
              <div className="flex items-center gap-2 font-mono text-[10.5px] uppercase tracking-[0.16em] text-muted-foreground">
                <span>agentlab</span>
                <span className="text-primary">/</span>
                <span className="text-foreground">workspace</span>
              </div>
              <h1 className="mt-1 truncate font-serif text-[26px] leading-tight tracking-tight text-foreground">
                {activeProject ? activeProject.title : "Workspace"}
              </h1>
            </div>
            <div className="flex items-center gap-2">
              <span className="hidden items-center gap-2 rounded-full border border-panel-border bg-panel px-3 py-1.5 font-mono text-[10.5px] uppercase tracking-[0.14em] text-muted-foreground sm:inline-flex">
                <Dot tone={hasRealBackend() ? "success" : "warning"} />{" "}
                {hasRealBackend() ? "backend live" : "no backend"}
              </span>
              <NewProjectSheet
                onCreate={(p) => {
                  setProjects((cur) => [p, ...cur]);
                  setActiveId(p.id);
                }}
              />
            </div>
          </div>
        </section>

        {/* Intro + project rail */}
        <section className="mt-7 grid gap-6 lg:grid-cols-[minmax(0,320px)_minmax(0,1fr)] lg:items-start">
          <div className="panel relative overflow-hidden p-5">
            <div className="gradient-rule absolute inset-x-0 top-0" />
            <p className="mono-label">research workspace</p>
            <p className="mt-3 text-[14px] leading-[1.65] text-muted-foreground">
              Pick a project, edit its brief, set a budget, then start a run.
              Logs stream live; the export stays locked until the final gate is
              signed off.
            </p>
          </div>

          <div className="min-w-0">
            <div className="mb-3 flex items-center justify-between">
              <span className="mono-label">projects</span>
              <span className="font-mono text-[11px] text-muted-foreground">
                {projects.length} total
              </span>
            </div>
            <ProjectRail
              projects={projects}
              activeId={activeId}
              setActiveId={setActiveId}
            />
          </div>
        </section>

        {/* State strip */}
        <section
          id="pipeline"
          className="panel mt-6 grid divide-y divide-panel-border overflow-hidden sm:grid-cols-2 sm:divide-y-0 xl:grid-cols-4 [&>*+*]:sm:border-l [&>*+*]:sm:border-panel-border"
        >
          <StateCard
            label="PROJECT"
            value={activeProject?.status ?? (activeId ? "loading" : "none")}
            caption="current workspace state"
          />
          <StateCard
            label="BRIEF"
            value={brief ? (brief.is_approved ? "approved" : `v${brief.version} draft`) : "none"}
            caption="human gate G1"
          />
          <StateCard
            label="RUN"
            value={activeRun ? activeRun.status : "not started"}
            caption={activeRun ? `run #${activeRun.id} · gate G2` : "gate G2"}
          />
          <StateCard
            label="COST"
            value={
              activeRun ? `$${activeRun.actual_cost.toFixed(2)}` : activeProject ? "pending" : "—"
            }
            caption={
              usage
                ? `$${usage.remaining.toFixed(2)} left this month`
                : activeRun
                  ? `of $${activeRun.budget_threshold.toFixed(2)}`
                  : "actual · budget"
            }
          />
        </section>

        {/* Connection + provider keys */}
        <section className="mt-8 grid gap-6">
          <AgentConnectionPanel
            onChange={() => {
              setProjects([]);
              setActiveId(null);
              setActiveProject(null);
              setActiveRun(null);
              setBrief(null);
              setEvents([]);
              setFindings([]);
              void refreshProjects();
              health()
                .then(() => toast.success("Backend healthy"))
                .catch(() => {
                  /* AgentConnection panel already shows the Test result */
                });
            }}
          />
          <ProviderKeysGate />
          <UsageBudgetPanel usage={usage} capabilities={capabilities} loading={metaLoading} />
        </section>

        <RoleBanner />

        {activeProject ? (
          <>
            <section className="mt-8 grid gap-6 xl:grid-cols-2">
              <IdeaCard project={activeProject} onIdeaSaved={setActiveProject} />
              <BriefCard project={activeProject} brief={brief} setBrief={setBrief} />
            </section>

            <section className="mt-6 grid gap-6 xl:grid-cols-2">
              <RunSetupCard
                project={activeProject}
                briefApproved={briefApproved}
                activeRun={activeRun}
                capabilities={capabilities}
                usage={usage}
                onRunStarted={(run) => {
                  setActiveRun(run);
                  setEvents([]);
                }}
                onRunCancelled={setActiveRun}
              />
              <LiveLogs run={activeRun} events={events} />
            </section>

            <section className="mt-6 space-y-6">
              <ArtifactsAndDraft run={activeRun} exportApproved={exported} />
              <RevisionGate run={activeRun} onRunUpdated={setActiveRun} />
              <AgenticActionsCard run={activeRun} />
              <ExecutionResultsCard run={activeRun} />
            </section>

            <section id="review" className="mt-6 space-y-6">
              <StatusTimeline
                project={activeProject}
                brief={brief}
                activeRun={activeRun}
                findings={findings}
                exported={exported}
                actorName={user?.name || user?.email || "user"}
                projects={projects}
                onSelectProject={setActiveId}
              />
              <ReviewerPanel findings={findings} />
            </section>

            <section id="export" className="mt-6 space-y-4">
              <FinalExport
                project={activeProject}
                runComplete={Boolean(runComplete)}
                hasFindings={findings.length > 0}
                activeRun={activeRun}
                onExported={() => setExported(true)}
              />
              {exported && (
                <div className="rounded-md border border-primary/30 bg-primary/5 px-4 py-3 font-mono text-[12px] text-foreground">
                  Final package approved for project #{activeProject.id}.
                </div>
              )}
            </section>
          </>
        ) : (
          <section className="panel mt-10 overflow-hidden p-10 text-center">
            <p className="mono-label">no project selected</p>
            <h2 className="mt-3 font-serif text-3xl text-foreground">
              Create your first research project.
            </h2>
            <p className="mx-auto mt-3 max-w-md text-sm text-muted-foreground">
              Use the “New project” button above. Each project moves through
              idea, brief, run, review, export gates.
            </p>
          </section>
        )}
      </main>


      </div>

      <Toaster
        theme="dark"
        position="bottom-right"
        toastOptions={{ style: { fontFamily: "var(--font-mono)" } }}
      />
    </div>
    </AuthProvider>
  );
}

