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
    type SpAiWidgetExtraData,
    type SpAiWidgetInboundEvent,
} from "./sp_ai_data.ts";
import type {Event} from "./widget_data.ts";
import type {AnyWidgetData} from "./widget_schema.ts";

const widget_state_schema = z.object({
    version: z.int().check(z.nonnegative()),
    display: z.enum(["card_only", "card_with_caption"]),
    kind: z.enum(["thinking", "tool", "final", "error", "plan", "decision", "ask", "budget", "policy"]),
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

type WidgetState = z.infer<typeof widget_state_schema>;

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
    if (kind === "queue") return "Queue";
    if (kind === "plan") return "Plan";
    if (kind === "todo") return "Todo";
    if (kind === "stream" && channel === "stderr") return "Stderr";
    if (kind === "stream") return "Stdout";
    return "Block";
}

function normalize_turn_blocks(raw_blocks: unknown): WidgetState["blocks"] {
    if (!Array.isArray(raw_blocks)) {
        return [];
    }

    const blocks: WidgetState["blocks"] = [];

    for (const [index, raw] of raw_blocks.entries()) {
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

    const kind = String(turn?.kind || "") as WidgetState["kind"];
    const statusRaw = String(turn?.status || ed.status || "running");

    const parsed = widget_state_schema.safeParse({
        version: ed.version ?? 2,
        display: ed.display ?? "card_only",

        kind:
            kind === "thinking" ||
            kind === "tool" ||
            kind === "final" ||
            kind === "error" ||
            kind === "plan" ||
            kind === "decision" ||
            kind === "ask" ||
            kind === "budget" ||
            kind === "policy"
                ? kind
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
        blocks: normalize_turn_blocks(turn?.blocks),

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
        return parsed.data;
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
            has_input: state.blocks.length === 0 && state.input !== "",
            has_output: state.blocks.length === 0 && state.output !== "",
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
