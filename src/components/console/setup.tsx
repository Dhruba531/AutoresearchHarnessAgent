// Pre-run setup — pointing the app at a backend, and capturing the research
// question that seeds everything downstream.

import { useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import {
  clearApiBase,
  getApiBase,
  hasRealBackend,
  health,
  saveIdea,
  setApiBase,
  listUploads,
  uploadDataset,
  fileToBase64,
  runUploadedFile,
  type ProjectOut,
  type UploadOut,
  type CapabilitiesOut,
  type ExecutionOut,
} from "@/lib/api";
import { Dot, SectionHeader, friendlyError } from "./primitives";

export function AgentConnectionPanel({ onChange }: { onChange: () => void }) {
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
export function IdeaCard({
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

// ─── Dataset uploads ──────────────────────────────────────────────────────
//
// Files the researcher supplies for a project: a dataset, a notebook, notes the
// agents should read. Stored on the same durable volume as run artifacts and
// scoped to the project, so they survive redeploys and outlive any single run.
//
// This STORES files. It does not execute them — running generated or uploaded
// code is a separate, operator-gated path through an isolated RunPod sandbox
// that never executes on the API host.

/** Files the sandbox can execute: plain scripts, and notebooks whose code
 *  cells the backend concatenates into one script. */
const RUNNABLE = /\.(py|ipynb)$/i;

export function DatasetCard({
  project,
  capabilities,
}: {
  project: ProjectOut;
  capabilities: CapabilitiesOut | null;
}) {
  const [files, setFiles] = useState<UploadOut[]>([]);
  const [busy, setBusy] = useState(false);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const [endpointId, setEndpointId] = useState("");
  const [running, setRunning] = useState("");
  const [result, setResult] = useState<ExecutionOut | null>(null);

  // The sandbox needs an operator flag AND an enabled RunPod key. Rather than
  // let the button fail, it states the precondition — the same shape the rest of
  // the console uses for gated actions.
  const canExecute = Boolean(capabilities?.execute_ready);
  // With the private sandbox there is no third-party endpoint to name, so the
  // RunPod field is disabled rather than demanded.
  const usesSandbox =
    (capabilities as (typeof capabilities & { execution_backend?: string }) | null)
      ?.execution_backend === "sandbox";

  const execute = async (name: string) => {
    if (!usesSandbox && !endpointId.trim()) {
      toast.error("Enter your RunPod endpoint id first");
      return;
    }
    setRunning(name);
    setResult(null);
    try {
      const res = await runUploadedFile(project.id, name, endpointId.trim());
      setResult(res);
      if (res.ok) toast.success(`Finished in ${res.seconds.toFixed(1)}s`);
      else toast.warning(`Job ${res.status}`);
    } catch (e) {
      toast.error(friendlyError(e, "Could not run the file"));
    } finally {
      setRunning("");
    }
  };

  const refresh = async () => {
    try {
      setFiles((await listUploads(project.id)) ?? []);
    } catch {
      // A project with no uploads directory yet is not an error worth a toast.
      setFiles([]);
    }
  };

  useEffect(() => {
    void refresh();
  }, [project.id]);

  const send = async (list: FileList | null) => {
    const file = list?.[0];
    if (!file) return;
    setBusy(true);
    try {
      const b64 = await fileToBase64(file);
      const saved = await uploadDataset(project.id, file.name, b64);
      toast.success(`Uploaded ${saved.name}`, { description: humanSize(saved.size_bytes) });
      await refresh();
    } catch (e) {
      toast.error(friendlyError(e, "Could not upload file"));
    } finally {
      setBusy(false);
      if (inputRef.current) inputRef.current.value = "";
    }
  };

  return (
    <div className="panel overflow-hidden">
      <SectionHeader icon="⬓">DATASET UPLOAD</SectionHeader>
      <div className="p-5">
        <div
          onDragOver={(e) => {
            e.preventDefault();
            setDragging(true);
          }}
          onDragLeave={() => setDragging(false)}
          onDrop={(e) => {
            e.preventDefault();
            setDragging(false);
            void send(e.dataTransfer.files);
          }}
          onClick={() => inputRef.current?.click()}
          className={`cursor-pointer rounded-md border border-dashed p-6 text-center transition-colors ${
            dragging
              ? "border-primary bg-primary/5"
              : "border-panel-border bg-background/40 hover:border-foreground/40"
          }`}
        >
          <div className="font-mono text-xs text-foreground/80">
            {busy ? "Uploading…" : "Drop a file here, or click to choose"}
          </div>
          <div className="mt-1 font-mono text-[11px] text-muted-foreground">
            csv · json · txt · py · ipynb — up to 10 MB · .py/.ipynb are runnable
          </div>
          <input
            ref={inputRef}
            type="file"
            className="hidden"
            onChange={(e) => void send(e.target.files)}
          />
        </div>

        {files.some((f) => RUNNABLE.test(f.name)) && (
          <div className="mt-4">
            <label className="mono-label">runpod endpoint id</label>
            <input
              value={endpointId}
              onChange={(e) => setEndpointId(e.target.value)}
              placeholder={
                usesSandbox ? "not needed — running on the private sandbox" : "e.g. 5x9k2ab7cdef"
              }
              disabled={!canExecute || usesSandbox}
              className="mt-1 w-full rounded-md border border-panel-border bg-background/50 px-3 py-2 font-mono text-[12px] text-foreground focus:border-primary focus:outline-none disabled:opacity-50"
            />
            <p className="mt-1.5 font-mono text-[11px] text-muted-foreground">
              {usesSandbox
                ? "Running on the private sandbox: a separate container with no database, no keys, capped memory and wall-clock. Code never runs on the API host."
                : canExecute
                  ? "Runs on RunPod Serverless, isolated from the API host."
                  : "Requires the sandbox, or AGENTLAB_ALLOW_EXECUTION with a RunPod key."}
            </p>
          </div>
        )}

        {result && (
          <div className="mt-4 rounded-md border border-panel-border bg-background/40 p-3">
            <div className="flex items-center justify-between">
              <span className="mono-label">{result.ok ? "run complete" : `run ${result.status}`}</span>
              <span className="font-mono text-[11px] text-muted-foreground">
                {result.seconds.toFixed(1)}s · ${result.cost_usd.toFixed(4)}
              </span>
            </div>
            {result.stdout && (
              <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-foreground/80">
                {result.stdout}
              </pre>
            )}
            {result.stderr && (
              <pre className="mt-2 max-h-32 overflow-auto whitespace-pre-wrap font-mono text-[11px] leading-relaxed text-destructive/80">
                {result.stderr}
              </pre>
            )}
          </div>
        )}

        {files.length > 0 && (
          <ul className="mt-4 space-y-1.5">
            {files.map((f) => (
              <li
                key={f.name}
                className="flex items-center justify-between gap-3 rounded-md border border-panel-border bg-background/40 px-3 py-2"
              >
                <span className="truncate font-mono text-[12px] text-foreground/85">{f.name}</span>
                <span className="flex shrink-0 items-center gap-2">
                  <span className="font-mono text-[11px] text-muted-foreground">
                    {humanSize(f.size_bytes)}
                  </span>
                  {RUNNABLE.test(f.name) && (
                    <button
                      onClick={() => void execute(f.name)}
                      disabled={!canExecute || running !== ""}
                      title={
                        canExecute
                          ? "Run this file on the RunPod GPU sandbox"
                          : "Needs the operator execution flag and an enabled RunPod key"
                      }
                      className="rounded-sm border border-primary/40 px-2 py-0.5 font-mono text-[10px] text-primary hover:bg-primary/10 disabled:cursor-not-allowed disabled:border-panel-border disabled:text-muted-foreground"
                    >
                      {running === f.name ? "running…" : "▶ train on runpod"}
                    </button>
                  )}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

/** Bytes as a short human string; avoids "20 bytes" reading as a bug. */
function humanSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// ─── Brief ────────────────────────────────────────────────────────────────
// Gate G1 and the shared confirmation machinery used by all four gates.
