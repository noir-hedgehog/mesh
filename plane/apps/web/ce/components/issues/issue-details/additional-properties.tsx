/**
 * Copyright (c) 2023-present Plane Software, Inc. and contributors
 * SPDX-License-Identifier: AGPL-3.0-only
 * See the LICENSE file for details.
 */

import { useCallback, useEffect, useState } from "react";
import { observer } from "mobx-react";
import { Network, Play, Square, UserCheck } from "lucide-react";
import { LabelPropertyIcon } from "@plane/propel/icons";
import { TOAST_TYPE, setToast } from "@plane/propel/toast";
import { SidebarPropertyListItem } from "@/components/common/layout/sidebar/property-list-item";
import { useIssueDetail } from "@/hooks/store/use-issue-detail";
import { useLabel } from "@/hooks/store/use-label";

export type TWorkItemAdditionalSidebarProperties = {
  workItemId: string;
  workItemTypeId: string | null;
  projectId: string;
  workspaceSlug: string;
  isEditable: boolean;
  isPeekView?: boolean;
};

const KINDS = ["requirement", "bug", "task", "analysis"] as const;
const ACTIVE_RUN_STATES = new Set(["queued", "running", "waiting_for_assignee", "awaiting_approval"]);

type Runtime = {
  id: string;
  status: string;
  current_node_id: string;
  stages?: Array<{
    id: string;
    node_id: string;
    status: string;
    assigned_agent_id: string | null;
    functional_role: string | null;
    attempts: Array<{
      id: string;
      agent_id: string;
      status: string;
      provider: string;
      model_provider: string | null;
      model: string;
      cost: string | null;
      input_tokens: number;
      output_tokens: number;
      provider_state: string;
      failure_code: string;
      failure_message: string;
      evidence: Array<{ key: string; title: string; summary?: string; uri?: string; metadata?: unknown }>;
    }>;
  }>;
  handoffs?: Array<{
    from_agent_id: string | null;
    target_agent_id: string | null;
    status: string;
  }>;
};

type PublishedLoop = { id: string; name: string; slug: string; status: string; version: number };
type EligibleAgent = { agent_id: string; display_name: string; available: boolean };

export const WorkItemAdditionalSidebarProperties = observer(function WorkItemAdditionalSidebarProperties(
  props: TWorkItemAdditionalSidebarProperties
) {
  const { isEditable, projectId, workspaceSlug, workItemId } = props;
  const {
    issue: { getIssueById, updateIssue },
  } = useIssueDetail();
  const { fetchProjectLabels, getProjectLabels } = useLabel();
  const issue = getIssueById(workItemId);
  const labels = getProjectLabels(projectId);
  const [runtime, setRuntime] = useState<Runtime | null>(null);
  const [publishedLoops, setPublishedLoops] = useState<PublishedLoop[]>([]);
  const [runtimeBusy, setRuntimeBusy] = useState(false);
  const [eligibleAgents, setEligibleAgents] = useState<EligibleAgent[]>([]);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [selectedLoopId, setSelectedLoopId] = useState("");

  useEffect(() => {
    if (!labels) void fetchProjectLabels(workspaceSlug, projectId);
  }, [fetchProjectLabels, labels, projectId, workspaceSlug]);

  const loadRuntime = useCallback(async () => {
    const base = `/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/runs`;
    const [runsResponse, loopsResponse] = await Promise.all([
      fetch(`${base}/`, { credentials: "include" }),
      fetch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/loops/`, { credentials: "include" }),
    ]);
    const runsPayload = await runsResponse.json();
    const loopsPayload = await loopsResponse.json();
    const run =
      runsPayload.runs?.find(
        (item: { work_item_id: string; status: string }) =>
          item.work_item_id === workItemId && ACTIVE_RUN_STATES.has(item.status)
      ) ?? runsPayload.runs?.find((item: { work_item_id: string }) => item.work_item_id === workItemId);
    setPublishedLoops((loopsPayload.loops ?? []).filter((item: PublishedLoop) => item.status === "published"));
    if (!run) {
      setRuntime(null);
      return;
    }
    const detailsResponse = await fetch(`${base}/${run.id}/`, { credentials: "include" });
    const details = await detailsResponse.json();
    setRuntime(details.run ?? null);
  }, [projectId, workItemId, workspaceSlug]);

  useEffect(() => {
    let active = true;
    void loadRuntime().catch(() => active && setRuntime(null));
    return () => {
      active = false;
    };
  }, [loadRuntime]);

  useEffect(() => {
    if (!runtime || !ACTIVE_RUN_STATES.has(runtime.status)) return;
    const interval = window.setInterval(() => void loadRuntime().catch(() => undefined), 10000);
    return () => window.clearInterval(interval);
  }, [loadRuntime, runtime?.id, runtime?.status]);

  const latestStage = runtime?.stages?.at(-1);

  useEffect(() => {
    let active = true;
    if (latestStage?.status !== "waiting_for_assignee" || !latestStage.functional_role) {
      setEligibleAgents([]);
      setSelectedAgentId("");
      return () => {
        active = false;
      };
    }
    const query = new URLSearchParams({ roles: latestStage.functional_role });
    void fetch(`/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/eligible-agents/?${query.toString()}`, {
      credentials: "include",
    })
      .then(async (response) => {
        if (!response.ok) throw new Error("Could not load eligible Agents");
        return response.json();
      })
      .then((payload) => {
        if (!active) return;
        const agents = (payload.agents ?? []).filter((agent: EligibleAgent) => agent.available);
        setEligibleAgents(agents);
        setSelectedAgentId((current) =>
          agents.some((agent: EligibleAgent) => agent.agent_id === current) ? current : ""
        );
      })
      .catch(() => {
        if (active) setEligibleAgents([]);
      });
    return () => {
      active = false;
    };
  }, [latestStage?.functional_role, latestStage?.id, latestStage?.status, projectId, workspaceSlug]);

  if (!issue) return null;
  const kindLabels = (labels ?? []).filter((label) => label.name.toLowerCase().startsWith("kind:"));
  const current = kindLabels.find((label) => issue.label_ids?.includes(label.id));

  const updateKind = async (kind: string) => {
    const selected = kindLabels.find((label) => label.name.toLowerCase() === `kind:${kind}`);
    if (!selected) return;
    const kindIds = new Set(kindLabels.map((label) => label.id));
    const nextLabels = [...(issue.label_ids ?? []).filter((id) => !kindIds.has(id)), selected.id];
    await updateIssue(workspaceSlug, projectId, workItemId, { label_ids: nextLabels });
  };

  const latestAttempt = latestStage?.attempts?.at(-1);
  const latestHandoff = runtime?.handoffs?.at(-1);
  const canStart = isEditable && (!runtime || !ACTIVE_RUN_STATES.has(runtime.status)) && publishedLoops.length > 0;
  const canCancel = isEditable && runtime && ACTIVE_RUN_STATES.has(runtime.status);

  const startLoop = async () => {
    const loop = publishedLoops.find((item) => item.id === selectedLoopId) ?? publishedLoops[0];
    if (!loop) return;
    setRuntimeBusy(true);
    try {
      const response = await fetch(
        `/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/loops/${loop.id}/start/`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ work_item_id: workItemId }),
        }
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not start the Loop");
      setRuntime(payload.run);
    } catch (error) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Loop start failed",
        message: error instanceof Error ? error.message : "Please try again.",
      });
    } finally {
      setRuntimeBusy(false);
    }
  };

  const cancelLoop = async () => {
    if (!runtime) return;
    setRuntimeBusy(true);
    try {
      const response = await fetch(
        `/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/runs/${runtime.id}/cancel/`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "Canceled from the Work Item Runtime panel" }),
        }
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not cancel the Loop");
      setRuntime(payload.run);
    } catch (error) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Loop cancel failed",
        message: error instanceof Error ? error.message : "Please try again.",
      });
    } finally {
      setRuntimeBusy(false);
    }
  };

  const assignStage = async () => {
    if (!latestStage || !selectedAgentId) return;
    setRuntimeBusy(true);
    try {
      const response = await fetch(
        `/api/workspaces/${workspaceSlug}/projects/${projectId}/mesh/stages/${latestStage.id}/assign/`,
        {
          method: "POST",
          credentials: "include",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_agent_id: selectedAgentId,
            reason: "Assigned from the Work Item Runtime panel",
          }),
        }
      );
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || "Could not assign the Stage");
      await loadRuntime();
    } catch (error) {
      setToast({
        type: TOAST_TYPE.ERROR,
        title: "Stage assignment failed",
        message: error instanceof Error ? error.message : "Please try again.",
      });
    } finally {
      setRuntimeBusy(false);
    }
  };

  return (
    <>
      <SidebarPropertyListItem icon={LabelPropertyIcon} label="Work item kind">
        <select
          className="h-7.5 w-full grow rounded-sm bg-transparent px-2 text-body-xs-regular text-primary outline-none disabled:text-placeholder"
          value={current?.name.split(":", 2)[1] ?? ""}
          disabled={!isEditable || kindLabels.length === 0}
          onChange={(event) => void updateKind(event.target.value)}
          aria-label="Work item kind"
        >
          <option value="" disabled>
            None
          </option>
          {KINDS.map((kind) => (
            <option key={kind} value={kind}>
              {kind[0].toUpperCase() + kind.slice(1)}
            </option>
          ))}
        </select>
      </SidebarPropertyListItem>
      {(runtime || canStart) && (
        <SidebarPropertyListItem icon={Network} label="Mesh runtime" childrenClassName="min-w-0">
          <div className="flex w-full min-w-0 flex-col px-2 py-1 text-11">
            <div className="flex items-center justify-between gap-2">
              <span className="truncate text-primary">
                {runtime?.current_node_id || publishedLoops[0]?.name || "Complete"}
              </span>
              <div className="flex items-center gap-1">
                {runtime && <span className="text-secondary">{runtime.status}</span>}
                {canStart && (
                  <button
                    type="button"
                    title="Start Loop"
                    aria-label="Start Loop"
                    disabled={runtimeBusy}
                    className="grid size-6 place-items-center rounded hover:bg-surface-2"
                    onClick={() => void startLoop()}
                  >
                    <Play className="size-3.5" />
                  </button>
                )}
                {canCancel && (
                  <button
                    type="button"
                    title="Cancel Loop"
                    aria-label="Cancel Loop"
                    disabled={runtimeBusy}
                    className="grid size-6 place-items-center rounded text-danger-primary hover:bg-danger-subtle"
                    onClick={() => void cancelLoop()}
                  >
                    <Square className="size-3.5" />
                  </button>
                )}
              </div>
            </div>
            {(latestStage?.assigned_agent_id || latestAttempt) && (
              <div className="mt-0.5 truncate text-tertiary">
                {latestStage?.assigned_agent_id || "Unassigned"}
                {latestAttempt
                  ? ` / ${latestAttempt.model_provider || latestAttempt.provider}:${latestAttempt.model} / ${latestAttempt.cost ?? "Cost unknown"}`
                  : ""}
              </div>
            )}
            {latestAttempt?.provider_state && (
              <div className="mt-0.5 truncate text-tertiary">
                {latestAttempt.provider_state}
                {latestAttempt.evidence?.length ? ` / ${latestAttempt.evidence.length} evidence` : ""}
              </div>
            )}
            {latestAttempt?.failure_message && (
              <div className="mt-0.5 line-clamp-2 text-danger-primary">{latestAttempt.failure_message}</div>
            )}
            {latestStage?.status === "waiting_for_assignee" && eligibleAgents.length > 0 && (
              <div className="mt-1 flex min-w-0 items-center gap-1">
                <select
                  aria-label="Eligible Agent"
                  className="h-7 min-w-0 grow rounded-sm border border-subtle bg-surface-1 px-1 text-11 text-primary outline-none"
                  value={selectedAgentId}
                  disabled={runtimeBusy}
                  onChange={(event) => setSelectedAgentId(event.target.value)}
                >
                  <option value="">Unassigned</option>
                  {eligibleAgents.map((agent) => (
                    <option key={agent.agent_id} value={agent.agent_id}>
                      {agent.display_name} ({agent.agent_id})
                    </option>
                  ))}
                </select>
                <button
                  type="button"
                  title="Assign Stage"
                  aria-label="Assign Stage"
                  disabled={runtimeBusy || !selectedAgentId}
                  className="grid size-7 shrink-0 place-items-center rounded hover:bg-surface-2 disabled:text-placeholder"
                  onClick={() => void assignStage()}
                >
                  <UserCheck className="size-3.5" />
                </button>
              </div>
            )}
            {latestHandoff && (
              <div className="mt-0.5 truncate text-tertiary">
                {latestHandoff.from_agent_id || "Mesh"} to {latestHandoff.target_agent_id || "Unassigned"} /{" "}
                {latestHandoff.status}
              </div>
            )}
            {canStart && publishedLoops.length > 1 && (
              <select
                aria-label="Loop definition"
                value={selectedLoopId || publishedLoops[0].id}
                onChange={(event) => setSelectedLoopId(event.target.value)}
                className="mt-1 h-7 w-full min-w-0 rounded-sm border border-subtle bg-surface-1 px-1 text-primary"
              >
                {publishedLoops.map((loop) => (
                  <option key={loop.id} value={loop.id}>
                    {loop.name} v{loop.version}
                  </option>
                ))}
              </select>
            )}
            {!!runtime?.stages?.length && (
              <details className="mt-2 min-w-0 border-t border-subtle pt-2">
                <summary className="cursor-pointer text-secondary">Execution details</summary>
                <div className="mt-2 max-h-96 min-w-0 space-y-3 overflow-x-hidden overflow-y-auto [overflow-wrap:anywhere]">
                  {runtime.stages.map((stage) => (
                    <section key={stage.id} aria-label={`${stage.node_id} execution`}>
                      <div className="font-medium">
                        {stage.node_id} / {stage.status}
                      </div>
                      {stage.attempts.map((attempt, index) => (
                        <div key={attempt.id} className="mt-2 space-y-1 border-l border-subtle pl-2">
                          <div>
                            {index + 1}. {attempt.agent_id} / {attempt.status}
                          </div>
                          <div className="text-tertiary">
                            {attempt.model_provider || attempt.provider} / {attempt.model}
                          </div>
                          <div className="text-tertiary">
                            Tokens: {attempt.input_tokens} in / {attempt.output_tokens} out
                          </div>
                          <div className="text-tertiary">Cost: {attempt.cost ?? "Unknown"}</div>
                          {attempt.failure_message && <p className="text-danger-primary">{attempt.failure_message}</p>}
                          {attempt.evidence.map((entry) => (
                            <details key={entry.key} className="pt-1">
                              <summary className="cursor-pointer">{entry.title}</summary>
                              {entry.summary && <p className="mt-1 whitespace-pre-wrap">{entry.summary}</p>}
                              {entry.uri && <div className="font-mono mt-1 text-tertiary">{entry.uri}</div>}
                              {entry.metadata != null && (
                                <pre className="mt-1 whitespace-pre-wrap text-tertiary">
                                  {JSON.stringify(entry.metadata, null, 2)}
                                </pre>
                              )}
                            </details>
                          ))}
                        </div>
                      ))}
                    </section>
                  ))}
                </div>
              </details>
            )}
          </div>
        </SidebarPropertyListItem>
      )}
    </>
  );
});
