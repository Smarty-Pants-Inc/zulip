import assert from "minimalistic-assert";
import * as z from "zod/mini";

import render_widgets_sp_ai_widget from "../templates/widgets/sp_ai_widget.hbs";

// Use webpack's asset pipeline so the WASM is available in both dev and prod.
import onigWasmUrl from "vscode-oniguruma/release/onig.wasm";

import $ from "jquery";

import * as blueslip from "./blueslip.ts";
import type {Message} from "./message_store.ts";
import * as markdown from "./markdown.ts";
import {update_elements as update_rendered_markdown_elements} from "./rendered_markdown.ts";
import {
    sp_ai_widget_inbound_event_schema,
    sp_ai_subagent_group_block_schema,
    sp_ai_background_tasks_block_schema,
    sp_ai_plan_v2_block_schema,
    sp_ai_todo_v2_block_schema,
    type SpAiWidgetExtraData,
    type SpAiWidgetInboundEvent,
} from "./sp_ai_data.ts";
import type {Event} from "./widget_data.ts";
import type {AnyWidgetData} from "./widget_schema.ts";

const widget_state_schema = z.object({
    version: z.int().check(z.nonnegative()),
    display: z.enum(["card_only", "card_with_caption"]),
    kind: z.enum([
        "thinking",
        "tool",
        "final",
        "error",
        "plan",
        "decision",
        "ask",
        "budget",
        "policy",
        "subagent_group",
    ]),
    title: z.string(),
    caption: z.string(),
    status: z.enum(["pending", "running", "ok", "error", "aborted", "denied", "approval_requested", "approval_responded"]),
    tool: z.string(),
    input: z.string(),
    output: z.string(),
    blocks: z.array(
        z.object({
            index: z.number(),
            kind: z.string(),
            label: z.string(),
            title: z.string(),
            text: z.string(),
            language: z.string(),
            channel: z.enum(["stdout", "stderr", ""]),
            columns: z.array(z.string()),
            rows: z.array(z.array(z.string())),
            has_columns: z.boolean(),
            copy_text: z.string(),
            is_table: z.boolean(),
            is_stream: z.boolean(),
            is_unknown: z.boolean(),
            is_context_usage: z.boolean(),
            has_context_usage_percent: z.boolean(),
            context_usage_percent: z.number(),
            has_list_items: z.boolean(),
            list_items: z.array(z.string()),
            is_file_tree: z.boolean(),
        }),
    ),
    parallel: z.optional(
        z.object({
            groupId: z.string(),
            index: z.number(),
            count: z.number(),
            hasPrev: z.boolean(),
            hasNext: z.boolean(),
        }),
    ),
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

type BackgroundTaskStatusClass = "pending" | "running" | "ok" | "error" | "aborted" | "unknown";

type BackgroundTaskTemplate = {
    task_index: number;
    description: string;
    command: string;
    status: BackgroundTaskStatusClass;
    status_label: string;
    meta_line: string;
    has_output_preview: boolean;
    output_preview: string;
    error: string;
};

type BackgroundTasksGroupTemplate = {
    group_index: number;
    title: string;
    tasks: BackgroundTaskTemplate[];
};

type PlanStepStatusClass = "pending" | "running" | "ok" | "error" | "unknown";

type PlanStepTemplate = {
    step_index: number;
    step: string;
    status: PlanStepStatusClass;
    status_label: string;
};

type PlanGroupTemplate = {
    group_index: number;
    title: string;
    steps: PlanStepTemplate[];
};

type TodoItemTemplate = {
    item_index: number;
    text: string;
    checked: boolean;
};

type TodoGroupTemplate = {
    group_index: number;
    title: string;
    items: TodoItemTemplate[];
};

type WidgetState = WidgetBaseState & {
    turn_kind: string;
    turn_kind_label: string;
    has_subagent_groups: boolean;
    subagent_section_title: string;
    subagent_groups: SubagentGroupTemplate[];
    has_background_tasks: boolean;
    background_tasks_section_title: string;
    background_task_groups: BackgroundTasksGroupTemplate[];
    has_plan_blocks: boolean;
    plan_section_title: string;
    plan_groups: PlanGroupTemplate[];
    has_todo_blocks: boolean;
    todo_section_title: string;
    todo_groups: TodoGroupTemplate[];
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

function normalize_plan_step_status(raw_status: string): {status: PlanStepStatusClass; label: string} {
    const raw = raw_status.trim();
    const s = raw.toLowerCase();

    if (["ok", "success", "completed", "complete", "done"].includes(s)) {
        return {status: "ok", label: "Done"};
    }
    if (["error", "failed", "failure"].includes(s)) {
        return {status: "error", label: "Error"};
    }
    if (["running", "in_progress", "in-progress", "started"].includes(s)) {
        return {status: "running", label: "In progress"};
    }
    if (["pending", "queued", "queue", "todo"].includes(s)) {
        return {status: "pending", label: "Todo"};
    }

    return {status: "unknown", label: raw === "" ? "" : raw};
}

function normalize_plan_blocks(extra_data: SpAiWidgetExtraData): {
    plan_section_title: string;
    plan_groups: PlanGroupTemplate[];
    skip_block_indexes: Set<number>;
} {
    const blocks = Array.isArray(extra_data.turn?.blocks) ? extra_data.turn?.blocks : [];
    const skip_block_indexes = new Set<number>();
    const plan_groups: PlanGroupTemplate[] = [];

    let group_index = 0;
    for (const [index, block] of blocks.entries()) {
        // v2 structured block
        const parsed = sp_ai_plan_v2_block_schema.safeParse(block);
        if (parsed.success) {
            const raw_steps = parsed.data.steps ?? [];
            if (!Array.isArray(raw_steps) || raw_steps.length === 0) {
                continue;
            }

            const steps: PlanStepTemplate[] = raw_steps
                .map((row, step_index) => {
                    const step = String(row.step ?? "").trim();
                    const {status, label} = normalize_plan_step_status(String(row.status ?? ""));
                    if (step === "") {
                        return null;
                    }
                    return {
                        step_index,
                        step,
                        status,
                        status_label: label,
                    };
                })
                .filter(Boolean) as PlanStepTemplate[];

            if (steps.length === 0) {
                continue;
            }

            plan_groups.push({
                group_index,
                title: parsed.data.title ?? "",
                steps,
            });
            skip_block_indexes.add(index);
            group_index += 1;
            continue;
        }

        // Legacy plan list: {kind:"plan", items:["..."]}
        const rec: any = block && typeof block === "object" ? block : {};
        if (String(rec.kind || "") !== "plan") {
            continue;
        }
        const raw_items: unknown[] = Array.isArray(rec.items) ? rec.items : [];
        const items = raw_items.map((x) => (typeof x === "string" ? x.trim() : "")).filter((s) => s !== "");
        if (items.length === 0) {
            continue;
        }
        const steps: PlanStepTemplate[] = items.map((step, step_index) => ({
            step_index,
            step,
            status: "unknown",
            status_label: "",
        }));
        plan_groups.push({
            group_index,
            title: typeof rec.title === "string" ? rec.title : "",
            steps,
        });
        skip_block_indexes.add(index);
        group_index += 1;
    }

    const plan_section_title =
        plan_groups.length === 1 && plan_groups[0]?.title.trim() !== "" ? plan_groups[0].title : "Plan";

    return {plan_section_title, plan_groups, skip_block_indexes};
}

function normalize_todo_blocks(extra_data: SpAiWidgetExtraData): {
    todo_section_title: string;
    todo_groups: TodoGroupTemplate[];
    skip_block_indexes: Set<number>;
} {
    const blocks = Array.isArray(extra_data.turn?.blocks) ? extra_data.turn?.blocks : [];
    const skip_block_indexes = new Set<number>();
    const todo_groups: TodoGroupTemplate[] = [];

    let group_index = 0;
    for (const [index, block] of blocks.entries()) {
        // v2 structured block
        const parsed = sp_ai_todo_v2_block_schema.safeParse(block);
        if (parsed.success) {
            const raw_items = parsed.data.items ?? [];
            if (!Array.isArray(raw_items) || raw_items.length === 0) {
                continue;
            }
            const items: TodoItemTemplate[] = raw_items
                .map((row, item_index) => {
                    const text = String(row.text ?? "").trim();
                    if (text === "") {
                        return null;
                    }
                    return {item_index, text, checked: row.checked === true};
                })
                .filter(Boolean) as TodoItemTemplate[];
            if (items.length === 0) {
                continue;
            }
            todo_groups.push({
                group_index,
                title: parsed.data.title ?? "",
                items,
            });
            skip_block_indexes.add(index);
            group_index += 1;
            continue;
        }

        // Legacy todo list: {kind:"todo", items:["[x] ...", "[ ] ..."]}
        const rec: any = block && typeof block === "object" ? block : {};
        if (String(rec.kind || "") !== "todo") {
            continue;
        }
        const raw_items: unknown[] = Array.isArray(rec.items) ? rec.items : [];
        const items: TodoItemTemplate[] = [];
        for (const [item_index, raw] of raw_items.entries()) {
            if (typeof raw !== "string") {
                continue;
            }
            const s = raw.trim();
            const m = /^\s*\[( |x|X)\]\s+([\s\S]*)$/.exec(s);
            if (!m) {
                continue;
            }
            const text = String(m[2] || "").trim();
            if (text === "") {
                continue;
            }
            items.push({item_index, text, checked: m[1].toLowerCase() === "x"});
        }
        if (items.length === 0) {
            continue;
        }
        todo_groups.push({
            group_index,
            title: typeof rec.title === "string" ? rec.title : "",
            items,
        });
        skip_block_indexes.add(index);
        group_index += 1;
    }

    const todo_section_title =
        todo_groups.length === 1 && todo_groups[0]?.title.trim() !== "" ? todo_groups[0].title : "Todo";

    return {todo_section_title, todo_groups, skip_block_indexes};
}

function format_runtime_sec(runtime_sec: number): string {
    if (!Number.isFinite(runtime_sec) || runtime_sec < 0) {
        return "";
    }

    if (runtime_sec < 60) {
        return `${runtime_sec.toFixed(runtime_sec < 10 ? 1 : 0)}s`;
    }

    const minutes = Math.floor(runtime_sec / 60);
    const rem_seconds = Math.round(runtime_sec % 60);
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

function normalize_background_task_status(raw_status: string): {status: BackgroundTaskStatusClass; label: string} {
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
    if (["pending", "queued", "queue"].includes(s)) {
        return {status: "pending", label: "Pending"};
    }
    if (["aborted", "canceled", "cancelled"].includes(s)) {
        return {status: "aborted", label: "Aborted"};
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
    skip_block_indexes: Set<number>;
} {
    const turn_kind = extra_data.turn?.kind ?? "";
    const turn_kind_label = turn_kind === "" ? "" : default_title_for_turn_kind(turn_kind);
    const blocks = Array.isArray(extra_data.turn?.blocks) ? extra_data.turn?.blocks : [];

    const skip_block_indexes = new Set<number>();
    const subagent_groups: SubagentGroupTemplate[] = [];
    for (const [index, block] of blocks.entries()) {
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
        skip_block_indexes.add(index);
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
        skip_block_indexes,
    };
}

function normalize_background_tasks(extra_data: SpAiWidgetExtraData): {
    background_tasks_section_title: string;
    background_task_groups: BackgroundTasksGroupTemplate[];
    skip_block_indexes: Set<number>;
} {
    const blocks = Array.isArray(extra_data.turn?.blocks) ? extra_data.turn?.blocks : [];

    const skip_block_indexes = new Set<number>();
    const background_task_groups: BackgroundTasksGroupTemplate[] = [];

    let group_index = 0;
    for (const [index, block] of blocks.entries()) {
        const parsed = sp_ai_background_tasks_block_schema.safeParse(block);
        if (!parsed.success) {
            continue;
        }

        const raw_tasks = parsed.data.tasks ?? [];
        if (!Array.isArray(raw_tasks) || raw_tasks.length === 0) {
            // Back-compat: no structured tasks yet; let generic block rendering handle it.
            continue;
        }

        const tasks: BackgroundTaskTemplate[] = raw_tasks
            .map((task, task_index) => {
                const rec: any = task && typeof task === "object" ? task : {};

                const command = typeof rec.command === "string" ? rec.command : "";
                const description = typeof rec.description === "string" ? rec.description : "";
                const error = typeof rec.error === "string" ? rec.error : "";
                const output_preview = typeof rec.outputPreview === "string" ? rec.outputPreview : "";
                const has_output_preview = output_preview !== "";

                const {status, label} = normalize_background_task_status(typeof rec.status === "string" ? rec.status : "");

                const meta_parts: string[] = [];
                if (typeof rec.runtimeSec === "number") {
                    const rt = format_runtime_sec(rec.runtimeSec);
                    if (rt !== "") {
                        meta_parts.push(rt);
                    }
                }
                if (typeof rec.exitCode === "number" && Number.isFinite(rec.exitCode)) {
                    meta_parts.push(`exit ${rec.exitCode}`);
                }

                return {
                    task_index,
                    description,
                    command,
                    status,
                    status_label: label,
                    meta_line: meta_parts.join(" · "),
                    has_output_preview,
                    output_preview,
                    error,
                };
            })
            .filter((task) => task.command !== "" || task.description !== "" || task.has_output_preview || task.error !== "");

        if (tasks.length === 0) {
            continue;
        }

        background_task_groups.push({
            group_index,
            title: parsed.data.title ?? "",
            tasks,
        });
        skip_block_indexes.add(index);
        group_index += 1;
    }

    const background_tasks_section_title =
        background_task_groups.length === 1 && background_task_groups[0]?.title.trim() !== ""
            ? background_task_groups[0].title
            : "Background tasks";

    return {
        background_tasks_section_title,
        background_task_groups,
        skip_block_indexes,
    };
}

type WidgetAction = "abort" | "retry" | "approve" | "deny";

function stringify_unknown(value: unknown): string {
    if (typeof value === "string") {
        return value;
    }

    try {
        return JSON.stringify(value, null, 2);
    } catch {
        return String(value);
    }
}

function try_pretty_json(text: string): string | undefined {
    const trimmed = String(text || "").trim();
    if (!trimmed) return undefined;

    const looks_like_json =
        (trimmed.startsWith("{") && trimmed.endsWith("}")) ||
        (trimmed.startsWith("[") && trimmed.endsWith("]"));
    if (!looks_like_json) return undefined;

    try {
        const parsed = JSON.parse(trimmed);
        return JSON.stringify(parsed, null, 2);
    } catch {
        return undefined;
    }
}

//  Syntax highlighting (GitHub-style) via `@wooorm/starry-night`.
// Lazy-loaded so the initial Zulip bundle isn't penalized.
let starry_night_promise: Promise<any> | undefined;

async function get_starry_night(): Promise<any> {
    if (starry_night_promise) {
        return starry_night_promise;
    }

    starry_night_promise = (async () => {
        const [{common, createStarryNight}, {toDom}] = await Promise.all([
            import("@wooorm/starry-night"),
            import("hast-util-to-dom"),
        ]);

        const starryNight = await createStarryNight(common, {
            getOnigurumaUrlFetch() {
                // Stable URL in both dev and production.
                return new URL(onigWasmUrl, window.location.href);
            },
        });

        return {starryNight, toDom};
    })();

    return starry_night_promise;
}

async function highlight_code_in_element(elem: HTMLElement, language: string): Promise<void> {
    const lang = String(language || "").trim();
    if (!lang) return;

    const text = elem.textContent ?? "";
    if (!text) return;

    const {starryNight, toDom} = await get_starry_night();
    const scope = starryNight.flagToScope(lang.toLowerCase());
    if (!scope) return;

    const tree = starryNight.highlight(text, scope);
    const frag = toDom(tree, {fragment: true});
    elem.replaceChildren(frag);
    elem.setAttribute("data-highlight", "starry-night");
}

async function apply_sp_ai_highlighting(root: HTMLElement): Promise<void> {
    // If highlighting fails once (e.g. WASM can't load), don't keep retrying.
    if (root.getAttribute("data-highlight-error") === "1") {
        return;
    }

    try {
    const code_nodes = Array.from(root.querySelectorAll<HTMLElement>(
        ".sp-ai-tool-code[data-language], .sp-ai-pre[data-language]",
    ));

    for (const node of code_nodes) {
        if (node.getAttribute("data-highlight") === "starry-night") continue;
        const lang = node.getAttribute("data-language") ?? "";
        await highlight_code_in_element(node, lang);
    }

    // Highlight code blocks inside rendered markdown.
    const md_code_nodes = Array.from(root.querySelectorAll<HTMLElement>(
        ".sp-ai-markdown.rendered_markdown div.codehilite code",
    ));

    for (const node of md_code_nodes) {
        if (node.getAttribute("data-highlight") === "starry-night") continue;
        const container = node.closest<HTMLElement>("div.codehilite");
        const pretty = (container?.getAttribute("data-code-language") ?? "").trim();
        const flag = pretty ? pretty.toLowerCase().replaceAll(" ", "") : "";
        await highlight_code_in_element(node, flag);
    }
    } catch (error) {
        root.setAttribute("data-highlight-error", "1");
        blueslip.warn("sp_ai highlighting failed; disabling for this widget", {error});
    }
}

function kind_label(kind: WidgetState["kind"]): string {
    if (kind === "thinking") return "Thinking";
    if (kind === "tool") return "Tool";
    if (kind === "final") return "Final";
    if (kind === "error") return "Error";
    if (kind === "plan") return "Plan";
    if (kind === "decision") return "Decision";
    if (kind === "ask") return "Ask";
    if (kind === "budget") return "Budget";
    if (kind === "subagent_group") return "Subagents";
    return "Policy";
}

function block_label(kind: string, channel: "stdout" | "stderr" | ""): string {
    if (kind === "markdown") return "Markdown";
    if (kind === "text") return "Text";
    if (kind === "code") return "Code";
    if (kind === "diff") return "Diff";
    if (kind === "json") return "JSON";
    if (kind === "table") return "Table";
    if (kind === "context_usage") return "Context usage";
    if (kind === "file_tree") return "File tree";
    if (kind === "memory_blocks") return "Memory blocks";
    if (kind === "subagent_group") return "Subagent group";
    if (kind === "background_tasks" || kind === "bash_tasks") return "Background tasks";
    if (kind === "queue") return "Queue";
    if (kind === "plan") return "Plan";
    if (kind === "todo") return "Todo";
    if (kind === "stream" && channel === "stderr") return "Stderr";
    if (kind === "stream") return "Stdout";
    return "Block";
}

function normalize_turn_blocks(
    raw_blocks: unknown,
    opts?: {skip_block_indexes?: Set<number>},
): WidgetState["blocks"] {
    if (!Array.isArray(raw_blocks)) {
        return [];
    }

    const blocks: WidgetState["blocks"] = [];

    for (const [index, raw] of raw_blocks.entries()) {
        if (opts?.skip_block_indexes?.has(index)) {
            continue;
        }

        if (!raw || typeof raw !== "object") {
            continue;
        }

        const block = raw as Record<string, unknown>;
        const kind = typeof block["kind"] === "string" ? block["kind"] : "unknown";
        const title = typeof block["title"] === "string" ? block["title"] : "";

        const push_generic_block = (data: {
            text: string;
            language?: string;
            channel?: "stdout" | "stderr" | "";
            is_unknown?: boolean;
            list_items?: string[];
            has_context_usage_percent?: boolean;
            context_usage_percent?: number;
        }): void => {
            const language = data.language ?? "";
            const channel = data.channel ?? "";
            const list_items = data.list_items ?? [];
            const has_context_usage_percent = data.has_context_usage_percent ?? false;
            const context_usage_percent = data.context_usage_percent ?? 0;

            blocks.push({
                index,
                kind,
                label: block_label(kind, channel),
                title,
                text: data.text,
                language,
                channel,
                columns: [],
                rows: [],
                has_columns: false,
                copy_text: data.text,
                is_table: false,
                is_stream: kind === "stream" && channel !== "",
                is_unknown: data.is_unknown ?? false,
                is_context_usage: kind === "context_usage",
                has_context_usage_percent,
                context_usage_percent,
                has_list_items: list_items.length > 0,
                list_items,
                is_file_tree: kind === "file_tree",
            });
        };

        if (kind === "table") {
            const columns = Array.isArray(block["columns"])
                ? block["columns"].map((value) => (typeof value === "string" ? value : stringify_unknown(value)))
                : [];
            const rows = Array.isArray(block["rows"])
                ? block["rows"].map((row) =>
                      Array.isArray(row)
                          ? row.map((value) => (typeof value === "string" ? value : stringify_unknown(value)))
                          : [stringify_unknown(row)],
                  )
                : [];

            blocks.push({
                index,
                kind,
                label: block_label(kind, ""),
                title,
                text: "",
                language: "",
                channel: "",
                columns,
                rows,
                has_columns: columns.length > 0,
                copy_text: stringify_unknown({columns, rows}),
                is_table: true,
                is_stream: false,
                is_unknown: false,
                is_context_usage: false,
                has_context_usage_percent: false,
                context_usage_percent: 0,
                has_list_items: false,
                list_items: [],
                is_file_tree: false,
            });
            continue;
        }

        const channel =
            kind === "stream" && (block["channel"] === "stdout" || block["channel"] === "stderr")
                ? block["channel"]
                : "";

        let text = "";
        let language = "";
        let is_unknown = false;
        let list_items: string[] = [];
        let has_context_usage_percent = false;
        let context_usage_percent = 0;

        if (kind === "markdown" || kind === "text" || (kind === "stream" && channel !== "")) {
            text = typeof block["text"] === "string" ? block["text"] : stringify_unknown(block["text"]);
        } else if (kind === "code") {
            text = typeof block["code"] === "string" ? block["code"] : stringify_unknown(block["code"]);
            language = typeof block["language"] === "string" ? block["language"] : "";

            // Make JSON blocks look much closer to AI Elements (pretty-printed).
            if (language === "json") {
                const pretty = try_pretty_json(text);
                if (pretty !== undefined) {
                    text = pretty;
                }
            }
        } else if (kind === "diff") {
            text = typeof block["diff"] === "string" ? block["diff"] : stringify_unknown(block["diff"]);
            language = typeof block["language"] === "string" ? block["language"] : "diff";
        } else if (kind === "json") {
            if (typeof block["text"] === "string") {
                text = block["text"];
                const pretty = try_pretty_json(text);
                if (pretty !== undefined) {
                    text = pretty;
                }
            } else if (Object.hasOwn(block, "json")) {
                text = stringify_unknown(block["json"]);
            } else {
                text = stringify_unknown(block);
            }
            language = "json";
        } else if (kind === "context_usage") {
            const used = block["used"];
            const limit = block["limit"];
            const unit = typeof block["unit"] === "string" ? block["unit"] : "tokens";
            const breakdown = Array.isArray(block["breakdown"]) ? block["breakdown"] : [];

            if (
                typeof used === "number" &&
                typeof limit === "number" &&
                Number.isFinite(used) &&
                Number.isFinite(limit) &&
                limit > 0
            ) {
                has_context_usage_percent = true;
                context_usage_percent = Math.max(0, Math.min(100, (used / limit) * 100));
                text = `${used}/${limit} ${unit}`;
            } else if (typeof block["text"] === "string") {
                text = block["text"];
            } else {
                text = stringify_unknown(block);
            }

            list_items = breakdown.map((item) => stringify_unknown(item));
        } else if (kind === "file_tree") {
            if (Array.isArray(block["entries"])) {
                list_items = block["entries"].map((item) => stringify_unknown(item));
                text = list_items.join("\n");
            } else if (typeof block["tree"] === "string") {
                text = block["tree"];
            } else {
                text = stringify_unknown(block["entries"] ?? block["tree"] ?? block);
            }
        } else if (kind === "memory_blocks") {
            if (Array.isArray(block["blocks"])) {
                // Try to render an intentionally compact summary list.
                list_items = block["blocks"].map((item) => {
                    if (!item || typeof item !== "object") return stringify_unknown(item);
                    const rec = item as Record<string, unknown>;
                    const label = typeof rec["label"] === "string" ? rec["label"] : "(block)";
                    const chars = typeof rec["chars"] === "number" ? ` (${rec["chars"]} chars)` : "";
                    return `${label}${chars}`;
                });
                text = list_items.join("\n");
            } else if (typeof block["text"] === "string") {
                text = block["text"];
            } else {
                text = stringify_unknown(block["blocks"] ?? block);
            }
        } else if (kind === "subagent_group" || kind === "queue" || kind === "plan" || kind === "todo") {
            const raw_items = Array.isArray(block["items"]) ? block["items"] : Array.isArray(block["rows"]) ? block["rows"] : undefined;
            if (raw_items !== undefined) {
                list_items = raw_items.map((item) => stringify_unknown(item));
                text = list_items.join("\n");
            } else if (typeof block["text"] === "string") {
                text = block["text"];
            } else {
                text = stringify_unknown(block);
            }
        } else {
            text = stringify_unknown(block);
            is_unknown = true;
        }

        push_generic_block({
            text,
            language,
            channel,
            is_unknown,
            list_items,
            has_context_usage_percent,
            context_usage_percent,
        });
    }

    return blocks;
}

function normalize_extra_data(extra_data: SpAiWidgetExtraData): WidgetState {
    const ed: any = extra_data || {};

    // v2 turn payload, if present.
    const turn: any = ed.turn && typeof ed.turn === "object" ? ed.turn : null;

    const kindRaw = String(turn?.kind || "") as WidgetState["kind"];
    const statusRaw = String(turn?.status || ed.status || "running");

    const {
        turn_kind: normalized_turn_kind,
        turn_kind_label,
        subagent_section_title,
        subagent_groups,
        skip_block_indexes: subagent_skip_block_indexes,
    } = normalize_subagent_groups(extra_data);

    const {
        background_tasks_section_title,
        background_task_groups,
        skip_block_indexes: background_skip_block_indexes,
    } = normalize_background_tasks(extra_data);

    const {
        plan_section_title,
        plan_groups,
        skip_block_indexes: plan_skip_block_indexes,
    } = normalize_plan_blocks(extra_data);

    const {
        todo_section_title,
        todo_groups,
        skip_block_indexes: todo_skip_block_indexes,
    } = normalize_todo_blocks(extra_data);

    const skip_block_indexes = new Set<number>([
        ...subagent_skip_block_indexes,
        ...background_skip_block_indexes,
        ...plan_skip_block_indexes,
        ...todo_skip_block_indexes,
    ]);

    const parsed = widget_state_schema.safeParse({
        version: ed.version ?? 2,
        display: ed.display ?? "card_only",

        kind:
            kindRaw === "thinking" ||
            kindRaw === "tool" ||
            kindRaw === "final" ||
            kindRaw === "error" ||
            kindRaw === "plan" ||
            kindRaw === "decision" ||
            kindRaw === "ask" ||
            kindRaw === "budget" ||
            kindRaw === "policy" ||
            kindRaw === "subagent_group"
                ? kindRaw
                : "final",

        title: turn?.title ?? ed.title ?? "Agent output",
        caption: turn?.subtitle ?? ed.caption ?? "",

        status:
            statusRaw === "pending" ||
            statusRaw === "ok" ||
            statusRaw === "error" ||
            statusRaw === "aborted" ||
            statusRaw === "denied" ||
            statusRaw === "approval_requested" ||
            statusRaw === "approval_responded" ||
            statusRaw === "running"
                ? statusRaw
                : "running",

        tool: turn?.tool?.name ?? ed.tool ?? "",
        input: turn?.tool?.argsText ?? ed.input ?? "",
        output: turn?.output ?? ed.output ?? "",
        blocks: normalize_turn_blocks(turn?.blocks, {skip_block_indexes}),

        parallel: (() => {
            const p: any = turn?.parallel;
            if (!p || typeof p !== "object") return undefined;

            const groupId = typeof p.groupId === "string" ? p.groupId : "";
            const index = typeof p.index === "number" ? p.index : 0;
            const count = typeof p.count === "number" ? p.count : Math.max(1, index + 1);
            const hasPrev = typeof p.hasPrev === "boolean" ? p.hasPrev : index > 0;
            const hasNext = typeof p.hasNext === "boolean" ? p.hasNext : index < count - 1;

            if (!groupId) return undefined;
            return {groupId, index, count, hasPrev, hasNext};
        })(),
    });


    if (parsed.success) {
        return {
            ...parsed.data,
            turn_kind: normalized_turn_kind,
            turn_kind_label,
            has_subagent_groups: subagent_groups.length > 0,
            subagent_section_title,
            subagent_groups,
            has_background_tasks: background_task_groups.length > 0,
            background_tasks_section_title,
            background_task_groups,
            has_plan_blocks: plan_groups.length > 0,
            plan_section_title,
            plan_groups,
            has_todo_blocks: todo_groups.length > 0,
            todo_section_title,
            todo_groups,
        };
    }

    blueslip.warn("sp_ai widget: invalid extra_data", {error: parsed.error});

    return {
        version: 2,
        display: "card_only",
        kind: "final",
        title: "Agent output",
        caption: "",
        status: "running",
        tool: "",
        input: "",
        output: "",
        blocks: [],
        parallel: undefined,
        turn_kind: normalized_turn_kind,
        turn_kind_label,
        has_subagent_groups: subagent_groups.length > 0,
        subagent_section_title,
        subagent_groups,
        has_background_tasks: background_task_groups.length > 0,
        background_tasks_section_title,
        background_task_groups,
        has_plan_blocks: plan_groups.length > 0,
        plan_section_title,
        plan_groups,
        has_todo_blocks: todo_groups.length > 0,
        todo_section_title,
        todo_groups,
    };
}

function status_label(status: WidgetState["status"]): string {
    if (status === "ok") return "OK";
    if (status === "pending") return "Pending";
    if (status === "approval_requested") return "Awaiting Approval";
    if (status === "approval_responded") return "Responded";
    if (status === "aborted") return "Aborted";
    if (status === "error") return "Error";
    if (status === "denied") return "Denied";
    return "Running";
}

function tool_status_label(status: WidgetState["status"]): string {
    // Mirror Vercel AI elements wording for tool cards.
    if (status === "ok") return "Completed";
    if (status === "pending") return "Pending";
    if (status === "approval_requested") return "Awaiting Approval";
    if (status === "approval_responded") return "Responded";
    if (status === "aborted") return "Aborted";
    if (status === "error") return "Error";
    if (status === "denied") return "Denied";
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

export function activate(opts: {
    $elem: JQuery;
    any_data: AnyWidgetData;
    message: Message;
    callback: (data: {type: "action"; action: WidgetAction}) => void;
}): (events: Event[]) => void {
    assert(opts.any_data.widget_type === "sp_ai");

    if (opts.any_data.extra_data === null) {
        blueslip.error("sp_ai widget: invalid extra_data");
        return (_events: Event[]): void => {
            /* noop */
        };
    }

    let state: WidgetState = normalize_extra_data(opts.any_data.extra_data);

    function render(): void {
        const blocks_for_template = state.blocks.map((b) => {
            if (b.kind === "markdown") {
                // Client-side markdown rendering, then enhance with rendered_markdown.ts
                // (spoilers, code block copy buttons, etc.).
                const rendered = markdown.render(b.text).content;
                return {
                    ...b,
                    is_markdown: true,
                    markdown_html: rendered,
                };
            }

            return {
                ...b,
                is_markdown: false,
                markdown_html: "",
            };
        });

        const html = render_widgets_sp_ai_widget({
            ...state,
            kind_label: kind_label(state.kind),
            status_label: status_label(state.status),
            show_status_pill: state.kind !== "error",
            tool_status_label: tool_status_label(state.status),
            is_thinking_kind: state.kind === "thinking",
            reasoning_label: state.status === "running" ? "Thinking\u2026" : "Thought for a few seconds",
            reasoning_text: state.output || state.caption || "",
            has_reasoning_text: (state.output || state.caption || "") !== "",
            // Auto-expand while streaming content; collapsed when done.
            reasoning_expanded: state.kind === "thinking" && state.status === "running" && (state.output || state.caption || "") !== "" ? "true" : "false",
            reasoning_content_hidden: state.kind === "thinking" && state.status === "running" && (state.output || state.caption || "") !== "" ? "false" : "true",
            is_tool_kind: state.kind === "tool",
            tool_title: state.tool || state.title,
            tool_open:
                state.kind === "tool" &&
                (state.status === "pending" ||
                    state.status === "running" ||
                    state.status === "approval_requested" ||
                    state.status === "error" ||
                    state.status === "denied"),
            show_tool_approval_actions: state.kind === "tool" && state.status === "approval_requested",
            show_caption: state.display === "card_with_caption" && state.caption !== "",
            show_tool: state.tool !== "",
            has_input:
                state.blocks.length === 0 &&
                !state.has_subagent_groups &&
                !state.has_background_tasks &&
                state.input !== "",
            has_output:
                state.blocks.length === 0 &&
                !state.has_subagent_groups &&
                !state.has_background_tasks &&
                state.output !== "",
            has_blocks: state.blocks.length > 0,
            blocks: blocks_for_template,
            show_abort: state.status === "running",
            show_retry: state.status !== "running",
            show_approve: state.kind === "ask",
            show_deny: state.kind === "ask",
            has_widget_actions: true,
            show_parallel: state.parallel !== undefined,
            parallel_first: state.parallel ? !state.parallel.hasPrev : false,
            parallel_last: state.parallel ? !state.parallel.hasNext : false,
        });

        opts.$elem.html(html);

        // Enhance rendered markdown blocks (spoilers, codeblock copy buttons, etc.).
        opts.$elem.find(".sp-ai-markdown.rendered_markdown").each(function () {
            update_rendered_markdown_elements($(this));
        });

        // Apply syntax highlighting to all code blocks inside this widget.
        void apply_sp_ai_highlighting(opts.$elem[0] as HTMLElement);

        // Reasoning trigger: toggle collapsible content.
        opts.$elem.find("button.sp-ai-reasoning-trigger").on("click", (e) => {
            e.stopPropagation();
            const trigger = e.currentTarget as HTMLElement;
            const expanded = trigger.getAttribute("aria-expanded") === "true";
            trigger.setAttribute("aria-expanded", String(!expanded));
            const content = trigger.closest(".sp-ai-reasoning")?.querySelector(".sp-ai-reasoning-content");
            if (content) {
                content.setAttribute("aria-hidden", String(expanded));
            }
        });

        opts.$elem.find("button[data-copy]").on("click", (e) => {
            e.preventDefault();
            e.stopPropagation();

            const target = $(e.currentTarget).attr("data-copy") ?? "";
            const text = target === "input" ? state.input : target === "output" ? state.output : "";
            void copy_text(text);
        });

        opts.$elem.find("button[data-copy-block]").on("click", (e) => {
            e.preventDefault();
            e.stopPropagation();
            const raw_index = $(e.currentTarget).attr("data-copy-block") ?? "";
            const index = Number.parseInt(raw_index, 10);
            if (!Number.isInteger(index)) {
                return;
            }

            const block = state.blocks.find((item) => item.index === index);
            if (!block) {
                return;
            }

            void copy_text(block.copy_text);
        });

        opts.$elem.find("button[data-copy-bg-task-command]").on("click", (e) => {
            e.preventDefault();
            e.stopPropagation();

            const raw = $(e.currentTarget).attr("data-copy-bg-task-command") ?? "";
            const [raw_group, raw_task] = raw.split(":", 2);
            const group_index = Number.parseInt(raw_group ?? "", 10);
            const task_index = Number.parseInt(raw_task ?? "", 10);
            if (!Number.isInteger(group_index) || !Number.isInteger(task_index)) {
                return;
            }

            const group = state.background_task_groups.find((g) => g.group_index === group_index);
            const task = group?.tasks.find((t) => t.task_index === task_index);
            if (!task) {
                return;
            }

            void copy_text(task.command);
        });

        opts.$elem.find("button[data-action]").on("click", (e) => {
            e.stopPropagation();
            const action = $(e.currentTarget).attr("data-action") ?? "";
            if (action === "abort" || action === "retry" || action === "approve" || action === "deny") {
                opts.callback({type: "action", action});
            }
        });
    }

    function apply_inbound_event(evt: SpAiWidgetInboundEvent): void {
        if (evt.type === "set_extra_data") {
            const extra: any = evt.extra_data;
            state = normalize_extra_data(extra);
            return;
        }

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
