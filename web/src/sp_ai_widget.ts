import assert from "minimalistic-assert";
import * as z from "zod/mini";

import render_widgets_sp_ai_widget from "../templates/widgets/sp_ai_widget.hbs";

import * as blueslip from "./blueslip.ts";
import type {Message} from "./message_store.ts";
import {
    sp_ai_widget_inbound_event_schema,
    sp_ai_subagent_group_block_schema,
    type SpAiWidgetExtraData,
    type SpAiWidgetInboundEvent,
} from "./sp_ai_data.ts";
import type {Event} from "./widget_data.ts";
import type {AnyWidgetData} from "./widget_schema.ts";

const widget_state_schema = z.object({
    version: z.int().check(z.nonnegative()),
    display: z.enum(["card_only", "card_with_caption"]),
    title: z.string(),
    caption: z.string(),
    status: z.enum(["running", "ok", "error"]),
    tool: z.string(),
    input: z.string(),
    output: z.string(),
});

type WidgetBaseState = z.infer<typeof widget_state_schema>;

type SubagentStatusClass = "running" | "ok" | "error" | "unknown";

type SubagentTemplate = {
    type: string;
    description: string;
    status: SubagentStatusClass;
    status_label: string;
    meta_line: string;
    agentURL: string;
    error: string;
};

type SubagentGroupTemplate = {
    title: string;
    agents: SubagentTemplate[];
    fallback_text: string;
};

type WidgetState = WidgetBaseState & {
    turn_kind: string;
    turn_kind_label: string;
    has_subagent_groups: boolean;
    subagent_section_title: string;
    subagent_groups: SubagentGroupTemplate[];
};

function format_duration_ms(duration_ms: number): string {
    if (!Number.isFinite(duration_ms) || duration_ms < 0) {
        return "";
    }

    if (duration_ms < 1000) {
        return `${Math.round(duration_ms)}ms`;
    }

    const seconds = duration_ms / 1000;
    if (seconds < 60) {
        return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
    }

    const minutes = Math.floor(seconds / 60);
    const rem_seconds = Math.round(seconds % 60);
    return `${minutes}m ${rem_seconds}s`;
}

function format_tokens(tokens: number): string {
    if (!Number.isFinite(tokens) || tokens < 0) {
        return "";
    }

    if (tokens >= 1000000) {
        return `${(tokens / 1000000).toFixed(1)}M tok`;
    }
    if (tokens >= 1000) {
        return `${(tokens / 1000).toFixed(1)}k tok`;
    }
    return `${Math.round(tokens)} tok`;
}

function normalize_subagent_status(raw_status: string): {status: SubagentStatusClass; label: string} {
    const raw = raw_status.trim();
    const s = raw.toLowerCase();

    if (["ok", "success", "completed", "complete"].includes(s)) {
        return {status: "ok", label: "OK"};
    }
    if (["error", "failed", "failure"].includes(s)) {
        return {status: "error", label: "Error"};
    }
    if (["running", "in_progress", "in-progress", "started"].includes(s)) {
        return {status: "running", label: "Running"};
    }

    return {status: "unknown", label: raw === "" ? "Unknown" : raw};
}

function default_title_for_turn_kind(kind: string): string {
    if (kind === "subagent_group") {
        return "Subagents";
    }
    if (kind === "tool") {
        return "Tool";
    }
    if (kind === "assistant") {
        return "Assistant";
    }
    return "Agent output";
}

function normalize_subagent_groups(extra_data: SpAiWidgetExtraData): {
    turn_kind: string;
    turn_kind_label: string;
    subagent_section_title: string;
    subagent_groups: SubagentGroupTemplate[];
} {
    const turn_kind = extra_data.turn?.kind ?? "";
    const turn_kind_label = turn_kind === "" ? "" : default_title_for_turn_kind(turn_kind);
    const blocks = extra_data.turn?.blocks ?? [];

    const subagent_groups: SubagentGroupTemplate[] = [];
    for (const block of blocks) {
        const parsed = sp_ai_subagent_group_block_schema.safeParse(block);
        if (!parsed.success) {
            continue;
        }

        const agents = parsed.data.agents ?? [];
        const fallback_text = parsed.data.text ?? "";

        // Back-compat: if the server isn't sending structured agent data yet, do
        // not show the rich UI; let existing plain output rendering take over.
        if (agents.length === 0 && fallback_text === "") {
            continue;
        }

        const normalized_agents: SubagentTemplate[] = agents.map((agent) => {
            const {status, label} = normalize_subagent_status(agent.status ?? "");

            const type = agent.type?.trim() ?? "";

            const meta_parts: string[] = [];
            if (agent.toolCount !== undefined) {
                meta_parts.push(`${agent.toolCount} tool${agent.toolCount === 1 ? "" : "s"}`);
            }
            if (agent.totalTokens !== undefined) {
                const tok = format_tokens(agent.totalTokens);
                if (tok !== "") {
                    meta_parts.push(tok);
                }
            }
            if (agent.durationMs !== undefined) {
                const dur = format_duration_ms(agent.durationMs);
                if (dur !== "") {
                    meta_parts.push(dur);
                }
            }
            if (agent.model !== undefined && agent.model.trim() !== "") {
                meta_parts.push(agent.model.trim());
            }

            return {
                type: type !== "" ? type : "subagent",
                description: agent.description ?? "",
                status,
                status_label: label,
                meta_line: meta_parts.join(" · "),
                agentURL: agent.agentURL ?? "",
                error: agent.error ?? "",
            };
        });

        subagent_groups.push({
            title: parsed.data.title ?? "",
            agents: normalized_agents,
            fallback_text,
        });
    }

    const subagent_section_title =
        subagent_groups.length === 1 && subagent_groups[0]?.title.trim() !== ""
            ? subagent_groups[0].title
            : "Subagents";

    return {
        turn_kind,
        turn_kind_label,
        subagent_section_title,
        subagent_groups,
    };
}

function normalize_extra_data(extra_data: SpAiWidgetExtraData): WidgetState {
    const turn_title = extra_data.turn?.title;
    const turn_kind = extra_data.turn?.kind;
    const default_title = turn_kind ? default_title_for_turn_kind(turn_kind) : "Agent output";

    const parsed = widget_state_schema.safeParse({
        version: extra_data.version ?? 1,
        display: extra_data.display ?? "card_only",
        title: extra_data.title ?? turn_title ?? default_title,
        caption: extra_data.caption ?? "",
        status: extra_data.status ?? "running",
        tool: extra_data.tool ?? "",
        input: extra_data.input ?? "",
        output: extra_data.output ?? "",
    });

    const {turn_kind: normalized_turn_kind, turn_kind_label, subagent_section_title, subagent_groups} =
        normalize_subagent_groups(extra_data);

    if (parsed.success) {
        return {
            ...parsed.data,
            turn_kind: normalized_turn_kind,
            turn_kind_label,
            has_subagent_groups: subagent_groups.length > 0,
            subagent_section_title,
            subagent_groups,
        };
    }

    blueslip.warn("sp_ai widget: invalid extra_data", {error: parsed.error});

    return {
        version: 1,
        display: "card_only",
        title: "Agent output",
        caption: "",
        status: "running",
        tool: "",
        input: "",
        output: "",
        turn_kind: normalized_turn_kind,
        turn_kind_label,
        has_subagent_groups: subagent_groups.length > 0,
        subagent_section_title,
        subagent_groups,
    };
}

function status_label(status: WidgetState["status"]): string {
    if (status === "ok") {
        return "OK";
    }
    if (status === "error") {
        return "Error";
    }
    return "Running";
}

async function copy_text(text: string): Promise<void> {
    try {
        await navigator.clipboard.writeText(text);
    } catch {
        // Best-effort fallback.
        const textarea = document.createElement("textarea");
        textarea.value = text;
        textarea.style.position = "fixed";
        textarea.style.left = "-9999px";
        document.body.appendChild(textarea);
        textarea.select();
        document.execCommand("copy");
        textarea.remove();
    }
}

export function activate(opts: {$elem: JQuery; any_data: AnyWidgetData; message: Message}): (
    events: Event[],
) => void {
    assert(opts.any_data.widget_type === "sp_ai");

    if (opts.any_data.extra_data === null) {
        blueslip.error("sp_ai widget: invalid extra_data");
        return (_events: Event[]): void => {
            /* noop */
        };
    }

    let state: WidgetState = normalize_extra_data(opts.any_data.extra_data);

    function render(): void {
        const html = render_widgets_sp_ai_widget({
            ...state,
            status_label: status_label(state.status),
            show_caption: state.display === "card_with_caption" && state.caption !== "",
            show_tool: state.tool !== "",
            has_input: state.input !== "",
            has_output: state.output !== "",
        });

        opts.$elem.html(html);

        opts.$elem.find("button.sp-ai-copy").on("click", (e) => {
            e.stopPropagation();

            const target = $(e.currentTarget).attr("data-copy") ?? "";
            const text = target === "input" ? state.input : target === "output" ? state.output : "";
            void copy_text(text);
        });
    }

    function apply_inbound_event(evt: SpAiWidgetInboundEvent): void {
        if (evt.type === "set_status") {
            state = {...state, status: evt.status};
            return;
        }

        if (evt.type === "set_output") {
            state = {...state, output: evt.output};
            return;
        }

        if (evt.type === "append_output") {
            const next_output = state.output === "" ? evt.chunk : state.output + evt.chunk;
            state = {...state, output: next_output};
            return;
        }

        const _never: never = evt;
        void _never;
    }

    const handle_events = function (events: Event[]): void {
        for (const event of events) {
            const parsed = sp_ai_widget_inbound_event_schema.safeParse(event.data);
            if (!parsed.success) {
                // Ignore unknown/invalid events; don't crash the message list.
                continue;
            }

            apply_inbound_event(parsed.data);
        }

        render();
    };

    render();

    return handle_events;
}
