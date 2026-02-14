import assert from "minimalistic-assert";
import * as z from "zod/mini";

import render_widgets_sp_ai_widget from "../templates/widgets/sp_ai_widget.hbs";

import * as blueslip from "./blueslip.ts";
import type {Message} from "./message_store.ts";
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
    title: z.string(),
    caption: z.string(),
    status: z.enum(["running", "ok", "error"]),
    tool: z.string(),
    input: z.string(),
    output: z.string(),
});

type WidgetState = z.infer<typeof widget_state_schema>;

function normalize_extra_data(extra_data: SpAiWidgetExtraData): WidgetState {
    const parsed = widget_state_schema.safeParse({
        version: extra_data.version ?? 1,
        display: extra_data.display ?? "card_only",
        title: extra_data.title ?? "Agent output",
        caption: extra_data.caption ?? "",
        status: extra_data.status ?? "running",
        tool: extra_data.tool ?? "",
        input: extra_data.input ?? "",
        output: extra_data.output ?? "",
    });

    if (parsed.success) {
        return parsed.data;
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
