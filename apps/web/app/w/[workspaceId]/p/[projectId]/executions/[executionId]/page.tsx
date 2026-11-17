import { notFound } from "next/navigation";

import { executionStatusTone, pillClassName } from "@/lib/agents/presentation";
import type { ExecutionStep } from "@/lib/api/executions";
import { loadExecutionDetail } from "@/lib/executions/load";
import {
  executionStatusLabel,
  executionTiming,
  initiatorSentence,
  stepKindLabel,
  stepOutputNote,
  stepOutputRows,
  stepStatusLabel,
  stepStatusTone,
  stepTiming,
  stepsEmptyState,
} from "@/lib/executions/presentation";
import { requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = { title: "Execution · AgentWorld" };

/**
 * One execution: what it was asked to do, what each step did, and how it
 * ended.
 *
 * **This page authorizes itself**, like the agent detail page and for the same
 * reason - a layout and its page execute concurrently, so the ordering lives
 * in `loadExecutionDetail`, which fetches the project, refuses a workspace
 * mismatch, and only then asks for the execution and its steps.
 *
 * **Steps are rendered in the order they arrive.** The API returns them in the
 * published version's own order, which is not the order they finished in and
 * is not recoverable from the step rows alone, so sorting them here would be
 * undoing work and losing the answer.
 *
 * Deliberately absent: the execution's `input`, `output` and `error` are
 * free-form JSON that the API does not narrow, so this prints whether each is
 * present rather than dumping it. A step's input is not served at all. And
 * there is no refresh: the page is server-rendered per request, so a running
 * execution is a snapshot and says so rather than pretending to be live.
 */
export default async function ExecutionDetailPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string; executionId: string }>;
}) {
  const { workspaceId, projectId, executionId } = await params;
  const { token } = await requireSession(
    `/w/${workspaceId}/p/${projectId}/executions/${executionId}`,
  );

  const result = await loadExecutionDetail(token, workspaceId, projectId, executionId);

  if (result.kind === "not_found") notFound();

  if (result.kind === "error") {
    return (
      <section className="stack">
        <h1 className="title">Execution</h1>
        <div className="card">
          <p className="form__error" role="alert">
            {result.message}
          </p>
          {result.requestId !== undefined && (
            <p className="muted">
              Request <code>{result.requestId}</code> if you are reporting this.
            </p>
          )}
        </div>
      </section>
    );
  }

  const { project, execution, steps } = result;
  const empty = stepsEmptyState();
  const running = execution.finished_at === null;

  return (
    <section className="stack">
      <div>
        <h1 className="title">
          Execution{" "}
          <span className={pillClassName(executionStatusTone(execution.status))}>
            {executionStatusLabel(execution.status)}
          </span>
        </h1>
        <p className="muted">
          <code>{execution.id}</code> in {project.name}
        </p>
        <p className="muted">
          {initiatorSentence(execution)} · {executionTiming(execution)}
        </p>
        {running && (
          // Said once, here, rather than on every step: this page is a
          // snapshot, and a reader deciding whether to reload should be told
          // so rather than left to infer it from a stale step list.
          <p className="muted">
            This execution has not finished. The page is a snapshot - reload to see where it
            got to.
          </p>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Steps</h2>
        {steps.length === 0 ? (
          <>
            <p>{empty.headline}</p>
            <p className="muted">{empty.detail}</p>
          </>
        ) : (
          <ol className="list">
            {steps.map((step) => (
              <StepItem key={`${step.step_key}:${String(step.attempt)}`} step={step} />
            ))}
          </ol>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">The execution itself</h2>
        <dl className="kv">
          <dt>Workflow</dt>
          <dd>
            <code>{execution.workflow_id}</code>
          </dd>

          <dt>Version it is bound to</dt>
          <dd>
            {/* The version, not the workflow's current one: a running
                execution keeps the graph it started on, and republishing does
                not reach it. */}
            <code>{execution.workflow_version_id}</code>
          </dd>

          <dt>Input</dt>
          <dd>
            {Object.keys(execution.input).length === 0 ? (
              <span className="muted">None given</span>
            ) : (
              // Whether, not what: `input` is free-form JSON the API does not
              // narrow, and whoever started the execution may have put
              // anything in it.
              `${String(Object.keys(execution.input).length)} field(s) given`
            )}
          </dd>

          <dt>Result</dt>
          <dd>
            {execution.output === null ? (
              <span className="muted">None yet</span>
            ) : (
              `${String(Object.keys(execution.output).length)} field(s) produced`
            )}
          </dd>

          {execution.error !== null && (
            <>
              <dt>Error</dt>
              <dd>{errorSentence(execution.error)}</dd>
            </>
          )}
        </dl>
      </div>
    </section>
  );
}

/**
 * One step, as a list row.
 *
 * The step key is the heading because it is what an input mapping refers to
 * and what a finding points at; `attempt` is printed only when it is not the
 * first, so an ordinary execution does not read as a list of retries.
 */
function StepItem({ step }: { readonly step: ExecutionStep }) {
  const rows = stepOutputRows(step);
  const note = stepOutputNote(step);

  return (
    <li className="list__item">
      <div>
        <span className="list__title">{step.step_key}</span>{" "}
        <span className="muted">
          {stepKindLabel(step.kind)}
          {step.agent_key !== null && (
            <>
              {" · "}
              <code>{step.agent_key}</code>
            </>
          )}
        </span>
        <p className="muted">{stepTiming(step)}</p>

        {rows.map((row) => (
          // `step__output` keeps the model's own line breaks and stops an
          // unbroken token running off the card; it is the only place on the
          // site where the text was written by something other than a person
          // filling in a form.
          <p className="step__output" key={row.label}>
            <span className="muted">{row.label}:</span> {row.value}
          </p>
        ))}
        {note !== null && <p className="muted">{note}</p>}
        {step.error !== null && (
          <p className="form__error" role="alert">
            {step.error.message} <code>{step.error.code}</code>
          </p>
        )}
      </div>

      <div>
        <span className={pillClassName(stepStatusTone(step.status))}>
          {stepStatusLabel(step.status)}
        </span>{" "}
        {step.attempt > 0 && <span className="pill">attempt {step.attempt + 1}</span>}
      </div>
    </li>
  );
}

/**
 * An execution-level error, which is not the narrowed step shape.
 *
 * `workflow_executions.error` is free-form JSON: the orchestrator writes
 * `{code, message}` there, but nothing in the schema says it must, so the two
 * fields are printed when they are strings and the shape is otherwise reported
 * rather than rendered.
 */
function errorSentence(error: Readonly<Record<string, unknown>>): string {
  const message = error["message"];
  const code = error["code"];
  if (typeof message === "string") {
    return typeof code === "string" ? `${message} (${code})` : message;
  }
  return `Recorded, in ${String(Object.keys(error).length)} field(s)`;
}
